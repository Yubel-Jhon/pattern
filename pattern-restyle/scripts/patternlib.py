# -*- coding: utf-8 -*-
"""pattern-workshop 公共库：工作区约定 / meta 台账 / 预览 / 边缘清理 / JSON 输出契约。

所有脚本共用，保证跨板块产物规格一致（联动因此自动成立）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# 板块 → 落盘目录（唯一事实源，别处不许再写死）
STEP_DIRS = {
    "raw": "00_raw",
    "extract": "01_extracted",
    "hd": "02_hd",
    "restyle": "03_restyle",
    "variants": "04_variants",
    "seamless": "05_seamless",
    "colorways": "06_colorways",
}

MIN_INPUT_SIDE = 512  # 低于此建议先走高清修复板块


# ---------- 工作区 ----------

def sanitize_name(name: str) -> str:
    """允许中文；去掉路径分隔符和空格。"""
    name = re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("._")
    return name or "pattern"


def ensure_workspace(out_root: Path, name: str) -> Path:
    ws = out_root / sanitize_name(name)
    for d in STEP_DIRS.values():
        (ws / d).mkdir(parents=True, exist_ok=True)
    return ws


def step_dir(ws: Path, step: str) -> Path:
    return ws / STEP_DIRS[step]


# ---------- meta 台账 ----------

def load_meta(ws: Path) -> dict:
    f = ws / "meta.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    return {}


def save_meta(ws: Path, meta: dict) -> None:
    meta.setdefault("created", datetime.now().isoformat(timespec="seconds"))
    meta["updated"] = datetime.now().isoformat(timespec="seconds")
    (ws / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def record_step(ws: Path, step: str, data: dict) -> None:
    meta = load_meta(ws)
    meta.setdefault("steps", {})[step] = {"at": datetime.now().isoformat(timespec="seconds"), **data}
    save_meta(ws, meta)


def save_raw(ws: Path, input_path: Path) -> Path:
    dst = step_dir(ws, "raw") / input_path.name
    if input_path.resolve() != dst.resolve():
        dst.write_bytes(input_path.read_bytes())
    return dst


# ---------- 图像处理 ----------

def content_crop(rgba: Image.Image, pad_ratio: float = 0.02) -> Image.Image:
    """按 alpha 内容 bbox 裁切 + 2% padding。无内容则原样返回。"""
    a = np.array(rgba.split()[-1])
    ys, xs = np.where(a > 8)
    if len(xs) == 0:
        return rgba
    x1, x2 = xs.min(), xs.max()
    y1, y2 = ys.min(), ys.max()
    px, py = int((x2 - x1) * pad_ratio), int((y2 - y1) * pad_ratio)
    box = (max(0, x1 - px), max(0, y1 - py), min(rgba.width, x2 + px + 1), min(rgba.height, y2 + py + 1))
    return rgba.crop(box)


def edge_clean(rgba: Image.Image, erode: int = 1, feather: float = 1.0,
               alpha_lo: int = 8, alpha_hi: int = 250) -> Image.Image:
    """边缘清理：alpha 阈值化 → 收缩 erode 像素（杀白边）→ 羽化。"""
    r, g, b, a = rgba.convert("RGBA").split()
    a_np = np.array(a)
    a_np = np.where(a_np < alpha_lo, 0, np.where(a_np > alpha_hi, 255, a_np)).astype(np.uint8)
    a2 = Image.fromarray(a_np)
    for _ in range(erode):
        a2 = a2.filter(ImageFilter.MinFilter(3))
    if feather:
        a2 = a2.filter(ImageFilter.GaussianBlur(feather))
    return Image.merge("RGBA", (r, g, b, a2))


def on_white(rgba: Image.Image) -> Image.Image:
    """透明底合成到白底（预览/白底副本用）。"""
    base = Image.new("RGB", rgba.size, (255, 255, 255))
    if rgba.mode == "RGBA":
        base.paste(rgba, mask=rgba.split()[-1])
    else:
        base.paste(rgba.convert("RGB"))
    return base


def make_sidebyside_preview(before: Image.Image, after_rgba: Image.Image,
                            max_dim: int = 768, left_label: str = "ORIGINAL",
                            right_label: str = "CUTOUT") -> Image.Image:
    """并排预览：原图 | 结果(白底)，底部 ASCII 标注（不依赖系统字体）。"""
    lb = before.convert("RGB") if before.mode != "RGB" else before
    rb = on_white(after_rgba)
    for img in (lb, rb):
        img.thumbnail((max_dim, max_dim))
    w = lb.width + rb.width + 12
    h = max(lb.height, rb.height) + 24
    canvas = Image.new("RGB", (w, h), (255, 255, 255))
    canvas.paste(lb, (0, 0))
    canvas.paste(rb, (lb.width + 12, 0))
    d = ImageDraw.Draw(canvas)
    d.text((4, h - 18), left_label, fill=(60, 60, 60))
    d.text((lb.width + 16, h - 18), right_label, fill=(60, 60, 60))
    return canvas


# ---------- 输出契约 ----------

def emit(ok: bool, files: list, warnings: list = None, next_action: str = None, **extra) -> None:
    """所有脚本的 stdout 契约：一行 JSON。失败也走这里 + 退出码 1。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    payload = {"ok": ok, "files": [str(f) for f in files],
               "warnings": warnings or [], "next_action": next_action}
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False))


def check_input_size(img: Image.Image) -> Optional[str]:
    short = min(img.size)
    if short < MIN_INPUT_SIDE:
        return (f"输入短边仅 {short}px (<{MIN_INPUT_SIDE})，建议先跑 02-高清修复板块再提取，"
                f"否则边缘质量有限")
    return None


# ---------- v3：能力探测 / 分诊 / 轻代码补位 ----------

COLOR_WORDS = [("red", 0, 15), ("orange", 15, 45), ("yellow", 45, 70),
               ("green", 70, 160), ("cyan", 160, 200), ("blue", 200, 255),
               ("purple", 255, 290), ("pink", 290, 335), ("red", 335, 360)]


def find_realesrgan() -> Optional[str]:
    """像素接力引擎探测（02 板块 §10）：skill tools/ → 环境变量 REALESRGAN_BIN → PATH。"""
    root = Path(__file__).resolve().parent.parent
    for p in sorted(root.glob("tools/realesrgan*/realesrgan-ncnn-vulkan.exe")):
        return str(p)
    env = os.environ.get("REALESRGAN_BIN")
    if env and Path(env).exists():
        return env
    return shutil.which("realesrgan-ncnn-vulkan")


def capabilities() -> dict:
    """能力探测：skill 三路线走哪条的依据（check_env 与各脚本共用）。"""
    caps = {"gen_api": None, "vlm": None, "pixel_tier": {}, "vision_agent": None, "hd": {}}
    key = os.environ.get("DASHSCOPE_API_KEY")
    if key:
        caps["gen_api"] = "dashscope(qwen-image-edit)"
        caps["vlm"] = "dashscope(qwen-vl)"
    ncnn = find_realesrgan()
    caps["hd"] = {"ncnn": ncnn, "lanczos": True,
                  "method": "ncnn_vulkan" if ncnn else "lanczos_only"}
    mods = {}
    for m in ("skimage", "imagehash", "rembg", "ultralytics"):
        try:
            __import__(m)
            mods[m] = True
        except Exception:
            mods[m] = False
    caps["pixel_tier"] = {"ready": mods["skimage"] and mods["imagehash"],
                          "skimage": mods["skimage"], "imagehash": mods["imagehash"],
                          "rembg": mods["rembg"], "fastsam": mods["ultralytics"]}
    return caps


def capabilities_next_action(caps: dict) -> Optional[str]:
    """冷启动引导：缺什么给什么接入路线图（永不硬失败的落地）。"""
    if caps["gen_api"]:
        return None
    if caps["pixel_tier"]["ready"]:
        return ("未检测到生图 API key（DASHSCOPE_API_KEY）。三种接法任选："
                "① 申请 DashScope key 后 export DASHSCOPE_API_KEY=sk-xxx（全自动生图主链）；"
                "② 手动模式：让 agent 按 scripts/prompt_templates.py 的模板给你提示词，"
                "在任意生图工具（网页版 GPT/Nano Banana 均可）生成后把结果图路径传给 --gen-image 继续打包；"
                "③ 本次自动走保真档（离线分割，像素级）。")
    return ("未检测到生图 API key，且保真档依赖缺失（pip install scikit-image imagehash）。"
            "推荐：pip install scikit-image imagehash，或申请 DashScope key 走生图主链。")


def triage(rgb: Image.Image) -> dict:
    """定量体检：清晰度 / 均匀底 / 前景占比 → 分诊提示（A/B/C/D + 异常信号）。"""
    from skimage.filters import laplace
    g = np.asarray(rgb.convert("L"), dtype=float) / 255.0
    clarity = float(laplace(g).var())
    rgbA = np.asarray(rgb.convert("RGB"), dtype=np.int16)
    h, w, _ = rgbA.shape
    ring = max(4, min(h, w) // 40)
    border = np.concatenate([rgbA[:ring].reshape(-1, 3), rgbA[-ring:].reshape(-1, 3),
                             rgbA[:, :ring].reshape(-1, 3), rgbA[:, -ring:].reshape(-1, 3)])
    bg_med = np.median(border, axis=0)
    bg_std = float(border.std(axis=0).mean())
    dist = np.sqrt(((rgbA - bg_med) ** 2).sum(-1))
    fg_ratio = float((dist > 60).mean())
    if fg_ratio > 0.62:
        hint = "fabric"          # D：满铺/整图即花纹域
    elif bg_std < 18:
        hint = "product"         # A：均匀浅底产品图
    else:
        hint = "scene"           # B：场景图（C 网感需用户/agent 声明 --type vibe）
    warnings = []
    if clarity < 0.0003:  # 阈值按实测调过：平坦合成图/扁平插画风 ≈0.001 属正常，真模糊图 <0.0003
        warnings.append(f"清晰度低（laplacian var={clarity:.4f}）→ 异常分支：建议先走板块2高清修复")
    return {"clarity": round(clarity, 5), "bg_std": round(bg_std, 2),
            "fg_ratio": round(fg_ratio, 3), "type_hint": hint, "warnings": warnings}


def trim_frame(img: Image.Image, tol: int = 12) -> Image.Image:
    """裁掉生图模型爱加的均匀白边/画框（逐边收缩直到颜色明显变化）。"""
    rgb = np.asarray(img.convert("RGB"), dtype=np.int16)
    corner = np.median(np.concatenate([rgb[:8, :8].reshape(-1, 3), rgb[-8:, -8:].reshape(-1, 3)]), axis=0)
    top = 0
    while top < img.height // 3 and np.sqrt(((rgb[top] - corner) ** 2).sum(-1)).mean() < tol:
        top += 1
    bot = img.height
    while bot > img.height * 2 // 3 and np.sqrt(((rgb[bot - 1] - corner) ** 2).sum(-1)).mean() < tol:
        bot -= 1
    left = 0
    while left < img.width // 3 and np.sqrt(((rgb[:, left] - corner) ** 2).sum(-1)).mean() < tol:
        left += 1
    right = img.width
    while right > img.width * 2 // 3 and np.sqrt(((rgb[:, right - 1] - corner) ** 2).sum(-1)).mean() < tol:
        right -= 1
    if right - left > 32 and bot - top > 32:
        return img.crop((left, top, right, bot))
    return img


def split_motifs(rgba: Image.Image, min_size: int = 32, merge_px: int = 6) -> list:
    """连通域拆分：alpha 膨胀并组（花瓣+花芯+叶=一件）→ 逐件裁切。
    返回 [{img(RGBA), bbox(x1,y1,x2,y2), npix}]，过滤 min_size 以下噪点。"""
    from skimage.measure import label, regionprops
    from skimage.morphology import dilation, disk
    a = np.array(rgba.split()[-1]) > 8
    if not a.any():
        return []
    lab = label(dilation(a, disk(merge_px)))  # dilation 即 morphology 版，消 FutureWarning
    out = []
    for r in regionprops(lab):
        y1, x1, y2, x2 = r.bbox
        if min(x2 - x1, y2 - y1) < min_size:
            continue
        crop = rgba.crop((x1, y1, x2, y2))
        if np.array(crop.split()[-1]).max() == 0:
            continue
        out.append({"img": crop, "bbox": (int(x1), int(y1), int(x2), int(y2)),
                    "npix": int((a[y1:y2, x1:x2] & (lab[y1:y2, x1:x2] == r.label)).sum())})
    out.sort(key=lambda m: -m["npix"])
    return out


def dedup_motifs(motifs: list, threshold: int = 8) -> list:
    """phash 判重：相似个体归并，每类留最完整实例为标准件，记重复次数与位置。"""
    import imagehash
    clusters = []
    for m in motifs:
        ph = imagehash.phash(on_white(m["img"]), hash_size=8)
        hit = None
        for c in clusters:
            if ph - c["ph"] <= threshold:
                hit = c
                break
        if hit:
            hit["count"] += 1
            hit["positions"].append(m["bbox"])
        else:
            clusters.append({"ph": ph, "img": m["img"], "bbox": m["bbox"],
                             "count": 1, "positions": [m["bbox"]]})
    for c in clusters:
        c.pop("ph")
    return clusters


def local_name(rgba: Image.Image, idx: int) -> str:
    """本地规则命名：主色相 + 序号（零依赖，无 API 也能用）。"""
    hsv = np.asarray(on_white(rgba).convert("HSV"), dtype=np.uint8)
    a = np.array(rgba.split()[-1]) > 128
    if not a.any():
        return f"motif_{idx:02d}"
    hues = hsv[..., 0][a].astype(int) * 360 // 256
    sat = hsv[..., 1][a].astype(int)
    word = "neutral"
    if sat.mean() > 40:
        hmed = int(np.median(hues))
        for wname, lo, hi in COLOR_WORDS:
            if lo <= hmed < hi:
                word = wname
                break
    return f"{word}_{idx:02d}"


def contact_sheet(clusters: list, names: list, cell: int = 200) -> Image.Image:
    """标准件一览表：平铺 + 重复次数标注。"""
    cols = min(4, max(1, len(clusters)))
    rows = (len(clusters) + cols - 1) // cols if clusters else 1
    canvas = Image.new("RGB", (cols * cell, rows * (cell + 20)), (250, 250, 250))
    d = ImageDraw.Draw(canvas)
    for i, c in enumerate(clusters[:12]):
        t = on_white(c["img"])
        t.thumbnail((cell - 8, cell - 8))
        cx, cy = (i % cols) * cell, (i // cols) * (cell + 20)
        canvas.paste(t, (cx + (cell - t.width) // 2, cy + (cell - t.height) // 2))
        d.text((cx + 4, cy + cell + 4), f"{names[i]}  x{c['count']}", fill=(60, 60, 60))
    return canvas


def annotated_preview(original: Image.Image, motifs: list) -> Image.Image:
    """原图 + 检测框标注（看见找到了哪些、在哪）。"""
    from PIL import ImageColor
    base = original.convert("RGB").copy()
    d = ImageDraw.Draw(base)
    for i, m in enumerate(motifs):
        x1, y1, x2, y2 = m["bbox"]
        color = ImageColor.getrgb(["#e63946", "#2a9d8f", "#e9c46a", "#4361ee", "#f77f00"][i % 5])
        d.rectangle((x1, y1, x2, y2), outline=color, width=max(2, base.width // 400))
        d.text((x1 + 3, y1 + 3), str(i + 1), fill=color)
    return base


def pasteback_preview(original: Image.Image, full_rgba: Image.Image) -> Image.Image:
    """三联回贴预览（仅保真档，mask 与原图同尺寸才准）：ORIGINAL | EXTRACTED | PASTEBACK。"""
    p1 = original.convert("RGB")
    p2 = on_white(full_rgba)
    p3 = p1.copy()
    if full_rgba.mode == "RGBA" and full_rgba.size == p3.size:
        p3.paste(full_rgba, mask=full_rgba.split()[-1])
    for p in (p1, p2, p3):
        p.thumbnail((512, 512))
    w, h = p1.width + p2.width + p3.width + 24, max(p1.height, p2.height, p3.height) + 24
    canvas = Image.new("RGB", (w, h), (255, 255, 255))
    x = 0
    for p, label in ((p1, "ORIGINAL"), (p2, "EXTRACTED"), (p3, "PASTEBACK")):
        canvas.paste(p, (x, 0))
        ImageDraw.Draw(canvas).text((x + 4, h - 18), label, fill=(60, 60, 60))
        x += p.width + 12
    return canvas


# ---------- 边缘连通色键去底（板块1 提取 / 板块2 生图段共用，原在 ai_extract.py）----------

def remove_bg(img: Image.Image, tol: int = 42) -> tuple:
    """边缘连通色键去底：只抠与边缘连通的底色，图案内部同色块（白花/白书页）不受影响。
    返回 (RGBA, 是否执行)。底色=边框颜色众数（量化 24/步，抗花纹污染）；
    边框底色覆盖率 <20% 判满幅无底，不硬抠。"""
    from scipy import ndimage
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
