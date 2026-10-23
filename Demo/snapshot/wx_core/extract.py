"""从一帧微信画面中解析出消息（Lead 维护）。

输入：整窗截图 + layout.Layout + OCRWorker
输出：ParsedBlock 列表（带画面坐标、类型、发送方、文本、媒体裁剪图）

设计要点：
* **一帧只调用一次 OCR**（整块消息区），再把 OCR 行按 y 坐标分配给各个视觉消息块，避免逐块 OCR 的数百毫秒开销。
* 视觉分块纯靠像素：行投影 → 消息带(band) → 带内列投影 → 头像块/内容块。
* 语音、文件、表情包、图片都靠"像素 + OCR 文本 + 尺寸启发式"综合判断。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from . import layout as L
from .models import (KIND_EMOJI, KIND_FILE, KIND_IMAGE, KIND_SYSTEM, KIND_TEXT, KIND_UNKNOWN,
                     KIND_VOICE, SENDER_FRIEND, SENDER_SELF, OcrLine)

Box = tuple[int, int, int, int]

# 文件扩展名（微信文件卡片）
_FILE_EXT = (
    "pdf|docx?|xlsx?|pptx?|txt|md|csv|zip|rar|7z|tar|gz|mp3|mp4|wav|avi|mov|mkv|apk|exe|msi|"
    "psd|ai|sketch|fig|dwg|dxf|py|js|ts|java|cpp|c|h|go|rs|json|xml|yaml|yml|html|css|sql|db|"
    "ttf|otf|png|jpe?g|gif|bmp|webp|svg|doc|ppt|xls|pages|numbers|key|ipa|dmg|iso"
)
FILE_NAME_RE = re.compile(r"^(.{1,120}?\.(?:" + _FILE_EXT + r"))$", re.IGNORECASE)
FILE_NAME_LOOSE_RE = re.compile(r"([^\s/\\]{1,120}\.(?:" + _FILE_EXT + r"))", re.IGNORECASE)
FILE_SIZE_RE = re.compile(r"^\s*\d+(\.\d+)?\s*(B|K|KB|M|MB|G|GB|T|TB)\s*$", re.IGNORECASE)
VOICE_TEXT_RE = re.compile(r"^\s*\d{1,3}\s*[\"'′’‘`″]{0,2}\s*(秒|s)?\s*$")
VOICE_LABEL_RE = re.compile(r"转文字|语音|\d{1,3}\s*[\"'′’‘`″]{1,2}")
TIME_ONLY_RE = re.compile(r"^\s*(\d{1,2}[:：]\d{2}(:\d{2})?|\d{1,2}月\d{1,2}日(\s*\d{1,2}[:：]\d{2})?|\d{4}年\d{1,2}月\d{1,2}日(\s*\d{1,2}[:：]\d{2})?|昨天(\s*\d{1,2}[:：]\d{2})?|前天(\s*\d{1,2}[:：]\d{2})?|星期[一二三四五六日天](\s*\d{1,2}[:：]\d{2})?)\s*$")
VOICE_HINT_RE = re.compile(r"语音|voice", re.IGNORECASE)
TIME_RE = re.compile(r"(\d{1,2}:\d{2})")
DATE_RE = re.compile(r"(\d{1,2}月\d{1,2}日|\d{4}年\d{1,2}月\d{1,2}日|昨天|前天|星期[一二三四五六日天])")
SYSTEM_HINTS = ("以下是新消息", "已添加", "撤回了一条消息", "拍了拍", "加入了群聊", "邀请", "开启了好友验证",
                "你已添加了", "现在可以开始聊天", "以上是历史消息", "查看更多消息", "该消息已过期")


@dataclass
class ParsedBlock:
    """一帧里的一个视觉消息块。"""

    box: Box                       # 画面坐标 (l,t,r,b)
    kind: str
    sender: str
    text: str = ""
    media: Image.Image | None = None   # 内容块的裁剪图（图片/表情包/文件缩略图）
    avatar: Image.Image | None = None
    extra: dict = field(default_factory=dict)

    @property
    def visual_hash(self) -> str:
        return visual_hash_of(self)

    def to_message(self, friend: str, **overrides):
        from .models import Message

        extra = dict(self.extra)
        msg = Message(friend=friend, kind=self.kind, sender=self.sender, text=self.text,
                      extra=extra, **overrides)
        return msg


def visual_hash_of(block: "ParsedBlock", size: int = 40) -> str:
    """视觉指纹：媒体裁剪图缩略后的哈希；文本消息用文本哈希。"""
    if block.media is not None:
        small = block.media.convert("L").resize((size, size))
        return hashlib.sha1(small.tobytes()).hexdigest()[:16]
    data = f"{block.sender}|{block.kind}|{block.text}".encode("utf-8", "replace")
    return hashlib.sha1(data).hexdigest()[:16]


# ---------------- 分割 ----------------

def _runs(mask: np.ndarray, min_len: int = 1, merge_gap: int = 0) -> list[tuple[int, int]]:
    """一维 bool 数组 → [(start, end_exclusive)]，可合并小间隙。"""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    out: list[list[int]] = [[int(idx[0]), int(idx[0]) + 1]]
    for i in idx[1:]:
        i = int(i)
        if i <= out[-1][1] + merge_gap:
            out[-1][1] = i + 1
        else:
            out.append([i, i + 1])
    return [(a, b) for a, b in out if b - a >= min_len]


def segment_bands(img: Image.Image, lay: L.Layout, mask_threshold: int = 12) -> list[Box]:
    """把消息区切成若干"消息带"（每带≈一条消息），返回画面坐标。"""
    mx0, my0, mx1, my1 = lay.message_area
    if mx1 - mx0 < 20 or my1 - my0 < 20:
        return []
    crop = np.asarray(img.crop((mx0, my0, mx1, my1)).convert("RGB")).astype(np.int16)
    bg = np.array(lay.chat_bg, dtype=np.int16)
    diff = np.abs(crop - bg).max(axis=2)
    mask = diff > mask_threshold
    # 右侧滚动条区域不算内容
    scroll_w = int(round(14 * lay.scale))
    if mask.shape[1] > scroll_w + 40:
        mask[:, -scroll_w:] = False
    row_counts = mask.sum(axis=1)
    bands = _runs(row_counts > 3, min_len=3, merge_gap=2)
    out: list[Box] = []
    for s, e in bands:
        # 去掉上下空白边缘
        row_local = row_counts[s:e]
        nz = np.flatnonzero(row_local > 3)
        if nz.size == 0:
            continue
        s2, e2 = s + int(nz[0]), s + int(nz[-1]) + 1
        out.append((mx0, my0 + s2, mx1, my0 + e2))
    return out


def _column_blocks(a: np.ndarray, bg: tuple[int, int, int], gap: int = 7) -> list[Box]:
    """带内按列投影切出子块，返回带内坐标 (l,t,r,b)。"""
    diff = np.abs(a.astype(np.int16) - np.array(bg, dtype=np.int16)).max(axis=2)
    mask = diff > 12
    col_counts = mask.sum(axis=0)
    cols = _runs(col_counts > 0, min_len=2, merge_gap=gap)
    blocks = []
    for s, e in cols:
        sub = mask[:, s:e]
        rows = np.flatnonzero(sub.sum(axis=1) > 0)
        if rows.size == 0:
            continue
        blocks.append((int(s), int(rows[0]), int(e), int(rows[-1]) + 1))
    return blocks


def _is_avatar(block: Box, area_w: int, area_h: int, scale: float) -> bool:
    l, t, r, b = block
    w, h = r - l, b - t
    size = 40 * scale
    if not (0.6 * size <= w <= 1.6 * size and 0.6 * size <= h <= 1.6 * size):
        return False
    # 头像只会在最左或最右
    center_x = (l + r) / 2
    return center_x < 90 * scale or center_x > area_w - 90 * scale


# ---------------- OCR 行合并 ----------------

_CJK = "\u4e00-\u9fff\u3000-\u303f\uff00-\uffef"


def clean_text(text: str) -> str:
    """清理 OCR 文本：去掉中文字符之间被插入的空格。"""
    t = (text or "").strip()
    if not t:
        return ""
    t = re.sub(r"(?<=[" + _CJK + r"])\s+(?=[" + _CJK + r"])", "", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()


def merge_ocr_rows(lines: list[OcrLine]) -> list[OcrLine]:
    """把同一水平行的多个 OCR 片段合并成一行（RapidOCR 常把一行中文切成多段）。"""
    if not lines:
        return []
    items = sorted(lines, key=lambda ln: ((ln.box[1] + ln.box[3]) / 2, ln.box[0]))
    rows: list[dict] = []
    for ln in items:
        cy = (ln.box[1] + ln.box[3]) / 2
        h = max(1, ln.box[3] - ln.box[1])
        placed = False
        for row in rows:
            tol = max(4.0, 0.55 * max(h, row["h"]))
            if abs(cy - row["cy"]) <= tol:
                row["lines"].append(ln)
                n = len(row["lines"])
                row["cy"] = (row["cy"] * (n - 1) + cy) / n
                row["h"] = max(row["h"], h)
                placed = True
                break
        if not placed:
            rows.append({"cy": cy, "h": h, "lines": [ln]})
    out: list[OcrLine] = []
    for row in rows:
        segs = sorted(row["lines"], key=lambda ln: ln.box[0])
        text = clean_text("".join(seg.text for seg in segs))
        if not text:
            continue
        box = (min(seg.box[0] for seg in segs), min(seg.box[1] for seg in segs),
               max(seg.box[2] for seg in segs), max(seg.box[3] for seg in segs))
        segments = [{"text": seg.text, "box": seg.box} for seg in segs]
        out.append(OcrLine(text=text, box=box, words=segments))
    out.sort(key=lambda ln: (ln.box[1], ln.box[0]))
    return out


# ---------------- 文本分类 ----------------

def classify_text(lines: list[OcrLine]) -> tuple[str, str]:
    """根据 OCR 行判断文本消息类型，返回 (kind, text)。"""
    texts = [ln.text.strip() for ln in lines if ln.text.strip()]
    if not texts:
        return KIND_UNKNOWN, ""
    joined = "\n".join(texts)
    flat = " ".join(texts)
    for t in texts:
        if VOICE_HINT_RE.search(t):
            return KIND_VOICE, flat
        if VOICE_TEXT_RE.match(t):
            return KIND_VOICE, flat
        if VOICE_LABEL_RE.search(t) and len(t) <= 12:
            return KIND_VOICE, flat
    # 文件：文件名 + 可选的体积文字
    for t in texts:
        m = FILE_NAME_RE.match(t.strip())
        if m:
            return KIND_FILE, m.group(1)
        m2 = FILE_NAME_LOOSE_RE.search(t)
        if m2 and len(t.strip()) <= len(m2.group(1)) + 12:
            return KIND_FILE, m2.group(1)
    # 纯体积文字 + 另一行是文件名（拆成两行时）
    if len(texts) == 2 and FILE_SIZE_RE.match(texts[1]):
        m = FILE_NAME_LOOSE_RE.search(texts[0])
        if m:
            return KIND_FILE, m.group(1)
    return KIND_TEXT, joined


def _is_system_text(text: str) -> bool:
    t = text.strip()
    if not t:
        return False
    if any(h in t for h in SYSTEM_HINTS):
        return True
    if len(t) <= 40:
        rest = TIME_RE.sub("", t)
        rest = DATE_RE.sub("", rest)
        if not rest.strip(" \u3000·-—"):
            return True
    return False


# ---------------- 主解析 ----------------

def parse_frame(img: Image.Image, lay: L.Layout, ocr, friend: str, *,
                ocr_lines: list[OcrLine] | None = None) -> list[ParsedBlock]:
    """解析一帧里的所有消息块。

    ``ocr_lines`` 可传入调用方已经做过的整区 OCR 结果（画面坐标），避免重复识别。
    """
    mx0, my0, mx1, my1 = lay.message_area
    area_w, area_h = mx1 - mx0, my1 - my0
    if area_w < 20 or area_h < 20:
        return []
    area_img = img.crop((mx0, my0, mx1, my1))
    a = np.asarray(area_img.convert("RGB"))
    bg = lay.chat_bg
    scale = lay.scale
    outgoing = lay.outgoing_bubble
    incoming = lay.incoming_bubble

    if ocr_lines is None:
        ocr_lines = ocr.ocr(area_img)
    ocr_lines = merge_ocr_rows(list(ocr_lines))
    # 统一到画面坐标
    lines: list[OcrLine] = []
    for ln in ocr_lines:
        b = ln.box
        lines.append(OcrLine(ln.text, (b[0] + mx0, b[1] + my0, b[2] + mx0, b[3] + my0), ln.words))

    blocks = segment_bands(img, lay)
    parsed: list[ParsedBlock] = []
    for band in blocks:
        bl, bt, br, bb = band
        # 贴着消息区上下边缘的是"半条消息"（被滚动裁切），OCR 会读出乱码，直接跳过
        if bt - my0 <= 2 or my1 - bb <= 2:
            continue
        local = a[bt - my0:bb - my0, bl - mx0:br - mx0, :]
        if local.size == 0:
            continue
        sub_blocks = _column_blocks(local, bg)
        avatars = [b for b in sub_blocks if _is_avatar(b, area_w, area_h, scale)]
        contents = [b for b in sub_blocks if b not in avatars]
        if not contents:
            continue
        # 取面积最大的非头像块作为内容块（表情包可能带多个小块）
        contents.sort(key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
        cb = contents[0]
        # 过滤 1~3 像素高的碎片（图片加载/滚动中间态容易切出这种块）
        if (cb[3] - cb[1]) < 10 * scale or (cb[2] - cb[0]) < 10 * scale:
            continue
        cimg = area_img.crop(cb)
        content_box = (bl + cb[0], bt + cb[1], bl + cb[2], bt + cb[3])
        avatar_img = None
        if avatars:
            avatars.sort(key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
            avatar_img = area_img.crop(avatars[0])

        # 带内的 OCR 行
        band_lines = [ln for ln in lines if ln.box[1] >= bt - 4 and ln.box[3] <= bb + 4]
        # 发送方
        sender = SENDER_FRIEND
        if avatars:
            av_center = (avatars[0][0] + avatars[0][2]) / 2
            sender = SENDER_SELF if av_center > area_w / 2 else SENDER_FRIEND
        else:
            sender = SENDER_SELF if (cb[0] + cb[2]) / 2 > area_w * 0.55 else SENDER_FRIEND

        # 气泡色占比
        sub = local[cb[1]:cb[3], cb[0]:cb[2], :].astype(np.int16)
        out_frac = float((np.abs(sub - np.array(outgoing)).max(axis=2) <= 10).mean())
        in_frac = float((np.abs(sub - np.array(incoming)).max(axis=2) <= 10).mean())
        has_bubble = max(out_frac, in_frac) > 0.10
        if not avatars and has_bubble:
            sender = SENDER_SELF if out_frac > in_frac else SENDER_FRIEND

        text_kind, text = classify_text(band_lines)

        # 纯时间/日期行（聊天时间分隔）一律当系统消息
        if TIME_ONLY_RE.match((text or "").strip()) and (bb - bt) <= int(30 * scale):
            parsed.append(ParsedBlock(box=band, kind=KIND_SYSTEM, sender=SENDER_FRIEND,
                                      text=text.strip(), extra={"center": True}))
            continue

        # 系统消息：居中、无头像、无气泡
        if not avatars and not has_bubble and band_lines:
            xs = [(ln.box[0] + ln.box[2]) / 2 for ln in band_lines]
            center_ok = xs and all(abs(x - (mx0 + area_w / 2)) < area_w * 0.18 for x in xs)
            if center_ok and _is_system_text(text):
                parsed.append(ParsedBlock(box=band, kind=KIND_SYSTEM, sender=SENDER_FRIEND,
                                          text=text.strip(), extra={"center": True}))
                continue
            if center_ok and len(text.strip()) <= 30 and not any(
                    ln.box[2] - ln.box[0] > area_w * 0.5 for ln in band_lines):
                parsed.append(ParsedBlock(box=band, kind=KIND_SYSTEM, sender=SENDER_FRIEND,
                                          text=text.strip(), extra={"center": True}))
                continue

        if text_kind == KIND_FILE or (text_kind == KIND_UNKNOWN and has_bubble and band_lines):
            if text_kind == KIND_UNKNOWN:
                text_kind = KIND_TEXT
            kind = KIND_FILE if text_kind == KIND_FILE else KIND_TEXT
            extra = {}
            if kind == KIND_FILE:
                size_line = next((ln.text.strip() for ln in band_lines if FILE_SIZE_RE.match(ln.text.strip())), "")
                if size_line:
                    extra["size_text"] = size_line
                extra["text"] = text
            parsed.append(ParsedBlock(box=content_box, kind=kind, sender=sender, text=text,
                                      media=None, avatar=avatar_img, extra=extra))
            continue

        if text_kind == KIND_VOICE:
            extra = {"notice": "语音消息未接收"}
            m = re.search(r"(\d{1,3})\s*[\"'′’‘`″]", text or "")
            if m:
                extra["duration"] = f"{m.group(1)}秒"
            else:
                for ln in band_lines:
                    mm = re.search(r"(?<!\d)(\d{1,2})(?!\d)", ln.text or "")
                    if mm and 1 <= int(mm.group(1)) <= 60:
                        extra["duration"] = f"{mm.group(1)}秒"
                        break
            parsed.append(ParsedBlock(box=content_box, kind=KIND_VOICE, sender=sender,
                                      text="", media=cimg,
                                      avatar=avatar_img, extra=extra))
            continue

        if text_kind == KIND_TEXT and band_lines and has_bubble:
            parsed.append(ParsedBlock(box=content_box, kind=KIND_TEXT, sender=sender, text=text,
                                      media=None, avatar=avatar_img))
            continue

        # 没有文字：图片 或 表情包
        if band_lines and has_bubble:
            parsed.append(ParsedBlock(box=content_box, kind=KIND_TEXT, sender=sender, text=text,
                                      media=None, avatar=avatar_img))
            continue

        w, h = cb[2] - cb[0], cb[3] - cb[1]
        max_side = max(w, h)
        if max_side <= 0:
            continue
        emoji_size = 210 * scale
        aspect = w / max(1, h)
        kind = KIND_EMOJI if (max_side <= emoji_size and 0.45 <= aspect <= 2.2) else KIND_IMAGE
        parsed.append(ParsedBlock(box=content_box, kind=kind, sender=sender, text="",
                                  media=cimg, avatar=avatar_img,
                                  extra={"w": w, "h": h, "has_bubble": has_bubble}))

    return parsed


def read_chat_title(img: Image.Image, lay: L.Layout, ocr) -> str:
    """OCR 聊天标题栏，返回当前会话名（失败返回空串）。"""
    cl, ct, cr, cb = lay.header
    # 标题文字在左侧，避开右上角的按钮
    box = (cl + int(round(8 * lay.scale)), ct + int(round(6 * lay.scale)),
           cl + int((cr - cl) * 0.55), cb - int(round(2 * lay.scale)))
    if box[2] - box[0] < 30 or box[3] - box[1] < 10:
        return ""
    crop = img.crop(box)
    if crop.width < 2 or crop.height < 2:
        return ""
    lines = merge_ocr_rows(ocr.ocr(crop))
    for ln in lines:
        t = ln.text.strip()
        if t:
            return t
    return ""


# ---------------- 左侧列表（会话列表 / 通讯录列表） ----------------

def list_rows(img: Image.Image, lay: L.Layout) -> list[Box]:
    """检测左侧列表里的每一行（靠头像块定位），返回画面坐标。"""
    lx0, ly0, lx1, ly1 = lay.conv_list
    scale = lay.scale
    top = ly0 + int(round(92 * scale))
    if ly1 - top < 60:
        return []
    ax0 = lx0 + int(round(8 * scale))
    ax1 = lx0 + int(round(62 * scale))
    strip = np.asarray(img.crop((ax0, top, ax1, ly1)).convert("RGB")).astype(np.int16)
    bg = np.array(lay.list_bg, dtype=np.int16)
    mask = np.abs(strip - bg).max(axis=2) > 22
    rows = _runs(mask.sum(axis=1) > 6, min_len=6, merge_gap=4)
    out: list[Box] = []
    for s, e in rows:
        h = e - s
        if not (26 * scale <= h <= 90 * scale):
            continue
        center = top + (s + e) // 2
        half = int(round(28 * scale))
        out.append((lx0, center - half, lx1, center + half))
    return out


def _row_text_x(img: Image.Image, lay: L.Layout, row_box: Box) -> int:
    """一行的文字起点：头像块右边缘 + 间距（会话列表与通讯录缩进不同）。"""
    lx0, lx1 = lay.conv_list[0], lay.conv_list[2]
    scale = lay.scale
    x0 = lx0 + int(round(4 * scale))
    x1 = lx1 - int(round(8 * scale))
    if x1 - x0 < 30:
        return lx0 + int(round(72 * scale))
    strip = np.asarray(img.crop((x0, row_box[1], x1, row_box[3])).convert("RGB")).astype(np.int16)
    bg = np.array(lay.list_bg, dtype=np.int16)
    mask = np.abs(strip - bg).max(axis=2) > 18
    # 被选中的行整行高亮，列投影会连成一片 → 用固定缩进
    if float(mask.mean()) > 0.55:
        return lx0 + int(round(48 * scale))
    cols = _runs(mask.sum(axis=0) > 2, min_len=2, merge_gap=2)
    for s, e in cols:
        if 18 * scale <= e - s <= 75 * scale:
            return x0 + e + int(round(4 * scale))
    return lx0 + int(round(50 * scale))


def _split_segments_by_gap(segments: list[dict], line_h: int) -> list[list[dict]]:
    """把同一 OCR 行里距离很远的片段分组（例如左侧"名字"与右侧"时间"）。"""
    groups: list[list[dict]] = []
    for seg in segments:
        if not groups:
            groups.append([seg])
            continue
        prev = groups[-1][-1]
        gap = seg["box"][0] - prev["box"][2]
        if gap > max(10, int(line_h * 1.8)):
            groups.append([seg])
        else:
            groups[-1].append(seg)
    return groups


_TRAIL_TIME_RE = re.compile(r"\s*(\d{1,2}[:：]\d{2}|\d{1,2}/\d{1,2}|\d{4}/\d{1,2}/\d{1,2}|昨天|星期[一二三四五六日天])\s*$")


def _strip_trailing_meta(name: str) -> str:
    """去掉粘在名字后面的时间/未读标记。"""
    t = name.strip()
    for _ in range(2):
        new = _TRAIL_TIME_RE.sub("", t)
        new = re.sub(r"[①-⑩]|^\d+\s*$", "", new).strip()
        if new == t:
            break
        t = new
    return t.strip()


def read_list_rows(img: Image.Image, lay: L.Layout, ocr, *, max_rows: int = 40) -> list[dict]:
    """读取左侧列表每一行的名称/预览，返回 [{name, preview, box, center_y, center_x}, ...]。

    实现：对"整个列表文字区"只做**一次** OCR（比逐行 OCR 快 3~5 倍，且整块识别率更高），
    再把 OCR 行按 y 坐标分配给各个视觉行。
    """
    rows = list_rows(img, lay)[:max_rows]
    if not rows:
        return []
    scale = lay.scale
    lx0, lx1 = lay.conv_list[0], lay.conv_list[2]
    top = min(b[1] for b in rows)
    bottom = max(b[3] for b in rows)
    tx0 = min(_row_text_x(img, lay, b) for b in rows) - int(round(4 * scale))
    crop = img.crop((max(0, tx0), top, lx1 - int(round(8 * scale)), bottom))
    if crop.width < 20 or crop.height < 10:
        return []
    lines = merge_ocr_rows(ocr.ocr(crop))
    if not lines:
        return []
    centers = [(b[1] + b[3]) / 2 for b in rows]
    grouped: list[list] = [[] for _ in rows]
    for ln in lines:
        cy = (ln.box[1] + ln.box[3]) / 2 + top
        idx = min(range(len(rows)), key=lambda i: abs(centers[i] - cy))
        grouped[idx].append(ln)
    out: list[dict] = []
    for box, row_lines in zip(rows, grouped):
        if not row_lines:
            continue
        row_lines.sort(key=lambda ln: (ln.box[1], ln.box[0]))
        first = row_lines[0]
        line_h = max(1, first.box[3] - first.box[1])
        segs = first.words or [{"text": first.text, "box": first.box}]
        groups = _split_segments_by_gap(segs, line_h)
        name = clean_text("".join(g["text"] for g in groups[0])) if groups else ""
        name = _strip_trailing_meta(name)
        preview = clean_text("".join(ln.text for ln in row_lines[1:]))
        if not name:
            continue
        out.append({"name": name, "preview": preview, "box": box,
                    "center_y": (box[1] + box[3]) // 2,
                    "center_x": (lx0 + lx1) // 2})
    return out
