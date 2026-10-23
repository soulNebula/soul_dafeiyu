"""wx_core/friends.py 的离线单测（不需要真实微信 / 真实 OCR）。

思路：把「截图 / 点击 / 滚轮 / OCR / sleep」全部换成内存里的假实现：
* ``_capture`` 返回一张假截图 ``_Shot``，它只记录「当前是聊天页还是通讯录页」和「滚动偏移」；
* ``_scroll`` 改偏移（模拟真实滚轮）；
* 假 OCR 根据偏移生成通讯录列表的 OCR 行（坐标模仿真实通讯录页，含表头、字母分组标题、
  入口项和上下被裁切的半行）；
* 其余解析/翻页/去重/过滤/回聊天页逻辑都是真代码。

因此这些测试能断言：能翻页、去重、过滤入口项与字母标题、过滤被裁切的行、
数量校验、异常时也会点回聊天页、滚轮操作前后光标与前台窗口被复原。
"""
from __future__ import annotations

import types

import pytest

from wx_core import friends as F
from wx_core.models import OcrLine

# ---------------------------------------------------------------- 假几何（画面坐标）
IMG_SIZE = (1118, 809)
PANEL = (84, 0, 384, 809)          # 列表面板
REGION_TOP = 200                   # 「联系人」表头下沿（178..198）+2
REGION_BOTTOM = 807
FIRST_ROW_TOP = 260                # 第一行文字 top
PITCH = 68                         # 行距（125% DPI 实测 68）
TEXT_H = 23                        # 名字文字高度
NAME_X = 90                        # 名字列 x（面板内相对坐标）
LETTER_X = 46                      # 字母分组标题 x
NAME_TEXT_W = 120
PIXELS_PER_NOTCH = 3 * PITCH       # 真实 Qt 列表 1 格滚 3 行

CONTACTS_POINT_Y = 160             # layout.scale=1.0 时通讯录图标 y
CHAT_POINT_Y = 109


def _contacts_book(n: int, *, with_entries: bool = True):
    """造一本通讯录：前 8 个朋友 / 字母分组标题 / 若 n>5 再插一个 B 组。"""
    items: list[tuple[str, str]] = []
    if with_entries:
        items += [("entry", "新的朋友"), ("entry", "群聊"), ("entry", "标签"), ("entry", "公众号")]
    names = [f"好友{i:03d}" for i in range(1, n + 1)]
    # 每 5 个朋友前加一个字母分组标题
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    for i, name in enumerate(names):
        if i == 0 or i % 5 == 0:
            items.append(("letter", letters[(i // 5) % 26]))
        items.append(("name", name))
    return items, names


class _Crop:
    """假截图裁剪结果：只携带「哪一帧 + 哪个区域」的信息，没有真实像素。"""

    def __init__(self, shot: "_Shot", box):
        self.shot = shot
        self.box = tuple(int(v) for v in box)

    def tobytes(self) -> bytes:
        return f"{self.shot.tab}|{self.shot.offset}|{self.box}".encode()

    def resize(self, size, *args, **kwargs):
        return self


class _Shot:
    """假截图（PIL.Image 的最小子集）。"""

    size = IMG_SIZE

    def __init__(self, state: "_State"):
        self.state = state

    @property
    def offset(self) -> int:
        return int(self.state.offset)

    @property
    def tab(self) -> str:
        return self.state.tab

    def crop(self, box):
        return _Crop(self, box)

    def convert(self, mode):
        return self

    def tobytes(self) -> bytes:
        return f"{self.tab}|{self.offset}".encode()


class _State:
    """被测试代码操作的"真实世界"状态。"""

    def __init__(self, book, names, *, total=None, tab="chat", header_v=False):
        self.book = list(book)
        self.names = list(names)
        self.total = total
        self.tab = tab
        self.header_v = header_v
        self.offset = 0
        self.max_offset = max(0, FIRST_ROW_TOP + len(book) * PITCH - REGION_BOTTOM)
        self.clicked: list[tuple[int, int]] = []
        self.wheel: list[int] = []
        self.captures = 0

    # ---- 假窗口动作 ----
    def click(self, win, x, y):
        self.clicked.append((int(x), int(y)))
        self.tab = "contacts" if int(y) >= (CONTACTS_POINT_Y + CHAT_POINT_Y) // 2 else "chat"
        if self.tab == "chat":
            self.offset = 0
        return True

    def scroll(self, win, x, y, notches):
        self.wheel.append(int(notches))
        self.offset = min(self.max_offset, max(0, self.offset - int(notches) * PIXELS_PER_NOTCH))
        return True

    def capture(self, win):
        self.captures += 1
        return _Shot(self)


class _FakeOcr:
    """假 OCR：按「当前是哪一页 + 滚动偏移」生成通讯录列表的 OCR 行。"""

    def __init__(self, state: _State):
        self.state = state
        self.calls = 0
        self.fail_on: set[int] = set()

    def ocr(self, image):
        self.calls += 1
        if self.calls in self.fail_on:
            raise RuntimeError(f"fake ocr failure #{self.calls}")
        shot = image.shot
        return self._lines_for(shot)

    def _lines_for(self, shot: _Shot) -> list[OcrLine]:
        out: list[OcrLine] = []
        if shot.tab != "contacts":
            # 会话页：没有表头/通讯录管理按钮，只有会话名 → _scan 应判定"不在通讯录页"
            out.append(OcrLine(text="搜索", box=(25, 62, 82, 80)))
            out.append(OcrLine(text="文件传输助手", box=(NAME_X, 278, NAME_X + NAME_TEXT_W, 301)))
            out.append(OcrLine(text="微信团队", box=(NAME_X, 346, NAME_X + NAME_TEXT_W, 369)))
            return out
        # 通讯录页固定头部：「搜索」「通讯录管理」「联系人 N」
        out.append(OcrLine(text="搜索", box=(25, 62, 82, 80)))
        out.append(OcrLine(text="通讯录管理", box=(123, 113, 211, 131)))
        header_text = "V联系人" if self.state.header_v else "联系人"
        out.append(OcrLine(text=header_text, box=(25, 178, 98, 198)))
        if self.state.total is not None:
            out.append(OcrLine(text=str(self.state.total), box=(270, 180, 278, 196)))
        # 列表行
        offset = shot.offset
        for i, (kind, text) in enumerate(self.state.book):
            row_top = FIRST_ROW_TOP + i * PITCH - offset
            y0, y1 = row_top, row_top + TEXT_H
            # 与滚动区求交，模拟"上下被裁切的半行"
            y0c = max(y0, REGION_TOP)
            y1c = min(y1, REGION_BOTTOM)
            if y1c <= y0c:
                continue
            x0 = LETTER_X if kind == "letter" else NAME_X
            w = 8 if kind == "letter" else NAME_TEXT_W
            out.append(OcrLine(text=text, box=(x0, y0c, x0 + w, y1c)))
        return out


def _make_lay():
    return types.SimpleNamespace(
        image_size=IMG_SIZE,
        content=(0, 0, IMG_SIZE[0], IMG_SIZE[1]),
        sidebar=(0, 0, 84, IMG_SIZE[1]),
        conv_list=(84, 0, 384, IMG_SIZE[1]),
        chat=(384, 0, IMG_SIZE[0], IMG_SIZE[1]),
        row_height=PITCH,
        scale=1.0,
        dpi=96,
        sidebar_bg=(41, 41, 42),
        list_bg=(47, 47, 48),
        chat_bg=(30, 30, 31),
        warnings=[],
    )


def _install(monkeypatch, state: _State) -> _FakeOcr:
    """把 friends 模块的外部依赖换成假实现。"""
    ocr = _FakeOcr(state)
    lay = _make_lay()
    monkeypatch.setattr(F, "_capture", state.capture)
    monkeypatch.setattr(F, "_click", state.click)
    monkeypatch.setattr(F, "_scroll", state.scroll)
    monkeypatch.setattr(F, "_sleep", lambda seconds: None)
    monkeypatch.setattr(F, "_list_panel", lambda img, lay: PANEL)
    monkeypatch.setattr(F, "_analyze", lambda img, win: lay)
    monkeypatch.setattr(F, "_foreground", lambda: 0)
    monkeypatch.setattr(F, "_set_foreground", lambda hwnd: None)
    monkeypatch.setattr(F, "_cursor", lambda: (10, 20))
    monkeypatch.setattr(F, "_set_cursor", lambda x, y: None)
    monkeypatch.setattr(F, "_sidebar_tabs", lambda img, lay: [])
    return ocr


class _FakeWin:
    hwnd = 4321
    alive = True
    title = "微信"
    dpi = 96
    render_hwnd = 0
    client_rect = (0, 0, IMG_SIZE[0], IMG_SIZE[1])

    @property
    def window_rect(self):
        return (0, 0, IMG_SIZE[0], IMG_SIZE[1])

    def image_to_screen(self, x, y):
        return (100 + int(x), 200 + int(y))


@pytest.fixture
def win():
    return _FakeWin()


def _enumerate(monkeypatch, state, **kwargs):
    ocr = _install(monkeypatch, state)
    logs: list[tuple[str, str]] = []
    lay = _make_lay()
    names = F.enumerate_friends(_FakeWin(), lay, ocr,
                                log_cb=lambda t, lv="info": logs.append((t, lv)),
                                **kwargs)
    return names, logs, ocr


# ---------------------------------------------------------------- 测试

def test_pagination_dedupe_and_order(monkeypatch):
    """默认传 30 个好友（超过一屏），应翻页收集全部、按通讯录顺序、不重复。"""
    book, expected = _contacts_book(30)
    state = _State(book, expected, total=len(expected))
    names, logs, ocr = _enumerate(monkeypatch, state)

    assert names == expected, f"顺序/内容不对: {names}"
    assert len(names) == len(set(names)), "有重复好友名"
    assert any(notch < 0 for notch in state.wheel), "没有发生过向下滚动（没翻页）"
    assert state.tab == "chat", "结束时没有回到聊天页"
    assert state.clicked[-1][1] < CONTACTS_POINT_Y, "最后一次点击不是侧边栏「聊天」"
    assert any("已同步" in t for t, _ in logs), "缺少进度日志"
    assert ocr.calls > 1, "应该多屏 OCR"


def test_filters_entries_and_letter_headings(monkeypatch):
    """入口项（新的朋友/群聊/标签/公众号）、字母分组标题、数量数字都不应进名单。"""
    book, expected = _contacts_book(12)
    state = _State(book, expected, total=len(expected))
    names, _logs, _ocr = _enumerate(monkeypatch, state)

    assert names == expected
    for bad in ("新的朋友", "群聊", "标签", "公众号", "通讯录管理", "联系人", "12",
                "A", "B", "C", "D"):
        assert bad not in names, f"不该出现的入口项进了名单: {bad}"


def test_single_screen_skips_scrolling(monkeypatch):
    """首屏就装下全部（表头数量 == 首屏数量）时不该抢前台滚轮。"""
    book, expected = _contacts_book(4, with_entries=False)
    state = _State(book, expected, total=len(expected))
    names, logs, _ocr = _enumerate(monkeypatch, state)

    assert names == expected
    assert state.wheel == [], f"不该滚动，却滚了: {state.wheel}"
    assert state.tab == "chat"
    assert any("无需滚动" in t for t, _ in logs)


def test_scroll_clipped_rows_are_dropped(monkeypatch):
    """测试的假 OCR 会生成上下被裁切的半行；这些行不应污染名单（只允许完整行）。"""
    book, expected = _contacts_book(40)
    state = _State(book, expected, total=None)      # 没有数量校验，只能靠翻页
    names, _logs, _ocr = _enumerate(monkeypatch, state)

    assert names == expected, f"裁切行/漏行导致名单不对: {names}"
    assert len(names) == len(set(names))


def test_returns_to_chat_even_on_ocr_exception(monkeypatch):
    """OCR 中途抛异常时，finally 仍必须把微信切回聊天页。"""
    book, expected = _contacts_book(40)
    state = _State(book, expected, total=None)
    ocr = _install(monkeypatch, state)
    ocr.fail_on = {4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14}

    names = F.enumerate_friends(_FakeWin(), _make_lay(), ocr)
    assert len(names) < len(expected), "异常测试应当提前中断"
    assert state.tab == "chat", "异常后没有回到聊天页"
    assert state.clicked and state.clicked[-1][1] < CONTACTS_POINT_Y


def test_opens_contacts_page_first(monkeypatch):
    """初始在聊天页 → 必须先点「通讯录」图标，结束时再点回「聊天」。"""
    book, expected = _contacts_book(5)
    state = _State(book, expected, total=len(expected), tab="chat")
    names, logs, _ocr = _enumerate(monkeypatch, state)

    assert names == expected
    contact_clicks = [pt for pt in state.clicked if pt[1] >= CONTACTS_POINT_Y]
    chat_clicks = [pt for pt in state.clicked if pt[1] < CONTACTS_POINT_Y]
    assert contact_clicks, "没有点「通讯录」"
    assert chat_clicks, "没有点回「聊天」"
    assert any("通讯录" in t for t, _ in logs)


def test_already_on_contacts_page_skips_first_click(monkeypatch):
    """已经在通讯录页时不必再点一次「通讯录」。"""
    book, expected = _contacts_book(3)
    state = _State(book, expected, total=len(expected), tab="contacts")
    names, _logs, _ocr = _enumerate(monkeypatch, state)

    assert names == expected
    contact_clicks = [pt for pt in state.clicked if pt[1] >= CONTACTS_POINT_Y]
    assert not contact_clicks, f"不该再点通讯录: {contact_clicks}"
    assert state.tab == "chat"


def test_max_scrolls_guard(monkeypatch):
    """max_scrolls 很小的时候应提前收手，而不是死循环。"""
    book, expected = _contacts_book(60)
    state = _State(book, expected, total=None)
    names, logs, _ocr = _enumerate(monkeypatch, state, max_scrolls=1)

    assert len(names) < len(expected), "max_scrolls=1 不应该读全"
    assert any("最大滚动次数" in t for t, _ in logs)
    assert state.tab == "chat"


def test_time_budget_guard(monkeypatch):
    """time_budget 已经耗尽时立即停止翻页。"""
    book, expected = _contacts_book(60)
    state = _State(book, expected, total=None)
    names, logs, _ocr = _enumerate(monkeypatch, state, time_budget=-1.0)

    assert names == expected[:len(names)]
    assert any("超时保护" in t for t, _ in logs)
    assert state.tab == "chat"


# ---------------------------------------------------------------- 真实滚轮的 try/finally

def test_scroll_restores_cursor_and_foreground(monkeypatch):
    """_scroll 发滚轮前抢前台/移动光标，结束后必须复原光标位置与前台窗口。"""
    calls = {"cursor": [], "wheel": [], "restore": [], "force": []}
    monkeypatch.setattr(F, "_foreground", lambda: 4321)
    monkeypatch.setattr(F, "_cursor", lambda: (5, 6))
    monkeypatch.setattr(F, "_set_cursor", lambda x, y: calls["cursor"].append((x, y)))
    monkeypatch.setattr(F, "_force_foreground", lambda hwnd: calls["force"].append(hwnd) or True)
    monkeypatch.setattr(F, "_restore_foreground",
                        lambda prev, win=None: calls["restore"].append(prev) or True)
    monkeypatch.setattr(F, "_mouse_wheel", lambda delta: calls["wheel"].append(delta))
    monkeypatch.setattr(F, "_sleep", lambda seconds: None)
    monkeypatch.setattr(F.W, "is_foreground", lambda win: True)

    assert F._scroll(_FakeWin(), 300, 450, -3) is True
    assert calls["wheel"] == [-120, -120, -120]
    assert calls["cursor"][0] == (100 + 300, 200 + 450), "没有把光标移到列表上"
    assert calls["cursor"][-1] == (5, 6), "光标没有复原"
    assert calls["restore"] == [4321], "原前台窗口没有复原"
    assert calls["force"] == [], "已经是前台了不需要再抢"


def test_scroll_aborts_without_foreground(monkeypatch):
    """抢不到前台时不发滚轮（避免把别的窗口滚掉），且照样复原光标。"""
    calls = {"cursor": [], "wheel": [], "force": []}
    monkeypatch.setattr(F, "_foreground", lambda: 999)
    monkeypatch.setattr(F, "_cursor", lambda: (5, 6))
    monkeypatch.setattr(F, "_set_cursor", lambda x, y: calls["cursor"].append((x, y)))
    monkeypatch.setattr(F, "_force_foreground", lambda hwnd: calls["force"].append(hwnd) or False)
    monkeypatch.setattr(F, "_restore_foreground", lambda prev, win=None: True)
    monkeypatch.setattr(F, "_mouse_wheel", lambda delta: calls["wheel"].append(delta))
    monkeypatch.setattr(F, "_sleep", lambda seconds: None)
    monkeypatch.setattr(F.W, "is_foreground", lambda win: False)

    assert F._scroll(_FakeWin(), 300, 450, -2) is False
    assert calls["wheel"] == [], "没抢到前台却发了滚轮"
    assert calls["force"] == [_FakeWin.hwnd], "应尝试把微信抢到前台"
    assert calls["cursor"][-1] == (5, 6)


class _FakeUser32ForForeground:
    """模拟 Windows 前台锁：只有 AttachThreadInput 过、或用户真实按键后才允许切前台。"""

    def __init__(self):
        self.attached = False
        self.alt_pressed = False
        self.fg = 7

    def GetWindowThreadProcessId(self, hwnd, ptr):
        import ctypes

        ctypes.cast(ptr, ctypes.POINTER(__import__("ctypes").wintypes.DWORD))[0].value = 1234

    def AttachThreadInput(self, a, b, on):
        self.attached = bool(on)

    def keybd_event(self, vk, sc, flags, extra):
        self.alt_pressed = True

    def SetForegroundWindow(self, hwnd):
        if self.attached or self.alt_pressed:
            self.fg = int(hwnd)
            return True
        return False


def _install_force_env(monkeypatch, fake: _FakeUser32ForForeground):
    monkeypatch.setattr(F.W, "user32", fake)
    monkeypatch.setattr(F.W, "kernel32", types.SimpleNamespace(GetCurrentThreadId=lambda: 555))
    monkeypatch.setattr(F, "_foreground", lambda: fake.fg)
    monkeypatch.setattr(F, "_set_foreground", lambda hwnd: fake.SetForegroundWindow(hwnd))
    monkeypatch.setattr(F, "_sleep", lambda seconds: None)


def test_force_foreground_attach_thread_input(monkeypatch):
    """SetForegroundWindow 被系统拒绝后，用 AttachThreadInput 兜底。"""
    fake = _FakeUser32ForForeground()
    _install_force_env(monkeypatch, fake)
    assert F._force_foreground(4321) is True
    assert fake.fg == 4321


def test_force_foreground_alt_fallback(monkeypatch):
    """AttachThreadInput 也失败时，用轻敲 ALT（真实键盘输入）解除前台锁。"""
    fake = _FakeUser32ForForeground()
    fake.AttachThreadInput = lambda a, b, on: None      # 让 attach 兜底失效
    _install_force_env(monkeypatch, fake)
    assert F._force_foreground(4321) is True
    assert fake.alt_pressed is True
    assert fake.fg == 4321


def test_force_foreground_noop_when_already_foreground(monkeypatch):
    fake = _FakeUser32ForForeground()
    fake.fg = 4321
    _install_force_env(monkeypatch, fake)
    assert F._force_foreground(4321) is True
    assert fake.alt_pressed is False


def test_restore_foreground_only_when_taken(monkeypatch):
    win = _FakeWin()
    monkeypatch.setattr(F, "_foreground", lambda: 4321)
    forced: list[int] = []
    monkeypatch.setattr(F, "_force_foreground", lambda hwnd: forced.append(hwnd) or True)

    # 前台本来就是微信 → 不需要恢复
    assert F._restore_foreground(4321, win) is True
    assert forced == []
    # 前台被抢成微信 → 恢复成原来的 999
    assert F._restore_foreground(999, win) is True
    assert forced == [999]
    # 前台已经是原窗口 → 不动作
    forced.clear()
    monkeypatch.setattr(F, "_foreground", lambda: 999)
    assert F._restore_foreground(999, win) is True
    assert forced == []


# ---------------------------------------------------------------- 纯函数

def test_header_with_chevron_prefix(monkeypatch):
    """真实微信把展开箭头和「联系人」连成一行（OCR 成 "V联系人"）时也要认得出表头。"""
    book, expected = _contacts_book(4, with_entries=False)
    state = _State(book, expected, total=len(expected), header_v=True)
    names, logs, _ocr = _enumerate(monkeypatch, state)

    assert names == expected
    assert any("联系人总数：4" in t for t, _ in logs)
    assert state.wheel == [], "读到表头数量后应直接结束，不滚轮"


def test_count_from_text_and_words():
    assert F._count_from_text("联系人 128") == 128
    assert F._count_from_text("联系人１２８") == 128
    assert F._count_from_text("联系人") is None
    # OCR 把同一个数字识别成「全角 + 半角」两段的伪影（实测出现过 联系人４4）
    assert F._count_from_text("联系人４4") == 4
    assert F._count_from_text("联系人4") == 4
    assert F._count_from_text("联系人44") == 44
    # 展开箭头被识别成 V / 〈 后连在「联系人」前面（实测）
    assert F._count_from_text("V联系人4") == 4
    assert F._count_from_text("〈联系人 4") == 4
    assert F._count_from_words(
        [{"text": "联系人", "box": (41, 178, 98, 198)},
         {"text": "4", "box": (270, 180, 278, 196)}], 300) == 4
    # 同一个数字被识别成重叠的两段 → 去重后仍是 4
    assert F._count_from_words(
        [{"text": "４", "box": (268, 180, 278, 196)},
         {"text": "4", "box": (269, 180, 279, 196)}], 300) == 4


def test_entry_and_heading_filter():
    for bad in ("A", "z", "通讯录管理", "新的朋友", "群聊", "标签", "公众号", "12", "01:59"):
        assert F._is_entry_or_heading(bad), bad
    for good in ("瀑布", "白云一片去悠悠", "微信团队", "文件传输助手", "张三·工作号"):
        assert not F._is_entry_or_heading(good), good


def test_public_api_surface():
    assert F.__all__ == ["enumerate_friends"]
    import inspect

    sig = inspect.signature(F.enumerate_friends)
    assert list(sig.parameters) == ["win", "lay", "ocr", "log_cb", "max_scrolls", "time_budget"]
    assert sig.parameters["max_scrolls"].default == 120
    assert sig.parameters["time_budget"].default == 90.0
