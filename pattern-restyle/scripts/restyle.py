# -*- coding: utf-8 -*-
"""板块3 风格延展主链：开放式识别 → 逐方向重画 → 出口质检 → 回炉≤2 → 联络表。

识别驱动（03-风格延展.md v2.1）：**方向不预设**——1 次 VLM 对着原稿出「元素清单 + 原稿风格
画像 + 延展方向提案」，方向由识别端自己框定（离原稿足够远、可商用）；生成端 prompt 写
CURRENT STYLE（画像）→ TARGET STYLE（方向 spec）强关联，不再用孤立模板 spec。
模板 6 方向降级为预设档（--styles 点名才走）。

宪法（03-风格延展.md v2）：**必须改风格，但不能换内容**。与板块2「永不改设计」相反相成——
所以出口质检不是设计一致性比对，而是拿元素清单逐项核对（元素都在？构图没变？目标风格出现了？
还是原稿风格？）。

三引擎（engines.py）：gpt=终点引擎（透明底/多参考/提示词遵循）｜qwen=对照档（编辑原图心智，
跑版风险高，VLM 出口质检兜底）｜manual=ChatGPT 手动卡（GPT 路线第一交付形态，--collect 回接）。
**兜底与板块2 不同：风格延展没有像素档（像素档换不了画法）**——质检不过 = 如实标注漂移分
交付 + 指路手动档，不假装及格。

两段式花钱：--quality draft（默认，探索档）出方向联络表挑方向 → 选中方向 --quality final
--count 3 出成品。Qwen 档 quality 无效（模型无该参数）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from engines import (EngineError, generate, gpt_caps, pick_gpt_size, pick_qwen_size,
                     qwen_caps, resolve_engine)
from patternlib import (content_crop, edge_clean, emit, ensure_workspace,
                        on_white, record_step, remove_bg, save_raw, trim_frame)
from style_templates import (ALL_STYLES, build_gpt_prompt, build_qwen_prompt, get_style,
                             make_custom_style, make_direction, zh_card)
from triage_card import VLM_MODELS, _parse_json, offline_card, vlm_card

MAX_RETRIES = 2      # 每风格最多回炉 2 次（3 次生成），超次如实标注交付
MAX_COUNT = 4        # 单风格张数上限（成本护栏）
PALETTE_WARN = 80.0  # palette_policy=keep 时色板距离告警阈（只报不毙，VLM 裁决为准）

RECOGNIZE_PROMPT = """You are a textile pattern developer. Examine this print artwork, then plan style extensions for it.
Output strict JSON only, no extra text:
{"elements":["<3-8 short EN noun phrases, one per motif TYPE, with count and color, e.g. 'orange cat with white belly', 'two green maple leaves'>"],
 "summary":"<one short EN sentence: subject + composition + palette mood>",
 "style_portrait":"<one short EN sentence describing the artwork's CURRENT art style: medium, line quality, color treatment, texture, mood>",
 "directions":[
  {"key":"<snake_case_en_id>","label":"<2-4 Chinese chars>","spec":"<one dense EN sentence, concrete and visual>","palette_impact":"keep或restyle"}
 ]}
Rules:
- elements: instances of the same subject count as one element (three roses = one 'roses' entry with count). Ignore plain background color, fabric weave and watermark.
- directions: propose 5-6 target styles for commercial textile prints. Each must be clearly DIFFERENT from style_portrait — push AWAY from the current style; never re-propose it or a close cousin. Vary which dimension changes (medium / line quality / color treatment / era / level of abstraction). spec must be visual and concrete; no artist names.
- palette_impact: keep = the target style preserves the original palette; restyle = the target style naturally brings its own palette (faded, earthy, duotone...)."""

CHECK_PROMPT = """You are a print QA inspector. Image 1 = original artwork, image 2 = restyled version
(a different art style is INTENDED — do not fail it for looking different).
Checklist of motifs that MUST be present: {elements}
Intended target style: {target}
Answer strict JSON only:
{{"elements_present":{{"<each checklist item verbatim>":true或false}},
 "composition_same":true或false,
 "style_applied":true或false,
 "verdict":"pass或fail",
 "note":"<one short Chinese sentence>"}}
Judgement: verdict=fail ONLY if a checklist motif is missing or the layout/composition is
clearly a different design. verdict=pass if all motifs survive even though the rendering
style changed. style_applied=false if image 2 still reads as image 1's ORIGINAL art style,
or reads as a style other than the intended target (weak or wrong style = drift, not fail)."""


# ============ 小工具（enhance.py 同款，自备副本） ============

def kmeans_palette(img: Image.Image, k: int = 6, iters: int = 12) -> tuple:
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
    return centers


def palette_distance(img_a: Image.Image, img_b: Image.Image) -> float:
    ca, cb = kmeans_palette(img_a), kmeans_palette(img_b)
    d = np.sqrt(((ca[:, None, :] - cb[None, :, :]) ** 2).sum(-1))
    return round(float((d.min(1).mean() + d.min(0).mean()) / 2), 1)


def is_black_or_empty(img: Image.Image) -> bool:
    rgba = img.convert("RGBA")
    if np.array(rgba.split()[-1]).max() == 0:
        return True
    rgb = np.asarray(rgba.convert("RGB"), dtype=np.float64)
    return bool(rgb.mean() < 8 and rgb.std() < 8)


def edge_continuity(rgb: Image.Image, band: int = 6) -> dict:
    a = np.asarray(rgb.convert("RGB"), dtype=np.float64)
    hd = float(np.abs(a[:, :band] - a[:, -band:]).mean())
    vd = float(np.abs(a[:band, :] - a[-band:, :]).mean())
    return {"h": round(hd, 1), "v": round(vd, 1),
            "ok": bool(hd <= 30 and vd <= 30)}


def hex_palette(img: Image.Image, k: int = 5) -> list:
    return ["#%02x%02x%02x" % tuple(int(v) for v in c) for c in kmeans_palette(img, k=k)]


def has_alpha(img: Image.Image) -> bool:
    return img.mode == "RGBA" and bool((np.array(img.split()[-1]) < 250).any())


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ============ VLM：开放式识别 / 出口核对 ============

def _norm_direction(d: dict, idx: int) -> tuple | None:
    """VLM 方向提案 → (key, style_dict)；key 清洗防撞。"""
    spec = str(d.get("spec", "")).strip()
    if not spec:
        return None
    raw_key = "".join(ch if (ch.isascii() and ch.isalnum()) else "_" for ch in str(d.get("key", "")))[:24]
    key = raw_key.strip("_").lower() or f"dir{idx + 1}"
    label = str(d.get("label", "")).strip()[:6] or key
    policy = "restyle" if str(d.get("palette_impact", "")).lower().startswith("rest") else "keep"
    return key, make_direction(key, label, spec, policy)


def vlm_recognize(img_white: Image.Image) -> dict:
    """1 次 VLM 开放式识别（全链锚点）：元素清单（prompt 锁词 × 质检 checklist 双用）
    + 原稿风格画像（生成端 CURRENT 锚）+ 方向提案（识别端自己框定延展范围，不预设风格框）。"""
    from dashscope_gen import vlm_chat
    for model in VLM_MODELS:
        try:
            raw = _parse_json(vlm_chat([img_white], RECOGNIZE_PROMPT, timeout_s=120, model=model))
            elements = [str(e).strip()[:80] for e in raw.get("elements", []) if str(e).strip()][:8]
            if not elements:
                continue
            dirs: dict = {}
            for i, d in enumerate(raw.get("directions", []) if isinstance(raw.get("directions"), list) else []):
                if not isinstance(d, dict):
                    continue
                nd = _norm_direction(d, i)
                if nd:
                    key, st = nd
                    while key in dirs:  # key 撞名追加序号
                        key = f"{key}_{len(dirs) + 1}"
                    dirs[key] = st
            return {"elements": elements, "summary": str(raw.get("summary", ""))[:200],
                    "style_portrait": str(raw.get("style_portrait", ""))[:200],
                    "directions": dirs, "_model": model}
        except Exception:
            continue
    return {"elements": [], "summary": "", "style_portrait": "", "directions": {}, "_model": None}


def vlm_check(orig_white: Image.Image, out_white: Image.Image, elements: list,
              target: str | None = None) -> dict:
    """出口质检：逐元素核对 + 构图 + 风格出现（对目标方向判，不对孤立风格判）。
    verdict: pass|fail；style_applied=false→drift（含「还是原稿风格」的同族漂移）。"""
    from dashscope_gen import vlm_chat
    prompt = CHECK_PROMPT.format(
        elements="; ".join(elements) if elements else "(all motifs in image 1)",
        target=target or "(any art style clearly different from image 1)")
    for model in VLM_MODELS:
        try:
            raw = _parse_json(vlm_chat([orig_white, out_white], prompt, timeout_s=120, model=model))
            present = raw.get("elements_present", {})
            if not isinstance(present, dict):
                present = {}
            missing = [k for k, v in present.items() if not v]
            c = {"missing": [str(k)[:60] for k in missing][:8],
                 "composition_same": bool(raw.get("composition_same", True)),
                 "style_applied": bool(raw.get("style_applied", True)),
                 "note": str(raw.get("note", ""))[:150], "_model": model}
            c["verdict"] = "fail" if (c["missing"] or not c["composition_same"]) else "pass"
            return c
        except Exception:
            continue
    return {"missing": [], "composition_same": True, "style_applied": True,
            "verdict": "unknown", "note": "VLM 不可用，未做出口核对（色板距离仍兜底）", "_model": None}


# ============ 产物后处理 ============

def postprocess(img: Image.Image, carrier: str, want_transparent: bool, warnings: list) -> Image.Image:
    """motif：trim_frame → 透明化（模型原生 alpha 优先，否则边缘连通色键）→ 裁紧 → 清边。
    allover：满幅原样保留，不裁不抠。"""
    if carrier != "motif":
        return img.convert("RGBA")
    out = trim_frame(img)
    if not has_alpha(out):
        rgba, done = remove_bg(out)
        if done:
            out = rgba
            warnings.append("非透明输出已自动色键去底")
        elif want_transparent:
            warnings.append("无法去底：输出满幅无底色概念，按满幅交付")
            return out.convert("RGBA")
    out = content_crop(out)
    return edge_clean(out, erode=0, feather=1.0)


# ============ 单风格执行（生成 → 质检 → 回炉） ============

def run_style(style_key: str, style: dict, orig_rgba: Image.Image, orig_white: Image.Image,
              carrier: str, inventory: dict, engine: str, quality: str, count: int,
              transparent: bool, refs: list, out_dir: Path, stem: str) -> dict:
    warnings: list = []
    attempts: list = []
    best, best_rank = None, (-1, -1, -1)  # (verdict分, style_applied, attempt序)
    gpt_size = pick_gpt_size(*orig_rgba.size)
    qwen_size = pick_qwen_size(*orig_rgba.size)
    for v in range(1, count + 1):
        remind, stronger = None, False
        for attempt in range(1, MAX_RETRIES + 2):
            if engine == "gpt":
                prompt = build_gpt_prompt(style, inventory["elements"], carrier,
                                          inventory["summary"], remind, stronger,
                                          portrait=inventory.get("style_portrait", ""))
                gen_imgs = [orig_rgba] + refs
                size = gpt_size
            else:  # qwen：喂白底合成图（RGBA 直转 RGB 会黑底）
                prompt = build_qwen_prompt(style, inventory["elements"], carrier, remind,
                                           stronger, portrait=inventory.get("style_portrait", ""))
                gen_imgs = [orig_white] + [on_white(r) for r in refs]
                size = qwen_size
            try:
                raw_out, gen_meta = generate(gen_imgs, prompt, engine, quality=quality,
                                             transparent=(transparent and engine == "gpt"),
                                             size=size)
            except Exception as e:  # 生成失败：记档，换变量再试
                attempts.append({"variant": v, "attempt": attempt, "error": str(e)[:200]})
                warn = f"{style_key} v{v} 第{attempt}次生成失败：{str(e)[:120]}"
                warnings.append(warn)
                log(f"    [restyle] {warn}")
                if "key" in str(e).lower() or "401" in str(e):
                    break  # 权限类错误重试无意义
                continue
            if is_black_or_empty(raw_out):
                attempts.append({"variant": v, "attempt": attempt, "error": "black/empty guard"})
                warnings.append(f"{style_key} v{v} 第{attempt}次出黑图，重试")
                continue
            out = postprocess(raw_out, carrier, transparent, warnings)
            check = vlm_check(orig_white, on_white(out), inventory["elements"],
                              target=style["gpt"]) \
                if inventory["elements"] else {"verdict": "unknown", "missing": [],
                                               "composition_same": True, "style_applied": True,
                                               "note": "无元素清单（无 VLM），仅色板兜底"}
            pd = palette_distance(orig_rgba, out)
            keep_warn = (style["palette_policy"] == "keep" and pd > PALETTE_WARN)
            if keep_warn:
                warnings.append(f"{style_key} v{v} 色板距离 {pd}（keep 策略偏大，只报不毙）")
            cont = edge_continuity(on_white(out)) if carrier == "allover" else None
            if cont and not cont["ok"]:
                warnings.append(f"{style_key} v{v} 四边连续性超阈（h={cont['h']},v={cont['v']}）→ 无缝化指路板块5")
            attempts.append({"variant": v, "attempt": attempt, "seconds": gen_meta["seconds"],
                             "verdict": check["verdict"], "style_applied": check["style_applied"],
                             "missing": check["missing"], "palette_dist": pd,
                             "note": check["note"], "prompt": prompt})
            rank = (0 if check["verdict"] == "pass" else (1 if check["verdict"] == "unknown" else 2),
                    0 if check["style_applied"] else 1, -attempt)
            if best is None or rank < best_rank:
                best, best_rank = {"img": out, "check": check, "gen_meta": gen_meta,
                                   "attempt": attempt, "variant": v, "palette_dist": pd,
                                   "prompt": prompt}, rank
            # 回炉换变量（每次必须换）：缺元素 → 逐元素点名；风格没出来 → 加风格强度
            if check["verdict"] == "pass":
                break
            if check["verdict"] == "fail" and check["missing"]:
                remind = check["missing"]
            if not check["style_applied"]:
                stronger = True
            log(f"    [restyle] {style_key} v{v} 第{attempt}次 {check['verdict']}"
                f"（缺{len(check['missing'])}项/风格{'未出' if not check['style_applied'] else '已出'}）→ 回炉换变量")
        # count>1 时同风格变体共享回炉结论，不再把失败变量计入 best
    if best is None:
        return {"style": style_key, "ok": False, "attempts": attempts, "warnings": warnings,
                "note": "全部尝试失败（生成层），见 attempts.error"}
    v = best["variant"]
    suffix = f"_v{v}" if count > 1 else ""
    dst = out_dir / f"{stem}_{style_key}{suffix}.png"
    best["img"].save(dst)
    final_verdict = best["check"]["verdict"]
    if final_verdict == "fail":
        warnings.append(f"{style_key}：{len(best['check']['missing'])} 项元素缺失/构图漂移，已如实标注交付"
                        f"——风格延展无像素档兜底，建议走手动档调 prompt 重出")
    return {"style": style_key, "ok": final_verdict != "fail", "verdict": final_verdict,
            "style_applied": best["check"]["style_applied"], "missing": best["check"]["missing"],
            "palette_dist": best["palette_dist"], "attempts": len(attempts), "variant": v,
            "engine": best["gen_meta"]["engine"], "seconds": best["gen_meta"]["seconds"],
            "file": str(dst), "prompt": best["prompt"], "warnings": warnings}


# ============ 手动档：出卡 / 收图 ============

def write_manual_cards(out_dir: Path, stem: str, styles: list, style_map: dict,
                       inventory: dict, carrier: str, gpt_size: str) -> Path:
    portrait = inventory.get("style_portrait", "")
    lines = [f"【板块3 风格延展 · 手动档提示词卡】"
             + (f"｜原稿风格画像: {portrait}" if portrait else ""),
             f"工作区: {out_dir.parent.name}    建议输出尺寸: {gpt_size}（任意生图工具均可）",
             "用法：输入图 + 下面对应方向的整段英文提示词，喂给 ChatGPT/GPT-Image 等任意生图工具；",
             f"每张结果按  <stem>_<风格key>_manual[_序号].png  存进 {out_dir.name}/ ，",
             f"然后跑  python restyle.py <原图路径> --collect  即可回接质检+打包。\n"]
    for key in styles:
        lines.append("=" * 60)
        lines.append(zh_card(style_map[key], inventory["elements"], carrier,
                             inventory["summary"], portrait))
        lines.append("")
    card = out_dir / "_prompt_cards.txt"
    card.write_text("\n".join(lines), encoding="utf-8")
    return card


def collect_manual(orig_rgba: Image.Image, orig_white: Image.Image, out_dir: Path, stem: str,
                   inventory: dict, carrier: str, warnings: list) -> list:
    items = []
    for f in sorted(out_dir.glob(f"{stem}_*_manual*.png")):
        img = Image.open(f)
        img.load()
        out = postprocess(img, carrier, True, warnings)
        if out.size != img.size:
            out.save(f)  # 去底/裁紧后回写，保留手动档产物即可复用
        check = vlm_check(orig_white, on_white(out), inventory["elements"]) \
            if inventory["elements"] else {"verdict": "unknown", "missing": [],
                                           "composition_same": True, "style_applied": True,
                                           "note": "无元素清单，仅色板兜底"}
        style_key = f.stem[len(stem) + 1:].split("_manual")[0]
        pd = palette_distance(orig_rgba, out)
        if check["verdict"] == "fail":
            warnings.append(f"{f.name}：元素缺失/构图漂移（{', '.join(check['missing'][:3])}），如实标注")
        items.append({"style": style_key, "ok": check["verdict"] != "fail", "verdict": check["verdict"],
                      "missing": check["missing"], "palette_dist": pd, "engine": "manual",
                      "file": str(f), "note": check["note"]})
    return items


# ============ 联络表 ============

def make_contact_sheet(orig: Image.Image, items: list, path: Path, cell: int = 300) -> None:
    cells = [("ORIGINAL", on_white(orig))]
    for it in items:
        img = Image.open(it["file"])
        mark = {"pass": "OK", "unknown": "?"}.get(it.get("verdict"), it.get("verdict", "?").upper())
        cells.append((f"{it['style']}:{mark}", on_white(img)))
    cols = min(4, len(cells))
    rows = (len(cells) + cols - 1) // cols
    canvas = Image.new("RGB", (cols * cell, rows * (cell + 22)), (250, 250, 250))
    d = ImageDraw.Draw(canvas)
    for i, (label, img) in enumerate(cells):
        t = img.copy()
        t.thumbnail((cell - 8, cell - 8))
        cx, cy = (i % cols) * cell, (i // cols) * (cell + 22)
        canvas.paste(t, (cx + (cell - t.width) // 2, cy + (cell - t.height) // 2))
        color = (60, 60, 60) if i == 0 else ((40, 140, 60) if ":OK" in label else (200, 60, 40))
        d.text((cx + 4, cy + cell + 4), label, fill=color)
    canvas.save(path, quality=88)


# ============ 主流程 ============

def restyle_one(src: Path, args, out_root: Path) -> dict:
    t0 = time.time()
    warnings: list = []
    stem = src.stem
    # 工作区：01_extracted/02_hd/00_raw 里的输入自动续接原工作区
    ws = (src.parent.parent.resolve() if src.parent.name in ("00_raw", "01_extracted", "02_hd")
          else ensure_workspace(out_root, args.name or f"rs_{stem}"))
    if src.parent.name not in ("00_raw", "01_extracted", "02_hd"):
        save_raw(ws, src)
    out_dir = ws / "03_restyle"
    out_dir.mkdir(parents=True, exist_ok=True)

    orig = Image.open(src)
    orig.load()
    orig_rgba = orig.convert("RGBA")
    orig_white = on_white(orig_rgba)

    # ---- 分诊（资格门沿用板块2 判型卡）----
    carrier = args.carrier
    card = None
    if qwen_caps()["key"]:
        try:
            card = vlm_card(orig_rgba)
        except Exception as e:
            warnings.append(f"VLM 判型失败（{str(e)[:80]}），按输入原样继续")
        if card:
            if card["gate"]["is_pattern"] == "no":
                emit(False, [], [f"资格门拒收：这不是花稿（{card['gate']['reason']}）"],
                     "先走板块1 图案提取（ai_extract.py）剥离印花，再回来做风格延展")
                return {"ok": False, "_ws": str(ws)}
            if card["gate"]["is_pattern"] == "partial":
                warnings.append("输入疑似整衣/场景图而非独立花稿——背景会参与生成，建议先走板块1")
            if carrier == "auto":
                carrier = card["carrier"]
    if carrier == "auto":
        carrier = "motif"
        warnings.append("无 VLM 判型，默认按 motif（独立图案稿）处理；满幅花布请 --carrier allover")

    # ---- 开放式识别（元素清单双用锚点 + 原稿风格画像 + VLM 方向提案）----
    inventory = vlm_recognize(orig_white) if qwen_caps()["key"] else \
        {"elements": [], "summary": "", "style_portrait": "", "directions": {}}
    if not inventory["elements"]:
        warnings.append("无元素清单（无 VLM 或清单为空）：prompt 锚点仅剩原图本身，质检只剩色板兜底")
    palette = hex_palette(orig_rgba)

    # ---- 风格集合（识别驱动：auto=VLM 对着原稿现场提案；点名=预设档）----
    styles_arg = (args.styles or "auto").strip()
    style_map: dict = {}
    if styles_arg == "auto":
        if inventory["directions"]:
            style_map = dict(inventory["directions"])
        else:
            style_map = {s: get_style(s) for s in ALL_STYLES}
            warnings.append("VLM 未给出方向提案（无 VLM 或解析失败）→ 回落预设 6 方向")
    else:
        for s in [x.strip() for x in styles_arg.split(",") if x.strip()]:
            if s.startswith("custom:"):
                style_map[f"custom{len(style_map)}"] = make_custom_style(s[len("custom:"):])
            else:
                st = get_style(s)
                if not st:
                    emit(False, [], [f"未知风格：{s}"],
                         f"可选：auto（VLM 提案，默认）/ all / {', '.join(ALL_STYLES)} / custom:<描述>")
                    return {"ok": False, "_ws": str(ws)}
                style_map[s] = st
        # 预设档点名：提醒与原稿风格画像同族的方向（w1 教训：水彩稿跑水彩方向差异偏微）
        pl = (inventory.get("style_portrait") or "").lower()
        for k in style_map:
            if k in pl:
                warnings.append(f"预设方向 {k} 与原稿风格画像同族（{pl[:60]}…），延展差异可能偏微")
    if not style_map:
        emit(False, [], ["风格集合为空"], "--styles auto（默认，VLM 提案）/ all / 逗号分隔 / custom:<描述>")
        return {"ok": False, "_ws": str(ws)}
    dir_source = ("vlm" if inventory["directions"] else "preset") if styles_arg == "auto" \
        else ("custom" if styles_arg.startswith("custom:") else "preset")

    # ---- 引擎（--collect=只收手动档产物做质检，不再生图）----
    engine = "manual" if args.collect else resolve_engine(args.engine)
    transparent = {"on": True, "off": False}.get(args.transparent, carrier == "motif")

    # ---- 手动档：出卡即交付（--collect 时不走这里，往下收图质检）----
    if engine == "manual" and not args.collect:
        card_path = write_manual_cards(out_dir, stem, list(style_map), style_map,
                                       inventory, carrier, pick_gpt_size(*orig_rgba.size))
        record_step(ws, "restyle", {"mode": "manual_cards", "styles": list(style_map),
                                    "inventory": inventory, "carrier": carrier})
        emit(True, [card_path], warnings,
             f"把卡里的英文提示词+输入图喂给任意生图工具，结果按 <stem>_<风格>_manual.png 存进 {out_dir.name}/，"
             f"再跑 python restyle.py {src} --collect 回接质检打包",
             engine="manual", carrier=carrier, inventory=inventory, palette=palette,
             items=[], seconds=round(time.time() - t0, 1))
        return {"ok": True, "_ws": str(ws)}

    # ---- 生成 + 质检 + 回炉 ----
    items = []
    if engine != "manual":
        for key, st in style_map.items():
            log(f"  [restyle] {key}（{st['label']}，{engine}，{args.quality}）…")
            it = run_style(key, st, orig_rgba, orig_white, carrier, inventory, engine,
                           args.quality, min(args.count, MAX_COUNT), transparent,
                           [Image.open(r) for r in args.ref] if args.ref else [], out_dir, stem)
            items.append(it)

    # ---- collect：手动档结果回接质检 ----
    if args.collect:
        found = sorted(out_dir.glob(f"{stem}_*_manual*.png"))
        if not found:
            warnings.append(f"{out_dir.name}/ 里没有 {stem}_*_manual*.png，无可收产物")
        items += collect_manual(orig_rgba, orig_white, out_dir, stem, inventory, carrier, warnings)

    # ---- 联络表 + 台账 ----
    ok_items = [it for it in items if it.get("file")]
    sheet = out_dir / "contact_sheet.jpg"
    if ok_items:
        make_contact_sheet(orig_rgba, ok_items, sheet)
    (out_dir / "meta.json").write_text(json.dumps(
        {"input": src.name, "engine": engine, "quality": args.quality, "carrier": carrier,
         "transparent": transparent, "directions_source": dir_source,
         "inventory": inventory, "palette": palette,
         "items": [{k: v for k, v in it.items() if k != "warnings"} for it in items]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    record_step(ws, "restyle", {"engine": engine, "quality": args.quality,
                                "styles": [it["style"] for it in items],
                                "files": [it.get("file") for it in ok_items],
                                "inventory_elements": inventory["elements"],
                                "verdicts": {it["style"]: it.get("verdict") for it in items}})
    passed = sum(1 for it in items if it.get("ok"))
    nxt = None
    if any(it.get("verdict") == "fail" for it in items):
        nxt = "有风格跑版（元素缺失/构图漂移）：按 meta 里 attempts 的 prompt 换变量重跑该方向，或走手动档调 prompt"
    elif args.quality == "draft":
        nxt = (f"探索档完成：挑中方向后重跑 --styles <key> --quality final --count 3 出成品；"
               f"联络表 {sheet}")
    files = [it["file"] for it in ok_items] + ([str(sheet)] if ok_items else [])
    emit(passed == len(items) and bool(items), files, warnings, nxt,
         engine=engine, carrier=carrier, inventory=inventory, palette=palette,
         items=[{k: v for k, v in it.items() if k not in ("prompt",)} for it in items],
         seconds=round(time.time() - t0, 1))
    return {"ok": True, "_ws": str(ws)}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="板块3 风格延展：GPT 为主引擎，元素清单双用锚点，出口质检按风格分策略")
    ap.add_argument("input", help="输入图（01_extracted/motif_XX.png 或 02_hd 高清稿最佳）")
    ap.add_argument("--styles", default="auto",
                    help="auto=VLM 对着原稿现场提案方向（默认，识别驱动）；all=6 预设方向；"
                         "或逗号分隔（watercolor,geometric,...）；custom:<描述> 加一次性风格")
    ap.add_argument("--count", type=int, default=1, help="每风格张数（1-4，默认 1）")
    ap.add_argument("--engine", choices=("auto", "gpt", "qwen", "manual"), default="auto",
                    help="auto=GPT key 优先→qwen→manual；qwen=对照档；manual=只出提示词卡")
    ap.add_argument("--quality", choices=("draft", "final", "low", "medium", "high"),
                    default="draft", help="draft=探索档（gpt medium）/ final=成品档（gpt high）；qwen 忽略")
    ap.add_argument("--carrier", choices=("auto", "motif", "allover"), default="auto",
                    help="覆盖判型载体：motif=独立图案稿（透明底）/ allover=满幅花布")
    ap.add_argument("--transparent", choices=("auto", "on", "off"), default="auto",
                    help="motif 默认透明底（gpt 原生 alpha；qwen/手动档自动色键去底）")
    ap.add_argument("--ref", nargs="*", default=None, help="风格参考图（可多张，GPT 档原生支持）")
    ap.add_argument("--collect", action="store_true", help="回接手动档产物：质检+打包，不再生图")
    ap.add_argument("--name", default=None, help="工作区名（独立输入默认 rs_<文件名>）")
    ap.add_argument("--out", default="pattern_output", help="输出根目录（默认 CWD pattern_output）")
    args = ap.parse_args()
    if args.count > MAX_COUNT:
        args.count = MAX_COUNT

    src = Path(args.input)
    if not src.exists():
        emit(False, [], [f"输入不存在：{src}"], "确认路径（建议先跑板块1 提取，产物 motif_XX.png 直喂）")
        return 1
    if src.is_dir():
        emit(False, [], ["不支持目录批量：风格延展按张计费（每风格 1-3 次生成），批量易烧钱",
                         "批量需求请写外层循环，逐张调用并对联络表人工过目"], None)
        return 1
    out_root = Path(args.out).resolve()
    try:
        r = restyle_one(src, args, out_root)
        return 0 if r.get("ok") else 1
    except Exception as e:
        emit(False, [], [f"{e.__class__.__name__}: {e}"], "失败进反馈层：换输入/换引擎/手动档 三选一")
        return 1


if __name__ == "__main__":
    sys.exit(main())
