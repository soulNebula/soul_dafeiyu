"""好友列表同步：打开「通讯录」→ 逐屏 OCR → 真实滚轮翻页 → 收集全部好友名 → 切回「聊天」。

对外只暴露一个函数::

    enumerate_friends(win, lay, ocr, log_cb=None, max_scrolls=120, time_budget=90.0) -> list[str]

返回：按通讯录顺序排好的好友显示名列表（已去重）。

**红线**：本模块只用「窗口截图（PrintWindow）+ 屏幕 OCR + 窗口级点击（PostMessage）+
真实滚轮（mouse_event）」这一套外部观察手段；绝不读取微信进程内存、不注入 DLL、不 Hook、
不修改微信任何文件、绝不发送消息。

## 实现要点

* **页面识别**：截图 → OCR 列表面板；出现「通讯录管理」按钮或「联系人 N」表头即认定在通讯录页。
* **行解析**：一帧只 OCR **一次**（整个列表面板），再按几何规则过滤：
  - 表头区（「搜索 / 通讯录管理 / 联系人 N」）整段丢弃；
  - 名字列（面板左边缘 + ``NAME_COL_OFFSET`` 逻辑像素以右）之外的文字（字母分组标题 B/W、
    头像）丢弃；
  - 黑名单入口项（通讯录管理 / 新的朋友 / 群聊 / 标签 / 公众号 …）、单个字母、纯数字丢弃；
  - 被滚动区上下边缘**裁切**的文字行丢弃（文字可能不完整 → 会污染名单）。
* **翻页**：先向上滚回顶部；再向下滚 1 格测出「1 格 ≈ 多少像素」，按
  ``可见行数 - ROW_OVERLAP`` 计算每次滚动的格数（保留重叠，防漏行）；连续 2 次没有新名字
  即判定到底，另有 ``max_scrolls`` 与 ``time_budget`` 兜底。
* **安全**：所有抢前台 / 移动光标 / 滚轮操作都在 ``try/finally`` 里复原（光标位置、原前台窗口），
  无论成功失败最后都会点回侧边栏「聊天」图标。

测试注入点见本文件「可注入依赖」一节：``_capture`` / ``_click`` / ``_analyze`` / ``_scroll``
都是模块级函数，单测里 monkeypatch 掉即可完全离线。
"""
from __future__ import annotations

import ctypes
import hashlib
import re
import statistics
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
from PIL import Image

from . import extract as E
from . import layout as L
from . import win as W
from .models import OcrLine

__all__ = ["enumerate_friends"]

# ---------------------------------------------------------------- 逻辑像素常量
# 微信 4.x @100% DPI 实测（125% DPI 时乘以 layout.scale）
LOGICAL_SIDEBAR_W = 67.2      # 左侧图标栏宽度
LOGICAL_SIDEBAR_CX = 36.0     # 图标栏中心 x
LOGICAL_TAB_Y = (108.8, 160.0, 208.0, 256.0)   # 聊天 / 通讯录 / 收藏 / 朋友圈 图标中心 y
TAB_NAMES = ("chat", "contacts", "favorites", "moments")
TAB_INDEX = {name: i for i, name in enumerate(TAB_NAMES)}

NAME_COL_OFFSET = 55.0        # 名字列相对列表面板左边缘的最小偏移（逻辑 px）
REGION_TOP_FALLBACK = 168.0   # 找不到「联系人」表头时，滚动区上边界的兜底值（逻辑 px）
ROW_OVERLAP = 3               # 每次滚动保留的重叠行数
DEFAULT_ROW_H = 57.6          # 兜底行距（逻辑 px）

MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120

# 「通讯录」页里除好友以外的入口/按钮文字，全部过滤
ENTRY_NAMES = frozenset({
    "通讯录管理", "新的朋友", "群聊", "标签", "公众号", "服务号", "企业微信联系人",
    "联系人", "搜索", "更多", "新的朋友添加", "添加朋友", "通讯录",
})

_LETTER_RE = re.compile(r"^[A-Za-z]$")
_NUMERIC_RE = re.compile(r"^[\d\s:：/.\-—年月日时分秒]+$")
_DIGITS_RE = re.compile(r"^[0-9]+$")
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


# ---------------------------------------------------------------- 可注入依赖
# 单测里直接 monkeypatch 这些模块级函数，就能在没有微信、没有 Windows OCR 的机器上跑。

def _capture(win) -> Optional[Image.Image]:
    """截取微信窗口画面（画面坐标原点 = 窗口左上角）。测试注入点。"""
    return W.capture(win)


def _click(win, x: int, y: int) -> bool:
    """向微信主窗口投递一次点击（不抢焦点）。测试注入点。"""
    return W.post_click(win, int(x), int(y))


def _analyze(img: Image.Image, win) -> "L.Layout":
    """重新分析布局。测试注入点。"""
    return L.analyze(img, win)


def _now() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    """统一 sleep（测试里 patch 成空实现，让单测秒过）。"""
    time.sleep(float(seconds))


def _foreground() -> int:
    try:
        return int(W.user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def _set_foreground(hwnd: int) -> None:
    try:
        W.user32.SetForegroundWindow(int(hwnd))
    except Exception:
        pass


def _cursor() -> tuple[int, int]:
    try:
        pt = wintypes.POINT()
        W.user32.GetCursorPos(ctypes.byref(pt))
        return (int(pt.x), int(pt.y))
    except Exception:
        return (0, 0)


def _set_cursor(x: int, y: int) -> None:
    try:
        W.user32.SetCursorPos(int(x), int(y))
    except Exception:
        pass


def _mouse_wheel(delta: int) -> None:
    """发一次真实滚轮事件（delta>0 向上，<0 向下）。测试注入点。"""
    W.user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, int(delta), 0)


def _scroll(win, x: int, y: int, notches: int) -> bool:
    """真实滚轮滚动列表（必须先把微信抢到前台）。

    ``notches > 0`` 向上滚，``< 0`` 向下滚。返回 True 表示滚轮事件确实发出去了。
    无论成功失败，光标位置与原前台窗口都会在 finally 里复原。
    """
    n = int(notches)
    if n == 0:
        return True
    prev_fg = _foreground()
    prev_cursor = _cursor()
    try:
        try:
            if not W.is_foreground(win):
                _force_foreground(int(getattr(win, "hwnd", 0) or 0))
        except Exception:
            pass
        try:
            if not W.is_foreground(win):
                return False
        except Exception:
            return False
        try:
            sx, sy = win.image_to_screen(int(x), int(y))
        except Exception:
            return False
        _set_cursor(int(sx), int(sy))
        _sleep(0.04)
        step = WHEEL_DELTA if n > 0 else -WHEEL_DELTA
        for _ in range(abs(n)):
            _mouse_wheel(step)
            _sleep(0.02)
        _sleep(0.06)
        return True
    finally:
        try:
            _set_cursor(*prev_cursor)
        except Exception:
            pass
        try:
            _restore_foreground(prev_fg, win)
        except Exception:
            pass


VK_MENU = 0x12


def _force_foreground(hwnd: int) -> bool:
    """把指定窗口切到前台。

    ``SetForegroundWindow`` 在"当前进程不是前台"时经常被系统静默拒绝（实测微信窗口
    就是这样），所以依次尝试：SetForegroundWindow → AttachThreadInput 到当前前台线程再
    SetForegroundWindow → 轻敲一下 ALT（SendInput 会被视为真实用户输入，解除前台锁）。
    返回是否成功切到前台。
    """
    hwnd = int(hwnd or 0)
    if not hwnd:
        return False
    try:
        if _foreground() == hwnd:
            return True
    except Exception:
        return False
    _set_foreground(hwnd)
    _sleep(0.10)
    if _foreground() == hwnd:
        return True
    try:
        fg = _foreground()
        tid = wintypes.DWORD()
        W.user32.GetWindowThreadProcessId(int(fg), ctypes.byref(tid))
        cur = int(W.kernel32.GetCurrentThreadId())
        W.user32.AttachThreadInput(cur, int(tid.value), True)
        try:
            W.user32.SetForegroundWindow(hwnd)
        finally:
            W.user32.AttachThreadInput(cur, int(tid.value), False)
        _sleep(0.10)
        if _foreground() == hwnd:
            return True
    except Exception:
        pass
    try:
        W.user32.keybd_event(VK_MENU, 0, 0, 0)
        W.user32.keybd_event(VK_MENU, 0, 2, 0)
        _sleep(0.05)
        W.user32.SetForegroundWindow(hwnd)
        _sleep(0.10)
    except Exception:
        pass
    return _foreground() == hwnd


def _restore_foreground(prev_fg: int, win=None) -> bool:
    """把前台窗口还给调用前那个窗口（只在被抢走时才动作）。"""
    prev_fg = int(prev_fg or 0)
    if not prev_fg:
        return False
    win_hwnd = int(getattr(win, "hwnd", 0) or 0) if win is not None else 0
    if prev_fg == win_hwnd:
        return True
    try:
        if _foreground() == prev_fg:
            return True
    except Exception:
        pass
    return _force_foreground(prev_fg)


# ---------------------------------------------------------------- 小工具

def _scale(lay) -> float:
    try:
        s = float(getattr(lay, "scale", 1.0) or 1.0)
        return s if s > 0 else 1.0
    except Exception:
        return 1.0


def _logger(log_cb: Optional[Callable]) -> Callable[[str, str], None]:
    def log(text: str, level: str = "info") -> None:
        if log_cb is None:
            return
        try:
            log_cb(str(text), str(level))
        except TypeError:
            try:
                log_cb(str(text))
            except Exception:
                pass
        except Exception:
            pass

    return log


def _merge_ocr(lines) -> list[OcrLine]:
    if not lines:
        return []
    try:
        return E.merge_ocr_rows(list(lines))
    except Exception:
        return [ln for ln in lines if getattr(ln, "text", "")]


def _clean(text: str) -> str:
    try:
        return E.clean_text(text)
    except Exception:
        return (text or "").strip()


def _box_of(line) -> tuple[int, int, int, int]:
    b = getattr(line, "box", (0, 0, 0, 0))
    return (int(b[0]), int(b[1]), int(b[2]), int(b[3]))


def _is_entry_or_heading(text: str) -> bool:
    core = (text or "").strip().replace(" ", "")
    if not core:
        return True
    if _LETTER_RE.match(core):
        return True
    if core in ENTRY_NAMES:
        return True
    if _NUMERIC_RE.match(core):
        return True
    return False


def _digits_of(text: str) -> Optional[int]:
    t = (text or "").strip().translate(_FULLWIDTH_DIGITS).replace(" ", "")
    if not t or not _DIGITS_RE.match(t):
        return None
    try:
        n = int(t)
    except ValueError:
        return None
    return n if 0 < n < 1_000_000 else None


def _collapse_digit_dup(t: str) -> str:
    """去掉 OCR 把同一个数字识别成「全角 + 半角」两段的伪影（如 ``４4`` → ``4``）。"""
    if not t:
        return t
    out: list[str] = []
    i = 0
    while i < len(t):
        ch = t[i]
        if i + 1 < len(t):
            a, b = ch, t[i + 1]
            a_half = a.translate(_FULLWIDTH_DIGITS)
            b_half = b.translate(_FULLWIDTH_DIGITS)
            if (a != a_half or b != b_half) and a_half.isdigit() and a_half == b_half:
                out.append(a_half)
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _count_from_text(text: str) -> Optional[int]:
    """从「联系人 N」整行文本里取 N（OCR 有时把数字直接拼进同一行）。

    实测微信 4.1.15.56 的展开箭头会被识别成 ``V`` 或 ``〈``，和「联系人」连成一行，
    所以只要求行尾是「联系人 + 数字」，不要求行首就是「联系人」。
    """
    t = _collapse_digit_dup((text or "").strip()).translate(_FULLWIDTH_DIGITS).strip()
    m = re.search(r"联系人\s*(\d{1,6})\s*$", t)
    if not m:
        return None
    try:
        n = int(m.group(1))
    except ValueError:
        return None
    return n if 0 < n < 1_000_000 else None


def _count_from_words(words, panel_width: int) -> Optional[int]:
    """从「联系人 N」这一行的 OCR 片段里取出官方总数 N。

    微信把数字画在表头最右侧，且同一数字可能被识别成重叠的两段（``４`` 与 ``4``），
    所以先只保留面板右半侧的纯数字段，再按重叠去重，最后按 x 顺序拼起来。
    """
    segs: list[tuple[int, int, str]] = []
    for seg in words or []:
        try:
            t = str(seg.get("text", ""))
            b = seg.get("box") or (0, 0, 0, 0)
            l, r = int(b[0]), int(b[2])
        except Exception:
            continue
        n = _digits_of(t)
        if n is None:
            continue
        if panel_width > 0 and r < panel_width * 0.5:
            continue
        segs.append((l, r, str(n)))
    if not segs:
        return None
    segs.sort(key=lambda s: s[0])
    kept: list[tuple[int, int, str]] = []
    for l, r, t in segs:
        dup = False
        for l2, r2, _ in kept:
            inter = min(r, r2) - max(l, l2)
            if inter > 0.5 * max(1, min(r - l, r2 - l2)):
                dup = True
                break
        if not dup:
            kept.append((l, r, t))
    if not kept:
        return None
    joined = "".join(t for _, _, t in kept)
    try:
        n = int(joined)
    except ValueError:
        return None
    return n if 0 < n < 1_000_000 else None


# ---------------------------------------------------------------- 几何检测

def _list_panel_left(img: Image.Image, lay) -> int:
    """列表面板左边界（画面坐标）：从聊天区左边缘往左走，找「列表底色」连续段。"""
    s = _scale(lay)
    try:
        cx0, cy0, cx1, cy1 = lay.content
    except Exception:
        return int(round(LOGICAL_SIDEBAR_W * s))
    try:
        chat_left = int(lay.chat[0])
    except Exception:
        return cx0 + int(round(LOGICAL_SIDEBAR_W * s))
    fallback = cx0 + int(round(LOGICAL_SIDEBAR_W * s))
    if chat_left - cx0 < int(round(160 * s)):
        return fallback
    try:
        a = np.asarray(img.convert("RGB")).astype(np.int16)
    except Exception:
        return fallback
    h = a.shape[0]
    y0 = cy0 + int((cy1 - cy0) * 0.15)
    y1 = cy0 + int((cy1 - cy0) * 0.85)
    if y1 - y0 < 40:
        return fallback
    step = max(1, (y1 - y0) // 150)
    sub = a[y0:y1:step, cx0:chat_left, :]
    if sub.size == 0:
        return fallback
    med = np.median(sub.astype(np.float32), axis=0)          # (n_cols, 3)
    n = med.shape[0]
    ref = int(chat_left - cx0 - 6)
    if not (0 <= ref < n):
        return fallback
    panel_bg = med[ref]
    x = n - 1
    while x > 0 and float(np.abs(med[x - 1] - panel_bg).max()) <= 3.0:
        x -= 1
    left = cx0 + int(x)
    if not (cx0 + int(round(40 * s)) <= left <= chat_left - int(round(120 * s))):
        return fallback
    return left


def _list_panel(img: Image.Image, lay) -> tuple[int, int, int, int]:
    """列表面板矩形（画面坐标）。测试注入点。"""
    cx0, cy0, cx1, cy1 = lay.content
    try:
        chat_left = int(lay.chat[0])
    except Exception:
        chat_left = int(cx1)
    if chat_left <= cx0 + 100:
        chat_left = int(cx1)
    return (_list_panel_left(img, lay), int(cy0), chat_left, int(cy1))


def _runs(mask: np.ndarray, min_len: int = 1, merge_gap: int = 0) -> list[tuple[int, int]]:
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


def _sidebar_tabs(img: Image.Image, lay) -> list[dict]:
    """检测左侧图标栏里竖排的图标块（聊天/通讯录/收藏/朋友圈），按 y 排序。"""
    s = _scale(lay)
    try:
        cx0, cy0, cx1, cy1 = lay.content
    except Exception:
        return []
    try:
        a = np.asarray(img.convert("RGB")).astype(np.int16)
    except Exception:
        return []
    try:
        bg = np.array(lay.sidebar_bg, dtype=np.int16)
    except Exception:
        return []
    x_from = cx0 + int(round(8 * s))
    x_to = cx0 + int(round(LOGICAL_SIDEBAR_W * s) - 6 * s)
    if x_to - x_from < int(12 * s) or x_to > a.shape[1] or cy1 > a.shape[0]:
        return []
    strip = a[int(cy0):int(cy1), x_from:x_to, :]
    if strip.size == 0:
        return []
    mask = np.abs(strip - bg).max(axis=2) > 16
    runs = _runs(mask.sum(axis=1) > max(2, int(2 * s)), min_len=max(4, int(6 * s)),
                 merge_gap=int(round(6 * s)))
    out: list[dict] = []
    lo = cy0 + 85 * s
    hi = cy0 + 0.62 * (cy1 - cy0)
    for a0, b0 in runs:
        h = b0 - a0
        if h < 10 * s or h > 38 * s:
            continue
        cols = np.flatnonzero(mask[a0:b0].sum(axis=0) > 0)
        if cols.size == 0:
            continue
        w = int(cols[-1]) - int(cols[0])
        if w < 8 * s or w > 46 * s:
            continue
        cy = cy0 + (a0 + b0) // 2
        if not (lo <= cy <= hi):
            continue
        out.append({"cx": x_from + int((int(cols[0]) + int(cols[-1])) // 2), "cy": cy, "h": h})
    out.sort(key=lambda d: d["cy"])
    return out


def _tab_point(img: Image.Image, lay, name: str, attempt: int = 0) -> tuple[int, int]:
    """侧边栏某个图标的点击点（画面坐标）。第一次用像素检测，第 2 次用逻辑常量兜底。"""
    s = _scale(lay)
    try:
        cx0, cy0 = lay.content[0], lay.content[1]
    except Exception:
        cx0 = cy0 = 0
    expected = (cx0 + int(round(LOGICAL_SIDEBAR_CX * s)),
                cy0 + int(round(LOGICAL_TAB_Y[TAB_INDEX[name]] * s)))
    if attempt != 1:
        tabs = _sidebar_tabs(img, lay)
        idx = TAB_INDEX[name]
        if len(tabs) > idx:
            cand = tabs[idx]
            if abs(cand["cy"] - expected[1]) <= 16 * s:
                return (int(cand["cx"]), int(cand["cy"]))
    if attempt >= 2:
        return (expected[0], expected[1] + int(round(10 * s)))
    return expected


def _region_hash(img: Image.Image, box) -> str:
    try:
        crop = img.crop((int(box[0]), int(box[1]), int(box[2]), int(box[3])))
        data = crop.tobytes()
    except Exception:
        return ""
    return hashlib.sha1(data).hexdigest()


# ---------------------------------------------------------------- 帧解析

@dataclass
class _Screen:
    """一帧通讯录页的解析结果。"""

    ok: bool = False                                   # 是否通讯录页
    names: list[str] = field(default_factory=list)     # 可安全采用的好友名（按 y 顺序）
    name_centers: list[int] = field(default_factory=list)
    lines: list[tuple[str, int]] = field(default_factory=list)   # 滚动区内所有文字行 (text, cy)
    total: Optional[int] = None                        # 「联系人 N」里的 N
    header_box: tuple[int, int, int, int] = (0, 0, 0, 0)
    panel: tuple[int, int, int, int] = (0, 0, 0, 0)
    region_top: int = 0
    region_bottom: int = 0

    @property
    def sig(self) -> tuple:
        return tuple(self.lines)


def _scan(img: Image.Image, lay, ocr) -> _Screen:
    """解析一帧：一帧只调用一次 OCR（整个列表面板），返回 _Screen。"""
    res = _Screen()
    try:
        panel = _list_panel(img, lay)
    except Exception:
        return res
    l, t, r, b = panel
    res.panel = panel
    s = _scale(lay)
    if r - l < int(40 * s) or b - t < int(80 * s):
        return res
    try:
        crop = img.crop(panel)
        raw = ocr.ocr(crop)
    except Exception:
        return res
    lines: list[tuple[str, tuple[int, int, int, int], list]] = []
    for ln in _merge_ocr(raw):
        box = _box_of(ln)
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        text = _clean(getattr(ln, "text", ""))
        if not text:
            continue
        abs_box = (box[0] + l, box[1] + t, box[2] + l, box[3] + t)
        lines.append((text, abs_box, list(getattr(ln, "words", None) or [])))
    if not lines:
        return res

    header: Optional[tuple[str, tuple[int, int, int, int], list]] = None
    for item in lines:
        text = item[0]
        if "通讯录管理" in text:
            res.ok = True
        # 「联系人 N」表头：OCR 可能识别成 "联系人"、"V联系人"、"〈联系人 4" 等
        if "联系人" in text and header is None:
            res.ok = True
            header = item
    if not res.ok:
        return res

    if header is not None:
        res.header_box = header[1]
        res.total = _count_from_words(header[2], r - l)
        if res.total is None:
            res.total = _count_from_text(header[0])
        region_top = int(header[1][3]) + 2
    else:
        region_top = t + int(round(REGION_TOP_FALLBACK * s))
    res.region_top = region_top
    res.region_bottom = int(b - 2)

    name_min_x = l + int(round(NAME_COL_OFFSET * s))
    cand = [(text, box) for text, box, _w in lines
            if box[0] >= name_min_x and box[1] >= region_top and box[3] <= res.region_bottom]
    med_h = 0.0
    if cand:
        hs = sorted(box[3] - box[1] for _text, box in cand)
        med_h = float(hs[len(hs) // 2])
    for text, cy in [(text, (box[1] + box[3]) // 2) for text, box, _w in lines]:
        if region_top <= cy <= res.region_bottom:
            res.lines.append((text, cy))
    for text, box in cand:
        if _is_entry_or_heading(text):
            continue
        if med_h > 0 and (box[3] - box[1]) < 0.62 * med_h:
            continue
        res.names.append(text)
        res.name_centers.append((box[1] + box[3]) // 2)
    return res


def _read_total(img: Image.Image, lay, scan: _Screen, ocr) -> Optional[int]:
    """表头整行 OCR 没读到数字时，单独放大表头那一行再试。

    实测（125% DPI）裁剪上下边界差 2px 就会让「4」被连读成「44」或被漏掉，
    所以最多试 2 个高度略有差异的变体，只有结果一致才采用。
    """
    if not scan.ok or scan.header_box[3] <= scan.header_box[1]:
        return None
    s = _scale(lay)
    x0, x1 = scan.panel[0], scan.panel[2]
    if x1 - x0 < 40:
        return None
    top, bottom = int(scan.header_box[1]), int(scan.header_box[3])
    variants = [
        (top - int(round(3 * s)), bottom + int(round(5 * s))),
        (top - int(round(5 * s)), bottom + int(round(7 * s))),
    ]
    found: list[int] = []
    for y0, y1 in variants:
        y0 = max(int(scan.panel[1]), y0)
        y1 = min(int(scan.panel[3]), y1)
        if y1 - y0 < int(8 * s):
            continue
        try:
            crop = img.crop((int(x0), int(y0), int(x1), int(y1)))
            crop = crop.resize((crop.width * 2, crop.height * 2), Image.Resampling.LANCZOS)
            lines = _merge_ocr(ocr.ocr(crop))
        except Exception:
            continue
        n: Optional[int] = None
        for ln in lines:
            n = _count_from_words(list(getattr(ln, "words", None) or []), (x1 - x0) * 2)
            if n is None:
                n = _count_from_text(getattr(ln, "text", ""))
            if n is not None:
                break
        if n is not None:
            found.append(n)
        if len(found) == 2:
            break
    if not found:
        return None
    if len(found) >= 2 and found[0] != found[1]:
        return None
    return found[0]


def _row_pitch(scan: _Screen) -> float:
    cs = sorted(scan.name_centers)
    if len(cs) < 2:
        return 0.0
    diffs = [b - a for a, b in zip(cs, cs[1:]) if 10 <= b - a <= 400]
    if not diffs:
        return 0.0
    return float(statistics.median(diffs))


def _list_center(scan: _Screen) -> tuple[int, int]:
    l, _t, r, _b = scan.panel
    x = (int(l) + int(r)) // 2
    top = scan.region_top
    bottom = scan.region_bottom if scan.region_bottom > top + 10 else top + 100
    y = (top + bottom) // 2
    return x, y


# ---------------------------------------------------------------- 动作

def _snap(win, lay, ocr) -> tuple[Optional[Image.Image], object, _Screen]:
    """截图 + 布局 + 解析。"""
    img = _capture(win)
    if img is None:
        return None, lay, _Screen()
    lay = _lay_for(img, lay, win)
    return img, lay, _scan(img, lay, ocr)


def _lay_for(img: Image.Image, lay, win):
    try:
        if lay is not None and tuple(getattr(lay, "image_size", ())) == tuple(img.size):
            return lay
    except Exception:
        pass
    try:
        return _analyze(img, win)
    except Exception:
        return lay


def _open_contacts_page(win, lay, ocr, log) -> tuple[Optional[Image.Image], object, Optional[_Screen]]:
    """点侧边栏「通讯录」图标并确认页面切换成功。"""
    img = _capture(win)
    if img is None:
        log("微信窗口截图失败（可能已最小化），无法打开通讯录", "warn")
        return None, lay, None
    lay = _lay_for(img, lay, win)
    for attempt in range(3):
        pt = _tab_point(img, lay, "contacts", attempt)
        log(f"点击侧边栏「通讯录」({pt[0]}, {pt[1]})")
        if not _click(win, pt[0], pt[1]):
            log("点击投递失败，重试", "warn")
        _sleep(0.55)
        img = _capture(win)
        if img is None:
            continue
        lay2 = _lay_for(img, lay, win)
        scan = _scan(img, lay2, ocr)
        if scan.ok:
            log("已打开通讯录页")
            return img, lay2, scan
        lay = lay2
    return None, lay, None


def _scroll_to_top(win, lay, ocr, scan: _Screen, log) -> tuple[Optional[Image.Image], object, _Screen]:
    """向上滚回列表顶部（靠像素签名判断是否已经到顶）。"""
    up = max(2, min(15, len(scan.names) + 2))
    prev_hash = None
    for _ in range(8):
        img = _capture(win)
        if img is None:
            break
        lay = _lay_for(img, lay, win)
        box = (scan.panel[0], scan.region_top, scan.panel[2], scan.region_bottom)
        h = _region_hash(img, box)
        if h and h == prev_hash:
            break
        prev_hash = h
        x, y = _list_center(scan)
        if not _scroll(win, x, y, up):
            log("滚轮未生效（微信未能切到前台），放弃回顶部", "warn")
            break
        _sleep(0.22)
    return _snap(win, lay, ocr)


def _scroll_step(win, lay, ocr, scan: _Screen, notches: int, log):
    """向下滚 ``notches`` 格并重新解析。"""
    x, y = _list_center(scan)
    if not _scroll(win, x, y, -abs(int(notches))):
        log("滚轮未生效（微信未能切到前台），停止翻页", "warn")
        return None, lay, None
    _sleep(0.30)
    img, lay, scan2 = _snap(win, lay, ocr)
    if img is None:
        log("滚动后截图失败", "warn")
        return None, lay, None
    return img, lay, scan2


def _measure_notches(win, lay, ocr, scan: _Screen, log):
    """向下滚 1 格测位移，算出每次滚动应该滚几格（保留 ROW_OVERLAP 行重叠）。"""
    s = _scale(lay)
    pitch = _row_pitch(scan)
    if pitch <= 0:
        try:
            pitch = float(lay.row_height)
        except Exception:
            pitch = 0.0
    if pitch <= 0:
        pitch = DEFAULT_ROW_H * s
    img2, lay2, scan2 = _scroll_step(win, lay, ocr, scan, 1, log)
    dy = 0.0
    if scan2 is not None and scan2.ok:
        after: dict[str, int] = {}
        for text, cy in scan2.lines:
            after.setdefault(text, cy)
        deltas = sorted(cy - after[text] for text, cy in scan.lines if text in after)
        if deltas:
            dy = float(deltas[len(deltas) // 2])
    if dy <= 2.0:
        dy = 3.0 * pitch          # 测不到位移时按 Qt 列表的常见值（1 格 3 行）兜底
    visible = max(1, len(scan.names) or len(scan.lines))
    step_rows = max(1, visible - ROW_OVERLAP)
    notches = int(round(step_rows * pitch / dy))
    notches = max(1, min(12, notches))
    while notches > 1 and visible - (notches * dy / pitch) < 2.0:
        notches -= 1
    log(f"滚动步长自适应：1 格约 {dy:.0f}px ≈ {dy / pitch:.1f} 行，"
        f"可见 {visible} 行，每次滚 {notches} 格")
    return img2, lay2, scan2, notches


def _back_to_chat(win, lay, ocr, log) -> bool:
    """点回侧边栏「聊天」图标，并尽量校验确实切回去了。"""
    img = _capture(win)
    if img is None:
        log("切回聊天页：截图失败（窗口可能已最小化）", "warn")
        return False
    lay = _lay_for(img, lay, win)
    pt = _tab_point(img, lay, "chat", 0)
    _click(win, pt[0], pt[1])
    _sleep(0.45)
    img2 = _capture(win)
    if img2 is None:
        return True
    lay2 = _lay_for(img2, lay, win)
    scan = _scan(img2, lay2, ocr)
    if not scan.ok:
        log("已切回聊天页")
        return True
    log("尝试切回聊天页后仍识别到通讯录页，请手动点一下「聊天」", "warn")
    return False


# ---------------------------------------------------------------- 主流程

def enumerate_friends(win, lay, ocr, log_cb=None, max_scrolls: int = 120,
                      time_budget: float = 90.0) -> list[str]:
    """打开通讯录，翻页枚举全部好友名，最后切回聊天页。

    参数
    ----
    win : ``win.WinInfo``
    lay : ``layout.Layout``（传 None 会自动分析；截图尺寸变化时也会自动重算）
    ocr : ``OCRWorker``（``ocr(PIL.Image) -> list[OcrLine]``）
    log_cb : ``log_cb(text, level="info")`` 进度回调，可省略
    max_scrolls : 最多翻滚页次数
    time_budget : 总耗时上限（秒）

    返回
    ----
    list[str]：通讯录顺序的好友名（去重）。失败返回已收集到的部分（可能为空）。
    """
    log = _logger(log_cb)
    start = _now()
    names: list[str] = []
    seen: set[str] = set()
    done_reason = "正常结束"
    prev_fg = _foreground()
    prev_cursor = _cursor()
    log(f"开始同步好友列表（上限 {time_budget:.0f}s / {max_scrolls} 次滚动）")

    def add(new_names) -> int:
        added = 0
        for name in new_names:
            name = (name or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
            added += 1
        return added

    try:
        if win is None or not getattr(win, "alive", True):
            log("微信窗口不可用（可能已关闭或最小化）", "warn")
            return []

        img, lay, scan = _snap(win, lay, ocr)
        if scan.ok:
            log("当前已在通讯录页")
        else:
            img, lay, scan = _open_contacts_page(win, lay, ocr, log)
            if scan is None:
                log("无法打开通讯录页，好友同步终止", "error")
                return []
        if scan.total is None:
            scan.total = _read_total(img, lay, scan, ocr)
        if scan.total is None:
            # 刚切到通讯录页的那一帧可能还在渲染，换一帧再读表头（只 OCR 表头小条）
            _sleep(0.35)
            fresh = _capture(win)
            if fresh is not None:
                scan.total = _read_total(fresh, lay, scan, ocr)
        if scan.total is not None and scan.total < len(scan.names):
            log(f"表头数量 {scan.total} 小于本屏可见好友数 {len(scan.names)}，"
                f"疑似识别错误，忽略数量校验", "warn")
            scan.total = None
        if scan.total is not None:
            log(f"通讯录表头显示联系人总数：{scan.total}")
        else:
            log("未能从表头读到联系人总数，仅按翻页结果收集", "warn")
        add(scan.names)
        log(f"首屏读到 {len(scan.names)} 位好友")

        pitch = _row_pitch(scan) or 0.0
        if pitch <= 0:
            try:
                pitch = float(lay.row_height)
            except Exception:
                pitch = 0.0
        if pitch <= 0:
            pitch = DEFAULT_ROW_H * _scale(lay)

        # 首屏就装下了全部联系人（表头数量 = 首屏可见数量）→ 不需要滚动，
        # 也就不需要抢前台/移动光标，最不打扰用户。
        exhausted = scan.total is not None and scan.total > 0 and scan.total == len(scan.names)
        notches = 1
        if exhausted:
            done_reason = f"首屏已包含全部 {scan.total} 位联系人，无需滚动"
            log(done_reason)
        else:
            img, lay, scan = _scroll_to_top(win, lay, ocr, scan, log)
            if img is None:
                log("回到列表顶部失败（截图失败），按当前画面继续", "warn")
            else:
                add(scan.names)
                pitch = _row_pitch(scan) or pitch
                log(f"已回到列表顶部，当前累计 {len(seen)} 位好友")
            prev_sig = scan.sig
            img2, lay2, scan2, notches = _measure_notches(win, lay, ocr, scan, log)
            if scan2 is not None and scan2.ok:
                add(scan2.names)
                if scan2.sig == prev_sig:
                    log("向下滚 1 格后列表内容没有变化，判定列表已到底（无需继续翻页）")
                    done_reason = "列表没有更多内容"
                    exhausted = True
                else:
                    lay, scan = lay2, scan2
            else:
                log("第一次滚动失败，终止翻页", "warn")
                done_reason = "滚轮不可用"
                exhausted = True

        scrolls = 0
        no_new = 0
        prev_sig = scan.sig
        while not exhausted:
            if _now() - start > time_budget:
                done_reason = f"超时保护（{time_budget:.0f}s）提前结束"
                log(done_reason, "warn")
                break
            if scrolls >= int(max_scrolls):
                done_reason = f"达到最大滚动次数 {max_scrolls}"
                log(done_reason, "warn")
                break
            if no_new >= 2:
                done_reason = "连续 2 次滚动没有新好友，判定已到列表底部"
                log(done_reason)
                break
            img, lay, scan2 = _scroll_step(win, lay, ocr, scan, notches, log)
            scrolls += 1
            if scan2 is None or not scan2.ok:
                no_new += 1
                if scan2 is not None and not scan2.ok:
                    log("滚动后未识别到通讯录列表（可能有弹窗或遮挡）", "warn")
                continue
            if scan2.sig == prev_sig:
                no_new += 1
                log("滚动后列表内容没有变化（可能已到底部，或窗口未抢到前台）", "warn")
                continue
            added = add(scan2.names)
            if scan.total:
                log(f"已同步 {len(seen)}/{scan.total}")
            else:
                log(f"已同步 {len(seen)} 位")
            no_new = 0 if added else no_new + 1
            prev_sig = scan2.sig
            scan = scan2
        if scan.total is not None and len(seen) != scan.total:
            log(f"数量校验不一致：表头 {scan.total} 位，实际读到 {len(seen)} 位", "warn")
    except Exception as exc:  # noqa: BLE001 - 任何异常都不能把微信卡在通讯录页
        done_reason = f"异常中断：{exc}"
        log(f"好友同步异常：{exc}", "error")
    finally:
        try:
            _back_to_chat(win, lay, ocr, log)
        except Exception as exc:  # noqa: BLE001
            log(f"切回聊天页失败：{exc}", "warn")
        try:
            _set_cursor(*prev_cursor)
        except Exception:
            pass
        try:
            _restore_foreground(prev_fg, win)
        except Exception as exc:  # noqa: BLE001
            log(f"恢复原前台窗口失败：{exc}", "warn")

    log(f"好友同步结束：共 {len(names)} 位好友，用时 {_now() - start:.1f}s（{done_reason}）")
    return names
