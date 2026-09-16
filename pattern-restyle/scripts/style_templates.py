# -*- coding: utf-8 -*-
"""风格方向话术库：预设 6 方向 + VLM 方向提案适配器，每个方向三套话术（gpt / qwen / zh）+ 质检策略字段。

设计原则（03-风格延展.md v2.1，识别驱动）：
  - **方向不预设**：默认 --styles auto = VLM 看着原稿现场提案（离原稿风格足够远、可商用）；
    本文件的 6 模板**降级为预设档**，只在用户点名（--styles watercolor,... / all）时走
  - 生成端强关联：prompt 里写 CURRENT STYLE（原稿画像，识别产出）→ TARGET STYLE（方向 spec），
    不再用孤立的模板 spec——风格延展是「从这里画到那里」，锚在识别结果上
  - GPT 档：结构化 prompt，无否定清单堆叠（板块1 实测 GPT 不需要）；
    锁词前置（DNA Lock 模式），风格 spec 全部正面描述
  - qwen 档：编辑原图心智，风格指令要对抗它的「保持原样」惯性——先重指令后严条款
  - palette_policy：keep=质检对原色板距离盯得紧；restyle=风格本身会动色板（做旧/换色系），
    只记录不毙图——**03/06 边界：色板漂移是容忍不是手段**，主动换色是板块6 的活
  - drift_tolerance：normal=逐元素核对；loose=该风格允许密度/色彩数量有意减少（minimal）
  - 一次性风格：--styles custom:<描述> 走 make_custom_style()，不入库
"""

# 预设方向（v1 拍板沿用：watercolor/geometric/ethnic/vintage/minimal/handdrawn）。
# ⚠️ 默认不跑这 6 个——它们只是用户点名时的快捷方式；默认走 VLM 现场提案。
STYLES: dict[str, dict] = {
    "watercolor": {
        "label": "水彩",
        "palette_policy": "keep",
        "drift_tolerance": "normal",
        "gpt": ("loose watercolor painting style: soft bleeding wet edges, translucent "
                "layered washes, subtle paper grain, gentle pigment pooling; hand-painted, airy, light"),
        "qwen": ("Repaint this pattern in a loose watercolor style: soft bleeding edges, "
                 "translucent washes, subtle paper grain. Keep the painting airy and light."),
        "zh": "水彩风：软边晕染、透明叠色、纸纹肌理，配色不变。",
    },
    "geometric": {
        "label": "几何",
        "palette_policy": "keep",
        "drift_tolerance": "normal",
        "gpt": ("clean flat geometric style: motifs rebuilt from precise circles, triangles, "
                "arcs and stripes; flat solid color blocks, crisp vector edges, no gradients, no texture"),
        "qwen": ("Repaint this pattern in a flat geometric style: motifs rebuilt from clean "
                 "circles, triangles and stripes, flat solid colors, crisp vector edges."),
        "zh": "几何风：圆/三角/条纹重构图形，平涂色块、边缘锐利，配色不变。",
    },
    "ethnic": {
        "label": "民族",
        "palette_policy": "restyle",
        "drift_tolerance": "normal",
        "gpt": ("traditional folk-art textile style: dense decorative ornament, symmetrical "
                "borders and small repeated details inside each motif, hand-crafted embroidery/"
                "block-print feel, warm earthy folk palette"),
        "qwen": ("Repaint this pattern in a traditional folk-art textile style: dense decorative "
                 "ornament, symmetrical inner borders, hand-crafted block-print feel."),
        "zh": "民族风：纹样内部加对称装饰边、密铺小细节，手工版画质感，允许偏向大地色系。",
    },
    "vintage": {
        "label": "复古",
        "palette_policy": "restyle",
        "drift_tolerance": "normal",
        "gpt": ("vintage 1970s screen-printed textile style: slightly faded and muted colors, "
                "retro grain, soft halftone dots, gentle misregistration of print layers"),
        "qwen": ("Repaint this pattern in a vintage 1970s screen-print style: faded muted "
                 "colors, retro grain, soft halftone dots."),
        "zh": "复古风：70s 丝网印质感，褪色做旧、半调网点，色板自然做旧偏灰。",
    },
    "minimal": {
        "label": "极简",
        "palette_policy": "restyle",
        "drift_tolerance": "loose",
        "gpt": ("minimalist line-art style: motifs reduced to a few confident continuous "
                "lines with generous negative space, at most 1-2 accent colors, nothing decorative"),
        "qwen": ("Repaint this pattern as minimalist line art: motifs reduced to a few "
                 "confident continuous lines, generous negative space, at most 2 colors."),
        "zh": "极简风：连笔线稿化、大量留白、最多 1-2 个强调色（元素可减少，属风格本意）。",
    },
    "handdrawn": {
        "label": "手绘",
        "palette_policy": "keep",
        "drift_tolerance": "normal",
        "gpt": ("playful hand-drawn crayon and ink sketch style: wobbly confident outlines, "
                "visible pencil/crayon strokes, slightly off-register coloring, childlike warmth"),
        "qwen": ("Repaint this pattern in a playful hand-drawn crayon and ink sketch style: "
                 "wobbly outlines, visible strokes, slightly off-register coloring."),
        "zh": "手绘风：蜡笔+钢笔线稿，抖动笔触、套色微偏，配色不变。",
    },
}

ALL_STYLES = list(STYLES.keys())


def get_style(key: str) -> dict | None:
    return STYLES.get(key)


def make_custom_style(desc: str) -> dict:
    """--styles custom:<描述>：一次性风格。描述建议英文（GPT/qwen 出图质量更好），
    中文描述会原样拼进 prompt（VLM/生图模型双语都能吃，但英文更稳）。"""
    return {"label": "custom", "palette_policy": "keep", "drift_tolerance": "normal",
            "gpt": desc, "qwen": f"Repaint this pattern in this style: {desc}.",
            "zh": f"自定义风格：{desc}"}


def make_direction(key: str, label: str, spec: str, palette_policy: str = "keep") -> dict:
    """VLM 方向提案 → 与预设模板同构的 style dict（三个 builder 零改动消费）。
    spec 是 VLM 对着原稿现场写的目标风格描述（英文），palette_impact 由 VLM 判定。"""
    spec = spec.strip() or label
    return {"label": label, "palette_policy": palette_policy, "drift_tolerance": "normal",
            "gpt": spec,
            "qwen": f"Repaint this pattern in this style: {spec}.",
            "zh": f"{label}（VLM 提案）：{spec}"}


# ---------- prompt 组装 ----------

def _elements_clause(elements: list) -> str:
    return "; ".join(elements) if elements else "all motifs exactly as in the reference image"


def build_gpt_prompt(style: dict, elements: list, carrier: str, summary: str = "",
                     remind_elements: list | None = None, stronger: bool = False,
                     portrait: str = "") -> str:
    """GPT 档：锁词（DNA Lock + 元素清单）→ 原稿画像 → 色板策略 → 目标风格 → 输出规格。
    强关联：CURRENT STYLE（识别产出）→ TARGET STYLE，风格延展=从这里画到那里。无否定堆叠。"""
    style_spec = style["gpt"]
    if stronger:
        style_spec = (f"Apply this style strongly and unmistakably — every stroke and every "
                      f"surface must read as this style at first glance: {style_spec}")
    current = (f"CURRENT STYLE (to move away from): {portrait}.\n" if portrait else "")
    palette = ("PALETTE: keep the original color palette unchanged — same hues, same saturation."
               if style["palette_policy"] == "keep" else
               "PALETTE: colors may shift toward the style's natural palette; the color-block "
               "structure must stay recognizable.")
    out = ("OUTPUT: render the artwork alone on a fully transparent background (PNG with alpha), "
           "tight composition, no frame."
           if carrier == "motif" else
           "OUTPUT: full-bleed repeat field covering the entire canvas edge to edge — no border, "
           "no blank corners, no frame.")
    reminder = ""
    if remind_elements:
        reminder = ("\nREMINDER (highest priority): these motifs were missing in a previous "
                    f"attempt and MUST appear exactly as in the reference: {_elements_clause(remind_elements)}.")
    return (
        "CRITICAL HIGHEST PRIORITY: The provided image is a textile print artwork"
        + (f" ({summary})" if summary else "")
        + ". Re-render this EXACT artwork in a new art style. The result must be a sibling of "
        "the reference — same design, different rendering — not a new design.\n"
        f"KEEP EXACTLY: the same composition and layout; every motif present with the same "
        f"shape, position and proportion: {_elements_clause(elements)}.\n"
        f"{current}"
        f"{palette}\n"
        f"TARGET STYLE (the ONLY thing that changes): {style_spec}.\n"
        f"{out} Do not add text, watermark, signature or garment."
        + reminder
    )


def build_qwen_prompt(style: dict, elements: list, carrier: str,
                      remind_elements: list | None = None, stronger: bool = False,
                      portrait: str = "") -> str:
    """qwen 档：编辑原图心智——重风格指令在前，保真条款钉死在后（MATERIAL_REF_LOCK 同款）。
    portrait 让「离开原样」有锚：明确要离开当前风格，而不是笼统换风格。"""
    intensity = "Apply the style strongly and unmistakably. " if stronger else ""
    away = f" Move clearly away from the artwork's current style ({portrait})." if portrait else ""
    keep = (f"Strictly keep the same composition, the same motifs ({_elements_clause(elements)}), "
            "same shapes, positions and proportions; do NOT rearrange, add or remove any element.")
    if remind_elements:
        keep += (f" These motifs were missing before and MUST appear exactly as in the "
                 f"reference: {_elements_clause(remind_elements)}.")
    out = ("Output the artwork alone on a pure white background, complete and centered, "
           "no circular or square frame."
           if carrier == "motif" else
           "Output a full-bleed pattern field: the pattern continues to all four edges, "
           "no blank corners, no circular or square frame.")
    neg = ("No garment, no human body, no background scene, no shadow, no text, no watermark."
           if carrier == "motif" else
           "No garment, no human body, no shadow, no text, no watermark.")
    return (f"{intensity}{style['qwen']}{away} {keep} {out} {neg}")


def zh_card(style: dict, elements: list, carrier: str, summary: str = "",
            portrait: str = "") -> str:
    """手动档中文说明 + 英文 prompt（=GPT 档成品 prompt，ChatGPT 直接用）。"""
    en = build_gpt_prompt(style, elements, carrier, summary, portrait=portrait)
    out_hint = ("输出透明底独立图案" if carrier == "motif" else "输出满幅花布、直达四边")
    src = f"｜原稿风格画像: {portrait}" if portrait else ""
    return f"【{style['label']}】{style['zh']}{src}（{out_hint}）\n----- 英文提示词（整段复制）-----\n{en}"
