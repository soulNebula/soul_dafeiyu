"""界面布局自动识别（Lead 维护）。

只依赖截图做颜色/几何分析，不写死绝对坐标：微信换主题、改 DPI、调窗口大小后仍可用。
坐标系：全部是 **capture() 返回图像的像素坐标**（原点 = 窗口左上角）。
``Layout.logical()`` 可把像素换算成逻辑像素（除以 scale），便于复用同一套常量。

实现要点（性能）：整帧分析必须在 ~100ms 内完成，所以所有逐行/逐列循环都改成 numpy 向量化，
并对大区域做抽样。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

Box = tuple[int, int, int, int]  # (left, top, right, bottom)，右/下为开区间

# 兜底主题色（深色 / 浅色）
DARK_CHAT_BG = (30, 30, 31)
DARK_LIST_BG = (47, 47, 48)
DARK_SIDEBAR_BG = (45, 45, 46)
DARK_INCOMING = (47, 47, 48)
DARK_OUTGOING = (53, 210, 141)
LIGHT_CHAT_BG = (245, 245, 245)
LIGHT_LIST_BG = (250, 250, 250)
LIGHT_SIDEBAR_BG = (46, 46, 46)
LIGHT_INCOMING = (255, 255, 255)

# 逻辑像素常量（微信 4.x @100% DPI 实测）
LOGICAL_SIDEBAR_W = 60
LOGICAL_ROW_H = 57.6
LOGICAL_HEADER_H = 79.2
LOGICAL_SEARCH_TOP = 42


def _lum(c) -> float:
    return float(c[0]) * 0.299 + float(c[1]) * 0.587 + float(c[2]) * 0.114


def _is_gray(c, tol: int = 3) -> bool:
    return max(abs(int(c[0]) - int(c[1])), abs(int(c[1]) - int(c[2])), abs(int(c[0]) - int(c[2]))) <= tol


def _color_close(c1, c2, tol: int) -> bool:
    return max(abs(int(c1[0]) - int(c2[0])), abs(int(c1[1]) - int(c2[1])), abs(int(c1[2]) - int(c2[2]))) <= tol


def _mode_color(a: np.ndarray, max_px: int = 20000) -> tuple[int, int, int]:
    """区域出现次数最多的颜色（大区域自动抽样，保证 <10ms）。"""
    flat = np.asarray(a).reshape(-1, 3)
    if flat.shape[0] > max_px:
        flat = flat[:: max(1, flat.shape[0] // max_px)]
    colors, counts = np.unique(flat, axis=0, return_counts=True)
    return tuple(int(v) for v in colors[int(counts.argmax())])


def _runs(flags, min_len: int = 1) -> list[tuple[int, int]]:
    """把 bool 序列压缩成 True 连续段 [(start, end_exclusive), ...]。"""
    s = np.asarray(flags, dtype=bool)
    if s.size == 0:
        return []
    d = np.diff(np.concatenate(([0], s.view(np.int8), [0])))
    starts = np.flatnonzero(d == 1)
    ends = np.flatnonzero(d == -1)
    return [(int(a), int(b)) for a, b in zip(starts, ends) if b - a >= min_len]


def _detect_content_box(img: Image.Image) -> Box:
    """去掉窗口圆角/阴影边框，返回客户区在画面里的坐标。"""
    a = np.asarray(img.convert("RGB")).astype(np.int16)
    h, w, _ = a.shape
    frame = a[0, 0]
    row_close = (np.abs(a - frame).max(axis=2) <= 2).mean(axis=1) > 0.95
    col_close = (np.abs(a - frame).max(axis=2) <= 2).mean(axis=0) > 0.95
    top = 0
    while top < min(30, h // 4) and row_close[top]:
        top += 1
    bottom = h - 1
    while bottom > h - min(30, h // 4) and row_close[bottom]:
        bottom -= 1
    left = 0
    while left < min(30, w // 4) and col_close[left]:
        left += 1
    right = w - 1
    while right > w - min(30, w // 4) and col_close[right]:
        right -= 1
    if top >= bottom - 100 or left >= right - 200:
        return (0, 0, w, h)
    return (left, top, right + 1, bottom + 1)


@dataclass
class Layout:
    """一帧微信主窗口的布局信息（像素坐标）。"""

    image_size: tuple[int, int]
    content: Box
    sidebar: Box
    conv_list: Box
    chat: Box
    header: Box
    message_area: Box
    input_area: Box
    search_box: Box
    list_rows_region: Box
    row_height: int
    scale: float
    dpi: int
    chat_bg: tuple[int, int, int]
    list_bg: tuple[int, int, int]
    sidebar_bg: tuple[int, int, int]
    incoming_bubble: tuple[int, int, int]
    outgoing_bubble: tuple[int, int, int]
    dark_theme: bool = True
    warnings: list[str] = field(default_factory=list)

    def logical(self, v: float) -> float:
        return v / self.scale

    def physical(self, v: float) -> int:
        return int(round(v * self.scale))

    def crop(self, img: Image.Image, box: Box) -> Image.Image:
        return img.crop(box)

    def to_dict(self) -> dict:
        return {
            "image_size": self.image_size,
            "content": self.content,
            "sidebar": self.sidebar,
            "conv_list": self.conv_list,
            "chat": self.chat,
            "header": self.header,
            "message_area": self.message_area,
            "input_area": self.input_area,
            "search_box": self.search_box,
            "row_height": self.row_height,
            "scale": self.scale,
            "dpi": self.dpi,
            "chat_bg": self.chat_bg,
            "list_bg": self.list_bg,
            "sidebar_bg": self.sidebar_bg,
            "incoming_bubble": self.incoming_bubble,
            "outgoing_bubble": self.outgoing_bubble,
            "dark_theme": self.dark_theme,
            "warnings": self.warnings,
        }


def _column_colors(a: np.ndarray, x0: int, x1: int, y0: int, y1: int) -> np.ndarray:
    """每列的"代表色"：抽样行的中位数颜色，返回 (n_cols, 3) int16。"""
    step = max(1, (y1 - y0) // 120)
    sub = a[y0:y1:step, x0:x1, :].astype(np.float32)
    med = np.median(sub, axis=0)
    return med.astype(np.int16)


def _column_close_frac(a: np.ndarray, x0: int, x1: int, y0: int, y1: int,
                      color: tuple[int, int, int]) -> np.ndarray:
    """每列中"接近指定颜色"的像素比例。"""
    step = max(1, (y1 - y0) // 120)
    sub = a[y0:y1:step, x0:x1, :].astype(np.int16)
    return (np.abs(sub - np.array(color, dtype=np.int16)).max(axis=2) <= 6).mean(axis=0)


def _detect_panels(a: np.ndarray, content: Box, scale: float) -> tuple[int, int, list[str]]:
    """返回 (sidebar_right, chat_left, warnings) 像素 x 坐标。"""
    x0, y0, x1, y1 = content
    warnings: list[str] = []
    y_top = y0 + int((y1 - y0) * 0.15)
    y_bot = y1 - int((y1 - y0) * 0.15)
    med = _column_colors(a, x0, x1, y_top, y_bot)  # (n,3)
    # 聊天区判定：该列绝大多数像素等于聊天底色（图片/气泡会打断，取最长连续段）
    chat_bg_guess = _mode_color(a[y_bot - 60:y_bot - 5, x1 - 200:x1 - 20, :])
    col_close = (np.abs(med - np.array(chat_bg_guess, dtype=np.int16)).max(axis=1) <= 6)
    col_close_frac = _column_close_frac(a, x0, x1, y_top, y_bot, chat_bg_guess)
    cand = _runs(col_close_frac > 0.28, min_len=int(80 * scale))
    if cand:
        chat_left = x0 + cand[-1][0]
    elif _runs(col_close, min_len=int((x1 - x0) * 0.2)):
        chat_left = x0 + _runs(col_close, min_len=int((x1 - x0) * 0.2))[-1][0]
    else:
        chat_left = x0 + int((x1 - x0) * 0.34)
        warnings.append("未找到聊天区左边界，使用默认比例")

    # 图标栏右边界：从最左开始，代表色第一次明显变化处
    packed = (med[:, 0].astype(np.int32) << 16) | (med[:, 1].astype(np.int32) << 8) | med[:, 2].astype(np.int32)
    same = np.concatenate(([True], packed[1:] == packed[:-1]))
    runs = _runs(same, min_len=1)
    sidebar_right = x0
    for s, e in runs:
        if s > 0 and (e - s) >= 10:
            sidebar_right = x0 + s
            break
    if sidebar_right <= x0 + 20 or sidebar_right > chat_left - 60:
        sidebar_right = x0 + int(round(LOGICAL_SIDEBAR_W * scale))
        warnings.append("未找到图标栏边界，使用默认宽度")
    return sidebar_right, chat_left, warnings


def _row_bg_close_frac(a: np.ndarray, x0: int, x1: int, y0: int, y1: int,
                       bg: tuple[int, int, int]) -> tuple[np.ndarray, np.ndarray]:
    """逐行统计：与底色接近的比例、浅灰全宽线的比例。"""
    seg = a[y0:y1, x0:x1, :].astype(np.int16)
    bg_arr = np.array(bg, dtype=np.int16)
    diff = np.abs(seg - bg_arr).max(axis=2)
    gray = (np.abs(seg[:, :, 0] - seg[:, :, 1]) <= 2) & (np.abs(seg[:, :, 1] - seg[:, :, 2]) <= 2)
    dark = _lum(bg) < 128
    lo, hi = (5, 70) if dark else (3, 70)
    line = (diff >= lo) & (diff <= hi) & gray
    return (diff <= 5).mean(axis=1), line.mean(axis=1)


def _detect_lines(a: np.ndarray, x0: int, x1: int, y0: int, y1: int,
                 bg: tuple[int, int, int]) -> list[tuple[int, int]]:
    """找横贯全宽的**细**灰线（标题栏下边线 / 输入框上边线），返回 [(start,end), ...]。"""
    if y1 - y0 < 10:
        return []
    close_frac, line_frac = _row_bg_close_frac(a, x0, x1, y0, y1, bg)
    runs = _runs(line_frac > 0.75, min_len=1)
    out = []
    for s, e in runs:
        if e - s > 4:
            continue
        if s > y1 - y0 - 8:  # 贴着扫描区底边的不算（可能是底部工具条）
            continue
        # 上下文校验：线上方 6px / 下方 5px 应该是聊天底色
        ok = True
        for yy in ((s + y0 - 6) - y0, (e + y0 + 4) - y0):
            if 0 <= yy < len(close_frac) and close_frac[yy] < 0.55:
                ok = False
                break
        if ok:
            out.append((s + y0, e + y0))
    return out


def _detect_bubble_colors(a: np.ndarray, msg: Box, chat_bg: tuple[int, int, int],
                          dark_theme: bool) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """返回 (对方气泡色, 自己气泡色)。"""
    x0, y0, x1, y1 = msg
    step = max(1, (x1 - x0) // 220)
    rstep = max(1, (y1 - y0) // 160)
    sample = a[y0:y1:rstep, x0:x1:step, :]
    if sample.size == 0:
        return (DARK_INCOMING if dark_theme else LIGHT_INCOMING), DARK_OUTGOING
    flat = sample.reshape(-1, 3)
    colors, counts = np.unique(flat, axis=0, return_counts=True)
    order = np.argsort(counts)[::-1]
    total = max(1, flat.shape[0])
    incoming = DARK_INCOMING if dark_theme else LIGHT_INCOMING
    outgoing = DARK_OUTGOING
    found_in = found_out = False
    for idx in order[:40]:
        c = tuple(int(v) for v in colors[idx])
        frac = float(counts[idx]) / total
        if _color_close(c, chat_bg, 4):
            continue
        if int(c[1]) - max(int(c[0]), int(c[2])) > 25 and frac > 0.002:
            if not found_out:
                outgoing = c
                found_out = True
            continue
        if frac > 0.004 and _is_gray(c, 6) and not found_in:
            incoming = c
            found_in = True
        if found_in and found_out:
            break
    return incoming, outgoing


def _detect_row_height(a: np.ndarray, list_box: Box, list_bg: tuple[int, int, int], scale: float) -> int:
    """通过左侧头像块的垂直节距推算会话行高。"""
    x0, y0, x1, y1 = list_box
    if y1 - y0 < 100:
        return int(round(LOGICAL_ROW_H * scale))
    avatar_strip = a[y0:y1, x0 + 8:x0 + 56, :]
    diff = np.abs(avatar_strip - np.array(list_bg, dtype=np.int16)).max(axis=2)
    rowmask = (diff > 22).sum(axis=1) > 6
    runs = _runs(rowmask, min_len=2)
    centers = [(s + e) // 2 for s, e in runs if 22 * scale <= (e - s) <= 62 * scale]
    if len(centers) < 2:
        return int(round(LOGICAL_ROW_H * scale))
    diffs = [b - a2 for a2, b in zip(centers, centers[1:]) if 40 * scale < b - a2 < 130 * scale]
    if not diffs:
        return int(round(LOGICAL_ROW_H * scale))
    return int(statistics.median(diffs))


def analyze(img: Image.Image, win=None, dpi: int | None = None,
            content: Box | None = None) -> Layout:
    """分析一帧画面，得到布局。

    ``win`` 为可选的 win.WinInfo，用于精确求客户区；没有 win 时可用 ``content`` 直接指定
    客户区（测试/离线分析用），最后才退化为按像素猜边框。
    """
    rgb = img.convert("RGB")
    a = np.asarray(rgb).astype(np.int16)
    h, w, _ = a.shape

    if content is not None:
        pass
    elif win is not None and getattr(win, "window_rect", None) and getattr(win, "client_rect", None):
        ox = win.client_rect[0] - win.window_rect[0]
        oy = win.client_rect[1] - win.window_rect[1]
        ow = win.client_rect[2] - win.client_rect[0]
        oh = win.client_rect[3] - win.client_rect[1]
        if ox >= 0 and oy >= 0 and ox + ow <= w and oy + oh <= h and ow > 300 and oh > 300:
            content = (ox, oy, ox + ow, oy + oh)
        else:
            content = _detect_content_box(rgb)
    else:
        content = _detect_content_box(rgb)

    dpi_value = dpi or (getattr(win, "dpi", None) or 96)
    scale = dpi_value / 96.0
    cx0, cy0, cx1, cy1 = content
    warnings: list[str] = []

    # 主题色
    sample = a[cy0 + int((cy1 - cy0) * 0.65):cy1 - 12, cx1 - 220:cx1 - 30, :]
    if sample.size == 0:
        sample = a[cy0:cy1, cx0:cx1, :]
    chat_bg = _mode_color(sample)
    dark_theme = _lum(chat_bg) < 128
    list_sample = a[cy0 + int((cy1 - cy0) * 0.5):cy1 - int((cy1 - cy0) * 0.35), cx0 + 100:cx0 + 220, :]
    if list_sample.size == 0:
        list_sample = a[cy0:cy1, cx0:cx1, :]
    list_bg = _mode_color(list_sample)
    sidebar_sample = a[cy0 + 100:cy1 - 150, cx0 + 5:cx0 + 45, :]
    if sidebar_sample.size == 0:
        sidebar_sample = a[cy0:cy1, cx0:cx0 + 45, :]
    sidebar_bg = _mode_color(sidebar_sample)

    sidebar_right, chat_left, panel_warnings = _detect_panels(a, content, scale)
    warnings.extend(panel_warnings)

    # 标题栏下边线 + 输入框上边线
    header_lines = _detect_lines(a, chat_left + 10, cx1 - 10, cy0 + 10,
                                cy0 + int((cy1 - cy0) * 0.22), chat_bg)
    if header_lines:
        header_sep = header_lines[-1][1]
    else:
        header_sep = cy0 + int(round(LOGICAL_HEADER_H * scale))
        warnings.append("未找到标题栏分隔线，使用默认高度")

    input_lines = _detect_lines(a, chat_left + 10, cx1 - 10,
                               cy0 + int((cy1 - cy0) * 0.55), cy1 - 4, chat_bg)
    input_top = None
    # 输入框是一个圆角矩形：上边框与下边框成对出现，间距 = 输入框高度
    if len(input_lines) >= 2:
        last_e = input_lines[-1][1]
        for s, e in reversed(input_lines[:-1]):
            gap = last_e - s
            if 60 * scale <= gap <= 260 * scale:
                input_top = s
                break
    if input_top is None:
        cand = [s for s, e in input_lines if s > header_sep + 50]
        input_top = cand[-1] if cand else cy1 - int(round(170 * scale))
        if not cand:
            warnings.append("未找到输入框顶边，使用默认位置")

    header = (chat_left, cy0, cx1, header_sep)
    input_area = (chat_left, input_top, cx1, cy1)
    message_area = (chat_left, header_sep + 1, cx1 - 1, input_top - 1)
    search_box = (sidebar_right + int(round(10 * scale)), cy0 + int(round(LOGICAL_SEARCH_TOP * scale)),
                  chat_left - int(round(48 * scale)), cy0 + int(round((LOGICAL_SEARCH_TOP + 26) * scale)))

    msg_sample_box = (message_area[0] + 5, message_area[1] + 5, message_area[2] - 10, message_area[3] - 5)
    incoming, outgoing = _detect_bubble_colors(a, msg_sample_box, chat_bg, dark_theme)

    list_rows_region = (sidebar_right, cy0 + int(round(90 * scale)), chat_left - 1, cy1 - 1)
    row_height = _detect_row_height(a, list_rows_region, list_bg, scale)

    return Layout(
        image_size=(w, h),
        content=content,
        sidebar=(cx0, cy0, sidebar_right, cy1),
        conv_list=(sidebar_right, cy0, chat_left, cy1),
        chat=(chat_left, cy0, cx1, cy1),
        header=header,
        message_area=message_area,
        input_area=input_area,
        search_box=search_box,
        list_rows_region=list_rows_region,
        row_height=row_height,
        scale=scale,
        dpi=int(round(scale * 96)),
        chat_bg=chat_bg,
        list_bg=list_bg,
        sidebar_bg=sidebar_bg,
        incoming_bubble=incoming,
        outgoing_bubble=outgoing,
        dark_theme=dark_theme,
        warnings=warnings,
    )
