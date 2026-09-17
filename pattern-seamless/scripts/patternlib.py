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


# ---------- 板块4：布点引擎 / 散点采样 / 变换调色 / 量化度量（04-图案变体.md v2.3）----------

LAYOUTS = ("straight", "half-drop", "brick", "mirror", "toss")
ROTATIONS = ("one-way", "two-way", "four-way")
DENSITY_PRESETS = {"sparse": 0.25, "medium": 0.50, "dense": 0.75, "packed": 0.90}
SCALE_PRESETS = {"ditsy": 0.22, "medium": 0.40, "large": 0.62, "placement": 0.72}
FRAMES = {"square": (1024, 1024), "2:3": (1024, 1536), "3:2": (1536, 1024), "width145": (1536, 1024)}
COLOR_PRESETS = ("hue", "invert", "mono", "duotone", "vintage", "bright", "gray")


def poisson_points(w: float, h: float, min_dist: float, rng, k: int = 16, max_points: int = 600) -> np.ndarray:
    """泊松圆盘撒点（Bridson 层次掷镖，numpy 实现毫秒级）：随机 + 最小间距保证，toss 布点底座。
    返回 (N,2)。hdt-sampling 留作可选加速件，MVP 以本实现为准（效果同档）。"""
    import math
    cell = max(min_dist / math.sqrt(2), 4.0)
    gw, gh = int(w / cell) + 1, int(h / cell) + 1
    grid: dict = {}

    def too_close(x, y):
        cx, cy = int(x / cell), int(y / cell)
        for gx in range(max(0, cx - 2), min(gw, cx + 3)):
            for gy in range(max(0, cy - 2), min(gh, cy + 3)):
                if (gx, gy) in grid:
                    px, py = grid[(gx, gy)]
                    if (px - x) ** 2 + (py - y) ** 2 < min_dist * min_dist:
                        return True
        return False

    points: list = []
    active: list = []

    def add(x, y):
        grid[(int(x / cell), int(y / cell))] = (x, y)
        points.append((x, y))
        active.append((x, y))

    add(float(rng.uniform(0, w)), float(rng.uniform(0, h)))
    while active and len(points) < max_points:
        i = int(rng.integers(len(active)))
        px, py = active[i]
        placed = False
        for _ in range(k):
            ang = rng.uniform(0, 2 * math.pi)
            r = rng.uniform(min_dist, 2 * min_dist)
            qx, qy = px + r * math.cos(ang), py + r * math.sin(ang)
            if 0 <= qx < w and 0 <= qy < h and not too_close(qx, qy):
                add(qx, qy)
                placed = True
                break
        if not placed:
            active.pop(i)
    return np.array(points, dtype=float)


def transform_color(rgba: Image.Image, color: str) -> Image.Image:
    """变换调色（alpha 保真，只动前景像素）。color ∈ hue<N>|invert|mono|duotone|vintage|bright|gray|none"""
    if not color or color == "none":
        return rgba.convert("RGBA")
    im = rgba.convert("RGBA")
    arr = np.asarray(im, dtype=np.float32)
    rgb, a = arr[..., :3], arr[..., 3]
    m = a > 8
    if color.startswith("hue"):
        deg = float(color[3:] or 30)
        hsv = np.asarray(im.convert("HSV"), dtype=np.float32)
        hsv[..., 0] = (hsv[..., 0] + deg / 360.0 * 255.0) % 256.0
        rgbn = np.asarray(Image.fromarray(hsv.astype(np.uint8), "HSV").convert("RGB"), dtype=np.float32)
    elif color == "invert":
        rgbn = 255.0 - rgb
    elif color == "mono":
        L = rgb @ np.array([0.299, 0.587, 0.114])
        hsv = np.asarray(im.convert("HSV"), dtype=np.float32)
        hues, sats = hsv[..., 0][m], hsv[..., 1][m]
        ink_h = float(np.median(hues)) if len(hues) and sats.mean() > 40 else 0.0
        hsvn = np.zeros((*L.shape, 3), dtype=np.uint8)
        hsvn[..., 0], hsvn[..., 1] = int(ink_h), 153  # 0.6 饱和度墨色
        hsvn[..., 2] = np.clip(L, 0, 255).astype(np.uint8)
        rgbn = np.asarray(Image.fromarray(hsvn, "HSV").convert("RGB"), dtype=np.float32)
    elif color == "duotone":
        L = (rgb @ np.array([0.299, 0.587, 0.114])) / 255.0
        dark, light = np.array([38.0, 36.0, 46.0]), np.array([245.0, 242.0, 232.0])
        rgbn = dark + (light - dark) * L[..., None]
    elif color in ("vintage", "bright", "gray"):
        hsv = np.asarray(im.convert("HSV"), dtype=np.float32)
        hsv[..., 1] = {"vintage": hsv[..., 1] * 0.55,
                       "bright": np.clip(hsv[..., 1] * 1.3, 0, 255),
                       "gray": hsv[..., 1] * 0.15}[color]
        rgbn = np.asarray(Image.fromarray(hsv.astype(np.uint8), "HSV").convert("RGB"), dtype=np.float32)
        if color == "vintage":
            rgbn[..., 0] = np.clip(rgbn[..., 0] + 8, 0, 255)
    else:
        raise ValueError(f"未知 color：{color}")
    out = arr.copy()
    out[..., :3] = np.where(m[..., None], rgbn, rgb)
    return Image.fromarray(out.astype(np.uint8), "RGBA")


def grid_points(layout: str, W: int, H: int, cell: float) -> tuple:
    """刚性网格布点：straight / half-drop / brick / mirror。返回 (points Nx2, flips Nxbool)。"""
    cols = max(1, int(round(W / cell)))
    rows = max(1, int(round(H / cell)))
    sx, sy = W / cols, H / rows
    pts, flips = [], []
    for r in range(rows):
        for c in range(cols):
            x = (c + 0.5) * sx
            y = (r + 0.5) * sy
            if layout == "half-drop":
                x = (x + (c % 2) * sx * 0.5) % W
            elif layout == "brick":
                x = (x + (r % 2) * sx * 0.5) % W
            flip = layout == "mirror" and ((c % 2 == 1) ^ (r % 2 == 1))
            pts.append((x, y))
            flips.append(flip)
    return np.array(pts, dtype=float), np.array(flips, dtype=bool)


def tilt_points(points: np.ndarray, W: int, H: int, deg: float) -> np.ndarray:
    """整体斜排：布点阵绕画布中心旋转 deg 度（motif 保持直立），出斜格观感。"""
    if not deg:
        return points
    import math
    t = math.radians(deg)
    cx, cy = W / 2, H / 2
    rot = np.array([[math.cos(t), -math.sin(t)], [math.sin(t), math.cos(t)]])
    return (points - [cx, cy]) @ rot.T + [cx, cy]


def render_repeat(motif_rgba: Image.Image, W: int, H: int, points: np.ndarray, angles,
                  scales: np.ndarray, flips=None, bg=(255, 255, 255), transparent=False) -> Image.Image:
    """把 motif 画到画布（贴出界自然裁切；motif 居中对齐布点）。返回 RGBA。"""
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0) if transparent else (*bg, 255))
    for i, (x, y) in enumerate(points):
        m = motif_rgba
        if flips is not None and len(flips) > i and flips[i]:
            m = m.transpose(Image.FLIP_LEFT_RIGHT)
        s = float(scales[i])
        if abs(s - 1.0) > 1e-6:
            m = m.resize((max(1, int(m.width * s)), max(1, int(m.height * s))), Image.LANCZOS)
        ang = float(angles[i]) if angles is not None else 0.0
        if abs(ang) > 1e-6:
            m = m.rotate(ang, expand=True, resample=Image.BICUBIC)
        canvas.paste(m, (int(x - m.width / 2), int(y - m.height / 2)), m)
    return canvas


def zoom_allover(img: Image.Image, zoom: float) -> Image.Image:
    """满幅稿粗档：中心裁切 1/zoom 再放回（≈变相改花相对大小）。zoom 0.5-2.0。"""
    zoom = max(0.5, min(2.0, float(zoom or 1.0)))
    if abs(zoom - 1.0) < 1e-6:
        return img.convert("RGBA")
    W, H = img.size
    cw, ch = int(W / zoom), int(H / zoom)
    x1, y1 = (W - cw) // 2, (H - ch) // 2
    return img.crop((x1, y1, x1 + cw, y1 + ch)).resize((W, H), Image.LANCZOS).convert("RGBA")


# ---------- 量化度量（出口反馈：数字说话，不靠肉眼）----------

def measure_coverage(canvas_rgba: Image.Image) -> float:
    return float((np.array(canvas_rgba.split()[-1]) > 8).mean())


def count_components(canvas_rgba: Image.Image, min_size: int = 48) -> int:
    from skimage.measure import label
    a = np.array(canvas_rgba.split()[-1]) > 8
    if not a.any():
        return 0
    return int((np.bincount(label(a).ravel())[1:] >= min_size).sum())


def nn_min_dist(points: np.ndarray) -> float:
    """最近邻最小间距（toss 防重叠/防过疏）。单点返回 inf。"""
    if len(points) < 2:
        return float("inf")
    from scipy.spatial import cKDTree
    d, _ = cKDTree(points).query(points, k=2)
    return float(d[:, 1].min())


def hue_mean_deg(rgba: Image.Image) -> float:
    """前景平均色相（度，0-360，圆周均值）。近乎无彩时返回 nan。"""
    hsv = np.asarray(rgba.convert("HSV"), dtype=np.float32)
    a = np.array(rgba.split()[-1]) > 128
    if not a.any():
        return float("nan")
    h = hsv[..., 0][a] / 255.0 * 2 * np.pi
    s = hsv[..., 1][a]
    if s.mean() < 25:
        return float("nan")
    return float((np.arctan2(np.sin(h).mean(), np.cos(h).mean()) % (2 * np.pi)) / (2 * np.pi) * 360)


def palette_hex(img: Image.Image, k: int = 5) -> list:
    """KMeans 主色 hex（变体记录用，自实现，无 sklearn）。"""
    arr = np.asarray(img.convert("RGB"), dtype=np.float64).reshape(-1, 3)
    rng = np.random.default_rng(0)
    if len(arr) > 2048:
        arr = arr[rng.choice(len(arr), 2048, replace=False)]
    centers = arr[rng.choice(len(arr), min(k, len(arr)), replace=False)].copy()
    for _ in range(10):
        lab = ((arr[:, None, :] - centers[None, :, :]) ** 2).sum(-1).argmin(1)
        for i in range(len(centers)):
            mm = arr[lab == i]
            if len(mm):
                centers[i] = mm.mean(0)
    order = np.argsort([-len(arr[lab == i]) for i in range(len(centers))])
    return ["#%02x%02x%02x" % tuple(int(v) for v in centers[i]) for i in order]


# ---------- 板块5：环面布点 / wrap 绘制 / 周期检测 / 修缝 / 边缘跳变（05-无缝循环.md v2）----------
# 与板块4 布点的本质差异：板块4 render_repeat 贴出界自然裁切（单页语义）；
# 无缝必须环绕延续（构造性无缝），且布点间距按环面距离判定（否则接缝两侧挤叠）。

def _torus_d(d: float, size: float) -> float:
    """一维环绕距离：|d| 与绕过去 |size-d| 取小（左右上头粘起来的纸环）。"""
    d = abs(float(d))
    return min(d, size - d)


def poisson_points_torus(w: float, h: float, min_dist: float, rng,
                         k: int = 16, max_points: int = 400) -> np.ndarray:
    """环面版泊松撒点（Bridson，间距按环绕距离判定）：toss 无缝布点底座。"""
    import math
    cell = max(min_dist / math.sqrt(2), 4.0)
    gw, gh = max(1, int(w / cell)) + 1, max(1, int(h / cell)) + 1
    grid: dict = {}

    def too_close(x, y):
        cx, cy = int(x / cell), int(y / cell)
        for gx in range(cx - 2, cx + 3):
            for gy in range(cy - 2, cy + 3):
                hit = grid.get((gx % gw, gy % gh))
                if hit is not None:
                    px, py = hit
                    dx, dy = _torus_d(px - x, w), _torus_d(py - y, h)
                    if dx * dx + dy * dy < min_dist * min_dist:
                        return True
        return False

    points: list = []
    active: list = []

    def add(x, y):
        grid[(int(x / cell) % gw, int(y / cell) % gh)] = (x, y)
        points.append((x, y))
        active.append((x, y))

    add(float(rng.uniform(0, w)), float(rng.uniform(0, h)))
    while active and len(points) < max_points:
        i = int(rng.integers(len(active)))
        px, py = active[i]
        placed = False
        for _ in range(k):
            ang = rng.uniform(0, 2 * math.pi)
            r = rng.uniform(min_dist, 2 * min_dist)
            qx, qy = (px + r * math.cos(ang)) % w, (py + r * math.sin(ang)) % h
            if not too_close(qx, qy):
                add(qx, qy)
                placed = True
                break
        if not placed:
            active.pop(i)
    return np.array(points, dtype=float)


def nn_min_dist_torus(points: np.ndarray, w: float, h: float) -> float:
    """环面最近邻最小间距（出口校验：接缝两侧防挤叠）。单点返回 inf。"""
    if len(points) < 2:
        return float("inf")
    from scipy.spatial import cKDTree
    pts = np.asarray(points, dtype=float)
    offs = np.array([(ox, oy) for ox in (-w, 0, w) for oy in (-h, 0, h)], dtype=float)
    reps = np.concatenate([pts + o for o in offs], axis=0)
    d, _ = cKDTree(reps).query(pts, k=2)  # 第 1 近=自身副本(0)，第 2 近=真最近邻
    return float(d[:, 1].min())


def paste_wrap(canvas: Image.Image, m: Image.Image, x: float, y: float) -> None:
    """wrap 粘贴：本体 + 邻接偏移拷贝，越界部分从对侧延续（构造性无缝的核心）。"""
    W, H = canvas.size
    for ox in (-W, 0, W):
        px = x + ox
        if px + m.width <= 0 or px >= W:
            continue
        for oy in (-H, 0, H):
            py = y + oy
            if py + m.height <= 0 or py >= H:
                continue
            canvas.alpha_composite(m, (int(px), int(py)))


def render_repeat_wrap(motif_rgba: Image.Image, W: int, H: int, points: np.ndarray,
                       angles, scales: np.ndarray, flips=None) -> Image.Image:
    """无缝 tile 渲染：透明画布 + 环绕粘贴（对比板块4 render_repeat 的贴边裁切）。
    前提：max(motif 尺寸) <= tile（主链已守卫），否则相邻偏移拷贝会自叠。"""
    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for i, (x, y) in enumerate(points):
        m = motif_rgba
        if flips is not None and len(flips) > i and flips[i]:
            m = m.transpose(Image.FLIP_LEFT_RIGHT)
        s = float(scales[i])
        if abs(s - 1.0) > 1e-6:
            m = m.resize((max(1, int(m.width * s)), max(1, int(m.height * s))), Image.LANCZOS)
        ang = float(angles[i]) if angles is not None else 0.0
        if abs(ang) > 1e-6:
            m = m.rotate(ang, expand=True, resample=Image.BICUBIC)
        paste_wrap(canvas, m, x - m.width / 2, y - m.height / 2)
    return canvas


def edge_jump_ratio(rgba: Image.Image) -> float:
    """边缘跳变比（05 统一出口标尺）：环绕接缝的颜色跳变 ÷ 画布内部相邻行/列平均跳变。
    RGBA 按预乘 RGB+alpha 四通道算。构造性无缝 ≈1.0；通过线 ≤1.2（05 §6）。"""
    arr = np.asarray(rgba.convert("RGBA"), dtype=np.float64)
    a = arr[..., 3:4] / 255.0
    prem = np.concatenate([arr[..., :3] * a, a], axis=-1)
    H, W = prem.shape[:2]
    if W < 4 or H < 4:
        return 1.0
    col_j = np.sqrt(((prem[:, 1:] - prem[:, :-1]) ** 2).sum(-1))   # 内部相邻列
    row_j = np.sqrt(((prem[1:, :] - prem[:-1, :]) ** 2).sum(-1))   # 内部相邻行
    wrap = (np.sqrt(((prem[:, 0] - prem[:, -1]) ** 2).sum(-1)).mean()
            + np.sqrt(((prem[0, :] - prem[-1, :]) ** 2).sum(-1)).mean()) / 2.0
    base = max((col_j.mean() + row_j.mean()) / 2.0, 1e-6)
    return float(wrap / base)


def find_period(img: Image.Image, lo_frac: float = 0.12, hi_frac: float = 0.85) -> tuple:
    """FFT 自相关找主重复周期（路B）。返回 (pw, ph, conf)：源图像素尺度 + 归一化置信度（0-1）。
    无可靠峰 conf=0。2/3 整分周期若同样高 → 取更小的基本单元（基频修正）。"""
    w, h = img.size
    f = min(1.0, 384.0 / max(w, h))
    sw, sh = max(32, int(w * f)), max(32, int(h * f))
    g = np.asarray(img.convert("L").resize((sw, sh), Image.BILINEAR), dtype=np.float64)
    g = g - g.mean()
    F = np.fft.fft2(g)
    ac = np.fft.ifft2(np.abs(F) ** 2).real
    ac /= max(ac[0, 0], 1e-9)
    H, W = ac.shape
    xs, ys = np.arange(W), np.arange(H)
    row_lag = np.where(xs <= W // 2, xs, W - xs)          # 负 lag 折到正半区
    col_lag = np.where(ys <= H // 2, ys, H - ys)
    lagx = np.broadcast_to(row_lag, (H, W))               # 列方向 lag（每行同值）
    lagy = np.broadcast_to(col_lag[:, None], (H, W))      # 行方向 lag（每列同值）
    mx = (lagx >= max(2, int(W * lo_frac))) & (lagx <= max(3, int(W * hi_frac)))
    my = (lagy >= max(2, int(H * lo_frac))) & (lagy <= max(3, int(H * hi_frac)))
    mask = my & mx
    if not mask.any():
        return 0, 0, 0.0
    acm = np.where(mask, ac, -1.0)
    iy, ix = np.unravel_index(int(np.argmax(acm)), acm.shape)
    conf = float(ac[iy, ix])
    dx, dy = int(lagx[iy, ix]), int(lagy[iy, ix])

    def ac_at(lx: int, ly: int) -> float:
        ix2 = lx if lx <= W // 2 else W - lx
        iy2 = ly if ly <= H // 2 else H - ly
        return float(ac[iy2 % H, ix2 % W])

    for k in (3, 2):
        if dx % k == 0 and dy % k == 0:
            lx, ly = dx // k, dy // k
            if lx < max(2, int(W * lo_frac)) or ly < max(2, int(H * lo_frac)):
                continue   # 低于搜索下限的不采信：平滑渐变在低 lag 有自相关假峰
            sub = ac_at(lx, ly)
            if sub >= conf * 0.88:
                dx, dy, conf = lx, ly, sub
                break
    return min(int(round(dx / f)), w), min(int(round(dy / f)), h), round(conf, 3)


def crop_period(img: Image.Image, pw: int, ph: int, tile: int) -> Image.Image:
    """路B：按周期裁 repeat 单元（原点 0,0），等比 resize 到长边=tile（保周期）。"""
    pw = max(8, min(int(pw), img.width))
    ph = max(8, min(int(ph), img.height))
    t = img.convert("RGBA").crop((0, 0, pw, ph))
    f = tile / max(t.size)
    return t.resize((max(8, int(t.width * f)), max(8, int(t.height * f))), Image.LANCZOS)


def seam_blend(img: Image.Image, band_frac: float = 0.3, search: int = 12) -> Image.Image:
    """路C 纯代码兜底（img2texture 同思路）：错半格把缝挪到中央，中央带做 roll 稿↔原稿的
    三角渐变交叉溶解（缝对位置权重=0，wrap 两侧权重=1 → 周期成立可证）。
    wrap 衔接处=源图某个内部相邻对，故在中央 ±search 内搜 roll 偏移、取最平滑的一对。
    代价：中央带内容重影/涂抹——verdict=seam-fixed 如实标注。"""
    arr = np.asarray(img.convert("RGBA"), dtype=np.float64)
    out = arr.copy()
    H, W = arr.shape[:2]
    for axis, n in ((1, W), (0, H)):   # 先修水平周期，再修垂直
        # 选 roll 偏移 o：wrap 对 = 内部相邻对 (n-1-o, n-o)，取中央附近最平滑的一个
        best_o, best_d = n // 2, None
        for o in range(max(1, n // 2 - search), min(n - 1, n // 2 + search) + 1):
            if axis == 1:
                d = np.abs(out[:, n - 1 - o] - out[:, n - o]).mean()
            else:
                d = np.abs(out[n - 1 - o] - out[n - o]).mean()
            if best_d is None or d < best_d:
                best_o, best_d = o, d
        s = np.roll(out, best_o, axis=axis)
        b = min(max(2, int(n * band_frac) // 2), best_o, n - best_o)   # 单侧带宽
        wgt = np.ones(n)
        wgt[best_o - b:best_o] = np.linspace(1.0, 0.0, b)   # 左斜坡 1→0
        wgt[best_o:best_o + b] = np.linspace(0.0, 1.0, b)   # 右斜坡 0→1（缝对两侧权重=0）
        shape = [1, 1, 1]
        shape[axis] = n
        wgt = wgt.reshape(shape)
        out = wgt * s + (1.0 - wgt) * out
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGBA")


def mirror_tile(img: Image.Image) -> Image.Image:
    """路C 备选：四象限镜像（天然无缝；构图镜像翻倍——改了内容，meta 必须标 method=mirror）。"""
    im = img.convert("RGBA")
    qw, qh = max(8, im.width // 2), max(8, im.height // 2)
    q = im.crop((0, 0, qw, qh))
    tile = Image.new("RGBA", (qw * 2, qh * 2), (0, 0, 0, 0))
    tile.paste(q, (0, 0))
    tile.paste(q.transpose(Image.FLIP_LEFT_RIGHT), (qw, 0))
    tile.paste(q.transpose(Image.FLIP_TOP_BOTTOM), (0, qh))
    tile.paste(q.transpose(Image.ROTATE_180), (qw, qh))
    return tile


