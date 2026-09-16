---
name: pattern-variants
description: 图案变体（一稿多花稿）。两方向分岔：方向一=客户觉得能直接处理的图（参数化道，纯代码主轴零成本）——五轴菜单【选项+自由】两类（排列 straight/half-drop/brick/mirror/toss 散点、方向性、密度、比例、配色变换、底色画幅）+ 5 行业 preset + 一句话 --custom（1 次 VLM 解析成封闭参数）；丰富度核心=toss 散点（Poisson disk 布点+角度/缩放抖动，不重叠保证），同 seed 同参数像素级可复现，出口量化校验（覆盖率±25%带/最近邻/连通域，数字说话不靠肉眼）。方向二=复杂图案参数轴改不好的（照片级/扎染/满幅肌理/轴外需求）——--free "提示词" 提示词+原图直接交生图模型，中间无人动用户的话，不解析不质检，产物 verdict=free 联络表单独分区不混装。菜单优先不默认生成（不带参数=零成本出菜单）；方向二花真金白银必须用户显式选择。资格门拦截（非花稿连菜单都不出，指路板块1）。印花模块板块4。触发词：图案变体、变体、系列花稿、换排列、散点、toss、half-drop、半降、碎花、满铺、pattern variants、repeat 变体。
---

# Pattern Variants · 图案变体（印花模块 板块4）

一稿出系列花稿：同一张图案，改排布与配色出一批不同观感的花稿。**只改排布与配色，
不改图案本体**——这条宪法决定方向一的形状（纯代码=数学保证）；方向二的宪法是用户
提示词本身，产物亮明身份不混装。

**两方向分岔（顶层结构）**：

| | 方向一 · 直接处理 | 方向二 · 自由生成 |
|---|---|---|
| 适合的图 | 独立花型件（透明底最佳）、规整满幅稿 | 照片级/扎染/满幅肌理——像素变换会碎的 |
| 适合的需求 | 落在五轴上（换色/密度/比例/排列/画布） | 轴外的任何想法（"转黄昏调""氛围浓郁一点"） |
| 表达方式 | 菜单选项 / 一句话 --custom / preset | 用户自己写 --free 提示词 |
| 成本 | 纯代码免费（满幅排列档才花钱） | 按张计费 |
| 产物 | 参数全记可复现 | verdict=free，不与变体产物混装 |

**谁来裁决**：脚本出建议（菜单 JSON `suggest` 字段：alpha 完好度/色键可分离性/质地判型），
agent 结合需求词是否落在轴上判断，**用户拍板**。方向二花真金白银——用户显式选择才走
（`--free` 或明确说"自由生成"），agent 可推荐但不可替用户决定。

> 本 skill 是「印花模块」的板块4，容器是 `Desktop\印花模块\pattern-workshop\`。
> 上游：板块1 的 `01_extracted/motif_XX.png`、板块2 的 `02_hd/`、板块3 的 `03_restyle/`
> 产物直接喂入，工作区自动续接。设计文档 04-图案变体.md 在「印花模块」根目录。

## 第一步：冷启动（必做）

```bash
python scripts/check_env.py
```

stdout 一行 JSON。**方向一纯代码轴永远在线（零成本）**；`route` 只决定方向二/满幅排列档：
`gpt`（终点引擎）｜`qwen`（对照档）｜`manual`（提示词卡）。无 VLM（无 DASHSCOPE_API_KEY）
时资格门降离线档（无法验稿要警告）、`--custom` 不可用。key 全走环境变量，永不写死。

## 主通道（菜单优先，不默认生成）

```bash
python scripts/variants.py <输入图>                    # 默认=资格门+方向建议+菜单（零生成零成本）
python scripts/variants.py <图> --layout toss --rotation two-way --density sparse   # 点单式
python scripts/variants.py <图> --preset ditsy          # 行业方案一键（5 选）
python scripts/variants.py <图> --custom "疏一点的四向散点，整体偏蓝" --dry-run   # 一句话→参数，先看后跑
python scripts/variants.py <图> --sample auto --seed 7 --count 8    # 系列采样（显式点名）
python scripts/variants.py <图> --free "保留笔触，整张转黄昏暖调"     # 方向二（按张计费）
python scripts/variants.py <图> --free "..." --engine manual         # 方向二手动档（提示词卡）
python scripts/variants.py <图> --collect               # 回接手动档自由模式产物
```

五轴（每轴都有选项和一句话两种表达）：排列 `--layout`（straight/half-drop/brick/mirror/
**toss 散点**——丰富度主增量，Poisson 布点+±20° 或 360° 角度抖动+缩放抖动，不重叠有保证）；
方向性 `--rotation`（toss 专属）；密度 `--density`（sparse 25%/medium 50%/dense 75%/packed 90%
或 0.15-0.95）；比例 `--scale`（ditsy/medium/large/placement 独幅 或数值）；配色 `--color`
（hue<N> 任意度数/invert/mono/duotone/vintage/bright/gray——**指定色板重上色是板块6 的职责**）；
外加 `--tilt` 斜排、`--bg` 底色（white/transparent/#hex）、`--frame` 画幅（square/2:3/3:2/width145）。
未指定的轴用推荐默认值（写进 meta），**用户没下单永不生成**。

- **preset**：ditsy 清新碎花 / vintage-allover 复古满铺 / placement 独幅主花 / street-packed
  高街满铺 / airy 素雅留白
- **可复现**：同 seed 同参数=像素一致（tests 保证）；参数写进文件名
  （`<stem>_<布局>_s<比例>_d<密度>_<色>_v{n}.png`）和 meta.json，任意一张可复跑
- **--sample auto**：显式点名的系列采样——未指定轴逐张随机、指定轴钉死；网格布局无随机性，
  要系列差异用 toss 或 --sample auto（脚本会警告）
- **满幅稿**：换色像素级可做；比例=中心裁切变放粗档；**排列轴走生成式重排档**（唯一花钱
  路径，回炉≤2 每次 VLM 本体核对，超次如实标注）——stdout 会先声明
- **量化校验**（数字说话不靠肉眼）：覆盖率 vs 目标 ±25% 带、toss 最近邻 vs 最小距（防重叠）、
  连通域计数；越界如实在 warnings 里报（纯代码档无回炉概念，参数即事实）
- 不支持目录批量（方向二按张计费易烧钱；方向一系列用 --sample auto --count N）

## Agent 行为约定（硬规则）

1. **模糊指令先回菜单**：用户说"来点变体"→ 跑不带参数的 variants.py，把菜单 JSON 转成
   可读清单给用户挑（逐轴选项 / preset / 一句话 / --free 四种下单方式），**不擅自生成**
2. **方向二必须用户显式选择**：需求词落在轴上→方向一；用户的话轴装不下（改本体/氛围类/
   复杂稿）→ 推荐方向二并等确认；判型 photo/复杂肌理 → 主动提示方向二更合适
3. **--custom 解析失败**：改逐轴选项问用户，或建议 --free 直说；解析出的越界部分
   （改图案本体）如实转告"这属于改图案"
4. **非花稿拒收**：资格门 no → 指路板块1（pattern-extraction）；partial → 警告后继续
5. 生成前转述将执行的参数组合（或用 --dry-run）；方向二按张计费，先报价再跑

## 输出契约

stdout 一行 JSON：`{ok, files, warnings, next_action, direction, carrier, suggest, seed,
items[{mode,file,measures,plan|prompt,verdict}], seconds}`。产物落
`<ws>/04_variants/`（+ `contact_sheet.jpg` 联络表：方向二产物单独 FREE 分区橙色标注
"未做一致性校验"）+ `meta.json`（全参数/提示词原文/verdict 全记）。stderr 进度日志。

## 边界（不做什么）

- 不改图案本体（加删元素/换画法=板块3 风格延展；指定色板重上色=板块6）
- 不做无缝循环（四方连续可生产化=板块5；本板块 toss 间隙按不重叠布点，不保证平铺连续）
- 不做开放设计提案（轴封闭；自由创作走 --free 用户自己写提示词）
- 生成式重排仅兜满幅稿排列轴，不承担大改变体（那是 --free 的活）
