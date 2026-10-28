"""Win32 外部观察层：找窗口、截图、窗口级输入。

**红线**：本模块只使用公开的 Win32 窗口 API（EnumWindows / GetWindowRect / PrintWindow /
PostMessage / SendInput / GetCursorPos 等），不打开微信进程句柄读内存，不注入，不 Hook，
不修改微信任何文件。

坐标系约定（重要）：
* ``capture()`` 返回的画面 = 微信窗口矩形 (GetWindowRect) 的内容，坐标 (0,0) 对应窗口左上角。
* 本模块对外暴露的 ``click(x, y)`` / ``scroll(x, y)`` 里的 x,y 都使用**画面坐标**（即截图坐标）。
* 内部换算到 render 子窗口客户区坐标（PostMessage）或屏幕坐标（SendInput）。
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Optional

import psutil
from PIL import Image

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ---------------- 函数原型（不做设置的话 64 位下句柄会被截断） ----------------
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]
user32.EnumChildWindows.restype = wintypes.BOOL
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClientRect.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ClientToScreen.restype = wintypes.BOOL
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ScreenToClient.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowDC.argtypes = [wintypes.HWND]
user32.GetWindowDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = wintypes.LPARAM
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetWindowPlacement.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.GetWindowPlacement.restype = wintypes.BOOL
user32.GetDpiForWindow.argtypes = [wintypes.HWND]
user32.GetDpiForWindow.restype = wintypes.UINT
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                            ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]

MI_OUTPUT_DX = 1
CF_UNICODETEXT = 13
SMTO_ABORTIFHUNG = 0x0002

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_MOUSEWHEEL = 0x020A
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
MK_LBUTTON = 0x0001
VK_RETURN = 0x0D
VK_ESCAPE = 0x1B

WECHAT_EXES = {"weixin.exe", "wechat.exe"}
WECHAT_TITLES = ("微信", "WeChat")
MAIN_WINDOW_CLASS_HINTS = ("QWindowIcon", "WeChatMainWndForPC", "Qt515")

# PrintWindow 标志：2 = PW_RENDERFULLCONTENT，能抓到被遮挡/使用硬件渲染的内容
PW_RENDERFULLCONTENT = 0x00000002


class WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT), ("showCmd", wintypes.UINT),
                ("ptMinPosition", wintypes.POINT), ("ptMaxPosition", wintypes.POINT),
                ("rcNormalPosition", wintypes.RECT)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


def _is_dpi_aware() -> bool:
    try:
        return bool(user32.GetThreadDpiAwarenessContext)  # noqa: B009 - 仅作存在性判断
    except Exception:
        return False


_DPI_READY = False


def ensure_dpi_aware() -> None:
    """必须在任何坐标/截图操作前调用一次，否则高 DPI 下坐标会缩水。"""
    global _DPI_READY
    if _DPI_READY:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE_V2
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    _DPI_READY = True


@dataclass
class WinInfo:
    """微信主窗口信息。"""

    hwnd: int
    pid: int
    exe: str
    title: str
    cls: str
    window_rect: tuple[int, int, int, int]      # 屏幕坐标 (l, t, r, b)
    client_rect: tuple[int, int, int, int]      # 屏幕坐标
    render_hwnd: int = 0                        # 自绘子窗口 MMUIRenderSubWindowHW
    dpi: int = 96
    _render_origin: tuple[int, int] = (0, 0)    # render 客户区原点对应画面坐标

    # ---- 便捷属性 ----
    @property
    def width(self) -> int:
        return self.window_rect[2] - self.window_rect[0]

    @property
    def height(self) -> int:
        return self.window_rect[3] - self.window_rect[1]

    @property
    def alive(self) -> bool:
        return bool(self.hwnd) and bool(user32.IsWindow(self.hwnd))

    @property
    def minimized(self) -> bool:
        return bool(user32.IsIconic(self.hwnd))

    def screen_to_image(self, x: int, y: int) -> tuple[int, int]:
        """屏幕坐标 -> 画面坐标。"""
        return x - self.window_rect[0], y - self.window_rect[1]

    def image_to_screen(self, x: int, y: int) -> tuple[int, int]:
        """画面坐标 -> 屏幕坐标。"""
        return self.window_rect[0] + x, self.window_rect[1] + y


def _exe_of(pid: int) -> str:
    try:
        return psutil.Process(pid).name().lower()
    except Exception:
        return ""


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _window_title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(hwnd, buf, n + 2)
    return buf.value


def _rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _client_rect_screen(hwnd: int) -> tuple[int, int, int, int]:
    rc = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, pt.x + rc.right, pt.y + rc.bottom


def _top_level_windows() -> list[int]:
    out: list[int] = []

    @WNDENUMPROC
    def cb(hwnd, _lparam):
        out.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return out


def find_render_child(hwnd: int) -> int:
    """找微信的自绘子窗口（内容全在这里面画）。"""
    children: list[int] = []

    @WNDENUMPROC
    def cb(h, _l):
        children.append(h)
        return True

    user32.EnumChildWindows(hwnd, cb, 0)
    best = 0
    best_area = 0
    for c in children:
        cls = _class_name(c)
        l, t, r, b = _rect(c)
        area = max(0, r - l) * max(0, b - t)
        if "Render" in cls or "Qt" in cls:
            if area > best_area:
                best, best_area = c, area
    return best


def _restored_rect(hwnd: int) -> tuple[int, int, int, int]:
    """最小化窗口的"正常显示时"矩形（GetWindowPlacement）。"""
    wp = WINDOWPLACEMENT()
    wp.length = ctypes.sizeof(WINDOWPLACEMENT)
    try:
        if user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
            r = wp.rcNormalPosition
            if r.right - r.left > 100 and r.bottom - r.top > 100:
                return (r.left, r.top, r.right, r.bottom)
    except Exception:
        pass
    return _rect(hwnd)


# 不是主窗口的微信窗口标题关键词（聊天记录/独立聊天窗口等）
_NOT_MAIN_TITLE_HINTS = ("聊天记录", "的聊天", "小程序", "视频号", "公众号文章", "图片查看", "文件预览")
_MAIN_TITLES = ("微信", "WeChat", "Weixin")


def find_wechat_window() -> Optional[WinInfo]:
    """查找微信电脑版**主窗口**；找不到返回 None。

    微信 4.x 可能同时存在多个 Qt 顶层窗口（例如「与'某某'的聊天记录」独立窗口），
    这里按"标题恰好是 微信/WeChat/Weixin"优先 + 排除聊天记录窗口 + 面积大者优先来选，
    避免把聊天记录窗口误当主窗口。
    """
    ensure_dpi_aware()
    candidates: list[tuple[int, WinInfo]] = []
    for hwnd in _top_level_windows():
        try:
            if not user32.IsWindowVisible(hwnd):
                continue
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            exe = _exe_of(pid.value)
            if exe not in WECHAT_EXES:
                continue
            cls = _class_name(hwnd)
            title = _window_title(hwnd)
            iconic = bool(user32.IsIconic(hwnd))
            rect = _restored_rect(hwnd) if iconic else _rect(hwnd)
            l, t, r, b = rect
            score = 0
            if title.strip() in _MAIN_TITLES:
                score += 1000
            if any(h in title for h in _NOT_MAIN_TITLE_HINTS):
                score -= 2000
            if iconic:
                score -= 50
            if r - l >= 900 and b - t >= 600:
                score += 100
            score += min((r - l) * (b - t) // 20000, 80)
            if score < 0:
                continue
            if r - l < 200 or b - t < 200:
                continue
            win = WinInfo(
                hwnd=int(hwnd), pid=int(pid.value), exe=exe, title=title, cls=cls,
                window_rect=rect, client_rect=_client_rect_screen(hwnd) if not iconic else rect,
                dpi=int(user32.GetDpiForWindow(hwnd) or 96),
            )
            win.render_hwnd = find_render_child(hwnd)
            if win.render_hwnd and not iconic:
                rl, rt, _, _ = _client_rect_screen(win.render_hwnd)
                win._render_origin = (rl - win.window_rect[0], rt - win.window_rect[1])
            candidates.append((score, win))
        except Exception:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1].width * item[1].height), reverse=True)
    return candidates[0][1]


# ---------------- 截图 ----------------
def _grab_window(hwnd: int, width: int, height: int, flag: int = PW_RENDERFULLCONTENT) -> Optional[Image.Image]:
    hdc = user32.GetWindowDC(hwnd)
    if not hdc:
        return None
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, width, height)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        ok = user32.PrintWindow(hwnd, mdc, flag)
        if not ok:
            return None
        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = width
        bmi.biHeight = -height
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0
        buf = ctypes.create_string_buffer(width * height * 4)
        got = gdi32.GetDIBits(mdc, bmp, 0, height, buf, ctypes.byref(bmi), 0)
        if got != height:
            return None
        return Image.frombuffer("RGBA", (width, height), buf, "raw", "BGRA", 0, 1).convert("RGB")
    finally:
        try:
            gdi32.SelectObject(mdc, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mdc)
        finally:
            user32.ReleaseDC(hwnd, hdc)


def capture(win: WinInfo, verify: bool = True) -> Optional[Image.Image]:
    """截取微信窗口画面。

    返回与窗口同尺寸的 PIL Image（画面坐标原点=窗口左上角）。
    窗口最小化、PrintWindow 失败、或画面全黑/全白（verify=True）时返回 None。
    """
    ensure_dpi_aware()
    if not win or not win.alive:
        return None
    if win.minimized:
        return None
    width, height = win.width, win.height
    if width <= 0 or height <= 0:
        return None
    img = _grab_window(win.hwnd, width, height, PW_RENDERFULLCONTENT)
    if img is None:
        img = _grab_window(win.hwnd, width, height, 0)
    if img is None:
        return None
    if verify:
        extrema = img.convert("L").getextrema()
        if extrema[1] - extrema[0] < 6:  # 纯色画面，视为失败
            return None
    return img


def capture_region(win: WinInfo, box: tuple[int, int, int, int], verify: bool = True) -> Optional[Image.Image]:
    """按画面坐标截取局部。"""
    img = capture(win, verify=verify)
    if img is None:
        return None
    return img.crop(box)


def bring_to_front(win: WinInfo) -> bool:
    """把微信窗口切到前台（需要时才用，避免打扰用户）。"""
    if not win or not win.alive:
        return False
    try:
        # 用 ShowWindow(SW_SHOW) 而不是 maximize，保持窗口大小
        user32.ShowWindow(win.hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(win.hwnd)
        return True
    except Exception:
        return False


# ---------------- 输入：PostMessage 优先，SendInput 兜底 ----------------
def _make_lparam(x: int, y: int) -> int:
    return (int(y) << 16) | (int(x) & 0xFFFF)


def _main_client_point(win: WinInfo, x: int, y: int) -> tuple[int, int]:
    """画面坐标 -> 主窗口客户区坐标。

    实测（微信 4.1.15.56）：PostMessage 必须投给**主窗口 HWND** 并用主窗口客户区坐标，
    投给自绘子窗口或主窗口但用子窗口坐标都无效；投给主窗口不会抢焦点。
    """
    ox = win.client_rect[0] - win.window_rect[0]
    oy = win.client_rect[1] - win.window_rect[1]
    return int(x - ox), int(y - oy)


def post_click(win: WinInfo, x: int, y: int, double: bool = False, target: str = "main") -> bool:
    """向微信主窗口投递点击（不抢焦点、不动真实鼠标）。x,y 为画面坐标。

    返回 True 只代表消息已投递，调用方仍需校验界面是否真的响应。
    """
    if not win or not win.alive:
        return False
    hwnd = win.hwnd if target == "main" else (win.render_hwnd or win.hwnd)
    if target == "main":
        cx, cy = _main_client_point(win, x, y)
    else:
        cx, cy = _main_client_point(win, x, y)
    lp = _make_lparam(cx, cy)
    try:
        user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp)
        time.sleep(0.01)
        user32.PostMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lp)
        time.sleep(0.03)
        user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)
        if double:
            time.sleep(0.03)
            user32.PostMessageW(hwnd, WM_LBUTTONDBLCLK, MK_LBUTTON, lp)
            time.sleep(0.03)
            user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, lp)
        return True
    except Exception:
        return False


def post_scroll(win: WinInfo, x: int, y: int, delta: int) -> bool:
    """向微信主窗口投递滚轮事件（delta>0 向上滚）。x,y 为画面坐标。"""
    if not win or not win.alive:
        return False
    hwnd = win.hwnd
    cx, cy = _main_client_point(win, x, y)
    wparam = ((int(delta) & 0xFFFF) << 16)
    lp = _make_lparam(cx, cy)
    try:
        user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, lp)
        time.sleep(0.01)
        user32.PostMessageW(hwnd, WM_MOUSEWHEEL, wparam, lp)
        return True
    except Exception:
        return False


def real_click(win: WinInfo, x: int, y: int, double: bool = False) -> bool:
    """真实鼠标点击（会移动用户鼠标、需要窗口在前台）。x,y 为画面坐标。"""
    if not win or not win.alive:
        return False
    sx, sy = win.image_to_screen(x, y)
    try:
        user32.SetCursorPos(sx, sy)
        time.sleep(0.03)
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
        time.sleep(0.03)
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
        if double:
            time.sleep(0.05)
            user32.mouse_event(0x0002, 0, 0, 0, 0)
            time.sleep(0.03)
            user32.mouse_event(0x0004, 0, 0, 0, 0)
        return True
    except Exception:
        return False


# SendInput 结构体（用于输入中文等任意 Unicode 字符，绕过输入法）
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004


def _send_unicode_char(ch: str) -> None:
    code = ord(ch)
    down = INPUT(type=1, union=INPUT_UNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, None)))
    up = INPUT(type=1, union=INPUT_UNION(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, None)))
    user32.SendInput(1, ctypes.byref(down), ctypes.sizeof(INPUT))
    user32.SendInput(1, ctypes.byref(up), ctypes.sizeof(INPUT))


def type_text(text: str, interval: float = 0.01) -> None:
    """用 SendInput 的 Unicode 模式输入文本（发送到当前前台窗口，不受输入法影响）。"""
    for ch in text:
        _send_unicode_char(ch)
        time.sleep(interval)


def press_key(vk: int) -> None:
    """按下并抬起一个虚拟键（发送到当前前台窗口）。"""
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.02)
    user32.keybd_event(vk, 0, 2, 0)


def force_foreground(hwnd: int) -> bool:
    """可靠地把指定窗口切到前台（AttachThreadInput + ALT 解锁兜底）。

    用于"必须发送真实键盘事件"的场景（例如让微信图片查看器接收 Ctrl+S）。
    """
    if not hwnd or not user32.IsWindow(hwnd):
        return False
    if user32.GetForegroundWindow() == hwnd:
        return True
    fg = user32.GetForegroundWindow()
    cur_tid = kernel32.GetCurrentThreadId()
    fg_tid = 0
    try:
        fg_tid = user32.GetWindowThreadProcessId(fg, None)
        user32.AttachThreadInput(cur_tid, fg_tid, True)
    except Exception:
        pass
    try:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    finally:
        if fg_tid:
            try:
                user32.AttachThreadInput(cur_tid, fg_tid, False)
            except Exception:
                pass
    if user32.GetForegroundWindow() == hwnd:
        return True
    # 系统前台锁：敲一下 ALT 再试
    try:
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        time.sleep(0.05)
        user32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    return user32.GetForegroundWindow() == hwnd


def is_foreground(win: WinInfo) -> bool:
    try:
        return user32.GetForegroundWindow() == win.hwnd
    except Exception:
        return False


def activate(win: WinInfo) -> bool:
    """可靠地把微信窗口切到前台（含 AttachThreadInput 兜底）。"""
    if not win or not win.alive:
        return False
    try:
        user32.ShowWindow(win.hwnd, 9)
    except Exception:
        pass
    try:
        user32.SetForegroundWindow(win.hwnd)
    except Exception:
        pass
    if is_foreground(win):
        return True
    try:
        fg = user32.GetForegroundWindow()
        cur_tid = kernel32.GetCurrentThreadId()
        fg_tid = user32.GetWindowThreadProcessId(fg, None)
        user32.AttachThreadInput(cur_tid, fg_tid, True)
        user32.SetForegroundWindow(win.hwnd)
        user32.AttachThreadInput(cur_tid, fg_tid, False)
    except Exception:
        pass
    return is_foreground(win)


def image_hash(img: Image.Image) -> str:
    """画面内容哈希（用于快速判断画面是否变化）。"""
    import hashlib

    small = img.convert("L").resize((64, 40))
    return hashlib.sha1(small.tobytes()).hexdigest()[:16]


def restore_no_activate(win: WinInfo) -> bool:
    """把最小化的微信窗口恢复出来，尽量不抢焦点。

    优先 SW_SHOWNOACTIVATE；如果窗口仍最小化，只能用 SW_RESTORE（可能短暂抢前台）。
    """
    if not win or not win.alive:
        return False
    if not user32.IsIconic(win.hwnd):
        return True
    try:
        user32.ShowWindow(win.hwnd, 4)  # SW_SHOWNOACTIVATE
    except Exception:
        pass
    time.sleep(0.15)
    if not user32.IsIconic(win.hwnd):
        return True
    try:
        user32.ShowWindow(win.hwnd, 9)  # SW_RESTORE（兜底）
    except Exception:
        pass
    time.sleep(0.2)
    return not bool(user32.IsIconic(win.hwnd))
