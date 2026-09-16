# -*- coding: utf-8 -*-
"""真实图池批量提取：一个进程跑完全池（rembg/FastSAM 会话跨图复用，省加载时间）。"""
from __future__ import annotations

import contextlib
import functools
import io
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_pattern as ep

# FastSAM 模型只加载一次
ep.fastsam_model = functools.lru_cache(maxsize=1)(ep.fastsam_model)

POOL = Path(__file__).resolve().parent.parent.parent.parent / "testset" / "real_pool"
OUT = Path(__file__).resolve().parent.parent.parent / "pattern_output"


def type_of(stem: str) -> str:
    if stem.startswith("fx"):
        return "fabric"
    return "garment"  # wg*/sg*


def run_one(path: Path) -> dict:
    name = f"real_{path.stem}"
    argv = [str(path), "--type", type_of(path.stem), "--name", name, "--out", str(OUT)]
    old = sys.argv
    buf = io.StringIO()
    t0 = time.time()
    code = 0
    try:
        sys.argv = ["extract_pattern.py"] + argv
        with contextlib.redirect_stdout(buf):
            code = ep.main()
    except Exception as e:
        return {"input": path.name, "ok": False, "error": f"{e.__class__.__name__}: {e}",
                "seconds": round(time.time() - t0, 1)}
    finally:
        sys.argv = old
    out = {"input": path.name, "seconds": round(time.time() - t0, 1), "exit": code}
    try:
        line = [l for l in buf.getvalue().strip().splitlines() if l.startswith("{")][-1]
        out.update(json.loads(line))
    except Exception:
        out["ok"] = False
        out["error"] = buf.getvalue()[-300:]
    return out


def main() -> int:
    images = sorted(list(POOL.glob("wg*.jpg")) + list(POOL.glob("wg*.png"))
                    + list(POOL.glob("sg*.jpg")) + list(POOL.glob("fx*.jpg"))
                    + list(POOL.glob("fx*.png")))
    results = []
    for i, p in enumerate(images, 1):
        r = run_one(p)
        n = len(r.get("motifs", [])) if r.get("ok") else 0
        print(f"[{i}/{len(images)}] {p.name} ok={r.get('ok')} motifs={n} {r['seconds']}s", flush=True)
        results.append(r)
    log = POOL / "_run_log.json"
    log.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    ok = sum(1 for r in results if r.get("ok"))
    total_motifs = sum(len(r.get("motifs", [])) for r in results)
    print(f"DONE ok={ok}/{len(results)} motifs_total={total_motifs} log={log}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
