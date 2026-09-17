#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板块5 无缝循环主链（05-无缝循环.md v2）：入口三路分岔——
A 排（花型件→wrap 环绕布点 tile，构造性无缝，免费） /
B 切（满幅重复稿→FFT 自相关找周期裁 repeat 单元，免费） /
C 修（满幅不重复→blend 修缝兜底免费；--seam-fix engine 生成式修缝=花钱显式选，roll 后缝在中央把全局难题切局部）。

宪法：无缝要可证明，证明不了的亮明身份——verdict: constructive / period-crop / seam-fixed 不混装。
统一出口标尺：边缘跳变比 ≤1.2（patternlib.edge_jump_ratio）+ 3×3 铺贴预览。
菜单优先：不带任何行动参数 = 零成本回菜单（含入口信号判定），不擅自生成、不擅自花钱。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).parent))

from engines import EngineError, generate, pick_gpt_size, pick_qwen_size, resolve_engine  # noqa: E402
from patternlib import (DENSITY_PRESETS, LAYOUTS, ROTATIONS, SCALE_PRESETS, STEP_DIRS,  # noqa: E402
                        content_crop, crop_period, edge_jump_ratio, emit, ensure_workspace,
                        find_period, grid_points, measure_coverage, mirror_tile,
                        nn_min_dist_torus, on_white, poisson_points_torus, record_step,
                        remove_bg, render_repeat_wrap, save_raw, seam_blend, step_dir)
from triage_card import _parse_json, offline_card, vlm_card  # noqa: E402

CONF_FLOOR = 0.35    # 路B 自相关置信度门槛：低于=峰弱，不硬裁（置信度说话）
JUMP_CEIL = 1.2      # 边缘跳变比通过线（05 §6 统一出口标尺）
NN_KEEP = 0.95       # 环面最小间距达成率（防接缝两侧挤叠）
DPI_DEFAULT = 150
TILE_DEFAULT = 1024

PRESET_PLANS = {  # 路A 预设（轴组合，05 §4）：preset < 显式 CLI 旗标
    "ditsy":           {"layout": "toss", "rotation": "two-way", "density": "medium", "scale": "ditsy"},
    "vintage-allover": {"layout": "half-drop", "rotation": "one-way", "density": "dense", "scale": "medium"},
    "street-packed":   {"layout": "toss", "rotation": "four-way", "density": "packed", "scale": "large"},
    "geo-grid":        {"layout": "straight", "rotation": "one-way", "density": "dense", "scale": "medium"},
    "airy":            {"layout": "toss", "rotation": "one-way", "density": "sparse", "scale": "ditsy"},
}

SEAM_PROMPT = (
    "This image is one seamless textile repeat tile that has been shifted (rolled) by half its "
    "width and height, so the tiling seams now form a cross in the CENTER of the image. "
    "Repair ONLY the central cross-shaped seam region: make the pattern join continuously across "
    "it, matching the surrounding motif shapes, colors, line weight and lighting exactly. "
    "Do not add new motifs, do not remove existing ones, do not change style, colors or scale, "
    "and leave the outer 60% of the image (including all four edges) untouched."
)
SEAM_PROMPT_STRICT = SEAM_PROMPT + (
    " CRITICAL: the four outer edges must remain pixel-identical to the input; only the central "
    "seam cross may change. The result must still read as one continuous pattern."
)

BODY_CHECK_PROMPT = (
    "Compare the two images: image 1 = original seamless tile, image 2 = seam-repaired tile. "
    'Answer ONLY JSON: {"same_pattern": true or false, "notes": "...", "verdict": "pass" or "fail"}. '
    "pass = same motifs, same style, same colors, nothing added or removed (local seam repair only)."
)


# ---------- 入口信号（离线可判，菜单期就给，不花一分钱） ----------

def route_signal(img: Image.Image) -> dict:
    """三路分岔信号：alpha 完好→A；满幅+自相关峰强→B；满幅+峰弱→C。纯像素判定。"""
    rgba = img.convert("RGBA")
    a = np.array(rgba.split()[-1])
    alpha_frac = float((a < 250).mean())       # 非全透明像素占比的补：有真透明通道吗
    cover = float((a > 8).mean())              # 内容占比
    reasons, period = [], None
    if alpha_frac >= 0.02 and cover <= 0.85:
        suggest = "A"
        reasons.append(f"透明通道完好（透明占比 {alpha_frac:.2f}，内容占比 {cover:.2f}）→ 花型件排布")
    else:
        pw, ph, conf = find_period(rgba)
        period = [int(pw), int(ph), conf]
        if conf >= CONF_FLOOR:
            suggest = "B"
            reasons.append(f"满幅 + 自相关峰强（周期≈{pw}x{ph}px, conf={conf}）→ 周期裁切")
        else:
            suggest = "C"
            why = f"满幅 + 自相关峰弱（conf={conf} < {CONF_FLOOR}）" if conf else "满幅 + 无可测周期"
            reasons.append(f"{why} → 修缝（blend 免费，生成式显式选）")
    return {"suggest": suggest, "alpha_frac": round(alpha_frac, 3), "content_ratio": round(cover, 3),
            "period": period, "reasons": reasons}


# ---------- 判型卡（资格门沿用全模块公共件） ----------

def get_card(img: Image.Image, args, warnings: list) -> dict:
    if args.no_card or not os.environ.get("DASHSCOPE_API_KEY"):
        card = offline_card(img)
        warnings.append("--no-card：跳过 VLM 判型" if args.no_card
                        else "无 DASHSCOPE_API_KEY：判型走离线档（资格门未验稿，请人工确认输入是花稿）")
        return card
    try:
        return vlm_card(img)
    except Exception as e:  # 永不硬失败：VLM 挂了降离线档
        warnings.append(f"VLM 判型失败（{type(e).__name__}），降离线档")
        return offline_card(img)


# ---------- 工作区续接（输入落在 01-06 任一步目录即复用原工作区） ----------

def resolve_workspace(src: Path, out_arg: str | None) -> tuple:
    if src.parent.name in set(STEP_DIRS.values()):
        ws = src.parent.parent
        return ws, step_dir(ws, "seamless"), False
    out_root = Path(out_arg) if out_arg else Path.cwd() / "pattern_output"
    ws = ensure_workspace(out_root, src.stem)
    save_raw(ws, src)
    return ws, step_dir(ws, "seamless"), True


def find_pending_collect(out_arg: str | None, warnings: list) -> tuple | None:
    """--collect 单独回接（不给原图）：在工作区根下找最近一次手动档留下的 roll 图。"""
    out_root = Path(out_arg) if out_arg else Path.cwd() / "pattern_output"
    rolled = sorted(out_root.glob("*/05_seamless/*_rolled_for_fix.png"),
                    key=lambda p: p.stat().st_mtime)
    if not rolled:
        return None
    r = rolled[-1]
    warnings.append(f"未给原图：按最近的手动档工作区续接 {r.parent.parent.name}")
    return r.parent.parent, r.parent, r.stem[: -len("_rolled_for_fix")]


# ---------- 参数解析（preset < 显式 CLI；缺轴补行业默认） ----------

def resolve_plan(args, warnings: list) -> dict:
    plan = {"tile": TILE_DEFAULT, "dpi": DPI_DEFAULT, "seed": args.seed, "quality": args.quality,
            "layout": "half-drop", "rotation": "two-way", "density": "medium",
            "scale": "medium", "bg": "transparent", "seam_fix": "blend"}
    if args.tile:
        if not 256 <= args.tile <= 4096:
            warnings.append(f"--tile {args.tile} 超出 256-4096，已收敛")
        plan["tile"] = max(256, min(4096, args.tile))
    if args.dpi:
        plan["dpi"] = max(72, min(600, args.dpi))
    if args.bg:
        plan["bg"] = args.bg
    if args.seam_fix:
        plan["seam_fix"] = args.seam_fix
    if args.preset:
        plan.update(PRESET_PLANS[args.preset])
        if args.layout or args.rotation or args.density or args.scale:
            warnings.append("preset 与显式轴并存：显式旗标覆盖 preset 对应轴")
    for k in ("layout", "rotation", "density", "scale"):
        if getattr(args, k):
            plan[k] = getattr(args, k)
    plan["scale_frac"] = SCALE_PRESETS[plan["scale"]]
    return plan


def pick_mode(args, sig: dict) -> tuple:
    """mode 显式 > 行动参数暗示 > 入口信号。返回 (mode, notes)。"""
    if args.mode and args.mode != "auto":
        return args.mode, []
    if args.period:
        return "crop", []
    if args.seam_fix:
        return "seam", []
    if args.preset or args.layout or args.rotation or args.density or args.scale:
        return "tile", []
    m = sig.get("suggest", "C")
    key = {"A": "tile", "B": "crop", "C": "seam"}[m]
    return key, [f"--mode auto：入口信号判为路{m}（{'；'.join(sig.get('reasons', []))}）"]


def cross_warnings(mode: str, args, warnings: list) -> None:
    if mode == "tile" and args.period:
        warnings.append("--period 只在路B（crop）生效，本次忽略")
    if mode in ("crop", "seam") and (args.layout or args.preset or args.rotation
                                     or args.density or args.scale):
        warnings.append("排布轴（layout/rotation/density/scale/preset）只在路A（tile）生效，本次忽略")
    if mode == "tile" and args.seam_fix:
        warnings.append("--seam-fix 只在路C（seam）生效，本次忽略")


# ---------- 路A：排布（构造性无缝） ----------

def angle_cycle(rotation: str, n: int, rng, jitter: float = 0.0) -> list:
    base = {"one-way": (0.0,), "two-way": (0.0, 180.0), "four-way": (0.0, 90.0, 180.0, 270.0)}[rotation]
    out = [float(base[i % len(base)]) for i in range(n)]
    if jitter:
        out = [a + float(rng.uniform(-jitter, jitter)) for a in out]
    return out


DENSITY_GAP = {"sparse": 1.5, "medium": 1.15, "dense": 0.95, "packed": 0.8}
DENSITY_CELL = {"sparse": 2.0, "medium": 1.4, "dense": 1.15, "packed": 1.0}


def route_A_points(layout: str, W: int, H: int, m: Image.Image, density: str, rng) -> tuple:
    """布点（密度=相对间距语义：间距/网格相对花型尺寸，覆盖率是结果不是目标，如实报告）。
    toss=环面泊松（间距环绕判定，packed 允许轻微叠压=纺织散点常态）；其余=刚性网格。
    返回 (points, flips, requested_dist)。"""
    dim = max(m.size)
    if layout == "toss":
        md = min(W / 2, H / 2, DENSITY_GAP[density] * dim)
        pts = poisson_points_torus(W, H, md, rng)
        for _ in range(2):                        # tile 太空守卫：n<4 收紧间距重撒
            if len(pts) >= 4:
                break
            md *= 0.75
            pts = poisson_points_torus(W, H, md, rng)
        return pts, np.zeros(len(pts), dtype=bool), md
    cell = DENSITY_CELL[density] * dim
    pts, flips = grid_points(layout, W, H, cell)
    for _ in range(2):
        if len(pts) >= 4:
            break
        cell *= 0.85
        pts, flips = grid_points(layout, W, H, cell)
    return pts, flips, cell


def run_tile(img: Image.Image, out_dir: Path, stem: str, plan: dict, warnings: list) -> dict:
    rgba = img.convert("RGBA")
    a = np.array(rgba.split()[-1])
    if float((a < 250).mean()) < 0.02:            # 无透明通道：白底花型件自动去底兜底一次
        rgba, done = remove_bg(rgba)
        a = np.array(rgba.split()[-1])
        cover = float((a > 8).mean())
        if not done or cover < 0.02 or cover > 0.85:
            # 去底无从下手 / 去完啥也不剩（满幅连续质地） / 去完仍满幅 → 都不是花型件
            return {"ok": False, "route": "A", "verdict": None, "files": [], "checks": {},
                    "warnings": warnings + ["输入是满幅不透明稿，无花型件可排"
                                            + (f"（去底后内容占比 {cover:.2f}）" if done else "")],
                    "next_action": "满幅稿请走 --mode crop（周期裁切）或 --mode seam（修缝）；"
                                   "满幅重排走板块4。要拿花型件先走板块1 提取"}
        warnings.append("输入无透明通道，已自动边缘去底；要标准透明底件请先走板块1 提取")
    m = content_crop(rgba)
    target = plan["scale_frac"] * plan["tile"]
    if max(m.size) > plan["tile"] * 0.9:
        target = plan["tile"] * 0.9               # 守卫：本体 > tile 会让相邻偏移拷贝自叠
        warnings.append("花型按 scale 预设超出 tile 上限，已收敛到 tile 的 90%")
    f = target / max(m.size)
    if abs(f - 1.0) > 1e-6:
        m = m.resize((max(1, int(m.width * f)), max(1, int(m.height * f))), Image.LANCZOS)
    W = H = plan["tile"]
    rng = np.random.default_rng(plan["seed"])
    pts, flips, dist = route_A_points(plan["layout"], W, H, m, plan["density"], rng)
    n = len(pts)
    if n < 4:
        warnings.append(f"布点数仅 {n}（tile 太空）：花型相对 tile 偏大，"
                        "建议 --scale ditsy/medium、提高 --density 或加大 --tile")
    angles = angle_cycle(plan["rotation"], n, rng, jitter=8.0 if plan["layout"] == "toss" else 0.0)
    scales = rng.uniform(0.85, 1.15, n) if plan["layout"] == "toss" else np.ones(n)
    tile_img = render_repeat_wrap(m, W, H, pts, angles, scales, flips)
    if plan["bg"] == "white":
        tile_img = Image.alpha_composite(Image.new("RGBA", (W, H), (255, 255, 255, 255)), tile_img)

    checks = {"points": int(n)}
    if plan["layout"] == "toss":
        nn = nn_min_dist_torus(pts, W, H)
        checks["nn_min_dist"] = round(nn, 1)
        if nn < NN_KEEP * dist:
            warnings.append(f"环面最小间距 {nn:.0f}px < 目标 {dist:.0f}px（接缝两侧有挤叠风险）")
    cov = measure_coverage(tile_img)
    checks["coverage"] = round(cov, 3)
    if plan["bg"] == "transparent" and cov < 0.04:
        warnings.append(f"覆盖率仅 {cov:.2f}（近乎空 tile）：花型相对 tile 太小，建议提高 --scale/--density")
    jump = edge_jump_ratio(tile_img)
    checks["edge_jump_ratio"] = round(jump, 3)
    verdict = "constructive" if jump <= JUMP_CEIL else "check-failed"
    if jump > JUMP_CEIL:
        warnings.append(f"边缘跳变比 {jump:.2f} > {JUMP_CEIL}：构造性无缝不应出现此结果，请把本图反馈排查")

    out = _vname(out_dir, stem, plan["layout"], plan["tile"])
    _save_png(tile_img, out, plan["dpi"])
    files = [out, preview_3x3(tile_img, out.with_name(out.stem + "_preview3x3.jpg"))]
    return {"ok": verdict != "check-failed", "route": "A", "mode": "tile", "verdict": verdict,
            "files": files, "checks": checks,
            "params": {k: plan[k] for k in ("layout", "rotation", "density", "scale",
                                            "bg", "tile", "dpi", "seed")},
            "warnings": warnings, "next_action": None}


# ---------- 路B：周期裁切 ----------

def run_crop(img: Image.Image, out_dir: Path, stem: str, plan: dict, args, warnings: list) -> dict:
    rgba = img.convert("RGBA")
    if args.period:
        try:
            pw, ph = (int(v) for v in args.period.lower().split("x", 1))
        except ValueError:
            return {"ok": False, "route": "B", "verdict": None, "files": [], "checks": {},
                    "warnings": warnings + [f"--period 格式应为 WxH（收到 {args.period}）"],
                    "next_action": "例：--period 512x512"}
        conf = None
        warnings.append(f"周期手动指定 {pw}x{ph}（未经自相关验证，跳变比见 checks）")
    else:
        pw, ph, conf = find_period(rgba)
        if conf < CONF_FLOOR:
            return {"ok": False, "route": "B", "verdict": None, "files": [],
                    "checks": {"period": [int(pw), int(ph)], "confidence": conf},
                    "warnings": warnings,
                    "next_action": f"自相关峰弱（conf={conf} < {CONF_FLOOR}），硬裁会得到假周期。"
                                   "改走 --mode seam --seam-fix blend（免费兜底）；"
                                   "确信有周期就 --period WxH 手动指定"}
    tile = crop_period(rgba, pw, ph, plan["tile"])
    jump = edge_jump_ratio(tile)
    checks = {"period": [int(pw), int(ph)], "confidence": conf,
              "edge_jump_ratio": round(jump, 3), "tile": list(tile.size)}
    out = out_dir / f"{stem}_period_{int(pw)}x{int(ph)}.png"
    _save_png(tile, out, plan["dpi"])
    files = [out, preview_3x3(tile, out.with_name(out.stem + "_preview3x3.jpg"))]
    verdict = "period-crop"
    if jump > JUMP_CEIL:
        warnings.append(f"裁切单元边缘跳变比 {jump:.2f} > {JUMP_CEIL}：源图非严格重复（近似周期）")
        fixed = seam_blend(tile)
        checks["edge_jump_ratio_blendfix"] = round(edge_jump_ratio(fixed), 3)
        out2 = out.with_name(out.stem + "_blendfix.png")
        _save_png(fixed, out2, plan["dpi"])
        files.append(out2)
        files.append(preview_3x3(fixed, out2.with_name(out2.stem + "_preview3x3.jpg")))
        warnings.append("已附 blend 修缝版（免费兜底），两版都交付供选")
    return {"ok": True, "route": "B", "mode": "crop", "verdict": verdict,
            "files": files, "checks": checks,
            "params": {"tile": plan["tile"], "dpi": plan["dpi"], "seed": plan["seed"]},
            "warnings": warnings, "next_action": None}


# ---------- 路C：修缝 ----------

def vlm_body_check(orig_rgba: Image.Image, fixed_rgba: Image.Image) -> dict | None:
    """生成式修缝的本体比对（VLM 可用才做）：修缝只准动缝带，不准动内容。"""
    if not os.environ.get("DASHSCOPE_API_KEY"):
        return None
    try:
        from dashscope_gen import vlm_chat
        text = vlm_chat([on_white(orig_rgba), on_white(fixed_rgba)], BODY_CHECK_PROMPT, timeout_s=120)
        d = _parse_json(text)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def run_seam(img: Image.Image, out_dir: Path, stem: str, plan: dict, args, warnings: list) -> dict:
    rgba = img.convert("RGBA")
    W, H = rgba.size
    method = plan["seam_fix"]
    params = {"method": method, "tile": list(rgba.size), "dpi": plan["dpi"]}

    if method == "blend":
        out = seam_blend(rgba)
        jump = edge_jump_ratio(out)
        path = out_dir / f"{stem}_seamfix_blend.png"
        _save_png(out, path, plan["dpi"])
        files = [path, preview_3x3(out, path.with_name(path.stem + "_preview3x3.jpg"))]
        warnings.append("blend 是数学兜底：边缘跳变比可证，但接缝带可能有涂抹/鬼影，看预览确认")
        return {"ok": jump <= JUMP_CEIL, "route": "C", "mode": "seam",
                "verdict": "seam-fixed" if jump <= JUMP_CEIL else "check-failed",
                "files": files, "checks": {"edge_jump_ratio": round(jump, 3)},
                "params": params, "warnings": warnings, "next_action": None}

    if method == "mirror":
        out = mirror_tile(rgba)
        jump = edge_jump_ratio(out)
        path = out_dir / f"{stem}_seamfix_mirror.png"
        _save_png(out, path, plan["dpi"])
        files = [path, preview_3x3(out, path.with_name(path.stem + "_preview3x3.jpg"))]
        warnings.append("mirror 改了构图（四象限镜像翻倍）——内容本体未改，构图已变，meta/verdict 如实标注")
        return {"ok": jump <= JUMP_CEIL, "route": "C", "mode": "seam",
                "verdict": "seam-fixed" if jump <= JUMP_CEIL else "check-failed",
                "files": files, "checks": {"edge_jump_ratio": round(jump, 3)},
                "params": params, "warnings": warnings, "next_action": None}

    if method == "manual":
        rolled = Image.fromarray(np.roll(np.roll(np.asarray(rgba), W // 2, axis=1), H // 2, axis=0))
        r_path = out_dir / f"{stem}_rolled_for_fix.png"
        _save_png(rolled, r_path, plan["dpi"])
        c_path = out_dir / "_seamfix_prompt_card.txt"
        c_path.write_text(
            "【板块5 无缝循环 · 生成式修缝提示词卡（手动档）】\n"
            f"工作区: {out_dir.parent.name}\n"
            "1. 上传 rolled_for_fix.png（已错半格，接缝=画面中央的十字）到任意修图工具\n"
            "   （ChatGPT / Nano Banana / 即梦均可）\n"
            "2. 提示词：\n" + SEAM_PROMPT + "\n"
            "3. 修好后把结果图路径交给 --collect 回接（自动 roll 回 + 边缘跳变校验）\n"
            "   例：python seamless.py <原图> --collect <修好的图>\n",
            encoding="utf-8")
        warnings.append("手动档：已出 roll 图（缝在中央）+ 提示词卡；修完 --collect 回接校验")
        return {"ok": True, "route": "C", "mode": "seam", "verdict": "manual-pending",
                "files": [r_path, c_path], "checks": {},
                "params": params, "warnings": warnings,
                "next_action": f"--collect <修好的图路径> 回接（自动 roll 回 + 跳变校验）"}

    # ---- engine：生成式修缝（花钱显式档，05 §5：先 blend 后 gen）----
    engine = resolve_engine(args.engine or "auto")
    if not engine:
        warnings.append("无可用生图 key，降手动档（提示词卡）")
        return run_seam(img, out_dir, stem, plan | {"seam_fix": "manual"}, args, warnings)
    blend_img = seam_blend(rgba)
    b_path = out_dir / f"{stem}_seamfix_blend.png"
    _save_png(blend_img, b_path, plan["dpi"])
    files = [b_path, preview_3x3(blend_img, b_path.with_name(b_path.stem + "_preview3x3.jpg"))]
    warnings.append("blend 兜底版已同场免费交付（先便宜后贵）")

    rolled = Image.fromarray(np.roll(np.roll(np.asarray(rgba), W // 2, axis=1), H // 2, axis=0))
    size = pick_gpt_size(W, H) if engine == "gpt" else pick_qwen_size(W, H)
    best = None
    for attempt in (1, 2):                        # 回炉≤2，每次换变量（提示词加严）
        prompt = SEAM_PROMPT if attempt == 1 else SEAM_PROMPT_STRICT
        res, meta = generate([rolled], prompt, engine, quality=plan["quality"], size=size)
        if res.size != (W, H):
            res = res.convert("RGBA").resize((W, H), Image.LANCZOS)
            meta["resized_to"] = [W, H]
        fixed = Image.fromarray(np.roll(np.roll(np.asarray(res.convert("RGBA")),
                                                -(W // 2), axis=1), -(H // 2), axis=0))
        jump = edge_jump_ratio(fixed)
        if best is None or jump < best[1]:
            best = (fixed, jump, meta, attempt)
        if jump <= JUMP_CEIL:
            break
    fixed, jump, meta, attempt = best
    checks = {"edge_jump_ratio": round(jump, 3), "attempts": attempt, "engine": meta}
    if jump > JUMP_CEIL:
        warnings.append(f"生成式修缝 {attempt} 次后边缘跳变仍 {jump:.2f} > {JUMP_CEIL}："
                        "已保留跳变最小的一版（verdict 如实标注），可用 blend 兜底版交付")
    else:
        body = vlm_body_check(rgba, fixed)
        if body:
            checks["vlm_body_check"] = body
            if str(body.get("verdict", "")).lower() != "pass":
                warnings.append(f"VLM 本体比对未过：{body.get('notes', '')}（修缝动了不该动的内容）")
    e_path = out_dir / f"{stem}_seamfix_engine.png"
    _save_png(fixed, e_path, plan["dpi"])
    files += [e_path, preview_3x3(fixed, e_path.with_name(e_path.stem + "_preview3x3.jpg"))]
    return {"ok": jump <= JUMP_CEIL, "route": "C", "mode": "seam",
            "verdict": "seam-fixed" if jump <= JUMP_CEIL else "check-failed",
            "files": files, "checks": checks, "params": params,
            "warnings": warnings, "next_action": None}


# ---------- 手动档回接 ----------

def run_collect(collect_path: str, out_dir: Path, stem: str, plan: dict, warnings: list) -> dict:
    fixed = Image.open(Path(collect_path)).convert("RGBA")
    fixed.load()
    r_path = out_dir / f"{stem}_rolled_for_fix.png"
    if r_path.exists() and fixed.size == Image.open(r_path).size:
        W, H = fixed.size
        fixed = Image.fromarray(np.roll(np.roll(np.asarray(fixed), -(W // 2), axis=1), -(H // 2), axis=0))
        warnings.append("检测到 rolled_for_fix 图：已自动 roll 回（缝从中央搬回四边）")
    jump = edge_jump_ratio(fixed)
    out = out_dir / f"{stem}_seamfix_manual.png"
    _save_png(fixed, out, plan["dpi"])
    files = [out, preview_3x3(fixed, out.with_name(out.stem + "_preview3x3.jpg"))]
    ok = jump <= JUMP_CEIL
    return {"ok": ok, "route": "C", "mode": "collect",
            "verdict": "seam-fixed" if ok else "check-failed",
            "files": files, "checks": {"edge_jump_ratio": round(jump, 3)},
            "params": {"method": "manual", "dpi": plan["dpi"]},
            "warnings": warnings + ([] if ok else
                                    [f"边缘跳变比 {jump:.2f} > {JUMP_CEIL}：手动修缝未过校验，如实标注"]),
            "next_action": None if ok else "换一版修缝稿再 --collect，或走 --mode seam --seam-fix blend 兜底"}


# ---------- 预览 / 落盘 / 版本 ----------

def _save_png(img: Image.Image, path: Path, dpi: int) -> Path:
    img.save(path, dpi=(dpi, dpi))
    return path


def _vname(out_dir: Path, stem: str, layout: str, tile: int) -> Path:
    n = 1
    while (out_dir / f"{stem}_{layout}_t{tile}_v{n}.png").exists():
        n += 1
    return out_dir / f"{stem}_{layout}_t{tile}_v{n}.png"


def preview_3x3(tile: Image.Image, out_path: Path) -> Path:
    """3×3 铺贴预览（白底 + 灰线标 tile 边界）——无缝好不好，贴出来看。"""
    t = tile.convert("RGBA")
    w, h = t.size
    canvas = Image.new("RGBA", (w * 3, h * 3), (255, 255, 255, 255))
    for dx in (0, 1, 2):
        for dy in (0, 1, 2):
            canvas.alpha_composite(t, (dx * w, dy * h))
    rgb = canvas.convert("RGB")
    if max(rgb.size) > 1800:
        f = 1800 / max(rgb.size)
        rgb = rgb.resize((int(rgb.width * f), int(rgb.height * f)), Image.LANCZOS)
    d = ImageDraw.Draw(rgb)
    f = rgb.width / (w * 3)
    lw = max(1, rgb.width // 1200)
    for k in (1, 2):
        d.line([(int(w * k * f), 0), (int(w * k * f), rgb.height)], fill=(165, 165, 165), width=lw)
        d.line([(0, int(h * k * f)), (rgb.width, int(h * k * f))], fill=(165, 165, 165), width=lw)
    d.text((6, rgb.height - 16), "3X3 SEAMLESS PREVIEW | gray lines = tile boundaries",
           fill=(90, 90, 90))
    rgb.save(out_path, quality=92)
    return out_path


# ---------- 菜单（零成本，不调任何 API） ----------

def build_menu(img: Image.Image | None, src: Path | None) -> dict:
    sig = route_signal(img) if img is not None else None
    return {
        "constitution": "无缝要可证明，证明不了的亮明身份（verdict: constructive/period-crop/seam-fixed）",
        "exit_metric": f"边缘跳变比 ≤{JUMP_CEIL} + 3×3 铺贴预览",
        "routes": [
            {"route": "A 排", "flag": "--mode tile",
             "desc": "花型件 → wrap 环绕布点 tile（构造性无缝，免费）",
             "example": "python seamless.py <花型件> --mode tile --preset ditsy"},
            {"route": "B 切", "flag": "--mode crop",
             "desc": "满幅重复稿 → 自相关找周期裁 repeat 单元（免费，置信度说话）",
             "example": "python seamless.py <满幅稿> --mode crop"},
            {"route": "C 修", "flag": "--mode seam",
             "desc": "满幅不重复稿 → blend 修缝（免费兜底）｜--seam-fix engine 生成式修缝（花钱显式）",
             "example": "python seamless.py <满幅稿> --mode seam --seam-fix engine"},
        ],
        "routeA_axes": {"--layout": list(LAYOUTS), "--rotation": list(ROTATIONS),
                        "--density": sorted(DENSITY_PRESETS), "--scale": sorted(SCALE_PRESETS),
                        "--bg": ["transparent", "white"],
                        "--tile": TILE_DEFAULT, "--dpi": DPI_DEFAULT,
                        "default": "half-drop / two-way / medium / medium / transparent"},
        "routeA_presets": {k: " ".join(f"{a}={v}" for a, v in p.items())
                           for k, p in PRESET_PLANS.items()},
        "signal": sig,
    }


def ordered(args) -> bool:
    """是否下了单（任一路线/轴/回接）。没下单=只出菜单，零成本。"""
    return bool(args.mode or args.preset or args.layout or args.rotation or args.density
                or args.scale or args.period or args.seam_fix or args.collect)


# ---------- main ----------

def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        description="板块5 无缝循环：菜单优先不默认生成；A 排 / B 切 / C 修 三路分岔（05-无缝循环.md v2）")
    ap.add_argument("input", nargs="?", help="输入图（花型件 / 满幅稿 / tile）")
    ap.add_argument("--mode", choices=["auto", "tile", "crop", "seam"],
                    help="路线：auto=按入口信号；缺省=行动参数暗示")
    ap.add_argument("--layout", choices=LAYOUTS, help="路A 布局（默认 half-drop）")
    ap.add_argument("--rotation", choices=ROTATIONS, help="路A 旋转（默认 two-way）")
    ap.add_argument("--density", choices=sorted(DENSITY_PRESETS),
                    help="路A 密度=相对间距（sparse 疏 / medium / dense / packed 密，默认 medium）")
    ap.add_argument("--scale", choices=sorted(SCALE_PRESETS), help="路A 花型相对大小（默认 medium）")
    ap.add_argument("--bg", choices=["transparent", "white"], help="路A 底（默认 transparent）")
    ap.add_argument("--preset", choices=sorted(PRESET_PLANS), help="路A 预设组合")
    ap.add_argument("--tile", type=int, help=f"tile 边长 px（默认 {TILE_DEFAULT}）")
    ap.add_argument("--dpi", type=int, help=f"落盘 DPI（默认 {DPI_DEFAULT}）")
    ap.add_argument("--seed", type=int, default=42, help="随机种子（默认 42，确定性复跑）")
    ap.add_argument("--period", help="路B 手动指定周期 WxH（跳过自相关）")
    ap.add_argument("--seam-fix", dest="seam_fix",
                    choices=["blend", "mirror", "engine", "manual"],
                    help="路C 修缝方式：blend 免费兜底 / mirror 镜像 / engine 生成式(花钱) / manual 提示词卡")
    ap.add_argument("--engine", choices=["gpt", "qwen"], help="路C engine 档指定引擎（默认按 key 自动）")
    ap.add_argument("--quality", choices=["draft", "final"], default="final",
                    help="生成式修缝档位（默认 final）")
    ap.add_argument("--collect", help="手动档回接：修好的图路径（自动 roll 回 + 校验）")
    ap.add_argument("--no-card", action="store_true", help="跳过 VLM 判型（省钱/离线）")
    ap.add_argument("--out", help="工作区根目录（默认 ./pattern_output；输入在 01-06 目录下则自动续接）")
    args = ap.parse_args()

    if not args.input and not args.collect:
        emit(True, [], warnings=["未给输入图"],
             menu=build_menu(None, None),
             next_action="先给输入图路径（python seamless.py <图>），会回带入口信号的菜单；不下单不生成")
        return 0
    src = Path(args.input) if args.input else None
    if src is not None and not src.exists():
        emit(False, [], warnings=[f"输入不存在：{src}"],
             next_action="检查路径（中文路径建议整体 quote）")
        return 1

    stem = src.stem if src else "collect"
    if args.collect and stem.endswith("_rolled_for_fix"):
        stem = stem[: -len("_rolled_for_fix")]    # 回接 roll 图：工作区/命名回到原花稿
    if not ordered(args):
        img = Image.open(src)
        img.load()
        emit(True, [], warnings=["菜单模式：未下发任何行动参数，零成本未调 API"],
             menu=build_menu(img, src),
             next_action="从 menu.routes 选一路下发参数（含 --mode ...）后才会生成")
        return 0

    cont: list = []
    if src is not None:
        ws, out_dir, fresh = resolve_workspace(src, args.out)
        if not fresh:
            cont.append(f"工作区续接：{ws.name}")
    else:
        found = find_pending_collect(args.out, cont)
        if not found:
            emit(False, [], warnings=cont,
                 next_action="未找到待回接的手动档（*_rolled_for_fix.png）；"
                             "请同时给原图：python seamless.py <原图> --collect <修好的图>")
            return 1
        ws, out_dir, stem = found
        cont.append(f"工作区续接：{ws.name}")
    warnings = cont
    plan = resolve_plan(args, warnings)
    if args.collect:
        mode, notes = "collect", []
    else:
        mode, notes = pick_mode(args, route_signal(Image.open(src)))
    warnings = notes + warnings
    cross_warnings(mode, args, warnings)
    global args_collect_path
    args_collect_path = args.collect

    card = None
    if not args.collect:
        card = get_card(Image.open(src), args, warnings)
        if card["gate"]["action"] == "route_extract":
            emit(False, [], warnings=warnings + ["资格门：这不是完整花稿（疑似成衣/局部/衣服照）"],
                 verdict=None, route=mode,
                 next_action="先走板块1 图案提取（pattern-extraction）拿花稿再回来")
            return 1
        if card["gate"]["is_pattern"] == "uncertain":
            warnings.append("资格门未确认（离线档/不确定），请人工确认输入是花稿")
        if mode == "tile" and card["carrier"] == "allover":
            warnings.append("判型为满幅稿：排布路通常吃花型件；仍按指示执行，建议先看 --mode crop/seam")
        try:
            if mode == "tile":
                res = run_tile(Image.open(src), out_dir, stem, plan, warnings)
            elif mode == "crop":
                res = run_crop(Image.open(src), out_dir, stem, plan, args, warnings)
            else:
                res = run_seam(Image.open(src), out_dir, stem, plan, args, warnings)
        except EngineError as e:
            emit(False, [], warnings=warnings + [f"生成引擎失败：{e}"], route=mode, verdict=None,
                 next_action="--seam-fix blend 免费兜底可先交付；或检查 OPENAI_API_KEY/OPENAI_BASE_URL 后重试")
            return 1
        except Exception as e:
            emit(False, [], warnings=warnings + [f"执行失败：{type(e).__name__}: {e}"],
                 route=mode, verdict=None,
                 next_action="把本条 JSON 反馈排查；路A/路B/blend 修缝是纯代码路线，不依赖任何 key，理应可跑")
            return 1
    else:
        res = run_collect(args.collect, out_dir, stem, plan, warnings)

    if res.get("ok"):
        record_step(ws, "seamless", {
            "route": res.get("route"), "mode": res.get("mode"), "verdict": res.get("verdict"),
            "params": res.get("params"), "checks": res.get("checks"),
            "files": [Path(f).name for f in res.get("files", [])],
            "card_model": (card or {}).get("_model"), "source": src.name if src else None})
    emit(res.get("ok", False), res.get("files", []), warnings=res.get("warnings", []),
         next_action=res.get("next_action"), route=res.get("route"), mode=res.get("mode"),
         verdict=res.get("verdict"), checks=res.get("checks"), params=res.get("params"),
         workspace=ws.name, card_model=(card or {}).get("_model"))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
