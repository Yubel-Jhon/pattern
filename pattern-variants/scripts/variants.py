# -*- coding: utf-8 -*-
"""板块4 图案变体主链：资格门 → 方向裁决 → 菜单/下单 → 生成 → 量化校验 → 联络表。

两方向分岔（04-图案变体.md v2.3）：
  方向一 · 直接处理  花稿可参数化 → 菜单（选项 + 一句话解析）→ 点单/preset/采样 → 量化校验。
                     宪法 = 只改排布与配色，不改图案本体（纯代码档数学保证）。
  方向二 · 自由生成  复杂图案/轴外需求 → --free "提示词"，提示词+原图直接交生图模型，
                     不解析不拼模板不质检（提示词即宪法），产物 verdict=free 如实标注。

菜单优先，不默认生成：不带任何变体参数时只出菜单（零生成）。方向二花真金白银，
必须用户显式选择（--free / --engine gpt|qwen）才走。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from engines import (EngineError, generate, gpt_caps, pick_gpt_size, pick_qwen_size,
                     qwen_caps, resolve_engine)
from patternlib import (COLOR_PRESETS, DENSITY_PRESETS, FRAMES, LAYOUTS, ROTATIONS,
                        SCALE_PRESETS, STEP_DIRS, content_crop, emit, ensure_workspace,
                        grid_points, hue_mean_deg, measure_coverage, nn_min_dist, on_white,
                        palette_hex, poisson_points, record_step, remove_bg, render_repeat,
                        save_raw, tilt_points, transform_color, zoom_allover)


def has_alpha(img: Image.Image) -> bool:
    return img.mode == "RGBA" and bool((np.array(img.split()[-1]) < 250).any())
from triage_card import VLM_MODELS, _parse_json, offline_card, vlm_card

MAX_COUNT = 12       # 单次系列张数上限（成本护栏，生成式部分另按张计费）
MAX_FREE = 4         # 方向二单次张数上限
COV_BAND = 0.25      # 覆盖率兑现带宽 ±25%
NN_FLOOR = 0.95      # toss 最近邻间距不得低于布点最小距的 95%


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ============ 菜单（本板块的交互核心：选项给固定项挑，自由给一句话写）============

MENU = {
    "axes": [
        {"axis": "layout", "zh": "排列（怎么重复铺）",
         "options": {"straight": "直排网格", "half-drop": "半降（行业默认错落）", "brick": "砖形",
                     "mirror": "镜像", "toss": "散点（丰富度主增量，配 --rotation）"},
         "flag": "--layout", "free": "“斜着排”“乱一点但别太挤”（--custom 一句话）"},
        {"axis": "rotation", "zh": "方向性（toss 专属）",
         "options": {"one-way": "不转", "two-way": "±20° 乱转（默认）", "four-way": "0-360° 随便转"},
         "flag": "--rotation"},
        {"axis": "density", "zh": "密度（单位面积花出现多少）",
         "options": {k: f"覆盖率 ~{int(v * 100)}%" for k, v in DENSITY_PRESETS.items()},
         "flag": "--density", "free": "“比原图疏一半”"},
        {"axis": "scale", "zh": "比例（花相对画布大小）",
         "options": {k: {"ditsy": "小碎花", "medium": "中花", "large": "大花",
                         "placement": "独幅定位（一朵居中占大头）"}[k] for k in SCALE_PRESETS},
         "flag": "--scale", "free": "“花朵再大两成，别顶到边”"},
        {"axis": "color", "zh": "配色（变换类；指定色板重上色见板块6）",
         "options": {"hue30/hue90/hue180": "色相旋转（度数任意 hue<N>）", "mono": "单色化",
                     "duotone": "双色调", "invert": "反相（花底互换）",
                     "vintage": "做旧低饱和", "bright": "提亮高饱和", "gray": "灰调"},
         "flag": "--color"},
        {"axis": "tilt", "zh": "整体斜排", "options": {"0": "不斜", "45": "斜 45°", "90": "斜 90°"},
         "flag": "--tilt"},
        {"axis": "bg", "zh": "底色", "options": {"white": "白底", "transparent": "透明底", "#hex": "指定色"},
         "flag": "--bg"},
        {"axis": "frame", "zh": "画幅", "options": dict(square="正方", **{"2:3": "竖 2:3", "3:2": "横 3:2"},
                                                    width145="面料幅宽横条"), "flag": "--frame"},
    ],
    "presets": {"ditsy": "清新碎花=toss双向+小花+稀疏", "vintage-allover": "复古满铺=half-drop+大花+密集+做旧",
                "placement": "独幅主花=placement+白底+留边", "street-packed": "高街满铺=brick+大花+满铺+提亮",
                "airy": "素雅留白=half-drop+中花+稀疏+灰调"},
    "free_mode": {"flag": "--free <提示词>",
                  "when": "复杂图案（照片级/扎染/满幅肌理）参数轴改不好，或需求在轴外",
                  "note": "提示词+原图直接交生图模型，跳过所有轴、不解析不质检；产物 verdict=free 单独标注"},
}

PRESET_PLANS = {
    "ditsy": {"layout": "toss", "rotation": "two-way", "scale": "ditsy", "density": "sparse"},
    "vintage-allover": {"layout": "half-drop", "scale": "large", "density": "dense", "color": "vintage"},
    "placement": {"scale": "placement", "bg": "white"},
    "street-packed": {"layout": "brick", "scale": "large", "density": "packed", "color": "bright"},
    "airy": {"layout": "half-drop", "scale": "medium", "density": "sparse", "color": "gray"},
}

CUSTOM_PARSE_PROMPT = """You map a user's free-form textile-pattern variant request onto fixed parameters.
Available parameters (null = not requested, will use default):
{{"layout":"straight|half-drop|brick|mirror|toss|null",
 "rotation":"one-way|two-way|four-way|null",
 "tilt":0|45|90|other number|null,
 "density":"sparse|medium|dense|packed|number 0.15-0.95|null",
 "scale":"ditsy|medium|large|placement|number 0.15-0.8|null",
 "color":"hue<degrees like hue30>|invert|mono|duotone|vintage|bright|gray|null",
 "bg":"white|transparent|#rrggbb|null"}}
User request: "{req}"
Rules: map faithfully, guess numbers when the user gives relative wording ("sparser by half").
Size / density / layout / color requests are ALL in scope — map them, never put them in out_of_scope.
Put in "out_of_scope" (one short Chinese sentence) ONLY requests to redraw or redesign the artwork
itself (add/remove/redraw elements, change art style, add new motifs) and ignore those in the parameters.
If nothing is out of scope, set "out_of_scope" to null — never leave a comment there.
Output strict JSON only."""


# ============ 参数解析小件 ============

def parse_density(s):
    if s is None:
        return None
    if s in DENSITY_PRESETS:
        return DENSITY_PRESETS[s]
    try:
        v = float(s)
    except ValueError:
        raise SystemExit(json.dumps({"ok": False, "warnings": [f"无法解析 --density：{s}"],
                                     "next_action": "sparse|medium|dense|packed 或 0.15-0.95 数值"}))
    return max(0.15, min(0.95, v))


def parse_scale(s):
    if s is None:
        return None
    if s in SCALE_PRESETS:
        return SCALE_PRESETS[s]
    try:
        v = float(s)
    except ValueError:
        raise SystemExit(json.dumps({"ok": False, "warnings": [f"无法解析 --scale：{s}"],
                                     "next_action": "ditsy|medium|large|placement 或 0.15-0.8 数值"}))
    return max(0.15, min(0.8, v))


def parse_color(s):
    if s is None or s == "none":
        return "none"
    if s.startswith("hue") and (s == "hue" or s[3:].isdigit()):
        return f"hue{int(s[3:] or 30)}"
    if s in COLOR_PRESETS:
        return s
    raise SystemExit(json.dumps({"ok": False, "warnings": [f"无法解析 --color：{s}"],
                                 "next_action": "hue<N>|invert|mono|duotone|vintage|bright|gray|none"}))


def parse_bg(s):
    if s in (None, "white"):
        return (255, 255, 255)
    if s == "transparent":
        return None  # 透明底
    if re.fullmatch(r"#[0-9a-fA-F]{6}", s or ""):
        return tuple(int(s[i:i + 2], 16) for i in (1, 3, 5))
    raise SystemExit(json.dumps({"ok": False, "warnings": [f"无法解析 --bg：{s}"],
                                 "next_action": "white|transparent|#rrggbb"}))


def vlm_parse_custom(req: str) -> dict:
    """1 次 VLM 把一句话解析成封闭参数（不做开放提案）。失败返回 {}。"""
    from dashscope_gen import vlm_chat
    for model in VLM_MODELS:
        try:
            raw = _parse_json(vlm_chat(
                [], CUSTOM_PARSE_PROMPT.format(req=req.replace('"', "'")),
                timeout_s=120, model=model))
            out = {"_model": model, "_out_of_scope": str(raw.get("out_of_scope", "") or "")[:150]}
            for k in ("layout", "rotation", "density", "scale", "color", "bg"):
                v = raw.get(k)
                if v and str(v).lower() != "null":
                    out[k] = str(v)
            t = raw.get("tilt")
            if t not in (None, "null", ""):
                try:
                    out["tilt"] = float(t)
                except (TypeError, ValueError):
                    pass
            return out
        except Exception:
            continue
    return {}


# ============ 方向裁决信号（脚本出建议，用户拍板）============

def direction_signal(rgba: Image.Image, card: dict | None) -> dict:
    """可测信号：alpha 完好度 / 色键可分离性 → suggest=direction1|direction2。"""
    a = np.array(rgba.convert("RGBA").split()[-1])
    alpha_ok = rgba.mode == "RGBA" and bool((a < 250).any()) and float((a > 8).mean()) < 0.97
    _, keyed = remove_bg(on_white(rgba))
    texture = (card or {}).get("texture", "flat")
    suggest = "direction1"
    notes = []
    if not alpha_ok and not keyed:
        notes.append("无透明通道且色键不可分离（照片级/满幅肌理）")
        suggest = "direction2"
    if texture == "photo":
        notes.append("质地判型 photo：像素变换在复杂边缘上易碎")
        suggest = "direction2"
    if keyed and not alpha_ok:
        notes.append("白底稿可自动色键 → 参数轴可用")
    return {"suggest": suggest, "signals": {"alpha_ok": alpha_ok, "colorkey": keyed,
                                            "texture": texture, "notes": notes}}


# ============ 方向一：参数化变体（纯代码主轴）============

def resolve_plan(args, card, warnings) -> dict:
    """preset < custom(VLM解析) < 显式CLI旗标；缺轴补推荐默认值。返回 plan dict。"""
    plan = {"layout": None, "rotation": None, "tilt": 0.0, "density": None, "scale": None,
            "color": None, "bg": None, "frame": args.frame, "_layout_explicit": False}
    if args.preset:
        if args.preset not in PRESET_PLANS:
            raise SystemExit(json.dumps({"ok": False, "warnings": [f"未知 preset：{args.preset}"],
                                         "next_action": f"可选：{', '.join(PRESET_PLANS)}"}))
        p = PRESET_PLANS[args.preset]
        plan.update(p)
        plan["_layout_explicit"] = "layout" in p
    if args.custom:
        parsed = vlm_parse_custom(args.custom)
        if not parsed:
            warnings.append("--custom 一句话解析失败（无 VLM 或解析失败）：改用逐轴选项，或换个说法重试")
        else:
            if parsed.get("_out_of_scope"):
                warnings.append(f"超出变体板块（改图案本体）的部分已忽略：{parsed['_out_of_scope']}——"
                                f"那类需求走 --free 自由模式")
            for k in ("layout", "rotation", "density", "scale", "color", "bg", "tilt"):
                if parsed.get(k) is not None:
                    plan[k] = parsed[k]
                    if k == "layout":
                        plan["_layout_explicit"] = True
        plan["_custom_req"] = args.custom
    if args.layout:
        plan["layout"] = args.layout
        plan["_layout_explicit"] = True
    if args.rotation:
        plan["rotation"] = args.rotation
    if args.tilt:
        plan["tilt"] = float(args.tilt)
    if args.density:
        plan["density"] = parse_density(args.density)
    if args.scale:
        plan["scale"] = parse_scale(args.scale)
    if args.color:
        plan["color"] = parse_color(args.color)
    plan["bg"] = parse_bg(args.bg)
    # 缺轴默认值（推荐认知，写进 meta）
    if plan["scale"] == "placement" or plan["scale"] == SCALE_PRESETS["placement"]:
        plan["layout"] = plan["layout"] or "straight"
    plan["layout"] = plan["layout"] or "half-drop"
    plan["rotation"] = plan["rotation"] or "two-way"
    plan["density"] = plan["density"] if plan["density"] is not None else DENSITY_PRESETS["medium"]
    plan["scale"] = plan["scale"] if plan["scale"] is not None else SCALE_PRESETS["medium"]
    plan["color"] = plan["color"] or "none"
    if plan["layout"] not in LAYOUTS:
        raise SystemExit(json.dumps({"ok": False, "warnings": [f"未知 --layout：{plan['layout']}"],
                                     "next_action": f"可选：auto|{', '.join(LAYOUTS)}"}))
    return plan


def plan_tag(plan: dict, v_idx: int, sampled: dict | None = None) -> str:
    """产物文件名标签：ASCII 参数摘要（可复现锚）。"""
    p = dict(plan)
    if sampled:
        p.update(sampled)
    lay = p["layout"] if p["layout"] != "toss" else f"toss{p['rotation'][0].upper()}"
    if p.get("tilt"):
        lay += f"x{int(p['tilt'])}"
    return (f"{lay}_s{int(round(p['scale'] * 100))}_d{int(round(p['density'] * 100))}"
            + (f"_{p['color']}" if p["color"] != "none" else ""))


def render_plan(plan: dict, v_idx: int, motif_rgba: Image.Image, W: int, H: int) -> tuple:
    """纯代码渲染单张变体（确定性：seed+v_idx 全决定）。返回 (RGBA画布, measures)。"""
    seed = (plan["seed"] * 1000 + v_idx) % (2 ** 32)
    rng = np.random.default_rng(seed)
    motif = transform_color(motif_rgba, plan["color"])
    m = content_crop(motif)
    scale_frac = float(plan["scale"])
    placement = plan["scale"] == SCALE_PRESETS["placement"]
    target = int(scale_frac * min(W, H))
    ratio = target / max(m.size)
    m = m.resize((max(1, int(m.width * ratio)), max(1, int(m.height * ratio))), Image.LANCZOS)
    a = np.array(m.split()[-1])
    area = float((a > 8).mean()) * m.width * m.height
    cov = float(plan["density"])
    measures = {"seed": int(seed), "target_coverage": cov, "target_scale_frac": scale_frac}
    flips = None
    if placement:
        points = np.array([[W / 2, H / 2]], dtype=float)
        angles, scales = np.array([0.0]), np.array([1.0])
        min_dist = float("inf")
    elif plan["layout"] == "toss":
        min_dist = 0.72 * (area / cov) ** 0.5
        min_dist = max(min_dist, max(m.size) * 0.55)  # 不重叠优先；覆盖率不足如实报
        points = poisson_points(W, H, min_dist, rng, max_points=600)
        n = len(points)
        deg = {"one-way": 0.0, "two-way": 20.0, "four-way": 180.0}[plan["rotation"]]
        angles = np.zeros(n) if deg == 0 else rng.uniform(-deg, deg, n) if deg < 90 else rng.uniform(0, 360, n)
        scales = rng.uniform(0.85, 1.15, n)
    else:
        cell = (area / cov) ** 0.5
        if cell < max(m.size) * 0.7:
            measures["overlap_warning"] = True
        points, flips = grid_points(plan["layout"], W, H, cell)
        angles, scales = np.zeros(len(points)), np.ones(len(points))
        min_dist = float(cell)
        if plan.get("tilt"):
            points = tilt_points(points, W, H, plan["tilt"])
            min_dist = float(cell)
    if plan.get("tilt") and plan["layout"] == "toss":
        pass  # toss 本身随机，tilt 无意义，忽略
    transparent = plan["bg"] is None
    # 先在透明画布上渲染并度量（覆盖率=花型 alpha 占比），再合成底色——
    # 否则白底下 alpha 全 255，覆盖率恒 1.0
    canvas = render_repeat(m, W, H, points, angles, scales, flips=flips,
                           bg=(255, 255, 255), transparent=True)
    cov_act = measure_coverage(canvas)
    if not transparent:
        canvas = Image.alpha_composite(Image.new("RGBA", (W, H), (*plan["bg"], 255)), canvas)
    measures.update({"n_motifs": int(len(points)), "actual_coverage": round(cov_act, 4),
                     "nn_min_dist": round(nn_min_dist(points), 1),
                     "min_dist": round(min_dist, 1) if min_dist != float("inf") else None,
                     "hue_deg": hue_mean_deg(canvas)})
    return canvas, measures


def build_pixel_variants(plan, motif_rgba, count, W, H):
    """count 张（sample 模式下未指定轴逐张采样；显式轴钉死）。"""
    items = []
    sample_axes = plan.get("_sample") or {}
    for i in range(count):
        v_plan = dict(plan)
        sampled = {}
        if sample_axes:
            rng = np.random.default_rng((plan["seed"] * 77 + i) % (2 ** 32))
            if "layout" in sample_axes:
                cyc = ["toss", "half-drop", "toss", "brick", "straight", "mirror"]
                v_plan["layout"] = cyc[i % len(cyc)]
                sampled["layout"] = v_plan["layout"]
            if "density" in sample_axes:
                v_plan["density"] = round(float(rng.uniform(0.3, 0.75)), 2)
                sampled["density"] = v_plan["density"]
            if "scale" in sample_axes:
                v_plan["scale"] = round(float(rng.uniform(0.25, 0.55)), 2)
                sampled["scale"] = v_plan["scale"]
            if "color" in sample_axes and i % 3 == 1:
                v_plan["color"] = f"hue{int(rng.integers(-60, 61) or 30)}"
                sampled["color"] = v_plan["color"]
        canvas, meas = render_plan(v_plan, i, motif_rgba, W, H)
        items.append({"canvas": canvas, "plan": v_plan, "sampled": sampled, "measures": meas})
    return items


def build_allover_coarse(plan, orig_rgba: Image.Image, out_dir: Path, stem: str) -> list:
    """满幅稿粗档（方向一）：换色像素级；比例=中心裁切变放（zoom 估算）。确定性，单张。"""
    color = plan["color"]
    zoom = 1.0
    if plan["scale"] not in (None, SCALE_PRESETS["medium"]):
        zoom = max(0.5, min(2.0, float(plan["scale"]) / SCALE_PRESETS["medium"]))
    img = transform_color(orig_rgba, color) if color != "none" else orig_rgba
    tag = "allover" + (f"_{color}" if color != "none" else "")
    if abs(zoom - 1.0) > 0.05:
        img = zoom_allover(img, zoom)
        tag += f"_z{int(round(zoom * 100))}"
    dst = out_dir / f"{stem}_{tag}_v1.png"
    img.save(dst)
    return [{"mode": "pixel", "label": tag, "file": str(dst),
             "measures": {"zoom": round(zoom, 2), "color": color, "note": "allover coarse"},
             "plan": {k: v for k, v in plan.items() if not k.startswith("_")}}]


# ============ 方向一生成式段（仅满幅稿排列轴 / engines.py）============

REARRANGE_PROMPT = """Take this textile pattern artwork. Every motif must stay EXACTLY as drawn:
same shapes, same colors, same details. {lock}Rearrange ONLY the layout into a {layout} repeat,
target motif coverage about {density} percent of the canvas, full-bleed edge to edge.
Do not add new elements, do not change the art style, do not change colors."""

REARRANGE_CHECK_PROMPT = """Image 1 = original textile pattern, image 2 = rearranged version.
Intended layout change: {layout}.
Answer strict JSON only:
{{"motifs_same":true或false,
 "layout_applied":true或false,
 "verdict":"pass或fail",
 "note":"<one short Chinese sentence>"}}
motifs_same=false if any motif is altered, added or removed (style may not change).
layout_applied=false if image 2 still shows image 1's layout.
verdict=fail ONLY when motifs_same=false."""


def vlm_rearrange_check(orig_white, out_white, layout) -> dict:
    from dashscope_gen import vlm_chat
    for model in VLM_MODELS:
        try:
            raw = _parse_json(vlm_chat([orig_white, out_white],
                                       REARRANGE_CHECK_PROMPT.format(layout=layout),
                                       timeout_s=120, model=model))
            ms, la = bool(raw.get("motifs_same", True)), bool(raw.get("layout_applied", True))
            return {"motifs_same": ms, "layout_applied": la,
                    "verdict": "pass" if ms else "fail",
                    "note": str(raw.get("note", ""))[:150], "_model": model}
        except Exception:
            continue
    return {"motifs_same": True, "layout_applied": True, "verdict": "unknown",
            "note": "VLM 不可用，未做本体核对", "_model": None}


def run_allover_rearrange(plan, orig_rgba, orig_white, engine, out_dir, stem, warnings):
    """满幅稿排列轴生成式重排：回炉≤2，每次换变量（本体变形→加锁定条款；排列没出→加强度）。"""
    items = []
    for attempt in range(1, 4):
        lock = "CRITICAL: every motif must be pixel-identical to the input. " if attempt > 1 else ""
        prompt = REARRANGE_PROMPT.format(lock=lock, layout=plan["layout"],
                                         density=int(plan["density"] * 100))
        imgs = [orig_white if engine == "qwen" else orig_rgba]
        try:
            raw_out, gen_meta = generate(imgs, prompt, engine, quality="medium",
                                         transparent=False,
                                         size=pick_qwen_size(*orig_rgba.size) if engine == "qwen"
                                         else pick_gpt_size(*orig_rgba.size))
        except Exception as e:
            warnings.append(f"生成式重排第{attempt}次失败：{str(e)[:120]}")
            if "key" in str(e).lower() or "401" in str(e):
                break
            continue
        rgb = np.asarray(raw_out.convert("RGB"), dtype=np.float64)
        if rgb.mean() < 8 and rgb.std() < 8:
            warnings.append(f"第{attempt}次出黑图，重试")
            continue
        out = raw_out.convert("RGBA")
        check = vlm_rearrange_check(orig_white, on_white(out), plan["layout"]) \
            if qwen_caps()["key"] else {"verdict": "unknown", "note": "无 VLM，未核对"}
        dst = out_dir / f"{stem}_{plan['layout']}_gen{'' if attempt == 1 else attempt}.png"
        out.save(dst)
        items.append({"layout": plan["layout"], "file": str(dst), "engine": engine,
                      "verdict": check["verdict"], "motifs_same": check["motifs_same"],
                      "layout_applied": check["layout_applied"], "note": check["note"],
                      "prompt": prompt, "attempts": attempt, "seconds": gen_meta["seconds"]})
        if check["verdict"] == "pass":
            break
        if not check["motifs_same"]:
            warnings.append(f"第{attempt}次本体变形 → 回炉加锁定条款")
        elif not check["layout_applied"]:
            warnings.append(f"第{attempt}次排列未出 → 回炉加强措辞（重排档措辞已最强，超次如实标注）")
            break
    if items and items[-1]["verdict"] != "pass":
        warnings.append("生成式重排未完全过检，已如实标注 verdict 交付；可用 --free 手写提示词精调")
    return items


# ============ 方向二：自由模式（提示词+原图 → 模型，中间无人动）============

def run_free(free_req, orig_rgba, orig_white, engine, count, want_transparent, out_dir, stem, warnings):
    items = []
    if engine == "manual":
        card = out_dir / "_free_prompt_card.txt"
        card.write_text(
            "【板块4 图案变体 · 自由模式提示词卡】\n"
            f"工作区: {out_dir.parent.name}\n"
            "用法：输入图 + 下面这段提示词（原样，不要再加工），喂给 ChatGPT/GPT-Image 等任意生图工具；\n"
            f"每张结果按  {stem}_free_manual[_序号].png  存进 {out_dir.name}/ ，\n"
            f"然后跑  python variants.py <原图> --collect  回接打包（自由模式不做一致性校验）。\n\n"
            "你的提示词（原文）：\n" + free_req + "\n", encoding="utf-8")
        return [{"mode": "free", "engine": "manual", "file": str(card), "prompt": free_req,
                 "verdict": "free"}]
    for i in range(min(count, MAX_FREE)):
        try:
            raw_out, gen_meta = generate(
                [orig_white if engine == "qwen" else orig_rgba], free_req, engine,
                quality="medium", transparent=(want_transparent and engine == "gpt"),
                size=pick_qwen_size(*orig_rgba.size) if engine == "qwen" else pick_gpt_size(*orig_rgba.size))
        except Exception as e:
            warnings.append(f"自由模式第{i + 1}张失败：{str(e)[:120]}")
            if "key" in str(e).lower() or "401" in str(e):
                break
            continue
        rgb = np.asarray(raw_out.convert("RGB"), dtype=np.float64)
        if rgb.mean() < 8 and rgb.std() < 8:
            warnings.append(f"自由模式第{i + 1}张黑图，换引擎重试一次或改写提示词")
            continue
        out = raw_out.convert("RGBA")
        if want_transparent and engine != "gpt" and not has_alpha(out):
            keyed, done = remove_bg(out)
            if done:
                out = keyed
                warnings.append("自由模式输出已自动色键去底")
        dst = out_dir / f"{stem}_free_v{i + 1}.png"
        out.save(dst)
        items.append({"mode": "free", "engine": engine, "file": str(dst), "prompt": free_req,
                      "verdict": "free", "seconds": gen_meta["seconds"]})
    return items


def collect_free(out_dir, stem):
    """自由模式手动档回接：收图进联络表（不质检，如实标注）。"""
    return [{"mode": "free", "engine": "manual", "verdict": "free", "file": str(f)}
            for f in sorted(out_dir.glob(f"{stem}_free*_manual*.png"))]


# ============ 联络表（方向一产物 + 方向二产物分区，诚实标注）============

def make_sheet(orig, items, free_items, path, cell=320):
    rows = [(["ORIGINAL"] + [it["label"] for it in items], [on_white(orig)] + [it["thumb"] for it in items])]
    if free_items:
        rows.append((["FREE MODE (unchecked)"] + [f"free" for _ in free_items],
                     [None] + [it["thumb"] for it in free_items]))
    cols = min(4, max(len(r[1]) for r in rows))
    total_h = sum(cell + 22 for labels, _ in rows)
    canvas = Image.new("RGB", (cols * cell, total_h), (250, 250, 250))
    d = ImageDraw.Draw(canvas)
    y = 0
    for labels, thumbs in rows:
        for i, (label, im) in enumerate(zip(labels, thumbs)):
            if im is not None:
                t = im.copy()
                t.thumbnail((cell - 8, cell - 8))
                canvas.paste(t, (i * cell + (cell - t.width) // 2, y + (cell - t.height) // 2))
            color = (60, 60, 60) if i == 0 else ((40, 140, 60) if "free" not in label.lower() else (200, 120, 30))
            d.text((i * cell + 4, y + cell + 4), label[:40], fill=color)
        y += cell + 22
    canvas.save(path, quality=88)


# ============ 主流程 ============

def ordered(args) -> bool:
    """是否下了单（任一内容轴/preset/custom/free/采样）。没下单=只出菜单。"""
    return bool(args.layout or args.density or args.scale or args.color or args.preset
                or args.custom or args.free or args.sample == "auto")


def variants_one(src: Path, args, out_root: Path) -> dict:
    t0 = time.time()
    warnings: list = []
    stem = src.stem
    ws = (src.parent.parent.resolve() if src.parent.name in
          ("00_raw", "01_extracted", "02_hd", "03_restyle")
          else ensure_workspace(out_root, args.name or f"var_{stem}"))
    if src.parent.name not in ("00_raw", "01_extracted", "02_hd", "03_restyle"):
        save_raw(ws, src)
    out_dir = ws / STEP_DIRS["variants"]
    out_dir.mkdir(parents=True, exist_ok=True)

    orig = Image.open(src)
    orig.load()
    orig_rgba = orig.convert("RGBA")
    orig_white = on_white(orig_rgba)

    # ---- 资格门（板块2 判型卡同源；非花稿连菜单都不出）----
    card = None
    if qwen_caps()["key"]:
        try:
            card = vlm_card(orig_rgba)
        except Exception as e:
            warnings.append(f"VLM 判型失败（{str(e)[:80]}），按输入原样继续")
    else:
        card = offline_card(orig_rgba)
        warnings.append("离线档判型：无法验稿（请确认输入是花稿）")
    if card["gate"]["is_pattern"] == "no":
        emit(False, [], [f"资格门拒收：这不是花稿（{card['gate']['reason']}）"],
             "先走板块1 图案提取（pattern-extraction 的 ai_extract.py），产物 motif_XX.png 直喂本板块")
        return {"ok": False, "_ws": str(ws)}
    if card["gate"]["is_pattern"] == "partial":
        warnings.append("输入疑似整衣/场景图而非独立花稿——建议先走板块1 提取")

    # ---- 方向裁决信号（脚本出建议，用户拍板）----
    signal = direction_signal(orig_rgba, card)
    carrier = args.carrier if args.carrier != "auto" else card["carrier"]

    # ---- 菜单模式（默认：不生成，零成本）----
    if not ordered(args) and not args.collect:
        doable = {"motif": "全轴可做", "allover": "配色可做；密度/比例粗档；排列轴需生成式重排档"}[carrier]
        if signal["suggest"] == "direction2":
            doable += "；本稿建议走方向二（自由模式）"
        emit(True, [], warnings,
             "把菜单给用户挑：逐轴选项 / --preset / --custom 一句话 / --free 自由生成；"
             "用户没下单前本板块不生成",
             gate=card["gate"], carrier=carrier, suggest=signal["suggest"], signals=signal["signals"],
             menu=MENU, doable=doable, seconds=round(time.time() - t0, 1))
        return {"ok": True, "_ws": str(ws)}

    seed = args.seed if args.seed is not None else (abs(hash(f"{stem}_{int(t0)}")) % 100000)
    palette = palette_hex(orig_rgba)
    free_items, var_items = [], []

    # ---- 方向二：自由模式（显式 --free 才走；提示词原文直达模型）----
    if args.free:
        engine = resolve_engine(args.engine if args.engine != "pixel" else "auto")
        if engine == "manual":
            warnings.append("无生图 key → 手动档：提示词卡已出，ChatGPT 跑完存回再 --collect")
        want_transparent = args.bg == "transparent"
        free_items = run_free(args.free, orig_rgba, orig_white, engine, args.count,
                              want_transparent, out_dir, stem, warnings)
    elif args.collect:
        free_items = collect_free(out_dir, stem)
        if not free_items:
            warnings.append(f"{out_dir.name}/ 里没有 {stem}_free*_manual*.png，无可收产物")

    # ---- 方向一：参数化变体 ----
    if not args.free:
        plan = resolve_plan(args, card, warnings)
        layout_ordered = bool(plan.get("_layout_explicit")) or args.sample == "auto"
        if carrier == "allover" and layout_ordered:
            # 满幅稿排列轴：纯代码不可做 → 生成式重排档（唯一花钱路径，已在 stdout 声明）
            engine = resolve_engine(args.engine if args.engine != "pixel" else "auto")
            if engine == "manual":
                cardf = out_dir / "_rearrange_prompt_card.txt"
                cardf.write_text("【满幅稿排列重排 · 手动档】\n工作区: " + out_dir.parent.name +
                                 "\n提示词：\n" + REARRANGE_PROMPT.format(
                                     lock="", layout=plan["layout"], density=int(plan["density"] * 100)) + "\n",
                                 encoding="utf-8")
                emit(True, [cardf], warnings, "把提示词+原图喂给任意生图工具，结果存回本目录",
                     direction=1, engine="manual", carrier=carrier,
                     seconds=round(time.time() - t0, 1))
                return {"ok": True, "_ws": str(ws)}
            log(f"  [variants] 满幅稿排列轴 → 生成式重排档（{engine}，按张计费）")
            var_items = run_allover_rearrange(plan, orig_rgba, orig_white, engine,
                                              out_dir, stem, warnings)
        elif carrier == "allover":
            # 满幅稿粗档：换色像素级全可做；比例=中心裁切变放粗档；密度轴不可控
            plan["seed"] = int(seed)
            warnings.append("满幅稿粗档：换色可做；比例按中心裁切变放（zoom 估算）；密度/排列轴不可控——"
                            "排列需求走生成式重排档（--layout），要精确变体回板块1 提成 motif 件或 --free")
            var_items = build_allover_coarse(plan, orig_rgba, out_dir, stem)
        else:
            plan["seed"] = int(seed)
            if args.sample == "auto":
                plan["_sample"] = [ax for ax in ("layout", "density", "scale", "color")
                                   if {"layout": args.layout, "density": args.density,
                                       "scale": args.scale, "color": args.color}.get(ax) is None]
            if plan["layout"] != "toss" and args.rotation:
                warnings.append("--rotation 只对 toss 生效，网格布局忽略（要方向性请 --layout toss）")
            if plan.get("tilt") and plan["layout"] == "toss":
                warnings.append("--tilt 对 toss 无意义（本身随机），已忽略")
            if args.count > 1 and plan["layout"] != "toss" and args.sample != "auto" \
                    and not plan.get("_sample"):
                warnings.append("同参数网格复跑结果相同：要系列差异请 --sample auto 或 --layout toss")
            # motif 预处理：无 alpha 试色键（复用板块2 函数副本）
            motif = orig_rgba
            if carrier == "motif" and not has_alpha(motif):
                keyed, done = remove_bg(on_white(motif))
                if done:
                    motif = keyed
                    warnings.append("白底稿已自动色键去底")
            W, H = FRAMES.get(plan["frame"], FRAMES["square"])
            gen_items = build_pixel_variants(plan, motif, min(args.count, MAX_COUNT), W, H)
            for i, it in enumerate(gen_items, 1):
                m = it["measures"]
                if m.get("overlap_warning"):
                    warnings.append(f"v{i}：密集设置导致网格重叠（cell < motif 0.7×）")
                if abs(m["actual_coverage"] - m["target_coverage"]) / max(m["target_coverage"], 1e-6) > COV_BAND:
                    warnings.append(f"v{i}：覆盖率 {m['actual_coverage']:.2f} 偏离目标 {m['target_coverage']:.2f} "
                                    f"超 ±25%（不重叠优先，可降低 --density 或加大 --scale）")
                if it["plan"]["layout"] == "toss" and m["nn_min_dist"] < m["min_dist"] * NN_FLOOR:
                    warnings.append(f"v{i}：toss 最近邻 {m['nn_min_dist']} < 最小距 {m['min_dist']}")
                fname = f"{stem}_{plan_tag(it['plan'], i, it['sampled'])}_v{i}.png"
                dst = out_dir / fname
                it["canvas"].save(dst)
                var_items.append({"mode": "pixel", "label": f"{plan_tag(it['plan'], i, it['sampled'])}",
                                  "file": str(dst), "measures": m,
                                  "plan": {k: v for k, v in it["plan"].items()
                                           if not k.startswith("_")}})
                log(f"  [variants] v{i} {fname} 覆盖率={m['actual_coverage']:.2f} "
                    f"件数={m['n_motifs']} 最近邻={m['nn_min_dist']}")

    # ---- 联络表（方向二产物单独分区标注）----
    for it in var_items + free_items:
        f = it.get("file")
        if f and Path(f).suffix == ".png" and Path(f).exists():
            t = on_white(Image.open(f))
            t.thumbnail((300, 300))
            it["thumb"] = t
        else:
            it["thumb"] = None
    sheet_items = [it for it in var_items if it.get("thumb")]
    sheet_free = [it for it in free_items if it.get("thumb")]
    sheet = None
    if sheet_items or sheet_free:
        sheet = out_dir / "contact_sheet.jpg"
        make_sheet(orig_rgba, sheet_items, sheet_free, sheet)

    meta = {"input": src.name, "seed": int(seed), "carrier": carrier,
            "direction_suggest": signal["suggest"], "palette": palette,
            "items": [{k: v for k, v in it.items() if k not in ("canvas", "thumb")} for it in var_items]
                     + [{k: v for k, v in it.items() if k not in ("thumb",)} for it in free_items],
            "warnings": warnings}
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    record_step(ws, "variants", {
        "directions": sorted({it["mode"] for it in var_items + free_items}),
        "files": [it.get("file") for it in var_items + free_items if it.get("file")],
        "seed": int(seed), "verdicts": {Path(it["file"]).name: it.get("verdict", "checked")
                                        for it in var_items + free_items if it.get("file")}})
    files = [it.get("file") for it in var_items + free_items if it.get("file")]
    if sheet:
        files.append(str(sheet))
    nxt = None
    if free_items and not var_items:
        nxt = "自由模式完成（不做一致性校验）；要参数化变体：先跑菜单模式挑轴"
    elif any(it.get("verdict") == "fail" for it in var_items):
        nxt = "生成式重排跑版：按 meta 里 prompt 换变量重跑，或 --free 手写提示词精调"
    elif var_items:
        nxt = "挑中意的变体后，参数已写进文件名和 meta（可复现）；换系列用 --sample auto --seed N"
    emit(bool(files), files, warnings, nxt,
         direction=2 if (free_items and not var_items) else 1,
         carrier=carrier, suggest=signal["suggest"], seed=int(seed),
         items=[{k: v for k, v in it.items() if k not in ("canvas", "thumb")} for it in var_items]
               + [{k: v for k, v in it.items() if k not in ("thumb",)} for it in free_items],
         seconds=round(time.time() - t0, 1))
    return {"ok": bool(files), "_ws": str(ws)}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(
        description="板块4 图案变体：菜单优先不默认生成；方向一=参数化（像素主轴），方向二=--free 自由生成")
    ap.add_argument("input", help="输入图（01_extracted/motif_XX.png 最佳；满幅稿排列轴走生成式档）")
    ap.add_argument("--layout", choices=("auto",) + LAYOUTS, default=None,
                    help="排列：straight|half-drop|brick|mirror|toss；不指定=菜单模式或默认 half-drop")
    ap.add_argument("--rotation", choices=ROTATIONS, default=None, help="toss 方向性（默认 two-way）")
    ap.add_argument("--tilt", type=float, default=None, help="整体斜排角度（0/45/90）")
    ap.add_argument("--density", default=None, help="sparse|medium|dense|packed 或 0.15-0.95")
    ap.add_argument("--scale", default=None, help="ditsy|medium|large|placement 或 0.15-0.8")
    ap.add_argument("--color", default=None, help="hue<N>|invert|mono|duotone|vintage|bright|gray|none")
    ap.add_argument("--bg", default="white", help="white|transparent|#rrggbb（变体画布底色）")
    ap.add_argument("--frame", choices=tuple(FRAMES), default="square", help="画幅")
    ap.add_argument("--count", type=int, default=1, help="张数（1-12；--sample auto 时为系列长度）")
    ap.add_argument("--seed", type=int, default=None, help="随机种子（默认派生并写 meta；同 seed 同参数=同结果）")
    ap.add_argument("--sample", choices=("auto", "off"), default="off",
                    help="auto=显式点名的系列采样（未指定轴逐张随机，指定轴钉死）")
    ap.add_argument("--preset", choices=tuple(PRESET_PLANS), default=None, help="行业现成方案组合")
    ap.add_argument("--custom", default=None, help="一句话描述 → 1 次 VLM 解析成参数（解析结果记 meta）")
    ap.add_argument("--free", default=None, help="自由模式：提示词+原图直接交生图模型（方向二，按张计费）")
    ap.add_argument("--dry-run", action="store_true", help="只解析/校验参数并输出将生成什么，不真跑")
    ap.add_argument("--collect", action="store_true", help="回接自由模式手动档产物（不质检，如实标注）")
    ap.add_argument("--engine", choices=("auto", "pixel", "gpt", "qwen", "manual"), default="auto",
                    help="auto=生成式档 GPT 优先→qwen→manual；pixel=强制纯代码（满幅排列轴不可用）")
    ap.add_argument("--carrier", choices=("auto", "motif", "allover"), default="auto", help="覆盖判型载体")
    ap.add_argument("--name", default=None, help="工作区名（独立输入默认 var_<文件名>）")
    ap.add_argument("--out", default="pattern_output", help="输出根目录（默认 CWD pattern_output）")
    args = ap.parse_args()
    args.count = max(1, min(args.count, MAX_COUNT))

    src = Path(args.input)
    if not src.exists():
        emit(False, [], [f"输入不存在：{src}"], "确认路径（建议先跑板块1 提取，产物 motif_XX.png 直喂）")
        return 1
    if src.is_dir():
        emit(False, [], ["不支持目录批量：方向二按张计费，方向一系列请用 --sample auto --count N"], None)
        return 1
    out_root = Path(args.out).resolve()
    try:
        if args.dry_run and not args.free:
            warnings = []
            card = None
            plan = resolve_plan(args, card, warnings)
            print(json.dumps({"ok": True, "dry_run": True, "plan": {k: v for k, v in plan.items()
                                                                    if not k.startswith("_")},
                              "warnings": warnings + ["dry-run：参数解析如上，去掉 --dry-run 真跑"],
                              "menu": MENU}, ensure_ascii=False))
            return 0
        r = variants_one(src, args, out_root)
        return 0 if r.get("ok") else 1
    except SystemExit as e:
        print(e.code if isinstance(e.code, str) else json.dumps({"ok": False, "warnings": [str(e.code)]},
                                                                ensure_ascii=False))
        return 1
    except Exception as e:
        emit(False, [], [f"{e.__class__.__name__}: {e}"], "失败进反馈层：换输入/换引擎/手动档 三选一")
        return 1


if __name__ == "__main__":
    sys.exit(main())
