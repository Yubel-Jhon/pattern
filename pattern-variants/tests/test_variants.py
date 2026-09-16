# -*- coding: utf-8 -*-
"""板块4 离线测试：确定性复跑（同 seed 同参数=像素一致）+ 四类载体合成图跑通纯代码主轴。
不联网、不花钱。python tests/test_variants.py"""
from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import numpy as np
from PIL import Image, ImageDraw

from patternlib import (DENSITY_PRESETS, SCALE_PRESETS, LAYOUTS, count_components,
                        measure_coverage, poisson_points, transform_color)

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))


def synth_motif() -> Image.Image:
    """透明底合成花型（圆花+叶子，非对称，便于验证角度/镜像）。"""
    im = Image.new("RGBA", (200, 200), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((50, 40, 150, 140), fill=(220, 80, 90, 255))
    d.ellipse((75, 65, 125, 115), fill=(250, 220, 120, 255))
    d.polygon([(100, 140), (130, 190), (70, 190)], fill=(80, 160, 90, 255))
    return im


def synth_allover() -> Image.Image:
    """白底满幅稿（小点规律铺满）。"""
    im = Image.new("RGBA", (512, 512), (255, 255, 255, 255))
    d = ImageDraw.Draw(im)
    for y in range(0, 512, 64):
        for x in range(0, 512, 64):
            d.ellipse((x + 10, y + 10, x + 44, y + 44), fill=(60, 110, 180, 255))
    return im


def synth_photo() -> Image.Image:
    """渐变+噪声「照片级」图（无 alpha、满幅、无底）。"""
    rng = np.random.default_rng(0)
    x = np.linspace(0, 255, 512, dtype=np.uint8)
    g = np.tile(x, (512, 1)).astype(np.float64)
    g += rng.normal(0, 15, g.shape)
    rgb = np.stack([g, g * 0.6, 255 - g], -1).clip(0, 255).astype(np.uint8)
    return Image.fromarray(rgb, "RGB").convert("RGBA")


def plan_for(**kw) -> dict:
    base = {"layout": "half-drop", "rotation": "two-way", "tilt": 0.0,
            "density": DENSITY_PRESETS["medium"], "scale": SCALE_PRESETS["medium"],
            "color": "none", "bg": (255, 255, 255), "frame": "square", "seed": 7}
    base.update(kw)
    return base


def test_determinism():
    from variants import render_plan
    motif = synth_motif()
    p1 = plan_for(seed=42)
    c1, m1 = render_plan(p1, 0, motif, 1024, 1024)
    c2, m2 = render_plan(plan_for(seed=42), 0, motif, 1024, 1024)
    check("确定性：同 seed 同参数像素一致", np.array_equal(np.array(c1), np.array(c2)))
    c3, _ = render_plan(plan_for(seed=43, layout="toss"), 0, motif, 1024, 1024)
    c4, _ = render_plan(plan_for(seed=44, layout="toss"), 0, motif, 1024, 1024)
    # 网格布局无随机性，seed 只影响 toss（抖动布点）；断言 toss 对 seed 敏感
    check("确定性：toss 换 seed 结果不同", not np.array_equal(np.array(c3), np.array(c4)))
    check("确定性：网格布局与 seed 无关（纯参数变换）",
          np.array_equal(np.array(c1), np.array(render_plan(plan_for(seed=99), 0, motif, 1024, 1024)[0])))
    check("度量可复现：n_motifs 一致", m1["n_motifs"] == m2["n_motifs"])


def test_layouts():
    from variants import render_plan
    motif = synth_motif()
    for lay in LAYOUTS:
        plan = plan_for(layout=lay, seed=5) if lay != "toss" else plan_for(layout="toss", seed=5)
        canvas, m = render_plan(plan, 0, motif, 1024, 1024)
        cov = m["actual_coverage"]
        check(f"布局 {lay}：渲染出花（覆盖 {cov:.2f}）", 0.05 < cov < 0.95, f"cov={cov}")
        check(f"布局 {lay}：连通域>0", count_components(canvas) > 0)


def test_toss_no_overlap():
    from variants import render_plan
    canvas, m = render_plan(plan_for(layout="toss", rotation="two-way", seed=11), 0,
                            synth_motif(), 1024, 1024)
    check("toss：最近邻≥最小距（不重叠）", m["nn_min_dist"] >= m["min_dist"] * 0.95,
          f"nn={m['nn_min_dist']} md={m['min_dist']}")
    pts = poisson_points(1024, 1024, 80.0, np.random.default_rng(1))
    check("poisson：点数合理（40-500）", 40 < len(pts) <= 500, f"n={len(pts)}")


def test_color_transforms():
    motif = synth_motif()
    base = np.array(transform_color(motif, "none"))
    for c in ("hue180", "mono", "invert", "vintage", "bright", "gray", "duotone"):
        out = np.array(transform_color(motif, c))
        check(f"调色 {c}：结果改变且 alpha 保持", (not np.array_equal(base, out))
              and np.array_equal(base[..., 3] > 8, out[..., 3] > 8))
    # hue 度数越界安全
    _ = transform_color(motif, "hue400")


def test_allover_coarse():
    from variants import build_allover_coarse
    plan = plan_for(color="vintage", scale=SCALE_PRESETS["large"])
    with tempfile.TemporaryDirectory() as td:
        items = build_allover_coarse(plan, synth_allover(), Path(td), "t")
        check("满幅粗档：出 1 张", len(items) == 1 and Path(items[0]["file"]).exists())


def test_carrier_synthetic():
    """三类合成载体跑通方向信号（不联网）。"""
    from variants import direction_signal
    s_allover = direction_signal(synth_allover(), {"texture": "flat"})
    s_photo = direction_signal(synth_photo(), {"texture": "photo"})
    s_motif = direction_signal(synth_motif(), {"texture": "watercolor"})
    check("载体信号：满幅白底稿→可色键（建议方向一）", s_allover["signals"]["colorkey"])
    check("载体信号：照片级→建议方向二", s_photo["suggest"] == "direction2")
    check("载体信号：透明底花型件→alpha_ok", s_motif["signals"]["alpha_ok"])


def test_count_guard():
    from variants import MAX_COUNT, MAX_FREE
    check("成本护栏：count≤12、free≤4", MAX_COUNT <= 12 and MAX_FREE <= 4)


if __name__ == "__main__":
    print("[test_variants] 离线测试（不联网不花钱）")
    for fn in (test_determinism, test_layouts, test_toss_no_overlap, test_color_transforms,
               test_allover_coarse, test_carrier_synthetic, test_count_guard):
        print(f"-- {fn.__name__}")
        fn()
    print(f"\n结果：{len(PASS)} pass / {len(FAIL)} fail")
    sys.exit(1 if FAIL else 0)
