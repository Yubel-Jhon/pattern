# -*- coding: utf-8 -*-
"""引擎公共件（板块3 首建，后续板块自备副本）：gpt_api | qwen | manual 三实现，统一 generate()。

定位（03-风格延展.md v2）：**GPT 是终点引擎**——qwen i2i 是「编辑原图」心智（构图被保留、
原稿质感甩不掉），适合保真任务（板块1/2 的省钱档）；GPT-Image 是「理解后重新合成」，
适合改设计本身的任务（板块3 风格延展主引擎）。眼睛永远走便宜的 qwen-vl
（分诊/元素清单/出口质检全在 dashscope_gen.vlm_chat），GPT 只花在生图刀刃上。

key/base_url 只从环境变量读，永不写死：
  OPENAI_API_KEY      GPT 档（中国网络建议配中转，直连官方需系统代理可用）
  OPENAI_BASE_URL     中转地址（不配=官方地址 https://api.openai.com/v1）
                      配了中转 → 直连不走系统代理；不配 → 走系统代理（官方域名被墙常态）
  GPT_IMAGE_MODEL     默认 gpt-image-1（出新版改环境变量，代码不写死）
  DASHSCOPE_API_KEY   qwen 对照档 + 全链 VLM

手动档不是降级：engine=manual 时不发任何请求，由调用方出提示词卡，用户在 ChatGPT 网页跑完
回接 --collect（板块2 --gen off 同款三路线）。
"""
from __future__ import annotations

import base64
import io
import json
import os
import time
import urllib.error
import urllib.request
from uuid import uuid4

from PIL import Image

from dashscope_gen import DashScopeError, qwen_i2i

DEFAULT_GPT_BASE = "https://api.openai.com/v1"

# qwen i2i 支持档（pattern-enhance 实测版）：挑长宽比最接近的，压构图漂移
QWEN_SIZES = ["1024*1024", "1440*1440", "1472*1140", "1140*1472",
              "1664*928", "928*1664", "1584*1056", "1056*1584"]
# gpt-image 档：官方仅四种
GPT_SIZES = ("1024x1024", "1536x1024", "1024x1536", "auto")


class EngineError(RuntimeError):
    pass


# ---------- 尺寸 ----------

def pick_gpt_size(w: int, h: int) -> str:
    r = w / h
    if r > 1.2:
        return "1536x1024"
    if r < 0.83:
        return "1024x1536"
    return "1024x1024"


def pick_qwen_size(w: int, h: int) -> str:
    target = w / h
    return min(QWEN_SIZES, key=lambda s: abs(int(s.split("*")[0]) / int(s.split("*")[1]) - target))


# ---------- 能力探测 ----------

def gpt_caps() -> dict:
    return {"key": bool(os.environ.get("OPENAI_API_KEY")),
            "base_url": os.environ.get("OPENAI_BASE_URL", DEFAULT_GPT_BASE),
            "model": os.environ.get("GPT_IMAGE_MODEL", "gpt-image-1"),
            "relay": bool(os.environ.get("OPENAI_BASE_URL"))}


def qwen_caps() -> dict:
    return {"key": bool(os.environ.get("DASHSCOPE_API_KEY")),
            "model": "qwen-image-3.0-pro"}


def resolve_engine(engine: str) -> str | None:
    """auto=GPT 优先（终点引擎），qwen 对照档兜底，都没有 → manual（调用方出卡）。"""
    if engine != "auto":
        return engine
    caps = gpt_caps()
    if caps["key"]:
        return "gpt"
    if qwen_caps()["key"]:
        return "qwen"
    return "manual"


# ---------- GPT 档（images/edits，multipart 手搓，无 SDK 依赖） ----------

def _png_bytes(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.convert("RGBA" if im.mode == "RGBA" else "RGB").save(buf, format="PNG")
    return buf.getvalue()


def _multipart(fields: dict, files: list) -> tuple:
    boundary = "----patternworkshop" + uuid4().hex
    buf = io.BytesIO()
    for k, v in fields.items():
        buf.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode("utf-8"))
    for name, filename, ctype, data in files:
        buf.write((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
                   f"filename=\"{filename}\"\r\nContent-Type: {ctype}\r\n\r\n").encode("utf-8"))
        buf.write(data)
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode("utf-8"))
    return buf.getvalue(), boundary


def _friendly_gpt(e: urllib.error.HTTPError) -> EngineError:
    try:
        detail = e.read().decode("utf-8", "replace")[:300]
    except Exception:
        detail = ""
    if e.code == 401:
        return EngineError(f"[GPT key 无效] 检查 OPENAI_API_KEY。API 返回：{detail}")
    if e.code == 403:
        return EngineError(f"[GPT 拒绝访问] key 无权限/组织未验证/模型未开通。API 返回：{detail}")
    if e.code == 429:
        return EngineError(f"[GPT 限流或额度不足] 稍后重试或充值。API 返回：{detail}")
    return EngineError(f"[GPT 请求失败] HTTP {e.code}：{detail}")


def gpt_edit(images: list, prompt: str, quality: str = "medium", transparent: bool = False,
             size: str | None = None, timeout_s: int = 600, retries: int = 2) -> Image.Image:
    """GPT-Image edits：图 + prompt → 新图。images[0]=花稿本体，其余=风格参考（--ref）。
    中转（OPENAI_BASE_URL）直连；官方域名走系统代理（代理死是常态，建议配中转）。"""
    caps = gpt_caps()
    if not caps["key"]:
        raise EngineError("OPENAI_API_KEY not set")
    fields = {"model": caps["model"], "prompt": prompt, "n": "1",
              "quality": quality, "output_format": "png",
              "size": size or "auto",
              "background": "transparent" if transparent else "auto"}
    files = [("image[]", f"img_{i}.png", "image/png", _png_bytes(im))
             for i, im in enumerate(images)]
    body, boundary = _multipart(fields, files)
    req = urllib.request.Request(
        f"{caps['base_url'].rstrip('/')}/images/edits", data=body, method="POST",
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    if caps["relay"]:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 中转直连
    else:
        opener = urllib.request.build_opener()  # 官方：交给系统代理环境变量
    last: Exception = EngineError("unknown")
    for attempt in range(retries + 1):
        try:
            with opener.open(req, timeout=timeout_s) as r:
                resp = json.loads(r.read().decode("utf-8"))
            b64 = (resp.get("data") or [{}])[0].get("b64_json")
            if not b64:
                raise EngineError(f"GPT 响应无图：{json.dumps(resp, ensure_ascii=False)[:300]}")
            img = Image.open(io.BytesIO(base64.b64decode(b64)))
            img.load()
            return img
        except urllib.error.HTTPError as e:
            if e.code == 429 or 500 <= e.code < 600:
                last = _friendly_gpt(e)
                time.sleep(8 * (2 ** attempt))
                continue
            raise _friendly_gpt(e) from e
        except urllib.error.URLError as e:
            last = EngineError(f"[网络错误] GPT 端点不可达（官方域名需系统代理，建议配 OPENAI_BASE_URL 中转）：{e.reason}")
            time.sleep(5 * (2 ** attempt))
        except EngineError as e:
            last = e
            time.sleep(5)
    raise last


# ---------- 统一入口 ----------

def generate(images: list, prompt: str, engine: str, quality: str = "draft",
             transparent: bool = False, size: str | None = None,
             timeout_s: int = 600) -> tuple:
    """唯一生图接口。返回 (PIL.Image, meta)；engine='manual' 由调用方处理，不走这里。
    quality: draft→gpt medium（探索档，便宜）/ final→gpt high（成品档）；qwen 忽略该参数。"""
    t0 = time.time()
    if engine == "gpt":
        q = quality if quality in ("low", "medium", "high", "auto") else ("high" if quality == "final" else "medium")
        img = gpt_edit(images, prompt, quality=q, transparent=transparent,
                       size=size, timeout_s=timeout_s)
        return img, {"engine": "gpt", "model": gpt_caps()["model"], "quality": q,
                     "size": size or "auto", "seconds": round(time.time() - t0, 1)}
    if engine == "qwen":
        img = qwen_i2i(images, prompt, timeout_s=timeout_s, size=size)
        return img, {"engine": "qwen", "model": "qwen-image-3.0-pro",
                     "quality": "fixed", "size": size or "default",
                     "seconds": round(time.time() - t0, 1)}
    raise EngineError(f"未知引擎：{engine}（gpt | qwen | manual）")
