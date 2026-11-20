# -*- coding: utf-8 -*-
"""文件落盘与指纹：原子替换、sha256、安全文件名、宽松读文本。

迁自上游知识库管线：
  - `kb/atomic.py`   —— 同目录临时文件 + fsync + os.replace；
  - `kb/hashing.py`  —— sha256_file / doc_hash（sha256 前 16 位）；
  - `kb/convert_one.py` —— safe_name（文件名收敛）；
  - `reconvert.py`   —— _read_md（utf-8-sig → gb18030，替换符过多即失败）。

为什么值得迁：正文明写会在断电/崩溃时留下半截 md，而下游（切题/检索/门禁）只看
「文件在不在」，半截文件会被当成完成。原子替换 + 指纹 sidecar 是这一层的地基。
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile

CHUNK = 1 << 20
ILLEGAL = r'[\\/:*?"<>|\r\n\t]'
_WIN_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)


def atomic_write_bytes(path, data):
    """写文件：临时文件与目标同目录，fsync 后 os.replace 原子替换。"""
    target = str(path)
    parent = os.path.dirname(os.path.abspath(target))
    os.makedirs(parent, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="." + os.path.basename(target) + ".",
                                    suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
        # POSIX 同步目录项；Windows 不支持目录 fsync，忽略平台差异
        try:
            dir_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except (OSError, AttributeError):
            pass
        return target
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def atomic_write_text(path, text, encoding="utf-8"):
    return atomic_write_bytes(path, text.encode(encoding))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def doc_hash(path):
    """主键用的短指纹：sha256 前 16 位（10 万量级文件下碰撞概率可忽略）。"""
    return sha256_file(path)[:16]


def short_hash(data, n=12):
    """字节内容短指纹，用于图片去重与产物命名。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.md5(data).hexdigest()[:n]


def safe_name(name, limit=80):
    """把不可信目录/文件名收敛为单个普通路径段（迁自 convert_one.safe_name）。"""
    value = re.sub(ILLEGAL, "_", str(name)).strip(" .")[:limit]
    if value in ("", ".", ".."):
        return ""
    if _WIN_RESERVED.match(value):
        value = "_" + value
    return value


def read_text_fallback(path, per_1000=1):
    """读 md/txt：先 utf-8-sig，再 gb18030；替换符过多直接失败，不静默污染文本。

    `per_1000` 是允许的替换符密度（每 1000 字符），下限固定 3 个字符。
    """
    for enc in ("utf-8-sig", "gb18030"):
        try:
            with open(path, encoding=enc) as fh:
                text = fh.read()
        except UnicodeDecodeError:
            continue
        bad = text.count("\ufffd")
        if bad > max(3, len(text) * per_1000 // 1000):
            raise ValueError("%s: %s 解码后含 %d 个替换符" % (os.path.basename(path), enc, bad))
        return text
    raise ValueError("%s: 既不是 UTF-8 也不是 GB18030" % os.path.basename(path))
