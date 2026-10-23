"""wx_box 主窗口（纯 Tkinter + ttk + Pillow）。

设计约束（SPEC 第 3/4 节）：
* GUI 线程只做两件事：``queue.get_nowait()`` 消费事件 + ``root.after(100)`` 定时刷新；
* 截图 / OCR / 输入等采集动作一律由后台 Backend 完成，本文件绝不调用；
* 所有后端调用都包在 try/except 里，后端异常不会拖垮界面。
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from collections import deque
from tkinter import messagebox, ttk
from pathlib import Path

from wx_core.models import Friend, Message

from . import widgets as W


def _elide(text: str, limit: int = 18) -> str:
    """把过长文本截断到 limit 个字符并以省略号结尾。"""
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


class MainWindow:
    """微信好友消息监听器主窗口。"""

    PUMP_INTERVAL_MS = 100      # 事件泵间隔（毫秒），SPEC 要求 100ms
    MAX_CARDS = 400             # 当前视图最多卡片数
    MAX_ALL_MESSAGES = 3000     # 内存中最多保留的全部消息数
    MAX_PER_FRIEND = 500        # 每个好友最多保留消息数
    MAX_SEEN = 5000             # 去重集合上限
    MAX_DRAIN_PER_TICK = 200    # 每次泵最多消费事件数，避免卡死

    def __init__(
        self,
        root: tk.Tk,
        backend,
        config: dict | None = None,
        *,
        demo_mode: bool = False,
        demo_note: str = "",
    ):
        """初始化主窗口；只构建界面，不启动事件泵（调用 start() 启动）。"""
        self.root = root
        self.backend = backend
        self.config = dict(config or {})
        self.demo_mode = bool(demo_mode)
        self._demo_note = str(demo_note or "")
        self.events: "queue.Queue[dict]" = getattr(backend, "events", queue.Queue())

        # 数据 / 状态
        self._friends: list[Friend] = []
        self._friend_names: list[str] = []
        self._checked: set[str] = set()
        self._unread: dict[str, int] = {}
        self._friend_rows: dict[str, W.FriendRow] = {}
        self._messages: dict[str, list[Message]] = {}
        self._all_messages: list[Message] = []
        self._seen: set[str] = set()
        self._seen_order: deque[str] = deque()
        self._current: str | None = None
        self._cards: list[W.MessageCard] = []
        self._gifs: set[W.GifPlayer] = set()
        self._previews: list[tk.Toplevel] = []
        self._status: dict = {}
        self._last_backend_error = ""
        self._initial_checked_applied = False
        self._listening = False
        self._log_collapsed = False
        self._pending_scroll = False
        self._reflow_after_id: str | None = None
        self._closing = False
        self._after_id: str | None = None

        self._setup_window()
        self._build_ui()
        self._refresh_status_ui()
        self._refresh_chat_title()
        self._refresh_message_count()

    # ==================================================================
    # 窗口与布局
    # ==================================================================
    def _setup_window(self) -> None:
        """设置窗口标题、尺寸、深色主题与关闭协议。"""
        self.root.title("微信好友消息监听器")
        self.root.configure(bg=W.BG)
        self.root.minsize(960, 620)
        width, height = 1220, 860
        try:
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
            x = max(0, (screen_w - width) // 2)
            y = max(0, (screen_h - height) // 2 - 20)
            self.root.geometry(f"{width}x{height}+{x}+{y}")
        except tk.TclError:
            self.root.geometry(f"{width}x{height}")
        W.setup_dark_style(self.root)

    def _build_ui(self) -> None:
        """构建整体布局：状态栏 / 上下可拖动分栏（主区 + 日志）。"""
        self.root.grid_rowconfigure(1, weight=1)
        self.root.grid_columnconfigure(0, weight=1)

        self.status_bar = tk.Frame(self.root, bg=W.BG_DARK)
        self.status_bar.grid(row=0, column=0, sticky="ew")
        self._build_status_bar()

        # 上下分栏：上半部分是左右分栏主区，下半部分是日志区（可拖动、有最小高度）
        self.vpaned = tk.PanedWindow(
            self.root,
            orient="vertical",
            bg=W.BORDER,
            sashwidth=6,
            sashrelief="flat",
            bd=0,
            opaqueresize=True,
        )
        self.vpaned.grid(row=1, column=0, sticky="nsew", padx=8, pady=8)

        self.upper = tk.Frame(self.vpaned, bg=W.BG)
        self.upper.grid_rowconfigure(0, weight=1)
        self.upper.grid_columnconfigure(0, weight=1)

        self.body = tk.Frame(self.upper, bg=W.BG)
        self.body.grid(row=0, column=0, sticky="nsew")
        self.body.grid_rowconfigure(0, weight=1)
        self.body.grid_columnconfigure(0, weight=1)

        self.paned = tk.PanedWindow(
            self.body,
            orient="horizontal",
            bg=W.BORDER,
            sashwidth=5,
            sashrelief="flat",
            bd=0,
            opaqueresize=True,
        )
        self.paned.grid(row=0, column=0, sticky="nsew")

        self.friend_panel = tk.Frame(self.paned, bg=W.PANEL)
        self.message_panel = tk.Frame(self.paned, bg=W.BG)
        self.paned.add(self.friend_panel, minsize=240, width=310)
        self.paned.add(self.message_panel, minsize=420, stretch="always")
        self._build_friend_panel(self.friend_panel)
        self._build_message_panel(self.message_panel)

        self.control_bar = tk.Frame(self.upper, bg=W.BG)
        self.control_bar.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self._build_control_bar()

        self.log_container = tk.Frame(self.vpaned, bg=W.BG)
        self.log_container.grid_rowconfigure(1, weight=1)
        self.log_container.grid_columnconfigure(0, weight=1)
        self._build_log_panel(self.log_container)

        # 日志区最小高度 100px，窗口缩小时优先压缩主区
        self.vpaned.add(self.upper, minsize=340, stretch="always")
        self.vpaned.add(self.log_container, minsize=100, height=170, stretch="never")

    def _build_status_bar(self) -> None:
        """构建顶部状态栏：连接状态、当前会话、监听状态；错误红字单独一行或省略。"""
        bar = self.status_bar
        bar.grid_columnconfigure(0, weight=1)

        top = tk.Frame(bar, bg=W.BG_DARK)
        top.grid(row=0, column=0, sticky="ew")

        self.conn_dot = tk.Label(
            top, text="●", bg=W.BG_DARK, fg=W.FG_MUTED, font=(W.FONT_FAMILY, 11)
        )
        self.conn_dot.pack(side="left", padx=(12, 4), pady=7)
        self.conn_label = tk.Label(
            top, text="微信未连接", bg=W.BG_DARK, fg=W.FG_DIM, font=W.FONT_UI_BOLD
        )
        self.conn_label.pack(side="left", pady=7)

        self.demo_badge = tk.Label(
            top,
            text="演示模式",
            bg=W.ACCENT_DARK,
            fg=W.ACCENT,
            font=W.FONT_TINY,
            padx=8,
            pady=2,
        )
        if self.demo_mode:
            self.demo_badge.pack(side="left", padx=(10, 0), pady=7)

        self._status_sep(top)

        tk.Label(top, text="当前会话：", bg=W.BG_DARK, fg=W.FG_MUTED, font=W.FONT_SMALL).pack(
            side="left", pady=7
        )
        self.chat_title_label = tk.Label(
            top, text="全部好友", bg=W.BG_DARK, fg=W.FG, font=W.FONT_UI_BOLD
        )
        self.chat_title_label.pack(side="left", pady=7)

        self._status_sep(top)

        tk.Label(top, text="监听：", bg=W.BG_DARK, fg=W.FG_MUTED, font=W.FONT_SMALL).pack(
            side="left", pady=7
        )
        self.listen_label = tk.Label(
            top, text="未监听", bg=W.BG_DARK, fg=W.FG_MUTED, font=W.FONT_UI
        )
        self.listen_label.pack(side="left", pady=7)

        # 错误红字放第二行：文本过长时自动换行，不会被左右内容挤掉
        self.error_label = tk.Label(
            bar,
            text="",
            bg=W.BG_DARK,
            fg=W.ERROR,
            font=W.FONT_SMALL,
            anchor="w",
            justify="left",
            wraplength=600,
        )
        bar.bind("<Configure>", self._on_status_bar_configure, add="+")

    def _status_sep(self, parent: tk.Widget) -> None:
        """在状态栏里插入一条竖向分隔线。"""
        tk.Frame(parent, bg=W.BORDER_SOFT, width=1, height=16).pack(
            side="left", padx=12, pady=8, fill="y"
        )

    def _on_status_bar_configure(self, event: tk.Event) -> None:
        """状态栏尺寸变化时按可视宽度重算错误文本的换行宽度。"""
        try:
            self.error_label.configure(wraplength=max(240, int(event.width) - 40))
        except (tk.TclError, AttributeError):
            pass

    def _build_friend_panel(self, parent: tk.Widget) -> None:
        """构建左侧好友区：总数、搜索、全选/清空/重新同步、可滚动好友列表。"""
        wrap = tk.Frame(parent, bg=W.PANEL)
        wrap.pack(fill="both", expand=True, padx=10, pady=10)

        header = tk.Frame(wrap, bg=W.PANEL)
        header.pack(fill="x")
        tk.Label(header, text="好友", bg=W.PANEL, fg=W.FG, font=W.FONT_TITLE).pack(side="left")
        self.total_label = tk.Label(
            header, text="好友总数: 0", bg=W.PANEL, fg=W.FG_DIM, font=W.FONT_SMALL
        )
        self.total_label.pack(side="right")

        search_row = tk.Frame(wrap, bg=W.PANEL)
        search_row.pack(fill="x", pady=(9, 0))
        tk.Label(search_row, text="🔍", bg=W.BUBBLE, fg=W.FG_DIM, font=W.FONT_SMALL, padx=7).pack(
            side="left"
        )
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_args: self._render_friend_rows())
        self.search_entry = ttk.Entry(
            search_row, textvariable=self.search_var, style="Dark.TEntry"
        )
        self.search_entry.pack(side="left", fill="x", expand=True)

        tools = tk.Frame(wrap, bg=W.PANEL)
        tools.pack(fill="x", pady=(7, 0))
        W.make_button(tools, "全选", self._select_all, "Ghost.TButton").pack(side="left")
        W.make_button(tools, "清空", self._clear_selection, "Ghost.TButton").pack(
            side="left", padx=6
        )
        W.make_button(tools, "重新同步好友", self._on_resync, "Ghost.TButton").pack(side="left")

        self.all_row = W.FriendRow(
            wrap,
            "全部好友",
            on_select=self.select_conversation,
            bg=W.PANEL,
            show_checkbox=False,
        )
        self.all_row.pack(fill="x", pady=(8, 2))
        self.all_row.set_selected(True)

        tk.Label(
            wrap,
            text="点击名字查看该好友消息；勾选决定监听对象",
            bg=W.PANEL,
            fg=W.FG_MUTED,
            font=W.FONT_TINY,
            anchor="w",
            justify="left",
            wraplength=280,
        ).pack(side="bottom", fill="x", pady=(6, 0))

        self.friends_scroll = W.ScrollFrame(wrap, bg=W.PANEL)
        self.friends_scroll.pack(fill="both", expand=True, pady=(2, 0))
        self.friends_scroll.set_empty_text("尚未同步好友，点击「重新同步好友」")

    def _build_message_panel(self, parent: tk.Widget) -> None:
        """构建右侧消息区：会话标题 + 可滚动消息卡片流。"""
        header = tk.Frame(parent, bg=W.BG)
        header.pack(fill="x", padx=10, pady=(8, 4))
        self.conv_title_label = tk.Label(
            header, text="全部好友", bg=W.BG, fg=W.FG, font=W.FONT_TITLE
        )
        self.conv_title_label.pack(side="left")
        self.msg_count_label = tk.Label(
            header, text="0 条消息", bg=W.BG, fg=W.FG_MUTED, font=W.FONT_SMALL
        )
        self.msg_count_label.pack(side="left", padx=(10, 0))
        W.make_button(
            header, "查看全部", lambda: self.select_conversation(None), "Ghost.TButton"
        ).pack(side="right")

        self.msg_scroll = W.ScrollFrame(parent, bg=W.BG)
        self.msg_scroll.pack(fill="both", expand=True, padx=(8, 4), pady=(0, 8))
        self.msg_scroll.set_empty_text("暂无消息，等待好友发来消息…")
        # Canvas 宽度变化时统一重算所有卡片的换行宽度（防横向溢出）
        self.msg_scroll.canvas.bind("<Configure>", self._on_message_canvas_configure, add="+")

    def _on_message_canvas_configure(self, event: tk.Event) -> None:
        """消息区宽度变化时延迟重排卡片文本，避免拖拽窗口时频繁重算。"""
        if self._reflow_after_id is not None:
            try:
                self.root.after_cancel(self._reflow_after_id)
            except tk.TclError:
                pass
            self._reflow_after_id = None
        width = int(event.width)
        self._reflow_after_id = self.root.after(80, lambda: self._reflow_cards(width))

    def _reflow_cards(self, width: int | None = None) -> None:
        """按当前 Canvas 可视宽度重算全部卡片文本换行宽度。"""
        self._reflow_after_id = None
        if self._closing:
            return
        try:
            available = int(width) if width else self.msg_scroll.canvas.winfo_width()
        except (TypeError, ValueError, tk.TclError):
            return
        for card in self._cards:
            try:
                card.set_available_width(available)
            except tk.TclError:
                pass

    def _build_control_bar(self) -> None:
        """构建底部控制栏：开始/停止监听、清空消息、打开媒体目录、日志折叠。"""
        bar = self.control_bar
        self.start_btn = W.make_button(
            bar, "开始监听", self._on_start_stop, style="Accent.TButton"
        )
        self.start_btn.pack(side="left")
        W.make_button(bar, "清空消息", self.clear_messages, "Dark.TButton").pack(
            side="left", padx=8
        )
        W.make_button(bar, "打开媒体目录", self._on_open_media, "Dark.TButton").pack(side="left")
        W.make_button(bar, "同步历史", self._on_sync_history, "Dark.TButton").pack(
            side="left", padx=(8, 0)
        )
        self.selected_label = tk.Label(
            bar, text="已勾选: 0 位", bg=W.BG, fg=W.FG_DIM, font=W.FONT_SMALL
        )
        self.selected_label.pack(side="right")
        self.log_toggle_btn = W.make_button(
            bar, "隐藏日志", self._toggle_log, "Ghost.TButton"
        )
        self.log_toggle_btn.pack(side="right", padx=(0, 8))

    def _build_log_panel(self, parent: tk.Widget) -> None:
        """构建底部日志区：按 info/warn/error 着色、可滚动，最小高度 100px。"""
        header = tk.Frame(parent, bg=W.BG)
        header.grid(row=0, column=0, sticky="ew")
        tk.Label(
            header, text="运行日志", bg=W.BG, fg=W.FG_DIM, font=W.FONT_UI_BOLD
        ).pack(side="left")
        W.make_button(header, "清空日志", self._clear_log, "Ghost.TButton").pack(side="right")

        self.log_pane = W.LogPane(parent, height=5)
        self.log_pane.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        self.log_pane.append("info", "界面已就绪：仅通过 queue + root.after(100) 消费后端事件。")

    def _toggle_log(self) -> None:
        """折叠/展开底部日志区（折叠后可通过控制栏按钮恢复）。"""
        if not hasattr(self, "vpaned") or not hasattr(self, "log_container"):
            return
        if self._log_collapsed:
            self.vpaned.add(self.log_container, minsize=100, height=170, stretch="never")
            self._log_collapsed = False
            self.log_toggle_btn.configure(text="隐藏日志")
        else:
            self.vpaned.forget(self.log_container)
            self._log_collapsed = True
            self.log_toggle_btn.configure(text="显示日志")

    def _clear_log(self) -> None:
        """清空底部日志区。"""
        if hasattr(self, "log_pane"):
            self.log_pane.clear()

    # ==================================================================
    # 生命周期
    # ==================================================================
    def start(self) -> None:
        """启动 100ms 事件泵并绑定窗口关闭事件。"""
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._update_selected_label()
        self.log("info", "界面已启动：事件泵每 100ms 消费一次后台事件队列。")
        if self.demo_mode:
            self.log("warn", "当前为假后端演示模式：不连接真实微信，仅用于界面演示/测试。")
        if self._demo_note:
            self.log("warn", self._demo_note)
        self._schedule_pump()

    def close(self) -> None:
        """关闭窗口：取消所有 after 回调、停止 GIF、停止后端并销毁窗口。"""
        if self._closing:
            return
        self._closing = True
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        if self._reflow_after_id is not None:
            try:
                self.root.after_cancel(self._reflow_after_id)
            except tk.TclError:
                pass
            self._reflow_after_id = None
        self._stop_animations()
        for preview in list(self._previews):
            try:
                preview.destroy()
            except tk.TclError:
                pass
        self._previews.clear()
        # 先销毁窗口，再同步停止后端（真实后端 stop 可能 join 数秒，
        # 放到销毁之后执行，用户看到的是窗口立刻关闭而不是界面卡住）。
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        try:
            self.backend.stop()
        except Exception:  # noqa: BLE001 - 关闭流程不允许抛异常
            pass

    def _schedule_pump(self) -> None:
        """安排下一次事件泵（100ms 后）。"""
        if self._closing:
            return
        try:
            self._after_id = self.root.after(self.PUMP_INTERVAL_MS, self.pump_events)
        except tk.TclError:
            self._after_id = None

    def pump_events(self) -> None:
        """消费后台事件队列（GUI 线程内，非阻塞），随后重新安排下一次。"""
        if self._closing:
            return
        if self._after_id is not None:
            try:
                self.root.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        handled = 0
        try:
            while handled < self.MAX_DRAIN_PER_TICK:
                try:
                    event = self.events.get_nowait()
                except queue.Empty:
                    break
                except Exception:  # noqa: BLE001 - 防御性：队列实现异常也不崩
                    break
                handled += 1
                try:
                    self.handle_event(event)
                except Exception as exc:  # noqa: BLE001 - 单条事件异常不影响后续
                    self.log("error", f"处理事件失败：{exc}")
        finally:
            if self._pending_scroll:
                self._pending_scroll = False
                self.msg_scroll.scroll_to_bottom()
            self._schedule_pump()

    # ==================================================================
    # 事件处理（SPEC 第 3 节）
    # ==================================================================
    def handle_event(self, event) -> None:
        """按 type 分发一条后端事件：status / message / friends / log。"""
        if not isinstance(event, dict):
            return
        event_type = event.get("type")
        data = event.get("data") or {}
        if event_type == "status":
            self._on_status_event(data)
        elif event_type == "message":
            self._on_message_event(data)
        elif event_type == "friends":
            self._on_friends_event(data)
        elif event_type == "log":
            self._on_log_event(data)
        elif event_type == "history":
            self._on_history_event(data)

    def _on_status_event(self, data: dict) -> None:
        """处理 status 事件：刷新连接、监听、当前会话与红色错误提示。"""
        if not isinstance(data, dict):
            return
        self._status = dict(data)
        found = bool(data.get("wechat_found"))
        running = bool(data.get("running"))
        monitoring = [str(name) for name in (data.get("monitoring") or []) if str(name).strip()]
        error = data.get("error")

        self.conn_dot.configure(fg=W.ACCENT if found else W.ERROR)
        self.conn_label.configure(
            text="微信已连接" if found else "微信未连接",
            fg=W.FG if found else W.FG_DIM,
        )

        if running and monitoring:
            shown = _elide(monitoring[0], 10)
            if len(monitoring) > 1:
                shown += " 等"
            self.listen_label.configure(
                text=f"监听中：{shown}（{len(monitoring)} 位）", fg=W.ACCENT
            )
            self._listening = True
            self.start_btn.configure(text="停止监听", style="Danger.TButton")
        elif running:
            self.listen_label.configure(text="后端运行中，未设置监听好友", fg=W.FG_DIM)
            self._listening = False
            self.start_btn.configure(text="开始监听", style="Accent.TButton")
        else:
            self.listen_label.configure(text="未监听", fg=W.FG_MUTED)
            self._listening = False
            self.start_btn.configure(text="开始监听", style="Accent.TButton")

        if error:
            error_text = str(error)
            self.show_error(error_text)
            if error_text != self._last_backend_error:
                self._last_backend_error = error_text
                self.log("error", error_text)
        else:
            self._last_backend_error = ""
            if found:
                self.show_error("")
            else:
                self.show_error("未检测到微信窗口：请打开微信桌面端，并确保窗口未最小化。")
        self._refresh_chat_title()

    def _on_message_event(self, data) -> None:
        """处理 message 事件：去重入库，并按当前会话决定是否立即显示。"""
        try:
            message = data if isinstance(data, Message) else Message.from_dict(data)
        except Exception as exc:  # noqa: BLE001
            self.log("warn", f"忽略无法解析的消息事件：{exc}")
            return
        if not message.friend:
            message.friend = "未知会话"
        if message.id in self._seen:
            return
        self._remember_seen(message.id)

        bucket = self._messages.setdefault(message.friend, [])
        bucket.append(message)
        if len(bucket) > self.MAX_PER_FRIEND:
            del bucket[: len(bucket) - self.MAX_PER_FRIEND]
        self._all_messages.append(message)
        if len(self._all_messages) > self.MAX_ALL_MESSAGES:
            del self._all_messages[: len(self._all_messages) - self.MAX_ALL_MESSAGES]

        if self._current is None or self._current == message.friend:
            self._append_card(message)
        else:
            count = self._unread.get(message.friend, 0) + 1
            self._unread[message.friend] = count
            row = self._friend_rows.get(message.friend)
            if row is not None:
                row.set_unread(count)
            self.all_row.set_unread(sum(self._unread.values()))
        self._refresh_message_count()

    def _remember_seen(self, message_id: str) -> None:
        """记录已显示的消息 id（带上限，防止内存无限增长）。"""
        self._seen.add(message_id)
        self._seen_order.append(message_id)
        while len(self._seen_order) > self.MAX_SEEN:
            old = self._seen_order.popleft()
            self._seen.discard(old)

    def _on_friends_event(self, data: dict) -> None:
        """处理 friends 事件：更新总数与好友列表。"""
        if not isinstance(data, dict):
            return
        friends: list[Friend] = []
        for item in data.get("friends") or []:
            try:
                if isinstance(item, Friend):
                    friends.append(item)
                elif isinstance(item, dict):
                    friends.append(Friend.from_dict(item))
            except Exception:  # noqa: BLE001
                continue
        self._friends = friends
        self._friend_names = [friend.name for friend in friends if friend.name]
        try:
            total = int(data.get("total") or len(self._friend_names))
        except (TypeError, ValueError):
            total = len(self._friend_names)
        total = max(total, len(self._friend_names))
        self.total_label.configure(text=f"好友总数: {total}")

        # 首次同步时恢复 config.json 里保存的监听名单（若好友仍存在）
        if not self._initial_checked_applied:
            self._initial_checked_applied = True
            configured = [
                str(name) for name in (self.config.get("monitored") or []) if str(name).strip()
            ]
            restored = [name for name in configured if name in self._friend_names]
            if restored:
                self._checked.update(restored)
                self.log("info", f"已恢复上次的监听名单：{'、'.join(restored)}")

        self._checked &= set(self._friend_names)
        self._render_friend_rows()
        self.log("info", f"好友列表已同步：共 {total} 位")

    def _on_log_event(self, data: dict) -> None:
        """处理 log 事件：写入底部日志区（按级别着色）。"""
        if not isinstance(data, dict):
            return
        self.log(str(data.get("level") or "info"), str(data.get("text") or ""))

    # ==================================================================
    # 好友列表与选择
    # ==================================================================
    def _visible_friend_names(self) -> list[str]:
        """按当前搜索关键词返回可见好友名（即时过滤）。"""
        try:
            keyword = self.search_var.get().strip().lower()
        except (AttributeError, tk.TclError):
            keyword = ""
        if not keyword:
            return list(self._friend_names)
        return [name for name in self._friend_names if keyword in name.lower()]

    MAX_RENDER_FRIENDS = 300

    def _render_friend_rows(self) -> None:
        """重建好友列表（搜索过滤后），并恢复勾选/未读/会话选中状态。

        只渲染前 MAX_RENDER_FRIENDS 行：好友很多时一次性建几千个 Tk 控件会把主线程卡死
        （实测 3000+ 行 → "未响应"）。超出部分用搜索框过滤后再显示。
        """
        if not hasattr(self, "friends_scroll"):
            return
        for row in self._friend_rows.values():
            try:
                row.destroy()
            except tk.TclError:
                pass
        self._friend_rows.clear()
        self.friends_scroll.clear()

        visible = self._visible_friend_names()
        if not visible:
            empty_text = (
                "没有匹配的好友"
                if self.search_var.get().strip()
                else "尚未同步好友，点击「重新同步好友」"
            )
            self.friends_scroll.set_empty_text(empty_text)
        else:
            shown = visible[: self.MAX_RENDER_FRIENDS]
            for name in shown:
                row = W.FriendRow(
                    self.friends_scroll.inner,
                    name,
                    checked=name in self._checked,
                    on_toggle=self._on_friend_toggle,
                    on_select=self.select_conversation,
                    bg=W.PANEL,
                )
                row.pack(fill="x", padx=2)
                row.set_unread(self._unread.get(name, 0))
                row.set_selected(self._current == name)
                self._friend_rows[name] = row
            if len(visible) > len(shown):
                tk.Label(
                    self.friends_scroll.inner,
                    text=f"还有 {len(visible) - len(shown)} 个，输入关键词搜索",
                    bg=W.PANEL, fg=W.FG_DIM, font=W.FONT_SMALL, anchor="w",
                ).pack(fill="x", padx=10, pady=(4, 8))
            self.friends_scroll.attach_wheel(self.friends_scroll.inner)

        self.all_row.set_selected(self._current is None)
        self.all_row.set_unread(sum(self._unread.values()))
        self._update_selected_label()

    def _on_friend_toggle(self, name: str, checked: bool) -> None:
        """好友复选框回调：维护监听勾选集合。"""
        if checked:
            self._checked.add(name)
        else:
            self._checked.discard(name)
        self._update_selected_label()
        self.log(
            "info",
            f"{'勾选' if checked else '取消勾选'}好友：{name}（已勾选 {len(self._checked)} 位）",
        )

    def _select_all(self) -> None:
        """全选当前可见（搜索过滤后）的好友。"""
        visible = self._visible_friend_names()
        if not visible:
            self.log("warn", "全选失败：当前没有可勾选的好友。")
            return
        self._checked.update(visible)
        self._render_friend_rows()
        self.log("info", f"已全选 {len(visible)} 位好友。")

    def _clear_selection(self) -> None:
        """清空所有好友勾选。"""
        self._checked.clear()
        self._render_friend_rows()
        self.log("info", "已清空好友勾选。")

    def _update_selected_label(self) -> None:
        """刷新「已勾选: N 位」提示。"""
        if hasattr(self, "selected_label"):
            self.selected_label.configure(text=f"已勾选: {len(self._checked)} 位")

    def set_checked(self, name: str, checked: bool = True) -> None:
        """外部/测试设置某个好友的勾选状态。"""
        if checked:
            self._checked.add(name)
        else:
            self._checked.discard(name)
        row = self._friend_rows.get(name)
        if row is not None:
            row.set_checked(checked)
        self._update_selected_label()

    def checked_names(self) -> list[str]:
        """按好友顺序返回已勾选的好友名列表。"""
        return [name for name in self._friend_names if name in self._checked]

    # ==================================================================
    # 会话切换与消息渲染
    # ==================================================================
    def select_conversation(self, name) -> None:
        """切换当前会话；name 为 None 或「全部好友」时显示全部消息。"""
        target = None if name in (None, "", "全部好友") else str(name)
        self._current = target
        if target is not None:
            self._unread[target] = 0
        self._refresh_chat_title()
        for row_name, row in self._friend_rows.items():
            row.set_selected(row_name == target)
        self.all_row.set_selected(target is None)
        self.all_row.set_unread(sum(self._unread.values()))
        self._render_messages()

    def _refresh_chat_title(self) -> None:
        """刷新顶部状态栏与消息区标题（当前会话，过长自动省略）。"""
        title = _elide(self._current or "全部好友", 16)
        if hasattr(self, "chat_title_label"):
            self.chat_title_label.configure(text=title)
        if hasattr(self, "conv_title_label"):
            self.conv_title_label.configure(text=title)

    def _render_messages(self) -> None:
        """重建当前会话的消息卡片视图（切换好友 / 清空时调用）。"""
        self._destroy_cards()
        self.msg_scroll.clear()
        if self._current is None:
            messages = self._all_messages[-self.MAX_CARDS:]
            empty_text = "暂无消息，等待好友发来消息…"
        else:
            messages = self._messages.get(self._current, [])
            empty_text = f"「{self._current}」暂无消息"
        if not messages:
            self.msg_scroll.set_empty_text(empty_text)
        else:
            for message in messages:
                self._append_card(message, scroll=False)
            self.msg_scroll.scroll_to_bottom()
        self._refresh_message_count()

    def _append_card(self, message: Message, scroll: bool = True) -> None:
        """向消息区追加一张卡片（超过上限时自动重建视图）。"""
        if len(self._cards) >= self.MAX_CARDS:
            self._render_messages()
            return
        try:
            card = W.MessageCard(
                self.msg_scroll.inner,
                message,
                show_friend=self._current is None,
                gif_registry=self._gifs,
                on_image_click=self._open_preview,
                media_base=W.ROOT,
            )
            card.pack(fill="x", padx=10, pady=(0, 6))
        except Exception as exc:  # noqa: BLE001 - 单条消息渲染失败不影响界面
            self.log("error", f"渲染消息卡片失败：{exc}")
            return
        self._cards.append(card)
        self.msg_scroll.hide_empty()
        self.msg_scroll.attach_wheel(card)
        try:
            card.set_available_width(self.msg_scroll.canvas.winfo_width())
        except tk.TclError:
            pass
        if scroll:
            self._pending_scroll = True
            self.msg_scroll.scroll_to_bottom()

    def _destroy_cards(self) -> None:
        """销毁当前视图所有卡片并停止其中动画。"""
        self._stop_animations()
        for card in self._cards:
            try:
                card.destroy()
            except tk.TclError:
                pass
        self._cards.clear()

    def _stop_animations(self) -> None:
        """停止所有 GIF 的 after 回调（切换会话/清空/关闭时调用）。"""
        for player in list(self._gifs):
            try:
                player.stop()
            except tk.TclError:
                pass
        self._gifs.clear()

    def _refresh_message_count(self) -> None:
        """刷新消息数量标签。"""
        if not hasattr(self, "msg_count_label"):
            return
        if self._current is None:
            count = len(self._all_messages)
        else:
            count = len(self._messages.get(self._current, []))
        self.msg_count_label.configure(text=f"{count} 条消息")

    def _open_preview(self, path: str, title: str = "查看大图") -> None:
        """点击图片时打开非模态大图预览窗口。"""
        try:
            preview = W.ImagePreviewWindow(self.root, path, title)
            self._previews.append(preview)
            self._previews = [item for item in self._previews if item.winfo_exists()]
        except Exception as exc:  # noqa: BLE001
            self.log("warn", f"打开图片预览失败：{exc}")

    # ==================================================================
    # 按钮回调
    # ==================================================================
    def _safe_backend(self, label: str, func, *args, **kwargs) -> bool:
        """安全调用后端方法：异常转日志与红色错误提示，不向 GUI 冒泡。"""
        try:
            func(*args, **kwargs)
            return True
        except Exception as exc:  # noqa: BLE001
            text = f"{label}失败：{exc}"
            self.log("error", text)
            self.show_error(text)
            return False

    def _stop_backend_async(self) -> None:
        """在后台线程停止后端（真实后端 stop 可能 join 数秒），避免 GUI 卡顿。"""

        def worker() -> None:
            """后台调用 backend.stop()，完成或失败都通过事件队列回报。"""
            try:
                self.backend.stop()
                self.events.put(
                    {
                        "type": "log",
                        "data": {"level": "info", "text": "后端采集线程已停止。"},
                    }
                )
            except Exception as exc:  # noqa: BLE001 - 后台异常转事件
                self.events.put(
                    {
                        "type": "log",
                        "data": {"level": "error", "text": f"停止后端失败：{exc}"},
                    }
                )

        threading.Thread(target=worker, name="wxbox-stop", daemon=True).start()

    def _on_start_stop(self) -> None:
        """开始/停止监听按钮：勾选好友 -> backend.start() + set_monitored()。"""
        if self._listening:
            self._safe_backend("停止监听", self.backend.set_monitored, [])
            self._listening = False
            self.start_btn.configure(text="开始监听", style="Accent.TButton")
            self.listen_label.configure(text="未监听", fg=W.FG_MUTED)
            self.log("info", "正在停止监听…")
            self._stop_backend_async()
            return

        names = self.checked_names()
        if not names:
            text = "请先在左侧勾选至少一个好友，再点击「开始监听」。"
            self.log("warn", text)
            self.show_error(text)
            return

        self.show_error("")
        if not self._safe_backend("启动后端线程", self.backend.start):
            return
        if not self._safe_backend("设置监听好友", self.backend.set_monitored, names):
            return
        self._listening = True
        self.start_btn.configure(text="停止监听", style="Danger.TButton")
        shown = _elide(names[0], 10)
        if len(names) > 1:
            shown += " 等"
        self.listen_label.configure(
            text=f"监听中：{shown}（{len(names)} 位）", fg=W.ACCENT
        )
        self.log("info", f"开始监听 {len(names)} 位好友：{'、'.join(names)}")

    def clear_messages(self) -> None:
        """清空全部消息记录与当前视图（同时停止所有 GIF 动画）。"""
        self._messages.clear()
        self._all_messages.clear()
        self._seen.clear()
        self._seen_order.clear()
        self._unread.clear()
        self._destroy_cards()
        self.msg_scroll.clear()
        empty_text = (
            "暂无消息，等待好友发来消息…"
            if self._current is None
            else f"「{self._current}」暂无消息"
        )
        self.msg_scroll.set_empty_text(empty_text)
        for row in self._friend_rows.values():
            row.set_unread(0)
        self.all_row.set_unread(0)
        self._refresh_message_count()
        self.log("info", "已清空消息记录与当前视图。")

    def _on_resync(self) -> None:
        """重新同步好友按钮：后台线程触发后端重新枚举好友，绝不阻塞 GUI。"""
        self.log("info", "正在重新同步好友列表…")

        def worker() -> None:
            """后台调用 list_friends(refresh=True)；失败时通过事件队列回报。"""
            try:
                self.backend.list_friends(True)
            except Exception as exc:  # noqa: BLE001 - 后台异常转事件，不碰 Tk
                self.events.put(
                    {
                        "type": "log",
                        "data": {"level": "error", "text": f"重新同步好友失败：{exc}"},
                    }
                )

        threading.Thread(target=worker, name="wxbox-resync", daemon=True).start()

    # ------------------------------------------------------------------
    # 同步历史聊天记录（临时数据：不写 log/媒体，关闭程序即清除）
    # ------------------------------------------------------------------
    def _on_sync_history(self) -> None:
        """「同步历史」按钮：选择条数 → 后台读取所选好友的历史记录。"""
        names = sorted(self._checked)
        if not names:
            messagebox.showinfo("提示", "请先在左侧勾选要同步历史记录的好友（可多选）。", parent=self.root)
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("同步历史聊天记录")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        dlg.grab_set()
        tk.Label(dlg, text=f"为已勾选的 {len(names)} 位好友同步最近：",
                 bg=W.BG, fg=W.FG).pack(anchor="w", padx=16, pady=(14, 8))
        var = tk.StringVar(value="20")
        grid = tk.Frame(dlg, bg=W.BG)
        grid.pack(padx=16, fill="x")
        options = [("5 条", "5"), ("10 条", "10"), ("20 条", "20"),
                   ("50 条", "50"), ("100 条", "100"), ("全部（本机缓存）", "0")]
        for i, (text, value) in enumerate(options):
            tk.Radiobutton(grid, text=text, value=value, variable=var,
                           bg=W.BG, fg=W.FG, selectcolor=W.PANEL,
                           activebackground=W.BG, activeforeground=W.FG,
                           highlightthickness=0, font=W.FONT_SMALL
                           ).grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 18), pady=3)
        tk.Label(dlg, text="提示：同步结果只显示在界面内存中，关闭程序后自动清除，不写入日志/媒体目录。",
                 bg=W.BG, fg=W.FG_DIM, font=W.FONT_SMALL, wraplength=380,
                 justify="left").pack(anchor="w", padx=16, pady=(10, 4))

        def confirm() -> None:
            raw = var.get()
            count = None if raw == "0" else int(raw)
            dlg.destroy()
            label = "全部（本机缓存）" if count is None else f"{count} 条"
            self.log("info", f"开始同步 {len(names)} 位好友的历史记录：{label}（临时数据，不落盘）")
            threading.Thread(target=self._sync_history_worker, args=(list(names), count),
                             name="wxbox-history", daemon=True).start()

        btns = tk.Frame(dlg, bg=W.BG)
        btns.pack(fill="x", padx=16, pady=(8, 14))
        W.make_button(btns, "确定", confirm, style="Accent.TButton").pack(side="right")
        W.make_button(btns, "取消", dlg.destroy, style="Dark.TButton").pack(side="right", padx=(0, 8))

    def _sync_history_worker(self, names: list[str], count) -> None:
        try:
            res = self.backend.sync_history(names, count)
            if res.get("error"):
                self.events.put({"type": "log", "data": {
                    "level": "warn", "text": f"同步历史失败：{res['error']}"}})
        except Exception as exc:  # noqa: BLE001 - 后台异常转事件
            self.events.put({"type": "log", "data": {
                "level": "error", "text": f"同步历史失败：{exc}"}})

    def _on_history_event(self, data: dict) -> None:
        if not isinstance(data, dict):
            return
        if data.get("error"):
            messagebox.showwarning("同步历史", str(data["error"]), parent=self.root)
            return
        short = data.get("short") or []
        if short:
            nl = chr(10)
            detail = nl.join(f"「{s['friend']}」只有 {s['available']} 条" for s in short)
            messagebox.showwarning(
                "没有这么多条聊天记录",
                "没有这么多条聊天记录：" + nl + detail + nl + nl + "已把实际可用的记录同步到界面。",
                parent=self.root,
            )
        elif int(data.get("synced") or 0) == 0:
            messagebox.showinfo("同步历史", "没有可同步的聊天记录。", parent=self.root)

    def _on_open_media(self) -> None:
        """打开 media 媒体目录（用系统资源管理器）。"""
        media_dir = W.MEDIA_DIR
        try:
            media_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.log("error", f"创建媒体目录失败：{exc}")
            self.show_error(f"创建媒体目录失败：{exc}")
            return
        try:
            if sys.platform.startswith("win") and hasattr(os, "startfile"):
                os.startfile(str(media_dir))  # type: ignore[attr-defined]
            else:
                subprocess.Popen(["xdg-open", str(media_dir)])
            self.log("info", f"已打开媒体目录：{media_dir}")
        except Exception as exc:  # noqa: BLE001
            self.log("error", f"打开媒体目录失败：{exc}")
            self.show_error(f"打开媒体目录失败：{exc}")

    def auto_demo_start(self, count: int = 3, attempt: int = 0) -> None:
        """演示模式：自动勾选前几位好友并开始监听（好友为空时短暂重试）。"""
        if self._closing:
            return
        if not self._friend_names:
            if attempt < 8:
                self.root.after(400, lambda: self.auto_demo_start(count, attempt + 1))
            else:
                self.log("warn", "演示启动：好友列表为空，请手动点击「重新同步好友」。")
            return
        self._checked = set(self._friend_names[: max(1, count)])
        self._render_friend_rows()
        self.log("info", f"演示模式：自动勾选 {'、'.join(sorted(self._checked))} 并开始监听。")
        if not self._listening:
            self._on_start_stop()

    # ==================================================================
    # 日志 / 错误提示 / 状态查询（供 app 与测试调用）
    # ==================================================================
    def log(self, level: str, text: str) -> None:
        """向底部日志区追加一行（按 info/warn/error 着色）。"""
        if hasattr(self, "log_pane"):
            self.log_pane.append(level, text)

    def log_text(self) -> str:
        """返回日志区全文（测试/自检用）。"""
        return self.log_pane.get_text() if hasattr(self, "log_pane") else ""

    def show_error(self, text: str) -> None:
        """在状态栏第二行显示红色错误文字；空字符串表示隐藏该行。"""
        if not hasattr(self, "error_label"):
            return
        text = str(text or "")
        self.error_label.configure(text=text)
        if text:
            self.error_label.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 7))
        else:
            self.error_label.grid_remove()

    def set_demo_mode(self, enabled: bool, note: str = "") -> None:
        """切换演示模式徽标，并可追加一条提示日志。"""
        self.demo_mode = bool(enabled)
        if hasattr(self, "demo_badge"):
            if self.demo_mode:
                self.demo_badge.pack(side="left", padx=(10, 0), pady=7)
            else:
                self.demo_badge.pack_forget()
        if note:
            self.log("warn", note)

    def _refresh_status_ui(self) -> None:
        """按缓存的 status 数据刷新顶部状态栏（初始化/手动刷新用）。"""
        if self._status:
            self._on_status_event(self._status)
        else:
            self.show_error("")

    # ---- 测试辅助 ----
    def message_card_count(self) -> int:
        """返回当前消息区卡片数量。"""
        return len(self._cards)

    def friend_row_count(self) -> int:
        """返回当前可见好友行数量（受搜索过滤影响）。"""
        return len(self._friend_rows)

    def active_animation_count(self) -> int:
        """返回正在播放的 GIF 数量（用于验证 after 回调已清理）。"""
        return len(self._gifs)

    def current_conversation(self) -> str | None:
        """返回当前会话名；None 表示「全部好友」。"""
        return self._current

    def card_kinds(self) -> list[str]:
        """返回当前视图所有卡片的类型标签（测试用）。"""
        return [getattr(card.message, "kind", "") for card in self._cards]

    def status_snapshot(self) -> dict:
        """返回本地缓存的最近一次 status 数据副本。"""
        return dict(self._status)

    def unread_counts(self) -> dict[str, int]:
        """返回各好友未读数副本（测试/调试用）。"""
        return dict(self._unread)
