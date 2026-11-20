# Changelog

本文件记录所有值得注意的变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

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
