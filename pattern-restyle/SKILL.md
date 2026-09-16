---
name: pattern-restyle
description: 风格延展（一稿多款）。识别驱动：VLM 看着原稿出「元素清单+原稿风格画像+延展方向提案」（默认 --styles auto，方向不预设），生成 prompt 走 CURRENT STYLE→TARGET STYLE 强关联，重画出多个风格版本（6 预设方向 watercolor/geometric/ethnic/vintage/minimal/handdrawn 仅点名时走 + custom 自定义）：GPT-Image 终点引擎（理解后重合成、透明底原生输出、多参考图）+ qwen 对照档（省钱）+ ChatGPT 手动档（提示词卡→人工跑→--collect 回接）。元素清单双用锚点（prompt 锁词=质检 checklist），出口质检宪法=必须改风格、但不能换内容（style_applied 对目标方向判），回炉≤2，跑版如实标注（无像素档兜底）。印花模块板块3（唯一以 GPT 为主引擎的板块）。触发词：风格延展、一稿多款、换风格、风格化、restyle、style transfer、系列延展。
---

# Pattern Restyle · 风格延展（印花模块 板块3）

一稿多款：同一张花稿，换画法重画出系列风格版本。**必须改风格，但不能换内容**——
这条宪法决定整条链的形状：元素清单既是 prompt 锁词又是质检 checklist，出口比对逐项
核对而非问「是否一致」。

**识别驱动（与板块1/2 的本质差异）**：板块1/2 目标机械（清晰/提取），识别只需答「图里
有什么」；本板块目标是风格，识别要先答「它现在是什么风格、离它远的方向有哪些」——
方向由 VLM 对着原稿自己框定（默认 `--styles auto`），不由预置模板框；生成端 prompt 写
CURRENT STYLE（原稿画像）→ TARGET STYLE（方向 spec）强关联。6 预设方向只是点名快捷方式。

> 本 skill 是「印花模块」的板块3，容器是 `Desktop\印花模块\pattern-workshop\`。
> 上游：板块1（pattern-extraction）的 `01_extracted/motif_XX.png`、板块2（pattern-enhance）
> 的 `02_hd/*_hd.png` 直接喂入，工作区自动续接。设计文档 03-风格延展.md 在「印花模块」根目录。

## 第一步：冷启动（必做）

```bash
python scripts/check_env.py
```

stdout 一行 JSON。`route`：`gpt`（OPENAI_API_KEY，终点引擎）｜`qwen`（仅 DASHSCOPE_API_KEY，
对照档）｜`manual`（无 key，提示词卡）。key 全走环境变量，永不写死：
`OPENAI_API_KEY` + `OPENAI_BASE_URL`（中国网络建议中转，中转直连绕死代理）+ `GPT_IMAGE_MODEL`（默认 gpt-image-1）。
眼睛（分诊/元素清单/质检）永远走便宜的 qwen-vl，GPT 只花在生图刀刃上。

## 主通道

```bash
python scripts/restyle.py <输入图> [--styles auto|all|watercolor,geometric|custom:<描述>]
    [--count 1] [--engine auto|gpt|qwen|manual] [--quality draft|final]
    [--carrier auto|motif|allover] [--transparent auto|on|off] [--ref <风格参考图>...]
    [--collect] [--name X] [--out pattern_output]
```

- **两段式花钱（默认即探索档）**：`--quality draft` 出方向联络表挑方向（gpt medium，
  便宜）→ 选中方向 `--styles <key> --quality final --count 3` 出成品（gpt high）。不要一步到 final
- **`--styles auto`（默认，识别驱动）**：VLM 对着原稿现场提案 5-6 个延展方向（离原稿
  风格足够远、可商用、维度错开），每张图的联络表都不一样；无 VLM/提案失败自动回落预设
  6 方向。`all` / 点名 = 预设快捷方式（点名时会检查方向与原稿风格画像同族并警告）
- **元素清单双用锚点 + 风格画像**：1 次 VLM 出「逐元素清单+构图摘要+原稿风格画像+方向
  提案」→ 清单拼进每方向 prompt 的锁词（DNA Lock 模式），画像作 CURRENT STYLE 锚，
  同一份清单在出口质检逐项核对。GPT 档无否定清单堆叠（理解力强，正面描述就够）；
  qwen 档保真条款钉死在后（对抗它「编辑原图」的惯性）
- **出口质检（与板块2 六道的差异）**：VLM 逐元素核对（missing/composition_same/
  style_applied，**style_applied 对目标方向判**——「还是原稿风格」也算风格未出）
  + 色板距离按 palette_policy 分策略（keep 盯紧原色板；restyle 只记录不毙——色板漂移
  是容忍不是手段，主动换色是板块6 的职责）+ allover 四边连续性只报不修
- **回炉 ≤2，每次必须换变量**：缺元素 → 逐元素点名重申；风格没出来 → 加风格强度词。
  超次**如实标注 verdict 交付**（无像素档兜底——像素档换不了画法），指路手动档调 prompt
- **透明底**：motif 载体默认透明底——gpt 原生 `background=transparent`；qwen/手动档输出
  白底 → 自动边缘连通色键去底。allover 满幅不去底
- 单风格 1-3 次生成（回炉计入）；不支持目录批量（按张计费，批量易烧钱，外层循环自管）

## 三路线

| 路线 | 条件 | 走法 |
|---|---|---|
| GPT 档（终点引擎） | OPENAI_API_KEY | 全自动：分诊→识别→逐方向→质检→联络表；透明底/多参考 `--ref` 原生支持 |
| qwen 对照档 | 仅 DASHSCOPE_API_KEY | 同链换引擎；跑版风险高（编辑原图心智），靠 VLM 质检兜底，效果差异记档 |
| 手动档 | 无 key / 想用 ChatGPT | `--engine manual` 出 `_prompt_cards.txt`（每方向整段英文提示词+原稿画像）→ 人工生图 → 结果存 `03_restyle/<stem>_<风格>_manual.png` → `--collect` 回接质检打包 |

## 输出契约

stdout 一行 JSON：`{ok, files, warnings, next_action, engine, carrier, inventory, palette, items, seconds}`。
工作区续接板块1/2；独立输入建 `rs_<名>`。产物 `03_restyle/<stem>_<风格>[_vN].png`（motif=透明底
RGBA）+ `contact_sheet.jpg`（原稿列+各风格列，一眼判断）+ `meta.json`（逐张 prompt/verdict/
attempts/palette_dist 全记档）+ 手动档 `_prompt_cards.txt`。ws 台账 `record_step("restyle")` 记 lineage。

## 已知边界

- **无像素档兜底**（与板块2 最大差异）：质检不过 = 如实标注 missing/style_applied 交付
- qwen 档「水彩上叠几何线」式半吊子重绘是模型心智问题，提示词只能压不能根治——对照档定位是
  省钱探索，成品走 GPT
- 方向话术在 `scripts/style_templates.py`：`make_direction()` 把 VLM 提案归一成与预设同构的
  dict；6 预设仅快捷方式。常用 VLM 提案方向值得回流入库（跑几次后看 meta.json 提案统计）
- 一次性风格 `--styles custom:<描述>`（描述建议英文）；VLM 提案质量依赖原稿本身，
  离散型几何稿提案会比水彩稿更跳
- vintage/ethnic 与板块6（配色重组）的边界：03 允许风格自然色板漂移，**主动换色不做**
- allover 输出四边连续性超阈只报不修——无缝循环是板块5 的职责

## 与其他板块的衔接

| 板块 | 关系 |
|---|---|
| 板块1 pattern-extraction | 上游：`01_extracted/motif_XX.png` 直喂；资格门把非花稿指回它 |
| 板块2 pattern-enhance | 上游：`02_hd/` 高清稿直喂；判型卡 `triage_card.py` 同源副本 |
| 板块5 无缝循环 | allover 输出的四边连续性告警指向它 |
| 板块6 配色重组 | 配色变化的职责归属：本板块只容忍风格性漂移，不主动换色 |
| `engines.py` | 引擎公共件起源于本 skill（gpt/qwen/manual 统一 `generate()`），板块1/2 切 GPT 时自备副本改一行参数 |

## 依赖

```bash
pip install -r requirements.txt    # pillow numpy scipy scikit-image（API 全走 urllib，无 SDK）
```
