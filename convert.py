#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""pdf2md：把 PDF 转成标准 Markdown（截图 OCR + 文字层兜底 + 结构化抽取）。

内置路径（零第三方依赖，stdlib-only）：
  1. 截图型 PDF（整页一张长图、无文字层）→ stdlib 解 FlateDecode 图 → 手写 PNG
     → 切片 → Windows PowerShell 5.1 + WinRT OCR → 清洗合段。
  2. 文字层 PDF → 调外部 pdftotext（poppler）；探不到就报错，不静默产出垃圾。

合并进来的路径（依赖可选，缺了就明确报错）：
  3. structured：pymupdf4llm 结构化 md（标题/表格/列表）+ 内嵌配图抽取，pypdf 兜底；
  4. paddle：PaddleOCR-json 页级 OCR（多页扫描件），全文 sidecar 可续跑；
  5. 质量门禁：三态 pass/review/fail，`--strict` 时不通过就不写盘。

用法：
    py convert.py <path>              dry-run，MD 打到 stdout，不写盘
    py convert.py --check <path>      只读预检（形态/后端/依赖/门禁/残留）
    py convert.py --apply <path>      写 <同名>.md，已存在先备份 .md.bak
    py convert.py --apply --backend structured <path>
"""

import argparse
import os
import re
import shutil
import statistics
import struct
import subprocess
import sys
import tempfile
import zlib

import fsutil
import quality

__version__ = "0.1.1"

# ---------------------------------------------------------------- 常量

# CJK 表意文字 + 中日韩标点 + 全角符号
CJK = r"[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef]"
# 与 CJK 相邻时需要折叠空格的 ASCII 符号（字母单独处理，见 collapse_cjk_space）
SYM = r"[=+\-*/<>%#&|~^_\[\](){}.,:;'\"!?]"

# 伪文字层 PDF 兜底 OCR 时，pdftoppm 渲染的 DPI。
# 120 dpi 对 A4 约 1000×1400px，WinRT OCR 识别中文够用，且单页不用再切片。
RENDER_DPI = 120

# 把提示词整片抠掉后只剩标点/括号/空白 → 判为噪声行整行丢弃
NOISE_LINE_HINTS = (
    "关注", "点赞", "分享", "收藏", "举报", "阅读原文", "长按识别",
    "二维码", "扫码", "微信", "公众号", "原文链接", "展开全文",
)

TERM_PUNCT = "。！？…”」』；!?：."
SHORT_LINE_RATIO = 0.62

# ---------------------------------------------------------------- 噪声规则
#
# 下面这些不是猜的，是从 11 份真实 PDF 的产物（`_pdfout/`）里逐条数出来的：
# 849 行正文里有 151 行是噪声（18%），集中在知乎推荐区、QQ 浏览器热搜榜、
# 页脚页码、App 推广文案四类。规则按「宁可漏也不误杀」的顺序排列。

# 知乎推荐区：「6．7K 关注 · 387回答…」。OCR 会把 3.1K 认成 3． IK、
# 1.1K 认成 I.IK、36.0K 认成 36． OK，所以数字位必须容忍 I/l/|/全角点。
# 关键约束：必须「关注」「回答」同时出现 —— 全书 6 行只含「关注」的都是正文
# （交叉关注点 / 这些关注点 / 新材料制备关…），只靠「关注」会误杀。
RECOMMEND_RE = re.compile(
    r"[0-9０-９．.I l|]{1,8}\s*[Kk]?\s*关注\s*[·。，,.]?\s*[0-9０-９．.I lKk]{1,8}\s*回答"
)

# QQ 浏览器热搜榜尾巴：「545．5w 人在」
HOT_RE = re.compile(r"\d+[．.]\d+\s*w\s*人在")

# 整行就是 UI 文案（数字串由 drop_digit_runs 单独处理）。
# 后四项是 OCR 把「打开 App」「继续浏览内容」「知乎」切碎后的 1–2 字残留，
# 整行只有这几个字时必然是残渣，不可能是正文段落。
UI_LINE_EXACT = frozenset((
    "知乎", "发现更大的世界", "QQ 浏览器", "QQ 浏览器热搜榜", "App 内打开",
    "继续浏览内容", "热门推荐", "相关推荐", "立即下载", "展开全文",
    "查看全部榜单>", "查看全部榜单〉", "收起^", "收起",
    "打开知乎 App ，查看更多精彩讨论",
    "0", "打开", "继续", "知《",
))

# 信息流广告卡的「广告」角标永远钉在行尾（`微博的广告`、`0还呗借款的广告`），
# 正文提到广告时不会这么收尾（`社交媒体上的广告投放越来越精准。`）。
AD_BADGE_TAIL = "广告"

# 扫码引流话术。单独出现「二维码」未必是广告（可能是正文讲解），
# 必须与引流动词同现才判。
QR_PROMO_RE = re.compile(r"二维码.*(免费领取|小助手|扫码|添加)|(免费领取|小助手|扫码).*二维码")

# 信息流广告卡（知乎回答末尾的推荐位）。三类线索各自独立：
#   1) 贷款/理财广告的额度话术：`最高额度20万`
#   2) 卡片右下角的按钮：`查看详情`（OCR 也认成 `查看译情`）
#   3) 卡片署名：`商品运营分享的文章 · 7赞同…`
# 都要求整行就这些字，正文行中段出现不算。
FEED_AD_LINE_EXACT = frozenset(("最高额度20万", "查看详情", "查看译情"))
FEED_AD_CARD_RE = re.compile(r"^\S{2,12}分享的文章\s*[·・]|^\S{2,12}的文章\s*[·・].*赞同")

# 行内 UI 文案：整行丢弃救不了，这些嵌在正文段落中间，必须按子串剥掉。
INLINE_NOISE_PATTERNS = (
    re.compile(r"打开\s*知乎\s*App\s*[，,]?\s*查看更多精彩讨论"),
    re.compile(r"下载\s*App\s*注册登录"),
    re.compile(r"打开\s*App\s*查看更多评论"),
    re.compile(r"查看更多评论"),
    re.compile(r"查看更多精彩讨论"),
    re.compile(r"查看全部\s*\d*\s*个回答"),
    re.compile(r"查看全部榜单[>〉]?"),
    re.compile(r"点个在看少个\s*bug", re.I),
    re.compile(r"App\s*内打开"),
    re.compile(r"继续浏览内容"),
    re.compile(r"注册登录"),
    # 「回复知乎用户」被 OCR 认成「囗复知乎用户」，首字丢笔画，故首字放宽到回/囗/口。
    re.compile(r"[回囗口]\s*复\s*知乎用户"),
)

# 「1 / 21」这类页码。不能见着就当页码删：`20 / 8` 是网速换算算式，
# 判据是「同一分母重复出现 ≥ PAGE_REPEAT 次」，见 drop_page_numbers。
PAGE_RE = re.compile(r"^\s*\d{1,4}\s*/\s*\d{1,4}\s*$")
PAGE_REPEAT = 3

# 纯数字行（热搜榜序号 1 2 3 4 5）。孤立数字是表格首列，只有连续
# ≥ DIGIT_RUN_MIN 个才判噪声。
DIGIT_RE = re.compile(r"^\s*\d{1,3}\s*$")
DIGIT_RUN_MIN = 3

# 信息流整块噪声：行级规则删不掉「末尾连着 10 段知乎问题标题」这种结构，
# 得在合段之后按段落切，见 truncate_feed_tail。
# 锚点词之后的整块内容都是推荐位/往期目录（`0今日推荐阅读`、`程序汪往期精彩文章`）。
FEED_TAIL_ANCHORS = (
    "今日推荐阅读", "往期精彩文章", "QQ 浏览器热搜榜", "限时免费领", "免费领取", "扫码领取",
)
# 到 40 字为止都算标题形状；再长就是正文段落了（11 份语料里正文段落中位数 80+）。
FEED_TAIL_MAX_LEN = 40
# 末尾连跑 ≥3 段才截断；2 段可能是正文自身的连续设问（`为什么会这样？`）。
FEED_TAIL_MIN_RUN = 3

# 残留标记：成品 MD 里出现这些就说明过滤漏了
RESIDUAL_MARKERS = ("<script", "<style", "<div", "<span", "<p ", "data-v-", "&nbsp;")

# 抽取后端（合并上游知识库管线的 PDF→MD 能力后新增）
BACKENDS = ("auto", "builtin", "structured", "paddle")
BACKEND_LABEL = {
    "builtin": "内置（WinRT OCR / pdftotext）",
    "structured": "结构化（pymupdf4llm / pypdf）",
    "paddle": "页级 OCR（PaddleOCR-json）",
}
GATE_RANK = {"pass": 2, "review": 1, "fail": 0}
GATE_LABEL = {"pass": "通过", "review": "待裁定", "fail": "不通过"}
STRUCTURED_NOTE = ("正文由 PDF 结构化抽取得到，保留标题/表格/列表；配图另存 "
                   "<同名>.images/，不做广告/信息流噪声清洗。")
PADDLE_NOTE = ("正文由页级 OCR 得到，按页顺序拼接；OCR 全文与来源指纹见 "
               "<同名>.pdf.ocr.txt 及其 .meta.json。")

SLICE_STEP = 1800
SLICE_OVERLAP = 60

PDFTOTEXT_CANDIDATES = (
    r"C:\Program Files\Calibre2\app\bin\pdftotext.exe",
    r"C:\Program Files\poppler\Library\bin\pdftotext.exe",
    r"C:\poppler\bin\pdftotext.exe",
)

OCR_PS1 = r"""
param([string]$Dir, [string]$OutFile)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$g = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) {
    $t = $g.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result
}
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType=WindowsRuntime]
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { throw "no OCR engine (need a Windows OCR language pack)" }
$sb = New-Object System.Text.StringBuilder
foreach ($f in (Get-ChildItem $Dir -Filter 'slice_*.png' | Sort-Object Name)) {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($f.FullName)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    $dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bmp = Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $res = Await ($engine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
    foreach ($line in $res.Lines) { [void]$sb.AppendLine($line.Text) }
    $bmp.Dispose(); $stream.Dispose()
}
[System.IO.File]::WriteAllText($OutFile, $sb.ToString(), [System.Text.UTF8Encoding]::new($false))
"""


class PdfError(Exception):
    """无法处理的 PDF：形态未知、解图失败、缺 pdftotext、OCR 失败。"""


# ---------------------------------------------------------------- 形态判定


def detect_pdf(raw):
    """是不是 PDF：只看文件头，不信扩展名。"""
    return raw[:5] == b"%PDF-"


def kind_of(raw):
    """'image'（整页截图）/ 'text'（有文字层）/ 'unknown'。"""
    if re.search(rb"/Subtype\s*/Image", raw) and not re.search(rb"/Type\s*/Font", raw):
        return "image"
    if re.search(rb"/Type\s*/Font", raw):
        return "text"
    return "unknown"


# ---------------------------------------------------------------- 解图与 PNG


def _inflate(blob):
    """解 FlateDecode，容忍流尾多余的换行/填充。"""
    d = zlib.decompressobj()
    out = d.decompress(blob)
    out += d.flush()
    return out


def _image_candidates(raw):
    """扫出所有图片对象，产出 (w, h, rgb)。解不出的一律跳过。

    不能只看第一个：页面里常有 logo/图标排在页图前面，取错了就是空正文。
    """
    for m in re.finditer(rb"/Subtype\s*/Image(.{0,2000}?)stream\r?\n", raw, re.S):
        head = m.group(1)
        w = re.search(rb"/Width\s+(\d+)\b", head)
        h = re.search(rb"/Height\s+(\d+)\b", head)
        if not (w and h):
            continue  # 间接引用（/Width 5 0 R）解不了，跳过
        w, h = int(w.group(1)), int(h.group(1))
        # 必须显式声明 DeviceRGB：灰度/CMYK 会被按 3 字节解出错位像素
        if not re.search(rb"/ColorSpace\s*/DeviceRGB\b", head):
            continue
        start = m.end()
        end = raw.find(b"endstream", start)
        if end < 0:
            continue
        try:
            pix = _inflate(raw[start:end])
        except zlib.error:
            continue
        if len(pix) != w * h * 3:
            continue
        yield w, h, pix


def extract_image(raw):
    """取出页面那张长截图：返回 (w, h, rgb_bytes)，取不到返回 None。

    只认 DeviceRGB + 8bit：实测语料全是这个组合，别的组合宁可拒绝也不猜。
    多张图时取面积最大的（页图），避免拿到 logo。
    """
    best = None
    for cand in _image_candidates(raw):
        if best is None or cand[0] * cand[1] > best[0] * best[1]:
            best = cand
    return best


def png_bytes(w, h, rgb):
    """把裸 RGB 像素封成 PNG：每行一个 0x00 filter 字节。"""
    if w <= 0 or h <= 0:
        raise ValueError("PNG 尺寸必须为正数：%r x %r" % (w, h))
    if len(rgb) != w * h * 3:
        raise ValueError("像素字节数应为 %d，实际 %d" % (w * h * 3, len(rgb)))

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + rgb[y * w * 3:(y + 1) * w * 3] for y in range(h))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows, 6))
        + chunk(b"IEND", b"")
    )


def slice_plan(h, step=SLICE_STEP, overlap=SLICE_OVERLAP):
    """切片计划 [(y0, y1), ...]：相邻片留 overlap 像素重叠，末片覆盖到 h。

    WinRT OCR 有 MaxImageDimension = 10000，整张 20000px 长图会直接报
    「Image dimensions are too large!」，必须切片。
    """
    if step <= 0 or overlap < 0 or overlap >= step:
        raise ValueError("需要 step > overlap >= 0，实际 step=%r overlap=%r" % (step, overlap))
    if h <= 0:
        return []
    if h <= step:
        return [(0, h)]
    out, y = [], 0
    while True:
        y2 = min(y + step, h)
        out.append((y, y2))
        if y2 >= h:
            break
        y = y2 - overlap
    return out


# ---------------------------------------------------------------- 文本清洗


def collapse_cjk_space(s):
    """去掉 OCR 在汉字之间插入的空格，但保留英文单词间的空格。

    Windows OCR 中文模型会把每个 token 用空格隔开（"只 加 两 行"），
    一刀切去掉所有空格会毁掉英文（"hello world"），所以只折叠 CJK 邻接处。
    """
    prev = None
    while prev != s:
        prev = s
        s = re.sub("(" + CJK + r") +(" + CJK + ")", r"\1\2", s)
        s = re.sub("(" + CJK + r") +([0-9])", r"\1\2", s)
        s = re.sub(r"([0-9]) +(" + CJK + ")", r"\1\2", s)
        s = re.sub("(" + CJK + r") +(" + SYM + ")", r"\1\2", s)
        s = re.sub("(" + SYM + r") +(" + CJK + ")", r"\1\2", s)
    return s


def is_noise_line(line):
    """整行判噪声：剥掉提示词与标点后什么都不剩，才算噪声。

    不做子串删除 —— 正文里正常出现「他分享了自己十年的工作经验」不能丢；
    也不能只看「短且含提示词」，否则「分享经济」「点赞之交」会被误杀。
    """
    s = line.strip()
    if not s:
        return True
    if s in UI_LINE_EXACT:
        return True
    # ponytail: 行尾「广告」是信息流广告卡的角标，判据简单粗暴 —— 正文提到
    # 广告时不会正好以它收尾（11 份语料里 9 行命中全是广告卡，0 误杀）。
    # 天花板：正文若真以「广告」二字结尾会被误删，届时改判「短行 + 行尾广告」。
    if s.endswith(AD_BADGE_TAIL) and len(s) <= 60:
        return True
    if QR_PROMO_RE.search(s):
        return True
    if s in FEED_AD_LINE_EXACT or FEED_AD_CARD_RE.search(s):
        return True
    if RECOMMEND_RE.search(s) or HOT_RE.search(s):
        return True
    for hint in NOISE_LINE_HINTS:
        s = s.replace(hint, "")
    return not s.strip("".join(TERM_PUNCT) + "，、（）()[]【】<>《》 \t\u3000")


def strip_inline_noise(line):
    """把嵌在正文段落中间的 App 推广文案剥掉。

    整行丢弃救不了这类：`乎 Q*•• 下载 App 注册登录如果一家公司…` 前后都有正文，
    只能按子串删。删完顺手把两端多余空白去掉，全空就返回空串。
    """
    out = line
    for pat in INLINE_NOISE_PATTERNS:
        out = pat.sub("", out)
    return out.strip()


def drop_page_numbers(lines):
    """丢掉页脚页码，但要求同一分母重复出现足够多次。

    `11_中国制造2025` 抽出 14 行 `n / 21`，分母全是 21，整类可删；
    `02_99的人都不知道内网…` 有一行 `20 / 8`，但周边是 `1、25`、`2、50`、
    `100 / 8 =` —— 那是网速换算算式，分母 8 只出现一两次，必须留下。
    """
    denoms = {}
    for ln in lines:
        if PAGE_RE.match(ln):
            denom = ln.split("/")[1].strip()
            denoms[denom] = denoms.get(denom, 0) + 1
    bad = {d for d, n in denoms.items() if n >= PAGE_REPEAT}
    if not bad:
        return list(lines)
    return [ln for ln in lines if not (PAGE_RE.match(ln) and ln.split("/")[1].strip() in bad)]


def drop_digit_runs(lines):
    """丢掉连续 ≥3 个的纯数字行（热搜榜序号），孤立数字保留。

    `03`/`04`/`07` 号产物里 `1 2 3 4 5` 紧跟「QQ 浏览器热搜榜」出现；
    但 `11_中国制造2025` 表格首列的孤立 `2`/`3`/`5` 是数据，不能动。
    """
    out, run = [], []
    for ln in lines:
        if DIGIT_RE.match(ln):
            run.append(ln)
            continue
        if len(run) >= DIGIT_RUN_MIN:
            run = []
        else:
            out.extend(run)
            run = []
        out.append(ln)
    if len(run) < DIGIT_RUN_MIN:
        out.extend(run)
    return out


def _feed_like(paragraph):
    """一个段落像不像信息流卡片（知乎问题标题 / 推荐区计数行）。

    行级规则管不到它们：`男生的哪个瞬间让你感觉特别绅士？` 单看是一句正常中文，
    只有「末尾连着好几段」这个结构信息才说明它是推荐位。
    """
    if len(paragraph) > FEED_TAIL_MAX_LEN:
        return False
    s = paragraph.rstrip()
    if s.endswith("？") or s.endswith("?"):
        return True
    return "关注" in paragraph and "回答" in paragraph


def truncate_feed_tail(paragraphs):
    """砍掉正文末尾整块的信息流噪声（知乎推荐区 / QQ 热搜尾巴 / 往期文章目录）。

    两把刀，对 11 份真实产物逐份验证过：
      1) 锚点截断：段里出现 `0今日推荐阅读`、`程序汪往期精彩文章`、`QQ 浏览器热搜榜`
         这类标题词，且位置在全文后半段 —— 从该段起全部是推荐位/目录，整块丢。
         只认后半段，因为前半段出现同名词汇时往往正是在讲这件事本身。
      2) 尾部连跑截断：末尾连续 ≥3 段都是信息流标题形状（短、问号收尾或「关注·回答」
         计数行）才算，2 段不动 —— 正文里的连续设问也就这么长。
    """
    paragraphs = list(paragraphs)
    n = len(paragraphs)
    for i in range(n // 2, n):
        if any(a in paragraphs[i] for a in FEED_TAIL_ANCHORS):
            paragraphs = paragraphs[:i]
            break
    run = 0
    for p in reversed(paragraphs):
        if not _feed_like(p):
            break
        run += 1
    if run >= FEED_TAIL_MIN_RUN:
        return paragraphs[:len(paragraphs) - run]
    return paragraphs


# ---------------------------------------------------------------- 题号标题

# 题号行形态：行首数字 + 分隔符（. 、 ,）+ 题目文字。
# 分隔符后用负向预查排除「紧跟数字」——那是多级编号（1.1 / 6.14）或小数（3.14）；
# 再要求分隔符后有非空白内容，挡掉「1.」「2、」这类空题号残片。
# 「1. 什么是XX」「1. Redis」合法，「1.1 什么是XX」「1.」「1.  」不合法。
QUESTION_HEAD_RE = re.compile(r"^\d{1,3}\s*[.、,](?!\d)\s*\S")

# 题号识别的约束（都是被真实语料逼出来的，见 PITFALLS.md 坑 18）：
QUESTION_MAX_NUM = 200      # 题号超过 200 视为非题号（页码/年份/统计值）
QUESTION_MAX_LEN = 100      # 题目文字超过 100 字更像答案而非题目
QUESTION_GAP_MAX = 2        # 递增链允许跳 1~2 个号（题库常见删题/跳题）


def _question_num(paragraph):
    """若段落像题号开头，返回 (题号 int, 剩余文字)，否则 None。

    判据是「整段以题号开头」，不是「段落里含数字」——后者会把正文里
    提到的「第 1 章」「见 3.5 节」全误判（坑 15：单关键词是必要不充分条件）。
    """
    s = paragraph.strip()
    if not s or len(s) > QUESTION_MAX_LEN:
        return None
    m = QUESTION_HEAD_RE.match(s)
    if not m:
        return None
    num = int(re.match(r"\d{1,3}", s).group())
    if num > QUESTION_MAX_NUM:
        return None
    return num, s[m.end():].strip()


def mark_question_headings(paragraphs):
    """把面试题的题号段落升级为 Markdown 标题（`## 1. 题目`）。

    一段被标为题号，需**同时**满足两条（每条都被真实语料逼出来，见坑 18）：
      1) 形似题号：`_question_num` 通过 —— 行首数字+分隔符+非数字内容，
         题号 ≤ 上限、题目不过长。挡掉小数(3.14)、多级编号(1.1)、超大号(500)、空题号(1.)。
      2) 编号自洽：能连进一条递增链（相邻题号递增、跳号 ≤ GAP），
         或者它是全文**唯一**的题号（单题文档合法）。
         这条挡掉「1 ... 5」跳太远，以及答案内部的重排编号。

    刻意**不**用「题号后是否紧跟答案」做条件：面经常见「面试官连着列好几个
    问题再统一作答」，要求后一行为答案会漏标整块连续题号（面经语料实测）。
    答案内部编号用的是全角括号（`1）`），本就不匹配 QUESTION_HEAD_RE，天然安全。
    """
    n = len(paragraphs)
    nums = []  # 每段的题号（形似才非 None）
    for p in paragraphs:
        got = _question_num(p)
        nums.append(got[0] if got else None)

    # 条件 3 的「递增链」：从每个形似题号出发向后连，相邻题号递增且跳号 ≤ GAP。
    # in_chain[i] = 段 i 是否连进了长度 ≥2 的链。
    in_chain = [False] * n
    for i in range(n):
        if nums[i] is None:
            continue
        last, chain = nums[i], [i]
        for j in range(i + 1, n):
            if nums[j] is None:
                continue
            if last < nums[j] <= last + QUESTION_GAP_MAX:
                chain.append(j)
                last = nums[j]
            elif nums[j] > last + QUESTION_GAP_MAX:
                break
        if len(chain) >= 2:
            for k in chain:
                in_chain[k] = True

    total_q = sum(1 for x in nums if x is not None)

    out = []
    for i, p in enumerate(paragraphs):
        if nums[i] is None:
            out.append(p)
            continue
        # 形似题号 + 编号自洽（递增链 或 全文唯一题号）即标记。
        # 注意没有「题号后是否紧跟答案」这条：面经里面试官常连着列好几个问题
        # 再统一作答，要求后一行为答案会漏标整块连续题号。
        if in_chain[i] or total_q == 1:
            out.append("## " + p.strip())
        else:
            out.append(p)
    return out


def reflow(lines, median=None):
    """把 OCR 的短行合成段落。

    OCR 读不到 BoundingRect（PS 5.1 下 WinRT 结构体投影成 object[]），
    所以只能靠文本特征：以终止标点收尾、或明显短于中位行宽，就断段。
    """
    clean = [ln.strip() for ln in lines if ln and ln.strip()]
    if not clean:
        return []
    if median is None:
        median = statistics.median(len(ln) for ln in clean)
    short = median * SHORT_LINE_RATIO
    paras, buf = [], ""
    for ln in clean:
        buf = _join_line(buf, ln)
        if ln[-1] in TERM_PUNCT or len(ln) <= short:
            paras.append(buf)
            buf = ""
    if buf:
        paras.append(buf)
    return paras


def _needs_space(left, right):
    """拼行时这两半之间要不要补空格。

    中文不用空格分词，直接接上；英文/数字之间必须留空格，
    否则 "has no" + "terminating" 会粘成 "noterminating"。

    这里只列中日韩全角标点。英文的 ’ ” … 后面跟英文词时仍要空格，
    否则 "‘meant’" + "by" 会粘成 "meant’by"。
    """
    if not left or not right:
        return False
    a, b = left[-1], right[0]
    if re.match(CJK, a) or re.match(CJK, b):
        return False
    if a.isspace() or b.isspace():
        return False
    if a in "，。！？；：、）》」』" or b in "，。！？；：、）》」』":
        return False
    return True


def _join_line(buf, ln):
    return buf + (" " if _needs_space(buf, ln) else "") + ln


# ---------------------------------------------------------------- OCR


def ocr_available():
    """本机是否能跑 WinRT OCR：Windows + Windows PowerShell（不是 pwsh 7）。"""
    return os.name == "nt" and shutil.which("powershell") is not None


def ocr_pdf(raw):
    """截图 PDF → 行列表。调用 PowerShell 5.1 + WinRT OCR。"""
    got = extract_image(raw)
    if got is None:
        raise PdfError("解不出页面图（只支持 DeviceRGB/8bit/FlateDecode 的整页截图）")
    if not ocr_available():
        raise PdfError("本机找不到 Windows PowerShell，无法调 WinRT OCR")
    w, h, pix = got
    work = tempfile.mkdtemp(prefix="pdfo_")
    try:
        ps1 = os.path.join(work, "ocr.ps1")
        with open(ps1, "w", encoding="utf-8") as fh:
            fh.write(OCR_PS1)
        for i, (y0, y1) in enumerate(slice_plan(h)):
            png = png_bytes(w, y1 - y0, pix[y0 * w * 3:y1 * w * 3])
            with open(os.path.join(work, "slice_%02d.png" % i), "wb") as fh:
                fh.write(png)
        out = os.path.join(work, "out.txt")
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1,
                 "-Dir", work, "-OutFile", out],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
            )
        except subprocess.TimeoutExpired:
            raise PdfError("OCR 超时（>600 秒），文件可能过长")
        except OSError as exc:
            raise PdfError("启动 OCR 失败：%s" % exc)
        if proc.returncode != 0:
            raise PdfError("OCR 失败：" + (proc.stderr or "").strip()[:400])
        if not os.path.exists(out):
            raise PdfError("OCR 没有产出文本（引擎可能没有中文语言包）")
        with open(out, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # 切片间有重叠，重叠区会被识别两次，按「最近 30 行内出现过」去重
    lines, tail = [], []
    for raw_line in text.splitlines():
        line = collapse_cjk_space(raw_line).strip()
        if not line or line in tail:
            continue
        lines.append(line)
        tail = (tail + [line])[-30:]
    return lines


# ---------------------------------------------------------------- 文字层


def pdftotext_works(exe):
    r"""这个 pdftotext 能不能真跑起来。

    实测踩过：PATH 上排前面的一份 MiKTeX `pdftotext.EXE` 一跑就
    崩（log4cxx setFile 失败、exit -1073740791），不能只看「文件存在」。
    """
    if not exe:
        return False
    try:
        proc = subprocess.run(
            [exe, "-v"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and "pdftotext" in (proc.stderr + proc.stdout).lower()


def find_pdftotext():
    """找一个**真能用**的 pdftotext：环境变量 → PATH → 已知路径。找不到返回 None。"""
    cands = []
    env = os.environ.get("PDFTOTEXT")
    if env:
        cands.append(env)
    which = shutil.which("pdftotext")
    if which:
        cands.append(which)
    cands.extend(PDFTOTEXT_CANDIDATES)
    tried = set()
    for cand in cands:
        if not cand or cand in tried or not os.path.isfile(cand):
            continue
        tried.add(cand)
        if pdftotext_works(cand):
            return cand
    return None


def text_pdf(path):
    """文字层 PDF → 行列表（用外部 pdftotext）。"""
    exe = find_pdftotext()
    if not exe:
        raise PdfError(
            "该 PDF 有文字层，但本机找不到 pdftotext；"
            "请安装 poppler，或用环境变量 PDFTOTEXT 指定路径"
        )
    try:
        proc = subprocess.run(
            [exe, "-layout", "-enc", "UTF-8", path, "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
        )
    except subprocess.TimeoutExpired:
        raise PdfError("pdftotext 超时（>600 秒）")
    except OSError as exc:
        raise PdfError("启动 pdftotext 失败：%s" % exc)
    if proc.returncode != 0:
        raise PdfError("pdftotext 失败：" + (proc.stderr or "").strip()[:400])
    return [ln for ln in proc.stdout.splitlines()]


# ------------------------------------------------------- 伪文字层：渲染兜底 OCR

def find_pdftoppm():
    """找一个**真能用**的 pdftoppm（渲染 PDF 页为 PNG）。找不到返回 None。

    顺序：环境变量 PDFTOPPM → PATH → 已知路径。与 find_pdftotext 同策略。
    """
    cands = []
    env = os.environ.get("PDFTOPPM")
    if env:
        cands.append(env)
    which = shutil.which("pdftoppm")
    if which:
        cands.append(which)
    # 与 PDFTOTEXT 同目录（poppler/MiKTeX 通常一起装）
    exe = find_pdftotext()
    if exe:
        cands.append(os.path.join(os.path.dirname(exe), "pdftoppm.exe"))
        cands.append(os.path.join(os.path.dirname(exe), "pdftoppm"))
    tried = set()
    for cand in cands:
        if not cand or cand in tried or not os.path.isfile(cand):
            continue
        tried.add(cand)
        return cand  # pdftoppm 没有 --version 以外的探活，存在即用
    return None


def text_lines_or_empty(path):
    """文字层抽文本，返回 (行列表, 是否为空)。

    某些 PDF 含 /Type /Font 字典但每页实为整张图（字体来自水印/表单/校验层），
    pdftotext 会抽出 0 字符。这里不抛异常，让上层决定是否降级 OCR。
    """
    try:
        lines = text_pdf(path)
    except PdfError:
        return [], True
    return lines, not any(ln.strip() for ln in lines)


def ocr_pdf_via_render(path, dpi=RENDER_DPI):
    """伪文字层 PDF 兜底：pdftoppm 逐页渲染 PNG → 复用 WinRT OCR → 行列表。

    与 ocr_pdf 的差异：ocr_pdf 处理「单张超长整页图」，本函数处理「多页文档、
    每页一张图」。复用同一 OCR_PS1（扫描 slice_*.png），渲染时直接命名成
    slice_NN.png 就不用再切片。
    """
    if not ocr_available():
        raise PdfError("本机找不到 Windows PowerShell，无法调 WinRT OCR")
    exe = find_pdftoppm()
    if not exe:
        raise PdfError(
            "该 PDF 文字层为空，需用 pdftoppm 渲染后 OCR；"
            "但本机找不到 pdftoppm（请装 poppler/MiKTeX，或用环境变量 PDFTOPPM 指定路径）"
        )
    work = tempfile.mkdtemp(prefix="pdfo_render_")
    try:
        ps1 = os.path.join(work, "ocr.ps1")
        with open(ps1, "w", encoding="utf-8") as fh:
            fh.write(OCR_PS1)
        try:
            proc = subprocess.run(
                [exe, "-png", "-r", str(dpi), "-f", "1", "-l", "100000",
                 path, os.path.join(work, "slice")],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=600,
            )
        except subprocess.TimeoutExpired:
            raise PdfError("pdftoppm 渲染超时（>600 秒），文件可能过长")
        except OSError as exc:
            raise PdfError("启动 pdftoppm 失败：%s" % exc)
        if proc.returncode != 0:
            raise PdfError("pdftoppm 渲染失败：" + (proc.stderr or "").strip()[:400])

        # pdftoppm 输出 slice-01.png，重命名为 slice_01.png 复用 OCR 脚本
        import glob as _glob
        for png in _glob.glob(os.path.join(work, "slice-*.png")):
            base = os.path.basename(png)
            os.rename(png, os.path.join(work, base.replace("slice-", "slice_")))
        if not _glob.glob(os.path.join(work, "slice_*.png")):
            raise PdfError("pdftoppm 未产出任何页面图")

        out = os.path.join(work, "out.txt")
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", ps1, "-Dir", work, "-OutFile", out],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=600,
            )
        except subprocess.TimeoutExpired:
            raise PdfError("OCR 超时（>600 秒），文件可能过长")
        except OSError as exc:
            raise PdfError("启动 OCR 失败：%s" % exc)
        if proc.returncode != 0:
            raise PdfError("OCR 失败：" + (proc.stderr or "").strip()[:400])
        if not os.path.isfile(out):
            raise PdfError("OCR 未产出结果文件")
        with open(out, encoding="utf-8") as fh:
            return [ln for ln in fh.read().splitlines()]
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------- 渲染


def residual_noise(md):
    """成品里还残留的 HTML 痕迹，返回命中列表（空 = 干净）。"""
    return [m for m in RESIDUAL_MARKERS if m in md]


def render_markdown(title, paras, shape, source, note=None):
    """拼标准 MD：H1 + 来源/形态注记 + 正文段落。"""
    head = [
        "# " + title,
        "",
        "> 来源：" + source,
        "> 形态：" + shape,
    ]
    if note:
        head.append("> 说明：" + note)
    elif shape.startswith("截图"):
        head.append("> 说明：正文由截图 OCR 得到，段落边界为启发式推断，不还原原排版。")
    body = "\n\n".join(p for p in paras if p.strip())
    return "\n".join(head) + ("\n\n" + body if body else "") + "\n"


def _paras_from_lines(lines):
    """噪声行先丢，再合段，最后砍末尾整块信息流噪声。

    顺序有讲究：
      - 页码与数字串是「整块结构」判断（要看同一分母出现几次、数字连续几行），
        必须对原始行做完；
      - 再逐行判噪声、剥行内推广文案；
      - 最后合段 —— 信息流标题只有在成为「独立段落」之后才认得出，
        所以整块截断必须放在 reflow 之后。
    """
    kept = drop_digit_runs(drop_page_numbers(lines))
    kept = [s for s in (strip_inline_noise(ln) for ln in kept if not is_noise_line(ln)) if s]
    paras = truncate_feed_tail(reflow(kept))
    # 题号标题化必须放在合段与截尾之后：只有成为「独立段落」后，
    # 题号行的递增链才认得出；截尾先砍掉推荐区，避免它们干扰题号链。
    return mark_question_headings(paras)


# ---------------------------------------------------------------- 后端编排


def paddle_available():
    """PaddleOCR-json 是否已配置（环境变量指向引擎 exe）。"""
    try:
        import paddle_ocr
        return paddle_ocr.available()
    except Exception:                                     # noqa: BLE001
        return False


def structured_available(extractor="pymupdf4llm"):
    try:
        import structured
        return structured.available(extractor)
    except Exception:                                     # noqa: BLE001
        return False


def resolve_backend(backend="auto", kind="unknown", extractor="pymupdf4llm"):
    """后端路由（`auto` 的判据写在注释里，结果可回归）。

    auto 规则：
      - 文字层 PDF 且结构化抽取器可用 → structured（标题/表格/列表比 pdftotext 行文本强）；
      - 截图型 / 伪文字层 / 认不出 → builtin（原来的 WinRT 路径一字不改）。

    为什么截图型不自动切页级 OCR：引擎要另装 PaddleOCR-json 并靠环境变量指路，
    默认替用户换引擎会让产物形态（单张长图 vs 多页）无声变化。要页级 OCR 就显式
    `--backend paddle`，或 `--ocr-fallback` 让门禁不通过时再降级过去。
    """
    if backend not in BACKENDS:
        raise PdfError("未知后端 %r（可选 %s）" % (backend, "/".join(BACKENDS)))
    if backend != "auto":
        return backend
    if kind == "text" and structured_available(extractor):
        return "structured"
    return "builtin"


def _builtin_body(path, raw, kind):
    """内置路径：截图 OCR / pdftotext（伪文字层自动渲染兜底）。返回段落列表。"""
    if kind == "image":
        shape = "截图OCR"
        lines = ocr_pdf(raw)
    elif kind == "text":
        lines, empty = text_lines_or_empty(path)
        if empty:
            # 伪文字层：含 /Type /Font 但抽不出字符（字体来自水印/表单/校验层，
            # 每页实为整张图）。降级到「渲染页面 → OCR」而不是静默产出空正文。
            print("警告：判为文字层但抽出 0 字符，尝试渲染页面后 OCR 兜底…", file=sys.stderr)
            shape = "伪文字层渲染OCR"
            lines = ocr_pdf_via_render(path)
        else:
            shape = "文字层"
    else:
        raise PdfError("认不出这个 PDF 的形态（既没有页面图，也没有字体字典）")
    paras = _paras_from_lines(lines)
    if not paras:
        print("警告：正文为空（%d 行全被判为噪声），产物只有头部" % len(lines), file=sys.stderr)
    return dict(shape=shape, paras=paras, lines=len(lines))


def _structured_body(path, extractor="pymupdf4llm", images_root=None, write_images=True):
    """结构化路径：pymupdf4llm 主路 + pypdf 空正文兜底，配图落到 <同名>.images/。

    `images_root` 默认取 PDF 所在目录；批量 `--out/--mirror` 时传产物目录，
    否则 md 与图片分家、相对引用会断。
    `write_images=False` 只统计不落盘，供 `--check` 使用。
    """
    import structured
    stem = os.path.splitext(os.path.basename(path))[0]
    images_rel = stem + ".images"
    base = images_root or os.path.dirname(os.path.abspath(path))
    images_dir = os.path.join(base, images_rel)
    try:
        res = structured.extract(path, images_dir=images_dir, images_rel=images_rel,
                                 stem=stem, extractor=extractor,
                                 write_images=write_images)
    except structured.StructuredError as exc:
        raise PdfError(str(exc)) from exc
    return dict(body=res["md"], shape="结构化(" + res["extractor"] + ")",
                pages=res["pages"], imgs_kept=res["imgs_kept"],
                imgs_skipped=res["imgs_skipped"], image_section=structured.image_section(
                    res["images"], images_rel), ocr_txt=None, extractor=res["extractor"])


def _paddle_body(path, page_marks=False):
    """页级 OCR 路径：PaddleOCR-json 逐页识别，全文 sidecar 可续跑。"""
    import paddle_ocr
    try:
        res = paddle_ocr.run(path)
    except paddle_ocr.OcrError as exc:
        raise PdfError(str(exc)) from exc
    return dict(body=paddle_ocr.render_pages_text(res["pages"], page_marks=page_marks),
                shape="页级OCR(PaddleOCR-json)", pages=res["total_pages"],
                imgs_kept=0, imgs_skipped=0, image_section="",
                ocr_txt=res["txt"], resumed=res["resumed"], stats=res["stats"])


def _wrap(title, body, shape, source, note):
    """H1 + 注记 + 正文：结构化/页级 OCR 正文自带空段，不能再过 reflow。"""
    head = render_markdown(title, [], shape, source, note=note).rstrip()
    body = (body or "").strip()
    return head + ("\n\n" + body if body else "") + "\n"


def convert_file(path, backend="auto", extractor="pymupdf4llm",
                 ocr_fallback=False, page_marks=False, images_root=None,
                 write_images=True):
    """读 PDF → 选后端 → 出 md + 门禁结论。返回报告 dict（md 在 `md` 键）。"""
    if not os.path.isfile(path):
        raise PdfError("文件不存在：" + path)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise PdfError("读取失败：%s" % exc)
    if not detect_pdf(raw):
        raise PdfError("不是 PDF 文件：" + path)

    kind = kind_of(raw)
    chosen = resolve_backend(backend, kind, extractor)
    title = os.path.splitext(os.path.basename(path))[0]
    source = os.path.basename(path)
    extra = dict(pages=None, imgs_kept=0, imgs_skipped=0, ocr_txt=None,
                 image_section="", extractor=None, resumed=None)

    if chosen == "builtin":
        b = _builtin_body(path, raw, kind)
        shape = b["shape"]
        md = render_markdown(title, b["paras"], shape, source)
    elif chosen == "structured":
        b = _structured_body(path, extractor, images_root=images_root,
                             write_images=write_images)
        shape = b["shape"]
        md = _wrap(title, b["body"], shape, source, STRUCTURED_NOTE)
        extra.update(pages=b["pages"], imgs_kept=b["imgs_kept"],
                     imgs_skipped=b["imgs_skipped"],
                     image_section=b["image_section"], extractor=b["extractor"])
    else:                                                 # paddle
        if not paddle_available():
            raise PdfError("--backend paddle 需要 PaddleOCR-json：设置 PDF2MD_OCR_EXE "
                           "或 PDF2MD_OCR_DIR 指向引擎")
        b = _paddle_body(path, page_marks)
        shape = b["shape"]
        md = _wrap(title, b["body"], shape, source, PADDLE_NOTE)
        extra.update(pages=b["pages"], ocr_txt=b["ocr_txt"], resumed=b["resumed"])

    gate = quality.assess(md)

    # 门禁不通过时可降级到页级 OCR：只在显式 --ocr-fallback 时做，且结果不比原来差才换
    if (ocr_fallback and gate["state"] != "pass"
            and chosen in ("builtin", "structured") and paddle_available()):
        try:
            pb = _paddle_body(path, page_marks)
            body = pb["body"]
            if extra.get("image_section"):                # 结构化产物的配图段不能丢
                body = body + "\n\n" + extra["image_section"]
            cand = _wrap(title, body, pb["shape"], source, PADDLE_NOTE)
            cand_gate = quality.assess(cand)
            if GATE_RANK[cand_gate["state"]] >= GATE_RANK[gate["state"]]:
                print("质量门禁 %s（%s），已改用页级 OCR：%s" % (
                    GATE_LABEL[gate["state"]], gate["reason"],
                    GATE_LABEL[cand_gate["state"]]), file=sys.stderr)
                md, gate, chosen = cand, cand_gate, "paddle"
                shape = pb["shape"]
                extra.update(pages=pb["pages"], ocr_txt=pb["ocr_txt"],
                             resumed=pb["resumed"])
        except PdfError as exc:
            print("警告：页级 OCR 降级失败，保留原产物：%s" % exc, file=sys.stderr)

    return dict(md=md, backend=chosen, shape=shape, gate=gate,
                extractor=extra.get("extractor"), pages=extra.get("pages"),
                imgs_kept=extra.get("imgs_kept", 0),
                imgs_skipped=extra.get("imgs_skipped", 0),
                ocr_txt=extra.get("ocr_txt"), resumed=extra.get("resumed"),
                chars=len(md), kind=kind)


def _report_gate(gate, stream=None):
    print("质量门禁：%s（%s） chars=%d cjk=%.1f%% bad=%.1f%%" % (
        GATE_LABEL[gate["state"]], gate["reason"] or "无", gate["chars"],
        gate["cjk"] * 100, gate["bad"] * 100), file=stream or sys.stderr)


def process_file(path, apply=False, backup=True, strict=False, **opts):
    """读 PDF → 判形态 → 选后端 → 渲染 MD。apply=True 才写盘。

    兼容旧签名：`process_file(path, apply, backup)` 仍是原来的行为
    （auto 路由；文字层在装了 pymupdf4llm 的机器上会走结构化）。
    """
    report = convert_file(path, **opts)
    md = report["md"]
    gate = report["gate"]
    if gate["state"] != "pass":
        _report_gate(gate)
    if strict and gate["state"] != "pass":
        raise PdfError("质量门禁未通过（%s）：%s" % (gate["state"], gate["reason"]))

    if apply:
        out = os.path.splitext(path)[0] + ".md"
        # 只在没有备份时备份：否则第二次 --apply 会用上次的产物顶掉原始件
        if backup and os.path.exists(out) and not os.path.exists(out + ".bak"):
            shutil.copyfile(out, out + ".bak")
        fsutil.atomic_write_text(out, md)
    return md


def check_file(path, backend="auto", extractor="pymupdf4llm"):
    """只读预检：打印形态、后端路由、依赖、门禁、残留，返回退出码。"""
    if not os.path.isfile(path):
        print("文件不存在：" + path, file=sys.stderr)
        return 1
    with open(path, "rb") as fh:
        raw = fh.read()
    if not detect_pdf(raw):
        print("不是 PDF 文件：" + path, file=sys.stderr)
        return 1
    kind = kind_of(raw)
    try:
        chosen = resolve_backend(backend, kind, extractor)
    except PdfError as exc:
        print("后端参数错误：%s" % exc, file=sys.stderr)
        return 2
    print("文件：" + os.path.basename(path))
    print("大小：%d 字节" % len(raw))
    if kind == "image":
        got = extract_image(raw)
        if got is None:
            # 多图截图型（每页若干张图拼成）解不出「单张整页图」，内置路径确实没法做；
            # 但显式指定 structured / paddle 时不该因此拦下预检。
            print("形态：截图OCR（无单张整页图）")
            if chosen == "builtin":
                print("残留：跳过（内置路径解不了；可试 --backend structured 或 paddle）")
                return 1
        else:
            w, h, _ = got
            print("形态：截图OCR")
            print("原图：%d × %d px，切 %d 片" % (w, h, len(slice_plan(h))))
            print("OCR 可用：%s" % ("是" if ocr_available() else "否（缺 Windows PowerShell）"))
    elif kind == "text":
        print("形态：文字层")
        print("pdftotext：%s" % (find_pdftotext() or "未找到（需要 poppler 或 PDFTOTEXT）"))
    else:
        print("形态：未知")
        if chosen == "builtin":
            return 1
    # 合并进来的两条后端所需的依赖，一次说清，免得 --apply 才报错
    try:
        import structured
        st = structured.extractor_status()
        print("结构化抽取：pymupdf=%s pymupdf4llm=%s pypdf=%s" % (
            "有" if st["pymupdf"] else "无",
            "有" if st["pymupdf4llm"] else "无",
            "有" if st["pypdf"] else "无"))
    except Exception:                                     # noqa: BLE001
        print("结构化抽取：不可用（模块导入失败）")
    try:
        import paddle_ocr
        exe = paddle_ocr.engine_exe()
        print("PaddleOCR-json：%s" % (exe if exe and paddle_ocr.available()
                                     else "未配置（PDF2MD_OCR_EXE / PDF2MD_OCR_DIR）"))
    except Exception:                                     # noqa: BLE001
        print("PaddleOCR-json：不可用（模块导入失败）")
    print("后端：%s → %s" % (backend, chosen))

    # 页级 OCR 整本跑一次要几分钟（约 0.8–1.0 s/页），预检绝不能顺手触发；
    # 有完整 sidecar 时只读回复核，没有就如实说「未跑」。
    if chosen == "paddle":
        try:
            import paddle_ocr
            import structured
            total = structured.page_count(path)
            txt = paddle_ocr.text_path_for(path)
            done = paddle_ocr.is_complete(path, total)
        except Exception as exc:                          # noqa: BLE001
            print("预检失败：%s" % exc, file=sys.stderr)
            return 1
        print("页数：%d" % total)
        if not done:
            print("页级 OCR：未跑或未完成（--check 不触发 OCR，避免整本预检）")
            print("质量门禁：未评估（先 --backend paddle --apply 跑一次）")
            return 0
        title = os.path.splitext(os.path.basename(path))[0]
        md = _wrap(title, paddle_ocr.render_pages_text(paddle_ocr.load_pages(txt)),
                   "页级OCR(PaddleOCR-json)", os.path.basename(path), PADDLE_NOTE)
        gate = quality.assess(md)
        print("OCR 全文：%s（续跑命中：是）" % txt)
        print("Markdown：%d 行 / %d 字符" % (len(md.splitlines()), len(md)))
        print("质量门禁：%s（%s） chars=%d cjk=%.1f%% bad=%.1f%%" % (
            GATE_LABEL[gate["state"]], gate["reason"] or "无", gate["chars"],
            gate["cjk"] * 100, gate["bad"] * 100))
        return 0

    try:
        report = convert_file(path, backend=backend, extractor=extractor,
                              write_images=False)
    except PdfError as exc:
        print("预检失败：%s" % exc, file=sys.stderr)
        return 1
    md = report["md"]
    print("Markdown：%d 行 / %d 字符" % (len(md.splitlines()), len(md)))
    if report["pages"]:
        print("页数：%s" % report["pages"])
    if report["imgs_kept"] or report["imgs_skipped"]:
        print("配图：保留 %d 张，跳过 %d 张（小图/装饰/重复）" % (
            report["imgs_kept"], report["imgs_skipped"]))
    if report["ocr_txt"]:
        print("OCR 全文：%s（续跑命中：%s）" % (
            report["ocr_txt"], "是" if report["resumed"] else "否"))
    print("质量门禁：%s（%s） chars=%d cjk=%.1f%% bad=%.1f%%" % (
        GATE_LABEL[report["gate"]["state"]], report["gate"]["reason"] or "无",
        report["gate"]["chars"], report["gate"]["cjk"] * 100, report["gate"]["bad"] * 100))
    print("残留：%s" % (residual_noise(md) or "[]"))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="PDF → 标准 Markdown（截图 OCR / 文字层 / 结构化抽取 / 页级 OCR）"
    )
    parser.add_argument("path", help="PDF 文件路径")
    parser.add_argument("--version", action="version", version="pdf2md " + __version__)
    parser.add_argument("--check", action="store_true", help="只读预检，不写盘")
    parser.add_argument("--apply", action="store_true", help="写 <同名>.md")
    parser.add_argument("--no-backup", action="store_true", help="不备份已有 .md")
    parser.add_argument("--backend", choices=BACKENDS, default="auto",
                        help="auto（默认）/ builtin / structured / paddle")
    parser.add_argument("--extractor", choices=("pymupdf4llm", "pypdf"),
                        default="pymupdf4llm", help="结构化路径的抽取器，默认 pymupdf4llm")
    parser.add_argument("--ocr-fallback", action="store_true",
                        help="门禁不通过时降级到页级 OCR（需配置 PaddleOCR-json）")
    parser.add_argument("--page-marks", action="store_true",
                        help="页级 OCR 正文保留 ===== page n ===== 页标记")
    parser.add_argument("--strict", action="store_true",
                        help="质量门禁非 pass 时直接失败（退出码 1），不写盘")
    args = parser.parse_args(argv)
    if args.check and args.apply:
        parser.error("--check 与 --apply 互斥，请只选一个")

    try:
        if args.check:
            return check_file(args.path, backend=args.backend, extractor=args.extractor)
        md = process_file(args.path, apply=args.apply, backup=not args.no_backup,
                          strict=args.strict, backend=args.backend,
                          extractor=args.extractor, ocr_fallback=args.ocr_fallback,
                          page_marks=args.page_marks)
        if args.apply:
            print("已写：" + os.path.splitext(args.path)[0] + ".md", file=sys.stderr)
        else:
            sys.stdout.write(md)
            print("[dry-run] 未写盘；加 --apply 落盘", file=sys.stderr)
        return 0
    except PdfError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
