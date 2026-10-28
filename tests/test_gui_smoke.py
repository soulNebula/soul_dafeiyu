"""GUI 冒烟测试（无人工交互）。

覆盖：
* 窗口能创建（withdraw 隐藏），事件泵消费 status/friends/log/message；
* 文字 / 图片 / GIF 表情包 / 文件 / 语音 五类消息卡片都能渲染；
* 好友搜索即时过滤、会话切换、未读计数；
* 清空消息时 GIF 的 after 回调被取消（active_animation_count() == 0）；
* 开始/停止监听的按钮流程（用记录调用的 StubBackend，不碰真实微信）；
* 假后端（ui/dev_backend.py）端到端产生各类消息；
* app.load_backend 的假后端 / 回退逻辑。

无桌面环境（CI）时自动 pytest.skip。
"""
from __future__ import annotations

import importlib.util
import queue
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

tk = pytest.importorskip("tkinter")
pytest.importorskip("PIL")

from wx_core.models import (  # noqa: E402
    KIND_EMOJI,
    KIND_FILE,
    KIND_IMAGE,
    KIND_TEXT,
    KIND_VOICE,
    SENDER_FRIEND,
    SENDER_SELF,
    Friend,
    Message,
    event_friends,
    event_log,
    event_message,
    event_status,
)
from ui.dev_backend import DevBackend  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402

# Tk 桌面窗口冒烟：需要 Windows 图形会话（CI 是 headless linux）。
# 用 marker 而不是 --ignore：SOULAGENT_CI=1 或非 Windows 时 conftest 统一 skip，
# skip 条数/原因在报告里可见；本机 Windows 上照常全跑。
pytestmark = pytest.mark.windows_only


# ---------------------------------------------------------------------------
# 素材 & 辅助
# ---------------------------------------------------------------------------
def _load_fixture_module():
    """按文件路径加载 tests/fixtures/gui/gui_fixtures.py。"""
    fixture_path = Path(__file__).parent / "fixtures" / "gui" / "gui_fixtures.py"
    spec = importlib.util.spec_from_file_location("wxbox_gui_fixtures", fixture_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gui_fixtures = _load_fixture_module()


@pytest.fixture(scope="session")
def media(tmp_path_factory) -> dict[str, Path]:
    """生成冒烟测试用的 PNG / GIF / PDF 素材。"""
    return gui_fixtures.make_media(tmp_path_factory.mktemp("gui_media"))


@pytest.fixture
def root():
    """创建隐藏的 Tk 根窗口；无桌面环境时跳过。"""
    try:
        window_root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - 取决于运行环境
        pytest.skip(f"无图形界面，跳过 GUI 冒烟测试：{exc}")
    window_root.withdraw()
    try:
        yield window_root
    finally:
        try:
            window_root.destroy()
        except tk.TclError:
            pass


class StubBackend:
    """记录方法调用的后端替身（不连接微信、不启动线程）。"""

    def __init__(self) -> None:
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.calls: list[tuple[str, object]] = []
        self._running = False
        self._monitored: list[str] = []
        self._friends = [Friend(name="张三"), Friend(name="李四"), Friend(name="王五")]

    def start(self) -> None:
        """记录 start 调用。"""
        self.calls.append(("start", None))
        self._running = True

    def stop(self) -> None:
        """记录 stop 调用。"""
        self.calls.append(("stop", None))
        self._running = False

    def get_state(self) -> dict:
        """返回简单状态快照。"""
        return {
            "running": self._running,
            "wechat_found": True,
            "chat_title": "",
            "monitoring": list(self._monitored),
            "error": None,
        }

    def list_friends(self, refresh: bool = False) -> list[Friend]:
        """记录 list_friends 调用并返回好友。"""
        self.calls.append(("list_friends", refresh))
        return list(self._friends)

    def set_monitored(self, names: list[str]) -> None:
        """记录 set_monitored 调用。"""
        self.calls.append(("set_monitored", list(names)))
        self._monitored = list(names)

    def is_running(self) -> bool:
        """返回运行状态。"""
        return self._running


def make_window(root, backend=None, *, demo: bool = False) -> tuple[MainWindow, object]:
    """创建 MainWindow 并启动事件泵（默认使用不自动发消息的假后端）。"""
    if backend is None:
        backend = DevBackend({}, interval=60.0)
    window = MainWindow(root, backend, config={}, demo_mode=demo)
    window.start()
    return window, backend


def pump(root, window: MainWindow, rounds: int = 3) -> None:
    """手动驱动事件泵与 Tk 事件循环若干轮。"""
    for _ in range(rounds):
        window.pump_events()
        try:
            root.update_idletasks()
            root.update()
        except tk.TclError:
            return


def friends_event(names: list[str]) -> dict:
    """构造 friends 事件。"""
    return event_friends(len(names), [Friend(name=name) for name in names])


def collect_label_texts(widget) -> list[str]:
    """递归收集控件树里所有 Label 的文本（断言卡片内容用）。"""
    texts: list[str] = []
    try:
        children = widget.winfo_children()
    except tk.TclError:
        return texts
    for child in children:
        if isinstance(child, tk.Label):
            try:
                texts.append(str(child.cget("text")))
            except tk.TclError:
                pass
        texts.extend(collect_label_texts(child))
    return texts


def iter_labels(widget) -> list[tk.Label]:
    """递归收集控件树里所有 Label（断言尺寸用）。"""
    labels: list[tk.Label] = []
    try:
        children = widget.winfo_children()
    except tk.TclError:
        return labels
    for child in children:
        if isinstance(child, tk.Label):
            labels.append(child)
        labels.extend(iter_labels(child))
    return labels


def show_invisible(root) -> None:
    """把根窗口设为全透明后 deiconify，让 Tk 完成真实布局但不显示给用户。"""
    try:
        root.attributes("-alpha", 0.0)
    except tk.TclError:
        pass
    root.deiconify()
    root.update()


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------
def test_window_title_and_empty_state(root):
    """窗口标题正确，初始状态显示未连接/未监听/好友总数 0。"""
    window, _backend = make_window(root)
    try:
        assert root.title() == "微信好友消息监听器"
        assert window.conn_label.cget("text") == "微信未连接"
        assert window.listen_label.cget("text") == "未监听"
        assert window.total_label.cget("text") == "好友总数: 0"
        assert window.message_card_count() == 0
        assert window.friend_row_count() == 0
    finally:
        window.close()


def test_event_pump_renders_all_kinds(root, media):
    """注入五类消息事件后卡片数量增加，状态栏与日志同步更新。"""
    window, backend = make_window(root)
    try:
        base = time.time()
        backend.events.put(
            event_status(
                running=True,
                wechat_found=True,
                chat_title="白云一片去悠悠",
                monitoring=["白云一片去悠悠"],
                tick=1,
            )
        )
        backend.events.put(friends_event(["白云一片去悠悠", "张三", "李四"]))
        backend.events.put(event_log("测试日志：后端已连接", "info"))
        pump(root, window)

        assert window.friend_row_count() == 3
        assert window.total_label.cget("text") == "好友总数: 3"
        assert window.conn_label.cget("text") == "微信已连接"
        assert "监听中" in window.listen_label.cget("text")
        assert "测试日志：后端已连接" in window.log_text()

        events = [
            event_message(
                Message(
                    friend="白云一片去悠悠",
                    kind=KIND_TEXT,
                    sender=SENDER_FRIEND,
                    text="你好，这是一条文字消息",
                    ts=base + 1,
                )
            ),
            event_message(
                Message(
                    friend="白云一片去悠悠",
                    kind=KIND_IMAGE,
                    sender=SENDER_FRIEND,
                    media_path=str(media["png"]),
                    ts=base + 2,
                )
            ),
            event_message(
                Message(
                    friend="白云一片去悠悠",
                    kind=KIND_EMOJI,
                    sender=SENDER_FRIEND,
                    media_path=str(media["gif"]),
                    extra={"animated": True, "n_frames": 3},
                    ts=base + 3,
                )
            ),
            event_message(
                Message(
                    friend="白云一片去悠悠",
                    kind=KIND_FILE,
                    sender=SENDER_FRIEND,
                    text="项目需求文档.pdf",
                    media_path=str(media["pdf"]),
                    extra={"size_text": "1.8 MB"},
                    ts=base + 4,
                )
            ),
            event_message(
                Message(
                    friend="白云一片去悠悠",
                    kind=KIND_VOICE,
                    sender=SENDER_FRIEND,
                    extra={"duration": 8},
                    ts=base + 5,
                )
            ),
        ]
        for event in events:
            backend.events.put(event)
        pump(root, window)

        assert window.message_card_count() == 5
        assert set(window.card_kinds()) == {
            KIND_TEXT,
            KIND_IMAGE,
            KIND_EMOJI,
            KIND_FILE,
            KIND_VOICE,
        }
        texts = " ".join(collect_label_texts(window.msg_scroll.inner))
        assert "你好，这是一条文字消息" in texts
        assert "语音消息（未接收）" in texts
        assert "项目需求文档.pdf" in texts
        assert "1.8 MB" in texts
        # GIF 表情包进入循环播放
        assert window.active_animation_count() >= 1
    finally:
        window.close()


def test_search_filter_conversation_and_unread(root):
    """好友搜索即时过滤、会话切换、非当前会话消息计入未读。"""
    window, backend = make_window(root)
    try:
        backend.events.put(friends_event(["张三", "李四", "王五", "赵六"]))
        pump(root, window)
        assert window.friend_row_count() == 4

        window.search_var.set("李")
        pump(root, window)
        assert window.friend_row_count() == 1
        window.search_var.set("")
        pump(root, window)
        assert window.friend_row_count() == 4

        window.set_checked("张三")
        window.set_checked("王五")
        assert window.checked_names() == ["张三", "王五"]

        backend.events.put(
            event_message(
                Message(friend="李四", kind=KIND_TEXT, text="李四的第一条消息", ts=time.time() + 10)
            )
        )
        pump(root, window)
        assert window.message_card_count() == 1

        window.select_conversation("李四")
        root.update()
        assert window.current_conversation() == "李四"
        assert window.message_card_count() == 1

        backend.events.put(
            event_message(
                Message(friend="王五", kind=KIND_TEXT, text="王五的消息", ts=time.time() + 11)
            )
        )
        pump(root, window)
        assert window.message_card_count() == 1
        assert window.unread_counts().get("王五") == 1

        window.select_conversation("王五")
        root.update()
        assert window.message_card_count() == 1
        assert window.unread_counts().get("王五", 0) == 0

        window.select_conversation(None)
        root.update()
        assert window.message_card_count() == 2
    finally:
        window.close()


def test_clear_messages_stops_gif_animations(root, media):
    """清空消息后卡片归零且 GIF 的 after 回调全部取消。"""
    window, backend = make_window(root)
    try:
        backend.events.put(friends_event(["张三"]))
        backend.events.put(
            event_message(
                Message(
                    friend="张三",
                    kind=KIND_EMOJI,
                    sender=SENDER_FRIEND,
                    media_path=str(media["gif"]),
                    extra={"animated": True},
                    ts=time.time() + 20,
                )
            )
        )
        pump(root, window)
        assert window.message_card_count() == 1
        assert window.active_animation_count() == 1

        window.clear_messages()
        root.update()
        assert window.message_card_count() == 0
        assert window.active_animation_count() == 0

        # 再驱动几轮事件循环，确认没有残留 after 回调把动画拉起来
        for _ in range(5):
            root.update()
            time.sleep(0.03)
        assert window.active_animation_count() == 0
        assert "已清空消息记录" in window.log_text()
    finally:
        window.close()


def test_start_stop_monitoring_flow(root):
    """勾选好友后开始监听会调用 start + set_monitored；停止监听清空名单。"""
    backend = StubBackend()
    backend.events.put(friends_event(["张三", "李四", "王五"]))
    window, _ = make_window(root, backend=backend)
    try:
        pump(root, window)
        window.set_checked("张三")
        window.set_checked("李四")

        window._on_start_stop()
        root.update()
        assert ("start", None) in backend.calls
        assert ("set_monitored", ["张三", "李四"]) in backend.calls
        assert backend.is_running()
        assert window.start_btn.cget("text") == "停止监听"

        window._on_start_stop()
        root.update()
        assert ("set_monitored", []) in backend.calls
        deadline = time.time() + 2
        while time.time() < deadline and ("stop", None) not in backend.calls:
            root.update()
            time.sleep(0.01)
        assert ("stop", None) in backend.calls
        assert not backend.is_running()
        assert window.start_btn.cget("text") == "开始监听"
    finally:
        window.close()


def test_start_without_selection_shows_error(root):
    """未勾选好友就点开始监听时，给出红色错误提示且不启动后端。"""
    backend = StubBackend()
    window, _ = make_window(root, backend=backend)
    try:
        backend.events.put(friends_event(["张三"]))
        pump(root, window)
        window._on_start_stop()
        root.update()
        assert "请先" in window.error_label.cget("text")
        assert not backend.is_running()
        assert ("start", None) not in backend.calls
    finally:
        window.close()


def test_duplicate_messages_are_ignored(root):
    """同一 id 的消息事件只显示一次（去重）。"""
    window, backend = make_window(root)
    try:
        message = Message(
            friend="张三", kind=KIND_TEXT, sender=SENDER_FRIEND, text="重复消息", ts=time.time()
        )
        backend.events.put(event_message(message))
        backend.events.put(event_message(message))
        pump(root, window)
        assert window.message_card_count() == 1
    finally:
        window.close()


def test_dev_backend_end_to_end(root):
    """假后端每 0.15 秒产生一条消息，覆盖文字/图片/GIF/文件/语音。"""
    backend = DevBackend({}, interval=0.15)
    window = MainWindow(root, backend, config={}, demo_mode=True)
    window.start()
    try:
        backend.start()
        deadline = time.time() + 10
        while time.time() < deadline and window.message_card_count() < 6:
            window.pump_events()
            root.update()
            time.sleep(0.02)
        assert window.friend_row_count() >= 3
        assert window.message_card_count() >= 5
        assert window.active_animation_count() >= 1
        assert "假后端" in window.log_text()
        assert {KIND_TEXT, KIND_IMAGE, KIND_EMOJI, KIND_FILE, KIND_VOICE} <= set(
            window.card_kinds()
        )
    finally:
        window.close()
        assert not backend.is_running()


def test_app_load_backend_force_fake():
    """app.load_backend(force_fake=True) 返回假后端且没有回退原因。"""
    import app as app_module

    backend, reason, is_fake = app_module.load_backend({}, force_fake=True)
    assert is_fake is True
    assert reason == ""
    assert isinstance(backend, DevBackend)


def test_app_load_backend_falls_back_to_fake(monkeypatch):
    """真实后端 import 失败时自动回退假后端并给出提示。"""
    import app as app_module

    monkeypatch.setattr(
        app_module,
        "_BACKEND_CANDIDATES",
        (("wx_core.__definitely_missing_backend__", ("create_backend",)),),
    )
    backend, reason, is_fake = app_module.load_backend({}, force_fake=False)
    assert is_fake is True
    assert isinstance(backend, DevBackend)
    assert "回退" in reason


def test_self_message_sender_chip(root):
    """自己发送的消息卡片显示「我」而不是「对方」。"""
    window, backend = make_window(root)
    try:
        backend.events.put(
            event_message(
                Message(
                    friend="张三",
                    kind=KIND_TEXT,
                    sender=SENDER_SELF,
                    text="我发出的一条消息",
                    ts=time.time() + 30,
                )
            )
        )
        pump(root, window)
        texts = collect_label_texts(window.msg_scroll.inner)
        assert "我" in texts
        assert "我发出的一条消息" in " ".join(texts)
    finally:
        window.close()


def test_image_preview_opens_toplevel(root, media):
    """点击图片预览时能打开大图 Toplevel，且不阻塞主窗口。"""
    window, _backend = make_window(root)
    try:
        window._open_preview(str(media["png"]), "图片预览")
        root.update()
        previews = [child for child in root.winfo_children() if isinstance(child, tk.Toplevel)]
        assert previews, "应当打开一个预览 Toplevel"
        assert window.message_card_count() == 0
        for preview in previews:
            preview.destroy()
    finally:
        window.close()


def test_switch_conversation_stops_animations(root, media):
    """切换会话会停止旧消息卡片里的 GIF after 回调，切回时重新创建。"""
    window, backend = make_window(root)
    try:
        backend.events.put(friends_event(["张三", "李四"]))
        backend.events.put(
            event_message(
                Message(
                    friend="张三",
                    kind=KIND_EMOJI,
                    media_path=str(media["gif"]),
                    extra={"animated": True},
                    ts=time.time() + 40,
                )
            )
        )
        pump(root, window)
        assert window.active_animation_count() == 1

        window.select_conversation("李四")
        root.update()
        assert window.message_card_count() == 0
        assert window.active_animation_count() == 0

        window.select_conversation(None)
        root.update()
        assert window.message_card_count() == 1
        assert window.active_animation_count() == 1
    finally:
        window.close()


def test_status_error_text_is_red(root):
    """status 里的 error 字段会以红色错误提示显示在状态栏。"""
    window, backend = make_window(root)
    try:
        backend.events.put(
            event_status(
                running=True,
                wechat_found=False,
                monitoring=["张三"],
                error="微信窗口已最小化，暂时无法截图",
            )
        )
        pump(root, window)
        assert "已最小化" in window.error_label.cget("text")
        assert window.error_label.cget("fg").lower() in ("#ff6b6b", "red")
        assert window.conn_label.cget("text") == "微信未连接"
    finally:
        window.close()


def test_resync_runs_in_background_thread(root):
    """重新同步好友会在后台线程调用 list_friends(refresh=True)，不阻塞 GUI。"""
    backend = StubBackend()
    window, _ = make_window(root, backend=backend)
    try:
        window._on_resync()
        deadline = time.time() + 2
        while time.time() < deadline and ("list_friends", True) not in backend.calls:
            root.update()
            time.sleep(0.01)
        assert ("list_friends", True) in backend.calls
        assert "正在重新同步" in window.log_text()
    finally:
        window.close()


def test_voice_duration_shown_once(root):
    """语音时长（后端给 "2秒"）只显示一次，不出现「2秒 秒」。"""
    window, backend = make_window(root)
    try:
        backend.events.put(
            event_message(
                Message(
                    friend="张三",
                    kind=KIND_VOICE,
                    sender=SENDER_FRIEND,
                    text="",
                    extra={"duration": "2秒"},
                    ts=time.time() + 60,
                )
            )
        )
        pump(root, window)
        texts = " ".join(collect_label_texts(window.msg_scroll.inner))
        assert "语音消息（未接收）" in texts
        assert "2秒" in texts
        assert "秒 秒" not in texts
        assert "2 秒" not in texts
    finally:
        window.close()


def test_media_preview_sizes_are_capped(root, media):
    """图片最大 320 宽、表情包最大 160×160，且等比缩放。"""
    window, backend = make_window(root)
    try:
        backend.events.put(
            event_message(
                Message(
                    friend="张三",
                    kind=KIND_IMAGE,
                    sender=SENDER_FRIEND,
                    media_path=str(media["big_png"]),
                    ts=time.time() + 61,
                )
            )
        )
        backend.events.put(
            event_message(
                Message(
                    friend="张三",
                    kind=KIND_EMOJI,
                    sender=SENDER_FRIEND,
                    media_path=str(media["big_gif"]),
                    extra={"animated": True},
                    ts=time.time() + 62,
                )
            )
        )
        pump(root, window)
        assert window.message_card_count() == 2

        image_card = window._cards[0]
        assert image_card._photos, "大图应生成 PhotoImage"
        photo = image_card._photos[0]
        assert photo.width() <= 320
        assert photo.height() <= 260
        assert photo.width() > 0 and photo.height() > 0

        emoji_card = window._cards[1]
        assert emoji_card._gifs, "动图表情包应使用 GifPlayer"
        frame = emoji_card._gifs[0]._frames[0]
        assert frame.width() <= 160
        assert frame.height() <= 160
    finally:
        window.close()


def test_long_text_stays_inside_message_area(root):
    """960 宽窗口下长文本自动换行，卡片与 Canvas 内容不横向溢出。"""
    window, backend = make_window(root)
    try:
        show_invisible(root)
        root.geometry("960x620")
        root.update()

        long_text = "这是一条很长的中文消息，用来验证窗口变窄时不会横向溢出，" * 8
        backend.events.put(
            event_message(
                Message(
                    friend="长消息好友",
                    kind=KIND_TEXT,
                    sender=SENDER_FRIEND,
                    text=long_text,
                    ts=time.time() + 63,
                )
            )
        )
        pump(root, window)
        root.update()

        canvas_width = window.msg_scroll.canvas.winfo_width()
        assert canvas_width > 200, "画布应已完成布局"
        card = window._cards[0]
        assert card.winfo_width() <= canvas_width

        bubble = next(
            label
            for label in iter_labels(card)
            if long_text[:8] in str(label.cget("text"))
        )
        assert bubble.winfo_reqwidth() <= card.winfo_width()
        bbox = window.msg_scroll.canvas.bbox("all")
        assert bbox is not None
        assert bbox[2] <= canvas_width + 2, "Canvas 内容宽度不应超出可视宽度"
    finally:
        window.close()


def test_new_messages_auto_scroll_to_bottom(root):
    """新消息到达后消息区自动滚动到最新一条。"""
    window, backend = make_window(root)
    try:
        show_invisible(root)
        root.geometry("990x726")
        root.update()
        base = time.time()
        for index in range(14):
            backend.events.put(
                event_message(
                    Message(
                        friend="张三",
                        kind=KIND_TEXT,
                        sender=SENDER_FRIEND,
                        text=f"第 {index} 条消息，" + "内容" * 20,
                        ts=base + index + 70,
                    )
                )
            )
        pump(root, window)
        root.update()
        _top, bottom = window.msg_scroll.canvas.yview()
        assert bottom >= 0.99, "应自动滚动到底部"
    finally:
        window.close()


def test_log_area_min_height_and_collapse(root):
    """日志区最小高度 100px，且支持折叠/展开。"""
    window, backend = make_window(root)
    try:
        show_invisible(root)
        root.geometry("990x726")
        root.update()
        assert window.log_container.winfo_height() >= 100
        assert len(window.vpaned.panes()) == 2

        window._toggle_log()
        root.update()
        assert window._log_collapsed is True
        assert len(window.vpaned.panes()) == 1
        assert window.log_toggle_btn.cget("text") == "显示日志"

        window._toggle_log()
        root.update()
        assert window._log_collapsed is False
        assert len(window.vpaned.panes()) == 2
        assert window.log_container.winfo_height() >= 100
    finally:
        window.close()


def test_status_bar_handles_long_text(root):
    """超长监听名/错误文本不会把状态栏撑破：名字省略、错误换行显示。"""
    window, backend = make_window(root)
    try:
        show_invisible(root)
        root.geometry("960x620")
        root.update()
        long_name = "超长名字好友" * 5
        backend.events.put(
            event_status(
                running=True,
                wechat_found=True,
                monitoring=[long_name],
                error="这是一条很长的错误信息，" * 10,
            )
        )
        pump(root, window)
        root.update()
        assert "…" in window.listen_label.cget("text")
        assert window.error_label.winfo_ismapped()
        assert str(window.error_label.cget("text")).startswith("这是一条很长的错误信息")
    finally:
        window.close()
