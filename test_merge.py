# -*- coding: utf-8 -*-
"""合并进来的 PDF→MD 能力回归测试：门禁 / 结构化 / 页级 OCR 持久化 / 合并 / 后端路由。

依赖口径：纯逻辑断言只用标准库；pymupdf / pymupdf4llm 相关用例 skipUnless 本机已装，
理由与既有 OCR 打桩一致——这些能力依赖本机第三方包，不能让单测变成必须装重依赖。
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import batch_convert  # noqa: E402
import convert  # noqa: E402
import fsutil  # noqa: E402
import ocr_merge  # noqa: E402
import paddle_ocr  # noqa: E402
import quality  # noqa: E402
import structured  # noqa: E402

HAS_PYMUPDF = structured.pymupdf_available()
HAS_LLM = structured.pymupdf4llm_available()


# ---------------------------------------------------------------- 夹具


def write_png(path, w, h, rgb):
    with open(path, "wb") as fh:
        fh.write(convert.png_bytes(w, h, bytes(rgb) * (w * h)))


def _read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def make_text_pdf(path, lines=("第一段中文正文。",), pages=1):
    import pymupdf
    doc = pymupdf.open()
    for _ in range(pages):
        page = doc.new_page()
        y = 72
        for line in lines:
            page.insert_text((72, y), line, fontsize=11, fontname="china-s")
            y += 20
    doc.save(path)
    doc.close()


def make_image_pdf(path, pages, deco_pages, tmp):
    """pages 页文档：同一张 100×100 图印在前 deco_pages 页（装饰），第 5 页再放一张正文图。"""
    import pymupdf
    deco = os.path.join(tmp, "deco.png")
    body = os.path.join(tmp, "body.png")
    write_png(deco, 100, 100, (200, 10, 10))
    write_png(body, 120, 120, (10, 200, 10))
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        if i < deco_pages:
            page.insert_image(pymupdf.Rect(40, 40, 140, 140), filename=deco)
        if i == 4:
            page.insert_image(pymupdf.Rect(40, 200, 160, 320), filename=body)
    doc.save(path)
    doc.close()


class FsutilTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pdf2md_fs_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_atomic_write_replaces_and_leaves_no_temp(self):
        p = os.path.join(self.dir, "a.md")
        fsutil.atomic_write_text(p, "第一版")
        fsutil.atomic_write_text(p, "第二版")
        with open(p, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "第二版")
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith(".tmp")], [])

    def test_safe_name(self):
        self.assertEqual(fsutil.safe_name('a/b:c*?"<>|'), "a_b_c______")
        self.assertEqual(fsutil.safe_name("CON.md"), "_CON.md")
        self.assertEqual(fsutil.safe_name(". ."), "")
        self.assertEqual(fsutil.safe_name("x" * 100), "x" * 80)

    def test_hashes(self):
        p = os.path.join(self.dir, "x.txt")
        fsutil.atomic_write_text(p, "内容")
        self.assertEqual(len(fsutil.doc_hash(p)), 16)
        self.assertEqual(fsutil.sha256_text("内容"), fsutil.sha256_file(p))

    def test_read_text_fallback_gbk(self):
        p = os.path.join(self.dir, "gbk.txt")
        with open(p, "w", encoding="gb18030") as fh:
            fh.write("中文内容")
        self.assertEqual(fsutil.read_text_fallback(p), "中文内容")

    def test_read_text_fallback_rejects_binary(self):
        p = os.path.join(self.dir, "bin.dat")
        with open(p, "wb") as fh:
            fh.write(b"\xff" * 500)          # 既非 UTF-8 也非 GB18030
        with self.assertRaises(ValueError):
            fsutil.read_text_fallback(p)


class QualityTest(unittest.TestCase):
    """三态门禁：阈值、NFKC、Markdown 清洗、分页标记都要按迁入口径走。"""

    def test_nfkc_fixes_kangxi_radical(self):
        """PDF 常把汉字映射进 Kangxi 部首区：⼤厂 归一后必须算成中文。"""
        m = quality.metrics("⼤厂")
        self.assertEqual(m["cjk"], 1.0)
        self.assertGreater(m["fixed"], 0)

    def test_cjk_ranges(self):
        self.assertTrue(quality.is_cjk("中"))
        self.assertTrue(quality.is_cjk("\u3400"))       # 扩展 A
        self.assertFalse(quality.is_cjk("A"))

    def test_short_is_fail(self):
        state, why = quality.judge(quality.metrics("太短"))
        self.assertEqual(state, "fail")
        self.assertIn("过短", why)

    def test_garbled_is_fail(self):
        text = ("正常中文" * 40) + ("ړୗ᭗" * 40)
        state, why = quality.judge(quality.metrics(text))
        self.assertEqual(state, "fail")
        self.assertIn("乱码", why)

    def test_low_cjk_is_review(self):
        text = "This is an English-only document. " * 20
        state, _ = quality.judge(quality.metrics(text))
        self.assertEqual(state, "review")

    def test_healthy_is_pass(self):
        text = "这是一段正常的中文正文，讲清楚了一件事情的来龙去脉。" * 20
        state, _ = quality.judge(quality.metrics(text))
        self.assertEqual(state, "pass")

    def test_markdown_and_page_marks_ignored(self):
        """配图语法、表格线、页标记都不能稀释 CJK 占比。"""
        text = ("# 标题\n\n===== page 1 =====\n\n| 列一 | 列二 |\n| --- | --- |\n\n"
                "![配图](./x.png)\n\n" + "正文内容" * 40)
        m = quality.metrics(text)
        self.assertGreater(m["cjk"], 0.9)

    def test_only_page_marks_is_empty(self):
        m = quality.metrics("===== page 1 =====\n\n===== page 2 =====\n")
        self.assertEqual(m["chars"], 0)
        self.assertEqual(quality.judge(m)[0], "fail")

    def test_assess_shape(self):
        report = quality.assess("中文正文。" * 50)
        for key in ("state", "reason", "chars", "cjk", "latin", "bad", "fixed"):
            self.assertIn(key, report)

    def test_thresholds_mirror_upstream(self):
        self.assertEqual(quality.MIN_CHARS, 100)
        self.assertEqual(quality.CJK_MIN, 0.15)
        self.assertEqual(quality.BAD_MAX, 0.02)



class StructuredFallbackQualityTest(unittest.TestCase):
    def test_pypdf_fallback_selected_only_when_gate_improves(self):
        good = "这是一段质量良好的中文正文。" * 20
        bad = "乱码" * 8
        self.assertTrue(structured._prefer_pypdf_fallback(bad, good))
        self.assertFalse(structured._prefer_pypdf_fallback(good, bad))
        self.assertFalse(structured._prefer_pypdf_fallback(good, good + "补充"))


class StructuredFallbackRobustnessTest(unittest.TestCase):
    """兜底候选自己失败时不能把主结果拖成 StructuredError。"""

    def test_pypdf_failure_keeps_primary(self):
        bad = "乱码" * 8
        with mock.patch.object(structured, "_md_via_pypdf",
                               side_effect=structured.StructuredError("pypdf 坏了")):
            self.assertIsNone(structured._pypdf_fallback_md("x.pdf", bad))

    def test_better_candidate_is_returned(self):
        good = "这是一段质量良好的中文正文。" * 20
        with mock.patch.object(structured, "_md_via_pypdf", return_value=good):
            self.assertEqual(structured._pypdf_fallback_md("x.pdf", "乱码" * 8), good)

    def test_worse_or_equal_candidate_is_dropped(self):
        good = "这是一段质量良好的中文正文。" * 20
        with mock.patch.object(structured, "_md_via_pypdf", return_value="短"):
            self.assertIsNone(structured._pypdf_fallback_md("x.pdf", good))


class StructuredFilterTest(unittest.TestCase):
    """图片筛选是纯逻辑，先把「小图/装饰/去重」锁死，不用真 PDF。"""

    def test_decorative_threshold(self):
        counts = {"deco": {0, 1, 2, 3}, "body": {4}}
        # 6 页：阈值 max(4, 6//3=2)=4 → 4 页重复即判装饰
        self.assertEqual(structured.decorative_hashes(counts, 6), {"deco"})
        # 2 页：阈值仍是 4 → 同一张图出现在 2 页不算装饰
        self.assertEqual(structured.decorative_hashes({"x": {0, 1}}, 2), set())

    def test_plan_saved_dedups_and_drops_decorative(self):
        kept = [("d", 1, b"a"), ("b", 2, b"b"), ("b", 3, b"b2"), ("c", 4, b"c")]
        saved = structured.plan_saved(kept, {"d"})
        self.assertEqual([h for h, _p, _d in saved], ["b", "c"])
        self.assertEqual([p for _h, p, _d in saved], [2, 4])   # 保留首次出现的页号

    def test_image_section(self):
        sec = structured.image_section([("t_abc.png", 3)], "t.images")
        self.assertIn("## 配图（1张）", sec)
        self.assertIn("![配图 原文第3页](./t.images/t_abc.png)", sec)

    def test_image_section_empty(self):
        self.assertEqual(structured.image_section([], "t.images"), "")

    def test_extractor_status_is_boolistic(self):
        st = structured.extractor_status()
        for key in ("pymupdf", "pymupdf4llm", "pypdf"):
            self.assertIsInstance(st[key], bool)


@unittest.skipUnless(HAS_PYMUPDF, "需要本机安装 pymupdf")
class StructuredExtractTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pdf2md_st_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    @unittest.skipUnless(HAS_LLM, "需要本机安装 pymupdf4llm")
    def test_image_filter_end_to_end(self):
        """装饰图（4/6 页重复）不落盘，正文图保留一张，引用是相对路径。"""
        pdf = os.path.join(self.dir, "画册.pdf")
        make_image_pdf(pdf, pages=6, deco_pages=4, tmp=self.dir)
        img_dir = os.path.join(self.dir, "画册.images")
        res = structured.extract(pdf, images_dir=img_dir, images_rel="画册.images",
                                 stem="画册")
        self.assertEqual(res["pages"], 6)
        self.assertEqual(res["imgs_kept"], 1)
        self.assertEqual(res["imgs_skipped"], 4)
        self.assertEqual(len(os.listdir(img_dir)), 1)
        self.assertIn("## 配图（1张）", res["md"])
        self.assertIn("./画册.images/", res["md"])

    @unittest.skipUnless(HAS_LLM, "需要本机安装 pymupdf4llm")
    def test_write_images_false_leaves_no_files(self):
        """--check 用 write_images=False：统计照算，但一个文件都不落。"""
        pdf = os.path.join(self.dir, "画册.pdf")
        make_image_pdf(pdf, pages=6, deco_pages=4, tmp=self.dir)
        img_dir = os.path.join(self.dir, "画册.images")
        res = structured.extract(pdf, images_dir=img_dir, images_rel="画册.images",
                                 stem="画册", write_images=False)
        self.assertEqual(res["imgs_kept"], 1)
        self.assertFalse(os.path.exists(img_dir))

    @unittest.skipUnless(HAS_LLM, "需要本机安装 pymupdf4llm")
    def test_tiny_image_skipped(self):
        import pymupdf
        pdf = os.path.join(self.dir, "小图.pdf")
        png = os.path.join(self.dir, "tiny.png")
        write_png(png, 20, 20, (1, 2, 3))
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_image(pymupdf.Rect(10, 10, 30, 30), filename=png)
        doc.save(pdf)
        doc.close()
        res = structured.extract(pdf, images_dir=os.path.join(self.dir, "i"),
                                 images_rel="i", stem="小图")
        self.assertEqual(res["imgs_kept"], 0)
        self.assertEqual(res["imgs_skipped"], 0)      # 小图在 kept 之前就被过滤

    @unittest.skipUnless(HAS_LLM, "需要本机安装 pymupdf4llm")
    def test_text_pdf_extracts_pages_and_cjk(self):
        pdf = os.path.join(self.dir, "书.pdf")
        make_text_pdf(pdf, lines=("第一章 总则", "这一章说明整体的设计原则与边界。"), pages=2)
        res = structured.extract(pdf, stem="书")
        self.assertEqual(res["pages"], 2)
        self.assertIn("这一章说明整体的设计原则与边界", res["md"])
        self.assertEqual(res["imgs_kept"], 0)

    def test_page_count_and_render(self):
        pdf = os.path.join(self.dir, "两页.pdf")
        make_text_pdf(pdf, pages=2)
        self.assertEqual(structured.page_count(pdf), 2)
        png = structured.render_page_png(pdf, 1, os.path.join(self.dir, "p1.png"), dpi=72)
        self.assertTrue(os.path.isfile(png))
        with self.assertRaises(ValueError):
            structured.render_page_png(pdf, 3, os.path.join(self.dir, "p3.png"), dpi=72)

    def test_missing_pdf_raises_structured_error(self):
        with self.assertRaises(structured.StructuredError):
            structured.extract(os.path.join(self.dir, "不存在.pdf"))


class PaddleParsingTest(unittest.TestCase):
    def test_parse_range(self):
        self.assertEqual(paddle_ocr.parse_range("1-3,7", 10), [1, 2, 3, 7])
        self.assertEqual(paddle_ocr.parse_range("1-99", 3), [1, 2, 3])
        self.assertEqual(paddle_ocr.parse_range("-1", 5), [])          # 负号不是范围
        self.assertEqual(paddle_ocr.parse_range("abc,,3", 5), [3])
        self.assertEqual(paddle_ocr.parse_range("0-2", 5), [1, 2])

    def test_text_paths(self):
        self.assertEqual(str(paddle_ocr.text_path_for(os.path.join("a", "b.pdf"))),
                         os.path.join("a", "b.pdf.ocr.txt"))
        self.assertEqual(str(paddle_ocr.meta_path_for(os.path.join("a", "b.pdf.ocr.txt"))),
                         os.path.join("a", "b.pdf.ocr.txt.meta.json"))

    def test_flatten_and_scores(self):
        payload = {"text": "整段", "blocks": [{"text": "行1", "score": 0.9},
                                              {"lines": [{"text": "行2", "score": 0.8}]}]}
        texts, scores = [], []
        paddle_ocr.flatten_texts(payload, texts)
        paddle_ocr.collect_scores(payload, scores)
        self.assertEqual(texts, ["整段", "行1", "行2"])
        self.assertEqual(scores, [0.9, 0.8])

    def test_non_ascii_image_path_rejected(self):
        engine = paddle_ocr.PaddleEngine(exe="fake.exe", work_dir=".")
        with self.assertRaises(paddle_ocr.OcrError):
            engine.ocr(os.path.join("目录", "页1.png"))

    def test_render_pages_text_marks_optional(self):
        pages = [dict(page=1, text="第一页"), dict(page=2, text="第二页")]
        plain = paddle_ocr.render_pages_text(pages)
        self.assertEqual(plain, "第一页\n\n第二页")
        self.assertNotIn("page 1", plain)
        marked = paddle_ocr.render_pages_text(pages, page_marks=True)
        self.assertIn("===== page 1 =====", marked)
        self.assertIn("第二页", marked)


@unittest.skipUnless(HAS_PYMUPDF, "需要本机安装 pymupdf")
class PaddleTextStoreTest(unittest.TestCase):
    """侧车完整性的四种失败面：页不全 / 源被换 / 正文被改 / meta 被改，都必须不算完成。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pdf2md_ocr_")
        self.pdf = os.path.join(self.dir, "扫描件.pdf")
        make_text_pdf(self.pdf, pages=2)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _pages(self, texts=("第一页正文", "第二页正文")):
        return [dict(page=i + 1, text=t, score=0.95, lines=1, seconds=0.5)
                for i, t in enumerate(texts)]

    def test_save_and_complete(self):
        txt = paddle_ocr.save_text(self.pdf, self._pages(), total_pages=2, dpi=200)
        self.assertTrue(os.path.isfile(txt))
        self.assertTrue(os.path.isfile(paddle_ocr.meta_path_for(txt)))
        self.assertTrue(paddle_ocr.is_complete(self.pdf))
        meta = _read_json(paddle_ocr.meta_path_for(txt))
        self.assertEqual(meta["total_pages"], 2)
        self.assertEqual(meta["completed_pages"], [1, 2])
        self.assertTrue(meta["complete"])
        self.assertEqual(meta["source_sha256"], fsutil.sha256_file(self.pdf))

    def test_partial_pages_not_complete(self):
        paddle_ocr.save_text(self.pdf, self._pages()[:1], total_pages=2)
        self.assertFalse(paddle_ocr.is_complete(self.pdf))

    def test_tampered_text_not_complete(self):
        txt = paddle_ocr.save_text(self.pdf, self._pages(), total_pages=2)
        with open(txt, "a", encoding="utf-8") as fh:
            fh.write("\n补一段")
        self.assertFalse(paddle_ocr.is_complete(self.pdf))

    def test_changed_source_not_complete(self):
        paddle_ocr.save_text(self.pdf, self._pages(), total_pages=2)
        make_text_pdf(self.pdf, lines=("换过的正文。",), pages=2)
        self.assertFalse(paddle_ocr.is_complete(self.pdf))

    def test_meta_flag_off_not_complete(self):
        txt = paddle_ocr.save_text(self.pdf, self._pages(), total_pages=2)
        meta_path = paddle_ocr.meta_path_for(txt)
        meta = _read_json(meta_path)
        meta["complete"] = False
        fsutil.atomic_write_text(meta_path, json.dumps(meta, ensure_ascii=False))
        self.assertFalse(paddle_ocr.is_complete(self.pdf))

    def test_load_pages_roundtrip(self):
        txt = paddle_ocr.save_text(self.pdf, self._pages(("甲页", "乙页")), total_pages=2)
        pages = paddle_ocr.load_pages(txt)
        self.assertEqual([p["page"] for p in pages], [1, 2])
        self.assertEqual([p["text"] for p in pages], ["甲页", "乙页"])

    def test_empty_text_not_complete(self):
        """引擎「成功」但返回空串：不能把空产物固化下来，必须允许重跑。"""
        paddle_ocr.save_text(self.pdf, self._pages(("", "")), total_pages=2)
        self.assertFalse(paddle_ocr.is_complete(self.pdf))


@unittest.skipUnless(HAS_PYMUPDF, "需要本机安装 pymupdf")
class OcrMergeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pdf2md_merge_")
        self.pdf = os.path.join(self.dir, "书.pdf")
        make_text_pdf(self.pdf, pages=1)
        self.md = os.path.join(self.dir, "书.md")
        fsutil.atomic_write_text(self.md, (
            "# 书\n\n> 来源：书.pdf\n> 形态：文字层\n> 说明：正文由 pdftotext 得到。\n\n"
            "旧正文第一段。\n\n---\n\n## 配图（1张）\n\n"
            "![配图 原文第1页](./书.images/a.png)\n"))
        paddle_ocr.save_text(self.pdf, [dict(page=1, text="OCR 新正文。",
                                            score=0.9, lines=1, seconds=0.4)],
                             total_pages=1)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_merge_keeps_head_and_images(self):
        rep = ocr_merge.merge_file(self.pdf)
        text = fsutil.read_text_fallback(self.md)
        self.assertTrue(text.startswith("# 书"))
        self.assertIn("> 形态：页级OCR（PaddleOCR-json 合并）", text)
        self.assertNotIn("> 形态：文字层", text)
        self.assertIn("OCR 新正文。", text)
        self.assertNotIn("旧正文第一段。", text)
        self.assertIn("## 配图（1张）", text)
        self.assertIn("./书.images/a.png", text)
        self.assertTrue(rep["imgs_kept"])
        self.assertTrue(rep["written"])
        self.assertTrue(os.path.isfile(self.md + ".ocr.bak"))

    def test_second_merge_does_not_overwrite_backup(self):
        ocr_merge.merge_file(self.pdf)
        first_bak = fsutil.read_text_fallback(self.md + ".ocr.bak")
        fsutil.atomic_write_text(self.md, fsutil.read_text_fallback(self.md) + "\n补一行\n")
        ocr_merge.merge_file(self.pdf)
        self.assertEqual(fsutil.read_text_fallback(self.md + ".ocr.bak"), first_bak)

    def test_dry_run_writes_nothing(self):
        before = fsutil.read_text_fallback(self.md)
        rep = ocr_merge.merge_file(self.pdf, dry_run=True)
        self.assertFalse(rep["written"])
        self.assertEqual(fsutil.read_text_fallback(self.md), before)
        self.assertFalse(os.path.exists(self.md + ".ocr.bak"))

    def test_missing_ocr_text_raises(self):
        os.unlink(str(paddle_ocr.text_path_for(self.pdf)))
        with self.assertRaises(ocr_merge.MergeError):
            ocr_merge.merge_file(self.pdf)

    def test_incomplete_ocr_text_raises(self):
        os.unlink(str(paddle_ocr.text_path_for(self.pdf)))
        paddle_ocr.save_text(self.pdf, [dict(page=1, text="半页", score=0.9,
                                            lines=1, seconds=0.3)], total_pages=2)
        with self.assertRaises(ocr_merge.MergeError):
            ocr_merge.merge_file(self.pdf)


class BackendRoutingTest(unittest.TestCase):
    def test_unknown_backend_raises(self):
        with self.assertRaises(convert.PdfError):
            convert.resolve_backend("marker", "text")

    def test_explicit_backend_passes_through(self):
        for name in ("builtin", "structured", "paddle"):
            self.assertEqual(convert.resolve_backend(name, "image"), name)

    def test_auto_image_stays_builtin(self):
        """截图型默认仍走内置 WinRT：换引擎必须显式 --backend paddle。"""
        self.assertEqual(convert.resolve_backend("auto", "image"), "builtin")

    def test_auto_text_prefers_structured_when_available(self):
        expected = "structured" if structured.pymupdf4llm_available() else "builtin"
        self.assertEqual(convert.resolve_backend("auto", "text"), expected)

    def test_auto_unknown_is_builtin(self):
        self.assertEqual(convert.resolve_backend("auto", "unknown"), "builtin")


class ConvertIntegrationTest(unittest.TestCase):
    """改 convert.py 时最怕两件事：门禁误拦正常产物、strict 写盘顺序倒挂。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pdf2md_int_")
        self.pdf = os.path.join(self.dir, "样例.pdf")
        with open(self.pdf, "wb") as fh:
            fh.write(_image_pdf(3, 2, bytes(range(18))))
        self._real_ocr = convert.ocr_pdf
        convert.ocr_pdf = lambda raw: ["甲。", "乙。"]

    def tearDown(self):
        convert.ocr_pdf = self._real_ocr
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_builtin_report_has_gate(self):
        rep = convert.convert_file(self.pdf, backend="builtin")
        self.assertEqual(rep["backend"], "builtin")
        self.assertEqual(rep["shape"], "截图OCR")
        self.assertIn(rep["gate"]["state"], ("pass", "review", "fail"))
        self.assertTrue(rep["md"].startswith("# 样例"))

    def test_short_body_writes_but_reports_fail(self):
        md = convert.process_file(self.pdf, apply=True)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "样例.md")))
        self.assertIn("甲。", md)
        rep = convert.convert_file(self.pdf, backend="builtin")
        self.assertEqual(rep["gate"]["state"], "fail")

    def test_strict_blocks_write(self):
        with self.assertRaises(convert.PdfError):
            convert.process_file(self.pdf, apply=True, strict=True)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "样例.md")))

    def test_structured_backend_wraps_and_gates(self):
        real = convert._structured_body

        def fake(path, extractor="pymupdf4llm", images_root=None, write_images=True):
            return dict(body="## 一、设计原则\n\n正文足够长的一段中文内容。" * 5,
                        shape="结构化(pymupdf4llm)", pages=3, imgs_kept=2,
                        imgs_skipped=1, image_section="\n---\n\n## 配图（2张）\n",
                        ocr_txt=None, extractor="pymupdf4llm")

        convert._structured_body = fake
        try:
            rep = convert.convert_file(self.pdf, backend="structured")
        finally:
            convert._structured_body = real
        self.assertEqual(rep["backend"], "structured")
        self.assertTrue(rep["md"].startswith("# 样例"))
        self.assertIn("> 形态：结构化(pymupdf4llm)", rep["md"])
        self.assertIn("## 一、设计原则", rep["md"])
        self.assertEqual(rep["imgs_kept"], 2)
        self.assertEqual(rep["pages"], 3)

    def test_check_prints_gate_and_backend(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = convert.check_file(self.pdf)
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("后端：auto → builtin", text)
        self.assertIn("质量门禁：", text)
        self.assertIn("结构化抽取：pymupdf=", text)

    @unittest.skipUnless(HAS_LLM, "需要本机安装 pymupdf4llm")
    def test_check_structured_does_not_write_images(self):
        """预检只读：结构化后端在 --check 下不得在源目录创建 .images/。"""
        pdf = os.path.join(self.dir, "画册.pdf")
        make_image_pdf(pdf, pages=6, deco_pages=4, tmp=self.dir)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = convert.check_file(pdf, backend="structured")
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "画册.images")))
        self.assertIn("配图：保留 1 张", out.getvalue())

    def test_page_marks_flag_only_affects_paddle(self):
        """内置路径不该受 --page-marks 影响（参数只对页级 OCR 有意义）。"""
        rep = convert.convert_file(self.pdf, backend="builtin", page_marks=True)
        self.assertNotIn("===== page 1 =====", rep["md"])



class PaddleEngineFailureTest(unittest.TestCase):
    def test_startup_oserror_is_ocr_error(self):
        from unittest import mock
        engine = paddle_ocr.PaddleEngine(exe="fake-engine")
        with mock.patch.object(paddle_ocr.os.path, "isfile", return_value=True):
            with mock.patch.object(paddle_ocr.subprocess, "Popen", side_effect=OSError("denied")):
                with self.assertRaises(paddle_ocr.OcrError) as ctx:
                    engine.__enter__()
        self.assertIsInstance(ctx.exception.__cause__, OSError)

    def test_broken_pipe_is_ocr_error(self):
        from unittest import mock
        engine = paddle_ocr.PaddleEngine(exe="fake")
        engine.proc = mock.Mock()
        engine.proc.stdin.write.side_effect = BrokenPipeError("closed")
        with self.assertRaises(paddle_ocr.OcrError) as ctx:
            engine.ocr("ascii.png")
        self.assertIsInstance(ctx.exception.__cause__, BrokenPipeError)


class PaddleStartupCleanupTest(unittest.TestCase):
    """引擎在 init 完成前退出：抛业务异常的同时必须把进程句柄收掉。"""

    def test_engine_exit_before_init_is_cleaned_up(self):
        engine = paddle_ocr.PaddleEngine(exe="fake")
        proc = mock.Mock()
        proc.stdin = mock.Mock()
        proc.stdout = iter([])
        proc.poll.return_value = 0
        engine._q.put(None)                     # 模拟 _pump 读到 EOF
        with mock.patch.object(paddle_ocr.os.path, "isfile", return_value=True):
            with mock.patch.object(paddle_ocr.subprocess, "Popen", return_value=proc):
                with self.assertRaises(paddle_ocr.OcrError):
                    engine.__enter__()
        self.assertIsNone(engine.proc)


class OcrSaveTransactionTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pdf2md_save_")
        self.pdf = os.path.join(self.dir, "a.pdf")
        with open(self.pdf, "wb") as fh:
            fh.write(b"%PDF-1.4\nsource")
        self.txt = paddle_ocr.text_path_for(self.pdf)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_failed_metadata_commit_restores_previous_pair(self):
        pages = [dict(page=1, text="old text", score=0.9, lines=1)]
        paddle_ocr.save_text(self.pdf, pages, 1)
        old_text = self.txt.read_bytes()
        old_meta = paddle_ocr.meta_path_for(self.txt).read_bytes()
        real_write = fsutil.atomic_write_text
        calls = []
        def fail_second(path, text, *args, **kwargs):
            calls.append(path)
            if len(calls) == 2:
                raise OSError("disk full")
            return real_write(path, text, *args, **kwargs)
        with mock.patch.object(fsutil, "atomic_write_text", side_effect=fail_second):
            with self.assertRaises(OSError):
                paddle_ocr.save_text(self.pdf,
                    [dict(page=1, text="new text", score=0.8, lines=1)], 1)
        self.assertEqual(self.txt.read_bytes(), old_text)
        self.assertEqual(paddle_ocr.meta_path_for(self.txt).read_bytes(), old_meta)
        self.assertTrue(paddle_ocr.is_complete(self.pdf, 1))

    def test_rollback_failure_keeps_original_error(self):
        paddle_ocr.save_text(self.pdf, [dict(page=1, text="old text", score=.9, lines=1)], 1)
        def fail_first(path, text, *args, **kwargs):
            raise OSError("disk full")
        with mock.patch.object(fsutil, "atomic_write_text", side_effect=fail_first):
            with mock.patch.object(fsutil, "atomic_write_bytes", side_effect=OSError("rollback failed")):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    with self.assertRaises(OSError) as ctx:
                        paddle_ocr.save_text(
                            self.pdf, [dict(page=1, text="new text", score=.8, lines=1)], 1)
        self.assertEqual(str(ctx.exception), "disk full")
        self.assertIn("回滚失败", err.getvalue())

    def test_unreadable_source_is_ocr_error(self):
        with mock.patch.object(fsutil, "sha256_file", side_effect=OSError("gone")):
            with self.assertRaises(paddle_ocr.OcrError) as ctx:
                paddle_ocr.save_text(
                    self.pdf, [dict(page=1, text="text", score=.9, lines=1)], 1)
        self.assertIsInstance(ctx.exception.__cause__, OSError)


class CheckFileReadFailureTest(unittest.TestCase):
    def test_read_oserror_returns_cli_error(self):
        from unittest import mock
        with mock.patch("os.path.isfile", return_value=True):
            with mock.patch("builtins.open", side_effect=PermissionError("denied")):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = convert.check_file("existing.pdf")
        self.assertEqual(code, 1)
        self.assertIn("读取失败", err.getvalue())



@unittest.skipUnless(HAS_PYMUPDF, "需要本机安装 pymupdf")
class OcrMergeFailureSafetyTest(unittest.TestCase):
    """用真实单页 PDF 走完整前置校验，确保失败点确实落在写入/指纹对比上。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="pdf2md_mergefail_")
        self.pdf = os.path.join(self.dir, "a.pdf")
        make_text_pdf(self.pdf, lines=("这是一页真实 PDF 正文。",), pages=1)
        self.md = os.path.splitext(self.pdf)[0] + ".md"
        with open(self.md, "w", encoding="utf-8") as fh:
            fh.write("# 原文\n\n原始 Markdown 内容")
        self.txt = paddle_ocr.text_path_for(self.pdf)
        paddle_ocr.save_text(self.pdf, [dict(page=1, text="OCR 正文", score=.9, lines=1)], 1)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_atomic_replace_failure_keeps_original_markdown(self):
        original = open(self.md, encoding="utf-8").read()
        with mock.patch.object(fsutil, "atomic_write_text", side_effect=OSError("disk full")):
            with self.assertRaises(ocr_merge.MergeError) as ctx:
                ocr_merge.merge_file(self.pdf)
        self.assertIn("写入正式 Markdown 失败", str(ctx.exception))
        self.assertEqual(open(self.md, encoding="utf-8").read(), original)

    def test_source_change_during_merge_is_rejected(self):
        real_read = fsutil.read_text_fallback
        def mutate_after_read(path):
            value = real_read(path)
            with open(self.pdf, "ab") as fh:
                fh.write(b"changed")
            return value
        with mock.patch.object(fsutil, "read_text_fallback", side_effect=mutate_after_read):
            with self.assertRaises(ocr_merge.MergeError) as ctx:
                ocr_merge.merge_file(self.pdf)
        self.assertIn("合并期间 PDF 发生变化", str(ctx.exception))


class BatchWriteFailureTest(unittest.TestCase):
    def test_one_output_failure_does_not_stop_later_files(self):
        root = tempfile.mkdtemp(prefix="pdf2md_batch_")
        try:
            for name in ("a.pdf", "b.pdf"):
                with open(os.path.join(root, name), "wb") as fh:
                    fh.write(b"%PDF-1.4")
            report = dict(gate={"state": "pass", "chars": 120, "reason": "",
                                "cjk": .9, "bad": 0}, backend="builtin", md="中文" * 60,
                          shape="截图OCR", pages=1, imgs_kept=0, ocr_txt=None, resumed=False)
            calls = []
            real_write = fsutil.atomic_write_text
            def fail_first(path, text):
                calls.append(path)
                if path.endswith("a.md"):
                    raise OSError("disk full")
                return real_write(path, text)
            with mock.patch.object(convert, "convert_file", return_value=report):
                with mock.patch.object(fsutil, "atomic_write_text", side_effect=fail_first):
                    code = batch_convert.main([root])
            self.assertEqual(code, 1)
            self.assertTrue(os.path.isfile(os.path.join(root, "b.md")))
            self.assertFalse(os.path.exists(os.path.join(root, "a.md")))
            self.assertEqual(len(calls), 2)
        finally:
            shutil.rmtree(root, ignore_errors=True)


class BatchTargetTest(unittest.TestCase):
    def test_targets(self):
        root = os.path.join("src", "题库")
        pdf = os.path.join(root, "面经", "a.pdf")
        self.assertEqual(batch_convert.target_for(pdf, root),
                         os.path.join(root, "面经", "a.md"))
        self.assertEqual(batch_convert.target_for(pdf, root, out="out"),
                         os.path.join("out", "a.md"))
        self.assertEqual(batch_convert.target_for(pdf, root, mirror="mirror"),
                         os.path.join("mirror", "面经", "a.md"))


def _image_pdf(w, h, rgb):
    """最小截图型 PDF（与 test_convert 的夹具同构，这里独立一份避免跨文件依赖）。"""
    import zlib
    pix = zlib.compress(rgb, 6)
    head = (b"%PDF-1.4\n1 0 obj\n<< /Type /XObject /Subtype /Image "
            + b"/Width %d /Height %d " % (w, h)
            + b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
            + b"/Length %d >>\nstream\n" % len(pix))
    return head + pix + b"\nendstream\nendobj\n%%EOF\n"


if __name__ == "__main__":
    unittest.main()
