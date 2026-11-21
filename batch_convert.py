# -*- coding: utf-8 -*-
"""批量把 PDF 转为 Markdown（工程批量入口）。

- 复用 convert.py 的全部清洗、题号标题化与后端路由，不另写一套；
- 默认对每个 PDF 调 convert_file()，产物落在各自 PDF 旁（同名 .md），
  已存在则由 convert 内建语义自动备份 .md.bak（批量模式下不备份，直接跳过）；
- 断点续跑：已存在同名 .md 自动跳过；--force 重转；
- 每件追加一行 JSONL（--log），崩了也能按行排查；末尾打后端/门禁分布。

用法：
    py batch_convert.py <根目录>                          # 扫描根目录下所有 .pdf
    py batch_convert.py <根目录> --force                  # 已有 .md 也重转
    py batch_convert.py <根目录> --mirror <目录>          # 镜像源目录结构（推荐）
    py batch_convert.py <根目录> --out <目录>             # 平铺收集到一个目录
    py batch_convert.py <根目录> --backend structured --strict
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import convert
import fsutil

GATE_LABEL = convert.GATE_LABEL


def iter_pdfs(root):
    for r, _dirs, files in os.walk(root):
        for f in files:
            if f.lower().endswith(".pdf"):
                yield os.path.join(r, f)


def target_for(pdf, root, out=None, mirror=None):
    """产物落点：--mirror 保层级 / --out 平铺 / 默认 PDF 旁。"""
    if mirror:
        rel = os.path.relpath(pdf, root)
        return os.path.join(mirror, os.path.splitext(rel)[0] + ".md")
    if out:
        return os.path.join(out, os.path.splitext(os.path.basename(pdf))[0] + ".md")
    return os.path.splitext(pdf)[0] + ".md"


def log_row(log_path, row):
    if not log_path:
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), **row},
                                ensure_ascii=False) + "\n")
    except OSError as exc:
        # 日志是旁路：写不进去不能把整批转码带崩
        print("警告：写日志失败（%s）：%s" % (log_path, exc), file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description="批量 PDF → Markdown")
    ap.add_argument("root", help="扫描根目录")
    ap.add_argument("--force", action="store_true", help="已有 .md 也重转")
    ap.add_argument("--out", help="统一收集到该目录（平铺，默认落在 PDF 旁边）")
    ap.add_argument("--mirror", help="镜像源目录结构输出到该目录（保持分类层级，推荐）")
    ap.add_argument("--backend", choices=convert.BACKENDS, default="auto",
                    help="auto（默认）/ builtin / structured / paddle")
    ap.add_argument("--extractor", choices=("pymupdf4llm", "pypdf"), default="pymupdf4llm",
                    help="结构化路径的抽取器，默认 pymupdf4llm")
    ap.add_argument("--ocr-fallback", action="store_true",
                    help="门禁不通过时降级到页级 OCR（需配置 PaddleOCR-json）")
    ap.add_argument("--page-marks", action="store_true",
                    help="页级 OCR 正文保留 ===== page n ===== 页标记")
    ap.add_argument("--strict", action="store_true",
                    help="质量门禁非 pass 的件不写盘，并计入失败（退出码 1）")
    ap.add_argument("--log", default=None, help="逐件 JSONL 日志路径（不写则不记）")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.root):
        print("错误：目录不存在：" + args.root, file=sys.stderr)
        return 1
    if args.out:
        os.makedirs(args.out, exist_ok=True)
    if args.mirror:
        os.makedirs(args.mirror, exist_ok=True)

    pdfs = sorted(iter_pdfs(args.root))
    print("扫描到 %d 个 PDF" % len(pdfs))

    ok = skipped = short = 0
    by_gate = {"pass": 0, "review": 0, "fail": 0}
    by_backend = {}
    ocr_runs = 0
    failed = []
    seen_targets = {}
    t0 = time.time()
    for i, p in enumerate(pdfs, 1):
        out = target_for(p, args.root, args.out, args.mirror)
        if not args.force and os.path.exists(out):
            skipped += 1
            continue
        # 平铺 --out 时不同目录的同名 PDF 会撞同一个产物：先写的赢，后者宁可失败也不覆盖
        out_abs = os.path.abspath(out)
        owner = seen_targets.get(out_abs)
        if owner and os.path.abspath(owner) != os.path.abspath(p):
            msg = "产物重名，跳过：%s（已被 %s 占用）" % (out, os.path.basename(owner))
            print("  警告：" + msg, file=sys.stderr)
            failed.append((os.path.basename(p), "产物重名"))
            log_row(args.log, dict(pdf=p, ok=False, err=msg))
            continue
        seen_targets[out_abs] = p
        started = time.time()
        try:
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            report = convert.convert_file(
                p, backend=args.backend, extractor=args.extractor,
                ocr_fallback=args.ocr_fallback, page_marks=args.page_marks,
                images_root=os.path.dirname(os.path.abspath(out)))
        except (convert.PdfError, OSError) as exc:
            failed.append((os.path.basename(p), str(exc)[:80]))
            log_row(args.log, dict(pdf=p, ok=False, err=str(exc)[:200]))
            continue

        gate = report["gate"]
        by_gate[gate["state"]] += 1
        by_backend[report["backend"]] = by_backend.get(report["backend"], 0) + 1
        if report["ocr_txt"] and not report["resumed"]:
            ocr_runs += 1
        if args.strict and gate["state"] != "pass":
            failed.append((os.path.basename(p),
                           "门禁%s：%s" % (GATE_LABEL[gate["state"]], gate["reason"])))
            log_row(args.log, dict(pdf=p, ok=False, gate=gate, backend=report["backend"]))
            continue

        md = report["md"]
        try:
            fsutil.atomic_write_text(out, md)
        except OSError as exc:
            msg = "写入失败：%s" % exc
            failed.append((os.path.basename(p), msg[:80]))
            log_row(args.log, dict(pdf=p, md=out, ok=False, err=msg[:200],
                                  backend=report["backend"], gate=gate))
            continue
        if gate["chars"] < convert.quality.MIN_CHARS:
            short += 1
        else:
            ok += 1
        log_row(args.log, dict(pdf=p, md=out, ok=True, backend=report["backend"],
                               shape=report["shape"], pages=report["pages"],
                               chars=len(md), imgs_kept=report["imgs_kept"],
                               ocr_txt=report["ocr_txt"], gate=gate,
                               sec=round(time.time() - started, 2)))
        if i % 20 == 0:
            print("  进度 %d/%d（%.0fs）" % (i, len(pdfs), time.time() - t0))

    print("=" * 50)
    print("完成 %d | 跳过(已有) %d | 空/过短 %d | 失败 %d" % (ok, skipped, short, len(failed)))
    print("门禁 pass %d / review %d / fail %d" % (
        by_gate["pass"], by_gate["review"], by_gate["fail"]))
    if by_backend:
        print("后端 " + "，".join("%s %d" % (k, by_backend[k]) for k in sorted(by_backend)))
    if ocr_runs:
        print("本次新跑页级 OCR %d 件" % ocr_runs)
    for n, e in failed:
        print("  失败 %s: %s" % (n, e))
    print("耗时 %.0f 秒" % (time.time() - t0))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
