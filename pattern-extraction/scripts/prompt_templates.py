# -*- coding: utf-8 -*-
"""提示词模板库：提取/变体/延展共用，内部模板统一英文（英文出图质量更好，实测结论），
注释双语说明。生图主链（路线①）脚本直接调用；手动模式（路线②）由 agent 取模板给用户。

模板设计原则（01 v3）：
  - 锚定保真：明确 "keep original details/colors unchanged"（防重绘漂移）
  - 锚定输出：白底/透明背景、独立个体、平铺排布（给轻代码拆分创造干净输入）
  - 类型分岔：A 锚保真、B 先裁后生、C 剥叠加物+还原色、D 取标准单元
"""

# 提取模板（板块1）。{extra} 由脚本按需追加（如 --keep-vibe 反向指令）。
EXTRACT_TEMPLATES = {
    "product": (
        "Extract the printed pattern motif(s) from this garment product photo. "
        "Output ONLY the pattern motif(s) themselves, separated from the garment, "
        "arranged on a pure white background with clear spacing between each motif. "
        "Keep the original pattern's shapes, details and colors exactly unchanged. "
        "Do not add shadows, frames, text or any decoration.{extra}"
    ),
    "scene": (
        "Extract the printed pattern motif(s) visible on the garment in this photo. "
        "Ignore the model, background, folds and lighting; reconstruct the flat pattern "
        "as it would look on fabric, and arrange the motif(s) on a pure white background "
        "with clear spacing. Preserve the original pattern's shapes and colors exactly "
        "unchanged. Do not add shadows, frames or text.{extra}"
    ),
    "vibe": (
        "Extract ONLY the printed pattern motif(s) printed on the garment fabric in this "
        "stylized photo. A motif is the graphic artwork printed ON the cloth. "
        "The output canvas must contain ONLY the motif artwork itself, tightly placed on a "
        "pure white background — every other pixel must be pure white: no garment, no plain "
        "fabric, no fabric color, no background, no stickers, badges, patches, text or "
        "watermarks. Remove the color cast caused by ambient lighting and restore the "
        "pattern's true fabric colors, keeping shapes and details exactly unchanged. "
        "Do not add shadows, frames or text.{extra}"
    ),
    "fabric": (
        "This fabric image contains a repeating pattern. Extract exactly ONE complete "
        "standard repeat unit / motif and output it as a SINGLE motif, centered and large, "
        "on a pure white background. Do NOT tile or repeat the pattern, do NOT output "
        "multiple motifs, do NOT reproduce the whole fabric field. Keep the original "
        "shapes, details and colors exactly unchanged. Do not add shadows, frames or text.{extra}"
    ),
}

# 手动模式给用户看的中文说明（同模板的人话版）
EXTRACT_TEMPLATES_ZH = {
    "product": "提取这件衣服产品图上的印花图案本身（不要衣服），白底平铺摆放、间距清晰，"
               "保持图案原始形状细节和颜色完全不变，不要阴影画框文字。",
    "scene": "提取照片中衣服上的印花图案，忽略模特/背景/褶皱/光影，还原成平面布料状态，"
             "白底摆放，形状颜色保持原样，不要阴影画框文字。",
    "vibe": "提取这张风格化照片里真正的印花图案，忽略文字贴纸水印滤镜，去掉环境光造成的偏色还原真实颜色，"
            "白底摆放，形状细节保持原样。",
    "fabric": "这张面料图是重复花纹，提取一个完整的标准花纹单元，白底居中摆放，保持原样。",
}

KEEP_VIBE_EXTRA = (
    " Note: this is a stylized image and the user wants to KEEP the stylized color mood "
    "of the pattern; do not restore original colors."
)

# C 类两段式的第二段：轻代码粗提构图 → 生图只负责重绘干净（不再让模型自己判断什么是印花）
REDRAW_PROMPT = (
    "Image 1 is a stylized photo of a garment; image 2 is a rough extraction of the printed "
    "pattern artwork on that garment (its composition is correct but noisy). Redraw this "
    "pattern artwork cleanly on a pure white background: same shapes, same layout, same "
    "details, using the pattern's true fabric colors as seen in image 1 (remove the ambient "
    "color cast and photo noise). Output ONLY the pattern motif on pure white — no garment, "
    "no fabric, no background, no shadows, no frames, no text.{extra}"
)

VLM_COMPARE_PROMPT = (
    "Image 1 is the original photo, image 2 is an extracted pattern motif. "
    "Compare: is the motif in image 2 the same pattern as printed in image 1? "
    "Answer in strict JSON: {\"same\": true/false, \"color_drift\": \"none|slight|severe\", "
    "\"missing_parts\": true/false, \"note\": \"<one short sentence in Chinese>\"}"
)

VLM_NAME_PROMPT = (
    "Describe this pattern motif for use as a filename: 1-4 lowercase English words "
    "(main subject + color), e.g. 'red_rose_leaf'. Answer with the words only."
)


def build_extract_prompt(itype: str, keep_vibe: bool = False, zh: bool = False) -> str:
    if zh:
        base = EXTRACT_TEMPLATES_ZH.get(itype, EXTRACT_TEMPLATES_ZH["scene"])
    else:
        base = EXTRACT_TEMPLATES.get(itype, EXTRACT_TEMPLATES["scene"])
    extra = KEEP_VIBE_EXTRA if keep_vibe else ""
    return base.format(extra=extra)
