# -*- coding: utf-8 -*-
"""四类合成测试图（对应分诊 A/B/C/D）+ 印花坐标输出（给 --box 用）。

用法：python make_test_images.py  → 生成到 tests/assets/
"""
from __future__ import annotations

import math
import random
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

random.seed(7)
OUT = Path(__file__).resolve().parent / "assets"


def flower(d: ImageDraw.ImageDraw, cx, cy, r,
           petal=(214, 69, 89), core=(247, 197, 72)):
    for k in range(6):
        ang = math.radians(k * 60 + 12)
        x, y = cx + r * math.cos(ang), cy + r * math.sin(ang)
        d.ellipse([x - r * 0.62, y - r * 0.62, x + r * 0.62, y + r * 0.62], fill=petal)
    d.ellipse([cx - r * 0.45, cy - r * 0.45, cx + r * 0.45, cy + r * 0.45], fill=core)


def leaf(d: ImageDraw.ImageDraw, cx, cy, s, color=(88, 140, 70)):
    d.polygon([(cx, cy - s), (cx + s * 0.7, cy + s * 0.6), (cx - s * 0.7, cy + s * 0.6)], fill=color)


def shirt(d: ImageDraw.ImageDraw, cx, cy, w, h, color):
    x0, y0 = cx - w // 2, cy - h // 2
    pts = [(x0 + int(w * .18), y0), (x0 + int(w * .82), y0),
           (x0 + w + int(w * .10), y0 + int(h * .30)), (x0 + w - int(w * .05), y0 + int(h * .46)),
           (x0 + int(w * .62), y0 + int(h * .34)), (x0 + int(w * .62), y0 + h),
           (x0 + int(w * .38), y0 + h), (x0 + int(w * .38), y0 + int(h * .34)),
           (x0 + int(w * .05), y0 + int(h * .46)), (x0 - int(w * .10), y0 + int(h * .30))]
    d.polygon(pts, fill=color)


def print_motif(d, cx, cy, s=48):
    leaf(d, cx - int(s * 1.2), cy + int(s * 0.9), int(s * 0.5))
    leaf(d, cx + int(s * 1.2), cy + int(s * 0.9), int(s * 0.5))
    flower(d, cx, cy, s)
    return (cx - int(s * 1.9), cy - int(s * 1.7), cx + int(s * 1.9), cy + int(s * 1.7))


def product():
    """A 白底产品图：白底 + 浅灰T恤 + 高对比胸前印花。"""
    img = Image.new("RGB", (1200, 900), (255, 255, 255))
    d = ImageDraw.Draw(img)
    shirt(d, 600, 470, 520, 560, (226, 229, 233))
    box = print_motif(d, 600, 430, 52)
    p = OUT / "typeA_product.png"
    img.save(p)
    return p, box


def scene():
    """B 场景商拍图：蓝灰渐变背景 + 衣服投影 + 同款印花。"""
    w, h = 1200, 900
    yy, xx = np.mgrid[0:h, 0:w]
    g = (150 + 60 * yy / h).astype(np.uint8)
    bg = np.stack([g - 8, g - 2, g + 14], -1).astype(np.uint8)
    img = Image.fromarray(bg, "RGB")
    d = ImageDraw.Draw(img)
    d.ellipse([300, 700, 900, 860], fill=(96, 100, 108))  # 投影
    img = img.filter(ImageFilter.GaussianBlur(6))
    d = ImageDraw.Draw(img)
    shirt(d, 600, 440, 520, 560, (222, 224, 227))
    box = print_motif(d, 600, 400, 52)
    p = OUT / "typeB_scene.png"
    img.save(p)
    return p, box


def vibe():
    """C 网感图：B 的底子 + 暖调滤镜 + 颗粒 + 文字贴纸叠加（风格≠差图）。"""
    p, _ = scene()
    img = Image.open(p).convert("RGB")
    a = np.asarray(img, dtype=np.float32)
    a[..., 0] = np.clip(a[..., 0] * 1.12 + 10, 0, 255)   # 暖调
    a[..., 2] = np.clip(a[..., 2] * 0.88, 0, 255)
    noise = np.random.default_rng(7).normal(0, 7, a.shape[:2])[..., None]
    a = np.clip(a + noise, 0, 255).astype(np.uint8)
    img = Image.fromarray(a, "RGB")
    d = ImageDraw.Draw(img)
    d.rectangle([60, 780, 620, 850], fill=(255, 255, 255))   # 贴纸条
    d.text((80, 800), "OUTFIT #23 - daily look", fill=(40, 40, 40))
    d.ellipse([1020, 60, 1140, 180], fill=(255, 210, 80), outline=(255, 160, 40), width=4)  # 贴纸
    p2 = OUT / "typeC_vibe.png"
    img.save(p2)
    return p2, None


def fabric():
    """D 面料平铺图：米色底 + 4x3 重复花纹（判重验收）。"""
    img = Image.new("RGB", (1200, 900), (238, 231, 220))
    d = ImageDraw.Draw(img)
    for r in range(3):
        for c in range(4):
            cx, cy = 150 + c * 300 + (r % 2) * 60, 150 + r * 300
            leaf(d, cx - 60, cy + 42, 26)
            leaf(d, cx + 60, cy + 42, 26)
            flower(d, cx, cy, 46, petal=(200 + random.randint(-10, 10), 66, 88))
    p = OUT / "typeD_fabric.png"
    img.save(p)
    return p, None


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for fn in (product, scene, vibe, fabric):
        p, box = fn()
        print(f"{p.name}  box={box}")
