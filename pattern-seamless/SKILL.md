---
name: pattern-seamless
description: 无缝循环（四方连续可生产 tile）。宪法=无缝要可证明，证明不了的亮明身份（verdict: constructive/period-crop/seam-fixed 不混装）。三路分岔：路A 排=花型件→wrap 环绕布点（环面 Poisson/网格 + 9 偏移越界延续绘制，构造性无缝可证明：tile 铺 3×3 中心裁切像素相等，免费）；路B 切=满幅重复稿 FFT 自相关找周期裁 repeat 单元（置信度 <0.35 不硬裁改荐路C；跳变超 1.2 自动附 blend 修缝版，两版都交付，免费）；路C 修=满幅不重复稿 blend 数学修缝（免费兜底、周期可证、接缝带可能涂抹）或生成式修缝（roll 半格缝移中央→GPT/qwen i2i→跳变+VLM 本体双校验回炉≤2，花钱必须用户显式选择）或手动档提示词卡+--collect 回接校验。统一出口=边缘跳变比 ≤1.2 + preview_3x3 白底铺贴（预览即产物）。菜单优先不默认生成；资格门拦非花稿指路板块1；密度=相对间距如实报告（不设绝对覆盖率硬目标）。印花模块板块5。触发词：无缝、四方连续、repeat、平铺、循环单元、修缝、无缝贴图、seamless、tile、重复单元。
---

# Pattern Seamless · 无缝循环（印花模块 板块5）

把花稿变成能交给印花厂的**四方连续 tile**。宪法一条——**无缝必须是构造出来的、可证明的，
不是「看起来无缝」；证明不了的亮明身份**。verdict 三档（constructive / period-crop /
seam-fixed）全程携带不混装：接缝错一毫米是生产事故，所以本板块是全模块最固化的一块——
自由度只开布点表达（菜单）与修缝（显式选择）两个口。

**三路分岔（顶层结构）**：

| | 路A · 排 | 路B · 切 | 路C · 修 |
|---|---|---|---|
| 适合的图 | 独立花型件（透明底最佳，白底自动色键） | 满幅**重复**稿（花布照片/回样） | 满幅**不重复**稿（肌理/扎染/照片级） |
| 干什么 | 环面布点 → wrap 绘制 → 无缝 tile | FFT 自相关找周期 → 裁 repeat 单元 | 修边成无缝（blend / 生成式 / 手动卡） |
| 引擎 | 纯代码（构造性） | 纯代码（numpy FFT） | blend 纯代码兜底 + 生成式花钱档 |
| 成本 | 免费 | 免费 | blend 免费；生成式按张计费 |
| verdict | constructive | period-crop | seam-fixed |

> 本 skill 是「印花模块」的板块5，容器是 `Desktop\印花模块\pattern-workshop\`。
> 上游：板块1 `01_extracted/`、板块2 `02_hd/`、板块3 `03_restyle/`、板块4 `04_variants/`
> 产物直接喂入，工作区自动续接。设计文档 05-无缝循环.md 在「印花模块」根目录。

## 第一步：冷启动（必做）

```bash
python scripts/check_env.py
```

stdout 一行 JSON。**路A 排、路B 切、路C blend 修缝=纯代码永远在线，零成本、不依赖 GPU/key**；
`route` 只决定路C 生成式档位：`gpt`（终点引擎）｜`qwen`（对照档）｜`manual`（提示词卡）。
无 VLM 时资格门降离线档（要警告）。key 全走环境变量，永不写死。

## 主通道（菜单优先，不默认生成）

```bash
python scripts/seamless.py <输入图>                        # 默认=资格门+三路建议+菜单（零生成零成本）
python scripts/seamless.py <图> --mode tile --preset ditsy  # 路A 行业方案一键（5 选）
python scripts/seamless.py <图> --mode tile --layout toss --rotation two-way --density sparse
python scripts/seamless.py <满幅稿> --mode crop             # 路B：自动找周期
python scripts/seamless.py <满幅稿> --mode crop --period 512x768   # 人工指定周期（跳过检测）
python scripts/seamless.py <满幅图> --mode seam --seam-fix blend    # 路C 免费兜底（先看效果）
python scripts/seamless.py <满幅图> --mode seam --seam-fix engine   # 路C 生成式（按张计费，显式选择）
python scripts/seamless.py <满幅图> --mode seam --seam-fix manual   # 路C 提示词卡（无 key / 自己跑）
python scripts/seamless.py --collect <修好的图>              # 回接手动档（原图可省，自动定位工作区）
```

**路A 轴**：`--layout`（straight/half-drop/brick/mirror/**toss 散点**）× `--rotation`
（toss 专属）× `--density`（sparse/medium/dense/packed——**密度=相对间距**：布点间距相对
花型尺寸；绝对覆盖率是防叠下限×着墨率的派生量，只报告不设硬目标）× `--scale` × `--bg`
（默认 transparent——RGBA tile 是板块6 的理想输入）× `--tile 1024` × `--dpi 150`（meta 记
`repeat_cm` 物理循环尺寸）× `--seed 42`（同 seed 同参数=像素级可复现）。preset：ditsy /
vintage-allover / street-packed / geo-grid / airy。

**路B 规则**：置信度 <0.35 不硬裁——如实报 warning 并建议路C；`--period` 人工指定跳过检测。
裁出的 tile 跳变 >1.2（源图近似周期而非严格周期）→ **自动附 blend 修缝版，两版都交付供选**。

**路C 规则（先便宜后贵）**：blend=roll 偏移搜索+三角凹口渐变混边，周期可证明，但接缝带可能
涂抹/鬼影（看预览确认）；生成式=roll 半格把缝移到画面中央 → i2i 修中央十字 → 跳变+VLM 本体
双校验 → 回炉 ≤2（每次加锁定条款）；手动档=出 `_rolled_for_fix.png`（缝在中央）+提示词卡，
修完 `--collect` 回接（自动 roll 回+跳变校验）。生成式无 key 自动降手动档，不报错。

**构造性证明（路A 的底气）**：布点在环面上（边界=绕过去），绘制时每个 motif 向 8 个邻接
偏移复制（越界 wrap 到对侧）——贴边 motif 完整延续不裁切。tests 保证：tile 铺 3×3 后中心
裁切与原 tile **像素相等**；同 seed 复跑像素一致。

## Agent 行为约定（硬规则）

1. **模糊指令先回菜单**：用户说"做个无缝"→ 跑不带参数的 seamless.py，把菜单 JSON+三路建议
   转成可读清单给用户挑，**不擅自生成**
2. **路C 生成式必须用户显式选择**：先推荐免费 blend，观感不够再上生成式，生成前先报价；
   agent 可推荐不可替选
3. **非花稿拒收**：资格门 no → 指路板块1（pattern-extraction）；partial → 警告后继续
4. **tile 观感调整回本板块改参数重跑**——别拿 tile 去板块4 重排（板块4 render 贴边裁切，
   会破坏无缝）；tile 放大回板块2 但必须声明 tile 来源（直过 ESRGAN 边缘会裂，需 wrap 预处理）
5. 生成前转述将执行的参数组合；不支持目录批量（生成式按张计费易烧钱）
6. **verdict 如实转告**：seam-fixed 必须提醒用户看 preview_3x3 接缝带（blend 涂抹/生成式跑版
   都靠肉眼终审，数字只是门槛）

## 输出契约

stdout 一行 JSON：`{ok, files, warnings, next_action, route, mode, verdict, checks
{edge_jump_ratio, period, confidence, coverage, nn_min_dist, ...}, params, workspace,
card_model, menu(菜单模式)}`；exit 0/1。产物落 `<ws>/05_seamless/`：

- 路A：`<stem>_<layout>_t<tile>_v{n}.png`（RGBA 透明底）
- 路B：`<stem>_period_<WxH>.png`（+超阈自动附 `_blendfix.png`）
- 路C：`<stem>_seamfix_<blend|mirror|engine|manual>.png`；手动档另有
  `_rolled_for_fix.png` + `_seamfix_prompt_card.txt`
- 公共：`*_preview3x3.jpg`（白底 3×3 铺贴，**预览即产物**）+ `meta.json`
  （`record_step("seamless")` 记 lineage/verdict/全参数/校验数字）

stderr 进度日志；永不硬失败（无 VLM 离线判型卡兜底、无 GPU 无影响——本板块不依赖算力）。

## 边界（不做什么）

- 不改图案本体（加删元素/换画法=板块3；指定色板重上色=板块6——RGBA tile 逐像素换色天然保周期）
- 不做单页变体（换排列出系列稿=板块4；两板块边界互认：它管单页、本板块管可生产 tile）
- `--mix` 多件混排（主花+小叶+点子）、`--custom` 一句话解析、`--sample auto` 系列采样=
  下一期（本期已拍板后置）
- 不承担 tile 高清化（回板块2，须声明 wrap 预处理——见上）

## 原型环节

五轴语义或 preset 调整后，可调 `ui-prototype` skill 刷新印花工坊界面原型
（`Desktop\印花模块\pattern-workshop\原型-印花工坊.html`）里「无缝循环」工序屏
（3×3 实时铺贴预览：布局/密度/比例）。
