# -*- coding: utf-8 -*-
"""真实图池跑批结果页：每图 原图|标注框|标准件一览 + 数据台账。"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent.parent  # 印花模块/
POOL = ROOT / "testset" / "real_pool"
OUT = ROOT / "pattern-workshop" / "pattern_output"
ASSETS = POOL / "_results_assets"

GROUPS = [
    ("wg", "A · 白底成衣图", "电商产品照：考「自动找印花、衣服不进产物」"),
    ("sg", "B · 场景成衣图", "模特实拍：光影褶皱干扰下的花纹提取"),
    ("fx", "C · 补充面料图", "布面花纹提取（对照 f1-f3）"),
]


def make_thumb(src: Path, dst: Path, maxside: int = 420) -> None:
    from PIL import Image
    im = Image.open(src).convert("RGB")
    im.thumbnail((maxside, maxside))
    im.save(dst, quality=82)


def main() -> int:
    log = json.loads((POOL / "_run_log.json").read_text(encoding="utf-8"))
    by_input = {r["input"]: r for r in log}
    if ASSETS.exists():
        shutil.rmtree(ASSETS)
    ASSETS.mkdir(parents=True)

    cards = {}
    for r in log:
        stem = r["input"].rsplit(".", 1)[0]
        d = OUT / f"real_{stem}" / "01_extracted"
        ok = r.get("ok")
        motifs = r.get("motifs", []) if ok else []
        warn = r.get("warnings", [])
        next_a = r.get("next_action")
        # 缩图资产
        src_img = POOL / r["input"]
        thumbs = {}
        for key, f in [("orig", src_img), ("anno", d / "preview_annotated.jpg"),
                       ("contact", d / "preview_contact.jpg")]:
            if f.exists():
                t = ASSETS / f"{stem}_{key}.jpg"
                make_thumb(f, t, 420 if key != "contact" else 520)
                thumbs[key] = f"{stem}_{key}.jpg"
        rows = "".join(
            f"<tr><td>M{i:02d}</td><td>{m['size'][0]}×{m['size'][1]}</td>"
            f"<td>×{m['count']}</td><td>{m['layer']}</td></tr>"
            for i, m in enumerate(motifs[:12], 1))
        warn_html = "".join(f"<div class='warn'>⚠ {w}</div>" for w in warn)
        na_html = f"<div class='na'>➜ {next_a}</div>" if next_a else ""
        table = (f"<table><tr><th>id</th><th>尺寸</th><th>重复</th><th>层</th></tr>{rows}</table>"
                 if rows else "<div class='err'>无花纹产出</div>")
        cards.setdefault(stem[:2], []).append(f"""
<div class="card">
  <div class="head"><b>{stem}</b>
    <span class="pill {'ok' if ok else 'bad'}">{"ok" if ok else "fail"} · {len(motifs)} 个花纹 · {r.get('seconds','?')}s</span></div>
  <div class="imgs">
    <figure><img src="_results_assets/{thumbs['orig']}" loading="lazy"><figcaption>原图</figcaption></figure>
    <figure><img src="_results_assets/{thumbs.get('anno','')}" loading="lazy"><figcaption>标注框</figcaption></figure>
    <figure><img src="_results_assets/{thumbs.get('contact','')}" loading="lazy"><figcaption>标准件（花稿）</figcaption></figure>
  </div>
  {warn_html}{na_html}{table}
</div>""")

    sections = []
    for prefix, title, desc in GROUPS:
        body = "\n".join(cards.get(prefix, [])) or "<p class='err'>本组无图</p>"
        sections.append(f"<h2>{title} <small>{desc}</small></h2>\n{body}")

    ok_n = sum(1 for r in log if r.get("ok"))
    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="UTF-8">
<title>板块1 v2 · 真实图池提取结果</title><style>
:root{{--bg:#111;--card:#1c1c1e;--line:#2c2c2e;--txt:#eee;--dim:#999;--acc:#4ade80;--warnc:#fbbf24}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--txt);font:14px/1.6 "Microsoft YaHei",sans-serif;padding:28px}}
h1{{font-size:21px}} .sub{{color:var(--dim);margin-bottom:10px}}
h2{{font-size:17px;margin:30px 0 12px;border-left:4px solid var(--acc);padding-left:10px}}
h2 small{{color:var(--dim);font-weight:400;font-size:12px;margin-left:8px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:16px}}
.head{{margin-bottom:10px}} .pill{{font-size:12px;padding:2px 8px;border-radius:5px;margin-left:10px}}
.pill.ok{{background:#14532d;color:#bbf7d0}} .pill.bad{{background:#7f1d1d;color:#fecaca}}
.imgs{{display:flex;gap:10px;flex-wrap:wrap}}
.imgs figure{{flex:1;min-width:200px;max-width:340px}}
.imgs img{{width:100%;height:250px;object-fit:contain;background:#0e0e10;border-radius:6px}}
figcaption{{color:var(--dim);font-size:12px;text-align:center}}
.warn{{color:var(--warnc);font-size:12px;margin-top:6px}} .na{{color:#93c5fd;font-size:12px}}
.err{{color:#f87171;font-size:12px}}
table{{border-collapse:collapse;font-size:12px;margin-top:8px}}
td,th{{border:1px solid var(--line);padding:2px 10px;color:var(--dim)}} th{{color:var(--txt)}}
</style></head><body>
<h1>板块1 图案提取 v2 · 真实图池跑批结果</h1>
<p class="sub">{ok_n}/{len(log)} 张成功 · 共 {sum(len(r.get('motifs',[])) for r in log)} 个花纹个体 · 每卡三联：原图 → 标注框 → 标准件花稿</p>
{''.join(sections)}
</body></html>"""
    (POOL / "results.html").write_text(html, encoding="utf-8")
    print("results.html written,", f"{ok_n}/{len(log)} ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
