# -*- coding: utf-8 -*-
"""文本质量门禁：三态 pass / review / fail（先 NFKC 归一）。

迁自上游知识库管线的 `gate.py` 与 `kb/textmetrics.py`，把阈值、乱码判据、
指标口径原样搬过来，去掉台账/数据库/报告那一层（这里只判定一段 md 文本）。

判据（先 NFKC 归一，去掉空白 / Markdown 语法 / 分页标记后算指标）：
    可见字符数 < 100    -> fail    极短产物
    异常字符比 >= 2%    -> fail    真乱码（ړ ୗ ᭗ 这类非中文码位）
    CJK 比例 < 15%      -> review  低中文占比，交给 OCR / 人工裁定
    其余                -> pass

为什么必须 NFKC：PDF 提取常把汉字映射进 Kangxi 部首区（U+2F00），「大厂」会变成
「⼤厂」。内容可读，但检索、去重、聚合全部失效。实测健康件异常率从 4% 降到 0.2%。

判定语义边界：`pass` 只表示「没有可识别的非中文垃圾字符 + CJK/长度达标」，
**不等于文本可读或语义正确**。ToUnicode 映射错误把乱码映射成正常汉字时，
字符白名单识别不出来，只能靠人工抽检或重复字符占比等额外手段。
"""
from __future__ import annotations

import re
import unicodedata

# 阈值：与上游管线的 config.TEXT_MIN_CHARS / GATE_CJK_MIN、gate.TH_BAD 同值
MIN_CHARS = 100
CJK_MIN = 0.15
BAD_MAX = 0.02

CJK_RANGES = (
    (0x3400, 0x4DBF),    # 扩展 A
    (0x4E00, 0x9FFF),    # 基本区
    (0xF900, 0xFAFF),    # 兼容汉字
    (0x20000, 0x2FA1F),  # 扩展 B~F
)

# 本语料里出现过的「乱码特征」：不该出现在中文文档里的字母/组合符号
_GARBLED_SCRIPT_PREFIXES = (
    "ARABIC", "DEVANAGARI", "BENGALI", "GURMUKHI", "GUJARATI", "ORIYA",
    "TAMIL", "TELUGU", "KANNADA", "MALAYALAM", "SINHALA", "THAI", "LAO",
    "KHMER", "MYANMAR", "TIBETAN", "ETHIOPIC", "ARMENIAN", "GEORGIAN",
    "HEBREW", "SYRIAC", "THAANA", "MONGOLIAN", "MODIFIER LETTER",
)

COMMON_PUNCT = set(
    "，。、；：？！“”‘’（）【】《》〈〉—…·～＋－×÷＝％＃＠＆＊"
    ",.;:?!()[]{}<>\"'`~-_/\\|*#%&+=@$^"
)

_WS_RE = re.compile(r"[\s\u200b-\u200d\ufeff]+")
PAGE_MARK_RE = re.compile(r"^[ \t]*=====[ \t]*page[ \t]+\d+[ \t]*=====[ \t]*$", re.I | re.M)
CODE_FENCE_RE = re.compile(r"```.*?```", re.S)


def is_cjk(ch):
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in CJK_RANGES)


def is_garbled(ch):
    """乱码字符：替换符、控制符、组合附加符、非中文语系的字母。"""
    cp = ord(ch)
    if cp == 0xFFFD:
        return True
    if 0x00 <= cp <= 0x08 or 0x0B <= cp <= 0x1F or 0x7F <= cp <= 0x9F:
        return True
    if unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return True
    if ch.isascii():
        return False
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return True                     # 私用区 / 未定义
    return name.startswith(_GARBLED_SCRIPT_PREFIXES)


def is_bad_char(ch):
    """异常字符：明确不该出现在中文文档里的码位（gate._is_bad_char 原样搬入）。

    不把 emoji / 希腊字母 / 假名 / CJK 扩展汉字判坏；U+25A0–25FF 方框单独判坏，
    这是 PDF 字体映射失败的典型特征。
    """
    o = ord(ch)
    if ch.isspace() or o in (0x200B, 0x200C, 0x200D, 0xFE0F, 0xFEFF):
        return False
    if o < 0x80:
        return False
    if is_cjk(ch):
        return False
    if 0x25A0 <= o <= 0x25FF:
        return True
    if 0xE000 <= o <= 0xF8FF or o == 0xFFFD:
        return True
    if 0xFE00 <= o <= 0xFE0F:
        return False
    if (0x00A0 <= o <= 0x024F or 0x0370 <= o <= 0x03FF or 0x0400 <= o <= 0x04FF
            or 0x2000 <= o <= 0x206F or 0x2070 <= o <= 0x209F
            or 0x20A0 <= o <= 0x20CF or 0x2100 <= o <= 0x214F
            or 0x2150 <= o <= 0x218F or 0x2190 <= o <= 0x21FF
            or 0x2200 <= o <= 0x22FF or 0x2460 <= o <= 0x24FF
            or 0x3000 <= o <= 0x303F or 0x3040 <= o <= 0x30FF
            or 0x31F0 <= o <= 0x31FF or 0xAC00 <= o <= 0xD7AF
            or 0xFF00 <= o <= 0xFFEF or 0x1F000 <= o <= 0x1FAFF
            or 0x2600 <= o <= 0x27BF or 0x2B00 <= o <= 0x2BFF):
        return False
    return True


def visible_chars(text):
    return len(_WS_RE.sub("", text))


def cjk_count(text):
    return sum(1 for ch in text if is_cjk(ch))


def cjk_ratio(text):
    vis = _WS_RE.sub("", text)
    return cjk_count(vis) / len(vis) if vis else 0.0


def garbled_ratio(text):
    vis = _WS_RE.sub("", text)
    return sum(1 for ch in vis if is_garbled(ch)) / len(vis) if vis else 0.0


def valid_ratio(text):
    """有效字符占比：CJK、ASCII 可见字符、常见标点算有效。"""
    vis = _WS_RE.sub("", text)
    if not vis:
        return 0.0
    ok = 0
    for ch in vis:
        if is_cjk(ch) or ch.isascii() or ch in COMMON_PUNCT:
            ok += 1
        elif unicodedata.category(ch) in ("Nd", "Nl", "Po", "Ps", "Pe", "Pd", "Pi", "Pf"):
            ok += 1
    return ok / len(vis)


def strip_markdown_noise(text):
    """去掉图片/链接目标/代码围栏标记/标题符号，只留可读正文，避免指标被语法稀释。"""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^```.*$", "", text, flags=re.M)
    text = re.sub(r"^\s*[#>|-]{1,}\s*", "", text, flags=re.M)
    return text


def clean_for_metrics(raw):
    """分页标记 → 代码围栏 → Markdown 语法，按序清洗后交给指标计算。"""
    return strip_markdown_noise(CODE_FENCE_RE.sub("", PAGE_MARK_RE.sub("", raw)))


def metrics(raw):
    """NFKC 归一后算三态指标。

    chars 为**可见字符数**（去掉空白、Markdown 语法、代码围栏、分页标记）；
    cjk / latin / bad 均以可见字符数为分母；fixed 为 NFKC 实际改写字符数。
    """
    norm = unicodedata.normalize("NFKC", raw)
    text = unicodedata.normalize("NFKC", clean_for_metrics(raw))
    vis = visible_chars(text)
    fixed = sum(1 for a, b in zip(raw, norm) if a != b) + abs(len(raw) - len(norm))
    if vis == 0:
        return dict(chars=0, cjk=0.0, latin=0.0, bad=1.0, fixed=fixed)
    clean = _WS_RE.sub("", text)
    n = len(clean)
    cjk = sum(1 for c in clean if is_cjk(c))
    latin = sum(1 for c in clean if ord(c) < 0x80 and c.isalpha())
    bad = sum(1 for c in clean if is_bad_char(c))
    return dict(chars=n, cjk=cjk / n, latin=latin / n, bad=bad / n, fixed=fixed)


def judge(m):
    """三态判定：fail（真乱码/过短）→ review（低 CJK 灰区）→ pass。"""
    if m["chars"] < MIN_CHARS:
        return "fail", "产物过短(%d字符)" % m["chars"]
    if m["bad"] >= BAD_MAX:
        return "fail", "乱码(异常字符%.1f%%)" % (m["bad"] * 100)
    if m["cjk"] < CJK_MIN:
        return "review", "低CJK(%.1f%%) 需裁定" % (m["cjk"] * 100)
    return "pass", ""


def assess(md):
    """一段 md → 三态结论。返回 {state, reason, chars, cjk, latin, bad, fixed}。"""
    m = metrics(md)
    state, reason = judge(m)
    return dict(state=state, reason=reason, chars=m["chars"],
                cjk=round(m["cjk"], 4), latin=round(m["latin"], 4),
                bad=round(m["bad"], 4), fixed=m["fixed"])


def has_page_marks(md):
    return bool(PAGE_MARK_RE.search(md))


def thresholds():
    """阈值快照，报告里带上，避免数字漂移后无法解释历史结论。"""
    return dict(min_chars=MIN_CHARS, cjk_min=CJK_MIN, bad_max=BAD_MAX, nfkc=True)
