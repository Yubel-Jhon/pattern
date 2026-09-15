# -*- coding: utf-8 -*-
"""板块2 · 高清修复：生图+VLM 主链，像素层降为尺寸引擎，整体成环（02-高清修复.md v2）。

宪法（§1）：只修质量，永不改设计。生图重绘=补细节（保真条款+VLM 比对把关+色差量化）；
像素层=尺寸引擎（1024→4096 接力）；VLM=质检员（入口判型+出口比对）。

主链（§6）：
  入口三拍：资格门+分诊（1 次 VLM，triage_card 公共件）→ 路由
  → 预处理段（重度校色/去压噪）→ 生图段（短边<1024 才进，§3 门槛写死）
  → 后处理段（去底：色键/alpha 分源；接力：ESRGAN ncnn→Lanczos；edge_clean/trim_frame；轻锐化）
  → 校验六道（§8）→ 02_hd/ 产物；不过 → 回炉≤2（每次必须换变量，§9）→ 反馈层

用法：
  python enhance.py <输入图或目录> [--scale 4] [--gen auto|on|off] [--texture auto]
      [--carrier auto] [--model auto|anime|photo] [--sharpen auto|off] [--force]
      [--name X] [--out pattern_output]

stdout 契约（§7）：每图一行 JSON {ok, files, warnings, next_action, gate, triage,
  stages:{preprocess,gen,upscale}, method, seconds}
  method ∈ ncnn_vulkan | torch_cpu | lanczos_only | skipped_already_hd
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from patternlib import (edge_clean, ensure_workspace, on_white, record_step, remove_bg,
                        save_raw, step_dir, trim_frame, find_realesrgan)
from dashscope_gen import qwen_i2i
from triage_card import offline_card, vlm_card

# ---- 门槛常量（§3 写死）----
GEN_GATE_SHORT = 1024   # 只有短边 <1024 的输入才进生图段
SKIP_SHORT = 2048       # 短边 ≥2048 默认跳过全流程（--force 可强跑）
MAX_RETRIES = 2         # 每图最多回炉 2 次，超次进反馈层（防死循环）
MAX_OUT_SIDE = 8192     # 内存护栏

I2I_SIZES = ["1024*1024", "1440*1440", "1472*1140", "1140*1472",
             "1664*928", "928*1664", "1584*1056", "1056*1584"]

# ---- 提示词四段拼装（§4）：基座 + 质地 + 载体 + 缺陷 ----
BASE_CLAUSE = ("严格保持图案的造型、元素、构图与颜色完全不变，不新增也不删除任何元素；"
               "只提升清晰度、锐化边缘、恢复被低清丢失的纹理细节。")
DRIFT_CLAUSE = "所有元素保持在原位置，不得移动、缩放、旋转或重排任何元素。"
TIGHTEN_CLAUSE = ("这是最高优先级要求：逐个元素对照原图，元素的数量、形状、位置必须一一对应，"
                  "有任何疑虑宁可保持原样，绝不自行发挥。")
TEXTURE_CLAUSES = {
    "flat": "这是平色插画风格花稿：保持色块均匀、线条锐利清晰、边缘干净。",
    "watercolor": "这是水彩晕染风格花稿：保留晕染的柔和软边与水彩渐变，绝不要把软边锐化成硬边。",
    "photo": "这是照片质感花稿：保留真实的织物纹理与颗粒感，不要把纹理抹平或过度平滑。",
}
TEXTURE_CONSERVATIVE = "保留原有的边缘质感与纹理特征，不做激进的锐化或硬边化，宁可保守也不要改变质感。"
CARRIER_CLAUSES = {
    "motif": "这是独立图案稿：图案完整居中于纯白背景上，不要圆形或方形边框，不要添加背景装饰。",
    "allover": ("这是满幅花布回样：纹样延续铺满整个画面、直达四边，四角不能留白，"
                "不要圆形或方形边框；保持四边花纹的连续性。"),
}
DEFECT_CLAUSES = {
    "clarity": "消除模糊和 JPEG 压缩噪点色块，让画面变清晰。",
    "detail": "恢复被低清抹掉的纹理细节：织纹、笔触、渐变层次。",
    "edge": "修齐毛边、锯齿和白边，让轮廓干净利落。",
    "color": "修正偏色与褪色，还原本来的颜色；只做还原，不改变设计配色。",
}

COMPARE_PROMPT = """你是印花花稿质检员。第一张图是原始低清稿，第二张是高清修复结果。
只比对「设计是否一致」，不要评价谁更清晰。只输出 JSON：
{"elements_ok":true或false,"added":["被新增的元素"],"removed":["被删除的元素"],
 "position_ok":true或false,"palette_ok":true或false,"texture_ok":true或false,"notes":"一句话中文"}
判据：elements_ok=元素种类与数量一一对应；position_ok=元素位置与构图未变；
palette_ok=主色板一致（允许还原性的校色，不允许变成另一套配色）；
texture_ok=（水彩稿）晕染软边还在/（照片肌理稿）织物颗粒还在/（平色插画稿）色块依然均匀。"""


# ============ 小工具 ============

def pick_size(w: int, h: int) -> str:
    """从生图端支持档位里挑长宽比最接近的，压构图漂移与分源错位。"""
    target = w / h
    return min(I2I_SIZES, key=lambda s: abs(int(s.split("*")[0]) / int(s.split("*")[1]) - target))


def cast_ratios(rgb: Image.Image) -> np.ndarray:
    """通道均值/灰均值 → 偏色度量（>1.14 或 <0.87 算重度偏色）。"""
    arr = np.asarray(rgb.convert("RGB"), dtype=np.float64).reshape(-1, 3)
    means = arr.mean(axis=0)
    return means / max(means.mean(), 1.0)


def gray_world(rgb: Image.Image) -> Image.Image:
    """灰世界白平衡：重度偏色先校再喂，否则生图照着偏色画偏色画（§3 预处理段）。"""
    arr = np.asarray(rgb.convert("RGB"), dtype=np.float64)
    means = arr.reshape(-1, 3).mean(axis=0)
    gains = means.mean() / np.maximum(means, 1.0)
    return Image.fromarray(np.clip(arr * gains, 0, 255).astype(np.uint8))


def blockiness(rgb: Image.Image) -> float:
    """JPEG 8px 网格线梯度 / 其余区域梯度 → 压缩噪信号（>1.35 才去压噪，防抹平纹理）。"""
    g = np.asarray(rgb.convert("L"), dtype=np.float64)
    dx = np.abs(np.diff(g, axis=1))
    dy = np.abs(np.diff(g, axis=0))
    gx = np.ones(dx.shape[1], bool)
    gx[7::8] = False
    gy = np.ones(dy.shape[0], bool)
    gy[7::8] = False
    grid = float(np.concatenate([dx[:, ~gx].ravel(), dy[~gy, :].ravel()]).mean())
    rest = float(np.concatenate([dx[:, gx].ravel(), dy[gy, :].ravel()]).mean())
    return grid / max(rest, 1e-6)


def kmeans_palette(img: Image.Image, k: int = 6, iters: int = 12) -> tuple:
    """主色板（KMeans top-N，§8-3）：前景优先采样（RGBA 用 alpha>128 的像素）。"""
    rgba = img.convert("RGBA")
    arr = np.asarray(rgba, dtype=np.float64)
    a = arr[..., 3]
    fg = arr[..., :3][a > 128] if (a > 128).sum() > 500 else arr[..., :3].reshape(-1, 3)
    rng = np.random.default_rng(0)
    if len(fg) > 2304:
        fg = fg[rng.choice(len(fg), 2304, replace=False)]
    centers = fg[rng.choice(len(fg), min(k, len(fg)), replace=False)].copy()
    lab = np.zeros(len(fg), int)
    for _ in range(iters):
        d = ((fg[:, None, :] - centers[None, :, :]) ** 2).sum(-1)
        lab = d.argmin(1)
        for i in range(len(centers)):
            m = fg[lab == i]
            if len(m):
                centers[i] = m.mean(0)
    shares = np.bincount(lab, minlength=len(centers)) / max(len(fg), 1)
    return centers, shares


def palette_distance(img_a: Image.Image, img_b: Image.Image) -> tuple:
    """主色板距离（不逐像素，§5-⑥）：加权对称最近邻距离 + 漂移最大的色对。"""
    ca, sa = kmeans_palette(img_a)
    cb, sb = kmeans_palette(img_b)
    d = np.sqrt(((ca[:, None, :] - cb[None, :, :]) ** 2).sum(-1))
    ab = float((d.min(1) * sa).sum())
    ba = float((d.min(0) * sb).sum())
    i, j = np.unravel_index(int(d.argmax()), d.shape)
    worst = {"a": [int(v) for v in ca[i]], "b": [int(v) for v in cb[j]],
             "dist": round(float(d[i, j]), 1)}
    return round((ab + ba) / 2, 1), worst


def unsharp_rgb(img: Image.Image, radius: float, percent: int, threshold: int = 2) -> Image.Image:
    """只锐 RGB 不动 alpha（RGBA 上直接 UnsharpMask 会把透明边缘锐出脏边）。"""
    if img.mode != "RGBA":
        return img.filter(ImageFilter.UnsharpMask(radius, percent, threshold))
    r, g, b, a = img.split()
    rgb = Image.merge("RGB", (r, g, b)).filter(ImageFilter.UnsharpMask(radius, percent, threshold))
    return Image.merge("RGBA", (*rgb.split(), a))


def color_rgb(img: Image.Image, factor: float) -> Image.Image:
    """只调 RGB 饱和度不动 alpha（ImageEnhance.Color 走 HSV 往返会丢透明通道）。"""
    if img.mode != "RGBA":
        return ImageEnhance.Color(img).enhance(factor)
    r, g, b, a = img.split()
    rgb = ImageEnhance.Color(Image.merge("RGB", (r, g, b))).enhance(factor)
    return Image.merge("RGBA", (*rgb.split(), a))


def register_split(orig_rgba: Image.Image, redraw_hd: Image.Image,
                   tgt: tuple, scale: int) -> tuple:
    """分源配准（§5-② 轻代码粗对齐）：把重绘稿按前景 bbox 缩放+平移回原稿几何。
    在高分辨率空间做（接力后），不损生图细节。返回 (canvas_rgb, iou, info)；
    配准后形状 IoU 仍低 = 真构图漂移 → 调用方放弃分源，白底稿交付。"""
    W, H = tgt
    oa = np.array(orig_rgba.split()[-1]) > 64
    ys, xs = np.where(oa)
    if len(xs) == 0:
        return None, 0.0, {"reason": "原图无前景"}
    ba_t = [xs.min() * scale, ys.min() * scale, (xs.max() + 1) * scale, (ys.max() + 1) * scale]
    rr = np.asarray(redraw_hd.convert("RGB"), dtype=np.float64)
    rm = np.sqrt(((rr - 255.0) ** 2).sum(-1)) > 60  # 白底上的前景
    ys2, xs2 = np.where(rm)
    if len(xs2) == 0:
        return None, 0.0, {"reason": "重绘稿无前景"}
    bb = [xs2.min(), ys2.min(), xs2.max() + 1, ys2.max() + 1]
    bw, bh = bb[2] - bb[0], bb[3] - bb[1]
    ow, oh = ba_t[2] - ba_t[0], ba_t[3] - ba_t[1]
    S = ((ow / bw) + (oh / bh)) / 2
    if not (0.3 <= S <= 3.0):  # 体量跳变过大，没有配准意义
        return None, 0.0, {"reason": f"scale jump {S:.2f}"}
    reg = redraw_hd.resize((max(1, int(redraw_hd.width * S)), max(1, int(redraw_hd.height * S))), Image.LANCZOS)
    rm2 = np.sqrt(((np.asarray(reg.convert("RGB"), dtype=np.float64) - 255.0) ** 2).sum(-1)) > 60
    ys3, xs3 = np.where(rm2)
    if len(xs3) == 0:
        return None, 0.0, {"reason": "缩放后无前景"}
    dx = int(round((ba_t[0] + ba_t[2]) / 2 - (xs3.min() + xs3.max() + 1) / 2))
    dy = int(round((ba_t[1] + ba_t[3]) / 2 - (ys3.min() + ys3.max() + 1) / 2))
    canvas = Image.new("RGB", tgt, (255, 255, 255))
    canvas.paste(reg, (dx, dy))
    # 配准后形状复核：同尺寸直接比 IoU
    cm = np.sqrt(((np.asarray(canvas, dtype=np.float64) - 255.0) ** 2).sum(-1)) > 60
    oat = np.asarray(Image.fromarray((oa * 255).astype(np.uint8)).resize(tgt)) > 64
    iou = float((cm & oat).sum()) / max(float((cm | oat).sum()), 1)
    return canvas, round(iou, 3), {"scale": round(S, 3), "shift": [dx, dy]}


def esrgan_upscale(src: Path, dst: Path, model: str, scale: int = 4, tile: int = 256) -> None:
    """ncnn 便携包接力（§10 级①）。失败/黑图由调用方兜底。"""
    exe = find_realesrgan()
    if not exe:
        raise FileNotFoundError("realesrgan-ncnn-vulkan 未找到")
    cmd = [exe, "-i", str(src), "-o", str(dst), "-n", model,
           "-s", str(scale), "-t", str(tile), "-f", "png"]
    proc = subprocess.run(cmd, cwd=str(Path(exe).parent), capture_output=True, text=True, timeout=600)
    if proc.returncode != 0 or not dst.exists():
        raise RuntimeError(f"ncnn rc={proc.returncode}: {(proc.stderr or '')[-200:]}")


def lanczos_upscale(img: Image.Image, scale: int) -> Image.Image:
    """零依赖兜底（§10 级③）：Lanczos 几何放大，诚实标 lanczos_only。"""
    return img.resize((img.width * scale, img.height * scale), Image.LANCZOS)


def is_black_or_empty(img: Image.Image) -> bool:
    """黑图/空图守卫（§8-1）：ncnn README 自认部分机器出黑图。"""
    rgba = img.convert("RGBA")
    if np.array(rgba.split()[-1]).max() == 0:
        return True
    rgb = np.asarray(rgba.convert("RGB"), dtype=np.float64)
    return bool(rgb.mean() < 8 and rgb.std() < 8)


def upscale_smart(img: Image.Image, scale: int, model: str, sharpen: str) -> tuple:
    """尺寸引擎：ncnn 优先 → Lanczos 兜底；返回 (result, method, note)。
    scale=2 按 4x 再降采样实现（§7）。RGBA 整图进 ncnn（冒烟已验 alpha 保真）。"""
    method, note, out = None, "", None
    with tempfile.TemporaryDirectory() as td:
        src, dst = Path(td) / "in.png", Path(td) / "out.png"
        img.save(src)
        if find_realesrgan():
            try:
                esrgan_upscale(src, dst, model, scale=4)
                cand = Image.open(dst)
                cand.load()
                if is_black_or_empty(cand):
                    note = "ncnn 黑图守卫触发（§8-1），降级 Lanczos"
                else:
                    out, method = cand.convert(img.mode), "ncnn_vulkan"
            except Exception as e:  # Vulkan 初始化失败/超时 → 落 Lanczos（§10 known bug 对策）
                note = f"ncnn 不可用（{e.__class__.__name__}），降级 Lanczos"
    if out is None:
        out, method = lanczos_upscale(img, 4), "lanczos_only"
    if scale == 2:
        out = out.resize((img.width * 2, img.height * 2), Image.LANCZOS)
    if max(out.size) > MAX_OUT_SIDE:
        s = MAX_OUT_SIDE / max(out.size)
        out = out.resize((int(out.width * s), int(out.height * s)), Image.LANCZOS)
        note = (note + "；超 8192 已降采样").strip("；")
    if sharpen != "off":
        heavy = method == "lanczos_only"  # lanczos 路更重，esrgan 路更轻（§7）
        out = unsharp_rgb(out, 2.0 if heavy else 1.2, 140 if heavy else 70)
    return out, method, note


def edge_continuity(rgb: Image.Image, band: int = 6) -> dict:
    """四边连续性（§8-5，满幅专属）：左右/上下边缘带差值。只报不修，超阈指路板块5。"""
    a = np.asarray(rgb.convert("RGB"), dtype=np.float64)
    h_diff = float(np.abs(a[:, :band] - a[:, -band:]).mean())
    v_diff = float(np.abs(a[:band, :] - a[-band:, :]).mean())
    return {"h": round(h_diff, 1), "v": round(v_diff, 1),
            "ok": bool(h_diff <= 30 and v_diff <= 30)}


def best_zoom_window(rgb: Image.Image, side: int) -> tuple:
    """梯度最密窗口（§7 局部放大对比取景）。"""
    g = np.asarray(rgb.convert("L"), dtype=np.float64)
    gy, gx = np.gradient(g)
    ii = np.hypot(gx, gy).cumsum(0).cumsum(1)
    ii = np.pad(ii, ((1, 0), (1, 0)))
    if ii.shape[0] <= side + 1 or ii.shape[1] <= side + 1:
        return (0, 0, rgb.width, rgb.height)
    s = ii[side:, side:] - ii[:-side, side:] - ii[side:, :-side] + ii[:-side, :-side]
    y, x = np.unravel_index(int(s.argmax()), s.shape)
    return (int(x), int(y), int(x + side), int(y + side))


def make_before_after(orig: Image.Image, final: Image.Image, out_path: Path) -> None:
    """并排 + 局部放大对比（§7 演示镜头）：上排全图，下排梯度最密区域同倍率并置。"""
    a, b = orig.convert("RGB").copy(), (on_white(final) if final.mode == "RGBA" else final.convert("RGB")).copy()
    for im in (a, b):
        im.thumbnail((460, 460))
    fb = on_white(final) if final.mode == "RGBA" else final.convert("RGB")
    side = max(64, min(orig.size) // 3)
    win = best_zoom_window(orig, side)
    fx = (win[0] / orig.width * final.width, win[1] / orig.height * final.height,
          win[2] / orig.width * final.width, win[3] / orig.height * final.height)
    za = orig.convert("RGB").crop(win).resize((300, 300), Image.LANCZOS)
    zb = fb.crop((int(fx[0]), int(fx[1]), int(fx[2]), int(fx[3]))).resize((300, 300), Image.LANCZOS)
    w = a.width + b.width + 16
    h = max(a.height, b.height) + 340
    canvas = Image.new("RGB", (w, h), (255, 255, 255))
    canvas.paste(a, (0, 0))
    canvas.paste(b, (a.width + 16, 0))
    yz = max(a.height, b.height) + 24
    canvas.paste(za, (0, yz))
    canvas.paste(zb, (316, yz))
    from PIL import ImageDraw
    d = ImageDraw.Draw(canvas)
    d.text((4, 4), "BEFORE (low-res)", fill=(200, 40, 40))
    d.text((a.width + 20, 4), "AFTER (HD)", fill=(40, 140, 60))
    d.text((4, yz - 14), "ZOOM BEFORE", fill=(200, 40, 40))
    d.text((320, yz - 14), "ZOOM AFTER", fill=(40, 140, 60))
    canvas.save(out_path, quality=88)


def write_prompt_card(out_dir: Path, prompt: str, src: Path, ws_name: str, size: str) -> Path:
    """手动档提示词卡（§10 手动路线）：生图挂了/没 key 也不空手。"""
    card = out_dir / "_prompt_card.txt"
    card.write_text(
        "【板块2 高清修复 · 手动档提示词卡】\n"
        f"工作区: {ws_name}\n输入图: {src}\n建议输出尺寸: {size}（任意外生图工具均可）\n\n"
        "把下面整段提示词 + 输入图喂给任意生图工具（GPT-Image / 即梦 / 通义万相...），\n"
        "生成后把结果图存下来，执行：\n"
        f"  python enhance.py <结果图路径> --gen off --name {ws_name}\n"
        "即可接力放大 + 校验 + 打包。\n\n----- 提示词 -----\n" + prompt + "\n",
        encoding="utf-8")
    return card


# ============ VLM 出口比对（§8-2/4）============

def vlm_compare(orig_white: Image.Image, final_white: Image.Image) -> dict:
    """设计一致性 + 质地复查，一次调用（全程 VLM×2 预算的第二次）。"""
    from dashscope_gen import vlm_chat
    from triage_card import VLM_MODELS, _parse_json
    for model in VLM_MODELS:
        try:
            text = vlm_chat([orig_white, final_white], COMPARE_PROMPT, timeout_s=120, model=model)
            raw = _parse_json(text)
            c = {"elements_ok": bool(raw.get("elements_ok")),
                 "position_ok": bool(raw.get("position_ok")),
                 "palette_ok": bool(raw.get("palette_ok")),
                 "texture_ok": bool(raw.get("texture_ok", True)),
                 "added": [str(x)[:40] for x in raw.get("added", [])][:5],
                 "removed": [str(x)[:40] for x in raw.get("removed", [])][:5],
                 "notes": str(raw.get("notes", ""))[:150], "_model": model}
            c["ok"] = bool(c["elements_ok"] and c["position_ok"] and c["palette_ok"])
            return c
        except Exception:
            continue
    return {"ok": None, "notes": "VLM 比对不可用（色差量化仍兜底，不阻断交付）"}


# ============ 主流程 ============

def enhance_one(src: Path, args, ws: Path) -> dict:
    t0 = time.time()
    warnings: list = []
    img = Image.open(src)
    img.load()
    img = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P") else img.convert("RGB")
    short = min(img.size)
    stem = src.stem
    stages = {"preprocess": {}, "gen": {}, "upscale": {}}
    retries_log: list = []

    # -- 短边 ≥2048 默认跳过（§3）--
    if short >= SKIP_SHORT and not args.force:
        return {"ok": True, "files": [], "stem": stem,
                "warnings": [f"短边 {short}px ≥{SKIP_SHORT}，已够高清默认跳过（--force 可强跑）"],
                "next_action": None, "gate": None, "triage": None, "stages": stages,
                "method": "skipped_already_hd", "seconds": round(time.time() - t0, 1), "_ws": ws}

    # -- ① 资格门+分诊（1 次 VLM；--gen off 跳过 VLM 走离线卡）--
    if args.gen == "off":
        card = offline_card(img)
        warnings.append("离线档：无法验稿，请确认输入是花稿（--gen auto 可启用 VLM 判型）")
        warnings += card.pop("_warnings", [])
    else:
        card = None
        try:
            card = vlm_card(img)
        except Exception:
            # X 异常回路（§2）：糊到判不了型 → 像素层先抬清 → 重新分诊（回炉≤1）
            try:
                lifted = unsharp_rgb(img.resize((img.width * 2, img.height * 2), Image.LANCZOS), 1.5, 100)
                card = vlm_card(lifted)
                card["_anomaly_loop"] = "pixel_lift_retriage"
                warnings.append("判型失败→像素抬清后重新分诊成功（X 异常回路）")
            except Exception as e2:
                card = offline_card(img)
                warnings.append(f"VLM 判型不可用（{e2.__class__.__name__}），降级离线卡")
                warnings += card.pop("_warnings", [])
        stages["preprocess"]["anomaly_loop"] = card.get("_anomaly_loop")

    # -- 资格门裁决（§2）--
    gate = card["gate"]
    triage_view = {k: card[k] for k in ("carrier", "texture", "defects", "focus")}
    if gate["action"] == "route_extract":
        return {"ok": False, "files": [], "stem": stem, "warnings": warnings,
                "next_action": "画面里有印花但主体不是花稿 → 先跑板块1 图案提取（ai_extract.py），不要跳过提取直接高清化",
                "gate": gate, "triage": triage_view, "stages": stages,
                "method": "skipped_already_hd", "seconds": round(time.time() - t0, 1), "_ws": ws}
    if gate["action"] == "ask_user" and (args.texture == "auto" or args.carrier == "auto"):
        return {"ok": False, "files": [], "stem": stem, "warnings": warnings,
                "next_action": f"拿不准这是不是花稿（{gate['reason']}）。确认后可用 --texture/--carrier 手动覆盖身份直跑",
                "gate": gate, "triage": triage_view, "stages": stages,
                "method": "skipped_already_hd", "seconds": round(time.time() - t0, 1), "_ws": ws}

    # -- 手动覆盖判型（§5-⑧ 逃生口）--
    if args.texture != "auto":
        card["texture"] = args.texture
    if args.carrier != "auto":
        card["carrier"] = args.carrier
    carrier, texture = card["carrier"], card["texture"]
    defects = list(card["defects"])
    if "size" not in defects and short < GEN_GATE_SHORT:
        defects.append("size")

    # -- 生图段门槛（§3 写死，一次性判定）--
    do_gen = True
    if short >= GEN_GATE_SHORT:
        do_gen = False
        warnings.append(f"短边 {short}px ≥{GEN_GATE_SHORT}，跳过生图段直接像素接力（先降质再增强=荒谬）")
    elif args.gen == "off":
        do_gen = False
    elif not os.environ.get("DASHSCOPE_API_KEY"):
        do_gen = False
        warnings.append("无 DASHSCOPE_API_KEY，生图段跳过 → 像素档兜底 + 手动档提示词卡")

    # -- ② 路由：提示词四段拼装 --
    def build_prompt(conservative: bool, tighten: bool) -> str:
        parts = [BASE_CLAUSE, DRIFT_CLAUSE,
                 TEXTURE_CONSERVATIVE if conservative else TEXTURE_CLAUSES[texture],
                 CARRIER_CLAUSES[carrier]]
        parts += [DEFECT_CLAUSES[d] for d in defects if d in DEFECT_CLAUSES]
        if tighten:
            parts.append(TIGHTEN_CLAUSE)
        return "。".join(p.rstrip("。") for p in parts) + "。"

    prompt0 = build_prompt(False, False)
    size_pref = pick_size(*img.size)

    # -- ③ 预处理段（拓扑序：校色/去噪在生图前）--
    flat_view = on_white(img) if img.mode == "RGBA" else img.convert("RGB")
    ratios = cast_ratios(flat_view)
    pre_wb = bool("color" in defects and (ratios.max() > 1.14 or ratios.min() < 0.87))
    feed = gray_world(img) if pre_wb else img
    pre_notes = {}
    if pre_wb:
        pre_notes["white_balance"] = "gray_world（重度偏色先校再喂，§3）"
    if "clarity" in defects and blockiness(flat_view) > 1.35:
        from scipy import ndimage
        arr = np.asarray(feed.convert("RGB"))
        den = np.stack([ndimage.median_filter(arr[..., c], size=3) for c in range(3)], -1)
        feed = Image.fromarray(den).convert(img.mode)
        pre_notes["denoise"] = "median3（JPEG 压缩噪，网格梯度超阈才动，防抹平纹理）"
    stages["preprocess"].update({"notes": pre_notes, "defects": defects})

    # 手动档提示词卡：短边 <1024 就写（§10 手动路线）
    prompt_card = None
    if short < GEN_GATE_SHORT:
        prompt_card = write_prompt_card(step_dir(ws, "hd"), prompt0, src, ws.name, size_pref)

    orig_white = flat_view
    orig_alpha = img.split()[-1] if img.mode == "RGBA" else None
    has_input_alpha = orig_alpha is not None and np.array(orig_alpha).min() < 250
    out_dir = step_dir(ws, "hd")
    attempts_full: list = []  # 每次尝试的完整台账（除结果图），复盘用

    # -- 单次尝试：生图（或像素直出）→ 去底/分源 → 接力 → 校验 --
    def attempt(pixel_only: bool, conservative: bool, tighten: bool, crop_focus: bool) -> dict:
        rec = {"pixel_only": pixel_only, "conservative": conservative,
               "tighten": tighten, "crop_focus": crop_focus, "pre_wb": pre_wb}
        gen_img = None
        if not pixel_only and do_gen:
            pf = feed
            if crop_focus:  # 绿网兜底：按 focus 内收 8% 重裁（§9 第一行）
                x1, y1, x2, y2 = card["focus"]
                W, H = pf.size
                dx, dy = int((x2 - x1) / 1000 * W * 0.08), int((y2 - y1) / 1000 * H * 0.08)
                box = (max(0, int(x1 / 1000 * W) + dx), max(0, int(y1 / 1000 * H) + dy),
                       min(W, int(x2 / 1000 * W) - dx), min(H, int(y2 / 1000 * H) - dy))
                if box[2] - box[0] > 32 and box[3] - box[1] > 32:
                    pf = pf.crop(box)
                    rec["crop_focus_applied"] = True
            feed_white = on_white(pf) if pf.mode == "RGBA" else pf.convert("RGB")
            prompt = build_prompt(conservative, tighten)
            tg = time.time()
            try:
                try:
                    gen_img = qwen_i2i([feed_white], prompt, size=size_pref)
                except Exception as e:
                    if size_pref and ("size" in str(e).lower() or "invalid" in str(e).lower()):
                        gen_img = qwen_i2i([feed_white], prompt)  # 尺寸档探测失败→模型默认
                        rec["size_fallback"] = True
                    else:
                        raise
            except Exception as e:
                tag = "DataInspectionFailed" if "DataInspectionFailed" in str(e) else e.__class__.__name__
                rec["gen_error"] = f"{tag}: {str(e)[:160]}"
            rec["gen_seconds"] = round(time.time() - tg, 1)
        rec["gen_ok"] = gen_img is not None
        if gen_img is not None:  # 生图原稿留档（复盘/手动档对照/演示）
            gen_img.save(out_dir / f"_gen_{len(attempts_full) + 1}.png")
        if not pixel_only:
            stages["gen"] = {"ok": rec["gen_ok"], "model": "qwen-image-3.0-pro",
                             "size": size_pref if do_gen else None,
                             "seconds": rec.get("gen_seconds"),
                             "error": rec.get("gen_error"),
                             "conservative": conservative, "tighten": tighten,
                             "crop_focus": crop_focus}

        # -- 后处理段 1/3：去底/形态选择（§5-①）--
        cand, cand_kind, want_split = None, None, False
        if gen_img is not None:
            gen_img = trim_frame(gen_img)
            if carrier == "motif" and texture == "flat":
                rgba_ck, did = remove_bg(gen_img)
                if did:
                    cand, cand_kind = edge_clean(rgba_ck), "rgba"
                    a = np.array(cand.split()[-1])
                    if (a > 128).mean() < 0.02:  # §8-6 近空守卫
                        cand, cand_kind = None, None
                        rec["alpha_guard"] = "色键结果近空，弃用"
                    elif has_input_alpha:
                        oa = np.array(orig_alpha.resize(cand.size)) > 128
                        if (a > 128).mean() < float(oa.mean()) * 0.5:  # §8-6 底色侵入
                            rec["alpha_guard"] = "主体面积骤减→换 alpha 分源"
                            cand, cand_kind = None, None
                if cand is None and not has_input_alpha:
                    cand, cand_kind = gen_img.convert("RGB"), "rgb"
                    rec["colorkey"] = "未去底（边缘不均匀或守卫触发）→白底稿交付"
                elif cand is None:
                    cand, cand_kind, want_split = gen_img.convert("RGB"), "rgb", True
            elif carrier == "motif":  # watercolor/photo：软边/肌理不碰色键
                if has_input_alpha:
                    cand, cand_kind, want_split = gen_img.convert("RGB"), "rgb", True
                else:
                    cand, cand_kind = gen_img.convert("RGB"), "rgb"
                    warnings.append(f"{texture} 质地且输入无 alpha：色键会毁软边（§5-①），白底稿交付")
            else:
                cand, cand_kind = gen_img.convert("RGB"), "rgb"
        if cand is None:  # 像素档：原图直出（RGBA 保真，设计零改动）
            cand = img
            cand_kind = "rgba" if img.mode == "RGBA" else "rgb"

        # -- 后处理段 2/3：尺寸接力（§10 三级）--
        model_name = ("realesrgan-x4plus-anime" if texture in ("flat", "watercolor")
                      else "realesrgan-x4plus") if args.model == "auto" else \
                     ("realesrgan-x4plus-anime" if args.model == "anime" else "realesrgan-x4plus")
        split_source = False
        if want_split:
            # alpha 分源（§5-①②）：RGB 用重绘高清版（配准回原稿几何），alpha 用原图 alpha 像素放大
            rgb_hd, method, note = upscale_smart(cand, args.scale, model_name, "off")
            tgt = (img.width * args.scale, img.height * args.scale)
            reg, iou, reginfo = register_split(img, rgb_hd, tgt, args.scale)
            alpha_hd = orig_alpha.resize(tgt, Image.LANCZOS)  # 软边 alpha 走像素放大，保渐变
            stages["upscale"] = {"method": method, "note": note, "engine_model": model_name,
                                 "alpha": "lanczos(原图 alpha)"}
            if reg is not None and iou >= 0.6:
                final = Image.merge("RGBA", (*reg.split(), alpha_hd))
                split_source = True
                stages["upscale"]["split_source"] = True
                stages["upscale"]["registration"] = {"iou": iou, **reginfo}
            else:
                final = (reg if reg is not None else rgb_hd).convert("RGB")
                rec["split_source"] = f"配准失败({reginfo}, iou={iou}) → 放弃分源，白底稿交付"
                warnings.append("alpha 分源失败（构图漂移超阈），诚实降级白底稿（§5-②）")
            if args.sharpen != "off":
                final = unsharp_rgb(final, 1.2, 70)
        else:
            final, method, note = upscale_smart(cand, args.scale, model_name, args.sharpen)
            stages["upscale"] = {"method": method, "note": note, "engine_model": model_name}
        if cand_kind == "rgb" and not split_source and final.mode == "RGBA":
            final = final.convert("RGB")

        # 轻度校色（后处理段；重度已在预处理喂前校过）
        if "color" in defects and not pre_wb:
            final = color_rgb(final, 1.05)
            rec["light_color"] = "Color×1.05（轻度，保真方向）"

        # -- 校验六道（§8）--
        checks = {"black_guard": {"ok": not is_black_or_empty(final), "note": "§8-1"}}
        if rec["gen_ok"]:
            fw = on_white(final) if final.mode == "RGBA" else final.convert("RGB")
            checks["design"] = vlm_compare(orig_white, fw)
            dist, worst = palette_distance(orig_white, fw)
            checks["palette"] = {"dist": dist, "worst": worst, "ok": dist <= 45,
                                 "note": "主色板距离（§8-3），阈值 45"}
        else:
            checks["design"] = {"ok": True, "notes": "像素档不改设计，免比对（§5-⑥）"}
            checks["palette"] = {"ok": True, "dist": 0, "note": "像素档几何放大"}
        if carrier == "allover":
            checks["edge_continuity"] = edge_continuity(final)  # 只报不修（§8-5）
        rec["checks"] = checks
        fails = []
        d = checks["design"]
        if d.get("ok") is False:
            if not (d.get("elements_ok") and d.get("position_ok")):
                fails.append("design")
            if d.get("palette_ok") is False:
                fails.append("palette")
            if d.get("texture_ok") is False:
                fails.append("texture")
        if checks["palette"].get("ok") is False:
            fails.append("palette")
        rec["fails"] = sorted(set(fails))
        rec["final"] = final
        attempts_full.append({k: v for k, v in rec.items() if k != "final"})
        return rec

    # -- 回炉循环（§9：每次必须换变量，防死循环≤2）--
    rec = attempt(False, False, False, False)
    blocked = "DataInspectionFailed" in str(rec.get("gen_error", ""))
    attempt_no = 0
    while rec["fails"] and attempt_no < MAX_RETRIES and not rec.get("pixel_only"):
        attempt_no += 1
        changes: dict = {}
        if blocked:
            if attempt_no == 1:
                changes["crop_focus"] = True  # 绿网：内收 8% 重裁重试
            else:
                changes["pixel_only"] = True
        elif attempt_no == 1:
            if "design" in rec["fails"]:
                changes["tighten"] = True  # 收紧「不新增不删除」条款
            if "texture" in rec["fails"]:
                changes["conservative"] = True  # 保守条款重试
            if "palette" in rec["fails"]:
                changes["pre_wb"] = True  # 先校色再重喂
            changes.setdefault("crop_focus", blocked)
        else:
            changes["pixel_only"] = True  # 第 2 回炉换路线：降纯像素档
        if changes.get("pre_wb") and not pre_wb:
            feed = gray_world(feed)
            pre_wb = True
        retries_log.append({"attempt": attempt_no, "changes": {k: v for k, v in changes.items() if v},
                            "prev_fails": rec["fails"]})
        rec = attempt(changes.get("pixel_only", False), changes.get("conservative", False),
                      changes.get("tighten", False), changes.get("crop_focus", False))
        blocked = "DataInspectionFailed" in str(rec.get("gen_error", ""))

    # -- 反馈层（§9 末列）：gen 档仍不过 → 保真像素稿兜底，不硬给不合格的生图稿 --
    if rec["fails"] and not rec.get("pixel_only"):
        warnings.append(f"生图档未过校验（{rec['fails']}）→ 反馈层：交付保真像素档 + 手动档提示词卡")
        rec = attempt(True, False, False, False)
    if rec.get("pixel_only") and any(a.get("gen_ok") for a in attempts_full[:-1]):
        warnings.append("最终交付 = 保真像素档（生图档未过校验被撤下，无补细节；"
                        "可看 02_hd/_gen_*.png 对照手动档重跑）")
    if rec["checks"].get("edge_continuity", {}).get("ok") is False:
        warnings.append(f"满幅稿四边拼接差超阈（h={rec['checks']['edge_continuity']['h']} v="
                        f"{rec['checks']['edge_continuity']['v']}）→ 建议走板块5 无缝循环（本板块只报不修）")
    final = rec["final"]
    method = stages.get("upscale", {}).get("method", "lanczos_only")
    blocked = blocked or "DataInspectionFailed" in str(rec.get("gen_error", ""))

    # -- 落盘（产物在 attempt 内已生成，这里汇总 meta）--
    out_path = out_dir / f"{stem}_hd.png"
    final.save(out_path)
    make_before_after(orig_white, final, out_dir / "before_after.jpg")
    files = [out_path, out_dir / "before_after.jpg"] + ([prompt_card] if prompt_card else [])

    lineage = ("01_extracted/" + src.name) if src.parent.name == "01_extracted" else str(src)
    detail = {"source": str(src), "lineage": lineage, "input_size": list(img.size),
              "output_size": list(final.size), "gate": card["gate"], "triage": triage_view,
              "stages": stages, "retries": retries_log, "checks": rec["checks"],
              "attempts": attempts_full, "upscale_method": method,
              "seconds": round(time.time() - t0, 1)}

    next_action = None
    if blocked:
        next_action = "生图被审核拦截：可用手动档提示词卡（_prompt_card.txt）在外部工具完成，或换图重试"
    return {"ok": True, "files": [str(f) for f in files], "stem": stem, "warnings": warnings,
            "next_action": next_action, "gate": card["gate"], "triage": triage_view,
            "stages": stages, "method": method, "seconds": round(time.time() - t0, 1),
            "retries": retries_log, "detail": detail, "_ws": ws}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="板块2 高清修复：生图+VLM 主链，像素接力成环")
    ap.add_argument("input", nargs="?", help="输入图片 / 目录 / 01_extracted 下的 motif 文件")
    ap.add_argument("--scale", type=int, choices=(2, 4), default=4, help="像素接力目标倍数（默认 4）")
    ap.add_argument("--gen", choices=("auto", "on", "off"), default="auto",
                    help="auto/on=按需生图；off=纯像素档（跳过生图与 VLM）")
    ap.add_argument("--texture", choices=("auto", "flat", "watercolor", "photo"), default="auto",
                    help="覆盖判型质地（判型翻车逃生口）")
    ap.add_argument("--carrier", choices=("auto", "motif", "allover"), default="auto",
                    help="覆盖判型载体")
    ap.add_argument("--model", choices=("auto", "anime", "photo"), default="auto",
                    help="像素接力引擎模型；auto 按质地选")
    ap.add_argument("--sharpen", choices=("auto", "off"), default="auto",
                    help="接力后轻锐化（lanczos 路更重，esrgan 路更轻）")
    ap.add_argument("--force", action="store_true", help="短边≥2048 仍强制跑")
    ap.add_argument("--name", default=None, help="工作区名（独立输入时默认 hd_<文件名>）")
    ap.add_argument("--out", default="pattern_output", help="输出根目录（默认 CWD pattern_output）")
    args = ap.parse_args()
    out_root = Path(args.out).resolve()

    if not args.input:
        ap.error("需要 input")
    src = Path(args.input)
    if not src.exists():
        print(json.dumps({"ok": False, "error": f"输入不存在：{src}"}, ensure_ascii=False))
        return 1

    # 工作区识别（§7）：01_extracted/motif_XX.png → 原工作区；目录 → 循环全件
    if src.is_dir():
        imgs = sorted([p for p in src.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")
                       and not p.name.startswith("_")])
        ws_fixed = src.parent.resolve() if src.name == "01_extracted" else None
        jobs = [(p, ws_fixed) for p in imgs]
    else:
        ws0 = src.parent.parent.resolve() if src.parent.name == "01_extracted" else None
        jobs = [(src, ws0)]
    if not jobs:
        print(json.dumps({"ok": False, "error": "目录里没有可处理图片"}, ensure_ascii=False))
        return 1

    results, ws_details = [], {}
    for i, (p, ws_hint) in enumerate(jobs, 1):
        ws = ws_hint or ensure_workspace(out_root, args.name or f"hd_{p.stem}")
        if ws_hint is None:
            save_raw(ws, p)
        try:
            r = enhance_one(p, args, ws)
        except Exception as e:  # 永不硬失败：异常也走反馈层
            r = {"ok": False, "error": f"{e.__class__.__name__}: {e}", "files": [], "warnings": [],
                 "next_action": "失败进反馈层：像素档/手动档/换图 三选一", "stem": p.stem}
        r["input"] = p.name
        if r.get("_ws") is not None:
            ws_details.setdefault(r["_ws"], []).append(r.pop("detail", {"stem": p.stem}))
        r.pop("_ws", None)
        results.append(r)
        print(f"[{i}/{len(jobs)}] " + json.dumps(r, ensure_ascii=False), flush=True)

    # 02_hd/meta.json（细账，多图聚合成 items）+ ws 台账 record_step（lineage）
    for ws, details in ws_details.items():
        out_dir = ws / "02_hd"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "meta.json").write_text(json.dumps({"items": details}, ensure_ascii=False, indent=2),
                                           encoding="utf-8")
        record_step(ws, "hd", {"files": [f"{d.get('source', '')}" for d in details],
                               "items": [{k: d.get(k) for k in
                                          ("lineage", "upscale_method", "retries", "seconds")} for d in details]})
    ok_all = all(r.get("ok") for r in results)
    if len(results) > 1:
        print(f"DONE ok={sum(1 for r in results if r.get('ok'))}/{len(results)}", flush=True)
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
