# -*- coding: utf-8 -*-
"""冷启动能力探测：GPT 档 / qwen 对照档 / 手动档 三路线判定。stdout 一行 JSON，永不硬失败。"""
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
    if gpt["key"]:
        route = "gpt"
        note = (f"GPT 档就绪（{'中转' if gpt['relay'] else '官方直连（需系统代理可用）'}，"
                f"model={gpt['model']}）。VLM 眼睛仍走 qwen-vl。" if vlm
                else "GPT key 就绪但无 DASHSCOPE_API_KEY：生图走 GPT，但分诊/元素清单/质检无 VLM，降级为仅色板兜底")
    elif qwen["key"]:
        route = "qwen"
        note = "无 OPENAI_API_KEY → qwen 对照档（编辑原图心智，跑版风险高，VLM 出口质检兜底）。建议配 GPT key 或走手动档"
    else:
        route = "manual"
        note = "无任何生图 key → 手动档（提示词卡 → ChatGPT 人工跑 → --collect 回接），效果不受限"
    caps = {"gpt": gpt, "qwen": qwen, "vlm": "dashscope(qwen-vl)" if vlm else None,
            "deps": mods, "deps_ok": mods.get("PIL") and mods.get("numpy")}
    payload = {"ok": caps["deps_ok"], "route": route, "capabilities": caps,
               "note": note,
               "next_action": (None if caps["deps_ok"] else
                               "pip install -r requirements.txt（pillow numpy scipy scikit-image）")}
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if caps["deps_ok"] else 3


if __name__ == "__main__":
    sys.exit(main())
