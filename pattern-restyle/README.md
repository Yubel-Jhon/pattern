# 🎨 风格延展 · Pattern Restyle Skill

一稿多款：同一张花稿，换画法重画出一系列风格版本。印花模块板块3，也是模块里**唯一以 GPT 为主引擎**的板块。

`命令行 skill · 任意 Agent 可跑` `识别驱动：延展方向由 VLM 看图现场提案` `三路线：GPT 终点 / qwen 对照 / ChatGPT 手动档` `宪法：必须改风格，但不能换内容` `v2.1 · 2026-09`

**👉 使用方式**：把本文件夹放进 `~/.claude/skills/`，让任意 Agent（Claude Code / Codex 等）执行下方命令。上游是板块1 图案提取 / 板块2 高清修复的花稿产物，直接喂入自动续接工作区。架构全景见 [`../pattern-restyle-arch.png`](../pattern-restyle-arch.png)。

---

## 1. 这是什么 / 不是什么

> **✓ 它是**：一条**识别驱动**的扇出-回炉流水线。入口 VLM 看图一次性产出四样东西——判型卡、逐元素清单（带数量颜色）、**原稿风格画像**（现在是什么风格）、**5-6 个延展方向提案**（VLM 自己框定，离原稿足够远、可商用、维度错开）。每条方向独立重画、独立质检、独立回炉≤2，最后汇成联络表。

> **✗ 它不是**：换色工具 / 无缝循环工具。宪法一条——**必须改风格，但不能换内容**：元素不丢不增、构图不变，风格画像→目标方向的强关联写进每条 prompt。主动换色是板块6 配色重组的职责，四方连续是板块5 的职责，本 skill 只指路不越界。

**为什么方向不预设**：板块1/2 的目标是机械的（提取/清晰），识别答「图里有什么」就够；板块3 的目标是风格，识别必须先答「它现在是什么风格、离它远的方向有哪些」。这个范围让识图模型自己框——每张图的联络表都不一样。内置 6 预设方向（watercolor/geometric/ethnic/vintage/minimal/handdrawn）降级为点名快捷方式（`--styles watercolor,...`），点名时还会做同族检查并警告。

## 2. 使用场景

| 场景 | 说明 |
|---|---|
| 👕 **一稿多款 / 系列延展** | 一张主花稿延展出水彩、几何、民族、版画等多风格版本，供系列企划 |
| 🛍 **竞品风格库** | 把竞品花稿批量转成不同画法，研究同一纹样的风格弹性 |
| ⚡ **提案快反** | `--quality draft` 先出低成本方向联络表，挑中方向再 `final` 出成品，不一步到位烧钱 |
| 🖨 **无 key 环境** | 手动档出整段英文提示词卡，粘贴到 ChatGPT/GPT-Image 网页人工生图，`--collect` 回接质检打包 |

## 3. 快速上手

```bash
# ① 冷启动：探测引擎路线（gpt / qwen / manual）
python scripts/check_env.py

# ② 探索档：VLM 现场提案方向，出联络表（默认 --styles auto）
python scripts/restyle.py 花稿.png

# ③ 收敛档：挑中方向出成品（高质量 ×3）
python scripts/restyle.py 花稿.png --styles midcentury_modern --quality final --count 3

# 手动档：出提示词卡 → ChatGPT 生图 → 结果存回后回接
python scripts/restyle.py 花稿.png --engine manual
python scripts/restyle.py 花稿.png --collect
```

| 依赖 | 有 → | 没有 → |
|---|---|---|
| `OPENAI_API_KEY`（+可选 `OPENAI_BASE_URL` 中转） | GPT 档全程自动：透明底原生输出、`--ref` 多参考图 | 走下面两行 |
| `DASHSCOPE_API_KEY` | qwen 对照档（省钱探索，跑版风险靠 VLM 质检兜底） | 手动档（零成本，效果上限=ChatGPT 网页版） |
| `pip install pillow numpy scipy scikit-image` | 全部校验 | 部分校验降级 |

key 全走环境变量，代码零写死。眼睛（识别/质检）永远走便宜的 qwen-vl，GPT 只花在生图刀刃上。

## 4. 两段式花钱（默认即探索档）

| 阶段 | 生成次数 | 档位 | 用途 |
|---|---|---|---|
| 探索 | 5-6 方向 ×1 | draft | 联络表挑方向 |
| 收敛 | 1-2 方向 ×2-3 | final | 成品 |

单方向回炉≤2 计入成本；**不做目录批量**——按张计费，批量易烧钱，外层循环自管。

## 5. 输出产物结构

```
03_restyle/
├─ <stem>_<风格>[_vN].png     # motif=透明底 RGBA，allover=满幅 RGB
├─ contact_sheet.jpg          # 原稿列+各风格列，OK/FAIL 标色，一眼判断
├─ _prompt_cards.txt          # 手动档提示词卡（含原稿风格画像）
└─ meta.json                  # 逐张 prompt / verdict / attempts / palette_dist 全记档
```

## 6. 已知边界（诚实声明）

- **无像素档兜底**（与板块2 最大差异）：风格化重画没有"原样抠取"式的保真路径，质检不过 = 如实标注 verdict 交付并指路手动档，绝不冒充
- qwen 对照档可能出现「水彩上叠几何线」式半吊子重绘（编辑原图的模型心智），提示词只能压不能根治——对照档定位是省钱探索，成品走 GPT
- GPT 档依赖 key/中转接入；未接入时手动档是完整可用的第一交付形态
- allover 四边连续性超阈只报不修，指向板块5
