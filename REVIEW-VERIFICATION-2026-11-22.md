# pdf2md 审查报告与修复补丁复核结论

- 复核对象：`pdf2md` 代码审查报告（2026-11-22）、`pdf2md-fix.patch`、`pdf2md-test-report.md`
- 仓库：`D:\Project\my\vibe-ready-code\pdf2md`
- 基线提交：`63ca255`（与报告声明一致，工作树干净）
- 复核日期：2026-11-22

## 结论摘要

1. **审查报告的事实基础成立**：7 个问题在基线代码中均能定位，报告给出的测试基线（172 / 167 通过 / 1 跳过 / 1 失败 / 3 错误）在本机复现，失败原因确为 `pymupdf` 已装、`pymupdf4llm` 未装导致的测试依赖错配。
2. **补丁可干净应用，且能达成其声明的测试结果**：应用原始补丁后 180 个用例、175 通过、5 跳过、0 失败/错误，与 `pdf2md-test-report.md` 一致。
3. **补丁存在 1 个 P1 级验证缺陷和 3 个 P2/P3 级实现缺陷**，已在本次复核中修正：合并失败相关回归用例属于“假绿”，`pypdf` 兜底候选失败会拖垮主结果，引擎启动期异常未清理句柄，回滚失败会顶掉原始异常。
4. 修正后最终矩阵：完整依赖 **186/186 通过**（P3 后 191，P2 版本化后 194，见 §8）；仅 `pymupdf`+`pypdf` **181 通过 / 5 跳过**；零第三方依赖 **165 通过 / 21 跳过**；`py_compile` 与 `git diff --check` 均通过。
5. 真实 PaddleOCR-json 引擎在本机可用，已用 2 页 PDF 跑通「页级 OCR → sidecar → 合并」全链路，验证 ISSUE-01/02/06 的进程与提交路径，不再是纯 mock 结论。

## 1. 测试基线复现

环境：Python 3.13.18，`pymupdf 2.1`、`pypdf 7.1.1` 已装，`pymupdf4llm` 未装。

```text
python -m unittest discover -v
Ran 172 tests
FAILED (failures=1, errors=3, skipped=1)
```

失败/错误与报告完全一致，且全部集中在结构化抽取：

- ERROR：`test_image_filter_end_to_end`、`test_tiny_image_skipped`、`test_write_images_false_leaves_no_files`
- FAIL：`test_check_structured_does_not_write_images`
- 原因：`StructuredExtractTest` 类只按 `HAS_PYMUPDF` 跳过，但默认抽取器 `pymupdf4llm` 才是实际依赖。

同时确认：零第三方依赖环境下同一套用例是 `172 tests, OK (skipped=19)`，说明这些用例并不会“无差别失败”，报告对依赖隔离的判断准确。

## 2. 问题清单逐项核验

| Issue | 基线代码事实 | 补丁做法 | 复核结论 |
|---|---|---|---|
| ISSUE-01 (P1) | `PaddleEngine.__enter__` 直接 `Popen`，`ocr()` 直接写 stdin；`run()` 只捕 `OcrError/TimeoutError`，`OSError` 会冒到 CLI | 启动/写管道/读输出/退出清理统一转 `OcrError` 并保留 `__cause__`；`__exit__` 先关 stdin，再有界 terminate→kill | 成立。补充修正了启动期异常绕过 `__exit__` 的句柄泄漏 |
| ISSUE-02 (P1) | `save_text()` 先写全文、再写 sidecar，单文件原子但跨文件无事务 | 提交前快照旧版本；异常回滚；写后校验文本 hash 与源 PDF hash | 成立。崩溃窗口仍存在，但 `is_complete()` 会拒绝半成品并由下次 `run()` 重跑，报告已如实声明 |
| ISSUE-03 (P2) | 结构化端到端用例按 `pymupdf` 判断可用性，默认路径却要 `pymupdf4llm` | 4 个相关用例改用 `HAS_LLM` 跳过 | 成立且已复现：`pymupdf` 单装时由 1F/3E 变为 5 skip |
| ISSUE-04 (P2) | `check_file()` 的 `open()` 无 `OSError` 处理 | 读取失败打印稳定信息并返回 1 | 成立。与 `process_file()` 的 `PdfError` 语义对齐 |
| ISSUE-05 (P2) | 仅 `not md.strip()` 时走 pypdf 兜底，非空低质量结果不触发 | 质量门禁非 `pass` 即尝试 pypdf，且只有等级提升才采用 | 成立。补丁初版有回归风险（候选失败拖垮主结果），已修正 |
| ISSUE-06 (P2) | 备份后直接覆盖正式 md，无源指纹核对、无写后校验、写失败不转业务异常 | 合并前记指纹，读取后与提交前各核对一次；写失败转 `MergeError`；写后校验 hash | 成立。用真实单页 PDF 验证了失败点确实落在写入/指纹比较上 |
| ISSUE-07 (P2) | 批量入口只捕 `PdfError`，`makedirs` 与最终写盘在 try 外 | 目录创建、转换、正式写入的 `OSError` 都进入单文件失败并继续 | 成立。单件写盘失败后后续文件仍能产出，JSONL 记录失败 |

严重程度评价：ISSUE-01/02 定 P1 合理（直接影响 CLI 可用性与产物可信度）；ISSUE-03 属测试基础设施问题，定 P2 合理；其余 P2 与影响面匹配。

## 3. 补丁复核发现的问题（已修正）

### D-01（P1，验证假绿）合并失败用例并未覆盖新代码

`OcrMergeFailureSafetyTest` 用 4 字节伪 PDF（`b"%PDF-1.4\nsource"`）建夹具。`merge_file()` 在写入前先调 `paddle_ocr.is_complete()`，而它会用 pymupdf 打开这个伪 PDF 统计页数，失败后返回 `False`，于是：

```text
EXC: MergeError OCR 全文不完整或与当前 PDF 不符（sidecar 校验失败）
```

两个用例都在**前置校验**处抛错，`mock` 的写盘失败、源文件变更根本没有执行；即便删掉补丁新增的 `try/except` 与指纹核对，这两个用例依然会过。这类“绿”不能作为 ISSUE-06 的验收证据。

修正：改用 pymupdf 生成的真实单页 PDF（无 pymupdf 时整类跳过），并断言异常信息必须分别是 `写入正式 Markdown 失败`、`合并期间 PDF 发生变化`。

### D-02（P2，行为回归）pypdf 兜底候选失败会让本来可用的结果变成硬失败

补丁在 `quality.assess(md)["state"] != "pass"` 时直接 `candidate = _md_via_pypdf(path)`。若 pypdf 自身报错（加密、结构损坏、依赖异常），`StructuredError` 会一路抛到 `convert_file()`，把一份“review 但可用”的主抽取结果变成整个转换失败；修改前这种文档是能正常产出 md 的。

修正：抽出 `_pypdf_fallback_md()`，候选失败时打警告并保留主结果，仅在候选质量等级更高时替换。形态标签同时保持兼容：空正文仍是 `pypdf(空正文兜底)`，非空低质量才用 `pypdf(质量兜底)`。

### D-03（P2，资源清理）引擎启动期异常绕过 `__exit__`

`__enter__` 的等待循环里，`_next_line()` 在进程提前退出时会抛 `OcrError`，原补丁与基线都不会调用 `__exit__`，`stdin/stdout` 管道保持在 `self.proc` 上直到 GC。ISSUE-01 要求“引擎异常退出时的清理”，这里没闭环。

修正：启动循环包 `try/except`，异常时先 `self.__exit__()` 再抛出；新增用例断言 `engine.proc is None`。

### D-04（P3，错误语义）回滚失败会吞掉根因；指纹读取 `OSError` 会冒 traceback

- `save_text()` 回滚写在 `except` 块内，回滚自身失败时抛出的是回滚异常，调用方看不到原本的“磁盘满”。
- `merge_file()` 新增的 `sha256_file()` 在 PDF 被删除/无权限时抛 `OSError`，`main()` 只捕 `MergeError`，仍会出现 traceback。
- `save_text()` 读源 PDF 指纹同样可能抛 `OSError`。

修正：回滚失败只记警告并 `raise` 原异常；`save_text()` 源读取失败转 `OcrError`；`ocr_merge` 统一经 `_sha256_file()` 转 `MergeError`。

## 4. 残余风险（本次未扩大改动范围）

1. **跨文件提交仍非真原子**：进程在 OCR 全文与 sidecar 两次 `os.replace` 之间被强杀，会留下不匹配的“单边新版本”。`is_complete()` 会拒绝，下一次 OCR 重跑；但不会自动保留上一份完整版本。要彻底解决需版本化目录 + 单一原子指针文件，属于结构性改造，报告已声明。（2026-11-22 P2 已修：v2 版本目录 + 原子指针，见 §8）
2. **合并写后 hash 不匹配不回滚备份**：仅可能出现在文件系统异常写入，`atomic_write` 已显著降低概率；未加自动恢复，避免扩大分支。
3. **批量统计口径**：`by_gate` / `by_backend` 在写盘前就累加，某件写盘失败时仍会计入门禁分布（`failed` 与退出码正确）。属报表口径问题，不影响任务结果。
4. **质量门禁有语义盲区**：能识别过短、乱码、低 CJK，但识别不了“长度足够却语义错乱/重复”的文本；ISSUE-05 的“重复字符占比”等增强未做。
5. **真实引擎只做了单样本冒烟**：未做长时间批量、引擎中途崩溃、磁盘写满等真机故障演练；这些路径目前由故障注入用例覆盖。

## 5. 最终验证矩阵

> 本节是修复提交 `51ae8ca` 时点的矩阵，不代表当前 HEAD。P3 后全量为 **191 条**，P2 OCR 版本化完成后为 **194 条**（完整依赖 194/194、仅 pymupdf+pypdf skips=7、零依赖 skips=26），见 §8。

| 环境 | 命令 | 结果 |
|---|---|---|
| 完整依赖（+`pymupdf4llm`） | `python -m unittest discover` | **186 tests, OK**（0 skip） |
| 仅 `pymupdf`+`pypdf` | 同上 | **186 tests, OK (skipped=5)** |
| 零第三方依赖 | 同上 | **186 tests, OK (skipped=21)** |
| 语法 | `python -m py_compile *.py` | 通过 |
| 补丁规范 | `git diff --check` | 通过 |

5 个 skip 均为明确依赖 `pymupdf4llm` 的端到端用例，与报告结论一致。

补充验证：

- 用 pymupdf 生成 2 页中文 PDF，`convert.py --check` 退出 0，`--apply` 产出 md；`--backend paddle` 真机跑通，生成 `.ocr.txt` 与 `.meta.json`，`is_complete` 通过；`ocr_merge.py` 合并成功并保留备份。
- 原始补丁应用前后均核对过 `git apply --check`，无冲突、无新增第三方依赖、无 CLI 参数变更。

## 6. 本次变更

| 文件 | 变更 |
|---|---|
| `batch_convert.py` | 目录创建移入 try；转换与正式写盘的 `OSError` 纳入单文件失败 |
| `convert.py` | `check_file()` 读取异常转稳定错误；修正结构化兜底注释 |
| `ocr_merge.py` | 源 PDF 指纹前后核对、写失败转 `MergeError`、写后 hash 校验、指纹读取异常收敛 |
| `paddle_ocr.py` | 子进程启动/读写/退出异常统一转 `OcrError`；启动期收尾；`save_text` 快照回滚、写后校验、源读取异常收敛、回滚不吞根因 |
| `structured.py` | 质量门禁兜底与候选失败隔离；空正文标签保持兼容 |
| `test_merge.py` | 依赖跳过的修正；合并用例改真实 PDF；新增 6 个故障注入用例（兜底候选失败、启动清理、回滚失败、源不可读等） |
| `CHANGELOG.md` / `README.md` | 记录修复与兜底口径变化 |

## 7. 建议的后续动作

1. 若业务要求“跨进程崩溃也必须保留上一份完整 OCR”，单独排期做版本化产物 + 原子指针，不建议在现有双文件结构上继续加锁。
2. 在具备生产 OCR 引擎的机器上跑一批真实扫描件（≥50 份），观察 `resumed`、`is_complete` 拒绝率与门禁分布，再评估是否需要语义级质量评估。
3. 批量报表可把门禁计数移到写盘成功之后，消除“失败件也进门禁分布”的口径歧义。

## 8. 文档一致性复核（2026-11-22）

### 已修正的不一致

| 文档 | 不一致 | 处理 |
|---|---|---|
| `README.md` | 测试数仍写 169（`test_convert` 113 + `test_merge` 56），实际 116 + 70 = 186；清单漏了 `QuestionHeadingTest` / `PackagingTest` 与新增故障用例类 | 更新总数与分项，补上 `QuestionHeadingTest`(17)、`PackagingTest`(3) 和 8 个新增用例类，清单合计 186 |
| `README.md` | 结构化兜底仍写“空正文且 pypdf 可用” | 改为“质量门禁未 pass 时尝试，仅等级提升才采用” |
| `README.md` | Changelog 徽章 `v0.1.0`，正文写 0.1.1 | 徽章改为 `v0.1.1` |
| `README.md` / `requirements.txt` | 文档写 Python 3.11+，`pyproject.toml` 写 `>=3.9` | 统一为“元数据 ≥3.9；开发与 CI 验证 3.11 / 3.13” |
| `requirements.txt` | `pypdf` 仍限定“仅空正文兜底” | 改为“空正文/乱码/低中文等质量不达标时兜底” |
| `README.md` §四 | 未写 OCR 全文与 sidecar 的跨文件事务边界 | 补一条风险说明（强杀后 `is_complete` 拒绝并重跑） |
| `CODE-REVIEW-2026-11-21.md` | 历史快照里的 169 条测试、“无 commit / 无 remote”、pypdf 未实测已过期 | 顶部加时效说明；两条“下一步”标注 2026-11-22 已完成 |
| `paddle_ocr.save_text` docstring | 仍称“原子落盘”，未说明双文件非事务 | 补充回滚与强杀边界 |

### 当前待办（P2 版本化 / P3 处理后更新，2026-11-22）

> 下表 P3 五项与「OCR 产物版本化」已在同日完成并随本轮提交；§4 的 186 条测试矩阵是修复提交 `51ae8ca` 时点的记录，当前全量为 **194 条**（完整依赖 194/194、仅 pymupdf+pypdf skips=7、零依赖 skips=26）。

| 优先级 | 事项 | 状态 / 说明 |
|---|---|---|
| P2 | OCR 产物版本化（目录 + 原子指针），覆盖两次替换之间被强杀的场景 | 已完成：`<pdf>.ocr/versions/<version>/text.txt` + `meta.json`，`<pdf>.ocr.txt.meta.json` 为原子指针；指针提交后崩溃仍读新版本，指针写失败保留上一版；`.ocr.txt` 为可落后镜像。v1 老布局只读兼容 |
| P2 | 真实语料批量回归（≥50 份，含引擎崩溃、磁盘写满） | 未做。本轮只有 2 页真机冒烟 + 故障注入用例 |
| P3 | 质量门禁语义增强（重复字符占比、低信息密度） | 已完成重复堆叠判据：仅统计 CJK 的 4+ 连续同字，占比 ≥30% 判 review；低信息密度未单列，仍由长度/CJK 判据覆盖 |
| P3 | 批量门禁计数移到写盘成功之后 | 已完成；写盘失败件不进入 `by_gate` / `by_backend`，并有回归用例 |
| P3 | CI 增加 push / PR 触发的测试工作流 | 已完成 `.github/workflows/ci.yml`：main push 与 PR，Ubuntu/Windows × 3.11/3.13，安装 `.[full]` 后跑 unittest |
| P3 | `pypdf` 的 `logging` 全局降级不恢复（历史复核第 12 项） | 已完成；`_md_via_pypdf` 用 `try/finally` 恢复原 level，并有 logger 恢复用例 |
| P3 | 结构化 `--strict` 拦下正文时配图已落盘（历史复核第 13 项） | 已完成；只回收本次新建图与本次新建的空目录，旧图保留；dry-run 同步关闭配图写入 |
| — | `CHANGELOG` 的 `Unreleased` 尚未定版本；`dist/` 仍是 0.1.1 旧构建 | 发布前需定版本并重建 |
| — | README §六“未做·后续”列的是产品边界（内置路径不抽图/不还原排版、OCR 后处理纠错、新 PDF 生成器扩展） | 不是缺陷，按需求再排期 |

### 代码里的 TODO 标记

`TODO / FIXME / XXX / HACK` 扫描结果：**没有待办注释**。唯一命中是 `paddle_ocr.run()` 的局部变量名 `todo`（待 OCR 的页列表），不是 TODO 项。
