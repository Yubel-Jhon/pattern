# -*- coding: utf-8 -*-
"""板块1 · 图案提取 v3：生图主链 + 轻代码补位 + 保真档（离线）。

三路线（能力探测自动选，见 00 §2.5）：
  ① 全自动  --fidelity gen（默认）：提示词×参考图 → DashScope qwen-image-edit → 白底提取图
  ② 半自动  --gen-image <path>：用户在任意生图工具手动生成的结果图 → 本脚本接手打包
  ③ 离线    --fidelity pixel：保真档（色键/色差/连通域；--box 走 FastSAM），像素级零幻觉

轻代码补位（生图做不到的）：连通域拆分独立纹样 / phash 判重 / 后处理与元数据。
输出契约：{ok, files, warnings, next_action, capabilities, motifs, ...}

用法：
  python extract_pattern.py <input> [--name X] [--type auto|product|scene|vibe|fabric]
      [--fidelity auto|gen|pixel] [--box x1,y1,x2,y2] [--min-size 32] [--no-dedup]
      [--max-motifs 24] [--granularity group|element] [--semantic-names] [--keep-vibe]
      [--gen-image <path>] [--vlm-check top|all|off] [--out pattern_output]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from patternlib import (annotated_preview, capabilities, capabilities_next_action,
                        check_input_size, contact_sheet, content_crop, dedup_motifs,
                        edge_clean, emit, ensure_workspace, local_name,
                        make_sidebyside_preview, on_white, pasteback_preview,
                        record_step, save_raw, split_motifs, step_dir, triage, trim_frame)
from prompt_templates import (KEEP_VIBE_EXTRA, VLM_COMPARE_PROMPT, VLM_NAME_PROMPT,
                              build_extract_prompt)

# ---------------- 保真档（v2 已测通代码，降级至此） ----------------

SESSIONS = {}


def rembg_cut(img: Image.Image, model: str) -> Image.Image:
    from rembg import new_session, remove
    if model not in SESSIONS:
        SESSIONS[model] = new_session(model)
    return remove(img, session=SESSIONS[model])


def colorkey_rgba(img: Image.Image, tol: int = 40) -> tuple:
    """均匀底色键（fabric/白底产品图保真档主路径）。返回 (RGBA, 警告)。"""
    warnings = []
    rgb = np.array(img.convert("RGB")).astype(np.int16)
    ring = 10
    border = np.concatenate([rgb[:ring].reshape(-1, 3), rgb[-ring:].reshape(-1, 3),
                             rgb[:, :ring].reshape(-1, 3), rgb[:, -ring:].reshape(-1, 3)])
    med = np.median(border, axis=0)
    if border.std(axis=0).mean() > 18:
        warnings.append("边界背景不均匀，保真档色键效果有限（建议走生图主链）")
        rgba = img.convert("RGBA")
    else:
        dist = np.sqrt(((rgb - med) ** 2).sum(-1))
        alpha = np.where(dist < tol, 0, 255).astype(np.uint8)
        rgba = Image.merge("RGBA", (*Image.fromarray(rgb.astype(np.uint8)).split(),
                                    Image.fromarray(alpha)))
    return rgba, warnings


FASTSAM_WEIGHTS = ("FastSAM-s.pt",)


def find_fastsam_weights():
    here = Path(__file__).resolve().parent.parent / "models"
    if here.exists():
        hits = sorted(here.glob("FastSAM*.pt"))
        if hits:
            return str(hits[0])
    for w in FASTSAM_WEIGHTS:
        if Path(w).exists():
            return w
    return None


def fastsam_box_cut(img: Image.Image, box: tuple) -> tuple:
    warnings = []
    try:
        from ultralytics import FastSAM
    except ImportError:
        warnings.append("未安装 ultralytics，--box 降级为裁剪+色键/抠图。增强档：pip install ultralytics")
        return None, warnings
    weights = find_fastsam_weights()
    try:
        model = FastSAM(weights) if weights else FastSAM(FASTSAM_WEIGHTS[0])
        if weights is None:
            warnings.append("FastSAM 权重自动下载中（FastSAM-s.pt，~23MB）")
        results = model(np.array(img.convert("RGB")), device="cpu", retina_masks=True,
                        bboxes=[box[0], box[1], box[2], box[3]], conf=0.4, iou=0.9, verbose=False)
        r0 = results[0]
        if r0.masks is None or len(r0.masks) == 0:
            warnings.append("FastSAM 未命中该框，降级为框内色键")
            return None, warnings
        m = r0.masks.data[int(r0.boxes.conf.argmax())] if (r0.boxes is not None and len(r0.boxes)) else r0.masks.data[0]
        alpha = (m.cpu().numpy() * 255).astype(np.uint8)
        if alpha.shape != (img.height, img.width):
            alpha = np.array(Image.fromarray(alpha).resize(img.size))
        rgba = Image.merge("RGBA", (*img.convert("RGB").split(), Image.fromarray(alpha)))
        return rgba, warnings
    except Exception as e:
        warnings.append(f"FastSAM 运行失败({e.__class__.__name__})，降级为框内色键")
        return None, warnings


def print_extract(garment_rgba: Image.Image, tol: int = 55) -> Image.Image:
    """从衣服（已去背景的区域）里剥印花：衣服主色=基底层，与主色差异大的=标记层。
    这是保真档对 product/scene 的正确语义——输出印花，不输出衣服。"""
    rgb = np.asarray(garment_rgba.convert("RGB"), dtype=np.float32)
    a = np.array(garment_rgba.split()[-1]) > 128
    if not a.any():
        return garment_rgba
    # 基底色 = 衣服区域占比最大的颜色（24 级量化直方图）
    q = (rgb[a] // 24).astype(int)
    keys = q[:, 0] * 10000 + q[:, 1] * 100 + q[:, 2]
    vals, counts = np.unique(keys, return_counts=True)
    top = vals[counts.argmax()]
    base = rgb[a][keys == top].mean(axis=0)
    # 标记层 = 与基底差异超阈值
    dist = np.sqrt(((rgb - base) ** 2).sum(-1))
    mask = (dist > tol) & a
    alpha = (mask * 255).astype(np.uint8)
    out = Image.merge("RGBA", (*Image.fromarray(rgb.astype(np.uint8)).split(),
                               Image.fromarray(alpha)))
    return out


def pixel_route(img: Image.Image, itype: str, box, warnings: list) -> tuple:
    """保真档：像素级提取，返回 (full_rgba 同原图尺寸, 模型标注)。"""
    if box:
        rgba, w = fastsam_box_cut(img, box)
        warnings += w
        if rgba is None:  # 降级：框内色键
            x1, y1, x2, y2 = [max(0, v) for v in box]
            crop = img.crop((x1, y1, min(img.width, x2), min(img.height, y2)))
            c_rgba, w2 = colorkey_rgba(crop)
            warnings += w2
            base = Image.new("RGBA", img.size, (0, 0, 0, 0))
            base.paste(c_rgba, (x1, y1))
            rgba = base
        return rgba, "fastsam-box" if "降级" not in "".join(w) else "crop-colorkey"
    if itype == "fabric":
        rgba, w = colorkey_rgba(img)
        warnings += w
        return rgba, "colorkey"
    if itype == "product":
        garment, w = colorkey_rgba(img)
        warnings += w
        return print_extract(garment), "colorkey+print-extract"
    # scene/vibe：rembg 先去背景拿衣服 → 再从衣服里剥印花
    try:
        garment = rembg_cut(img, "u2net")
        return print_extract(garment), "rembg+print-extract"
    except Exception:
        garment, w = colorkey_rgba(img)
        warnings += w + ["scene/vibe 保真档建议 pip install rembg（离线主体抠图）"]
        return print_extract(garment), "colorkey+print-extract"


# ---------------- 生图主链 ----------------

def white_key(gen_img: Image.Image) -> Image.Image:
    """生图输出的白底提取图 → RGBA（离白越远越不透明）。
    门槛 28：模型常把衣服残影淡化成极浅灰（dist 10~45），不设底噪会留鬼影。"""
    rgb = np.asarray(gen_img.convert("RGB"), dtype=np.float32)  # int16 会存不下 (px-255)^2 溢出成 NaN
    dist = np.sqrt(((rgb - 255) ** 2).sum(-1))
    alpha = np.clip((dist - 28) * 8, 0, 255).astype(np.uint8)
    return Image.merge("RGBA", (*gen_img.convert("RGB").split(), Image.fromarray(alpha)))


def clean_hint(rgba: Image.Image, garment_alpha: np.ndarray, erode_px: int = 6,
               min_sat: int = 30) -> Image.Image:
    """粗提引导图清洗（三刀，按序）：
    ① 衣服主体 = rembg alpha 最大连通域（贴纸/阴影常被误框成游离小块）；
    ② 蒙版腐蚀后只保留内部标记层（衣服轮廓线贴边，被腐蚀掉；印花在内部，保留；
       印花与轮廓相连也不误杀——不做连通域过滤）；
    ③ 低饱和且偏暗的像素 = 阴影类杂物，弃（纯灰印花会误伤，见 01 §6 已知局限）。"""
    from skimage.measure import label
    from skimage.morphology import disk, erosion
    mask = garment_alpha > 128
    labs = label(mask)
    if labs.max() > 1:  # 只留最大连通域 = 衣服主体
        sizes = np.bincount(labs.ravel())
        sizes[0] = 0
        mask = labs == sizes.argmax()
    interior = erosion(mask, disk(erode_px))
    hsv = np.asarray(rgba.convert("HSV"))
    junk = (hsv[..., 1] < min_sat) & (hsv[..., 2] < 200)  # 阴影/灰黑杂物
    a = np.array(rgba.split()[-1])
    alpha = np.where(interior & ~junk, a, 0).astype(np.uint8)
    out = rgba.copy()
    out.putalpha(Image.fromarray(alpha))
    return out


def gen_route(img: Image.Image, itype: str, args, caps: dict, warnings: list,
              out_dir: Path | None = None):
    """路线①②：返回 (白底提取图 PIL RGB, 途径标注) 或 None（走不了）。
    C 类（vibe）走两段式：轻代码粗提构图 → 生图重绘（实测直提时模型总把衣服端出来）。"""
    if args.gen_image:  # 半自动②：用户手动生成结果
        p = Path(args.gen_image)
        if not p.exists():
            warnings.append(f"--gen-image 路径不存在：{p}")
            return None, None
        return Image.open(p).convert("RGB"), "manual"
    if not caps["gen_api"]:
        return None, None
    try:
        from dashscope_gen import DashScopeError, qwen_i2i
        from prompt_templates import REDRAW_PROMPT, build_extract_prompt
        if itype == "vibe":
            try:
                garment = rembg_cut(img, "u2net")
            except Exception:
                garment, _ = colorkey_rgba(img)
            galpha = np.array(garment.split()[-1])
            hint = clean_hint(print_extract(garment), galpha)
            a = np.array(hint.split()[-1])
            if (a > 128).sum() >= 500:  # 粗提成功 → 两段式
                if out_dir is not None:
                    on_white(hint).save(out_dir / "_vibe_hint.png")
                extra = KEEP_VIBE_EXTRA if args.keep_vibe else ""
                out = qwen_i2i([img, on_white(hint)], REDRAW_PROMPT.format(extra=extra))
                return out, "dashscope两段式(粗提→重绘)"
            warnings.append("C 类粗提引导图失败，回退直提模板")
        prompt = build_extract_prompt(itype, keep_vibe=args.keep_vibe)
        out = qwen_i2i([img], prompt)
        return out, "dashscope-qwen-image-edit"
    except DashScopeError as e:
        warnings.append(f"生图主链调用失败：{e}")
        return None, None


# ---------------- 主流程 ----------------

def main() -> int:
    p = argparse.ArgumentParser(description="板块1 图案提取 v3")
    p.add_argument("input", help="输入图片路径")
    p.add_argument("--name", default=None)
    p.add_argument("--type", default="auto", choices=["auto", "product", "scene", "vibe", "fabric"])
    p.add_argument("--fidelity", default="auto", choices=["auto", "gen", "pixel"],
                   help="auto=有 key 走生图主链，否则保真档")
    p.add_argument("--box", default=None, help="x1,y1,x2,y2（保真档/先裁后生用）")
    p.add_argument("--min-size", type=int, default=32)
    p.add_argument("--no-dedup", action="store_true")
    p.add_argument("--max-motifs", type=int, default=24)
    p.add_argument("--granularity", default="group", choices=["group", "element"])
    p.add_argument("--semantic-names", action="store_true", help="VLM 语义命名（需 key）")
    p.add_argument("--keep-vibe", action="store_true", help="C 类保留网感色（默认本真还原）")
    p.add_argument("--gen-image", default=None, help="手动模式：已生成的白底提取图路径")
    p.add_argument("--vlm-check", default="top", choices=["top", "all", "off"])
    p.add_argument("--out", default="pattern_output")
    args = p.parse_args()

    src = Path(args.input)
    if not src.exists():
        caps = capabilities()
        emit(False, [], [], f"输入不存在：{src}", capabilities=caps,
             next_action_alt=capabilities_next_action(caps))
        return 1
    img = Image.open(src)
    img.load()
    caps = capabilities()
    warnings = []
    if w := check_input_size(img):
        warnings.append(w)

    # 分诊
    t = triage(img)
    warnings += t["warnings"]
    itype = args.type if args.type != "auto" else t["type_hint"]
    box = None
    if args.box:
        try:
            box = tuple(int(v) for v in args.box.split(","))
            assert len(box) == 4
        except Exception:
            emit(False, [], warnings, "--box 格式应为 x1,y1,x2,y2（整数像素）", capabilities=caps)
            return 1

    # 落盘准备（提前建 workspace：生图原始返回也要存，调后处理不必重烧 API）
    ws = ensure_workspace(Path(args.out), args.name or src.stem)
    save_raw(ws, src)
    out_dir = step_dir(ws, "extract")

    # 路线选择
    fid = args.fidelity
    if fid == "auto":
        fid = "gen" if (caps["gen_api"] or args.gen_image) else "pixel"
    full_rgba = None
    gen_img = None
    if fid == "gen" and not box:  # 有 box 时 box 优先（保真/精提）
        gen_img, _ = gen_route(img, itype, args, caps, warnings, out_dir=out_dir)
        if gen_img is not None:
            gen_img = trim_frame(gen_img)
            gen_img.save(out_dir / "_raw_gen_response.png")  # 原始返回留档
            full_rgba = white_key(gen_img)
        else:
            if caps["pixel_tier"]["ready"]:
                warnings.append("生图主链不可用，自动降级保真档")
                fid = "pixel"
            else:
                emit(False, [], warnings, capabilities_next_action(caps),
                     capabilities=caps, triage=t)
                return 1
    else:
        fid = "pixel"
    if fid == "pixel":
        full_rgba, model_used = pixel_route(img, itype, box, warnings)

    full_rgba = edge_clean(full_rgba)
    if np.array(full_rgba.split()[-1]).max() == 0:
        emit(False, [], warnings,
             "提取结果全透明：未检测到与基底差异的纹样。next_action：走生图主链（配 key 或 --gen-image 手动模式）；"
             "或放宽 --min-size；或 --box 指定纹样位置", capabilities=caps, triage=t)
        return 1

    # 轻代码补位：拆分 + 判重
    merge_px = 6 if args.granularity == "group" else 1
    motifs = split_motifs(full_rgba, min_size=args.min_size, merge_px=merge_px)
    if not motifs:
        emit(False, [], warnings,
             f"未拆出 >= {args.min_size}px 的独立纹样。next_action：调小 --min-size；"
             "或 --granularity element；或走生图主链重新提取", capabilities=caps, triage=t)
        return 1
    if len(motifs) > args.max_motifs:
        warnings.append(f"检出 {len(motifs)} 个纹样超过上限 {args.max_motifs}，已按完整性取前 "
                        f"{args.max_motifs} 个；密集小图建议调大 --min-size")
        motifs = motifs[:args.max_motifs]
    clusters = motifs if args.no_dedup else dedup_motifs(motifs)
    if args.no_dedup:
        clusters = [{"img": m["img"], "bbox": m["bbox"], "count": 1, "positions": [m["bbox"]]}
                    for m in motifs]

    # 命名：本地规则默认；--semantic-names 走 VLM（失败回落本地）
    names = []
    for i, c in enumerate(clusters, 1):
        nm = None
        if args.semantic_names and caps["vlm"]:
            try:
                from dashscope_gen import vlm_chat
                nm = vlm_chat([c["img"]], VLM_NAME_PROMPT).strip().split()[0][:24]
            except Exception:
                warnings.append("VLM 语义命名失败，回落本地规则命名")
        names.append(nm or local_name(c["img"], i))

    # 落盘（workspace 已在路线选择前建好）
    files = []
    meta_motifs = []
    for i, (c, nm) in enumerate(zip(clusters, names), 1):
        m_rgba = edge_clean(content_crop(c["img"]))
        fp = out_dir / f"{nm}.png"
        m_rgba.save(fp)
        wp = out_dir / f"{nm}_white.png"
        on_white(m_rgba).save(wp)
        files += [fp, wp]
        meta_motifs.append({"name": nm, "count": c["count"], "bbox": list(c["bbox"]),
                            "size": list(m_rgba.size),
                            "positions": [list(x) for x in c["positions"]]})

    # 预览三件套
    if fid == "pixel":
        prev_ann = annotated_preview(img, motifs)
        prev_back = pasteback_preview(img, full_rgba)
        prev_back.save(out_dir / "preview_pasteback.jpg", quality=88)
        files.append(out_dir / "preview_pasteback.jpg")
    else:
        prev_ann = make_sidebyside_preview(img, full_rgba, left_label="ORIGINAL",
                                           right_label="EXTRACTED SHEET")
    prev_ann.save(out_dir / "preview_annotated.jpg", quality=88)
    contact_sheet(clusters, names).save(out_dir / "preview_contact.jpg", quality=88)
    files += [out_dir / "preview_annotated.jpg", out_dir / "preview_contact.jpg"]

    # 校验②：颜色漂移量化（提取主色 vs 原图前景主色）
    try:
        fa = np.array(full_rgba.split()[-1]) > 128
        if fa.any():
            got = np.asarray(on_white(full_rgba), dtype=float)[fa].mean(axis=0)
            rgbA = np.asarray(img.convert("RGB"), dtype=float)
            tmask = np.sqrt(((rgbA - np.median(
                np.concatenate([rgbA[:10].reshape(-1, 3), rgbA[-10:].reshape(-1, 3)]), axis=0)) ** 2).sum(-1)) > 60
            ref = rgbA[tmask].mean(axis=0) if tmask.any() else rgbA.reshape(-1, 3).mean(axis=0)
            drift = float(np.abs(got - ref).mean())
            # scene/vibe 模板要求"还原成平面布料色/去环境光"，色差是还原不是漂移；
            # 只有 product/fabric（锚保真类型）高色差才算事故。
            if drift > 60 and itype in ("product", "fabric"):
                warnings.append(f"颜色漂移偏高（ΔRGB={drift:.0f}）：重绘可能改变了色调，"
                                "建议人工比对 preview_contact.jpg 与原图")
            t["color_drift"] = round(drift, 1)
            if drift > 60 and itype in ("scene", "vibe"):
                t["color_restore"] = round(drift, 1)  # 还原量，信息性指标
    except Exception:
        pass

    # 校验④：VLM 比对（默认只查最大件）
    vlm_note = None
    if args.vlm_check != "off" and caps["vlm"] and fid == "gen":
        try:
            from dashscope_gen import vlm_chat
            tops = clusters if args.vlm_check == "all" else clusters[:1]
            for c in tops:
                ans = vlm_chat([img, c["img"]], VLM_COMPARE_PROMPT)
                vlm_note = ans.strip()[:200]
                if '"same": false' in ans.replace(" ", ""):
                    warnings.append(f"VLM 比对存疑：{vlm_note}")
                break
        except Exception:
            warnings.append("VLM 比对调用失败，跳过（其余校验仍有效）")

    fidelity_label = {"pixel": "pixel", "gen": "gen"}[fid]
    record_step(ws, "extract", {
        "source": str(src), "type": itype, "fidelity": fidelity_label,
        "route": "manual" if args.gen_image else ("dashscope" if fid == "gen" else model_used),
        "triage": t, "motifs": meta_motifs, "files": [f.name for f in files],
        "warnings": warnings, "vlm_note": vlm_note,
    })
    na = capabilities_next_action(caps)
    emit(True, files, warnings, na, name=ws.name, type=itype, fidelity=fidelity_label,
         triage=t, motifs=meta_motifs, vlm_note=vlm_note, capabilities=caps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
