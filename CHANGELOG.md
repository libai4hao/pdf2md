# Changelog

本文件记录所有值得注意的变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Fixed

- OCR 子进程的启动、写 stdin、读 stdout 与退出清理异常统一转成 `OcrError`（保留 `__cause__`）；启动期进程提前退出也会收掉管道，不再留下半开句柄。
- OCR 全文与 sidecar 提交前保留旧版本快照，提交失败回滚；写入后校验输出 hash 与源 PDF hash；回滚失败不再顶掉原始异常。
- 结构化抽取不再只兜底空正文：质量门禁未达标时尝试 `pypdf`，只有等级提升才采用；候选抽取失败保留主结果，不把可用产物拖成失败。
- `--check` 读取文件失败转成稳定错误信息与退出码 1，与 `--apply` 的错误语义一致。
- OCR 合并前后核对 PDF 指纹，合并期间源文件变化则拒绝覆盖；正式 Markdown 写入失败转成 `MergeError`，写入后校验 hash。
- 批量转换把输出目录创建、转换与正式 Markdown 写入的 `OSError` 纳入单文件失败，并继续处理后续 PDF。
- 批量统计的「门禁 pass/review/fail」与「后端分布」改为只在正式 Markdown 落盘成功后计数，写盘失败不再污染分布。
- pypdf 兜底临时调高 `pypdf` logger 级别后，在 `finally` 恢复原 level，不再把全局降噪副作用留给调用方。
- 结构化路径 `--strict` 未通过门禁时，回收**本次新建**的配图；已存在的同 hash 图与既有目录保留。`process_file` 不落盘（dry-run）时同步关闭配图写入，不再往语料目录塞图。

### Changed

- OCR 产物改为**版本目录 + 原子指针**：正文与 sidecar 先完整写入 `<pdf>.ocr/versions/<version>/`，最后原子替换 `<pdf>.ocr.txt.meta.json` 指针；`<pdf>.ocr.txt` 降级为可落后的可读镜像。指针提交前崩溃保留上一完整版本，提交后崩溃以新版本为准，不再出现正文/sidecar 半配对。v1 老 sidecar 仍可只读兼容。
- 结构化兜底形态标签：空正文仍为 `pypdf(空正文兜底)`，非空低质量改用 `pypdf(质量兜底)`，老调用方的产物说明保持不变。
- 质量门禁新增「4+ 连续同字（仅 CJK）占比」判据：`>=30%` 判 `review`（重复堆叠），用于兜住 OCR 复读/装饰字，不升级为 `fail`。

### Added

- 新增 `outline.py`（CLI `pdf2md-outline`）：页级 OCR 产物的**结构后处理**——按手写章节表（`level<TAB>标题`，顺序贪心定位，定位不到记 `缺失` 不猜）重排标题层级并用章节表措辞覆盖 OCR 错字；读 PDF 注释里的 URI 复原条目超链接（URL 末段建索引，纯域名回退主机名首段，允许 ±2 字符前缀差异），名称对不上就记 `未匹配` 原样保留。`--drop-toc` 按项目符号密度丢目录页并裁目录尾，`--start-at` 手指定起点，`--check` 只出 JSON 计划不写盘，报告含页数/行数/丢弃页数/章节与链接明细。
- 新增 `tests/test_outline.py`：pymupdf 现造两页合成 PDF（目录页 + 两级标题 + 一条内嵌链接），锁住标题定位、链接复原、目录页与目录尾裁剪、无章节表时保持扁平、`--check` 零写盘。pymupdf 缺失整类跳过。
- 结构化端到端用例按真实依赖 `pymupdf4llm` 跳过，`pymupdf` 单装时不再误报 ERROR/FAIL。
- 新增 OCR 启动/管道故障、sidecar 提交回滚、合并写盘失败与源文件变化、批量写盘失败等故障注入用例；用真实单页 PDF 走完整合并前置校验，避免因 sidecar 先失败而误绿。
- 新增重复堆叠门禁、pypdf logger 恢复、strict 配图回收、dry-run 零配图、批量分布不计失败件等回归用例。
- 新增 OCR 版本提交用例：指针写失败保留上一版、指针提交后崩溃仍读新版本、v1 老 sidecar 兼容。
- 新增 `.github/workflows/ci.yml`：`main` push 与 PR 在 Ubuntu/Windows × Python 3.11/3.13 上安装 `.[full]` 并跑 `python -m unittest`。

## [0.1.1] - 2026-11-21

把工具装成命令，而不只是一个要 `py convert.py` 的脚本目录。

### Added

- `pyproject.toml`：`pipx install .`（推荐，隔离环境）或 `pip install .` 之后可直接用 `pdf2md` / `pdf2md-batch` / `pdf2md-merge` 三个命令；版本号唯一来源是 `convert.__version__`（setuptools 动态读取，不会与 tag 漂移）。
- 可选依赖组：`structured`（pymupdf + pymupdf4llm）、`pypdf`（空正文兜底）、`full`（两者全装）。核心路径仍是零依赖。
- `MANIFEST.in`：sdist 里带上 README / CHANGELOG / PITFALLS / LICENSE / requirements。
- 发布流水线新增 `python -m build`，把 wheel 与 sdist 一并附到 Release（不再只有源码 zip）。

### Changed

- `__version__` 从 0.1.0 升到 0.1.1。

## [0.1.0] - 2026-11-21

首个公开版本：把「一批中文 PDF 转 Markdown」的一次性处理，沉淀成可复跑、可验证的 CLI 工具。

### Added

- **内置后端（零第三方依赖）**：截图型 PDF 用标准库解 `FlateDecode` 单张整页图 → 手写 PNG → 切片 → Windows WinRT OCR → 中文清洗与启发式合段；文字层 PDF 调外部 `pdftotext`，抽空时自动降级「渲染页面再 OCR」。
- **结构化后端（可选）**：`--backend structured` 走 `pymupdf4llm`，保留标题/表格/列表；内嵌配图按 `<80px` 过滤、CMYK→sRGB、跨页装饰图 `max(4, 页数/3)`、同 hash 去重后落 `<stem>.images/`；空正文可用 `pypdf` 兜底。
- **页级 OCR 后端（可选）**：`--backend paddle` 接常驻 PaddleOCR-json 进程，逐页渲染识别，全文写 `<pdf>.ocr.txt` 并以 `.meta.json` 侧车（源 sha256 / 页集 / 输出 hash / `complete` / `body_chars`）自证完成度，支持断点续跑。
- **质量门禁**：三态 `pass / review / fail`（可见字符 <100 fail、异常字符 ≥2% fail、CJK <15% review），`--strict` 时非 pass 不写盘。
- **OCR 合并**：`ocr_merge.py` 把页级 OCR 全文回填为正式 md，保住原「配图」段与头部注记，备份 `<md>.ocr.bak` 只在没有备份时创建。
- **批量入口**：`batch_convert.py` 支持 `--mirror` / `--out`、断点跳过、`--log` 逐件 JSONL、门禁与后端分布统计、产物重名保护。
- **文档与测试**：`README.md`（用法、判定原理、风险边界、验收数据）、`PITFALLS.md`（27 条实测坑）、169 条 `unittest` 用例（第三方依赖用例按本机能力跳过）。

### Changed

- `--check` 是严格的只读预检：结构化路径只统计配图不落盘，页级 OCR 不触发识别，只读回已有 sidecar。
- `--version` 打印版本号；`convert_file()` 统一返回 md、后端、形态、门禁、页数、配图、OCR 全文等报告字段。

### Fixed

- 只读预检不再往语料目录写 `<stem>.images/` 与 `.ocr.txt`。
- `pymupdf4llm` 的解析提示改道 stderr，dry-run 的 stdout 只输出 Markdown。
- 配图在正文抽取成功后才落盘，失败不留孤立 PNG。
- 批量入口的空产物判定改用门禁可见字符数，不再用文件字节数；日志写失败不再拖崩整批。

[0.1.1]: https://github.com/libai4hao/pdf2md/releases/tag/v0.1.1
[0.1.0]: https://github.com/libai4hao/pdf2md/releases/tag/v0.1.0
