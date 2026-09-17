# -*- coding: utf-8 -*-
"""板块5 无缝循环 tests：确定性 / wrap 正确性 / 环面间距 / 五布局 / 周期检测 / blend 周期性 / 三路 CLI 冒烟。
全部用合成图，零 API、零 key（离线可跑）。运行：python -m unittest tests.test_seamless -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import patternlib as pl  # noqa: E402
import seamless as sm  # noqa: E402


# ---------- 合成图 ----------

def make_motif(size: int = 120) -> Image.Image:
    """合成花型件：红花朵 + 绿叶 + 茎，方形 bbox，近圆（旋转外扩可控）。"""
    s = size
    m = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(m)
    cx, cy = s // 2, s // 2
    d.line([(cx, cy + s * 0.1), (cx, s - 6)], fill=(60, 130, 60, 255), width=max(4, s // 20))
    for ang_deg in range(0, 360, 72):
        a = np.deg2rad(ang_deg)
        px, py = cx + (s * 0.30) * np.cos(a), cy + (s * 0.30) * np.sin(a)
        r = s * 0.16
        d.ellipse([px - r, py - r, px + r, py + r], fill=(210, 60, 70, 255))
    d.ellipse([cx - s * 0.12, cy - s * 0.12, cx + s * 0.12, cy + s * 0.12], fill=(240, 210, 90, 255))
    d.ellipse([cx - s * 0.45, cy - s * 0.05, cx - s * 0.15, cy + s * 0.15], fill=(80, 160, 80, 255))
    return m


def make_periodic(side: int = 128, reps: int = 4) -> Image.Image:
    """严格周期满幅稿：base 单元 blob 九宫偏移绘制（跨边环绕延续）→ 平铺 reps²，加轻微噪声。"""
    rng = np.random.default_rng(3)
    base = Image.new("RGB", (side, side), (245, 240, 230))
    d = ImageDraw.Draw(base)
    for _ in range(9):
        x, y = rng.integers(0, side, 2)
        r = int(rng.integers(side // 10, side // 4))
        col = tuple(int(v) for v in rng.integers(40, 220, 3))
        for ox in (-side, 0, side):   # 九宫偏移：跨边 blob 环绕延续 → base 才是真周期单元
            for oy in (-side, 0, side):
                d.ellipse([x + ox - r, y + oy - r, x + ox + r, y + oy + r], fill=col)
    big = Image.new("RGB", (side * reps, side * reps))
    for i in range(reps):
        for j in range(reps):
            big.paste(base, (i * side, j * side))
    arr = np.asarray(big, dtype=np.float64)
    arr += rng.normal(0, 2.0, arr.shape)
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")


def make_allover_np(side: int = 512) -> Image.Image:
    """满幅不重复稿：弱渐变底 + 全幅随机 blob + 噪声（无平铺结构）→ 路C 的标准输入。
    渐变故意弱：线性渐变在低 lag 有自相关拖尾，会伪装出假周期（find_period 的假想敌之一）。"""
    rng = np.random.default_rng(21)
    x = np.linspace(0, 1, side)[None, :, None]
    y = np.linspace(0, 1, side)[:, None, None]
    arr = 150 + 55 * (x * 0.9 + y * 0.6) * np.array([1.0, 0.85, 0.7])   # 弱斜向渐变底
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")
    d = ImageDraw.Draw(img)
    for _ in range(20):
        cx, cy = rng.integers(0, side, 2)
        r = int(rng.integers(side // 7, side // 3))
        col = tuple(int(v) for v in rng.integers(30, 230, 3))
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
    a = np.asarray(img, dtype=np.float64) + rng.normal(0, 4.0, (side, side, 3))
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGB")


def make_seamed(side: int = 512) -> Image.Image:
    """带硬缝稿：周期稿右/下边缘贴上错位条 → wrap 处硬跳变（blend 的修复对象）。"""
    src = make_periodic(128, 4).crop((0, 0, side, side))
    out = src.copy()
    strip = 48
    out.paste(src.crop((97, 0, 97 + strip, side)), (side - strip, 0))   # 右缘错位条
    out.paste(src.crop((0, 61, side, 61 + strip)), (0, side - strip))   # 下缘错位条
    return out


def make_noisy(side: int = 512) -> Image.Image:
    """无周期满幅稿：平滑随机场（路C 的另一种输入）。"""
    rng = np.random.default_rng(11)
    arr = rng.normal(128, 40, (side, side, 3))
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB").filter(
        ImageFilter.GaussianBlur(9))
    return img


# ---------- patternlib 环面 / 修缝单元 ----------

class TestTorus(unittest.TestCase):
    def test_torus_d(self):
        self.assertEqual(pl._torus_d(3, 10), 3)
        self.assertEqual(pl._torus_d(-8, 10), 2)
        self.assertEqual(pl._torus_d(0, 10), 0)

    def test_poisson_torus_no_overlap_across_seam(self):
        """环面间距：把点阵平铺 3×3 后最近邻间距仍 ≥ min_dist（接缝两侧不挤叠）。"""
        rng = np.random.default_rng(5)
        for md in (60.0, 123.0):
            pts = pl.poisson_points_torus(512, 384, md, rng)
            self.assertGreaterEqual(len(pts), 4)
            self.assertGreaterEqual(pl.nn_min_dist_torus(pts, 512, 384), md - 1e-6)

    def test_poisson_deterministic(self):
        a = pl.poisson_points_torus(512, 512, 80, np.random.default_rng(42))
        b = pl.poisson_points_torus(512, 512, 80, np.random.default_rng(42))
        np.testing.assert_array_equal(a, b)


class TestWrapRender(unittest.TestCase):
    def test_tile3x3_equals_direct_render(self):
        """构造性无缝铁证：tile 平铺 3×3 后，中心窗口逐像素等于原 tile。"""
        motif = make_motif(80)
        for layout in ("straight", "half-drop", "brick", "mirror"):
            with self.subTest(layout=layout):
                W = H = 256
                pts, flips, _ = sm.route_A_points(layout, W, H, motif, "medium", np.random.default_rng(7))
                tile = pl.render_repeat_wrap(motif, W, H, pts, [0.0] * len(pts),
                                             np.ones(len(pts)), flips)
                big = Image.new("RGBA", (W * 3, H * 3), (0, 0, 0, 0))
                for dx in (0, W, 2 * W):
                    for dy in (0, H, 2 * H):
                        big.alpha_composite(tile, (dx, dy))
                np.testing.assert_array_equal(np.asarray(big.crop((W, H, 2 * W, 2 * H))),
                                              np.asarray(tile))

    def test_toss_tile_wrap_continuous(self):
        """toss（带角度/缩放抖动）：边缘跳变比 ≈1（wrap 延续成立）。"""
        motif = make_motif(70)
        W = H = 256
        pts, _, _ = sm.route_A_points("toss", W, H, motif, "medium", np.random.default_rng(9))
        angles = sm.angle_cycle("four-way", len(pts), np.random.default_rng(9), jitter=8.0)
        scales = np.full(len(pts), 0.9)
        tile = pl.render_repeat_wrap(motif, W, H, pts, angles, scales)
        self.assertLessEqual(pl.edge_jump_ratio(tile), 1.2)

    def test_layouts_all_pass_jump(self):
        motif = make_motif(60)
        for layout in pl.LAYOUTS:
            with self.subTest(layout=layout):
                pts, flips, _ = sm.route_A_points(layout, 256, 256, motif, "medium",
                                                  np.random.default_rng(13))
                angles = sm.angle_cycle("two-way", len(pts), np.random.default_rng(13))
                tile = pl.render_repeat_wrap(motif, 256, 256, pts, angles, np.ones(len(pts)), flips)
                self.assertLessEqual(pl.edge_jump_ratio(tile), 1.2, f"{layout} 边缘跳变超标")


class TestEdgeJump(unittest.TestCase):
    def test_broken_seam_detected(self):
        img = make_seamed(512)
        self.assertGreater(pl.edge_jump_ratio(img), 1.2)  # 右/下缘错位条 → 硬缝必须被抓出来

    def test_periodic_image_low_jump(self):
        img = make_periodic(128, 4)   # 512 = 4×128，四边天然连续
        self.assertLessEqual(pl.edge_jump_ratio(img), 1.2)

    def test_hard_seam_detected(self):
        arr = np.full((100, 100, 4), 255, dtype=np.uint8)
        arr[:, -1] = (0, 0, 0, 255)
        self.assertGreater(pl.edge_jump_ratio(Image.fromarray(arr)), 3.0)


class TestPeriod(unittest.TestCase):
    def test_find_period_exact(self):
        pw, ph, conf = pl.find_period(make_periodic(128, 4))
        self.assertEqual((pw, ph), (128, 128))
        self.assertGreaterEqual(conf, 0.35)

    def test_find_period_rect(self):
        pw, ph, conf = pl.find_period(make_periodic(128, 3).resize((768, 1152)))
        self.assertGreaterEqual(conf, 0.35)

    def test_no_period_low_conf(self):
        _, _, conf = pl.find_period(make_noisy(512))
        self.assertLess(conf, 0.35)

    def test_crop_period_tiles_back(self):
        src = make_periodic(128, 4)
        tile = pl.crop_period(src, 128, 128, 256)
        self.assertLessEqual(pl.edge_jump_ratio(tile), 1.2)


class TestSeamFix(unittest.TestCase):
    def test_blend_restores_periodicity(self):
        seamed = make_seamed(512)
        self.assertGreater(pl.edge_jump_ratio(seamed), 1.2)
        fixed = pl.seam_blend(seamed)
        self.assertLessEqual(pl.edge_jump_ratio(fixed), 1.2)

    def test_blend_deterministic(self):
        seamed = make_seamed(256)
        a = np.asarray(pl.seam_blend(seamed))
        b = np.asarray(pl.seam_blend(seamed))
        np.testing.assert_array_equal(a, b)

    def test_blend_preserves_size(self):
        seamed = make_seamed(300)
        self.assertEqual(pl.seam_blend(seamed).size, seamed.size)

    def test_mirror_tile_seamless(self):
        fixed = pl.mirror_tile(make_seamed(300))
        self.assertEqual(fixed.size, (300, 300))
        self.assertLessEqual(pl.edge_jump_ratio(fixed), 1.2)


# ---------- 主链（离线，直接调函数） ----------

class TestRunTile(unittest.TestCase):
    def test_route_a_happy_path(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "05_seamless"
            out.mkdir()
            warnings = []
            res = sm.run_tile(make_motif(200), out, "m1",
                              {"layout": "toss", "rotation": "two-way",
                               "scale_frac": 0.4, "bg": "transparent", "tile": 512,
                               "dpi": 150, "seed": 42, "density": "medium", "scale": "medium"},
                              warnings)
            self.assertTrue(res["ok"], res)
            self.assertEqual(res["verdict"], "constructive")
            self.assertLessEqual(res["checks"]["edge_jump_ratio"], 1.2)
            for f in res["files"]:
                self.assertTrue(Path(f).exists(), f)

    def test_route_a_deterministic(self):
        plan = {"layout": "half-drop", "rotation": "two-way", "scale_frac": 0.4,
                "bg": "transparent", "tile": 384, "dpi": 150, "seed": 42,
                "density": "medium", "scale": "medium"}
        outs = []
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "05_seamless"
            out.mkdir()
            for i in range(2):
                res = sm.run_tile(make_motif(150), out, f"m{i}", plan, [])
                outs.append(np.asarray(Image.open(res["files"][0]).convert("RGBA")))
        np.testing.assert_array_equal(outs[0], outs[1])  # 同 seed 同参数 → 像素一致

    def test_route_a_refuses_allover(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "05_seamless"
            out.mkdir()
            res = sm.run_tile(make_noisy(256), out, "m", dict(
                self._plan(), tile=256), [])   # 平滑随机场：无透明、去底无从下手 → 拒收
            self.assertFalse(res["ok"])
            self.assertIn("crop", res["next_action"])

    @staticmethod
    def _plan():
        return {"layout": "toss", "rotation": "two-way", "scale_frac": 0.4,
                "bg": "transparent", "tile": 512, "dpi": 150, "seed": 42,
                "density": "medium", "scale": "medium"}


class TestRouteSignal(unittest.TestCase):
    def test_motif_suggests_A(self):
        self.assertEqual(sm.route_signal(make_motif(120))["suggest"], "A")

    def test_periodic_suggests_B(self):
        self.assertEqual(sm.route_signal(make_periodic(128, 4))["suggest"], "B")

    def test_broken_suggests_C(self):
        self.assertEqual(sm.route_signal(make_allover_np(512))["suggest"], "C")
        self.assertEqual(sm.route_signal(make_noisy(512))["suggest"], "C")


# ---------- CLI 冒烟（子进程，全离线：清掉 key） ----------

def run_cli(args: list, cwd: Path) -> tuple:
    env = {k: v for k, v in os.environ.items()
           if k not in ("DASHSCOPE_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL")}
    p = subprocess.run([sys.executable, str(SCRIPTS / "seamless.py")] + args,
                       capture_output=True, text=True, cwd=cwd, env=env, timeout=300,
                       encoding="utf-8", errors="replace")
    lines = [ln for ln in (p.stdout or "").strip().splitlines() if ln.strip()]
    payload = json.loads(lines[-1]) if lines else {}
    return p.returncode, payload, p.stdout, p.stderr


class TestCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.cwd = Path(cls.tmp.name)
        cls.motif = cls.cwd / "motif.png"
        make_motif(200).save(cls.motif)
        cls.periodic = cls.cwd / "allover_repeat.png"
        make_periodic(128, 4).save(cls.periodic)
        cls.broken = cls.cwd / "allover_broken.png"
        make_allover_np(512).save(cls.broken)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_01_menu_zero_cost(self):
        code, out, _, _ = run_cli([str(self.motif)], self.cwd)
        self.assertEqual(code, 0)
        self.assertTrue(out["ok"])
        self.assertEqual(out["files"], [])
        self.assertEqual(out["menu"]["signal"]["suggest"], "A")
        self.assertIn("menu", out)

    def test_02_route_a_tile(self):
        code, out, _, err = run_cli([str(self.motif), "--mode", "tile", "--preset", "ditsy",
                                     "--tile", "512"], self.cwd)
        self.assertEqual(code, 0, err)
        self.assertEqual(out["verdict"], "constructive")
        self.assertLessEqual(out["checks"]["edge_jump_ratio"], 1.2)
        for f in out["files"]:
            self.assertTrue(Path(f).exists())
        self.assertIn("05_seamless", out["files"][0])

    def test_03_route_a_deterministic(self):
        run_cli([str(self.motif), "--mode", "tile", "--layout", "straight", "--tile", "384",
                 "--seed", "7"], self.cwd)
        f1 = self.cwd / "pattern_output" / "motif" / "05_seamless" / "motif_straight_t384_v1.png"
        run_cli([str(self.motif), "--mode", "tile", "--layout", "straight", "--tile", "384",
                 "--seed", "7"], self.cwd)
        f2 = self.cwd / "pattern_output" / "motif" / "05_seamless" / "motif_straight_t384_v2.png"
        self.assertTrue(f1.exists() and f2.exists())
        self.assertEqual(f1.read_bytes(), f2.read_bytes())  # 同 seed 同参数 → 字节一致

    def test_04_route_b_crop(self):
        code, out, _, err = run_cli([str(self.periodic), "--mode", "crop", "--tile", "256",
                                     "--no-card"], self.cwd)
        self.assertEqual(code, 0, err)
        self.assertEqual(out["verdict"], "period-crop")
        self.assertEqual(out["checks"]["period"], [128, 128])
        self.assertLessEqual(out["checks"]["edge_jump_ratio"], 1.2)

    def test_05_route_b_refuses_weak_peak(self):
        code, out, _, _ = run_cli([str(self.broken), "--mode", "crop", "--no-card"], self.cwd)
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertIn("--period", out["next_action"])

    def test_06_route_c_blend(self):
        code, out, _, err = run_cli([str(self.broken), "--mode", "seam", "--seam-fix", "blend",
                                     "--no-card"], self.cwd)
        self.assertEqual(code, 0, err)
        self.assertEqual(out["verdict"], "seam-fixed")
        self.assertLessEqual(out["checks"]["edge_jump_ratio"], 1.2)

    def test_07_route_c_manual_and_collect(self):
        code, out, _, err = run_cli([str(self.broken), "--mode", "seam", "--seam-fix", "manual",
                                     "--no-card"], self.cwd)
        self.assertEqual(code, 0, err)
        self.assertEqual(out["verdict"], "manual-pending")
        rolled = next(Path(f) for f in out["files"] if f.endswith("_rolled_for_fix.png"))
        self.assertTrue(Path(rolled).exists())
        # 模拟用户修好了：用 blend 结果充当"修缝稿"（同尺寸 → 触发自动 roll 回）
        fixed = pl.seam_blend(make_allover_np(512))
        fake = rolled.with_name("fixed_by_user.png")
        fixed.save(fake)
        code, out2, _, err2 = run_cli([str(rolled), "--collect", str(fake), "--no-card"],
                                      self.cwd)
        self.assertEqual(code, 0, err2)
        self.assertEqual(out2["verdict"], "seam-fixed")
        self.assertIn("roll", " ".join(out2["warnings"]))

    def test_08_workspace_continuation(self):
        """输入落在 05_seamless 下 → 自动续接工作区（不再开新工作区）。"""
        ws = self.cwd / "pattern_output" / "motif"
        target = ws / "05_seamless" / "in_ws.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        make_motif(120).save(target)
        code, out, _, err = run_cli([str(target), "--mode", "tile", "--tile", "256",
                                     "--no-card"], self.cwd)
        self.assertEqual(code, 0, err)
        self.assertIn("工作区续接", " ".join(out["warnings"]))

    def test_09_gate_routes_partial_to_extraction(self):
        """资格门：满幅不透明稿走路A → 拒收并指路（离线档下走 next_action 而非 VLM）。"""
        code, out, _, _ = run_cli([str(self.broken), "--mode", "tile", "--no-card"], self.cwd)
        self.assertEqual(code, 1)
        self.assertFalse(out["ok"])
        self.assertIn("满幅", " ".join(out["warnings"]) + out.get("next_action", ""))


class TestCheckEnv(unittest.TestCase):
    def test_check_env_json(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("DASHSCOPE_API_KEY", "OPENAI_API_KEY")}
        p = subprocess.run([sys.executable, str(SCRIPTS / "check_env.py")],
                           capture_output=True, text=True, env=env, timeout=120,
                           encoding="utf-8", errors="replace")
        out = json.loads(p.stdout.strip().splitlines()[-1])
        self.assertTrue(out["ok"])
        self.assertEqual(out["route"], "manual")
        self.assertIn("blend", out["note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
