# -*- coding: utf-8 -*-
"""结构化抽取：pymupdf4llm（标题/表格/列表）→ pypdf 纯文本兜底 + 内嵌配图抽取。

迁自上游知识库管线的 `kb/convert_one.py`，保留全部判定细节：

  - 图片小于 80 px 的一律跳过（图标/水印碎片）；
  - 颜色空间 > 3 通道（CMYK 等）先转 sRGB 再存，否则 `tobytes("png")` 会出错位像素；
  - 同一张图在多页重复出现（`>= max(4, 页数/3)` 页）判为装饰/页眉页脚，不落盘；
  - 同一份文件里重复的图按内容 md5 去重，只存一次；
  - 落盘文件名 `<stem>_<hash>.png`，md 末尾追加「## 配图」段，图片引用是相对路径。

与上游的差异（都在调用方可见、可回归）：
  - 图片目录由调用方给（pdf2md 用 `<stem>.images/`，避免同目录多份 PDF 撞 `images/`）；
  - 不绑定语料根 / 分类 / 台账，只做「PDF → md + 图片」；
  - 依赖全部可选：缺 pymupdf/pymupdf4llm/pypdf 时给明确提示，不静默降级。
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import os
import re
import sys

import fsutil
import quality

MIN_IMG_SIDE = 80              # 小于这个边长的图一律不要（图标/水印碎片）
DECORATIVE_MIN_PAGES = 4       # 装饰图判定下限：出现页数 >= max(4, 总页/3)
IMG_SECTION_MARK = "## 配图"
EXTRACTORS = ("pymupdf4llm", "pypdf")


class StructuredError(Exception):
    """结构化抽取不可用 / 抽取失败：缺依赖、PDF 打不开、抽取器报错。"""


def _findable(mod):
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def pymupdf_available():
    return _findable("pymupdf")


def pymupdf4llm_available():
    return pymupdf_available() and _findable("pymupdf4llm")


def pypdf_available():
    return _findable("pypdf")


def extractor_status():
    """各抽取器可用性快照，供 --check 打印。"""
    return {"pymupdf": pymupdf_available(),
            "pymupdf4llm": pymupdf4llm_available(),
            "pypdf": pypdf_available()}


def available(extractor="pymupdf4llm"):
    """这条结构化路径现在能不能用。

    `pypdf` 抽取器同样要 pymupdf：页数统计、逐页渲染、内嵌图抽取都靠它，
    只有文字抽取换成 pypdf——所以两个都得装。
    """
    if extractor not in EXTRACTORS:
        return False
    if extractor == "pypdf":
        return pypdf_available() and pymupdf_available()
    return pymupdf4llm_available()


def _pymupdf():
    try:
        import pymupdf
    except ImportError as exc:
        raise StructuredError("结构化路径需要 pymupdf：python -m pip install pymupdf") from exc
    return pymupdf


def page_count(pdf_path):
    """PDF 页数（pymupdf）。"""
    pymupdf = _pymupdf()
    try:
        with pymupdf.open(str(pdf_path)) as doc:
            return doc.page_count
    except Exception as exc:                              # noqa: BLE001
        raise StructuredError("PDF 打不开：%s" % exc) from exc


def render_page_png(pdf_path, pno, out_png, dpi=200):
    """把单页渲染成 PNG（1-based 页号），给页级 OCR 用。"""
    pymupdf = _pymupdf()
    if not 1 <= int(pno):
        raise ValueError("页号必须从 1 开始：%r" % (pno,))
    os.makedirs(os.path.dirname(os.path.abspath(str(out_png))), exist_ok=True)
    try:
        with pymupdf.open(str(pdf_path)) as doc:
            if int(pno) > doc.page_count:
                raise ValueError("页号超界：%d/%d" % (pno, doc.page_count))
            pix = doc[int(pno) - 1].get_pixmap(dpi=dpi)
            pix.save(str(out_png))
    except ValueError:
        raise
    except Exception as exc:                              # noqa: BLE001
        raise StructuredError("渲染第 %s 页失败：%s" % (pno, exc)) from exc
    return str(out_png)


def img_hash(data):
    return hashlib.md5(data).hexdigest()[:12]


def decorative_hashes(page_counts, total_pages):
    """出现页数 >= max(4, 总页/3) 的图片哈希集合（页眉/页脚/水印这类装饰图）。"""
    limit = max(DECORATIVE_MIN_PAGES, int(total_pages) // 3)
    return {h for h, pages in page_counts.items() if len(pages) >= limit}


def plan_saved(kept, decorative):
    """kept = [(hash, page_no_1based, data)] → 去重去装饰后要落盘的子集。"""
    saved, seen = [], set()
    for h, pno, data in kept:
        if h in decorative or h in seen:
            continue
        seen.add(h)
        saved.append((h, pno, data))
    return saved


def image_section(refs, images_rel, total=None):
    """md 末尾的「## 配图」段。refs = [(filename, page_no)]。

    `total` 只用于说明「入池多少张、去重后剩多少张」这类审计信息，默认不写进正文
    ——正文里的数字应当是实际落盘的张数，避免读者以为丢了图。
    """
    if not refs:
        return ""
    n = len(refs)
    head = "## 配图（%d张）" % n
    rel = images_rel.replace("\\", "/").strip("/")
    lines = ["", "---", "", head, ""]
    for fn, pno in refs:
        lines.append("![配图 原文第%s页](./%s/%s)" % (pno if pno else "?", rel, fn))
    return "\n".join(lines) + "\n"


def _collect_images(doc, images_dir, stem, write=True):
    """抽图 → (refs, kept, skipped)。refs = [(filename, page_no)]。

    `write=False` 只做统计、不落盘（`--check` 用：预检不能往语料目录里塞文件）。
    """
    if not images_dir:
        return [], 0, 0
    pymupdf = _pymupdf()
    page_counts = {}
    kept = []
    for pno in range(len(doc)):
        for im in doc[pno].get_images(full=True):
            xref = im[0]
            try:
                pix = pymupdf.Pixmap(doc, xref)
                if pix.width < MIN_IMG_SIDE or pix.height < MIN_IMG_SIDE:
                    continue
                if pix.colorspace is not None and pix.colorspace.n > 3:
                    pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                data = pix.tobytes("png")
            except Exception:                             # noqa: BLE001
                continue                                 # 解不出的图跳过，不猜
            h = img_hash(data)
            page_counts.setdefault(h, set()).add(pno)
            kept.append((h, pno + 1, data))
    decorative = decorative_hashes(page_counts, len(doc))
    saved = plan_saved(kept, decorative)
    refs = []
    for h, pno, data in saved:
        fn = "%s_%s.png" % (stem, h)
        if write:
            fsutil.atomic_write_bytes(os.path.join(str(images_dir), fn), data)
        refs.append((fn, pno))
    return refs, len(saved), len(kept) - len(saved)


def _md_via_pypdf(path):
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise StructuredError("pypdf 兜底需要 pypdf：python -m pip install pypdf") from exc
    import logging
    logging.getLogger("pypdf").setLevel(logging.CRITICAL)
    try:
        reader = PdfReader(str(path))
        return "\n\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as exc:                              # noqa: BLE001
        raise StructuredError("pypdf 抽取失败：%s" % exc) from exc


def _md_via_pymupdf4llm(doc):
    try:
        import pymupdf4llm
    except ImportError as exc:
        raise StructuredError(
            "pymupdf4llm 未安装：python -m pip install pymupdf4llm") from exc
    try:
        return pymupdf4llm.to_markdown(doc, write_images=False, show_progress=False)
    except Exception as exc:                              # noqa: BLE001
        raise StructuredError("pymupdf4llm 抽取失败：%s" % exc) from exc


def _messages_to_stderr(pymupdf):
    """把 PyMuPDF 的 message 流临时改到 stderr，返回原流以便恢复。

    pymupdf4llm 遇到解析告警会 `pymupdf.message("=== Document parser messages ===")`
    加一段明细。它走 PyMuPDF 自己的输出流而不是 `sys.stdout`，所以 redirect_stdout
    拦不住；而本工具 dry-run 的 stdout 就是 md 本体，一行噪声就把产物弄坏。
    """
    old = getattr(pymupdf, "_g_out_message", None)
    try:
        pymupdf.set_messages(stream=sys.stderr)
    except Exception:                                     # noqa: BLE001
        return None
    return old


def _restore_messages(pymupdf, old):
    if old is None:
        return
    try:
        pymupdf.set_messages(stream=old)
    except Exception:                                     # noqa: BLE001
        pass


def _prefer_pypdf_fallback(primary_md, candidate_md):
    """只有质量门禁等级提升时才接受 pypdf 候选，避免按长度盲选。"""
    primary_gate = quality.assess(primary_md or "")
    candidate_gate = quality.assess(candidate_md or "")
    rank = {"fail": 0, "review": 1, "pass": 2}
    return rank[candidate_gate["state"]] > rank[primary_gate["state"]]


def _pypdf_fallback_md(path, primary_md):
    """主抽取质量未达标时试 pypdf：候选本身失败不能拖垮主结果，只接受更优候选。"""
    try:
        candidate = _md_via_pypdf(path)
    except StructuredError as exc:
        print("警告：pypdf 兜底失败，保留主抽取结果：%s" % exc, file=sys.stderr)
        return None
    return candidate if _prefer_pypdf_fallback(primary_md, candidate) else None


def extract(path, images_dir=None, images_rel="images", stem=None,
            extractor="pymupdf4llm", allow_fallback=True, write_images=True):
    """PDF → (结构化 md, 图片段)。返回 dict：md/pages/extractor/imgs_kept/imgs_skipped。

    `allow_fallback`：主抽取结果未通过质量门禁（空正文/过短/乱码/低中文）且 pypdf 可用时，
    重抽一次；只有候选质量等级更高才采用，候选自身失败不影响主结果。
    `write_images=False`：只统计配图、不落盘（`--check` 用，预检必须零写盘）。
    """
    if extractor not in EXTRACTORS:
        raise StructuredError("未知抽取器：%r（可选 %s）" % (extractor, "/".join(EXTRACTORS)))
    pymupdf = _pymupdf()
    stem = fsutil.safe_name(stem or os.path.splitext(os.path.basename(str(path)))[0]) or "doc"
    old_stream = _messages_to_stderr(pymupdf)
    try:
        try:
            doc = pymupdf.open(str(path))
        except Exception as exc:                          # noqa: BLE001
            raise StructuredError("PDF 打不开：%s" % exc) from exc
        try:
            with contextlib.redirect_stdout(sys.stderr):
                pages = len(doc)
                used = extractor
                if extractor == "pymupdf4llm":
                    md = _md_via_pymupdf4llm(doc)
                    # 不只兜底完全空正文：长度/乱码/CJK 门禁发现的明显低质量结果，
                    # 若 pypdf 候选质量等级更高才替换，避免单纯按文本长度误选。
                    if (allow_fallback and pypdf_available()
                            and quality.assess(md or "")["state"] != "pass"):
                        candidate = _pypdf_fallback_md(path, md)
                        if candidate is not None:
                            # 空正文仍沿用原标签，老调用方看到的形态说明不变
                            used = ("pypdf(空正文兜底)" if not (md or "").strip()
                                    else "pypdf(质量兜底)")
                            md = candidate
                else:
                    md = _md_via_pypdf(path)
                # 正文抽成功之后才落图：抽取失败不留孤立图片文件
                refs, kept, skipped = _collect_images(
                    doc, images_dir, stem, write=write_images)
        finally:
            doc.close()
    finally:
        _restore_messages(pymupdf, old_stream)
    md = (md or "").replace("\r\n", "\n").strip()
    section = image_section(refs, images_rel)
    if section:
        md = (md + "\n" + section) if md else section.lstrip("\n")
    return dict(md=md, pages=pages, extractor=used,
                imgs_kept=kept, imgs_skipped=skipped, images=refs)


def looks_structured(md):
    """粗判正文是否有结构化语法（标题/列表/表格），供 --check 说明。"""
    return bool(re.search(r"^(#{1,6} |\| |[-*] |\d+\. )", md or "", re.M))
