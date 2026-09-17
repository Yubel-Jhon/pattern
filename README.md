# 🌸 Pattern Workshop · 印花模块

服装印花全流程的 Agent skill 集：从任意服装图/面料图/网感图提取印花纹样，到高清化、风格延展的完整工具链。每个板块是**独立分包**（可单独放进 `~/.claude/skills/` 使用），一稿沿工作区自动续接流转。

| 模块 | 文件夹 | 一句话 | 主引擎 |
|---|---|---|---|
| ① 图案提取 | [`pattern-extraction/`](./pattern-extraction/) | 服装图 → 独立花稿（透明底）/ 满幅回样 + 纹样台账 | 生图重绘 + 像素级保真档 |
| ② 高清修复 | [`pattern-enhance/`](./pattern-enhance/) | 糊图 → 4096² 可用大图，宪法「永不改设计」 | 生图 + Realesrgan Vulkan |
| ③ 风格延展 | [`pattern-restyle/`](./pattern-restyle/) | 一稿多款：换画法重画系列风格版本，宪法「必须改风格但不能换内容」 | GPT-Image（识别驱动） |
| ④ 图案变体 | [`pattern-variants/`](./pattern-variants/) | 一稿多款系列花稿：换排列/密度/比例/配色（toss 散点=Poisson 布点），宪法「只改排布配色不改本体」 | 纯代码五轴 + 生成式重排档 |
| ⑤ 无缝循环 | [`pattern-seamless/`](./pattern-seamless/) | 四方连续可生产 tile：花型件排布 / 周期裁切 / 修缝三路，宪法「无缝要可证明，证明不了的亮明身份」 | 三路分岔：A排（构造性）/ B切（FFT 周期）/ C修（blend→生成式） |

- ⑥ 配色重组：开发中
- 各模块架构图：`pattern-*-arch.png`（仓库根目录）
- 上游产物直接喂下游（如 `01_extracted/motif_XX.png` → 板块2/3），工作区按 `00_raw → 01_extracted → 02_hd → 03_restyle → …` 自动续接
