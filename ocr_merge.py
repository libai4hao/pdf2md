# -*- coding: utf-8 -*-
"""OCR 定稿合并：把页级 OCR 全文回填成该 PDF 的正式 md，同时保住「## 配图」段。

迁自上游知识库管线的 `ocr_merge.py`，保留三条关键行为：

  1. 新正文 = OCR 全文（页标记可选），旧 md 的「## 配图」段原样接回——结构化路径抽出的
     图片引用不能因为一次 OCR 重做而丢；
  2. 旧 md 先备份，且**只在没有备份时备份**：反复合并不会用上一次的产物顶掉最初那份；
  3. 写盘用原子替换，合并幂等（重复跑得到同样内容）。

与上游的差异：不再从 gate_report.json 圈合并集（这里没有台账），改为按文件对
（PDF + `<pdf>.ocr.txt`）就地合并；备份落在 `<stem>.md.ocr.bak`。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import fsutil
import paddle_ocr

IMG_MARK = "## 配图"


class MergeError(Exception):
    """合并前置条件不满足：缺 OCR 全文 / 缺目标 md / OCR 未覆盖全页。"""


def split_images(old_md):
    """拆出「## 配图」段（含其前面那条 --- 分隔线），返回 (正文, 配图段)。"""
    idx = old_md.find(IMG_MARK)
    if idx < 0:
        return old_md, ""
    start = old_md.rfind("---", 0, idx)
    start = start if start >= 0 else idx
    return old_md[:start].rstrip() + "\n", old_md[start:].strip() + "\n"


def split_front_matter(md):
    """拆出 H1 + `>` 注记块（pdf2md 产物的固定头部），返回 (头部, 正文)。"""
    lines = md.splitlines()
    end = 0
    if lines and lines[0].startswith("# "):
        end = 1
        while end < len(lines) and (not lines[end].strip()
                                    or lines[end].lstrip().startswith(">")):
            end += 1
    head = "\n".join(lines[:end]).rstrip()
    return (head + "\n\n" if head else ""), "\n".join(lines[end:])


def refresh_head(head):
    """头部注记改写：形态/说明必须如实指向页级 OCR，不能沿用旧路径的说法。"""
    if not head.strip():
        return ""
    out = [ln for ln in head.rstrip("\n").splitlines()
           if not ln.startswith("> 形态：") and not ln.startswith("> 说明：")]
    while out and not out[-1].strip():
        out.pop()
    out += ["> 形态：页级OCR（PaddleOCR-json 合并）",
            "> 说明：正文来自页级 OCR，与同名 .ocr.txt 同源；原「配图」段保留。"]
    return "\n".join(out) + "\n\n"


def merge_text(ocr_body, old_md):
    """新 md = 旧头部（注记改写）+ OCR 正文 +（若原 md 有配图段）原配图段。"""
    head, old_body = split_front_matter(old_md)
    _, img_sec = split_images(old_body)
    new = refresh_head(head) + (ocr_body or "").strip()
    if img_sec:
        new = new + ("\n\n" if new.strip() else "") + img_sec
    return new.rstrip() + "\n"


def _md_path_for(pdf_path):
    return os.path.splitext(str(pdf_path))[0] + ".md"


def _backup_path_for(md_path):
    return md_path + ".ocr.bak"


def _sha256_file(path, what):
    """指纹读取失败也算合并前置条件不满足，不让 OSError 直接冒泡成 traceback。"""
    try:
        return fsutil.sha256_file(path)
    except OSError as exc:
        raise MergeError("读取%s指纹失败：%s" % (what, exc)) from exc


def merge_file(pdf_path, txt_path=None, dry_run=False):
    """把 `<pdf>.ocr.txt` 合并进 `<stem>.md`。返回报告 dict；失败抛 MergeError。"""
    pdf_path = str(pdf_path)
    md_path = _md_path_for(pdf_path)
    txt = paddle_ocr.text_path_for(pdf_path, txt_path)
    if not os.path.isfile(txt):
        raise MergeError("缺 OCR 全文：%s（先跑 paddle 后端，或用 --ocr-text 指定）" % txt)
    source_sha = _sha256_file(pdf_path, "PDF")
    if not paddle_ocr.is_complete(pdf_path, txt_path=txt_path):
        raise MergeError("OCR 全文不完整或与当前 PDF 不符（sidecar 校验失败）：%s" % txt)
    if not os.path.isfile(md_path):
        raise MergeError("目标 md 不存在：%s（先跑一次转换再合并配图）" % md_path)

    pages = paddle_ocr.load_pages(txt)
    ocr_body = paddle_ocr.render_pages_text(pages, page_marks=False)
    old_md = fsutil.read_text_fallback(md_path)
    if _sha256_file(pdf_path, "PDF") != source_sha:
        raise MergeError("合并期间 PDF 发生变化，拒绝提交：%s" % pdf_path)
    body_before, img_sec = split_images(old_md)
    new_md = merge_text(ocr_body, old_md)
    report = dict(md=md_path, ocr_txt=str(txt), pages=len(pages),
                  old_chars=len(body_before), new_chars=len(new_md),
                  imgs_kept=bool(img_sec), dry_run=bool(dry_run), written=False)
    if not dry_run:
        bak = _backup_path_for(md_path)
        if not os.path.exists(bak):
            fsutil.atomic_write_bytes(bak, old_md.encode("utf-8"))
            if fsutil.sha256_file(bak) != fsutil.sha256_text(old_md):
                raise MergeError("备份校验失败：%s" % bak)
        if _sha256_file(pdf_path, "PDF") != source_sha:
            raise MergeError("提交前 PDF 已变化，拒绝覆盖：%s" % pdf_path)
        try:
            fsutil.atomic_write_text(md_path, new_md)
        except OSError as exc:
            raise MergeError("写入正式 Markdown 失败（原文件保持不变）：%s" % exc) from exc
        if _sha256_file(md_path, "正式 Markdown") != fsutil.sha256_text(new_md):
            raise MergeError("正式 Markdown 写入后校验失败：%s" % md_path)
        report["written"] = True
    return report


def md_text_for_pdf(pdf_path, txt_path=None):
    """只读：返回把 OCR 全文合进现有 md 后的正文，不落盘。"""
    md_path = _md_path_for(pdf_path)
    old_md = fsutil.read_text_fallback(md_path) if os.path.isfile(md_path) else ""
    pages = paddle_ocr.load_pages(paddle_ocr.text_path_for(pdf_path, txt_path))
    return merge_text(paddle_ocr.render_pages_text(pages), old_md)


def main(argv=None):
    ap = argparse.ArgumentParser(description="把页级 OCR 全文合并回 PDF 同名 md（保住配图段）")
    ap.add_argument("pdf", help="PDF 文件路径")
    ap.add_argument("--ocr-text", default=None, help="OCR 全文路径（默认 <pdf>.ocr.txt）")
    ap.add_argument("--dry-run", action="store_true", help="只算差异，不写盘")
    args = ap.parse_args(argv)

    if not os.path.isfile(args.pdf):
        print("文件不存在：" + args.pdf, file=sys.stderr)
        return 1
    try:
        report = merge_file(args.pdf, txt_path=args.ocr_text, dry_run=args.dry_run)
    except MergeError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=1))
    if not args.dry_run:
        print("已写：%s（备份 %s）" % (report["md"], _backup_path_for(report["md"])),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
