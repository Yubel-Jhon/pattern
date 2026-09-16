# -*- coding: utf-8 -*-
"""冷启动能力探测（板块4 版）：方向一纯代码永远在线；方向二/满幅排列档按 key 定路线。
stdout 一行 JSON，永不硬失败。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from engines import gpt_caps, qwen_caps  # noqa: E402


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    mods = {}
    for m in ("PIL", "numpy", "scipy", "skimage"):
        try:
            __import__(m if m != "PIL" else "PIL.Image")
            mods[m] = True
        except Exception:
            mods[m] = False
    gpt, qwen = gpt_caps(), qwen_caps()
    vlm = qwen["key"]
    gen = "gpt" if gpt["key"] else ("qwen" if qwen["key"] else "manual")
    if gen == "gpt":
        gen_note = (f"方向二/满幅排列档=GPT 档（{'中转' if gpt['relay'] else '官方直连'}，"
                    f"model={gpt['model']}）" + ("" if vlm else "；无 DASHSCOPE_API_KEY → 无 VLM（资格门离线档、"
                                                 "排列重排不做本体核对）"))
    elif gen == "qwen":
        gen_note = "方向二/满幅排列档=qwen 对照档（编辑原图心智）；VLM 资格门/一句话解析可用"
    else:
        gen_note = "无生图 key：方向一纯代码轴全可做（不花钱）；方向二/满幅排列档走手动档（提示词卡）"
    note = (f"方向一（参数化）纯代码在线，零成本；{gen_note}。"
            + ("" if vlm else "无 VLM：资格门降离线档（无法验稿），--custom 一句话解析不可用"))
    caps = {"gpt": gpt, "qwen": qwen, "vlm": "dashscope(qwen-vl)" if vlm else None,
            "gen_engine": gen, "deps": mods,
            "deps_ok": mods.get("PIL") and mods.get("numpy") and mods.get("scipy") and mods.get("skimage")}
    payload = {"ok": caps["deps_ok"], "route": gen, "capabilities": caps, "note": note,
               "next_action": (None if caps["deps_ok"] else
                               "pip install -r requirements.txt（pillow numpy scipy scikit-image）")}
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if caps["deps_ok"] else 3


if __name__ == "__main__":
    sys.exit(main())
