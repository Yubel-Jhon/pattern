# -*- coding: utf-8 -*-
"""DashScope 公共客户端：生图 i2i + VLM 理解。key 只从环境变量读，永不写死。

实测坑（[[qwen-image-i2i-dashscope]] / 服装企划 agent qwen_client.py 验证过）：
  - i2i 用 qwen-image-3.0-pro，multimodal-generation 端点，同步为主（async 头 403）
  - image 必须带 data URI 前缀（data:image/jpeg;base64,...），裸 base64 报 400
  - parameters 要带 watermark=False（水印会毁提取）、prompt_extend=False（保提示词约束）
  - 返回 task_id 则转轮询（实测单张 1-3 分钟，post_timeout 拉到 600s）
  - 全局代理常死：用空 ProxyHandler opener 直连
  - 401/403/4xx 读响应体翻译成人话（key 无效/欠费/无权限）
"""
from __future__ import annotations

import base64
import io
import json
import os
import time
import urllib.error
import urllib.request

from PIL import Image

API = "https://dashscope.aliyuncs.com/api/v1"
GENERATE = f"{API}/services/aigc/multimodal-generation/generation"
DEFAULT_MODEL = "qwen-image-3.0-pro"

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 直连，绕死代理


class DashScopeError(RuntimeError):
    pass


def _friendly(e: urllib.error.HTTPError) -> DashScopeError:
    try:
        detail = e.read().decode("utf-8", "replace")[:300]
    except Exception:
        detail = ""
    if e.code == 401:
        return DashScopeError(f"[API key 无效] 检查 DASHSCOPE_API_KEY 是否完整。API 返回：{detail}")
    if e.code == 403:
        low = detail.lower()
        if "arrearage" in low or "quota" in low or "余额" in detail or "欠费" in detail:
            return DashScopeError(f"[账户余额不足] 去阿里云百炼控制台充值。API 返回：{detail}")
        return DashScopeError(f"[API 拒绝访问] 可能欠费/未开通该模型/key 无权限。API 返回：{detail}")
    return DashScopeError(f"[API 请求失败] HTTP {e.code}：{detail}")


def _key() -> str:
    key = os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        raise DashScopeError("DASHSCOPE_API_KEY not set")
    return key


def _http(url: str, body: dict | None, headers: dict, timeout: int,
          retries: int = 2) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    last: Exception = DashScopeError("unknown")
    for i in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with _OPENER.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code == 503 or 500 <= e.code < 600:
                time.sleep(5 * (2 ** i))  # 过载退避重试
                last = e
                continue
            raise _friendly(e) from e
        except urllib.error.URLError as e:
            last = DashScopeError(f"[网络错误] DashScope 直连失败（代理/防火墙？）：{e.reason}")
            time.sleep(3 * (2 ** i))
        except Exception as e:
            last = e
            time.sleep(3 * (2 ** i))
    raise last


def _b64(img: Image.Image, max_side: int = 1280) -> str:
    """图 → data URI，压到 max_side 内（提速上传，参考图不需要原图分辨率）。"""
    im = img.convert("RGB")
    if max(im.size) > max_side:
        s = max_side / max(im.size)
        im = im.resize((int(im.width * s), int(im.height * s)), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _download(url: str) -> Image.Image:
    req = urllib.request.Request(url, headers={"User-Agent": "pattern-workshop"})
    with _OPENER.open(req, timeout=120) as r:
        return Image.open(io.BytesIO(r.read())).convert("RGB")


def _extract_images(output: dict) -> list:
    urls = []
    for ch in output.get("choices", []):
        for item in ch.get("message", {}).get("content", []):
            if isinstance(item, dict) and item.get("image"):
                urls.append(item["image"])
    for item in output.get("results", []):
        if isinstance(item, dict) and item.get("url"):
            urls.append(item["url"])
    return urls


def _poll(task_id: str, timeout_s: int = 600) -> Image.Image:
    url = f"{API}/tasks/{task_id}"
    headers = {"Authorization": f"Bearer {_key()}"}
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        time.sleep(3)
        try:
            resp = _http(url, None, headers, timeout=30, retries=1)
        except Exception:
            continue  # 轮询偶发抖动，跳过继续等
        output = resp.get("output", {})
        status = output.get("task_status")
        if status == "SUCCEEDED":
            urls = _extract_images(output)
            if urls:
                return _download(urls[0])
            raise DashScopeError("任务成功但无图片 URL")
        if status in ("FAILED", "CANCELED", "UNKNOWN"):
            raise DashScopeError(f"task {status}: {json.dumps(output, ensure_ascii=False)[:300]}")
    raise DashScopeError("task 轮询超时（10 分钟）")


def qwen_i2i(images: list, prompt: str, timeout_s: int = 600,
             retries: int = 2, model: str = DEFAULT_MODEL, size: str | None = None) -> Image.Image:
    """图生图（文+图）。images: [PIL.Image]。返回生成的 PIL 图（RGB）。
    size：输出尺寸（如 "1140*1472"），None=模型默认。高清修复用最近比例档保构图。"""
    content = [{"image": _b64(im)} for im in images[:3]] + [{"text": prompt}]
    params = {"n": 1, "prompt_extend": False, "prompt_extend_mode": "direct",
              "watermark": False, "enable_thinking": False}
    if size:
        params["size"] = size
    body = {
        "model": model,
        "input": {"messages": [{"role": "user", "content": content}]},
        "parameters": params,
    }
    auth = {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"}
    last: Exception = DashScopeError("unknown")
    for attempt in range(retries + 1):
        try:
            resp = _http(GENERATE, body, auth, timeout=timeout_s, retries=1)
            output = resp.get("output", {})
            if output.get("task_id"):
                return _poll(output["task_id"], timeout_s)
            urls = _extract_images(output)
            if urls:
                return _download(urls[0])
            last = DashScopeError(f"响应无图片：{json.dumps(resp, ensure_ascii=False)[:300]}")
        except DashScopeError as e:
            if "API" in str(e) or "key" in str(e) or "余额" in str(e):
                raise  # 参数/权限类错误重试无意义
            last = e
            time.sleep(5)
    raise last


def vlm_chat(images: list, prompt: str, timeout_s: int = 120,
             model: str = "qwen-vl-max") -> str:
    """VLM 理解（校验比对/语义命名共用）。返回文本。"""
    content = [{"image": _b64(im, max_side=768)} for im in images[:3]] + [{"text": prompt}]
    body = {"model": model, "input": {"messages": [{"role": "user", "content": content}]}}
    auth = {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"}
    resp = _http(GENERATE, body, auth, timeout=timeout_s)
    for ch in resp.get("output", {}).get("choices", []):
        for item in ch.get("message", {}).get("content", []):
            if isinstance(item, dict) and "text" in item:
                return item["text"]
    raise DashScopeError(f"VLM 无文本返回：{json.dumps(resp, ensure_ascii=False)[:300]}")
