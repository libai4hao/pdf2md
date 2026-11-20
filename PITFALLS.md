# pdf2md 踩坑记录

本文件沉淀把一批 11 份 PDF 转 Markdown 过程中实际踩过 / 验证过的坑。每坑固定格式：**现象 → 根因 → 证据 → 正确写法 → 如何避免**。证据尽量落到 `convert.py` 行号或可复跑命令。

脚本：[convert.py](convert.py)　测试：[test_convert.py](test_convert.py)　用法：[README.md](README.md)

坑 1–9 来自首轮实现与真实语料验证；坑 10–13 来自独立代码审查后的实证复核（含变异测试）；坑 14–17 来自「按真实产物量化噪声率、再改规则」的第三轮迭代；坑 18 来自 145 份面试题批量转换时发现的「伪文字层」；坑 19–25 来自 2026-11-21 合并上游知识库管线的 PDF→MD 能力（结构化抽取 / 页级 OCR / 门禁 / 合并）时的实测。

---

## 坑 1：10/11 的 PDF 里根本没有文字层，`extract_text()` 返回 0 不是工具坏了

- **现象**：`pypdf` 的 `extract_text()` 对 11 份里有 10 份返回 `text=0`；`pdftotext` 也只吐 1 个换行。直觉是「库不支持中文」，于是想换 PDF 库。
- **根因**：这 10 份是 Chrome 打印出来的整页长截图（`/Producer (Skia/PDF m66)`），每份单页、单张 `/Subtype /Image`、`FlateDecode` + `DeviceRGB` + 8bit、宽恒 540 px、高 5313–20000 px，`/Type /Font` 计数为 **0**。文件里根本没有字形，任何文字抽取库都抽不出字。
- **证据**：逐份统计 `img=1 font=0 tounicode=0`；`py convert.py --check` 对它们全部判为「形态：截图OCR」。唯一有文字层的是 `中国制造2025.pdf`：21 页、`/Type0` + `/CIDFontType2` + `Identity-H`、`/Font` 12、`/ToUnicode` 2。
- **正确写法**：先判形态再选路径。`kind_of()`（`convert.py:177`）用 `/Subtype\s*/Image` 与 `/Type\s*/Font` 的组合分三态：`image` / `text` / `unknown`；`unknown` 直接报错退出，不猜。
- **如何避免**：拿到「抽不出文字」的 PDF，先数 `/Font` 与 `/Subtype /Image`，别先怀疑库。测试 `test_kind_of_image` / `test_kind_of_text` / `test_kind_of_unknown` 锁死三态。

---

## 坑 2：手写 PNG 时漏掉每行前置的 filter 字节，图是花的

- **现象**：把 `zlib` 解出的裸 RGB 直接当 IDAT 数据塞进 PNG，OCR 输出乱码或空。
- **根因**：PNG 的 IDAT 解压后是「逐行 = 1 个 filter 类型字节 + 该行像素」，不是纯像素数组。漏了 filter 字节，解码器会把每行第一个像素当 filter 类型，整幅图错位。
- **证据**：`png_bytes()`（`convert.py:238`）里 `rows = b"".join(b"\x00" + rgb[y*w*3:(y+1)*w*3] for y in range(h))`；`\x00` 即 filter type 0（None）。`test_png_structure` 断言魔数 + IHDR/IDAT/IEND 齐全，`test_ihdr_dimensions` 校验 `>IIBBBBB` 里的宽高字段。
- **正确写法**：IDAT 前先按行拼 filter 字节；`IHDR` 用 `struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)`（8 bit、color type 2 = RGB）；CRC 用 `zlib.crc32(body) & 0xFFFFFFFF`。
- **如何避免**：PNG 是最小可手写的二进制格式之一，写的时候严格按 chunk 结构走，别「看着像」就拼。

---

## 坑 3：`OcrEngine.MaxImageDimension = 10000`，540×20000 的整页图直接报错

- **现象**：把整页长图丢给 WinRT OCR，报 `The parameter is incorrect. Image dimensions are too large!`，OCR 一个字都不出。
- **根因**：`[Windows.Media.Ocr.OcrEngine]::MaxImageDimension` 实测是 **10000**，而这批图最高 20000 px。整图超限。
- **证据**：`slice_plan(h, step=1800, overlap=60)`（`convert.py:258`）；`540×20000` → 12 片，`540×5313` → 4 片。`test_tall_image_slices_with_overlap` 断言相邻片有 60 px 重叠，`test_last_slice_not_beyond_height` 断言末片不越界。
- **正确写法**：按高度切片，片间留 **60 px 重叠**——不留重叠会把恰好跨界的行切成两半，两个半行都认错；留太多则重复内容变多。片文件名 `slice_%02d.png` 保证 `Sort-Object Name` 顺序即从上到下。
- **如何避免**：任何「喂给有尺寸上限的引擎」的场景，先查 `MaxImageDimension` 这类硬限制，再定切片策略。

---

## 坑 4：拿不到单词坐标，段落边界只能靠文本特征猜

- **现象**：想用 `OcrLine.Words` 的边界框判断「这行是不是新段落开头」，结果取不到坐标。
- **根因**：PowerShell 5.1 下 WinRT 的投影类型不完整。实测三种取法全失败：
  - `$line.Words[0].BoundingRect` → 返回 `System.Object[]`（不是 `Windows.Foundation.Rect`）；
  - `[Windows.Foundation.Rect]$line.Words[0].BoundingRect` → `Cannot convert the "System.Object[]" value of type "System.Object[]" to type "Windows.Foundation.Rect"`；
  - `$line.Words.GetAt(0)` → `Method invocation failed because [Windows.Media.Ocr.OcrWord] does not contain a method named 'GetAt'`。
- **证据**：`reflow(lines, median=None)`（`convert.py:419`）只用两个文本特征，不碰坐标：行以 `TERM_PUNCT = "。！？…”」』；!?：."` 收尾 → 断段；`len(ln) <= median * SHORT_LINE_RATIO(0.62)` → 断段。中位行宽用 `statistics.median` 现算，不写死。拼行由 `_join_line(buf, ln)` → `_needs_space(left, right)` 决定要不要补空格：中文直接接上，英文/数字之间补空格。
- **正确写法**：承认拿不到坐标，用「终止标点 + 相对行宽」做启发式，并在 MD 注记里如实写明「段落边界为启发式推断」。`test_sentence_end_breaks` / `test_continuation_joined` / `test_short_line_breaks` / `test_tail_is_flushed` / `test_english_continuation_keeps_space` / `test_english_sentences_kept_apart` / `test_cjk_continuation_has_no_space` 七条锁住行为。
- **如何避免**：不要把「拿不到的能力」当默认可用。先写探针确认 API 真的返回值，再决定架构；已知缺陷写进注记，不装成精确结果。

---

## 坑 5：OCR 在每个汉字之间插空格，一刀切去空格会毁掉英文

- **现象**：OCR 结果是 `只 加 两 行 代 码 ， 为 什 么 要 用 两 天`，直接拿去渲染很难读。
- **根因**：中文 OCR 引擎按「每个字符一个 token」输出，token 间补空格；英文单词内部不能拆，所以不能 `re.sub(r'\s+', '', s)` 一刀切。
- **证据**：`collapse_cjk_space(s)`（`convert.py:283`）做五轮 `re.sub` 并循环到稳定：CJK↔CJK、CJK↔数字、数字↔CJK、CJK↔`SYM`、`SYM`↔CJK。`CJK = r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]"`。`test_english_spaces_kept` 断言 `this is a test` 保持原样，`test_cjk_spaces_removed` 断言 `只 加 两 行` → `只加两行`，`test_mixed_line` 覆盖 `代码 行数 = 工作量` → `代码行数=工作量`。
- **正确写法**：只折叠「CJK 与相邻字符之间」的空格；纯 ASCII 之间的空格一律保留。因为折叠会产生新的邻接（`a ， b` → `a，b` 后又出现 `a，` 与 `，b`），所以要**循环到稳定**而不是跑一遍。
- **如何避免**：清洗规则要写「保留什么」而不是「删掉什么」；对 CJK 混合文本，一条 `\s+ → ''` 是一定会出事的。

---

## 坑 6：`shutil.which('pdftotext')` 返回 PATH 上第一个，而它是坏的

- **现象**：`中国制造2025.pdf` 预检失败，报 `log4cxx: setFile(%LOCALAPPDATA%\MiKTeX\miktex\log/pdftotext.log,true) call failed.` 与 `Sorry, but <MiKTeX 安装目录>\pdftotext.EXE did not succeed.`，`exit -1073740791`。
- **根因**：本机 PATH 上 MiKTeX 自带一份 `pdftotext.EXE`，它一启动就崩（log4cxx 初始化失败）。第一版 `find_pdftotext()` 只做 `shutil.which` + `os.path.isfile`，直接返回了这个坏的。
- **证据**：`<MiKTeX 安装目录>\pdftotext.exe` 手动跑 `-v` → `exit=-1073740791`；`C:\Program Files\Calibre2\app\bin\pdftotext.exe` 跑 `-v` → `pdftotext version 25.11.0`、`exit 0`。修好后 `--check` 打出 `pdftotext：C:\Program Files\Calibre2\app\bin\pdftotext.exe`、424 行 / 22947 字符、残留 `[]`。
- **正确写法**：**找到之后真跑一次验证**。`pdftotext_works(exe)`（`convert.py:526`）跑 `[exe, "-v"]`，判据是 `returncode == 0 and "pdftotext" in (stderr + stdout).lower()`，`OSError` / `SubprocessError` 一律 `False`。`find_pdftotext()`（`convert.py:544`）候选序列 = `PDFTOTEXT` 环境变量 → `shutil.which` → `PDFTOTEXT_CANDIDATES`，逐个 `isfile` + `pdftotext_works` 过滤，全坏返回 `None` 并给出「请安装 poppler 或用 PDFTOTEXT 指定」的错误。
- **如何避免**：外部程序探测不要以「文件存在」为终点，要跑一次最小命令确认它真的能工作。`test_broken_exe_on_path_is_skipped` 用假 `which` 返回坏路径、假 `pdftotext_works` 只认 Calibre 那份，锁死「必须跳过坏的继续找」。

---

## 坑 7：`zlib.decompress` 遇到流尾填充会抛错，解图直接失败

- **现象**：`extract_image` 偶尔抛 `zlib.error`，同一份文件换机器就好、换机器就坏。
- **根因**：PDF 的 `FlateDecode` 流在 `endstream` 之前可能带多余换行/填充字节，`zlib.decompress` 严格要求流完整闭合，多一个字节就报错。
- **证据**：`_inflate(blob)`（`convert.py:189`）用 `zlib.decompressobj()` → `decompress()` + `flush()`，容忍尾部多余；`extract_image`（`convert.py:225`）把 `zlib.error` 转成返回 `None` 而不是崩。
- **正确写法**：解 PDF 流用 `decompressobj()` 的增量接口，别用一次性的 `zlib.decompress`；解完**必须校验 `len(pix) == w*h*3`**——字节数对得上才说明确实是 DeviceRGB 8bit 未压缩像素，对不上就是色彩空间/位深不匹配，宁可返回 `None` 报错也不要拿花屏数据去 OCR。
- **如何避免**：`test_size_mismatch_returns_none` 与 `test_no_image_returns_none` 锁住「宁可拒绝也不猜」。

---

## 坑 8：噪声过滤写成子串删除，会把正文整段删掉

- **现象**：第一版把「关注」「点赞」这类词做 `line.replace(hint, '')`，结果正文里出现「关注这个问题的人很多」的整句被削掉一半。
- **根因**：PDF/OCR 出来的正文是连续文本，提示词（关注/点赞/二维码/公众号/阅读原文）会合法地出现在句子中间；按子串删必然误伤。
- **证据**：`is_noise_line(line)`（`convert.py:300`）的判据是「把提示词整片抠掉后，剩下的字符只剩标点/括号/空白」—— `关注`、`点赞!!`、`【关注】`、`扫码 关注` 全判噪声，`关注这件事很久了`、`分享经济`、`微信小程序开发`、`公众号运营笔记`、`点赞之交` 全部保留。`test_body_line_with_hint_kept` 断言含「关注」的长句必须保留，`test_whole_line_noise_dropped` 断言孤立的「关注」按钮文字被丢，`test_short_but_meaningful_line_kept` 断言五个短正文不被误杀。
- **正确写法**：判断单位是**整行**，判据是「抠掉提示词后只剩标点/空白才丢」，**绝不做子串替换**。正文行里嵌的广告（`下载 App 注册登录`、`打开 App 查看更多评论查看全部249个回答`）交给 `strip_inline_noise`（`convert.py:327`）用**具体短语**剥离，不用单字短词。`test_body_line_with_hint_kept` 断言含「关注」的长句必须保留，`test_whole_line_noise_dropped` 断言孤立的「关注」按钮文字被丢，`test_short_but_meaningful_line_kept` 断言五个短正文不被误杀，`InlineStripTest.test_body_untouched` / `test_url_not_mangled` 断言行内剥离不伤正文与 URL。
- **如何避免**：去噪的规则越像「替换」越危险。把规则写成「整行是否等于噪声」而不是「行内是否含噪声」，并配一条「正文含提示词必须保留」的反向测试。

---

## 坑 9：拼行时直接 `buf += ln`，英文会被粘成一个词

- **现象**：`reflow(["This is a long english paragraph that has no", "terminating punctuation so it continues"])` 得到 `...that has noterminating punctuation...`；`["This is a complete sentence.", "Another one."]` 得到 `This is a complete sentence.Another one.`。
- **根因**：`buf += ln` 无条件直接拼接。中文不用空格分词所以看不出来，英文续行之间必须补空格；另外 `TERM_PUNCT` 里漏了 ASCII 句点 `.`，导致英文句子永远不断段。
- **证据**：修复前实测输出 `['This is a long english paragraph that has noterminating punctuation so it continues']` 与 `['This is a complete sentence.Another one.']`，两条新测试同时 RED（`AssertionError: Lists differ`）。修复后 `Ran 50 tests / OK`；11 份真实语料重跑，4 份字符数微增（如 `99的人都不知道…` 6267→6283），残留仍全 `[]`。
- **正确写法**：`_join_line(buf, ln)`（`convert.py:463`）调 `_needs_space(left, right)`（`convert.py:442`）判断：任一侧是 CJK → 不加空格；任一侧是空白 → 不加；任一侧是全角标点 → 不加；其余（英文/数字相接）**加空格**。`TERM_PUNCT` 补上 `.`。
- **如何避免**：凡「把多行拼成一段」的实现，必须显式回答「这里要不要空格」，而不是默认无空格。中文场景能过审不代表英文场景能过；用中英双语各写一条测试。

---

## 坑 10：PNG 断言「结构合法」等于没断言，像素全丢照样绿

- **现象**：`PngBytesTest` 50 条全绿，但把 `png_bytes` 里造扫描线的那行改成 `rows = b""`（像素全丢），测试**依然全绿**；`color type` 从 2（RGB）改成 0（灰度）也全绿。真实后果：OCR 产出 0 行、CLI 退 0、静默交付空文件。
- **根因**：断言只覆盖了「有魔数 + 有 IHDR/IDAT/IEND」和「IHDR 里能读回宽高」。这些在像素为空时**全部成立** —— 结构断言不碰数据，等于给编码器开了空头支票。
- **证据**：变异测试实测 —— `rows = b""` → `Ran 64 / OK`（修复前）；`color type 2→0` → `OK`。补断言后同样两个变异 → `FAILED (failures=1)`。
- **正确写法**：解回 IDAT 比对数据本身。`test_pixels_survive_roundtrip` 用 `zlib.decompress` 取出扫描线，断言 `len(scan) == h*(1+w*3)`、每行首字节是 `0`（filter）、去掉 filter 后与输入逐字节相同；`test_ihdr_color_type_and_depth` 直接读 `out[24]`/`out[25]` 锁死 `8` 与 `2`。
- **如何避免**：写编码器/序列化测试时问一句「把数据换成空会不会仍然通过」。会，就说明断言停在结构层。对二进制产物，**必须解回来比数据**，不能只比头部字段。

---

## 坑 11：`shutil.which` 之外还有一层 —— 子进程异常没接

- **现象**：OCR 超时或 PowerShell 起不来时，CLI 直接打印 Python traceback 而不是「错误：…」，退出码也不是 1。
- **根因**：`subprocess.run(..., timeout=600)` 会抛 `subprocess.TimeoutExpired`，`OSError` 也可能抛；`main()` 只 `except PdfError`，这两类异常穿透到顶。
- **证据**：打桩让 `subprocess.run` 抛 `TimeoutExpired` → `ocr_pdf` 直接漏出 `subprocess.TimeoutExpired`（`LEAKED TimeoutExpired`）。同文件 `pdftotext_works`（`convert.py` 的 `except (OSError, SubprocessError)`）是全文件唯一接对的地方，反倒成了反例。
- **正确写法**：每处 `subprocess.run` 都包 `try: ... except subprocess.TimeoutExpired: raise PdfError(...) except OSError as exc: raise PdfError(...)`，`ocr_pdf` 与 `text_pdf` 各一处；测试 `test_timeout_becomes_pdf_error` 打桩断言抛 `PdfError`。
- **如何避免**：凡是把外部进程拉进来的函数，异常清单要写全：非零退出、超时、启动失败，一个都不能漏。只接「非零退出」是最常见的半截活。

---

## 坑 12：备份不提「已存在」，第二次 `--apply` 用上次产物顶掉原件

- **现象**：对一个已有手写 `.md` 的 PDF 连续跑两次 `--apply`，`.bak` 里第一次是原始手写内容，第二次变成了上一次脚本生成的产物 —— 原始件永久丢失。
- **根因**：`if backup and os.path.exists(out): shutil.copyfile(out, out + ".bak")` 只判断「目标存在」，不判断「备份已存在」。第二次执行时 `out` 是上次产物，于是把真正的原始备份覆盖掉了。
- **证据**：实测 run1 `.bak` = `原始手写内容`，run2 `.bak` = `# a\n\n> 来源：a.pdf…`，`原始内容还在吗: False`。
- **正确写法**：`if backup and os.path.exists(out) and not os.path.exists(out + ".bak")` —— 首次备份即定稿，后续不再动。测试 `test_second_apply_keeps_original_backup` 连跑两次并断言 `.bak` 两轮相同且等于最初的原始内容。
- **如何避免**：「备份」语义是保留时间上最早的那份，不是最近一次的上一份。写备份逻辑时先问：重复执行会不会把备份本身变成被备份对象。

---

## 坑 13：`extract_image` 取「第一个」图片对象，页面上有个 logo 就全废

- **现象**：页面图前面若排着一张小 logo（如 20×20），`extract_image` 返回的却是 logo，OCR 出来一片空，`--check` 仍报 `残留：[]`、退出码 0。
- **根因**：`re.search` 只匹配第一个 `/Subtype /Image`。PDF 里图片对象顺序与页面重要性无关，logo、图标、装饰图都可能排在整页长图前面。
- **证据**：构造「20×20 logo + 540×1000 页图」的 PDF，修复前返回 `(20, 20)`（`AssertionError: Tuples differ: (20, 20) != (540, 1000)`）；修复后返回 `(540, 1000)` 且像素与页图一致。
- **正确写法**：`_image_candidates(raw)` 用 `re.finditer` 扫出全部可解图片，`extract_image` 取**面积最大**（`w*h`）的那张。同时把 `/ColorSpace /DeviceRGB` 从「有声明且非 RGB 才拒」改为**必须显式声明**，避免灰度/CMYK 被按 3 字节解出错位像素。
- **如何避免**：「取第一个匹配」在结构化的容器格式里几乎总是错的。PDF、ZIP、MHTML 都可能有多个候选，得按语义（面积、长度、类型）挑，不能按下标。

---

## 坑 14：噪声规则不按真实产物量误杀面，越写越宽直到吃掉正文

- **现象**：加一轮噪声规则后 `--check` 报的残留行数从 169 降到 31，看起来大获成功；但同一批产物里正文段落也少了，而测试全绿、无人发现。
- **根因**：规则是照「长什么样像广告」写的，没有先回答「什么样的正文会长成这样」。典型两例：只要求「关注」就会命中 `交叉关注点`、`这些关注点`、`新材料制备关…`；只要求 `\d+ / \d+` 就会把网速换算算式 `20 / 8` 当页码删掉。
- **证据**：先把 11 份真实产物按行分桶（`body`/`ui`/`rec`/`hot`/`digit`/`pageno`/`ad`）量出 16% 可丢率，再逐条量误杀面：全书「同时含 关注+回答」44 行、「只含 关注」6 行（**全是正文**）、页码分母分布 `{'8': 1, '21': 14}`、纯数字连续串只有 3 处（`[03]`×4、`[04]`×5、`[07]`×3）。据此把规则改成「关注+回答同现」「同分母 ≥3 次」「连续 ≥3 个」后，误杀面归零。
- **正确写法**：每条删除规则配一条反向测试锁住「不能删的那类」。现有五组配对：`NoiseRuleTest.test_recommend_line_dropped` ↔ `test_body_mentioning_attention_kept`、`PageNumberTest.test_dropped_when_denominator_repeats` ↔ `test_kept_when_denominator_unique`、`DigitRunTest.test_run_dropped` ↔ `test_isolated_digit_kept` / `AdAndFragmentTest.test_table_first_column_digit_kept`、`AdAndFragmentTest.test_ad_line_ending_with_badge_dropped` ↔ `test_body_mentioning_ad_kept`、`test_ocr_fragments_dropped` ↔ `test_short_body_word_kept`。
- **如何避免**：先量化再写规则。噪声清理的效果不能只看「删了多少」，必须同时看「留下的是不是正文」——用行集差比对时注意 `reflow` 会移动行边界，**差集里出现的长正文行很可能是被合并进别的段落**，必须去新产物里搜原文片段确认，不能凭差集断言丢失。

---

## 坑 15：单关键词判定必然误杀，判据要写成「同现」而不是「之一」

- **现象**：第一版推荐区正则只要命中「关注」就删行，`交叉关注点`、`这些关注点`、`跨领域的关注点`、`新材料制备关…` 四行正文被静默删掉；另一处 `[08] 你是谁啊？为啥在我关注列表里？[调皮]打开 App 查看更多评论查看全部263个回答` 也被整行误删。
- **根因**：推荐区行与正文行的区别不在「含不含某个词」，而在「是否同时具备两个以上结构特征」。单关键词是必要不充分条件。
- **证据**：`RECOMMEND_RE`（`convert.py:53`）写成 `[0-9０-９．.I l|]{1,8}\s*[Kk]?\s*关注\s*[·。，,.]?\s*[0-9０-９．.I lKk]{1,8}\s*回答` —— 必须「数字量级 + 关注 + 分隔符 + 数字量级 + 回答」同现。字符类里放 `I`/`l`/`|` 是因为 OCR 把 `3.1K` 认成 `3． IK`、`1.1K` 认成 `I.IK`、`36.0K` 认成 `36． OK`，不放容错会漏 2 行。
- **正确写法**：能写成「A 且 B」就不写「A 或 B」。同现条件越具体，越不需要靠阈值/长度兜底。
- **如何避免**：写关键词规则时先跑一遍「只命中 A 的行」全列出来 —— 如果里面混着正文，就必须加上 B。

---

## 坑 16：页码规则只靠正则形态，会删掉算式；必须按「同分母重复」判

- **现象**：想清掉 `中国制造2025.pdf` 的页脚 `1 / 21`…`21 / 21`（14 行），但同一批里另一份截图样本有一行 `20 / 8`，周围是 `1 0 / 8`、`4 / 8 = 0．`、`8 / 8 = 1．0`、`100 / 8 =` —— 那是网速换算算式，删掉就丢了正文。
- **根因**：`^\d+ / \d+$` 这个形态在「页脚」和「算式」里完全一样，单看一行无法区分。
- **证据**：全库 `PAGE_RE` 命中 15 行，分母分布 `{'8': 1, '21': 14}`。页脚必成组出现（同一份文档分母恒定），算式是孤例。
- **正确写法**：`drop_page_numbers(lines)`（`convert.py:339`）先统计分母频次，`bad = {d for d, n in denoms.items() if n >= PAGE_REPEAT}`（`PAGE_REPEAT = 3`），只有分母重复 ≥3 次的才整组删；无 `bad` 时直接原样返回。
- **如何避免**：形态规则遇到「同一文档内重复出现的结构」时，把「重复」本身当成判据 —— 页脚、页眉、水印都是成组出现的，正文里的算式/编号是孤例。

---

## 坑 17：结构型噪声只能整段截断，行级规则看不见「连着 10 段都是问句」

- **现象**：行级规则清理后，10 份产物的**末尾**还留着一整块知乎推荐区：`05` 末尾 7 段、`09` 5 段、`10` 6 段、`08` 3 段、`06` 3 段全是问题标题（`男生的哪个瞬间让你感觉特别绅士？`）；`07` 末尾 22 段从 `0今日推荐阅读` 开始的整片广告；`03` 末尾 6 段从 `程序汪往期精彩文章` 开始。每一段单独看都像正常中文。
- **根因**：`is_noise_line`（`pdf2md/convert.py:300`）判的是「这一行本身像不像噪声」。信息流推荐位的特征是**结构**——末尾连着好几个短问句、或某个标题词之后全是目录——单看一行什么也看不出来。
- **证据**：11 份产物尾部逐段 dump 后确认：末尾连续段中，问题标题的形状是「≤40 字 + 问号收尾」或「关注·回答」计数行；正文段落中位数 80+ 字，极少以问号结尾且连续 ≥3 段。`07` 的锚点词命中在第 40 段，正好砍掉整片 22 段广告。
- **正确写法**：`truncate_feed_tail(paragraphs)`（`pdf2md/convert.py:393`）放在 `reflow` **之后**（`_paras_from_lines` 在 `pdf2md/convert.py:608`）。两把刀：①锚点截断 —— 段落含 `今日推荐阅读`/`往期精彩文章`/`QQ 浏览器热搜榜`/`限时免费领` 且在全文后半段，从该段起全丢；②尾部连跑截断 —— 末尾连续 **≥3 段**（`FEED_TAIL_MIN_RUN`）都是信息流标题形状才砍，2 段不动。
- **如何避免**：行级规则做完后，**把产物尾部单独打出来看一遍**。噪声清理的正则永远只管「一行」，而推荐位、目录、热搜榜都是「一块」。另外注意两个边界：锚点词只在**后半段**才触发（前半段出现往往正是在讲这件事），连跑阈值取 3 不取 2（正文里的连续设问也就 2 句）。这条规则也遵守「宁可放过不肯错杀」——`05` 号末尾仍有 `房租水电都是钱，成年人的世界还真是要靠自己` 这种无问号短句打断连跑、整块没截干净的残留，是刻意接受的代价。

---

## 坑 18：有 `/Type /Font` 不等于有可用文字层 —— 「伪文字层」会抽出 0 字符

- **现象**：一份 7.7 MB、48 页的 `考试题库.pdf` 被 `kind_of()` 判为文字层，但 `pdftotext` 抽出 **0 字符**，`process_file()` 只打印一句「正文为空」就产出 65 字节的空 MD。而它是本批 145 份里**唯一**的空产物。
- **根因**：`kind_of()`（`convert.py`）用「PDF 里有 `/Type /Font`」当文字层的判据。但这份 PDF 是**每页一张整图**（pypdf 复核：48 页 `extract_text()` 全 0、每页 `images=1`），`/Font` 字典来自水印/表单/校验层 —— 字体对象存在，字形却没用来排版正文。`/Font 存在` 是「可能有文字层」的必要不充分条件。
- **证据**：pypdf 交叉验证 48 页 `len(extract_text())==0`、`len(pg.images)==1`；`extract_image(raw)` 返回 `None`（项目只解「单张超长整页图」，不处理多页文档）；`pdftotext -layout` 输出 0 有效行。改用 `pdftoppm -png -r 120` 逐页渲染 + WinRT OCR 后，产出 1128 行 / 40582 字符的真实内容，写盘 69 KB。
- **正确写法**：文字层路径抽空时**降级 OCR 而非静默产出空正文**。两处改动：①`text_lines_or_empty(path)` 把 `text_pdf()` 的 0 字符情形返回成 `(行, empty=True)` 而不抛异常；②`ocr_pdf_via_render(path, dpi=RENDER_DPI)` 用 `find_pdftoppm()` 渲染每页为 PNG（`RENDER_DPI=120`，A4 约 1000×1400px，单页不超 WinRT 10000px 上限故不再切片），重命名成 `slice_NN.png` **复用同一份 `OCR_PS1`**（它扫 `slice_*.png`，天然顺序即页序）。`process_file()` 在文字层 `empty` 时调它兜底。
- **如何避免**：形态判定只是「选哪条主路径」的粗筛，**抽出来的结果本身要再校验一次**。「判为 X 却产出空」永远要当成异常信号，优先降级到另一条路径，而不是 warn 完就放弃。`/Font`、`/ToUnicode` 这些对象计数是启发式不是真相 —— 真正可靠的判据是「抽出来的字符数」。

---

## 坑 19：`py convert.py` 和 `py -c` 可能不是同一个 Python —— 依赖「凭空消失」

- **现象**：同一个目录里，`py -c "import pymupdf"` 正常；`py convert.py --check` 却打出 `结构化抽取：pymupdf=无 pymupdf4llm=无 pypdf=无`，`后端：auto → builtin`。看起来像合并进来的模块没生效。
- **根因**：`convert.py` 首行是 `#!/usr/bin/env python`。Windows 的 `py` 启动器**会读脚本 shebang 选解释器**：`py -c` 用默认 3.11.4（`<Python 安装目录>\python.exe`），带 shebang 的脚本却解析到 PATH 上的另一个 `python.exe`（实测 3.13.18，site-packages 里没有 pymupdf）。能力不是没有，是跑在了错的解释器上。
- **证据**：两个探针脚本只差一行 shebang，打印的 `sys.executable` 分别是 3.13.18 与 3.11.4；`py -3.11 convert.py --check` 立刻显示 `pymupdf=有 pymupdf4llm=有`、`后端：auto → structured`。
- **正确写法**：钉版本跑——`py -3.11 convert.py ...` 或 `py -m convert ...`；装依赖、跑单测也用同一个解释器（`py -3.11 -m unittest`）。
- **如何避免**：`--check` 里新增的「结构化抽取」「PaddleOCR-json」两行就是判据：先看依赖行，再怀疑代码。跨机器部署时别假设 `py` 永远指向你装包的那个 Python。

---

## 坑 20：OCR 文本「文件存在」不等于「这份 PDF 的 OCR 做完了」

- **现象**：重跑页级 OCR 时，`<pdf>.ocr.txt` 非空就直接被复用；哪怕它只覆盖了 2/5 页、或源 PDF 已经被替换过，也会被当成「已完成」，产物来源无法追溯。
- **根因**：把「文件在」「文件非空」当成完成判据。OCR 是慢操作，一旦误复用，错的是整篇 md，而且没有任何信号。
- **证据**：`paddle_ocr.is_complete()` 的校验各自都有单测——`test_partial_pages_not_complete`（页集不全）、`test_tampered_text_not_complete`（正文被追加）、`test_changed_source_not_complete`（源 PDF 被换）、`test_meta_flag_off_not_complete`（`complete` 被改 False）、`test_empty_text_not_complete`（引擎返回空串）。任一条不过都返回 `False`，异常也 `False`（fail closed，含 PDF parser 报错）。
- **正确写法**：落盘全文时同时写 sidecar（`format_version` / `source_sha256` / `total_pages` / `completed_pages` / `complete` / `output_sha256` / `chars` / `body_chars`）。续跑前先验 sidecar：源指纹、页集、输出 hash 三者全对才复用；**判空要用纯正文的 `body_chars`，不能用含页标记的 `chars`**——空页也会让 `chars > 0`，这个坑在写测试时才暴露。
- **如何避免**：凡是「断点续跑」，都要能回答「这份产物属于这份输入、且完整」。只验存在性的续跑，等于把上次的中断变成永久的错误。

---

## 坑 21：OCR 合并会顺手丢掉「配图」段，还会让头部自相矛盾

- **现象**：把 OCR 全文直接覆盖写回 md 后，`## 配图` 里的图片引用没了（结构化阶段抽的图白抽）；头部仍写着 `> 形态：文字层`，正文却是 OCR 结果——文件在骗后来的人。
- **根因**：上游 `ocr_merge` 的 md 没有统一头部、也没有配图段，所以它可以直接 `new_md = ocr_text + 配图段`。这个仓库的 md 有 H1 + 三行 `>` 注记，直接覆盖会把头部一起冲掉。
- **证据**：`test_merge_keeps_head_and_images` 断言合并后 H1 保留、`> 形态：页级OCR（PaddleOCR-json 合并）` 替换旧形态、旧正文消失、`## 配图` 与 `./书.images/a.png` 仍在；`test_second_merge_does_not_overwrite_backup` 断言第二次合并不动首次 `.ocr.bak`。
- **正确写法**：`split_front_matter` 拆头部、`split_images` 拆配图段、`refresh_head` 改写形态/说明，再拼「头部 + OCR 正文 + 配图段」。备份只在没有备份时创建。
- **如何避免**：任何「用 A 覆盖 B 的正文」的合并，都要先问 B 里哪些部分是**别的阶段产出的、不能重算的**（这里是配图段与头部元信息）。

---

## 坑 22：装饰图阈值不能按「重复 2 次」判 —— 短文档的封面会被误杀

- **现象**：一份 2 页 PDF 的封面图在两页都出现。如果按「重复出现即装饰」过滤，唯一一张图就没了；而 6 页文档里出现 4 次的页眉，才应该算装饰。
- **根因**：装饰图（页眉/页脚/水印）确实「重复」，但重复次数必须相对文档规模看；小样本里重复两次可能只是跨页封面/续页图。
- **证据**：`test_decorative_threshold` 锁死 `max(4, 页数//3)`：6 页时阈值 4（4 页重复判装饰）、2 页时阈值仍是 4（2 页重复不判）。
- **正确写法**：`decorative_hashes(page_counts, total_pages)` 用 `limit = max(4, total_pages // 3)`；同时**先过滤 `<80px` 小图再统计**，否则图标会污染 page_counts（`test_tiny_image_skipped` 覆盖入库前过滤）。
- **如何避免**：沿用坑 16 的「重复即结构」，但补一条：**小样本的重复不足以证明是装饰**，阈值要有绝对下限。

---

## 坑 23：结构化 md 不能过 `reflow` 和噪声规则 —— 标题、表格会被拆散

- **现象**：把 `pymupdf4llm` 的产物丢进 `_paras_from_lines()`，`# 第一章` 被当成正文行参与合段、表格行 `| a | b |` 被行内剥离规则碰、列表符号被当成正文符号，层级和表格全毁。
- **根因**：`reflow` / `is_noise_line` / `strip_inline_noise` 是为「OCR 出来的纯文本行」设计的启发式，判据（终止标点、中位行宽、符号表）都不认识 Markdown 结构。
- **证据**：`test_structured_backend_wraps_and_gates` 断言结构化后端只做 `_wrap`（H1 + 注记 + 原样正文），正文里的 `## 一、设计原则` 不被改写；README 三节关键取舍也写了这条。
- **正确写法**：`builtin` 的行文本才进 `_paras_from_lines`；`structured` / `paddle` 走 `_wrap`，正文原样落盘。
- **如何避免**：清洗规则和抽取器绑定，别做「万能后处理」。同一套启发式套在两种结构上，必然一边过杀、一边漏杀。

---

## 坑 24：`pymupdf4llm` 会把正文行升成 `##` 标题 —— 「有 #」不是结构化成功的证据

- **现象**：本机 3 页样例里，11pt 的正文行 `正文段落继续展开说明。` 被输出成 `## 正文段落继续展开说明。`；同一句在每页都成了二级标题。
- **根因**：`pymupdf4llm` 靠字号/位置等版式启发式判标题，短行 + 独占一段 + 字号略大就会被提升；它不是语义判断。
- **证据**：冒烟产物 `样例书.md`（详见 README 第五节「合并后的真实冒烟」）；`test_text_pdf_extracts_pages_and_cjk` 只断言页数与正文片段，不断言标题层级。
- **正确写法**：把「结构化」理解为「保留版式线索」，不是「还原真实层级」。门禁（`quality`）只保证字符质量与 CJK 占比，**不负责层级正确性**；要核对就人工看产物。
- **如何避免**：接入第三方版面抽取器时，把它的启发式边界写进文档，并在测试里只断言「不该丢正文」，不要断言「标题一定对」。

---

## 坑 25：`pdftotext` 可以「退出码 0 + 零输出」—— 空正文直到门禁才暴露

- **现象**：一份文字层 PDF（pymupdf 用 `china-s` 字体写的中文）被 `pdftotext` 抽出 0 行，stderr 全是 `Missing language pack for 'Adobe-GB1'` / `Unknown font tag 'china-s'`，但**退出码是 0**。`text_lines_or_empty()` 判为伪文字层 → 降级渲染 OCR；本机 WinRT 又没产出文本，最终写出 35 字节的头 + 25 可见字符的 md，CLI 仍然退出 0。
- **根因**：`pdftotext` 的「抽不到字」有两个来源——文件真没文字层（坑 18），或字体缺 `ToUnicode`/语言包映射。两者退出码都是 0，只能靠「抽出来多少字符」区分。而下游降级链路再失败时，旧逻辑仍然照写空产物。
- **证据**：MiKTeX 与 Calibre 两版 `pdftotext` 对同一份样例都只吐语法错误、无正文；`convert.py --check` 的三态门禁给出 `不通过（产物过短(25字符)）`，`--strict` 时直接退出 1 且不写盘。
- **正确写法**：把门禁接到写盘前——`review`/`fail` 在 stderr 报出指标（chars / cjk / bad），`--strict` 直接失败；批量用 `--strict` 让「空产物」计入失败数，而不是伪装成功。
- **如何避免**：沿用坑 18 的结论再加一条：**降级链路也要有终点判据**。形态判定、抽取、降级、写盘四个环节，每个都要能对「产出 0 字符」说「不」。

---

## 坑 26：`--check` 曾经不是只读 —— 预检在语料目录里落了 131 张图

- **现象**：对 `<语料目录>` 跑一轮「只读预检」，`<样本A>.images\` 与 `<样本B>.images\` 两个目录凭空出现，共 121 + 10 张 PNG，时间戳全是预检那一刻；`--check --backend paddle` 更狠，会在 266 页的书上整本跑 OCR（约 4 分钟）再写 `.ocr.txt`。文档写着「一字节不写」。
- **根因**：预检复用了 `convert_file()`，而结构化路径的 `_collect_images()` 默认落盘、`paddle` 路径默认「没 sidecar 就跑」。两处「顺手写」在有产物的路径上无害，在**只为看一眼**的路径上就是污染。
- **证据**：语料目录出现两个 `.images/`（121、10 个文件，mtime 与预检同秒），而旧版产物里的 md 并不引用任何图片——纯属新写入；修复后 `test_check_structured_does_not_write_images` 断言 `--check --backend structured` 后目录不存在，同时仍报「配图：保留 1 张」。
- **正确写法**：`structured.extract(..., write_images=False)` 只统计不落盘；`--check` 对 `paddle` 不触发 OCR，只在有完整 sidecar 时读回并评估门禁，否则如实打「未跑」。
- **如何避免**：把「只读」当成接口契约而不是注释——凡是预检/试算入口，都要显式传「不写」开关，并用测试断言**文件系统里没有新文件**（只看返回值不够）。

---

## 坑 27：第三方库直接往 stdout 打日志 —— dry-run 的 md 被插了一行

- **现象**：`py convert.py "中国制造2025.pdf" > out.md` 的产物开头多出 `=== Document parser messages ===` 与 `Warning: RapidOCR is missing...`，md 直接被污染；`--check` 的每一段也被这两行插花。
- **根因**：`pymupdf4llm` 在解析告警时调 `pymupdf.message(...)`，走 PyMuPDF 自己的输出流（`_g_out_message`），不是 `sys.stdout`；`contextlib.redirect_stdout` 拦不住，`pymupdf.TOOLS.mupdf_display_errors(False)` 也拦不住（它管的是 MuPDF 告警，不管 message 流）。
- **证据**：`structured._messages_to_stderr()` 把 message 流临时改到 stderr 后，`stdout` 首行变回 `# 中国制造2025`、388 行、`===` 命中 0，两行提示全部落到 stderr（实跑 2026-11-21）。
- **正确写法**：抽取期间 `pymupdf.set_messages(stream=sys.stderr)`，并在 `finally` 里恢复原流（`_restore_messages`）；同时用 `redirect_stdout(sys.stderr)` 兜住库里普通的 `print()`。
- **如何避免**：任何「stdout 就是产物」的 CLI，都要有一条断言「stdout 能被下游直接消费」——本次是 `1> md 2> err` 分开验证，而不是看合并输出以为什么都没混。

---

## 一句话总结

这批 PDF 的坑集中在十件事：**先判形态再动手**（10/11 根本没文字层）、**拿不到的能力别当可用**（坐标取不到就靠文本特征，并写进注记）、**外部程序要实跑验证**（PATH 上第一个 pdftotext 是坏的）、**过滤规则写「保留什么」而非「删掉什么」**（子串删除必误伤正文）、**拼行要显式回答「要不要空格」**（中文看不出、英文会粘词）、**结构合法 ≠ 数据正确**（PNG 断言不解回像素就等于没断言）、**「第一个」和「最近一次」都是陷阱**（多候选要按语义挑，备份要保最早那份）、**先量误杀面再写规则**（每条删除规则配一条「不能删」的反向测试）、**单关键词是必要不充分条件**（推荐区要「关注+回答」同现，页码要「同分母重复 ≥3」）、**行级规则看不见结构噪声**（推荐位是「一块」不是「一行」，得合段后再整段截）。

新增（坑 18）的一个习惯：**产物为空要当异常而不是终点** —— 形态判定是粗筛，抽出来的字符数为 0 就该换路径，而不是 warn 完交一份空文件。

合并上游管线这轮（坑 19–25）又补了四条：

- **同一个命令不等于同一个解释器**（坑 19）：Windows `py` 会按 shebang 选 Python，依赖装在哪就跑在哪；
- **「完成」必须能自证**（坑 20）：续跑前验源指纹/页集/输出 hash，只验「文件在」等于把上次的中断变成永久错误；
- **覆盖正文前先问「哪些部分不能重算」**（坑 21）：配图段、头部注记是别的阶段的产物，合并时必须原样保留并改写注记；
- **小样本的重复不足以证明是装饰**（坑 22）：装饰图阈值要有绝对下限 `max(4, 页数/3)`；
- **清洗规则要和抽取器绑定**（坑 23）：为 OCR 行文本设计的启发式套到 Markdown 结构上，必然拆坏标题与表格；
- **第三方抽取器的启发式边界要写进文档**（坑 24）：`#` 是版式线索，不是层级真相；
- **降级链路也要有终点判据**（坑 25）：`pdftotext`/OCR 都可能「成功退出但零输出」，门禁必须在写盘前拦住。

代码复核这轮（坑 26–27）补了两条，都关于「看起来没写其实写了」：

- **「只读」要是接口契约**（坑 26）：预检复用转换函数时，必须显式关掉落图/跑 OCR，测试要断言文件系统没有新文件；
- **stdout 就是产物时，任何第三方 `print` 都是 bug**（坑 27）：message 流要显式改道 stderr 并在 finally 恢复。

最有价值的两个习惯：**每个断言都问「把数据换成空/换成反的会不会仍然通过」**（坑 10 就是这样发现 PNG 断言恒真的）；**每次删掉东西都去新产物里搜原文确认**（`reflow` 会移动行边界，凭行集差断言「正文丢失」会连续误判三次）。
