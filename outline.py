#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OCR / 文字层产物的结构后处理：章节表重排 + PDF 内嵌链接复原
=========================================================
页级 OCR 只给"一页一页的扁平文字"，字号/缩进/链接全丢了。本模块做两件确定性的事：

1. **章节表重排**：给一份 `level<TAB>标题` 的章节表（可以从原文目录页抄一次，
   也可以手工写），按顺序在扁平文本里定位每个标题行并写成 `## 标题`；
   OCR 的错字不会带进标题，标题用章节表里的措辞。
2. **链接复原**：PDF 里的链接是注释（annotation），文字层坏了也不影响；
   按 URL 末段与条目名做 slug 匹配，把 `1. mall：...` 改写成 `1. [mall](url)：...`。

用法：
  py outline.py 文档.pdf --text 文档.pdf.ocr.txt --headings 章节.tsv -o 文档.md
  py outline.py 文档.pdf --text 文档.pdf.ocr.txt --links --strip-page-marks -o 文档.md
  py outline.py 文档.pdf --text 文档.pdf.ocr.txt --headings 章节.tsv --check    # 只报计划

章节表（UTF-8，制表符分隔，空行与 # 注释忽略）：
  2	教程
  3	Java
  3	SpringBoot

依赖：pymupdf（可选后端里的同一个包）；纯文本处理部分是标准库。
"""
import argparse
import json
import os
import re
import sys

__version__ = "0.1.0"

PAGE_RE = re.compile(r"^=====\s*page\s*(\d+)\s*=====\s*$")
ITEM_RE = re.compile(r"^(\d{1,2})\s*[.．]\s*(\*\*[^*]+\*\*|[^*：:]{1,40}?)\s*([：:].*)?$")


def _pymupdf():
    try:
        import pymupdf
        return pymupdf
    except ImportError:
        try:
            import fitz as pymupdf
            return pymupdf
        except ImportError:
            raise SystemExit("需要 pymupdf：python -m pip install pymupdf")


def read_text(path):
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise SystemExit("无法识别编码：" + path)


def norm(s):
    s = s.replace("（", "(").replace("）", ")").replace("\u3000", " ")
    return re.sub(r"[ \t]+", "", s).lower()


def split_pages(text):
    """去掉 `===== page n =====` 记号，按行返回。"""
    pages, cur = [], []
    for line in text.split("\n"):
        m = PAGE_RE.match(line.strip())
        if m:
            if cur:
                pages.append(cur)
            cur = []
            continue
        if line.strip():
            cur.append(line.rstrip())
    if cur:
        pages.append(cur)
    return pages


TOC_BULLET = "·。•■○□▪●"


def is_toc_page(lines):
    """目录页判据：多数行以项目符号开头，或含「目录」且短行占绝大多数。"""
    if len(lines) < 5:
        return False
    bulleted = sum(1 for l in lines if l[:1] in TOC_BULLET)
    if bulleted >= len(lines) * 0.6:
        return True
    return any("目录" == l.strip() for l in lines) and bulleted >= len(lines) * 0.3


def load_headings(path):
    items = []
    for n, line in enumerate(read_text(path).split("\n"), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" not in line:
            raise SystemExit("章节表第 %d 行缺制表符：%s" % (n, line))
        level, title = line.split("\t", 1)
        level = level.strip()
        title = title.strip()
        if level not in ("1", "2", "3", "4", "5", "6") or not title:
            raise SystemExit("章节表第 %d 行格式不对：%s" % (n, line))
        items.append((int(level), title))
    return items


def place_headings(lines, headings):
    """按章节表顺序在正文里定位标题行；定位不到就记入 missing，不猜。"""
    placed, missing, cursor = [], [], 0
    for level, title in headings:
        target = norm(title)
        found = -1
        for i in range(cursor, len(lines)):
            cand = norm(lines[i])
            if not cand or len(cand) > 60:
                continue
            if cand == target or (len(target) >= 4 and (cand.startswith(target) or cand.endswith(target))):
                found = i
                break
        if found < 0:
            missing.append(title)
            continue
        lines[found] = "#" * level + " " + title
        placed.append({"行号": found, "级别": level, "标题": title})
        cursor = found + 1
    return placed, missing


def _slug(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _url_key(uri):
    """URL 建索引的键：末段优先；末段为空（只有域名）时退回主机名首段。"""
    body = re.sub(r"^https?://", "", uri)
    host, _, path = body.partition("/")
    path = path.strip("/")
    if path:
        tail = _slug(path.split("/")[-1])
        if tail:
            return tail
    return _slug(host.split(".")[0])


def lookup_link(links, name):
    """先精确命中，再容忍 ±2 字符的前缀差异（mycat → MyCat2 这类）。"""
    key = _slug(name)
    if not key:
        return None
    if key in links:
        return links[key]
    if len(key) >= 4:
        for cand, uri in links.items():
            if len(cand) >= 4 and cand.startswith(key) and len(cand) - len(key) <= 2:
                return uri
    return None


def collect_links(pdf_path):
    """PDF 注释里的 URI，按 URL 末段建索引（同一个 URL 只留第一条）。"""
    links = {}
    with _pymupdf().open(pdf_path) as doc:
        for page in doc:
            for link in page.get_links():
                uri = link.get("uri")
                if not uri or "://" not in uri:
                    continue
                key = _url_key(uri)
                if key and key not in links:
                    links[key] = uri
    return links


def attach_links(lines, links):
    """条目行里 `N. 名称：…` 的名称命中 URL 末段时，改写成 Markdown 链接。"""
    attached, unmatched = [], []
    for i, line in enumerate(lines):
        m = ITEM_RE.match(line)
        if not m:
            continue
        name = m.group(2).strip().strip("*").strip().lstrip("．.·•-— ")
        uri = lookup_link(links, name)
        if not uri:
            unmatched.append(name)
            continue
        tail = m.group(3) or ""
        bold = m.group(2).strip().startswith("**")
        label = "**[%s](%s)**" % (name, uri) if bold else "[%s](%s)" % (name, uri)
        lines[i] = "%s. %s%s" % (m.group(1), label, tail)
        attached.append({"行号": i, "名称": name, "链接": uri})
    return attached, unmatched


def run(pdf, text_path, headings_path=None, links=True, strip_marks=True, drop_toc=False, start_at=None):
    text = read_text(text_path)
    pages = split_pages(text) if strip_marks else [l for l in text.split("\n") if l.strip()]
    dropped = 0
    if drop_toc:
        kept = [p for p in pages if not is_toc_page(p)]
        dropped = len(pages) - len(kept)
        pages = kept
    lines = [l for page in pages for l in page]
    if start_at:
        idx = [i for i, l in enumerate(lines) if norm(start_at) in norm(l)]
        if not idx:
            raise SystemExit("--start-at 没在正文里找到：%s" % start_at)
        lines = lines[idx[0]:]
    report = {"页数": len(pages), "行数": len(lines)}
    if dropped:
        report["丢弃目录页"] = dropped

    if headings_path:
        placed, missing = place_headings(lines, load_headings(headings_path))
        report["章节"] = {"已定位": len(placed), "缺失": missing, "明细": placed}
        if drop_toc and placed and placed[0]["行号"] > 0:
            lead = lines[:placed[0]["行号"]]
            if all(len(l) <= 12 or l[:1] in TOC_BULLET or l[:2] in ("- ", "* ") for l in lead):
                lines = lines[placed[0]["行号"]:]
                for item in placed:
                    item["行号"] -= len(lead)
                report["丢弃目录尾"] = len(lead)

    if links:
        attached, unmatched = attach_links(lines, collect_links(pdf))
        report["链接"] = {"已复原": len(attached), "未匹配": unmatched, "明细": attached}
    return lines, report


def main():
    ap = argparse.ArgumentParser(description="OCR/文字层产物：章节表重排 + PDF 内嵌链接复原")
    ap.add_argument("pdf", help="源 PDF（用来读链接注释）")
    ap.add_argument("--text", required=True, help="扁平文本（如 <pdf>.ocr.txt）")
    ap.add_argument("--headings", help="章节表：level<TAB>标题，按原文顺序")
    ap.add_argument("--links", dest="links", action="store_true", default=True, help="复原 PDF 内嵌链接（默认开）")
    ap.add_argument("--no-links", dest="links", action="store_false", help="不做链接复原")
    ap.add_argument("--strip-page-marks", dest="strip_marks", action="store_true", default=True,
                    help="去掉 ===== page n ===== 记号（默认开）")
    ap.add_argument("--keep-page-marks", dest="strip_marks", action="store_false", help="保留页标记")
    ap.add_argument("--drop-toc", action="store_true", help="按符号密度丢掉目录页（多列目录字被 OCR 打散时用）")
    ap.add_argument("--start-at", help="从正文里首次出现该文本的行开始（可替代 --drop-toc）")
    ap.add_argument("-o", "--output", help="输出 md（默认 <text 去扩展名>.outline.md）")
    ap.add_argument("--check", action="store_true", help="只输出计划，不写盘")
    ap.add_argument("--overwrite", action="store_true", help="允许覆盖已有输出")
    args = ap.parse_args()

    for path in (args.pdf, args.text, args.headings):
        if path and not os.path.isfile(path):
            sys.exit("文件不存在：%s" % path)

    out = args.output or os.path.splitext(args.text)[0] + ".outline.md"
    if os.path.exists(out) and not (args.check or args.overwrite):
        sys.exit("目标已存在，加 --overwrite 才会覆盖：%s" % out)

    lines, report = run(args.pdf, args.text, args.headings, args.links, args.strip_marks,
                        args.drop_toc, args.start_at)
    md = "\n".join(lines).strip() + "\n"
    report["输出"] = None if args.check else out
    if not args.check:
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            f.write(md)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
