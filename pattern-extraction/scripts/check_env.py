# -*- coding: utf-8 -*-
"""能力探测：skill 三路线走哪条 + 冷启动接入指引。

用法：python check_env.py   （stdout 一行 JSON，退出码 0=全自动 2=降级可用 3=需补依赖）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from patternlib import capabilities, capabilities_next_action, emit


def main() -> int:
    caps = capabilities()
    na = capabilities_next_action(caps)
    if caps["gen_api"]:
        emit(True, [], [], na, capabilities=caps, route="gen")
        return 0
    if caps["pixel_tier"]["ready"]:
        emit(True, [], ["无生图 API，保真档（离线）可用"], na, capabilities=caps, route="pixel")
        return 2
    emit(False, [], ["无生图 API 且保真档依赖缺失"], na, capabilities=caps, route=None)
    return 3


if __name__ == "__main__":
    sys.exit(main())
