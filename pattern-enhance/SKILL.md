---
name: pattern-enhance
description: 纹样高清化（高清修复/超分辨率）。低清花稿一键高清化：生图模型补细节（糊稿→1024 高清稿）+ ESRGAN 像素接力（1024→4096，Vulkan 核显可用）+ VLM 质检成环（资格门拦非花稿、出口设计一致性比对）。宪法=只修质量，永不改设计——重绘漂了就回炉，再不行保真像素档兜底并如实告知。无 key 有提示词卡手动档与离线像素档。印花模块板块2。触发词：纹样高清化、高清修复、超分、放大花稿、补纹理、变清晰、enhance、upscale、HD。
---

# Pattern Enhance · 纹样高清化（印花模块 板块2）

低清花稿一键高清化，**只修质量，永不改设计**。三引擎分工：
生图模型=细节引擎（糊稿重绘到 1024），像素层=尺寸引擎（1024→4096 ESRGAN 接力），
VLM=质检员（入口资格门+判型，出口设计一致性比对）。

> 本 skill 是「印花模块」的板块2，容器是 `Desktop\印花模块\pattern-workshop\`，
> 板块1（图案提取）= 平级的 `pattern-extraction` skill；其产物 `01_extracted/motif_XX.png`
> 可直接喂给本 skill（工作区自动识别续接）。设计文档 02-高清修复.md 在「印花模块」根目录。

## 第一步：冷启动（必做）

```bash
python scripts/check_env.py
```

stdout 一行 JSON。`capabilities.hd.method`：`ncnn_vulkan`=GPU 超分可用（便携包已内置，
AMD/Intel 核显实测可用）｜`lanczos_only`=自动落 Lanczos（诚实标注，永不硬失败）。
key 走环境变量 `DASHSCOPE_API_KEY`，不写死、不落盘。

## 主通道

```bash
python scripts/enhance.py <输入图或目录> [--scale 4] [--gen auto|on|off]
    [--texture auto|flat|watercolor|photo] [--carrier auto|motif|allover]
    [--model auto|anime|photo] [--sharpen auto|off] [--force] [--name X] [--out pattern_output]
```

- **入口三拍**（1 次 VLM 出卡，`scripts/triage_card.py` 判型公共件）：
  资格门——不是花稿（服装照/场景照）→ 拒收并指路板块1；分诊——载体 motif/allover ×
  质地 flat/watercolor/photo + 缺陷清单。判型翻车用 `--texture/--carrier` 覆盖
- **执行拓扑序**：预处理（重度校色/去压噪）→ 生图段（**短边<1024 才进**；≥2048 默认跳过，
  `--force` 强跑）→ 后处理段（去底：平色=色键/软边=alpha 分源+bbox 配准；ESRGAN 4× 接力；
  轻锐化）
- **校验六道**：黑图守卫 / VLM 设计一致性（不问谁更清楚）/ 主色板距离（KMeans）/ 质地复查 /
  四边连续性（满幅只报不修，指路板块5）/ alpha 面积守卫
- **回炉 ≤2**，每次必须换变量（收紧条款/保守条款/先校色/裁焦点/换像素档）；超次进反馈层，
  **保真像素档兜底交付并明确告知**（生图原稿 `_gen_N.png` 留档复盘）
- 单图 40-92s；`method ∈ ncnn_vulkan | lanczos_only | skipped_already_hd`

## 输出契约

stdout 一行 JSON：`{ok, files, warnings, next_action, gate, triage, stages, method, seconds}`。
工作区：输入 `01_extracted/motif_XX.png` 自动续接原工作区；独立输入建 `hd_<名>` 工作区。
产物 `02_hd/<原名>_hd.png + before_after.jpg + meta.json`（+ `_prompt_card.txt` 手动档卡 +
`_gen_N.png` 生图留档），工作区 meta `record_step("hd")` 记 lineage。

## 三路线

| 路线 | 条件 | 走法 |
|---|---|---|
| 全自动 | DASHSCOPE_API_KEY + Vulkan GPU | 判型→生图→接力→六道校验全链 |
| 手动档 | 任意生图工具 | 用 `02_hd/_prompt_card.txt` 人工生图，结果图丢回跑 `--gen off` 接力打包 |
| 离线兜底 | 无 key | 像素档直跑，诚实标注 lanczos_only |

## 已知边界（详见 02-高清修复.md §13 落地实录）

- qwen i2i 重绘必然改设计（平色稿水彩化/元素重排）——靠 VLM 比对+回炉+像素兜底兑现宪法，
  版画类生图档可能被从严撤下走像素档（可用但无补纹）
- 质地判型偶偏（羽化软边平色稿判 watercolor），`--texture` 一键覆盖
- 满幅稿四边连续性超阈只报不修——无缝循环是板块5 的职责

## 与其他板块的衔接

| 板块 | 关系 |
|---|---|
| 板块1 pattern-extraction | 上游：`01_extracted/` 花稿直接喂；资格门把非花稿指回它 |
| 板块5 无缝循环 | 四边连续性告警指向它；它消费本板块的高清回样 |
| 板块3-6 | 判型卡公共件 `triage_card.py` 起源于本 skill，各板块自备副本 |

## 依赖

```bash
pip install pillow numpy scikit-image    # 无 SDK 依赖（API 走 urllib）
```

像素引擎 `tools/realesrgan/`：realesrgan-ncnn-vulkan **20220424** 便携包（45MB 级，解压即用，
需 Vulkan GPU——探测顺序 tools/ → REALESRGAN_BIN → PATH）；没有它自动落 Lanczos，永不硬失败。
