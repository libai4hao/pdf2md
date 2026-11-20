"""pdf2md 回归测试：只用标准库，不依赖本机 WinRT / poppler。"""

import os
import struct
import sys
import tempfile
import unittest
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import convert  # noqa: E402


def make_image_pdf(w, h, rgb, pad_dict=b""):
    """造一个最小可解的「截图型」PDF：单张 FlateDecode DeviceRGB 图。"""
    pix = zlib.compress(rgb, 6)
    head = (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /XObject /Subtype /Image "
        + b"/Width %d /Height %d " % (w, h)
        + b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
        + pad_dict
        + b"/Length %d >>\nstream\n" % len(pix)
    )
    return head + pix + b"\nendstream\nendobj\n%%EOF\n"


def make_text_pdf():
    """造一个带字体字典的假「文字层」PDF。"""
    return (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n"
        b"%%EOF\n"
    )


def _image_obj(w, h, rgb):
    """单独一个图片对象（不含 %PDF 头），便于拼多图 PDF。"""
    pix = zlib.compress(rgb, 6)
    return (
        b"1 0 obj\n<< /Type /XObject /Subtype /Image "
        + b"/Width %d /Height %d " % (w, h)
        + b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
        + b"/Length %d >>\nstream\n" % len(pix)
    ) + pix + b"\nendstream\nendobj\n"


def make_multi_image_pdf(sizes):
    """多张图按顺序排列：[(w, h, rgb), ...]，用来验证「取最大页图」而不是「取第一张」。"""
    body = b"%PDF-1.4\n"
    for w, h, rgb in sizes:
        body += _image_obj(w, h, rgb)
    return body + b"%%EOF\n"


class PdfDetectTest(unittest.TestCase):
    def test_pdf_magic(self):
        self.assertTrue(convert.detect_pdf(b"%PDF-1.4\n..."))
        self.assertFalse(convert.detect_pdf(b"<html></html>"))

    def test_kind_of_image(self):
        raw = make_image_pdf(3, 2, b"\x00" * 18)
        self.assertEqual(convert.kind_of(raw), "image")

    def test_kind_of_text(self):
        self.assertEqual(convert.kind_of(make_text_pdf()), "text")

    def test_kind_of_unknown(self):
        self.assertEqual(convert.kind_of(b"%PDF-1.4\nnothing here\n"), "unknown")


class ExtractImageTest(unittest.TestCase):
    def test_pixels_roundtrip(self):
        rgb = bytes(range(18))  # 3x2x3
        raw = make_image_pdf(3, 2, rgb)
        got = convert.extract_image(raw)
        self.assertIsNotNone(got)
        w, h, pix = got
        self.assertEqual((w, h), (3, 2))
        self.assertEqual(pix, rgb)

    def test_size_mismatch_returns_none(self):
        # 声明 3x2 但只给 3x1 的数据：必须拒绝，不能猜
        raw = make_image_pdf(3, 2, b"\x00" * 9)
        self.assertIsNone(convert.extract_image(raw))

    def test_no_image_returns_none(self):
        self.assertIsNone(convert.extract_image(make_text_pdf()))

    def test_picks_largest_image_not_first(self):
        """页面里 logo 排在页图前面时，必须取最大的那张。

        实测过：取「第一个」会拿到 20×20 的 logo，OCR 出来一片空，
        而 CLI 照样退 0。
        """
        logo = bytes([255] * (20 * 20 * 3))
        page = bytes([7] * (540 * 1000 * 3))
        raw = make_multi_image_pdf([(20, 20, logo), (540, 1000, page)])
        got = convert.extract_image(raw)
        self.assertIsNotNone(got)
        w, h, pix = got
        self.assertEqual((w, h), (540, 1000))
        self.assertEqual(pix, page)

    def test_missing_colorspace_is_rejected(self):
        """没声明 /ColorSpace 时不能默认放行 —— 灰度/CMYK 会被当 RGB 解出错位像素。"""
        rgb = b"\x00" * 18
        pix = zlib.compress(rgb)
        head = (
            b"%PDF-1.4\n1 0 obj\n<< /Type /XObject /Subtype /Image "
            b"/Width 3 /Height 2 /BitsPerComponent 8 /Filter /FlateDecode "
            b"/Length " + str(len(pix)).encode() + b" >>\nstream\n"
        )
        raw = head + pix + b"\nendstream\nendobj\n%%EOF\n"
        self.assertIsNone(convert.extract_image(raw))

    def test_indirect_width_returns_none(self):
        """/Width 5 0 R 这种间接引用解不了，必须返回 None 而不是崩。"""
        raw = (
            b"%PDF-1.4\n"
            b"1 0 obj\n<< /Type /XObject /Subtype /Image /Width 5 0 R /Height 4 "
            b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
            b"/Length 0 >>\nstream\n\nendstream\nendobj\n%%EOF\n"
        )
        self.assertIsNone(convert.extract_image(raw))


class PngBytesTest(unittest.TestCase):
    def test_png_structure(self):
        out = convert.png_bytes(2, 2, b"\x00" * 12)
        self.assertTrue(out.startswith(b"\x89PNG\r\n\x1a\n"))
        for tag in (b"IHDR", b"IDAT", b"IEND"):
            self.assertIn(tag, out)

    def test_ihdr_dimensions(self):
        out = convert.png_bytes(7, 5, b"\x00" * (7 * 5 * 3))
        # IHDR 数据紧跟在 8 字节 magic + 4 字节长度 + 4 字节 "IHDR" 之后
        w, h = struct.unpack(">II", out[16:24])
        self.assertEqual((w, h), (7, 5))

    def test_ihdr_color_type_and_depth(self):
        """必须声明 8bit + color type 2（RGB）。改成灰度也「结构合法」，所以要锁死。"""
        out = convert.png_bytes(4, 3, b"\x00" * (4 * 3 * 3))
        depth, color_type = out[24], out[25]
        self.assertEqual(depth, 8)
        self.assertEqual(color_type, 2)

    def test_pixels_survive_roundtrip(self):
        """解回 IDAT，逐字节核对像素与每行 filter 字节。

        只断言「有 IHDR/IDAT/IEND」在像素全丢时也成立（变异实测过），
        所以必须把扫描线解出来比对。
        """
        w, h = 7, 5
        rgb = bytes(range(w * h * 3))
        out = convert.png_bytes(w, h, rgb)
        i = out.find(b"IDAT")
        n = struct.unpack(">I", out[i - 4:i])[0]
        scan = zlib.decompress(out[i + 4:i + 4 + n])
        stride = 1 + w * 3
        self.assertEqual(len(scan), h * stride)
        # 每行开头的 filter 字节必须是 0（None）
        self.assertTrue(all(scan[y * stride] == 0 for y in range(h)))
        # 去掉 filter 字节后必须与输入像素逐字节相同
        got = b"".join(scan[y * stride + 1:(y + 1) * stride] for y in range(h))
        self.assertEqual(got, rgb)

    def test_rejects_nonpositive_size(self):
        for w, h in ((0, 5), (5, 0), (-3, 4)):
            with self.assertRaises(ValueError):
                convert.png_bytes(w, h, b"")


class SlicePlanTest(unittest.TestCase):
    def test_short_image_single_slice(self):
        self.assertEqual(convert.slice_plan(100), [(0, 100)])

    def test_tall_image_slices_with_overlap(self):
        plan = convert.slice_plan(10756)
        self.assertEqual(len(plan), 7)
        self.assertEqual(plan[0], (0, 1800))
        self.assertEqual(plan[-1][1], 10756)
        for (_, prev_end), (next_start, _) in zip(plan, plan[1:]):
            self.assertEqual(prev_end - next_start, 60)

    def test_last_slice_not_beyond_height(self):
        for y0, y1 in convert.slice_plan(5000):
            self.assertLessEqual(y1, 5000)
            self.assertLess(y0, y1)

    def test_bad_step_overlap_raises(self):
        """step <= overlap 会让 y 不前进、死循环。默认常量下不可达，但要拦住。"""
        for step, overlap in ((60, 60), (10, 60), (0, 0), (-1, 0), (1800, -1)):
            with self.assertRaises(ValueError):
                convert.slice_plan(5000, step=step, overlap=overlap)


class CollapseTest(unittest.TestCase):
    def test_cjk_spaces_removed(self):
        self.assertEqual(convert.collapse_cjk_space("只 加 两 行 代 码"), "只加两行代码")

    def test_cjk_digit_spaces_removed(self):
        self.assertEqual(convert.collapse_cjk_space("用 2 天"), "用2天")

    def test_english_spaces_kept(self):
        self.assertEqual(convert.collapse_cjk_space("hello world"), "hello world")

    def test_mixed_line(self):
        self.assertEqual(
            convert.collapse_cjk_space("代 码 行 数 = 工 作 量"), "代码行数=工作量"
        )


class ReflowTest(unittest.TestCase):
    def test_sentence_end_breaks(self):
        out = convert.reflow(["第一句话。", "第二句话。"], median=20)
        self.assertEqual(out, ["第一句话。", "第二句话。"])

    def test_continuation_joined(self):
        # 首行必须明显长于 0.62 × median(=20) = 12.4，否则会被当成短行断段
        out = convert.reflow(["这是一段还没有说完的比较长的话", "接着说完了。"], median=20)
        self.assertEqual(out, ["这是一段还没有说完的比较长的话接着说完了。"])

    def test_short_line_breaks(self):
        out = convert.reflow(["代码行数 = 工作量", "代码行数 = 价值"], median=20)
        self.assertEqual(len(out), 2)

    def test_tail_is_flushed(self):
        out = convert.reflow(["没有句号的一句话"], median=20)
        self.assertEqual(out, ["没有句号的一句话"])

    def test_empty_lines_skipped(self):
        out = convert.reflow(["", "有内容。"], median=20)
        self.assertEqual(out, ["有内容。"])

    def test_english_continuation_keeps_space(self):
        """英文续行之间必须有空格，直接拼接会得到 noterminating 这种粘词。"""
        out = convert.reflow(
            ["This is a long english paragraph that has no", "terminating punctuation so it continues"],
            median=60,
        )
        self.assertEqual(
            out, ["This is a long english paragraph that has no terminating punctuation so it continues"]
        )

    def test_english_sentences_kept_apart(self):
        out = convert.reflow(["This is a complete sentence.", "Another one."], median=40)
        self.assertEqual(out, ["This is a complete sentence.", "Another one."])

    def test_cjk_continuation_has_no_space(self):
        """中文续行不能插空格（中文本来就不用空格分词）。"""
        out = convert.reflow(["这是一段还没有说完的比较长的话", "接着说完了。"], median=20)
        self.assertEqual(out, ["这是一段还没有说完的比较长的话接着说完了。"])

    def test_cjk_then_digit_has_no_space(self):
        """中文接数字也不该补空格（与 test_continuation_joined 区分）。"""
        out = convert.reflow(["这本书一共讲了十种不同的思考方式以及配套", "2 个练习步骤。"], median=20)
        self.assertEqual(out, ["这本书一共讲了十种不同的思考方式以及配套2 个练习步骤。"])

    def test_closing_quote_after_english_keeps_space(self):
        """英文闭合引号 `’` 后面还有英文词时必须补空格。"""
        out = convert.reflow(
            ["he could not stop thinking about what the professor ‘meant’",
             "by that remark during the lecture on friday morning"],
            median=62,
        )
        self.assertIn("meant’ by", out[0])


class NoiseLineTest(unittest.TestCase):
    def test_whole_line_noise_dropped(self):
        self.assertTrue(convert.is_noise_line("关注"))
        self.assertTrue(convert.is_noise_line("长按识别二维码"))

    def test_body_line_with_hint_kept(self):
        # 正文里正常出现的「分享」二字不能被当成噪声整行丢弃
        self.assertFalse(convert.is_noise_line("他分享了自己十年的工作经验。"))

    def test_normal_line_kept(self):
        self.assertFalse(convert.is_noise_line("代码行数等于工作量吗？"))

    def test_short_but_meaningful_line_kept(self):
        """短、且含提示词，但不是「只剩提示词」的正文不能丢。"""
        for s in ("关注这件事很久了", "分享经济", "微信小程序开发", "公众号运营笔记", "点赞之交"):
            self.assertFalse(convert.is_noise_line(s), s)

    def test_hint_plus_punctuation_still_dropped(self):
        for s in ("关注。", "点赞!!", "【关注】", "扫码 关注"):
            self.assertTrue(convert.is_noise_line(s), s)


class OcrScriptTest(unittest.TestCase):
    def test_script_mentions_winrt_apis(self):
        for token in ("Windows.Media.Ocr", "TryCreateFromUserProfileLanguages", "OcrEngine"):
            self.assertIn(token, convert.OCR_PS1)

    def test_script_sorts_slices(self):
        self.assertIn("slice_*.png", convert.OCR_PS1)

    def test_ocr_available_returns_bool(self):
        self.assertIsInstance(convert.ocr_available(), bool)


class FindPdftotextTest(unittest.TestCase):
    def setUp(self):
        self._which = convert.shutil.which
        self._works = convert.pdftotext_works
        self._env = os.environ.pop("PDFTOTEXT", None)

    def tearDown(self):
        convert.shutil.which = self._which
        convert.pdftotext_works = self._works
        os.environ.pop("PDFTOTEXT", None)
        if self._env is not None:
            os.environ["PDFTOTEXT"] = self._env

    def test_env_var_wins(self):
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as fh:
            fake = fh.name
        os.environ["PDFTOTEXT"] = fake
        convert.pdftotext_works = lambda exe: True
        try:
            self.assertEqual(convert.find_pdftotext(), fake)
        finally:
            os.environ.pop("PDFTOTEXT", None)
            os.unlink(fake)

    def test_env_var_missing_file_ignored(self):
        missing = os.path.join(tempfile.gettempdir(), "no-such-pdftotext.exe")
        os.environ["PDFTOTEXT"] = missing
        convert.shutil.which = lambda name: None
        convert.pdftotext_works = lambda exe: True
        # 环境变量指向不存在的文件时不能原样返回它，要么继续找、要么 None
        self.assertNotEqual(convert.find_pdftotext(), missing)

    def test_broken_exe_on_path_is_skipped(self):
        """PATH 上有个跑不起来的 pdftotext（实测 MiKTeX 那份），必须跳过继续找。"""
        good = r"C:\Program Files\Calibre2\app\bin\pdftotext.exe"
        convert.shutil.which = lambda name: r"C:\tools\MiKTeX\miktex\bin\x64\pdftotext.EXE"
        convert.pdftotext_works = lambda exe: exe == good
        self.assertEqual(convert.find_pdftotext(), good)

    def test_all_broken_returns_none(self):
        convert.shutil.which = lambda name: "/fake/pdftotext"
        convert.pdftotext_works = lambda exe: False
        self.assertIsNone(convert.find_pdftotext())


class PdftotextWorksTest(unittest.TestCase):
    def setUp(self):
        self._run = convert.subprocess.run

    def tearDown(self):
        convert.subprocess.run = self._run

    def test_returns_false_on_nonzero_exit(self):
        class R:
            returncode = -1073740791
            stdout = ""
            stderr = "log4cxx: setFile call failed."

        convert.subprocess.run = lambda *a, **k: R()
        self.assertFalse(convert.pdftotext_works("C:/x/pdftotext.exe"))

    def test_returns_true_on_version_banner(self):
        class R:
            returncode = 0
            stdout = ""
            stderr = "pdftotext version 25.11.0\nCopyright ..."

        convert.subprocess.run = lambda *a, **k: R()
        self.assertTrue(convert.pdftotext_works("C:/x/pdftotext.exe"))

    def test_returns_false_when_launch_fails(self):
        def boom(*a, **k):
            raise OSError("cannot execute")

        convert.subprocess.run = boom
        self.assertFalse(convert.pdftotext_works("C:/x/pdftotext.exe"))

    def test_none_exe_is_false(self):
        self.assertFalse(convert.pdftotext_works(None))


class RenderMarkdownTest(unittest.TestCase):
    def test_header_and_note(self):
        md = convert.render_markdown(
            "某篇文章", ["第一段。", "第二段。"], shape="截图OCR", source="a.pdf"
        )
        self.assertTrue(md.startswith("# 某篇文章"))
        self.assertIn("> 来源：a.pdf", md)
        self.assertIn("> 形态：截图OCR", md)
        self.assertIn("第一段。", md)

    def test_paragraphs_separated_by_blank_line(self):
        md = convert.render_markdown("T", ["甲。", "乙。"], shape="文字层", source="b.pdf")
        body = [p.strip() for p in md.split("\n\n")]
        self.assertIn("甲。", body)
        self.assertIn("乙。", body)

    def test_residual_noise_clean(self):
        md = convert.render_markdown("T", ["甲。"], shape="文字层", source="c.pdf")
        self.assertEqual(convert.residual_noise(md), [])

    def test_residual_noise_detects_markup(self):
        self.assertTrue(convert.residual_noise("<script>x</script>"))
        self.assertTrue(convert.residual_noise("a <div class='x'>b"))
        self.assertTrue(convert.residual_noise("data-v-abc"))


class ProcessFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pdf2md_test_")
        self.pdf = os.path.join(self.dir, "样例.pdf")
        with open(self.pdf, "wb") as fh:
            fh.write(make_image_pdf(3, 2, bytes(range(18))))
        # OCR 依赖本机 WinRT 中文语言包，不进单测：这里用桩替换真实识别
        self._real_ocr = convert.ocr_pdf
        convert.ocr_pdf = lambda raw: ["甲。", "乙。"]

    def tearDown(self):
        import shutil

        convert.ocr_pdf = self._real_ocr
        shutil.rmtree(self.dir, ignore_errors=True)
        for extra in (".md", ".md.bak"):
            p = self.pdf[: -len(".pdf")] + extra
            if os.path.exists(p):
                os.unlink(p)

    def test_dry_run_writes_nothing(self):
        out_md = os.path.join(self.dir, "样例.md")
        result = convert.process_file(self.pdf, apply=False)
        self.assertIsInstance(result, str)
        self.assertFalse(os.path.exists(out_md))

    def test_apply_writes_markdown(self):
        result = convert.process_file(self.pdf, apply=True, backup=True)
        out_md = os.path.join(self.dir, "样例.md")
        self.assertTrue(os.path.exists(out_md))
        with open(out_md, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), result)

    def test_apply_backs_up_existing(self):
        out_md = os.path.join(self.dir, "样例.md")
        with open(out_md, "w", encoding="utf-8") as fh:
            fh.write("# 旧内容\n")
        convert.process_file(self.pdf, apply=True, backup=True)
        bak = out_md + ".bak"
        self.assertTrue(os.path.exists(bak))
        with open(bak, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "# 旧内容\n")

    def test_no_backup_flag(self):
        out_md = os.path.join(self.dir, "样例.md")
        with open(out_md, "w", encoding="utf-8") as fh:
            fh.write("# 旧内容\n")
        convert.process_file(self.pdf, apply=True, backup=False)
        self.assertFalse(os.path.exists(out_md + ".bak"))

    def test_bad_pdf_raises(self):
        bad = os.path.join(self.dir, "坏的.pdf")
        with open(bad, "wb") as fh:
            fh.write(b"%PDF-1.4\n%%EOF\n")
        with self.assertRaises(convert.PdfError):
            convert.process_file(bad)

    def test_second_apply_keeps_original_backup(self):
        """连续两次 --apply，第一次的原始件不能被第二次的产物顶掉。"""
        out_md = os.path.join(self.dir, "样例.md")
        with open(out_md, "w", encoding="utf-8") as fh:
            fh.write("# 原始手写内容\n")
        convert.process_file(self.pdf, apply=True, backup=True)
        with open(out_md + ".bak", encoding="utf-8") as fh:
            first_bak = fh.read()
        convert.process_file(self.pdf, apply=True, backup=True)
        with open(out_md + ".bak", encoding="utf-8") as fh:
            self.assertEqual(fh.read(), first_bak)
        self.assertEqual(first_bak, "# 原始手写内容\n")

    def test_timeout_becomes_pdf_error(self):
        """超时不能漏成 traceback，要转成 PdfError（否则 CLI 退出码与提示都不对）。"""
        real = convert.subprocess.run

        def boom(*a, **k):
            raise convert.subprocess.TimeoutExpired("powershell", 600)

        convert.subprocess.run = boom
        try:
            with open(self.pdf, "rb") as fh:
                raw = fh.read()
            # setUp 把 ocr_pdf 换成了桩，这里要用真函数
            with self.assertRaises(convert.PdfError):
                self._real_ocr(raw)
        finally:
            convert.subprocess.run = real

    def test_oserror_on_read_becomes_pdf_error(self):
        """读盘失败（权限/占用）也要转成 PdfError。"""
        import builtins

        real = builtins.open
        orig_pdf = self.pdf

        def boom(path, *a, **k):
            if isinstance(path, str) and path == orig_pdf and "b" in (a[0] if a else k.get("mode", "r")):
                raise PermissionError(13, "Permission denied", path)
            return real(path, *a, **k)

        builtins.open = boom
        try:
            with self.assertRaises(convert.PdfError):
                convert.process_file(orig_pdf)
        finally:
            builtins.open = real

    def test_empty_body_warns(self):
        """正文全被过滤掉时不能静默成功，要给 stderr 警告。"""
        import contextlib
        import io

        convert.ocr_pdf = lambda raw: ["关注", "点赞", "二维码"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            convert.process_file(self.pdf, apply=False)
        self.assertIn("正文为空", err.getvalue())


class NoiseRuleTest(unittest.TestCase):
    """第二轮噪声规则：知乎推荐区 / 热搜榜 / 页码 / 数字串。

    规则全部来自 11 份真实 PDF 的产物取证（`_pdfout`），不是拍脑袋加的。
    """

    def test_recommend_line_dropped(self):
        for s in (
            "6．7K 关注 · 387回答你经历过哪些明显与身边人 “ 格格不入 ” 的瞬间？",
            # OCR 把 3.1K 认成 3． IK，正则必须容忍 I / l / | / 全角点
            "3． IK 关注 · 89回答经常晚十二点后睡觉到底对身体有多大危害？",
            "776关注 · 223回答有哪些让你感觉惊艳的名字？",
            "13．6K 关注。1.3K 回答怎样挽留男友？找准时机用对方法",
        ):
            self.assertTrue(convert.is_noise_line(s), s)

    def test_body_mentioning_attention_kept(self):
        """正文里正常出现「关注点」的段落不能丢 —— 全书 6 行这样的正文。"""
        for s in (
            "这些通常都是相对短的部分，解释了设计如何影响这些关注点以及如何解决这些关注点。",
            "交叉关注点",
            "在这里，你的组织可以确保像安全性、隐私性、可观测性等跨领域问题被纳入考虑范围。",
        ):
            self.assertFalse(convert.is_noise_line(s), s)

    def test_hot_search_line_dropped(self):
        for s in (
            "乌鲁木齐部分区县降至疫情 “黑豹男主角博斯曼患癌去世545．5w 人在",
            "423．7w 人在",
            "女孩失足落水在湖面漂浮自救349．6w 人在",
        ):
            self.assertTrue(convert.is_noise_line(s), s)

    def test_ui_exact_lines_dropped(self):
        for s in (
            "知乎", "发现更大的世界", "QQ 浏览器", "App 内打开", "继续浏览内容",
            "热门推荐", "立即下载", "相关推荐", "查看全部榜单>", "收起^",
        ):
            self.assertTrue(convert.is_noise_line(s), s)


class InlineStripTest(unittest.TestCase):
    """行内 UI 文案：整行丢弃救不了，必须做子串剥离。"""

    def test_app_promo_stripped(self):
        out = convert.strip_inline_noise("乎 Q*•• 下载 App 注册登录如果一家公司执行永不加班政策")
        self.assertNotIn("注册登录", out)
        self.assertIn("如果一家公司执行永不加班政策", out)

    def test_tail_promo_stripped(self):
        out = convert.strip_inline_noise("打开 App 查看更多评论查看全部249个回答¥ 200m0兀")
        self.assertNotIn("查看更多评论", out)

    def test_strip_to_empty(self):
        self.assertEqual(convert.strip_inline_noise("App 内打开"), "")

    def test_body_untouched(self):
        for s in ("他分享了自己十年的工作经验。", "代码行数 = 工作量"):
            self.assertEqual(convert.strip_inline_noise(s), s)

    def test_url_not_mangled(self):
        """URL 里的 / 不能被空格归一化破坏（html2md 踩过同一个坑）。"""
        s = "https://www.industrialempathy.com/posts/design-docs-at-google/点个在看少个 bug"
        out = convert.strip_inline_noise(s)
        self.assertIn("https://www.industrialempathy.com/posts/design-docs-at-google/", out)
        self.assertNotIn("点个在看", out)


class PageNumberTest(unittest.TestCase):
    def test_dropped_when_denominator_repeats(self):
        lines = ["1 / 21", "正文段落。", "2 / 21", "又一段。", "3 / 21"]
        self.assertEqual(convert.drop_page_numbers(lines), ["正文段落。", "又一段。"])

    def test_kept_when_denominator_unique(self):
        """`20 / 8` 是网速换算算式，分母只出现一次，不能当页码删。"""
        lines = ["20 / 8", "1、25", "2、50", "100 / 8 ="]
        self.assertEqual(convert.drop_page_numbers(lines), lines)


class DigitRunTest(unittest.TestCase):
    def test_run_dropped(self):
        lines = ["QQ 浏览器热搜榜", "1", "2", "3", "特朗普回应安倍辞职"]
        self.assertEqual(convert.drop_digit_runs(lines), ["QQ 浏览器热搜榜", "特朗普回应安倍辞职"])

    def test_isolated_digit_kept(self):
        """表格行首列是孤立数字，不能删。"""
        lines = ["专利数 （件）", "2", "制造业质量竞争力指数 83.1"]
        self.assertEqual(convert.drop_digit_runs(lines), lines)

    def test_run_of_two_kept(self):
        self.assertEqual(convert.drop_digit_runs(["1", "2", "正文"]), ["1", "2", "正文"])


class ParasPipelineTest(unittest.TestCase):
    def test_end_to_end_clean(self):
        lines = [
            "1 / 21",
            "国务院印发《中国制造 2025》。",
            "2 / 21",
            "6．7K 关注 · 387回答你经历过哪些格格不入的瞬间？",
            "打开 App 查看更多评论查看全部249个回答",
            "QQ 浏览器热搜榜",
            "1",
            "2",
            "3",
            "坚持走中国特色新型工业化道路。",
            "3 / 21",
        ]
        self.assertEqual(
            convert._paras_from_lines(lines),
            ["国务院印发《中国制造 2025》。", "坚持走中国特色新型工业化道路。"],
        )


class AdAndFragmentTest(unittest.TestCase):
    def test_ad_line_ending_with_badge_dropped(self):
        """广告卡的「广告」角标永远在行尾，整行是卡片标题+角标，不是正文。"""
        for line in (
            "微博的广告",
            "0还呗借款的广告",
            "凭身份证+信用卡旨广告",
            "怎样挽留男友？找准时机用对方法，让他主动回来！23岁以上．．胃稳爱情感的广告",
        ):
            self.assertTrue(convert.is_noise_line(line), line)

    def test_body_mentioning_ad_kept(self):
        """正文提到「广告」但不在行尾，不能删。"""
        for line in (
            "社交媒体上的广告投放越来越精准。",
            "他对广告行业的理解比很多从业者都深。",
        ):
            self.assertFalse(convert.is_noise_line(line), line)

    def test_qr_promo_line_dropped(self):
        line = "扫描下方二维码，添加小助手，均可以免费领取价值 ¥ 999的资料包！"
        self.assertTrue(convert.is_noise_line(line))

    def test_ocr_fragments_dropped(self):
        """OCR 把「打开 App」「继续浏览内容」「知乎」切碎，残留 1–2 字的碎片。"""
        for line in ("0", "打开", "继续", "知《"):
            self.assertTrue(convert.is_noise_line(line), line)

    def test_short_body_word_kept(self):
        """真实语料里有一批 1–2 字的正文碎片（表格首列、断行残留），不能一概删。"""
        for line in ("与", "适配", "融合", "评审", "结论", "创新", "指定", "公式"):
            self.assertFalse(convert.is_noise_line(line), line)

    def test_table_first_column_digit_kept(self):
        """`11_中国制造2025` 表格首列是孤立数字 2/3/5，不是热搜序号。"""
        for line in ("2", "3", "5"):
            self.assertFalse(convert.is_noise_line(line), line)

    def test_reply_badge_stripped(self):
        """`囗复知乎用户` 是「回复知乎用户」被 OCR 认错首字，属评论操作按钮。"""
        self.assertEqual(convert.strip_inline_noise("囗复知乎用户"), "")
        self.assertEqual(convert.strip_inline_noise("回复知乎用户"), "")

    def test_feed_ad_card_dropped(self):
        """信息流广告卡：带「查看详情」按钮、贷款额度、或「某某的文章 · N赞同」署名。"""
        for line in (
            "最高额度20万",
            "查看详情",
            "查看译情",
            "商品运营分享的文章 · 7赞同怎样挽留男友？找准时机用对方法，让他主动回来！",
        ):
            self.assertTrue(convert.is_noise_line(line), line)

    def test_body_with_view_detail_word_kept(self):
        """「查看详情」这类词出现在正文行中段时不整行删。"""
        line = "具体做法请查看详情页面上的说明，我这里只讲思路。"
        self.assertFalse(convert.is_noise_line(line))


class TailTruncateTest(unittest.TestCase):
    """整块信息流噪声在末尾/锚点处成段出现，行级规则管不到 —— 要整段截断。"""

    def test_tail_question_run_truncated(self):
        """末尾连续 ≥3 段知乎问题标题（以问号收尾）整段砍掉。"""
        paras = [
            "这是正文第一段，讲清楚了一件事的来龙去脉。",
            "这是正文第二段，结论落在这里，写得比较实在。",
            "男生的哪个瞬间让你感觉特别绅士？",
            "有哪些让你感觉惊艳的名字？",
            "大学舍友给你上过什么课？",
        ]
        self.assertEqual(
            convert.truncate_feed_tail(paras),
            ["这是正文第一段，讲清楚了一件事的来龙去脉。",
             "这是正文第二段，结论落在这里，写得比较实在。"],
        )

    def test_two_tail_questions_kept(self):
        """只有 2 段问句时不动 —— 达不到 MIN_RUN，可能是正文里的连续设问。"""
        paras = ["正文段落写得很长，有足够的内容支撑。", "为什么会这样？", "后来又怎样了？"]
        self.assertEqual(convert.truncate_feed_tail(paras), paras)

    def test_body_question_in_middle_kept(self):
        """问句出现在中间而不是末尾时不动。"""
        paras = ["为什么会这样？", "正文继续往下写，讲了很多细节和推理。", "结论落在这里。"]
        self.assertEqual(convert.truncate_feed_tail(paras), paras)

    def test_recommend_counter_counts_as_feed(self):
        """`323．9K 关注 · 112彐 K 回答` 是推荐区计数行，也是信息流段。"""
        paras = [
            "正文讲完了，最后一句是完整的陈述句。",
            "男生的哪个瞬间让你感觉特别绅士？",
            "有哪些让你感觉惊艳的名字？",
            "323．9K 关注 · 112彐 K 回答",
        ]
        self.assertEqual(convert.truncate_feed_tail(paras), ["正文讲完了，最后一句是完整的陈述句。"])

    def test_anchor_truncates_from_its_position(self):
        """锚点词（今日推荐阅读/往期精彩文章/热搜榜）之后整块是无用内容。"""
        paras = [
            "正文第一段，内容扎实。",
            "正文第二段，把结论说清楚了。",
            "0今日推荐阅读",
            "如何基于 DDD 构建微服务？",
            "费领取价值 ¥ 999的资料包！",
        ]
        self.assertEqual(
            convert.truncate_feed_tail(paras),
            ["正文第一段，内容扎实。", "正文第二段，把结论说清楚了。"],
        )

    def test_anchor_in_first_half_kept(self):
        """锚点词出现在前半段时不截断 —— 那更可能是在讲这件事。"""
        paras = [
            "文章开头提到 QQ 浏览器热搜榜这个产品形态。",
            "然后正文讲了很长一段别的分析。",
            "最后给了结论。",
        ]
        self.assertEqual(convert.truncate_feed_tail(paras), paras)

    def test_pipeline_uses_tail_truncate(self):
        """端到端：行级规则 + 末尾整段截断一起生效。"""
        lines = [
            "正文第一段，讲清楚了一件事的来龙去脉。",
            "这是正文第二段，结论落在这里，写得比较实在。",
            "男生的哪个瞬间让你感觉特别绅士？",
            "有哪些让你感觉惊艳的名字？",
            "大学舍友给你上过什么课？",
        ]
        self.assertEqual(
            convert._paras_from_lines(lines),
            ["正文第一段，讲清楚了一件事的来龙去脉。",
             "这是正文第二段，结论落在这里，写得比较实在。"],
        )


class CliTest(unittest.TestCase):
    def test_check_and_apply_are_exclusive(self):
        with self.assertRaises(SystemExit):
            convert.main(["--check", "--apply", "whatever.pdf"])

    def test_missing_path_exits(self):
        with self.assertRaises(SystemExit):
            convert.main([])

    def test_runtime_error_exit_code(self):
        code = convert.main([os.path.join(tempfile.gettempdir(), "definitely-missing.pdf")])
        self.assertEqual(code, 1)


class QuestionHeadingTest(unittest.TestCase):
    """题号→Markdown 标题识别（面试题特有结构）。

    正向：题号（1. / 1、 / 1,）开头的独立行升级为 ## 标题。
    反向保护（坑14/15/16教训）：小数、多级编号、答案内部编号、
    正文提及、非递增数字、超大数字、过长题目都不能误判。
    """

    def test_single_question_marked(self):
        paras = [
            "1. 什么是微服务架构",
            "微服务架构就是将单体的应用程序分成多个应用程序，这多个应用程序就成为微服务。",
        ]
        r = convert.mark_question_headings(paras)
        self.assertEqual(r[0], "## 1. 什么是微服务架构")
        self.assertFalse(r[1].startswith("## "))

    def test_consecutive_questions_marked(self):
        paras = [
            "1. 什么是微服务架构",
            "微服务架构就是将单体的应用程序分成多个应用程序。",
            "2. 为什么需要学习Spring Cloud",
            "首先springcloud基于springboot的优雅简洁。",
        ]
        r = convert.mark_question_headings(paras)
        self.assertTrue(r[0].startswith("## "))
        self.assertTrue(r[2].startswith("## "))
        self.assertFalse(r[1].startswith("## "))
        self.assertFalse(r[3].startswith("## "))

    def test_chinese_and_english_comma_formats(self):
        paras = ["1、什么是 Redis？", "Redis 本质是内存数据库。",
                 "2、Redis 的优缺点", "Redis 出色之处是性能。"]
        r = convert.mark_question_headings(paras)
        self.assertTrue(r[0].startswith("## 1、"))
        self.assertTrue(r[2].startswith("## 2、"))

    def test_english_comma_format(self):
        paras = ["1,内存文件映射断电数据是否可以保存？", "断电后数据不保存。",
                 "2,为什么TCP需要拆包和拼包？", "因为MSS限制。"]
        r = convert.mark_question_headings(paras)
        self.assertTrue(r[0].startswith("## 1,"))
        self.assertTrue(r[2].startswith("## 2,"))

    def test_decimal_not_marked(self):
        paras = ["Pi is approximately 3.14 which is a famous constant.",
                 "This is another paragraph with 3.14 in it and also 2.71."]
        r = convert.mark_question_headings(paras)
        self.assertFalse(any(p.startswith("## ") for p in r))

    def test_multilevel_number_not_marked(self):
        paras = ["1.1 什么是Spring MVC？简单介绍下你对Spring MVC的理解？",
                 "Spring MVC是一个基于Java的实现了MVC设计模式的请求驱动类型的轻量级Web框架。",
                 "6.14 介绍一下 WebApplicationContext",
                 "WebApplicationContext继承了ApplicationContext。"]
        r = convert.mark_question_headings(paras)
        self.assertFalse(any(p.startswith("## ") for p in r))

    def test_answer_inner_number_not_marked(self):
        paras = ["1. 多线程有什么用？",
                 "1）发挥多核CPU的优势",
                 "随着工业的进步，现在的笔记本、台式机乃至商用的应用服务器至少也都是双核的。",
                 "2）防止阻塞",
                 "从程序运行效率的角度来看，单核CPU不但不会发挥出多线程的优势。"]
        r = convert.mark_question_headings(paras)
        self.assertTrue(r[0].startswith("## "))
        self.assertFalse(r[1].startswith("## "))
        self.assertFalse(r[3].startswith("## "))

    def test_non_sequential_not_marked(self):
        paras = ["1. 什么是XX？", "答案1...", "5. 什么是YY？", "答案5..."]
        r = convert.mark_question_headings(paras)
        self.assertFalse(r[0].startswith("## "))
        self.assertFalse(r[2].startswith("## "))

    def test_body_paragraph_not_marked(self):
        paras = ["他提到了第 1 章和第 2 章的内容。",
                 "这个问题在 1.2 节有详细说明。",
                 "参考答案见 3.5 小节。"]
        r = convert.mark_question_headings(paras)
        self.assertFalse(any(p.startswith("## ") for p in r))

    def test_number_too_large_not_marked(self):
        paras = ["500. 这不是一个真实的题号", "答案...",
                 "501. 另一个不真实的题号", "答案2..."]
        r = convert.mark_question_headings(paras)
        self.assertFalse(r[0].startswith("## "))
        self.assertFalse(r[2].startswith("## "))

    def test_no_content_after_number(self):
        paras = ["1.", "答案...", "2、", "答案2..."]
        r = convert.mark_question_headings(paras)
        self.assertFalse(any(p.startswith("## ") for p in r))

    def test_gap_of_one_allowed(self):
        paras = ["1. 题目1", "答案1...", "3. 题目3", "答案3...", "5. 题目5", "答案5..."]
        r = convert.mark_question_headings(paras)
        self.assertTrue(r[0].startswith("## "))
        self.assertTrue(r[2].startswith("## "))
        self.assertTrue(r[4].startswith("## "))

    def test_question_followed_by_multiline_answer(self):
        """题目1行 + 答案多段（真实面试题最常见形态）正常标记。

        题目本身短、答案长是面试题的常态，不能因「题目太短」否决；
        真正的判据是「题号 + 编号递增链」。
        """
        paras = [
            "1. 请列举出在JDK中几个常用的设计模式？",
            "单例模式用于Runtime、Calendar和其他的一些类中。工厂模式被用于各种不可变的类如Boolean。",
            "观察者模式被用于Swing和很多的事件监听中。装饰器设计模式被用于多个Java IO类中。",
            "2. 什么是设计模式？",
            "设计模式是程序员用来解决特定设计问题的尝试和测试的方法。",
        ]
        r = convert.mark_question_headings(paras)
        self.assertTrue(r[0].startswith("## 1. "))
        self.assertTrue(r[3].startswith("## 2. "))
        self.assertFalse(r[1].startswith("## "))
        self.assertFalse(r[2].startswith("## "))

    def test_consecutive_questions_then_answers(self):
        """面试官连着列多个问题、再统一作答（面经语料实测形态）。

        这是去掉「题号后必须紧跟答案」条件的原因：整块连续题号必须全标，
        不能只标第一个和最后一个。
        """
        paras = [
            "【美团A部门一面技术】",
            "1、JVM如何优化的？CMS算法执行流程？什么情况下发生的Full GC？",
            "2、分布式事务讲一下？",
            "3、ES是如何调优的？副本机制作用？",
            "4、其他也都是基础问题，印象不是很深刻了。",
            "面试官说先看简历。",
        ]
        r = convert.mark_question_headings(paras)
        self.assertTrue(r[1].startswith("## 1、"))
        self.assertTrue(r[2].startswith("## 2、"))
        self.assertTrue(r[3].startswith("## 3、"))
        self.assertTrue(r[4].startswith("## 4、"))
        # 非题号的「【...】」标题行和叙述行都不动
        self.assertFalse(r[0].startswith("## "))
        self.assertFalse(r[5].startswith("## "))

    def test_mixed_formats_work_together(self):
        paras = ["1. 第一个问题", "答案1...", "2、第二个问题",
                 "答案2...", "3. 第三个问题", "答案3..."]
        r = convert.mark_question_headings(paras)
        self.assertTrue(r[0].startswith("## "))
        self.assertTrue(r[2].startswith("## "))
        self.assertTrue(r[4].startswith("## "))

    def test_single_question_with_answer_marked(self):
        """单题文档（唯一题号 + 答案）合法，标记。

        一道题 + 答案就是合法的面试题，唯一题号应标记。
        真正的否决条件是「空题号」（1. 后无内容），由正则负责。
        """
        paras = ["1. 什么是微服务架构？", "微服务架构就是把单体应用拆成多个服务。"]
        r = convert.mark_question_headings(paras)
        self.assertTrue(r[0].startswith("## 1. "))
        self.assertFalse(r[1].startswith("## "))

    def test_bare_number_line_not_marked(self):
        """纯数字/空题号行（表格首列、页码残留）不标记。"""
        paras = ["1.", "2、", "3", "4、", "5."]
        r = convert.mark_question_headings(paras)
        self.assertFalse(any(p.startswith("## ") for p in r))


if __name__ == "__main__":
    unittest.main()
