#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
outline.py 回归测试：章节表重排 + PDF 内嵌链接复原
运行：python -m unittest tests.test_outline -v
需要 pymupdf（生成合成 PDF）；缺失则整类跳过。
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

try:
    import pymupdf
except ImportError:  # pragma: no cover
    pymupdf = None

import outline  # noqa: E402


def wt(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def make_pdf(path):
    """两页：目录页（带项目符号）+ 正文页（两级标题 + 一个带链接的条目）。"""
    doc = pymupdf.open()
    p1 = doc.new_page()
    p1.insert_text((72, 72), "目录", fontsize=20)
    for i, line in enumerate(("·教程", "。Java", "。SpringBoot", "- Markdown")):
        p1.insert_text((90, 110 + i * 20), line, fontsize=12)

    p2 = doc.new_page()
    p2.insert_text((72, 80), "教程", fontsize=20)
    p2.insert_text((72, 120), "Java", fontsize=14)
    name = "mall"
    p2.insert_text((72, 160), "1. %s：一套电商系统" % name, fontsize=12)
    rect = pymupdf.Rect(88, 146, 118, 164)
    p2.insert_link({"kind": pymupdf.LINK_URI, "from": rect, "uri": "https://github.com/macrozheng/mall"})
    doc.save(path)
    doc.close()


TEXT = """===== page 1 =====

目录
·教程
。Java
。SpringBoot
-Markdown

===== page 2 =====

教程
Java
1. mall：一套电商系统
2. 未收录项目：没有链接
"""

HEADINGS = "2\t教程\n3\tJava\n"


@unittest.skipIf(pymupdf is None, "需要 pymupdf")
class OutlineTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.pdf = os.path.join(self.td.name, "sample.pdf")
        self.txt = os.path.join(self.td.name, "sample.pdf.ocr.txt")
        self.tsv = os.path.join(self.td.name, "headings.tsv")
        make_pdf(self.pdf)
        wt(self.txt, TEXT)
        wt(self.tsv, HEADINGS)

    def tearDown(self):
        self.td.cleanup()

    def test_headings_and_links(self):
        lines, report = outline.run(self.pdf, self.txt, self.tsv, drop_toc=True)
        md = "\n".join(lines)
        self.assertIn("## 教程", md)
        self.assertIn("### Java", md)
        self.assertIn("[mall](https://github.com/macrozheng/mall)：一套电商系统", md)
        self.assertEqual(report["章节"]["缺失"], [])
        self.assertEqual(report["链接"]["已复原"], 1)
        self.assertIn("未收录项目", report["链接"]["未匹配"])

    def test_toc_dropped_and_tail_trimmed(self):
        lines, report = outline.run(self.pdf, self.txt, self.tsv, drop_toc=True)
        self.assertEqual(report["丢弃目录页"], 1)
        self.assertEqual(report.get("丢弃目录尾", 0), 0, "目录页整体被删后不应再有目录尾")
        self.assertEqual(lines[0], "## 教程")

    def test_no_headings_keeps_flat(self):
        lines, report = outline.run(self.pdf, self.txt, None, links=True)
        self.assertNotIn("## 教程", "\n".join(lines))
        self.assertEqual(report["链接"]["已复原"], 1)

    def test_cli_check_writes_nothing(self):
        out = os.path.join(self.td.name, "out.md")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "outline.py"), self.pdf,
                            "--text", self.txt, "--headings", self.tsv, "--drop-toc", "--check"],
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(out))
        report = json.loads(r.stdout)
        self.assertIsNone(report["输出"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
