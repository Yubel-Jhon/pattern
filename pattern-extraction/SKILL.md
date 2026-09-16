---
name: pattern-extraction
description: 图案提取（纹样提取）。从服装图/面料图/网感图中提取印花纹样成独立花稿：AI 档=VLM 定位+生图重绘，含去底出透明 PNG；无 key 有提示词卡手动档与离线保真档。印花模块板块1。触发词：印花提取、图案提取、纹样提取、花型回样、去底色、透明底花稿、服装印花、pattern extraction。
---

# Pattern Extraction · 图案提取（印花模块 板块1）

把任意服装/面料图里的印花纹样变成可用的设计资产：独立图案稿（透明底）、满幅花型回样、
透明花型件。核心是「提示词×参考图的任务编排 + 轻脚本」，**模型无关**——qwen 全自动脚本
内置，ChatGPT/GPT-Image/Banana 手动档给提示词卡，离线有保真档兜底。

> 本 skill 是「印花模块」的板块1。项目容器是 `pattern-workshop/`（本文件夹的上级），
> 板块2-6（高清修复/风格延展/图案变体/无缝循环/配色重组）落地后各自成为平级的 skill 文件夹。
> 设计文档 00~07 在「印花模块」根目录。

## 第一步：冷启动（必做）

```bash
python scripts/check_env.py
```

stdout 一行 JSON：`{ok, route, capabilities, next_action}`。退出码 0=全自动，2=降级可用，3=需补依赖。
按 `route` 走对应路线，**永不硬失败**：

| route | 条件 | 走法 |
|---|---|---|
| `gen` | 有 DASHSCOPE_API_KEY | 全自动脚本（主通道） |
| `pixel` | 无 key 但装了 scikit-image/imagehash | 保真档离线跑（色键+连通域，效果有上限） |
| 手动档 | 有 ChatGPT/GPT 等任意生图工具 | 用 §手动档 提示词卡人工生图，产物丢回工作区由脚本打包 |

key 永远走环境变量 `DASHSCOPE_API_KEY`，不写死、不落盘。

## 主通道：图案提取（已实测）

```bash
python scripts/ai_extract.py <图片路径> [--bg auto] [--max-motifs 12] [--out pattern_output]
python scripts/ai_extract.py --batch <目录>          # 批量跑图池，写 _run_log_ai.json
```

参数：
- `--bg auto|remove|keep` 去底色。auto=按纹样判型（见下）；remove=强制去底出透明 PNG；keep=不去
- `--max-motifs` 单图提取上限（默认 12）
- `--name` 工作区名（默认 ai_<文件名>）
- `--out` 输出目录（默认 CWD 下 pattern_output；建议显式指定到工作区，别写进 skill 文件夹里）

流程（全自动，单图 4-40s）：qwen3-vl-plus 看图**先判图片类型**再定位印花
（0-1000 归一化 bbox + 类型/颜色/纹样风格 JSON）→ 按类型路由裁剪 →
qwen-image-3.0-pro i2i 重绘 → 可选边缘连通色键去底 → 工作区落盘。

### 分诊路由（模型行为已实测钉死）

| VLM 判型 | 路线 | 产物 |
|---|---|---|
| `white-product` 白底产品图 | 满印→中心面料小块；单花→bbox 裁剪 | 满幅回样 / 独立稿 |
| `scene` 商拍图 / `lifestyle` 网感图 | 同上 + 皮肤审核兜底（收紧重裁自动重试） | 同上 |
| `fabric-flat` 面料平铺图 | 整张全幅直出不裁 | 满幅回样 |
| 印花 `repeat=allover` | 中心面料小块（内收25%）+ 满幅回样提示词 | 满幅回样 |
| 印花 `style=motif` | bbox + 剥离提示词；去底 | 透明 PNG 独立稿 |
| 印花 `style=continuous`（扎染/肌理/满铺一体） | 不去底 | 满幅回样 |

照片上叠加的水印/logo 不当印花（定位提示词已排除）。

### 输出契约

stdout 一行 JSON：`{ok, files, warnings, next_action, seconds, motifs:[{id,name,bbox,size,layer,bg}]}`
工作区：`<out>/<name>/00_raw → 01_extracted/{motif_XX.png, preview_annotated.jpg, preview_contact.jpg, meta.json}`
`motif_XX.png`：去底=RGBA 透明底；不去=白底。meta 记录 image_type/bg_mode/逐件 bg。

## 手动档提示词卡（ChatGPT / GPT-Image / Banana 通用）

要满幅花型回样（整片花布、满印类）：
```
输入是服装面料局部照片。把这块布料上的印花纹样原样复刻为满幅花型回样：
纹样延续铺满整个画面、直达四边，四角不能留白，不要圆形或方形边框；
严格保持纹样的造型、颜色、比例与细节；只要印花本身——
不要衣服、衣领、纽扣、门襟、人体、背景、阴影。
```
要独立图案稿（胸前大图案/贴布/独立文字）：
```
输入是一件衣服的照片。把衣服上这个印花图案完整剥离出来，重绘为独立图案：
纯白背景、图案完整居中；严格保持图案的造型、线条、颜色、比例与细节，不做风格化改动；
不要输出衣服、人体、背景、阴影，也不要保留布料底色。
```
产物命名丢回工作区 `01_extracted/` 即可进后续校验/打包流程。

## 跨模型适配

- **qwen 自动档**（内置）：补短板的工程已做——裁剪喂得准（qwen i2i 是"编辑原图"心智，
  输入大构图会被保留）、`watermark=False`、data URI 前缀、直连绕系统代理、
  绿网审核（DataInspectionFailed=裁剪带皮肤）自动收紧重试、4xx 透传状态码+响应体
- **GPT/ChatGPT 手动档**：理解力强，提示词卡直接用，无需否定清单堆叠
- 换生图模型：只改 `scripts/ai_extract.py` 的 `_post`/`i2i_redraw`，分诊与提示词不变

## 已知边界（详见设计文档 01-图案提取.md §9，在印花模块根目录）

- worn 服装褶皱光影会带进纹样（治法三招在 §9.1，提示词红利未尽前不碰微调）
- 复杂密集图形 bbox 粗、小件可能漏检（§9.2：refine/提分辨率/--box 兜底待接）
- 文字印花重绘字体走样（v2：文字件跳过生图直接抠原字）
- 满幅回样 ≠ 无缝循环单元——无缝是板块5的职责，别在提取端越界
- **标注微调现阶段不做**；重启条件=某类图提示词治不好，攒 50-100 张标注对评估 LoRA

## 与后续板块的衔接（联动点）

| 板块 | 消费本板块的什么 | 状态 |
|---|---|---|
| 2 高清修复 | 回样/花稿（1024）→ 超分到输出所需分辨率 | 设计文档就绪（video2x 便携路线） |
| 3 风格延展 | 独立稿/回样作参考图 → i2i 换风格 | 设计文档就绪（qwen i2i） |
| 4 图案变体 | 透明 PNG 花型件 → 换色/换形/重排 | 设计文档就绪 |
| 5 无缝循环 | 满幅回样 → repeat 单元 + 四方连续 | 设计文档就绪（布点 repeat + img2texture） |
| 6 配色重组 | RGBA 花型件 → KMeans 分层换色 | 设计文档就绪 |
| 反馈层 | 回炉≤2 次，优化后重新分诊，meta 记 lineage | 设计文档就绪 |

## 依赖

```bash
pip install -r requirements.txt        # 核心：pillow numpy scipy scikit-image imagehash
# 可选：ultralytics（FastSAM box 分割）、rembg[cpu]（保真档辅助）——首次运行自动下载模型
```
无任何 SDK 依赖（API 走 urllib）；FastSAM-s.pt 22.7MB 首次运行自动下载（已验证直连可用）。
