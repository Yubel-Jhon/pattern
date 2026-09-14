# -*- coding: utf-8 -*-
"""板块1 · 图案提取 AI 档（L0）：理解图模型定位 + 生图模型重绘独立花稿。

路线（用户拍板 2026-09-14）：纯算法在真实场景图上太差，改用
  1) qwen-vl-max-latest 看图 → 印花定位/类型/颜色（结构化 JSON，0-1000 归一化坐标）
  2) qwen-image-3.0-pro i2i → 按定位把纹样重绘成独立图案（白底，保持原样）
算法档（extract_pattern.py L1/L2/L3）保留作辅助与保底，本脚本为主通道。

用法：
  python ai_extract.py <input> [--name X] [--max-motifs 12] [--out pattern_output]
  python ai_extract.py --batch <dir>            # 批量：一个进程跑全池

stdout 契约：一行 JSON {ok, files, warnings, next_action, motifs: [{name,bbox,size,layer}]}
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import sys
import time
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from patternlib import ensure_workspace, record_step, save_raw, step_dir
from patternlib import edge_clean

API = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
VLM_MODELS = ("qwen3-vl-plus", "qwen-vl-max")  # 账号未开通的别名会 403，逐个回退
I2I_MODEL = "qwen-image-3.0-pro"

LOCATE_PROMPT = """你是服装印花定位专家。先判断图片类型 image_type（四选一）：
white-product=白底产品图（服装平拍/挂拍，背景干净）；scene=场景商拍图（模特穿着，有环境）；
lifestyle=网感实拍图（真人自拍/街拍/车内等随手拍）；fabric-flat=面料平铺图（布料平摊，画面基本全是印花）。
再找出图中的所有印花/图案，只输出 JSON：
{"image_type":"...","garment_type":"...","prints":[{"name":"图案名","bbox_2d":[左,上,右,下],"pattern_type":"graphic|floral|stripe|text|geometric|other","repeat":"single|allover","style":"motif|continuous","colors":["主色"]}]}
style 判据：motif=底布上有可分离的独立花型（花/图形/文字浮在底色上）；continuous=一体满铺、没有"底"的概念（扎染、渐变、笔触肌理、满铺纹理）。
坐标为 0-1000 归一化（相对图宽高）。胸前大图案、独立文字、小标签分开列；照片上叠加的水印/logo/品牌大字（不是印在衣服上的）不要列；没有印花输出 {"prints":[]}。只输出 JSON，不要多余文字。"""

REDRAW_PROMPT = ("输入是一件衣服的照片。把衣服上这个印花图案完整剥离出来，重绘为独立图案："
                 "纯白背景、图案完整居中；严格保持图案的造型、线条、颜色、比例与细节，不做风格化改动；"
                 "不要输出衣服、人体、背景、阴影，也不要保留布料底色。")

SWATCH_PROMPT = ("输入是服装面料局部照片。把这块布料上的印花纹样原样复刻为满幅花型回样："
                 "纹样延续铺满整个画面、直达四边，四角不能留白，不要圆形或方形边框；"
                 "严格保持纹样的造型、颜色、比例与细节；只要印花本身——"
                 "不要衣服、衣领、纽扣、门襟、人体、背景、阴影。")


def _key() -> str:
    import os
    k = os.environ.get("DASHSCOPE_API_KEY", "")
    if not k:
        raise RuntimeError("DASHSCOPE_API_KEY 未设置")
    return k


# 直连 opener：绕开 Windows 系统代理（10809 常死，dashscope_gen.py 同款）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _post(body: dict, timeout: int = 300, retries: int = 2) -> dict:
    req = urllib.request.Request(
        API, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"})
    last = None
    for i in range(retries + 1):
        try:
            with _OPENER.open(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:  # 4xx 重试无意义，带响应体抛出便于排查
            try:
                detail = e.read().decode("utf-8", "replace")[:260]
            except Exception:
                detail = ""
            if e.code < 500:
                raise RuntimeError(f"HTTP {e.code}: {detail}") from e
            last = RuntimeError(f"HTTP {e.code}: {detail}")
            time.sleep(2 * (i + 1))
        except Exception as e:  # 同步接口偶发超时，重试
            last = e
            time.sleep(2 * (i + 1))
    raise last


def _b64(path: Path, max_side: int = 1600) -> str:
    img = Image.open(path)
    img.load()
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=88)
    return base64.b64encode(buf.getvalue()).decode()


def _parse_json_text(t: str) -> dict:
    t = t.strip()
    if "```" in t:  # 剥掉 markdown 围栏
        t = t.split("```")[1]
        if t.startswith("json"):
            t = t[4:]
    start, end = t.find("{"), t.rfind("}")
    return json.loads(t[start:end + 1])


def vlm_locate(img_path: Path) -> dict:
    """理解图：返回 {"garment_type", "prints":[{name,bbox_2d(0-1000),...}]}"""
    b64 = f"data:image/jpeg;base64,{_b64(img_path)}"
    last = None
    for model in VLM_MODELS:
        try:
            resp = _post({
                "model": model,
                "input": {"messages": [{"role": "user", "content": [
                    {"image": b64}, {"text": LOCATE_PROMPT}]}]},
                "parameters": {"temperature": 0.1, "max_tokens": 1600},
            })
            text = resp["output"]["choices"][0]["message"]["content"]
            if isinstance(text, list):
                text = "".join(p.get("text", "") for p in text)
            out = _parse_json_text(text)
            out["_model"] = model
            return out
        except Exception as e:
            last = e
    raise last


def remove_bg(img: Image.Image, tol: int = 42) -> tuple:
    """边缘连通色键去底：只抠与边缘连通的底色，图案内部同色块（白花/白书页）不受影响。
    返回 (RGBA, 是否执行)。底色=边框颜色众数（量化 24/步，抗花纹污染）；
    边框底色覆盖率 <20% 判满幅无底，不硬抠。"""
    from scipy import ndimage
    import numpy as np
    rgb = np.asarray(img.convert("RGB"), dtype=np.float32)
    ring = 8
    border = np.concatenate([rgb[:ring].reshape(-1, 3), rgb[-ring:].reshape(-1, 3),
                             rgb[:, :ring].reshape(-1, 3), rgb[:, -ring:].reshape(-1, 3)])
    q = (border / 24).astype(int)  # 众数色：底色即便被花纹部分污染也能找对
    keys, counts = np.unique(q, axis=0, return_counts=True)
    med = (keys[counts.argmax()] + 0.5) * 24
    bg = np.sqrt(((rgb - med) ** 2).sum(-1)) < tol
    frac = bg[:ring, :].mean() + bg[-ring:, :].mean() + bg[:, :ring].mean() + bg[:, -ring:].mean()
    if frac / 4 < 0.2:  # 边缘几乎全是花纹 → 满幅无底概念
        return img.convert("RGBA"), False
    # 连通判定放宽到 1.6×tol：底色能穿过细缝扩散进封闭口袋（花丛间的残留底色），
    # 图案内部被描边包死的同色块（书页/白花瓣）依然安全
    near = np.sqrt(((rgb - med) ** 2).sum(-1)) < tol * 1.6
    seed = np.zeros_like(bg)
    seed[:ring, :] = bg[:ring, :]
    seed[-ring:, :] = bg[-ring:, :]
    seed[:, :ring] |= bg[:, :ring]
    seed[:, -ring:] |= bg[:, -ring:]
    filled = ndimage.binary_propagation(seed, mask=near)  # 从边缘在近底色区内传播
    a = np.where(filled, 0, 255).astype(np.uint8)
    rgba = img.convert("RGBA")
    rgba.putalpha(Image.fromarray(a))
    return rgba, True


def i2i_redraw(crop_b64: str, size: str = "1024*1024", prompt: str = REDRAW_PROMPT) -> bytes | None:
    """生图：把裁出的印花重绘成独立花稿，返回图片字节。"""
    resp = _post({
        "model": I2I_MODEL,
        "input": {"messages": [{"role": "user", "content": [
            {"image": f"data:image/jpeg;base64,{crop_b64}"},
            {"text": prompt}]}]},
        "parameters": {"size": size, "n": 1, "watermark": False, "prompt_extend": False},
    })
    out = resp.get("output", {})
    content = out.get("choices", [{}])[0].get("message", {}).get("content", [])
    url = None
    if isinstance(content, list):
        for p in content:
            if isinstance(p, dict) and p.get("image"):
                url = p["image"]
                break
    if not url:
        return None
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with _OPENER.open(req, timeout=120) as r:
        return r.read()


def extract_ai(input_path: Path, name: str, out_root: Path, max_motifs: int, bg: str = "auto") -> dict:
    t0 = time.time()
    img = Image.open(input_path)
    img.load()
    warnings = []
    ws = ensure_workspace(out_root, name)
    save_raw(ws, input_path)
    out_dir = step_dir(ws, "extract")

    # 1) 理解图定位
    try:
        loc = vlm_locate(input_path)
    except Exception as e:
        return {"ok": False, "error": f"看图定位失败：{e.__class__.__name__}: {e}",
                "seconds": round(time.time() - t0, 1)}
    prints = loc.get("prints", [])[:max_motifs]
    img_type = loc.get("image_type", "")
    if not prints and img_type != "fabric-flat":
        return {"ok": False, "error": "看图模型未找到印花", "locate": loc,
                "seconds": round(time.time() - t0, 1)}
    if not prints:  # fabric-flat 且 VLM 未列花型：整张即印花，按一体满铺处理
        prints = [{"name": "满幅印花", "bbox_2d": [0, 0, 1000, 1000], "pattern_type": "allover",
                   "repeat": "allover", "style": "continuous", "colors": []}]

    # 2) 按图片类型分诊 → 裁剪 → 并发生图重绘（i2i 单次 60-90s，串行太慢）
    #    fabric-flat：整张画面就是印花，全幅直出；allover：中心面料小块防"整衣端出"；
    #    single：bbox 裁剪独立重绘
    motifs, files_out = [], []
    W, H = img.size
    jobs = []  # (idx, name, pattern_type, colors, bbox_px, crop_b64, crop_box, prompt)
    for i, p in enumerate(prints, 1):
        x1, y1, x2, y2 = p.get("bbox_2d", [0, 0, 1000, 1000])
        px1, py1 = max(0, int(x1 / 1000 * W)), max(0, int(y1 / 1000 * H))
        px2, py2 = min(W, int(x2 / 1000 * W)), min(H, int(y2 / 1000 * H))
        pad = int(max(px2 - px1, py2 - py1) * 0.08)
        crop_box = (max(0, px1 - pad), max(0, py1 - pad), min(W, px2 + pad), min(H, py2 + pad))
        if crop_box[2] - crop_box[0] < 20 or crop_box[3] - crop_box[1] < 20:
            warnings.append(f"M{i:02d} 框过小已跳过：{p.get('name')}")
            continue
        if img_type == "fabric-flat":
            crop, prompt = img, SWATCH_PROMPT  # 整张就是印花，全幅直出不裁
            if max(crop.size) > 1600:  # 全幅直出护栏：控制上传体积
                crop = crop.copy()
                crop.thumbnail((1600, 1600))
        elif p.get("repeat") == "allover" or \
                (crop_box[2] - crop_box[0]) * (crop_box[3] - crop_box[1]) >= 0.35 * W * H:
            # 满印：取中心面料小块 + 花型回样提示词，防止生图端把整件衣服照着画
            # 内收 25%：压掉领口/门襟/袖窿被带进裁剪块的概率（18% 时 w2 撞出领口）
            x1, y1, x2, y2 = crop_box
            ix, iy = int((x2 - x1) * 0.25), int((y2 - y1) * 0.25)
            crop = img.crop((x1 + ix, y1 + iy, x2 - ix, y2 - iy))
            prompt = SWATCH_PROMPT
        else:
            crop = img.crop(crop_box)
            prompt = REDRAW_PROMPT
        buf = io.BytesIO()
        crop.convert("RGB").save(buf, format="JPEG", quality=90)
        jobs.append((i, p.get("name", ""), p.get("pattern_type", ""), p.get("colors", []),
                     [px1, py1, px2, py2], base64.b64encode(buf.getvalue()).decode(),
                     crop_box, prompt, p.get("style", "motif")))

    def _retry_tight(crop_box, prompt):
        """审核拦截后的兜底：向内收缩 8% 重裁（去外扩、避开人体皮肤）再试。"""
        x1, y1, x2, y2 = crop_box
        dx, dy = int((x2 - x1) * 0.08), int((y2 - y1) * 0.08)
        tight = img.crop((x1 + dx, y1 + dy, x2 - dx, y2 - dy))
        buf = io.BytesIO()
        tight.convert("RGB").save(buf, format="JPEG", quality=90)
        return i2i_redraw(base64.b64encode(buf.getvalue()).decode(), prompt=prompt)

    def _job(j):
        idx, name, ptype, colors, bbox_px, b64, crop_box, prompt, _style = j
        try:
            data = i2i_redraw(b64, prompt=prompt)
            err = None if data else "生图未返回图片"
        except Exception as e:
            data, err = None, f"生图失败({e})"
            if "DataInspectionFailed" in str(e) and crop_box:
                try:  # 绿网拦的多是带进来的皮肤/人体，收紧裁剪重试一次
                    data = _retry_tight(crop_box, prompt)
                    err = None if data else "生图未返回图片"
                    if data:
                        warnings.append(f"M{idx:02d} 审核拦截，收紧裁剪重试成功")
                except Exception as e2:
                    err = f"生图失败({e2})"
        return j, data, err

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        for j, data, err in pool.map(_job, jobs):
            idx, name, ptype, colors, bbox_px, _b64, _cb, _pr, style = j
            if err:
                warnings.append(f"M{idx:02d} {err}：{name}")
                continue
            fp = out_dir / f"motif_{idx:02d}.png"
            fp.write_bytes(data)
            bg_state = "kept"
            # 去底：remove 强制去；auto 按 VLM 判型（motif=有底可去 / continuous=一体不去）
            if bg == "remove" or (bg == "auto" and style == "motif"):
                im = Image.open(io.BytesIO(data))
                im.load()
                rgba, did = remove_bg(im)
                if did:
                    rgba = edge_clean(rgba)
                    rgba.save(fp)
                    bg_state = "removed"
                else:
                    warnings.append(f"M{idx:02d} 边缘不均匀，未去底：{name}")
            files_out.append(fp)
            motifs.append({"id": f"M{idx:02d}", "name": name, "pattern_type": ptype,
                           "bbox": bbox_px, "size": [1024, 1024],
                           "colors": colors, "layer": "L0", "bg": bg_state})
    motifs.sort(key=lambda m: m["id"])
    if not motifs:
        return {"ok": False, "error": "所有印花生图均失败", "warnings": warnings,
                "seconds": round(time.time() - t0, 1)}

    # 3) 预览：标注框 + 标准件一览
    base = img.convert("RGB")
    scale = min(1.0, 1100 / max(base.size))
    if scale < 1.0:
        base = base.resize((int(base.width * scale), int(base.height * scale)))
    d = ImageDraw.Draw(base)
    for m in motifs:
        x1, y1, x2, y2 = [int(v * scale) for v in m["bbox"]]
        d.rectangle([x1, y1, x2, y2], outline=(255, 40, 40), width=3)
        d.text((x1 + 3, y1 + 3), f"{m['id']} {m['pattern_type']}", fill=(255, 40, 40))
    base.save(out_dir / "preview_annotated.jpg", quality=88)
    files_out.append(out_dir / "preview_annotated.jpg")

    from PIL import Image as _I
    cell, cols = 190, min(6, len(motifs))
    rows = (len(motifs) + cols - 1) // cols
    sheet = _I.new("RGB", (cols * cell, rows * (cell + 22)), (24, 24, 26))
    ds = ImageDraw.Draw(sheet)
    for i, m in enumerate(motifs, 1):
        im = _I.open(out_dir / f"motif_{m['id'][1:4]}.png")
        im.thumbnail((cell - 10, cell - 10))
        x, y = ((i - 1) % cols) * cell, ((i - 1) // cols) * (cell + 22)
        mk = im if im.mode == "RGBA" else None  # 透明底件用自身当蒙版贴
        sheet.paste(im, (x + (cell - im.width) // 2, y + (cell - im.height) // 2), mk)
        tag = " ·透明" if m.get("bg") == "removed" else ""
        ds.text((x + 6, y + cell + 4), f"{m['id']} {m['name'][:12]}{tag}", fill=(230, 230, 230))
    sheet.save(out_dir / "preview_contact.jpg", quality=88)
    files_out.append(out_dir / "preview_contact.jpg")

    meta = {"source": str(input_path), "type": "garment", "layer": "L0",
            "vlm_model": loc.get("_model", ""), "i2i_model": I2I_MODEL,
            "image_type": img_type, "bg_mode": bg, "garment_type": loc.get("garment_type", ""),
            "motifs": [{k: m[k] for k in ("id", "name", "pattern_type", "bbox", "size", "colors", "layer")}
                       for m in motifs]}
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    record_step(ws, "extract", {"source": str(input_path), "type": "garment(L0-AI)",
                                "motif_count": len(motifs),
                                "files": [f.name for f in files_out]})

    return {"ok": True, "files": [str(f) for f in files_out], "warnings": warnings,
            "next_action": None, "seconds": round(time.time() - t0, 1),
            "motifs": [{k: m[k] for k in ("id", "name", "bbox", "size", "layer", "bg")}
                       for m in motifs]}


def main() -> int:
    ap = argparse.ArgumentParser(description="板块1 图案提取 AI 档：VLM 定位 + 生图重绘")
    ap.add_argument("input", nargs="?", help="输入图片路径")
    ap.add_argument("--batch", help="批量模式：目录")
    ap.add_argument("--name", default=None)
    ap.add_argument("--max-motifs", dest="max_motifs", type=int, default=12)
    ap.add_argument("--bg", choices=["auto", "remove", "keep"], default="auto",
                    help="去底色：auto=按纹样判型（花纹去底/一体不去）；remove=强制去；keep=不去")
    ap.add_argument("--out", default="pattern_output")
    args = ap.parse_args()
    out_root = Path(args.out).resolve()

    if args.batch:
        root = Path(args.batch)
        images = sorted([p for p in root.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png")
                         and not p.name.startswith("_")])
        results = []
        for i, p in enumerate(images, 1):
            r = extract_ai(p, f"ai_{p.stem}", out_root, args.max_motifs, bg=args.bg)
            n = len(r.get("motifs", [])) if r.get("ok") else 0
            print(f"[{i}/{len(images)}] {p.name} ok={r.get('ok')} motifs={n} {r.get('seconds')}s"
                  + ("" if r.get("ok") else f" err={r.get('error','')[:90]}"), flush=True)
            results.append({"input": p.name, **r})
            (root / "_run_log_ai.json").write_text(
                json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"DONE ok={sum(1 for r in results if r.get('ok'))}/{len(results)}", flush=True)
        return 0

    if not args.input:
        ap.error("需要 input 或 --batch")
    src = Path(args.input)
    if not src.exists():
        print(json.dumps({"ok": False, "error": f"输入不存在：{src}"}, ensure_ascii=False))
        return 1
    r = extract_ai(src, args.name or f"ai_{src.stem}", out_root, args.max_motifs, bg=args.bg)
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
