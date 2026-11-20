# pdf2md：PDF → 标准 Markdown（截图 OCR / 文字层 / 结构化抽取 / 页级 OCR）

[![release](https://github.com/libai4hao/pdf2md/actions/workflows/release.yml/badge.svg)](https://github.com/libai4hao/pdf2md/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Changelog](https://img.shields.io/badge/changelog-v0.1.0-informational)](CHANGELOG.md)

`pdf2md` 把中文 PDF 转成干净的 Markdown。核心路径只用 Python 标准库：截图型 PDF 走 WinRT OCR、文字层 PDF 走外部 `pdftotext`；可选后端补上 `pymupdf4llm` 结构化抽取（标题/表格/配图）与 PaddleOCR-json 页级 OCR，全程带三态质量门禁。

把一批 11 份 PDF 转 Markdown 的**一次性处理**，沉淀成可复跑的脚本与可核对的经验。内置路径（截图 OCR + pdftotext）仍然是 Python 侧零第三方依赖，只用标准库；2026-11-21 起把上游知识库管线的 PDF→MD 能力（pymupdf4llm 结构化抽取、PaddleOCR-json 页级 OCR、三态质量门禁、OCR 侧车与合并）合并进来，作为**可选后端**（见第七节）。

踩坑记录：[PITFALLS.md](PITFALLS.md)

---

## 一、这个工具解决什么问题

`html2md` 只能吃 HTML。这批 PDF 一上手就撞墙：

- **10/11 是无文字层的整页长截图**。`Producer: Skia/PDF m66`（Chrome 打印），每份单页、单张 `/Subtype /Image`、`FlateDecode` + `DeviceRGB` + 8bit、宽恒 540 px、高 5313–20000 px，`/Font` 计数为 0。
  pypdf 的 `extract_text()` 对这 10 份**全部返回 0 字符**；poppler 的 `pdftotext` 也只吐 1 个换行。不是工具不行，是文件里真的没有文字。
- 唯一有文字层的是 样本 11（文字层，公开政策文件）（21 页、`/Type0` + `/CIDFontType2` + `Identity-H`、`/ToUnicode` 2 个）。

这个工具按 PDF 形态走三条路径（前两条主路径，第三条是文字层抽空时的兜底）：

1. **截图型**：标准库 `zlib` 解出那张长图的裸 RGB 像素 → 手写 PNG 封装 → 按高度切片（`MaxImageDimension` 只有 10000 px）→ 交给 Windows 自带的 WinRT OCR → 中文清洗 + 启发式合段；
2. **文字层**：调外部 `pdftotext`（poppler）。探不到就**明确报错退出 1**，不静默产出空 MD。
3. **伪文字层兜底**：若判为文字层却抽出 0 字符（`/Font` 来自水印/表单层、每页实为整张图），自动降级到「`pdftoppm` 渲染每页 → 复用 WinRT OCR」，而不是产出空 MD（见 [PITFALLS](PITFALLS.md) 坑 18）。

> 这不是通用 PDF 转换器。截图路径假设「单页 = 一张整页长图 + DeviceRGB + 8bit + FlateDecode」，别的组合宁可拒绝也不猜；文字层路径依赖外部 exe，不把 poppler 打进仓库。

---

## 二、用法

环境：Python 3.11（`py`），内置路径无 `pip install`（见 [requirements.txt](requirements.txt)）。各形态所需外部能力：截图型与伪文字层兜底都要 Windows PowerShell 5.1 + 中文 OCR 语言包；文字层要 poppler 的 `pdftotext`；伪文字层兜底还要 `pdftoppm`（一般随 `pdftotext` 一起装）。可选后端另需 `pymupdf` / `pymupdf4llm`（结构化）、`PaddleOCR-json` 引擎（页级 OCR），见第七节。

> **解释器要钉住**：本仓库脚本首行是 `#!/usr/bin/env python`，Windows 的 `py` 启动器会读 shebang 选解释器。如果 PATH 上的 `python` 不是装了 `pymupdf4llm` 的那个，`py convert.py --check` 会显示「结构化抽取：无」——用 `py -3.11 convert.py ...` 或 `py -m convert ...` 钉版本（见 [PITFALLS 坑 19](PITFALLS.md)）。

```pwsh
cd pdf2md

# 1) 只读预检（强烈建议第一步）：形态、原图尺寸、切片数、OCR/ pdftotext / 结构化 / 引擎可用性、门禁、残留，一字节不写
py -3.11 convert.py "<语料目录>\只加两行代码，为什么要用两天？.pdf" --check

# 2) dry-run 预览：把生成的 Markdown 打印到 stdout，仍不写盘
py -3.11 convert.py "<语料目录>\只加两行代码，为什么要用两天？.pdf"

# 3) 真正写出：生成同名 .md；若目标已存在，先备份为 .md.bak
py -3.11 convert.py "<语料目录>\只加两行代码，为什么要用两天？.pdf" --apply

# 4) 不要备份（不推荐，除非你确定要覆盖）
py -3.11 convert.py "<路径>.pdf" --apply --no-backup

# 5) 文字层 PDF 与批量
py -3.11 convert.py "<语料目录>\中国制造2025.pdf" --check
Get-ChildItem "<语料目录>\*.pdf" | ForEach-Object { py -3.11 convert.py --check $_.FullName }

# 6) 显式选后端 / 门禁把关 / 页级 OCR
py -3.11 convert.py "<路径>.pdf" --apply --backend structured      # pymupdf4llm，出标题/表格/配图
py -3.11 convert.py "<路径>.pdf" --apply --backend paddle          # 多页扫描件，页级 OCR
py -3.11 convert.py "<路径>.pdf" --apply --strict                  # 门禁非 pass 直接失败，不写盘
py -3.11 convert.py "<路径>.pdf" --apply --ocr-fallback            # 门禁不通过时自动降级到页级 OCR
```

| 参数 | 行为 | 写盘 |
| --- | --- | --- |
| （无） | dry-run，MD 打印到 stdout，stderr 提示 `[dry-run] 未写盘` | 否 |
| `--check` | 只读预检报告，不生成 MD 正文 | 否 |
| `--apply` | 写出 `<同名>.md`，已存在先备份 `.md.bak` | 是 |
| `--apply --no-backup` | 写出但不备份 | 是 |
| `--backend auto\|builtin\|structured\|paddle` | 选抽取后端；`auto`：文字层优先结构化，截图型仍走内置 | 随 `--apply` |
| `--extractor pymupdf4llm\|pypdf` | 结构化路径的抽取器，默认 `pymupdf4llm` | 随 `--apply` |
| `--strict` | 质量门禁非 `pass` 时抛错退出（`1`），不写盘 | 否 |
| `--ocr-fallback` | 门禁不通过时改用页级 OCR（需配置引擎），结果不比原来差才换 | 随 `--apply` |
| `--page-marks` | 页级 OCR 正文保留 `===== page n =====` 页标记（默认不写，标记留在 `.ocr.txt`） | 随 `--apply` |

- `--check` 与 `--apply` **互斥**，同时给会 `parser.error` 退出；
- 退出码：**运行期异常**（不是 PDF、解图失败、缺 pdftotext、OCR 失败、读盘失败、**文件不存在**）返回 `1`；**用法错误**（缺参数、`--check` 与 `--apply` 同时给）由 argparse `parser.error` 处理并返回 `2`（Unix 惯例）。实测：`文件不存在 → 1`、`非 PDF → 1`、`--check --apply → 2`、`缺参数 → 2`。

输出形态：

```
# <文件名不含扩展名>

> 来源：<原文件名>
> 形态：截图OCR | 文字层 | 伪文字层渲染OCR
> 说明：正文由截图 OCR 得到，段落边界为启发式推断，不还原原排版。   ← 仅截图型有这行

<正文段落，段落间空一行>
```

### 批量入口

`batch_convert.py` 递归扫目录，支持「平铺收集」或「镜像源结构」两种输出，整库转换用这个：

```pwsh
# 镜像输出结构（推荐，保持分类层级）
py -3.11 batch_convert.py "<源目录>" --mirror "<输出目录>"

# 平铺收集到同一目录
py -3.11 batch_convert.py "<源目录>" --out "<输出目录>"

# 已有 .md 也重转（默认跳过已转换的）
py -3.11 batch_convert.py "<源目录>" --mirror "<输出目录>" --force

# 钉后端 + 门禁把关 + 逐件留痕
py -3.11 batch_convert.py "<源目录>" --mirror "<输出目录>" --backend structured --strict --log "<输出目录>\convert.jsonl"
```

- `root`：扫描根目录（递归找 `.pdf`）；
- `--out <DIR>`：产物平铺到该目录；`--mirror <DIR>`：按源相对结构落到该目录（推荐；配图跟着 md 落 `<产物目录>/<stem>.images/`）；
- `--force`：已有 `.md` 也重转；
- `--backend` / `--extractor` / `--ocr-fallback` / `--page-marks`：与单份 `convert.py` 同名参数一致；
- `--strict`：门禁非 `pass` 的件**不写盘**并计入失败；
- `--log <FILE>`：每件追加一行 JSONL（pdf / md / backend / shape / pages / chars / imgs_kept / ocr_txt / gate / sec），崩了也能按行排查；写日志失败只警告，不让整批陪葬；
- 平铺 `--out` 时不同目录的同名 PDF 会撞同一产物：**先写的赢，后者记「产物重名」失败**，不静默覆盖；
- 末尾打「完成 / 跳过 / 空·过短 / 失败」（空·过短按门禁可见字符数 < 100 判，不再用文件字节数）「门禁 pass/review/fail」「后端分布」「本次新跑页级 OCR 件数」；
- **退出码**：有失败（异常或 `--strict` 下的非 pass）返回 `1`，否则 `0`；
- 单份转换仍用 `convert.py --apply`。

`--check` 会打出「形态：截图OCR / 文字层」，截图型额外打「原图：540 × 10756 px，切 7 片」与「OCR 可用：是/否」，文字层额外打「pdftotext：<实际选中的路径>」；再加两行「结构化抽取：pymupdf / pymupdf4llm / pypdf 可用性」与「PaddleOCR-json：<引擎路径或未配置>」，最后一行「后端：auto → <实际选中的后端>」——一眼看出走了哪条路、用的哪个 exe、门禁给了什么结论。

`--check` 的「只读」是硬约束：结构化路径只统计配图、**不落 `<stem>.images/`**（`write_images=False`）；页级 OCR **不触发识别**，只有 sidecar 完整时才读回评估门禁，否则打「未跑或未完成」。显式 `--backend structured/paddle` 时，多图截图型也能预检（内置路径解不出单张整页图只会拦 `auto/builtin`）。见 [PITFALLS 坑 26](PITFALLS.md)。

---

## 三、判定原理

| 步骤 | 函数 | 做什么 |
| --- | --- | --- |
| 文件识别 | `detect_pdf(raw)` | 只看文件头 `%PDF-`，不信扩展名 |
| 形态判定 | `kind_of(raw)` | 有 `/Subtype /Image` 且无 `/Type /Font` → `image`；有 `/Type /Font` → `text`；都没有 → `unknown`（报错退出，不猜）。注意 `/Font` 只是启发式：**伪文字层** PDF 含 `/Font` 却抽出 0 字符，由 `process_file()` 在文字层抽空时降级到 `ocr_pdf_via_render()` |
| 解图 | `extract_image(raw)` | 扫所有 `/Subtype /Image`，取**面积最大**那张（避免拿到 logo）；必须有 `/ColorSpace /DeviceRGB` 显式声明；`zlib` 解流；**校验 `len(pix) == w*h*3`**，不符跳过 |
| PNG 封装 | `png_bytes(w, h, rgb)` | 手写 `IHDR`/`IDAT`/`IEND` + `zlib.crc32`，每行前置 `\x00` filter 字节 |
| 切片 | `slice_plan(h, step=1800, overlap=60)` | `MaxImageDimension` 只有 10000 px，最长的实测 20000 px，必须切片；片间留 60 px 重叠防止切断行 |
| OCR | `ocr_pdf(raw)` | 建 `mkdtemp` → 逐片写 PNG → 调 `powershell -NoProfile -ExecutionPolicy Bypass -File` → WinRT `OcrEngine` 逐行收集 → 重叠区按「最近 30 行内出现过」去重 |
| 中文清洗 | `collapse_cjk_space(s)` | OCR 会在每个汉字间插空格（`只 加 两 行`），五轮 `re.sub` 折叠 CJK↔CJK / CJK↔数字 / 数字↔CJK / CJK↔符号，**保英文空格** |
| 噪声行 | `is_noise_line(line)` | 六条判据：①整行等于提示词抠掉后只剩标点 ②整行命中 `UI_LINE_EXACT`（知乎/QQ 浏览器/App 内打开…以及 `0`/`打开`/`继续`/`知《` 这类 OCR 碎片）③行尾是「广告」角标（信息流广告卡）④扫码引流话术 ⑤知乎推荐区（`6．7K 关注 · 387回答`，必须「关注」「回答」同现）⑥QQ 浏览器热搜尾巴（`460．2w 人在`）与信息流广告卡。**不做子串删除**，交给下一行 |
| 行内剥离 | `strip_inline_noise(line)` | 广告在正文行中段时（`下载 App 注册登录`、`打开 App 查看更多评论查看全部249个回答`、`囗复知乎用户`）按 `INLINE_NOISE_PATTERNS` 摘掉，剩空则整行弃 |
| 整块清理 | `drop_page_numbers` / `drop_digit_runs` | 页码 `n / 21` 只在**同一分母出现 ≥3 次**时才删（避免误删 `20 / 8` 这种网速算式）；纯数字行只删**连续 ≥3 个**的串（热搜序号），孤立数字保留（`11_中国制造2025` 表格首列） |
| 整块截断 | `truncate_feed_tail(paragraphs)` | 行级规则删不掉「末尾连着 10 段知乎问题标题」这种**结构**噪声，得在合段之后按段落切。两把刀：①锚点截断（段落含 `今日推荐阅读`/`往期精彩文章`/`QQ 浏览器热搜榜`/`限时免费领`，且位置在全文后半段 → 从该段起全丢）②尾部连跑截断（末尾连续 **≥3 段**都是信息流标题形状：≤40 字且问号收尾、或「关注·回答」计数行） |
| 合段 | `reflow(lines, median=None)` | 行以 `。！？…”」』；!?：.` 收尾、或长度 ≤ `0.62 × 中位行宽` 即断段；拼行时 `_join_line`/`_needs_space` 决定要不要补空格（中文直接接上，英文/数字之间补空格）；末尾 buf 必 flush |
| 文字层 | `text_pdf(path)` | `pdftotext -layout -enc UTF-8 <path> -`，stdout 按行切 |
| 找 exe | `find_pdftotext()` | 环境变量 `PDFTOTEXT` → `shutil.which` → 已知路径，**每个都实跑 `-v` 验证**（`pdftotext_works`） |
| 渲染 | `render_markdown(title, paras, shape, source, note=None)` | H1 + `>` 注记 + 段落，段落间空一行 |
| 残留检测 | `residual_noise(md)` | 扫 `<script`/`<div`/`data-v-`/`&nbsp;` 等 |
| 后端路由 | `resolve_backend(backend, kind, extractor)` | `auto`：文字层且结构化可用 → `structured`；截图型/伪文字层/未知 → `builtin`（不自动换引擎） |
| 只读预检 | `check_file(path, ...)` | 形态/依赖/后端/门禁一次打全；**不落配图、不触发页级 OCR**（`write_images=False`，paddle 只读回 sidecar） |
| 结构化抽取 | `structured.extract(path, ...)` | `pymupdf4llm.to_markdown(write_images=False)` → 空正文且 pypdf 可用时降级 `pypdf` |
| 抽配图 | `structured._collect_images(doc, ...)` | `<80px` 跳过；`colorspace.n>3` 转 sRGB；同图出现 `>= max(4, 页数/3)` 页判装饰；同 hash 去重；落 `<stem>.images/<stem>_<hash>.png`，md 末尾追加「## 配图」 |
| 页级 OCR | `paddle_ocr.run(pdf, ...)` | 逐页 pymupdf 渲染（`PDF2MD_OCR_DPI`，默认 200）→ 常驻引擎一页一请求 → 页标记全文 + sidecar |
| 续跑校验 | `paddle_ocr.is_complete(pdf)` | 源 sha256 / 总页数 / 已完成页 / 输出 sha256 四道全过才算完成，异常一律 `False`（fail closed） |
| OCR 合并 | `ocr_merge.merge_file(pdf)` | 旧头部（注记改写为页级 OCR）+ OCR 正文 + 原「## 配图」段；备份 `<md>.ocr.bak` 只在没有备份时创建 |
| 质量门禁 | `quality.assess(md)` | NFKC → 去 Markdown/页标记 → 可见字符 `<100` fail / 异常字符 `>=2%` fail / CJK `<15%` review / 其余 pass |

关键取舍：

- **主路径互不兜底，但空结果必降级**。截图型不解文字层，文字层不解图——一份 PDF 走哪条主路在 `kind_of` 就定死，避免「两条路都不彻底、产物来源说不清」。唯一的例外是文字层**抽出 0 字符**：这说明 `/Font` 判定是误判（伪文字层），此时降级到 `ocr_pdf_via_render()` 渲染后 OCR，而不是交一份空 MD（见坑 18）。降级只针对「文字层抽空」这一种情形，截图型抽不出字仍直接报错，不引入「两条路来回试」的模糊逻辑。
- **`pdftotext_works` 实跑验证，不看文件存在**。本机 PATH 上排第一的 `<MiKTeX 安装目录>\pdftotext.EXE` 一跑就崩（log4cxx `setFile` 失败、`exit -1073740791`），只看 `os.path.isfile` 会让 样本 11（文字层，公开政策文件） 直接失败。见 [PITFALLS.md 坑 6](PITFALLS.md)。
- **`len(pix) == w*h*3` 是硬校验**。解出来字节数对得上，说明确实是 DeviceRGB 8bit 未压缩像素；对不上说明这 PDF 用了别的色彩空间或位深，此时返回 `None` 报错，比猜出来一堆花屏文字强。
- **分段只能靠文本特征**。WinRT 的 `OcrLine.Words[0].BoundingRect` 在 PowerShell 5.1 里取不到（返回 `System.Object[]`，强转 `Windows.Foundation.Rect` 失败），所以拿不到坐标，只能靠「终止标点 + 中位行宽」推断段落边界。这是已知的天花板，见 [PITFALLS.md 坑 4](PITFALLS.md)。
- **噪声规则宁可放过，不肯错杀**。每条规则都先在 11 份真实产物上量过误杀面才落地：知乎推荐区那条必须「关注」「回答」同现，因为全书 6 行只含「关注」的全是正文（`交叉关注点`、`新材料制备关…`）；页码那条要求分母重复 ≥3 次，否则 `02` 号网速算式里的 `20 / 8` 会被当成页码删掉；广告角标那条限定行尾 60 字内。尾部截断同理：必须有**连续 ≥3 段**才动手，2 段不动——正文里的连续设问也就这么长（`为什么会这样？`）。见 [PITFALLS.md 坑 14–17](PITFALLS.md)。
- **结构化产物不再过启发式清洗**。`--backend structured/paddle` 的正文自带标题、表格、列表与空段，直接 `_wrap` 拼头部落盘，不进 `reflow`/噪声规则——否则 `# 标题` 会被当正文行再合段、表格竖线会被当正文符号（见坑 23）。
- **能力缺失要显式报错，不静默降级**。`--backend structured` 缺 `pymupdf4llm`、`--backend paddle` 缺引擎时直接 `PdfError` 退出 1；只有 `--ocr-fallback` 是**显式**允许「门禁不通过 → 页级 OCR」的降级开关，且换掉之前会比较门禁等级（`GATE_RANK`），不比原来好就不换。
- **每种「完成」都要能自证**。OCR 全文旁边必须有一份 sidecar（源指纹/页集/输出 hash），没有或对不上就当没做——「文件存在」不是完成（见坑 20）；md 的原子替换与 `.md.bak`、`.ocr.bak` 也都是为了让中断与重跑可恢复。
- **预检必须零写盘，stdout 必须是产物**。`--check` 复用转换逻辑，但落图与 OCR 都得显式关掉（见坑 26）；`pymupdf4llm` 的解析提示会走 PyMuPDF 自己的输出流，抽取期间要改道 stderr 并在 `finally` 恢复，否则 dry-run 的 md 会被插进 `=== Document parser messages ===`（见坑 27）。

---

## 四、运行风险与边界

- **OCR 有固定小瑕疵，不承诺干净**。实测三类：（1）作者行会粘连，`原创小智、核子可乐架构头条7，月22日作者]小智、核子可乐`；（2）引号后段落偶有粘连，`"只加了两行代码，为什么你要用两天时这种问法看似合理…`；（3）全角/半角标点会走样（`7，月22日`）。这些是 OCR 引擎与「无坐标合段」共同造成的，脚本如实转写、不臆造修补。
- **广告卡与正文粘连时只能部分清理**。知乎信息流广告卡被 OCR 与正文并到同一行时（`怎样挽留男友？…23岁以上．．好多妹子都说自己天天敲山根…`），工具只能删掉可识别的角标/按钮，卡片标题文字会留在行首。要彻底分开需要版面坐标，而 WinRT 在这个环境下拿不到坐标（见坑 4）。这是已知天花板，**不为它加更激进的子串规则**——那会把正文一起吃掉。
- **不还原原排版（内置路径）**。`builtin` 只有段落，没有标题层级、列表、表格、代码块、图片，注记里已写明「不还原原排版」。要结构与配图用 `--backend structured`。
- **不抽内嵌图片（内置路径）**。截图型 PDF 的那张长图只用于 OCR，不解出、不落盘、不进 MD；`structured` 才会把配图存到 `<stem>.images/` 并在 md 末尾列引用。
- **截图路径假设窄**：单页 + 单张整页图 + `DeviceRGB` + 8bit + `FlateDecode`。换别的 PDF 生成器（多页、CMYK、JPEG2000、`DCTDecode`）会返回 `None` 并报错，需按需扩 `extract_image`。**多页扫描件只转面积最大那页**（当前实现不拼页），`--check` 不会为此报警 —— 已知天花板。
- **依赖本机能力，不可移植**：截图型要 Windows PowerShell 5.1（`pwsh 7` 里 WinRT 投影类型加载失败）+ 中文 OCR 语言包（实测只有 `zh-Hans-CN`）；文字层要 poppler。换机器先跑 `--check` 看「OCR 可用」「pdftotext」两行。
- **可选后端各带一份新依赖，缺了就明说**：`structured` 要 `pymupdf`/`pymupdf4llm`（`pypdf` 仅空正文兜底时用，缺了不报错）；`paddle` 要另装 PaddleOCR-json，用 `PDF2MD_OCR_EXE` 或 `PDF2MD_OCR_DIR` 指过去（也认历史变量名 `KB_OCR_ENGINE_EXE`/`KB_OCR_ENGINE_DIR`）。`--check` 的「结构化抽取」与「PaddleOCR-json」两行就是判据。
- **页级 OCR 的路径限制**：引擎只接受 **ASCII 图片路径**，脚本会把渲染图放进 ASCII 临时目录（系统临时目录含中文时回退到 `%SystemRoot%\Temp`），两者都不可用就直接报错；单页实测 0.8–1.0 s，多页扫描件按页数估算时间。
- **OCR 很慢**：11 份跑完 `--check` 共 24–26 秒（最长那份 12 片）；batch 时耐心等，别以为卡死。
- **结构化预检会完整抽一遍**：`--check` 为了给门禁结论必须真抽正文，一份 266 页的样本约 70 秒（`--apply` 同级）；`paddle` 的预检已改成零 OCR，只读 sidecar。配图字节在抽取期间整批驻留内存（121 张 PNG 约几十 MB），图片极多的画册要留意内存。
- **`--strict` 批量失败可能留下配图**：结构化路径先落图再判门禁，`--strict` 拦下正文时 `<stem>.images/` 已经写入（重跑会复用同 hash 文件名，不产生垃圾堆积，但不是零残留）。要绝对干净就先删掉该目录再重跑。
- **默认只读**：不加 `--apply` 绝不写盘；`--apply` 且目标已存在会自动 `.md.bak` 备份，覆盖前可回滚。**重复 `--apply` 不会用上次产物顶掉首次备份**（`.bak` 已存在就跳过），所以第二次起 `.bak` 始终是最初那份。

---

## 五、验收 · 自测

```pwsh
cd pdf2md
py -3.11 -m unittest -v        # 169 条测试，全绿（OCR 打桩、pymupdf 用例按本机能力跳过）
py -3.11 convert.py "<路径>.pdf" --check
```

已核对（真实 11 份，**`--backend builtin`**，总耗时 24–26 秒，全部 `残留：[]`；这是 2026-11-21 合并前的基线，`auto` 在装了 pymupdf4llm 的机器上会把文字层 PDF 改走结构化）：

| 语料 | 尺寸 px | 切片 | `--check` 关键输出 |
| --- | --- | --- | --- |
| 样本 01（截图型） | 540×20000 | 12 | 形态截图OCR / 215 行 / 7884 字符 |
| 样本 02（截图型） | 540×20000 | 12 | 形态截图OCR / 277 行 / 6280 字符 |
| 样本 03（截图型） | 540×11530 | 7 | 形态截图OCR / 149 行 / 3463 字符 |
| 样本 04（截图型） | 540×17491 | 11 | 形态截图OCR / 187 行 / 5078 字符 |
| 样本 05（截图型） | 540×7311 | 5 | 形态截图OCR / 87 行 / 1507 字符 |
| 样本 06（截图型） | 540×8686 | 5 | 形态截图OCR / 89 行 / 2194 字符 |
| 样本 07（截图型） | 540×10756 | 7 | 形态截图OCR / 85 行 / 2478 字符 |
| 样本 08（截图型） | 540×8646 | 5 | 形态截图OCR / 77 行 / 2422 字符 |
| 样本 09（截图型） | 540×5313 | 4 | 形态截图OCR / 63 行 / 1112 字符 |
| 样本 10（截图型） | 540×8137 | 5 | 形态截图OCR / 87 行 / 1733 字符 |
| 样本 11（文字层，公开政策文件） | 文字层 | — | 形态文字层 / `pdftotext` 选中 `C:\Program Files\Calibre2\app\bin\pdftotext.exe` / 382 行 / 22767 字符 |

**噪声清理实测效果**（按行分桶统计，判据见「三、判定原理」）：

| 版本 | 总行数 | 可丢行 | 占比 |
| --- | --- | --- | --- |
| 改进前 | 1063 | 169 | 16% |
| 加规则后 | 961 | 53 | 6% |
| 加广告角标/OCR 碎片/行内剥离后 | 925 | 31 | 3% |
| 最终（`_pdfout4`，未含尾部截断） | 919 | 23 | **3%** |

再叠上尾部整块截断后，正文段落数从 919 降到 **821 段**（噪声占比仍在 3% 量级，因为这一轮砍的是**整段**而非行，分母同步变小）。

尾部整块截断（`truncate_feed_tail`）在 11 份上逐份模拟验证过，实际砍掉的段落数：`05` 7 段、`08` 3 段、`09` 5 段、`10` 6 段、`06` 3 段、`07` 22 段（锚点命中 `0今日推荐阅读`）、`03` 6 段（锚点命中 `程序汪往期精彩文章`），`01`/`02`/`04`/`11` 零改动（文字层与干净语料不受影响）。

残留的 23 行全部人工核对过，无一是「本可安全删掉」的：其中 15 行是把正文里出现「知乎」二字的段落误报为 UI（如 `我深知在知乎上藏龙卧虎…`），真正属于噪声的只有 5 行 OCR 乱码行（`元整的 commit message 示例· 相应的 git og《 u 沙00 ]，…`）、3 行表格首列孤立数字（`11` 号的 `2`/`3`/`5`，**必须保留**）。**正文零丢失**已用「在新产物里逐行搜索原文片段」的方式核对（不是靠行集差——`reflow` 合段会让行边界移动，差集里的「消失行」很可能是被并进了别的段落）。


- 11 份全部 `残留：[]`；
- 产物抽样已人工核对，正文可读、段落已合、中文空格已折叠（`代码行数=工作量`）；
- 截图为 OCR 结果，**未逐字校对**，已知瑕疵见「四、运行风险与边界」；
- 全部临时产物（`_proto_pdf.py`、`mkdtemp` 临时目录、`_pdfout*/`、`__pycache__`）验证后已清理；11 份语料 PDF 零改动。

单测覆盖（`test_convert.py` 113 条；README 旧版写 96 条，后续补了题号标题化等用例）：

- `PdfDetectTest`(4)：文件头识别、三种形态判定；
- `ExtractImageTest`(6)：像素往返、尺寸不符返回 None、无图返回 None、**多图时取最大页图而非第一张**、**缺 `/ColorSpace` 声明必须拒绝**、`/Width 5 0 R` 间接引用返回 None；
- `PngBytesTest`(5)：PNG 结构（魔数 + IHDR/IDAT/IEND）、IHDR 尺寸字段、**IHDR 必须声明 8bit + color type 2**、**解回 IDAT 逐字节比对像素与 filter 字节**、非正尺寸抛 `ValueError`；
- `SlicePlanTest`(4)：短图单切片、长图切片且相邻片重叠 60 px、末片不越界、**`step <= overlap` 抛 `ValueError`（否则死循环）**；
- `CollapseTest`(4)：CJK↔CJK 去空格、CJK↔数字去空格、**英文空格必须保留**、混合行；
- `ReflowTest`(10)：终止标点断段、续行合并、短行断段、末尾 buf 必 flush、空行跳过、**英文续行补空格**、英文句子不粘连、中文续行不插空格、**英文闭合引号后仍补空格**、**中文接数字不补空格**；
- `NoiseLineTest`(5)：整行噪声丢弃、**正文含提示词的行必须保留**、普通行保留、**短但含提示词的正文不能丢**、**只剩提示词+标点仍要丢**；
- `NoiseRuleTest`(4)：知乎推荐区四种变体全丢（含 OCR 误识 `3． IK 关注 · 89回答`）、**「关注点」正文必须保留**、热搜尾巴丢弃、整行 UI 文案丢弃；
- `InlineStripTest`(5)：App 引流剥离、段尾广告剥离、剥空后整行弃、**正文行不受影响**、**URL 不被破坏**；
- `PageNumberTest`(2)：同分母重复时删、**分母唯一时保留（`20 / 8` 是网速算式）**；
- `DigitRunTest`(3)：连续 3 个删、**孤立数字保留**、连续 2 个保留；
- `AdAndFragmentTest`(9)：行尾广告角标丢、**正文提「广告」保留**、扫码引流丢、**`0`/`打开`/`继续`/`知《` OCR 碎片丢**、**`与`/`适配`/`融合` 等 1–2 字正文保留**、**表格首列孤立数字保留**、`囗复知乎用户` 剥离、信息流广告卡丢、**正文中段「查看详情」保留**；
- `ParasPipelineTest`(1)：页码 + 推荐区 + 引流 + 数字串 + 正文混排的端到端结果；
- `TailTruncateTest`(7)：末尾连续 ≥3 段问句截断、**2 段不动（正文连续设问）**、**中间的问句不动**、「关注·回答」计数行也算信息流段、锚点词截断、**锚点在前半段不动**、端到端含尾部截断；
- `OcrScriptTest`(3)：OCR 脚本确实调用 WinRT API、按 `slice_*` 文件名排序、`ocr_available()` 返回 bool；
- `FindPdftotextTest`(4)：环境变量优先、环境变量指向不存在文件时不原样返回、**PATH 上坏的 exe 必须跳过继续找**、全坏返回 None；
- `PdftotextWorksTest`(4)：非零退出返回 False、版本横幅返回 True、启动抛 OSError 返回 False、None 返回 False；
- `RenderMarkdownTest`(4)：H1 + 注记、段落空行分隔、残留检测干净/命中；
- `ProcessFileTest`(9)：dry-run 不写盘、`--apply` 写盘、`--apply` 备份已有、`--no-backup` 不备份、非 PDF 抛 `PdfError`、**连续两次 `--apply` 不覆盖原始备份**、**超时转 `PdfError`**、**读盘 OSError 转 `PdfError`**、**空正文给 stderr 警告**；
- `CliTest`(3)：`--check --apply` 互斥、缺参数退出、运行期异常退出码 1。

OCR 单测全部打桩（`convert.ocr_pdf = lambda raw: [...]`），因为真 OCR 依赖本机 WinRT 与语言包，进夹具会让测试不可移植。

**合并进来能力的单测（`test_merge.py` 56 条）**，同样只锁可移植的部分，pymupdf 用例按本机能力 `skipUnless`：

- `FsutilTest`(5)：原子替换且不残留临时文件、`safe_name` 收敛非法字符/保留名/超长、sha256 与 doc_hash、GB18030 回读、二进制拒读；
- `QualityTest`(9)：Kangxi 部首经 NFKC 归一后算中文、CJK 扩展 A、过短/乱码/低 CJK/健康四态、**配图语法与页标记不稀释 CJK**、只有页标记时判空、阈值与上游同值；
- `StructuredFilterTest`(5)：装饰图阈值 `max(4, 页/3)`（**2 页重复不算装饰**）、同 hash 去重且保留首次页号、配图段格式、抽取器可用性快照；
- `StructuredExtractTest`(5，需 pymupdf)：**6 页里 4 页重复的装饰图不落盘、正文图留 1 张**、`write_images=False` 时统计照算但**一个文件都不落**、20px 小图在入库前就被过滤、正文 PDF 页数与 CJK 抽取、页数/渲染/越界；
- `PaddleParsingTest`(5)：页范围解析（**`-1` 是负号不是范围**、非法段跳过、超界裁剪）、sidecar 路径、嵌套 `text/score` 收集、**非 ASCII 图片路径必须拒**、页标记默认不写正文；
- `PaddleTextStoreTest`(7，需 pymupdf)：落盘即完整、**页不全/正文被追加/源 PDF 被换/meta 标记关掉都不算完成**、**引擎返回空串也不算完成**、页标记 round-trip；
- `OcrMergeTest`(5，需 pymupdf)：合并保留 H1 与「配图」段且把注记改写为页级 OCR、**第二次合并不覆盖首次备份**、dry-run 不写盘不留备份、缺 OCR 全文/OCR 不全都报错；
- `BackendRoutingTest`(5)：未知后端报错、显式后端透传、**auto 的截图型仍走 builtin**、auto 文字层按结构化可用性路由；
- `ConvertIntegrationTest`(7)：报告带门禁与 shape、短正文照写但结论 fail、**`--strict` 不写盘**、结构化产物经 `_wrap` 拼头部、`--check` 打出后端与门禁行、**`--check --backend structured` 不创建 `.images/`**、`--page-marks` 不影响内置路径；
- `BatchTargetTest`(1)：`--out` 平铺 / `--mirror` 镜像 / 默认 PDF 旁三种落点。

**合并后的真实冒烟**（2026-11-21，本机 3 页样例 PDF，`py -3.11`）：`--backend structured` 产出 582 字节 md + `样例书.images/`；`--backend paddle` 产出 556 字节 md + 407 字节 `.ocr.txt` + 463 字节 sidecar（`complete: true`、`completed_pages: [1,2,3]`）；`ocr_merge.py --dry-run/--apply` 均正常，备份为 `扫描件.md.ocr.bak`。空配图不再创建空目录。

**变异测试核对过断言有效性**（第二轮审查后补）：对 `convert.py` 做 10 处定向变异（丢像素、改灰度、去 filter 字节、备份无条件覆盖、噪声回到短行阈值、英文引号塞回免空格表、去掉空正文警告、不接超时、`slice_plan` 不校验参数、`png` 不校验尺寸），全部 10 个变异体都被测试杀死，还原后 `OK`。第一轮审查发现的「PNG 断言恒真假绿」就是这个环节暴露的。

**噪声规则不是拍脑袋写的**（第三轮，实测驱动）：先把 11 份真实产物按行分桶统计（`body`/`ui`/`rec`/`hot`/`digit`/`pageno`/`ad`），量出 16% 可丢率，再逐类找证据、量误杀面，最后才写规则。每条规则都有一条对应的「不能删」反向测试：知乎推荐区 ↔ `test_body_mentioning_attention_kept`、页码 ↔ `test_kept_when_denominator_unique`、数字串 ↔ `test_isolated_digit_kept` / `test_table_first_column_digit_kept`、广告角标 ↔ `test_body_mentioning_ad_kept`、OCR 碎片 ↔ `test_short_body_word_kept`。这套「正向删 + 反向留」的配对测试，是防止规则越写越宽的唯一手段。

---

## 六、未做 · 后续

- **未自写文字层解析器**。实测过：ToUnicode CMap 解出 1054 条映射，但内容流里 252 个不同双字节码**只命中 1 个（0.4%）**；还得处理 `/ObjStm`（N=485, First=4638, len=45807）与多字体 CMap 归属，约 400 行只服务 11 份里的 1 份 —— YAGNI，交给 poppler。
- **内置路径未抽内嵌图、未做版面还原**（标题层级 / 列表 / 表格 / 代码块）。`structured` 后端已补上这一层（pymupdf4llm + 配图落盘），代价是引入 pymupdf 系依赖；两条路各自成立，不互相兜底。
- **未做 OCR 后处理自动纠错**（如 `7，月22日`）；这类修补靠猜，容易把对的改错。
- **未逐字校对 OCR 结果**；质量以「四、运行风险与边界」列明的三类瑕疵为准。
- ~~未做批量入口~~ → 已加 `batch_convert.py`（递归扫描 + `--mirror` 镜像 / `--out` 平铺两种输出 + `--force` 重转；默认跳过已有 `.md`）。
- **伪文字层已兜底**：`kind_of()` 判为 `text` 但 `pdftotext` 抽 0 字符时（48 页纯图 PDF 叠了水印字体层就是这种），`process_file()` 自动降级到 `ocr_pdf_via_render()`：`pdftoppm` 渲染每页 PNG（`RENDER_DPI=120`）→ 复用同一份 WinRT OCR 脚本。依赖 `pdftoppm`（poppler/MiKTeX 一般随 `pdftotext` 一起装）。
- 后续若遇到新 PDF 生成器：先 `--check` 看是 `image`/`text`/`unknown`，再决定扩 `extract_image`（支持新 ColorSpace/BitsPerComponent/Filter）还是接 `pdftohtml`。

---

## 七、合并自上游知识库管线的 PDF→MD 能力（2026-11-21）

从一套内部知识库管线抽出的是**「PDF → 结构化 md + 配图 + 页级 OCR + 质量门禁 + 完成度自证」这一段**，没有搬它的台账、分类、切题、建库、检索。对应关系：

| 上游管线 | pdf2md 落地 | 保留的行为 |
| --- | --- | --- |
| `kb/convert_one.py`（`pdf_to_md` / `pdf_extract_images` / `cjk_ratio`） | `structured.py` | pymupdf4llm 结构化主路、pypdf 兜底、`<80px` 过滤、CMYK→sRGB、跨页装饰图 `max(4, 页/3)`、同 hash 去重、`## 配图` 段 |
| `kb/convert_one.py`（`safe_name`） | `fsutil.safe_name` | 非法字符收敛、Windows 保留名加前缀、80 字符截断 |
| `kb/atomic.py` / `kb/hashing.py` | `fsutil.atomic_write_*` / `sha256_file` / `doc_hash` | 同目录临时文件 + fsync + `os.replace`；sha256 前 16 位短指纹 |
| `ocr_pages.py` | `paddle_ocr.py` | 常驻引擎一页一请求、启动等 `OCR init completed.`、ASCII 图片路径、页标记全文、四道 sidecar 校验、崩溃重启一次 |
| `ocr_merge.py` | `ocr_merge.py` | 拆/接 `## 配图` 段、备份只在没有备份时创建、幂等合并 |
| `gate.py` + `kb/textmetrics.py` | `quality.py` | NFKC 归一、乱码字符白名单、`<100 字符 fail` / `异常字符 ≥2% fail` / `CJK <15% review` |
| `reconvert.py`（`_read_md`） | `fsutil.read_text_fallback` | `utf-8-sig → gb18030`，替换符过多即失败 |
| `reconvert.py`（输出名带 `doc_hash` 后缀） | `ocr_merge` / `batch --log` 的报告字段 | 产物可回归、逐件留痕 |

**与上游刻意不同的三处**（都能在产物里看见，不是隐性行为）：

1. **产物就地放，不认语料根**。md 落在 PDF 旁，配图落 `<stem>.images/`（上游是 `20-题库MD/<类>/<题>/images/`），OCR 全文与 sidecar 落 `<pdf>.ocr.txt` / `.meta.json`（上游落 `build/ocr_text/<rel>.txt`）。理由：这个仓库是独立工具，不该要求 `KB_CORPUS_ROOT`；目录自包含才能换机器自证。
2. **md 正文不写页标记**。上游把 `===== page n =====` 留在 md 里做回溯；这里标记只留在 `.ocr.txt`，md 保持可读，需要回溯就开 `--page-marks` 或直接看 sidecar。
3. **md 有统一头部**。上游 `pymupdf4llm` 原样吐正文；这里统一加 H1 + `> 来源/形态/说明`，与 `builtin` 产物形态一致（结构化正文里可能再出现一个 `#` 标题，属于 PDF 自身标题）。

**后端矩阵**（`auto` 的判据在 `resolve_backend` 注释里，测试也锁了）：

| PDF 形态 | `auto` | 显式可选 | 依赖 |
| --- | --- | --- | --- |
| 文字层（`kind_of == text`） | `structured`（若 pymupdf4llm 可用），否则 `builtin` | 全部 | pymupdf/pymupdf4llm；`builtin` 要 pdftotext |
| 截图型（单张整页长图） | `builtin`（WinRT 切片 OCR） | `paddle` | Windows PowerShell 5.1 + 中文 OCR 包；`paddle` 要引擎 |
| 伪文字层（有 `/Font` 却抽 0 字符） | `builtin` → 渲染兜底 OCR | `structured` / `paddle` | pdftoppm 或 pymupdf + 引擎 |

**产物清单**（`--apply` 之后可能出现的东西，全部可回滚/可重建）：

| 文件 | 何时出现 | 作用 |
| --- | --- | --- |
| `<stem>.md` | 总是 | 正文；原子替换写入 |
| `<stem>.md.bak` | 覆盖已有 md 且没备份过 | 首次产物备份（不会二次覆盖） |
| `<stem>.images/<stem>_<hash>.png` | `structured` 且抽到配图 | md 里以 `./<stem>.images/...` 相对引用 |
| `<pdf>.ocr.txt` | `paddle` 或 `--ocr-fallback` 成功 | 带页标记的 OCR 全文 |
| `<pdf>.ocr.txt.meta.json` | 同上 | 源 sha256 / 总页数 / 已完成页 / 输出 sha256 / `chars`（含页标记）/ `body_chars`（纯正文，用于判空）/ `complete` |
| `<stem>.md.ocr.bak` | `ocr_merge.py` 第一次合并 | 合并前的 md 快照 |

**门禁与降级链路**（对应上游 `gate → dispatch → ocr_pages → ocr_merge`，去掉台账这一层）：

```
convert_file → 三态门禁 assess(md)
   ├─ pass      → 直接落盘
   ├─ review/fail + --strict        → 抛错，不写盘
   ├─ review/fail + --ocr-fallback  → 页级 OCR 重做（保留 structured 的配图段）
   │                                  只在 GATE_RANK 不比原来低时才采用
   └─ review/fail（都不给）          → 照写，stderr 打「质量门禁：…」并附指标
```

`ocr_merge.py` 是合并的下半段：把 `<pdf>.ocr.txt` 回填为 `<stem>.md` 的正文，头部注记改写为「页级 OCR（PaddleOCR-json 合并）」，原「配图」段原样接回。**sidecar 不完整就拒绝合并**——宁可不合并，也不要把半份 OCR 顶掉正常产物。

**没有合并的部分**（不是遗漏，是与「PDF→MD」无关）：`intake`/`classify`/`chunker`/`build_db`/`search`/`extract`/`verify_ledger`/`wlock`、`gate_report.json` 台账与 `dispatch_plan.csv`、语料树约定（`20-题库MD`、`31-收件箱`）、LLM 打标与隐私扫描。这些依赖 SQLite 台账、语料根与分类规则，搬进单文件工具只会带进一层不该有的状态。`wlock` 也只在多进程写库时才有意义，这里写盘已经是原子的。

---

## License

MIT，见 [LICENSE](LICENSE)。欢迎提 issue / PR；改行为请先跑 `py -3.11 -m unittest`，并在 PR 里附上语料前后对比（`--log` 的 chars / gate 两列就够）。
