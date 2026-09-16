# -*- coding: utf-8 -*-
"""判型卡（triage card）：调度层公共件，板块 2-6 消费同一张卡（02-高清修复.md §2 契约）。

一次 VLM 调用同时产出：
  gate    资格门——是不是花稿（yes/no/partial/uncertain）；不过门不发生图调用
  carrier 载体 motif（独立图案稿）| allover（满幅回样）
  texture 质地 flat（平色插画）| watercolor（水彩晕染）| photo（照片肌理）
  defects 缺陷清单 ⊆ {size, clarity, detail, edge, color}
  focus   主体区域 [x1,y1,x2,y2]，0-1000 归一化

与板块1 的 patternlib.triage()（定量体检：清晰度/均匀底/占比）互补：那是像素信号，
本卡是语义判型。一次判型全链路一致，meta 记卡保 lineage。

离线兜底（§2）：无 VLM 时像素信号判不了「是不是花稿」（照片肌理花稿本来就像照片），
offline_card() 诚实给 uncertain + 警告，由调用方（用户/agent）确认后继续，不假装会筛。

注意：喂 VLM 的图一律先合成白底——RGBA 直接 convert("RGB") 会把透明区变黑，毁判型。
"""
from __future__ import annotations

import json
import numpy as np
from PIL import Image

from patternlib import on_white

VLM_MODELS = ("qwen3-vl-plus", "qwen-vl-max")  # 账号未开通的别名会 403，逐个回退（板块1 同款）

GATE_VALUES = ("yes", "no", "partial", "uncertain")
CARRIER_VALUES = ("motif", "allover")
TEXTURE_VALUES = ("flat", "watercolor", "photo")
DEFECTS = ("size", "clarity", "detail", "edge", "color")

CARD_PROMPT = """你是印花开发流程的花稿质检员。判断这张图，只输出 JSON，不要多余文字：
{"gate":{"is_pattern":"yes|no|partial|uncertain","confidence":0到1的小数,"reason":"一句话中文依据","action":"pass|route_extract|ask_user"},
 "carrier":"motif|allover","texture":"flat|watercolor|photo",
 "defects":["从 size|clarity|detail|edge|color 中选确实存在的，可多选"],
 "focus":[左,上,右,下]}
判据：
- gate.is_pattern：yes=这是可用于印花开发的花稿（透明/白底上的独立图案稿，或整幅花布满幅回样）；
  no=与花稿完全无关（人物照、风景、文档、截图等）；partial=画面里有印花但主体不是花稿（商拍图、
  整件服装照片、带印花的产品照——应先走图案提取板块）；uncertain=拿不准（纯文字、Logo、极简抽象）。
- gate.action：yes→pass；no→按 reason 拒收；partial→route_extract；uncertain→ask_user。
- carrier：motif=独立图案稿（底色干净、花型可从底上分离，含透明背景）；allover=满幅回样（花纹铺满画面，无底色概念）。
- texture：flat=平色插画（色块均匀、边缘锐利、线条清晰）；watercolor=水彩晕染（软边、颜色渐变晕开、纸纹）；
  photo=照片/织物肌理（真实颗粒、织纹、摄影光影）。
- defects 可多选：size=分辨率不足；clarity=模糊、JPEG色块、压缩噪点；detail=纹理笔触细节丢失（画面发"平"）；
  edge=毛边、锯齿、白边（独立稿才可能有）；color=偏色、褪色、饱和度损失。没有缺陷给空数组。
- focus：主体区域 [左,上,右,下]，0-1000 归一化坐标；满幅或无法确定给 [0,0,1000,1000]。"""


def _parse_json(t: str) -> dict:
    """剥 markdown 围栏后取最外层 JSON（ai_extract 同款）。"""
    t = t.strip()
    if "```" in t:
        t = t.split("```")[1]
        if t.startswith("json"):
            t = t[4:]
    return json.loads(t[t.find("{"): t.rfind("}") + 1])


def normalize_card(raw: dict) -> dict:
    """枚举纠偏 + 缺省填充：VLM 偶尔溢出枚举值，卡必须总是结构完整。"""
    gate = raw.get("gate", {}) if isinstance(raw.get("gate"), dict) else {}
    is_pat = str(gate.get("is_pattern", "uncertain")).lower()
    if is_pat not in GATE_VALUES:
        is_pat = "uncertain"
    action = str(gate.get("action", "")).lower()
    if action not in ("pass", "route_extract", "ask_user"):
        action = {"yes": "pass", "no": "ask_user", "partial": "route_extract"}.get(is_pat, "ask_user")
    carrier = str(raw.get("carrier", "")).lower()
    if carrier not in CARRIER_VALUES:
        carrier = "motif"
    texture = str(raw.get("texture", "")).lower()
    if texture not in TEXTURE_VALUES:
        texture = "flat"
    defects = [d for d in raw.get("defects", []) if d in DEFECTS]
    focus = raw.get("focus", [0, 0, 1000, 1000])
    try:
        focus = [max(0, min(1000, int(v))) for v in focus][:4]
        if len(focus) != 4 or focus[2] <= focus[0] or focus[3] <= focus[1]:
            focus = [0, 0, 1000, 1000]
    except Exception:
        focus = [0, 0, 1000, 1000]
    try:
        conf = max(0.0, min(1.0, float(gate.get("confidence", 0.5))))
    except Exception:
        conf = 0.5
    return {"gate": {"is_pattern": is_pat, "confidence": round(conf, 2),
                     "reason": str(gate.get("reason", ""))[:200], "action": action},
            "carrier": carrier, "texture": texture, "defects": defects, "focus": focus}


def vlm_card(img: Image.Image, timeout_s: int = 120) -> dict:
    """语义判型：1 次 VLM 调用出卡。img 先合成白底再喂（透明直转 RGB 会变黑底）。
    全部模型失败则抛异常（调用方走 X 异常回路 / 离线降级）。"""
    from dashscope_gen import vlm_chat
    rgb = img.convert("RGBA")
    if rgb.mode == "RGBA" and (np.array(rgb.split()[-1]) < 250).any():
        rgb = on_white(rgb)
    last: Exception = RuntimeError("no model attempted")
    for model in VLM_MODELS:
        try:
            text = vlm_chat([rgb], CARD_PROMPT, timeout_s=timeout_s, model=model)
            return normalize_card(_parse_json(text)) | {"_model": model}
        except Exception as e:
            last = e
    raise last


def offline_card(img: Image.Image) -> dict:
    """离线降级卡：像素信号只能推 carrier/texture/部分 defects，身份判不了。
    gate 诚实给 uncertain + action=pass（调用方必须附带警告，由人确认后继续）。"""
    rgba = img.convert("RGBA")
    a = np.array(rgba.split()[-1])
    has_alpha = bool((a < 250).any()) and rgba.mode == "RGBA"
    warnings = []
    # carrier：透明稿内容占比小 → motif；铺满 → allover
    carrier = "motif"
    if has_alpha:
        cover = float((a > 32).mean())
        if cover > 0.85:
            carrier = "allover"
    else:
        carrier = "allover"  # 无透明通道按满幅处理（保守：不去底）
    # texture：alpha 软边占比高 → watercolor；灰度纹理起伏大 → photo；否则 flat
    texture = "flat"
    if has_alpha:
        mid = float(((a > 40) & (a < 215)).mean())
        if mid > 0.04:
            texture = "watercolor"
            warnings.append(f"离线档：alpha 软边占比 {mid:.2f} → 按 watercolor 处理")
    if texture == "flat":
        from skimage.filters import laplace
        g = np.asarray(on_white(rgba).convert("L"), dtype=float) / 255.0
        clarity = float(laplace(g).var())
        if 0.0003 <= clarity < 0.004 and carrier == "allover":
            texture = "photo"  # 满幅且有细腻纹理起伏 → 倾向照片肌理
            warnings.append("离线档：纹理起伏中等 → 按 photo 处理")
    # defects：只列像素上可测的
    defects = []
    if min(rgba.size) < 1024:
        defects.append("size")
    rgbf = np.asarray(rgba.convert("RGB"), dtype=float)
    from skimage.filters import laplace
    clarity_v = float(laplace(rgbf.mean(-1) / 255.0).var())
    if clarity_v < 0.0003:
        defects.append("clarity")
    means = rgbf.reshape(-1, 3).mean(0)
    ratios = means / max(means.mean(), 1)
    if ratios.max() > 1.14 or ratios.min() < 0.87:
        defects.append("color")
    return {"gate": {"is_pattern": "uncertain", "confidence": 0.0,
                     "reason": "离线档：无 VLM，无法验稿，请确认输入是花稿", "action": "pass"},
            "carrier": carrier, "texture": texture, "defects": defects,
            "focus": [0, 0, 1000, 1000], "_model": "offline", "_warnings": warnings}
