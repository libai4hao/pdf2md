# -*- coding: utf-8 -*-
"""PaddleOCR-json 页级 OCR：本地引擎、零 API 成本，带完整性 sidecar 与断点续跑。

迁自上游知识库管线的 `ocr_pages.py`，保留的实测约束与细节：

  - 引擎协议：stdin 每行一个 `{"image_path": "..."}`；stdout 返回 JSON，`code=100` 成功；
  - 进程 cwd 必须是引擎目录；
  - 启动后要等 `OCR init completed.`（实测初始化 0.8–1.0 s），否则第一页会撞超时；
  - **图片路径必须是 ASCII**，含中文会被引擎拒绝（文本输出路径不受限）；
  - 常驻进程一页一请求，避免反复加载模型（实测 0.8–1.0 s/页）；
  - 页级结果落盘为 `<pdf>.ocr.txt`，带 `===== page n =====` 页标记；
  - sidecar `<pdf>.ocr.txt.meta.json` 记录源 PDF sha256、总页数、已完成页、输出 hash、
    `complete` 标志：没有 sidecar 或 hash 对不上的旧文件**不算完成**，批量会重跑。

与上游的差异：储存在 PDF 旁的 sidecar（上游放在 build/ocr_text，靠台账 rel 定位），
所以换目录/换机器也能自证「这份 OCR 文本属于这份 PDF 且覆盖全页」。
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import fsutil

PAGE_MARK_FMT = "\n\n===== page {n} =====\n\n"
PAGE_MARK_RE = re.compile(r"^[ \t]*=====[ \t]*page[ \t]+(\d+)[ \t]*=====[ \t]*$", re.I | re.M)
EXE_NAME = "PaddleOCR-json.exe"
DEFAULT_DPI = 200
META_FORMAT_VERSION = 1


class OcrError(Exception):
    """OCR 不可用 / 引擎崩溃 / 超时：调用方决定是降级还是报错退出。"""


def default_dpi():
    try:
        return int(os.environ.get("PDF2MD_OCR_DPI") or DEFAULT_DPI)
    except ValueError:
        return DEFAULT_DPI


def _env(*names):
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


def engine_exe():
    """引擎可执行文件：PDF2MD_OCR_EXE → KB_OCR_ENGINE_EXE → <dir>/PaddleOCR-json.exe。"""
    explicit = _env("PDF2MD_OCR_EXE", "KB_OCR_ENGINE_EXE")
    if explicit:
        return explicit
    engine_dir = _env("PDF2MD_OCR_DIR", "KB_OCR_ENGINE_DIR")
    if engine_dir:
        return os.path.join(engine_dir, EXE_NAME)
    return None


def engine_dir(exe=None):
    """引擎目录（进程 cwd 必须是这里，否则引擎找不到模型文件）。"""
    exe = exe or engine_exe()
    if exe:
        return os.path.dirname(os.path.abspath(exe))
    return None


def available():
    exe = engine_exe()
    return bool(exe) and os.path.isfile(exe)


def flatten_texts(node, out):
    """从引擎返回的任意嵌套结构里把 text 收齐（上游 _flatten_texts）。"""
    if isinstance(node, dict):
        if isinstance(node.get("text"), str):
            out.append(node["text"])
        for value in node.values():
            flatten_texts(value, out)
    elif isinstance(node, list):
        for value in node:
            flatten_texts(value, out)


def collect_scores(node, out):
    if isinstance(node, dict):
        score = node.get("score")
        if isinstance(score, (int, float)):
            out.append(float(score))
        for value in node.values():
            collect_scores(value, out)
    elif isinstance(node, list):
        for value in node:
            collect_scores(value, out)


class PaddleEngine:
    """常驻引擎进程封装：一页一次请求，进程内复用模型。"""

    def __init__(self, exe=None, work_dir=None, startup_timeout=20.0, reply_timeout=120.0):
        self.exe = exe or engine_exe()
        self.work_dir = work_dir or engine_dir(self.exe)
        self.startup_timeout = startup_timeout
        self.reply_timeout = reply_timeout
        self.proc = None
        self.init_seconds = None
        self._q = queue.Queue()

    def __enter__(self):
        if not self.exe or not os.path.isfile(self.exe):
            raise OcrError("找不到 PaddleOCR-json：设置 PDF2MD_OCR_EXE 或 PDF2MD_OCR_DIR")
        started = time.perf_counter()
        self.proc = subprocess.Popen(
            [self.exe], cwd=self.work_dir,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        threading.Thread(target=self._pump, daemon=True).start()
        deadline = started + self.startup_timeout
        while time.perf_counter() < deadline:
            line = self._next_line(max(0.1, deadline - time.perf_counter()))
            if line and "OCR init completed" in line:
                self.init_seconds = round(time.perf_counter() - started, 2)
                return self
        self.__exit__(None, None, None)
        raise OcrError("引擎启动超时：未见 'OCR init completed.'（%s）" % self.exe)

    def _pump(self):
        for line in self.proc.stdout:
            self._q.put(line)
        self._q.put(None)

    def _next_line(self, timeout):
        try:
            item = self._q.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is None:
            raise OcrError("OCR 引擎进程已退出")
        return item.strip()

    def ocr(self, image_path):
        path = str(image_path)
        if not path.isascii():
            raise OcrError("图片路径必须是 ASCII（引擎限制）：%s" % path)
        if not self.proc or not self.proc.stdin:
            raise OcrError("引擎未启动")
        self.proc.stdin.write(json.dumps({"image_path": path}, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        deadline = time.perf_counter() + self.reply_timeout
        while time.perf_counter() < deadline:
            line = self._next_line(max(0.1, deadline - time.perf_counter()))
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or "code" not in payload:
                continue
            if str(payload.get("code")) != "100":
                raise OcrError("OCR 返回失败码 %s：%s" % (
                    payload.get("code"), str(payload.get("data"))[:160]))
            texts, scores = [], []
            flatten_texts(payload.get("data"), texts)
            collect_scores(payload.get("data"), scores)
            return dict(code=payload.get("code"), text="\n".join(texts),
                        score=round(sum(scores) / len(scores), 4) if scores else None,
                        lines=len(texts))
        raise OcrError("OCR 无响应超过 %ss：%s" % (self.reply_timeout, os.path.basename(path)))

    def __exit__(self, *exc):
        if self.proc:
            try:
                if self.proc.stdin:
                    self.proc.stdin.close()
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:                             # noqa: BLE001
                self.proc.kill()
            self.proc = None


def parse_range(text, total):
    """'1-3,7' → [1,2,3,7]。非法段跳过而非抛异常。

    首字符的 `-` 是负号不是范围分隔——上游原版 `split('-', 1)` 会把 '-1' 拆成空串
    再 ValueError，这里按 2026-11-18 的修复版实现。
    """
    out = []
    for chunk in str(text).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk[1:]:
            a_s, b_s = chunk.split("-", 1)
            try:
                out.extend(range(int(a_s), int(b_s) + 1))
            except ValueError:
                continue
        else:
            try:
                out.append(int(chunk))
            except ValueError:
                continue
    return [p for p in out if 1 <= p <= total]


def _ascii_dir(base=None):
    """引擎只收 ASCII 图片路径：拿不到 ASCII 目录就直接失败，不猜。"""
    base = base or tempfile.gettempdir()
    if str(base).isascii():
        return tempfile.mkdtemp(prefix="pdf2md_ocr_", dir=base)
    fallback = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Temp")
    if os.path.isdir(fallback) and str(fallback).isascii():
        return tempfile.mkdtemp(prefix="pdf2md_ocr_", dir=fallback)
    raise OcrError("临时目录含非 ASCII 字符，PaddleOCR-json 无法读取：%s" % base)


def ocr_pages(pdf_path, pages, engine, dpi=None, work_dir=None):
    """逐页 渲染 → OCR → 删图，图片不落盘累积。返回 [{page,text,score,lines,seconds}]。"""
    import structured

    dpi = dpi or default_dpi()
    temp = work_dir or _ascii_dir()
    os.makedirs(temp, exist_ok=True)
    png = os.path.join(temp, "page_now.png")          # 复用同一文件名：天然 ASCII 且不累积
    out = []
    try:
        for pno in pages:
            started = time.perf_counter()
            structured.render_page_png(pdf_path, pno, png, dpi)
            res = engine.ocr(png)
            out.append(dict(page=pno, text=res["text"], score=res["score"],
                            lines=res["lines"], seconds=round(time.perf_counter() - started, 2)))
    finally:
        try:
            os.unlink(png)
        except OSError:
            pass
        if work_dir is None:
            shutil.rmtree(temp, ignore_errors=True)
    return out


def text_path_for(pdf_path, txt_path=None):
    return Path(txt_path) if txt_path else Path(str(pdf_path) + ".ocr.txt")


def meta_path_for(txt_path):
    return Path(str(txt_path) + ".meta.json")


def save_text(pdf_path, pages, total_pages, txt_path=None, dpi=None, engine=None):
    """原子落盘 OCR 全文 + sidecar；sidecar 证明「属于这份 PDF、覆盖全页、内容未改」。"""
    txt = text_path_for(pdf_path, txt_path)
    text = render_pages_text(pages, page_marks=True)
    fsutil.atomic_write_text(txt, text)
    page_numbers = sorted({int(p["page"]) for p in pages})
    all_pages = list(range(1, int(total_pages) + 1))
    meta = dict(
        format_version=META_FORMAT_VERSION,
        source_sha256=fsutil.sha256_file(pdf_path),
        total_pages=int(total_pages),
        completed_pages=page_numbers,
        complete=len(pages) == len(page_numbers) == int(total_pages) and page_numbers == all_pages,
        output_sha256=fsutil.sha256_text(text),
        chars=len(text),
        body_chars=sum(len((p["text"] or "").strip()) for p in pages),
        page_markers=True,
        dpi=dpi or default_dpi(),
        engine=engine or engine_exe(),
    )
    fsutil.atomic_write_text(meta_path_for(txt), json.dumps(meta, ensure_ascii=False, indent=1))
    return txt


def is_complete(pdf_path, total_pages=None, txt_path=None):
    """sidecar 校验：源指纹、页集、输出 hash 全对上才算完成。异常一律 False。"""
    try:
        txt = text_path_for(pdf_path, txt_path)
        meta_path = meta_path_for(txt)
        if not txt.is_file() or not meta_path.is_file():
            return False
        if total_pages is None:
            import structured
            total_pages = structured.page_count(pdf_path)
        total_pages = int(total_pages)
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if (meta.get("format_version") != META_FORMAT_VERSION
                or meta.get("complete") is not True
                or meta.get("total_pages") != total_pages
                or meta.get("completed_pages") != list(range(1, total_pages + 1))):
            return False
        # 空文本不算完成：引擎「成功」返回空串是常见故障（缺语言模型/模型未加载），
        # 复用它会永久固化一份空产物，而重跑的成本只是重跑一次。
        # 用 body_chars 而不是 chars——后者含页标记，空页也会 >0。
        if int(meta.get("body_chars", meta.get("chars") or 0)) <= 0:
            return False
        if meta.get("source_sha256") != fsutil.sha256_file(pdf_path):
            return False
        return meta.get("output_sha256") == fsutil.sha256_file(txt)
    except Exception:                                   # noqa: BLE001
        return False


def load_pages(txt_path):
    """把带页标记的 OCR 全文还原成 [{page,text}]；无标记时整篇当第 1 页。"""
    text = fsutil.read_text_fallback(txt_path)
    parts = PAGE_MARK_RE.split(text)
    if len(parts) == 1:
        return [dict(page=1, text=text.strip())]
    # split 带捕获组：['', '1', '正文', '2', '正文', ...]
    out = []
    for i in range(1, len(parts), 2):
        out.append(dict(page=int(parts[i]), text=(parts[i + 1] if i + 1 < len(parts) else "").strip()))
    return out


def render_pages_text(pages, page_marks=False):
    """页级结果 → md 正文。默认不写页标记（正文干净），需要回溯时开 page_marks。"""
    if page_marks:
        return "".join(PAGE_MARK_FMT.format(n=p["page"]) + (p["text"] or "") for p in pages).strip()
    return "\n\n".join((p["text"] or "").strip() for p in pages if (p["text"] or "").strip())


def run(pdf_path, pages=None, dpi=None, txt_path=None, engine=None, retries=1):
    """跑一份 PDF 的页级 OCR，返回 {pages, total_pages, txt, resumed, stats}。

    - 已有完整 sidecar → 直接读回，不重复烧 OCR（断点续跑）；
    - 引擎崩溃 → 重启一次再试，第二次仍失败则抛 OcrError；
    - OCR 期间源 PDF 被改写 → 抛 OcrError，绝不把结果标成完成。
    """
    import structured

    total = structured.page_count(pdf_path)
    txt = text_path_for(pdf_path, txt_path)
    if is_complete(pdf_path, total, txt_path=txt_path):
        return dict(pages=load_pages(txt), total_pages=total, txt=str(txt), resumed=True,
                    stats=dict(pages=total, chars=len(fsutil.read_text_fallback(txt))))
    todo = pages if pages else list(range(1, total + 1))
    source_sha = fsutil.sha256_file(pdf_path)
    attempt = 0
    while True:
        engine = engine or PaddleEngine()
        try:
            with engine:
                results = ocr_pages(pdf_path, todo, engine, dpi)
            break
        except (OcrError, TimeoutError) as exc:
            attempt += 1
            if attempt > retries:
                raise OcrError(str(exc))
            engine = None                            # 引擎崩溃 → 换新进程重试一次
    if fsutil.sha256_file(pdf_path) != source_sha:
        raise OcrError("PDF 在 OCR 期间被改写，拒绝标记完成：%s" % pdf_path)
    saved = save_text(pdf_path, results, total, txt_path=txt_path, dpi=dpi)
    chars = sum(len(p["text"] or "") for p in results)
    scores = [p["score"] for p in results if p["score"] is not None]
    return dict(pages=results, total_pages=total, txt=str(saved), resumed=False,
                stats=dict(pages=len(results), chars=chars,
                           avg_score=round(sum(scores) / len(scores), 4) if scores else None))
