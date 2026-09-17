# 🔁 无缝循环 · Pattern Seamless Skill

把花稿变成能交给印花厂的**四方连续 tile**：花型件排布成无缝单元、满幅重复稿裁出 repeat、
满幅不重复稿修缝成无缝。印花模块板块5。

`命令行 skill · 任意 Agent 可跑` `菜单优先：用户没下单永不生成` `三路分岔：排 / 切 / 修` `宪法：无缝要可证明，证明不了的亮明身份` `v1 · 2026-09`

**👉 使用方式**：把本文件夹放进 `~/.claude/skills/`，让任意 Agent（Claude Code / Codex 等）
执行下方命令。上游是板块1 图案提取（[pattern-extraction](../pattern-extraction/README.md)）、
板块2 高清修复（[pattern-enhance](../pattern-enhance/README.md)）、板块3 风格延展
（[pattern-restyle](../pattern-restyle/README.md)）、板块4 图案变体
（[pattern-variants](../pattern-variants/README.md)）——产物直接喂进来，工作区自动续接。

---

## 这是什么 / 不是什么

> **✓ 它是**：可生产级无缝 tile 流水线。**无缝必须是构造出来、可证明的**——路A 用环面布点
> +wrap 绘制把无缝做进数学里（tile 铺 3×3 中心裁切与原 tile **像素相等**，tests 保证）；路B
> 用 FFT 自相关找周期裁 repeat 单元（置信度 <0.35 不硬裁）；路C 修缝分三档先便宜后贵
> （blend 纯代码免费 → 生成式花钱显式选择 → 手动提示词卡）。统一出口=**边缘跳变比 ≤1.2**
> （接缝跳变÷内部基线，数字说话不靠肉眼）+ **3×3 白底铺贴预览**（预览即产物，接缝一眼可判）。
> 证明不了 seamless 的产物亮明 verdict（period-crop 带置信度、seam-fixed 带校验数字），不混装。

> **✗ 它不是**：图案本体改造器（加删元素/换画法=板块3）；单页变体工具（换排列出系列稿=板块4，
> 且别拿本板块 tile 去板块4 重排——贴边裁切会破坏无缝）；配色工具（指定色板重上色=板块6，
> 本板块的 RGBA tile 是它的理想输入，逐像素换色天然保周期）。

## 三路分岔

| | 路A · 排 | 路B · 切 | 路C · 修 |
|---|---|---|---|
| 适合的图 | 独立花型件 | 满幅**重复**稿 | 满幅**不重复**稿 |
| 干什么 | 环面布点→wrap 绘制→tile | 自相关找周期→裁 repeat | blend/生成式/手动卡修缝 |
| 成本 | 免费 | 免费 | blend 免费；生成式计费 |
| verdict | constructive | period-crop | seam-fixed |

## 快速上手

```bash
python scripts/check_env.py                                  # 冷启动：路A/B/blend 永远在线零成本
python scripts/seamless.py <图>                              # 菜单（资格门+三路建议，零生成）
python scripts/seamless.py <花型件> --mode tile --preset ditsy          # 路A 行业方案一键
python scripts/seamless.py <满幅稿> --mode crop --period 512x768        # 路B 周期裁切
python scripts/seamless.py <满幅图> --mode seam --seam-fix blend        # 路C blend 兜底（免费）
python scripts/seamless.py <满幅图> --mode seam --seam-fix engine       # 路C 生成式（显式选择，计费）
python scripts/seamless.py --collect <修好的图>                          # 手动档回接（自动 roll 回+校验）
```

## 产物结构

```
<工作区>/05_seamless/
  <stem>_<layout>_t<tile>_v{n}.png     # 路A：RGBA 透明底 tile（参数进文件名，可复跑）
  <stem>_period_<WxH>.png              # 路B：repeat 单元（+_blendfix.png 超阈自动附）
  <stem>_seamfix_<method>.png          # 路C：修缝产物
  *_preview3x3.jpg                     # 白底 3×3 铺贴预览（预览即产物）
  meta.json                            # verdict/全参数/校验数字/lineage 台账
```

## 已知边界（诚实声明）

- **密度=相对间距**：花型间距相对花型尺寸（sparse 1.5×/medium 1.15×/dense 0.95×/packed 0.8×），
  绝对覆盖率是防叠下限×着墨率的派生量——只报告，不设硬目标（toss 物理上限摆在那）
- **blend 修缝**周期可证明，但接缝带可能涂抹/鬼影——看 3×3 预览确认；生成式观感好但周期靠
  校验数字背书，回炉 ≤2 超次如实标注
- **tile 放大回板块2 但须声明 tile 来源**：直过 ESRGAN 边缘局部上下文不同，放大完可能不无缝
  （wrap 预处理工具下期加）
- `--mix` 多件混排 / `--custom` 一句话 / `--sample auto` 系列 = 下一期
- 找不到可靠周期 ≠ 失败：如实报 + 荐路C；置信度 <0.35 不硬裁

## FAQ

**Q：怎么证明真的无缝？** 路A 是构造性的（布点在环面上、绘制向 8 邻接偏移延续，tests 保证
3×3 拼贴中心裁切像素相等）；路B 带自相关置信度数字；路C 带边缘跳变比——三种都另有 3×3
预览供肉眼终审。证不出来的一律不冒充 seamless。

**Q：同 seed 复跑为什么像素一致？** 全链 numpy 随机走显式 `np.random.default_rng(seed)`，
参数写进文件名与 meta——客户三个月后要「上次那张第 2 版」可精确复现。

**Q：没配任何 key 能用吗？** 能。路A/路B/blend 全是纯代码；VLM 判型降离线档（会警告）；
路C 生成式自动降手动档（出 roll 图+提示词卡，修完 --collect 回接）。
