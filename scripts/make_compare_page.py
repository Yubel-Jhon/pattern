# -*- coding: utf-8 -*-
"""算法档 vs AI档 对比页：每图 原图|算法花稿|AI定位|AI花稿 并排，供拍板。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent.parent.parent  # 印花模块/
POOL = ROOT / "testset" / "real_pool"
OUT = ROOT / "pattern-workshop" / "pattern_output"
ASSETS = POOL / "_cmp_assets"

GROUPS = [
    ("wg", "A · 白底成衣图", "电商产品照"),
    ("sg", "B · 场景成衣图", "模特实拍：光影褶皱干扰"),
    ("fx", "C · 面料图", "布面花纹"),
]


def thumb(src: Path, dst: Path, maxside: int = 420) -> bool:
    if not src.exists():
        return False
    im = Image.open(src).convert("RGB")
    im.thumbnail((maxside, maxside))
    im.save(dst, quality=84)
    return True


def ai_contact_with_names(d: Path, motifs: list, stem: str) -> bool:
    """AI 档花稿带名字重拼（默认 contact 的中文标签在 PIL 默认字体下是乱码方块）。"""
    pngs = sorted(d.glob("motif_*.png"))
    if not pngs:
        return False
    cell, cols = 200, min(6, len(pngs))
    rows = (len(pngs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * cell, rows * (cell + 26)), (24, 24, 26))
    dr = ImageDraw.Draw(sheet)
    try:
        from PIL import ImageFont
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 13)
    except Exception:
        font = None
    for i, m in enumerate(motifs[: len(pngs)]):
        f = d / f"motif_{m['id'][-2:]}.png"
        if not f.exists():
            continue
        im = Image.open(f).convert("RGB")
        im.thumbnail((cell - 10, cell - 10))
        x, y = (i % cols) * cell, (i // cols) * (cell + 26)
        sheet.paste(im, (x + (cell - im.width) // 2, y + (cell - im.height) // 2))
        name = (m.get("name") or m.get("pattern_type") or "")[:16]
        dr.text((x + 5, y + cell + 4), f"{m['id']} {name}", fill=(235, 235, 235), font=font)
    sheet.save(ASSETS / f"{stem}_ai.jpg", quality=84)
    return True


def main() -> int:
    alg = json.loads((POOL / "_run_log.json").read_text(encoding="utf-8"))
    ai_path = POOL / "_run_log_ai.json"
    ai = {r["input"]: r for r in json.loads(ai_path.read_text(encoding="utf-8"))} if ai_path.exists() else {}
    if ASSETS.exists():
        shutil.rmtree(ASSETS)
    ASSETS.mkdir(parents=True)

    cards = {}
    for r in alg:
        stem = r["input"].rsplit(".", 1)[0]
        d_alg = OUT / f"real_{stem}" / "01_extracted"
        d_ai = OUT / f"ai_{stem}" / "01_extracted"
        a = ai.get(r["input"], {})
        a_ok = a.get("ok")
        a_motifs = a.get("motifs", []) if a_ok else []
        thumbs = {}
        for key, f, ms in [
            ("orig", POOL / r["input"], 380),
            ("alg", d_alg / "preview_contact.jpg", 520),
            ("anno", d_ai / "preview_annotated.jpg", 380),
        ]:
            if thumb(f, ASSETS / f"{stem}_{key}.jpg", ms):
                thumbs[key] = f"{stem}_{key}.jpg"
        has_ai = ai_contact_with_names(d_ai, a_motifs, stem)
        if not has_ai and (d_ai / "preview_contact.jpg").exists():
            thumb(d_ai / "preview_contact.jpg", ASSETS / f"{stem}_ai.jpg", 560)
            has_ai = True

        n_alg = len(r.get("motifs", [])) if r.get("ok") else 0
        stat = (f"算法 {n_alg} 个 · {r.get('seconds','?')}s ｜ "
                f"AI {len(a_motifs)} 个 · {a.get('seconds','—')}s")
        err_ai = "" if a_ok or not a else f"<div class='err'>AI: {a.get('error','')[:80]}</div>"
        warn = "".join(f"<div class='warn'>⚠ {w}</div>" for w in a.get("warnings", []))

        def fig(key, cap):
            if key not in thumbs and key != "ai":
                return ""
            src = f"_cmp_assets/{thumbs.get(key, stem + '_ai.jpg')}" if (key in thumbs or has_ai) else ""
            if not src:
                return f"<figure><div class='missing'>—</div><figcaption>{cap}</figcaption></figure>"
            return f"<figure><img src='{src}' loading='lazy'><figcaption>{cap}</figcaption></figure>"

        cards.setdefault(stem[:2], []).append(f"""
<div class="card" id="{stem}">
  <div class="head"><b>{stem}</b><span class="pill">{stat}</span></div>
  <div class="imgs">
    {fig('orig', '原图')}{fig('alg', '算法档 · 花稿')}
    {fig('anno', 'AI档 · 定位框')}{fig('ai', 'AI档 · 花稿（生图重绘）')}
  </div>
  {err_ai}{warn}
</div>""")

    sections = []
    for prefix, title, desc in GROUPS:
        body = "\n".join(cards.get(prefix, [])) or "<p class='err'>本组无图</p>"
        sections.append(f"<h2>{title} <small>{desc}</small></h2>\n{body}")

    n_ai_ok = sum(1 for r in ai.values() if r.get("ok"))
    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8">
<title>板块1 · 算法档 vs AI档 对比</title><style>
:root{{--bg:#111;--card:#1c1c1e;--line:#2c2c2e;--txt:#eee;--dim:#999;--acc:#4ade80}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--txt);font:14px/1.6 "Microsoft YaHei",sans-serif;padding:28px}}
h1{{font-size:21px}} .sub{{color:var(--dim);margin-bottom:10px}}
h2{{font-size:17px;margin:30px 0 12px;border-left:4px solid var(--acc);padding-left:10px}}
h2 small{{color:var(--dim);font-weight:400;font-size:12px;margin-left:8px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:16px}}
.head{{margin-bottom:10px}} .pill{{font-size:12px;padding:2px 8px;border-radius:5px;margin-left:10px;background:#26263a;color:#c7d2fe}}
.imgs{{display:flex;gap:10px;flex-wrap:wrap}}
.imgs figure{{flex:1;min-width:190px;max-width:360px}}
.imgs img{{width:100%;height:240px;object-fit:contain;background:#0e0e10;border-radius:6px}}
.missing{{height:240px;display:flex;align-items:center;justify-content:center;color:#555;background:#0e0e10;border-radius:6px}}
figcaption{{color:var(--dim);font-size:12px;text-align:center}}
.warn{{color:#fbbf24;font-size:12px;margin-top:6px}} .err{{color:#f87171;font-size:12px}}
</style></head><body>
<h1>板块1 图案提取 · 算法档 vs AI档（VLM定位+生图重绘）</h1>
<p class="sub">共 {len(alg)} 张 · AI 档完成 {n_ai_ok}/{len(ai) if ai else len(alg)} · 重点看「AI档·花稿」是否忠实原印花、可直接当标准件</p>
{''.join(sections)}
</body></html>"""
    (POOL / "compare.html").write_text(html, encoding="utf-8")
    print("compare.html written,", f"ai_ok={n_ai_ok}/{len(ai) if ai else 0}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
