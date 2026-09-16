# -*- coding: utf-8 -*-
"""板块1 实测看板生成器：扫描 pattern_output/ 全部运行记录 → 单文件 HTML（图片内嵌 base64，
可整个文件发给别人/拷走，不依赖目录）。零绝对路径输出。

用法：python scripts/make_dashboard.py [--out 看板.html]
"""
from __future__ import annotations

import argparse
import base64
import html
import io
import json
import sys
import time
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent.parent  # pattern-workshop/
OUT_DIR = ROOT / "pattern_output"

TYPE_ZH = {"product": "A 白底产品图", "scene": "B 场景商拍图", "vibe": "C 网感图",
           "fabric": "D 面料平铺图", "garment": "成衣图(旧)", "reference": "参考图(旧)"}
FID_ZH = {"gen": "生图主链", "pixel": "保真档·离线", "manual": "半自动·手动生图"}
FID_CLS = {"gen": "b-gen", "pixel": "b-pixel", "manual": "b-manual"}


def img_uri(p: Path, max_side: int, quality: int = 82) -> str | None:
    """图片 → 内嵌 data URI（缩到 max_side 控制 HTML 体积）。坏图返回 None。"""
    try:
        im = Image.open(p)
        im.load()
        if im.mode in ("P", "LA"):
            im = im.convert("RGBA")
        if max(im.size) > max_side:
            s = max_side / max(im.size)
            im = im.resize((int(im.width * s), int(im.height * s)), Image.LANCZOS)
        buf = io.BytesIO()
        if im.mode == "RGBA":  # 纹样 PNG 保透明，压体积用 quantize 色板
            im.quantize(colors=256, method=Image.FASTOCTREE).save(buf, format="PNG", optimize=True)
        else:
            im.convert("RGB").save(buf, format="JPEG", quality=quality)
        mime = "image/png" if im.mode == "RGBA" else "image/jpeg"
        return f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def load_runs() -> list:
    runs = []
    for ws in sorted(OUT_DIR.iterdir()):
        meta_p = ws / "meta.json"
        if not meta_p.exists():
            continue
        try:
            meta = json.load(open(meta_p, encoding="utf-8"))
        except Exception:
            continue
        ex = (meta.get("steps") or {}).get("extract")
        if not ex:
            continue  # 空工作区（建了目录没跑成）
        ex_dir = ws / "01_extracted"
        run = {"name": ws.name, "dir": ws, "ex": ex, "ex_dir": ex_dir,
               "at": ex.get("at") or meta.get("updated", ""),
               "schema": "v3" if "triage" in ex else ("v2" if "motif_count" in ex else "v1")}
        runs.append(run)
    return runs


def motifs_of(run: dict, cap: int = 10) -> list:
    """取纹样缩略图（优先白底版），返回 [(uri, label)]。"""
    ex, ex_dir = run["ex"], run["ex_dir"]
    uris = []
    if run["schema"] == "v3":
        names = [m["name"] for m in ex.get("motifs", [])]
    else:
        names = sorted({f.name.replace("_white", "").replace(".png", "")
                        for f in ex_dir.glob("motif_*.png")})[:cap]
        names = [n for n in names if not n.endswith(".png")]
    for nm in names[:cap]:
        p = ex_dir / f"{nm}_white.png"
        if not p.exists():
            p = ex_dir / f"{nm}.png"
        uri = img_uri(p, 200) if p.exists() else None
        if uri:
            uris.append((uri, nm))
    return uris


def card(run: dict) -> str:
    ex = run["ex"]
    itype = TYPE_ZH.get(ex.get("type", "?"), ex.get("type", "未知"))
    fid = ex.get("fidelity", "manual" if ex.get("route") == "manual" else "?")
    fid_zh = FID_ZH.get(fid, fid or "早期版本")
    fid_cls = FID_CLS.get(fid, "b-old")
    t = ex.get("triage") or {}
    motifs = ex.get("motifs") or []
    n_motifs = len(motifs) or ex.get("motif_count", 0)

    # 指标行
    stats = [f"纹样 <b>{n_motifs}</b> 件"]
    if t.get("color_drift"):
        # scene/vibe 的模板要求还原布料色，色差是还原量不是事故
        key = "色彩还原" if (t.get("color_restore") or ex.get("type") in ("scene", "vibe")) else "色差ΔRGB"
        stats.append(f"{key} <b>{t['color_drift']}</b>")
    if t.get("fg_ratio"):
        stats.append(f"前景占比 <b>{t['fg_ratio']}</b>")
    stats_html = " · ".join(stats)

    # VLM 结论 / 警告
    notes = []
    if ex.get("vlm_note"):
        notes.append(f'<div class="note ok">🔍 VLM 校验：{html.escape(str(ex["vlm_note"]))}</div>')
    for w in ex.get("warnings") or []:
        notes.append(f'<div class="note warn">⚠ {html.escape(str(w))}</div>')
    if run["schema"] != "v3":
        notes.append('<div class="note old">早期架构（v2 保真档批量）产物，仅供对比</div>')
    notes_html = "".join(notes)

    # 主图：先对比图，退化到 contact，再退化到标注图
    main_uri = None
    for cand in ("preview_annotated.jpg", "preview_contact.jpg"):
        p = run["ex_dir"] / cand
        if p.exists():
            main_uri = img_uri(p, 760)
            if main_uri:
                break
    main_html = f'<img class="main" src="{main_uri}" alt="preview">' if main_uri else ""

    # 纹样条
    chips = "".join(
        f'<figure><img src="{u}" alt="{html.escape(nm)}"><figcaption>{html.escape(nm)}</figcaption></figure>'
        for u, nm in motifs_of(run))
    more = n_motifs - 10 if n_motifs > 10 else 0
    if more > 0:
        chips += f'<div class="more">+{more} 件</div>'
    chips_html = f'<div class="chips">{chips}</div>' if chips else ""

    return f"""
    <div class="card">
      <div class="head">
        <span class="badge {fid_cls}">{fid_zh}</span>
        <span class="badge t">{itype}</span>
        <span class="name">{html.escape(run['name'])}</span>
        <span class="time">{html.escape(run['at'][:16])}</span>
      </div>
      {main_html}
      {chips_html}
      <div class="stats">{stats_html}</div>
      {notes_html}
    </div>"""


CSS = """
:root{--bg:#0e1014;--card:#171b22;--line:#252b36;--tx:#e8eaf0;--dim:#8b93a5;
--red:#e5484d;--blue:#5b9dff;--purple:#a78bfa;--orange:#f5a524;--green:#46c48a}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--tx);font:14px/1.6 "Segoe UI",system-ui,"Microsoft YaHei",sans-serif;padding:28px 4vw 60px}
h1{font-size:22px;margin-bottom:4px}
h1 em{font-style:normal;color:var(--red)}
.sub{color:var(--dim);font-size:13px;margin-bottom:20px}
.sum{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:26px}
.sum div{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 16px;font-size:13px}
.sum b{font-size:20px;display:block}
h2{font-size:16px;margin:30px 0 12px;padding-left:10px;border-left:3px solid var(--red)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px}
.head{display:flex;align-items:center;gap:8px;margin-bottom:10px;flex-wrap:wrap}
.badge{font-size:11px;padding:2px 9px;border-radius:99px;background:#222938;color:var(--dim)}
.badge.t{background:#1d2433;color:var(--tx)}
.b-gen{background:#3a1518;color:#ff8f94}.b-pixel{background:#12291d;color:#6fe0a8}.b-manual{background:#2b2412;color:#ffd479}.b-old{background:#22262e;color:#7a8296}
.name{font-weight:600;font-size:13px}
.time{margin-left:auto;color:var(--dim);font-size:11px}
img.main{width:100%;border-radius:8px;display:block}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.chips figure{width:86px;margin:0;text-align:center}
.chips img{width:86px;height:86px;object-fit:contain;background:#fff;border-radius:8px;border:1px solid var(--line)}
.chips figcaption{font-size:10px;color:var(--dim);margin-top:3px}
.more{align-self:center;color:var(--dim);font-size:12px}
.stats{margin-top:10px;font-size:12px;color:var(--dim)}
.stats b{color:var(--tx)}
.note{font-size:12px;border-radius:8px;padding:7px 10px;margin-top:8px}
.note.ok{background:#122a1d;color:#8fe6b4}.note.warn{background:#33260f;color:#ffcf7d}.note.old{background:#1e222b;color:#7a8296}
footer{margin-top:40px;color:var(--dim);font-size:12px;line-height:2}
code{background:#1c212b;padding:2px 7px;border-radius:6px;font-size:11px}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "看板.html"))
    ap.add_argument("--all", action="store_true", help="收录全部运行（默认只放每类最新跑通的一次）")
    args = ap.parse_args()

    runs = load_runs()
    gen = [r for r in runs if r["schema"] == "v3" and r["ex"].get("fidelity") == "gen"]
    gen.sort(key=lambda r: r["at"], reverse=True)
    if args.all:
        shown = gen
    else:  # 每个分诊类型只取最新一次跑通的（调试迭代/失败品不进看板）
        shown, seen = [], set()
        for r in gen:
            ty = r["ex"].get("type")
            if ty not in seen:
                seen.add(ty)
                shown.append(r)

    n_all = sum(len(r["ex"].get("motifs") or []) for r in shown)
    types = "".join(f'<div><b>{"✅"}</b>{TYPE_ZH.get(r["ex"].get("type"), r["ex"].get("type"))}</div>'
                    for r in shown)

    body = f"""
<h1>印花模块 · <em>图案提取</em> 实测看板</h1>
<div class="sub">生图模型 qwen-image-3.0-pro（提取/重绘） · 多模态 qwen-vl-max（校验比对） · 生成时间 {time.strftime('%Y-%m-%d %H:%M')}</div>
<div class="sum">
  {types}
  <div><b>{n_all}</b>提取纹样件数</div>
</div>
<h2>生图主链 · 四类分诊实测（每类最新跑通）</h2>
<div class="grid">{''.join(card(r) for r in shown)}</div>
<footer>
复现：<code>python scripts/check_env.py</code>（能力探测）→
<code>python scripts/extract_pattern.py &lt;图&gt; --type auto</code>（全自动）；
无 key 时走 <code>--gen-image</code> 半自动或 <code>--fidelity pixel</code> 保真档。<br>
本页由 <code>scripts/make_dashboard.py</code> 扫描 pattern_output/ 自动生成（默认只收录每类最新跑通运行，--all 看全部），图片已内嵌，可整文件转发。
</footer>"""

    out = Path(args.out)
    out.write_text(f"<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>"
                   f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
                   f"<title>印花模块 · 图案提取实测看板</title><style>{CSS}</style></head>"
                   f"<body>{body}</body></html>", encoding="utf-8")
    print(json.dumps({"ok": True, "out": str(out), "runs": len(runs),
                      "size_kb": round(out.stat().st_size / 1024)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
