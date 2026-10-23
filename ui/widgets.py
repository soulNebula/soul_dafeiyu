"""wx_box 界面通用组件与深色主题。

本模块只包含纯 Tkinter/ttk + Pillow 的展示组件，不包含任何采集逻辑；
所有方法都只允许在 GUI 线程内调用（由 main_window 负责）。
"""
from __future__ import annotations

import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from PIL import Image, ImageTk

from wx_core.models import (
    KIND_EMOJI,
    KIND_FILE,
    KIND_IMAGE,
    KIND_SYSTEM,
    KIND_TEXT,
    KIND_UNKNOWN,
    KIND_VOICE,
)

# ---------------------------------------------------------------------------
# 主题色 / 字体（深色，参考 #1e1f22 系）
# ---------------------------------------------------------------------------
BG = "#1e1f22"            # 主窗口背景
BG_DARK = "#16171a"       # 状态栏 / 日志区背景
PANEL = "#232428"         # 左侧面板
PANEL_ALT = "#2b2d31"     # 按钮 / 输入框
CARD = "#26282c"          # 对方消息卡片
CARD_SELF = "#243029"     # 我发出的消息卡片（轻微绿调）
BUBBLE = "#31343a"        # 对方文字气泡
BUBBLE_SELF = "#2e5b45"   # 我的文字气泡
BORDER = "#101114"        # 分隔条
BORDER_SOFT = "#3a3d43"   # 卡片描边
SELECTED_BG = "#31343b"   # 好友行选中背景
FG = "#e6e7ea"            # 主文字
FG_DIM = "#a8abb2"        # 次要文字
FG_MUTED = "#777b83"      # 弱化文字
ACCENT = "#3eb575"        # 微信绿
ACCENT_HOVER = "#4fd08a"
ACCENT_PRESSED = "#34a066"
ACCENT_DARK = "#2b4a3a"
ERROR = "#ff6b6b"
WARN = "#f0b74a"
INFO = "#8ab4f8"

FONT_FAMILY = "Microsoft YaHei UI"
FONT_UI = (FONT_FAMILY, 10)
FONT_UI_BOLD = (FONT_FAMILY, 10, "bold")
FONT_TITLE = (FONT_FAMILY, 12, "bold")
FONT_SMALL = (FONT_FAMILY, 9)
FONT_TINY = (FONT_FAMILY, 8)
FONT_ICON = ("Segoe UI Emoji", 16)
FONT_LOG = ("Consolas", 9)

ROOT = Path(__file__).resolve().parent.parent
MEDIA_DIR = ROOT / "media"

KIND_FG = {
    KIND_TEXT: INFO,
    KIND_IMAGE: "#c792ea",
    KIND_EMOJI: "#ffcb6b",
    KIND_FILE: "#89ddff",
    KIND_VOICE: "#f78c6c",
    KIND_SYSTEM: FG_MUTED,
    KIND_UNKNOWN: FG_DIM,
}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def resolve_media_path(path: str | Path, base: Path | None = None) -> Path:
    """把后端给的媒体相对路径解析为绝对路径（相对项目根目录）。"""
    p = Path(path)
    if not p.is_absolute():
        p = (base or ROOT) / p
    return p


def human_size(num_bytes: float) -> str:
    """把字节数格式化成人类可读的大小文本。"""
    size = float(max(0.0, num_bytes))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


def format_duration(value) -> str:
    """把 extra["duration"] 规范成「N秒」文本，避免出现「2秒 秒」这类重复。"""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if text.endswith("秒"):
        return text
    if text.lower().endswith("s"):
        text = text[:-1].strip()
    try:
        seconds = float(text)
    except (TypeError, ValueError):
        return text
    if seconds == int(seconds):
        return f"{int(seconds)}秒"
    return f"{seconds:g}秒"


def load_photo(path: str | Path, max_size: tuple[int, int]) -> ImageTk.PhotoImage:
    """加载图片并按最大尺寸等比缩放，返回可放进 Label 的 PhotoImage。"""
    with Image.open(path) as im:
        im.seek(0)
        frame = im.convert("RGBA")
        frame.thumbnail(max_size, Image.LANCZOS)
        return ImageTk.PhotoImage(frame)


def load_gif_frames(
    path: str | Path, max_size: tuple[int, int], max_frames: int = 48
) -> tuple[list[ImageTk.PhotoImage], list[int]]:
    """读取动图所有帧并缩放，返回 (PhotoImage 列表, 每帧时长毫秒)。"""
    frames: list[ImageTk.PhotoImage] = []
    durations: list[int] = []
    with Image.open(path) as im:
        total = int(getattr(im, "n_frames", 1) or 1)
        for index in range(min(total, max_frames)):
            im.seek(index)
            frame = im.convert("RGBA")
            frame.thumbnail(max_size, Image.LANCZOS)
            frames.append(ImageTk.PhotoImage(frame))
            try:
                duration = int(im.info.get("duration", 100) or 100)
            except (TypeError, ValueError):
                duration = 100
            durations.append(max(30, min(1000, duration)))
    if not frames:
        raise ValueError("动图没有可用帧")
    return frames, durations


def autowrap(label: tk.Label, container: tk.Widget, padding: int = 120) -> None:
    """让 label 的换行宽度随容器宽度自适应（避免写死像素）。"""
    state = {"width": 0}

    def _apply(event: tk.Event) -> None:
        """容器尺寸变化时按新宽度更新 label 的换行宽度。"""
        width = max(140, int(event.width) - padding)
        if abs(width - state["width"]) >= 8:
            state["width"] = width
            try:
                label.configure(wraplength=width)
            except tk.TclError:
                pass

    container.bind("<Configure>", _apply, add="+")


def make_button(
    master: tk.Widget,
    text: str,
    command,
    style: str = "Dark.TButton",
    width: int | None = None,
) -> ttk.Button:
    """创建一个深色风格按钮，可指定宽度。"""
    button = ttk.Button(master, text=text, command=command, style=style, takefocus=0)
    if width:
        button.configure(width=width)
    return button


def setup_dark_style(root: tk.Misc) -> ttk.Style:
    """初始化全局深色 ttk 主题（clam），返回 Style 对象。"""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(".", font=FONT_UI, background=BG, foreground=FG)
    style.configure("TFrame", background=BG)
    style.configure("Panel.TFrame", background=PANEL)

    # 普通按钮
    style.configure(
        "Dark.TButton",
        background=PANEL_ALT,
        foreground=FG,
        bordercolor=BORDER_SOFT,
        lightcolor=PANEL_ALT,
        darkcolor=PANEL_ALT,
        relief="flat",
        padding=(10, 5),
        width=0,
        font=FONT_UI,
        focusthickness=0,
    )
    style.map(
        "Dark.TButton",
        background=[("disabled", PANEL), ("pressed", "#3c3f46"), ("active", "#3a3e45")],
        foreground=[("disabled", FG_MUTED)],
        bordercolor=[("active", "#4c5058")],
    )

    # 主操作按钮（微信绿）
    style.configure(
        "Accent.TButton",
        background=ACCENT,
        foreground="#0d1a13",
        bordercolor=ACCENT,
        lightcolor=ACCENT,
        darkcolor=ACCENT,
        relief="flat",
        padding=(16, 6),
        width=0,
        font=FONT_UI_BOLD,
        focusthickness=0,
    )
    style.map(
        "Accent.TButton",
        background=[("disabled", "#2f4a3b"), ("pressed", ACCENT_PRESSED), ("active", ACCENT_HOVER)],
        foreground=[("disabled", "#7e9a8b")],
    )

    # 停止按钮（红）
    style.configure(
        "Danger.TButton",
        background="#a93b3b",
        foreground="#ffecec",
        bordercolor="#a93b3b",
        lightcolor="#a93b3b",
        darkcolor="#a93b3b",
        relief="flat",
        padding=(16, 6),
        width=0,
        font=FONT_UI_BOLD,
        focusthickness=0,
    )
    style.map(
        "Danger.TButton",
        background=[("disabled", "#4a2b2b"), ("pressed", "#8e3030"), ("active", "#c04a4a")],
        foreground=[("disabled", "#b08a8a")],
    )

    # 小号幽灵按钮
    style.configure(
        "Ghost.TButton",
        background=PANEL,
        foreground=FG_DIM,
        bordercolor=BORDER_SOFT,
        lightcolor=PANEL,
        darkcolor=PANEL,
        relief="flat",
        padding=(8, 3),
        width=0,
        font=FONT_SMALL,
        focusthickness=0,
    )
    style.map(
        "Ghost.TButton",
        background=[("pressed", PANEL_ALT), ("active", PANEL_ALT)],
        foreground=[("active", FG)],
    )

    # 输入框
    style.configure(
        "Dark.TEntry",
        fieldbackground=BUBBLE,
        foreground=FG,
        insertcolor=FG,
        bordercolor=BORDER_SOFT,
        lightcolor=BUBBLE,
        darkcolor=BUBBLE,
        padding=6,
        relief="flat",
    )
    style.map(
        "Dark.TEntry",
        bordercolor=[("focus", ACCENT)],
        lightcolor=[("focus", ACCENT)],
        darkcolor=[("focus", ACCENT)],
    )

    # 滚动条
    style.configure(
        "Vertical.TScrollbar",
        background=PANEL_ALT,
        troughcolor=BG_DARK,
        bordercolor=BG_DARK,
        arrowcolor=FG_DIM,
        lightcolor=PANEL_ALT,
        darkcolor=PANEL_ALT,
        relief="flat",
        arrowsize=12,
    )
    style.map(
        "Vertical.TScrollbar",
        background=[("active", "#4a4d55"), ("pressed", "#5a5e66")],
        arrowcolor=[("active", FG)],
    )

    # 复选按钮（好友行用自绘控件，这里保留给可能的 ttk 用法）
    style.configure(
        "Dark.TCheckbutton",
        background=PANEL,
        foreground=FG,
        indicatorbackground=PANEL_ALT,
        indicatorforeground=ACCENT,
        bordercolor=BORDER_SOFT,
        focuscolor=PANEL,
        font=FONT_UI,
    )
    style.map(
        "Dark.TCheckbutton",
        background=[("active", PANEL)],
        foreground=[("disabled", FG_MUTED)],
        indicatorbackground=[("selected", ACCENT), ("active", "#45484f")],
        indicatorforeground=[("selected", "#10231a")],
    )
    return style


# ---------------------------------------------------------------------------
# 自绘复选框（深色主题下 ttk indicator 在 clam 里可定制性差，这里自己画）
# ---------------------------------------------------------------------------
class CheckBox(tk.Canvas):
    """18×18 自绘复选框，支持选中态与回调。"""

    SIZE = 18

    def __init__(self, master: tk.Widget, checked: bool = False, command=None, bg: str = PANEL):
        """创建 18×18 复选框，bg 需与所在行背景一致。"""
        super().__init__(
            master,
            width=self.SIZE,
            height=self.SIZE,
            bg=bg,
            highlightthickness=0,
            bd=0,
            cursor="hand2",
            takefocus=0,
        )
        self._checked = bool(checked)
        self._command = command
        self._bg = bg
        self._draw()
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", lambda _e: self._draw(hover=True))
        self.bind("<Leave>", lambda _e: self._draw(hover=False))

    def _draw(self, hover: bool = False) -> None:
        """按当前状态重绘复选框。"""
        self.delete("all")
        if self._checked:
            fill, outline = ACCENT, ACCENT
        elif hover:
            fill, outline = "#3a3e45", "#5a5f68"
        else:
            fill, outline = PANEL_ALT, "#5a5f68"
        self.create_rectangle(2, 2, self.SIZE - 1, self.SIZE - 1, fill=fill, outline=outline)
        if self._checked:
            self.create_text(
                self.SIZE / 2,
                self.SIZE / 2 - 1,
                text="✓",
                fill="#0d1a13",
                font=(FONT_FAMILY, 10, "bold"),
            )

    def _on_click(self, _event: tk.Event) -> None:
        """点击时切换选中态并触发回调。"""
        self.set_checked(not self._checked, notify=True)

    def set_checked(self, checked: bool, notify: bool = False) -> None:
        """设置选中状态；notify=True 时触发回调。"""
        checked = bool(checked)
        if checked == self._checked:
            return
        self._checked = checked
        self._draw()
        if notify and self._command is not None:
            self._command(self._checked)

    def is_checked(self) -> bool:
        """返回当前是否选中。"""
        return self._checked


# ---------------------------------------------------------------------------
# 滚动容器（Canvas + Frame）
# ---------------------------------------------------------------------------
class ScrollFrame(tk.Frame):
    """竖向滚动的 Canvas+Frame 容器，供好友列表与消息流复用。"""

    def __init__(self, master: tk.Widget, bg: str = BG, **kwargs):
        """创建滚动容器：Canvas + 内部 Frame + 深色滚动条。"""
        super().__init__(master, bg=bg, **kwargs)
        self._bg = bg
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, takefocus=0)
        self.vsb = tk.Scrollbar(
            self,
            orient="vertical",
            command=self.canvas.yview,
            bg="#3a3d44",
            troughcolor="#17181b",
            activebackground="#4a4e56",
            highlightthickness=0,
            bd=0,
            relief="flat",
            width=10,
            elementborderwidth=0,
            takefocus=0,
        )
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vsb.grid(row=0, column=1, sticky="ns")
        self._empty_label: tk.Label | None = None
        self._wheel_widgets: set[tk.Widget] = set()

        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.attach_wheel(self.canvas)
        self.attach_wheel(self.inner)

    # ---- 滚动行为 ----
    def _on_inner_configure(self, _event: tk.Event) -> None:
        """内层内容变化时刷新滚动区域。"""
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event: tk.Event) -> None:
        """画布尺寸变化时让内层 Frame 跟随宽度；原本贴底则继续贴底。"""
        try:
            was_at_bottom = self.canvas.yview()[1] >= 0.999
        except tk.TclError:
            was_at_bottom = False
        self.canvas.itemconfigure(self._window_id, width=event.width)
        if was_at_bottom:
            self.canvas.after_idle(self.scroll_to_bottom)

    def attach_wheel(self, widget: tk.Widget) -> None:
        """给新增控件（含子孙）绑定滚轮事件，避免滚轮失效。"""
        if widget is None:
            return
        if widget not in self._wheel_widgets:
            self._wheel_widgets.add(widget)
            widget.bind("<MouseWheel>", self._on_mousewheel, add="+")
        for child in widget.winfo_children():
            self.attach_wheel(child)

    def _on_mousewheel(self, event: tk.Event) -> str:
        """鼠标滚轮滚动消息/好友列表。"""
        delta = int(-1 * (event.delta / 120)) if event.delta else 0
        if delta:
            self.canvas.yview_scroll(delta, "units")
        return "break"

    def scroll_to_bottom(self) -> None:
        """滚动到最底部（新消息到达时调用）。"""
        try:
            self.canvas.update_idletasks()
            self.canvas.yview_moveto(1.0)
        except tk.TclError:
            pass

    def set_empty_text(self, text: str) -> None:
        """显示居中的空状态提示。"""
        if self._empty_label is None or not self._empty_label.winfo_exists():
            self._empty_label = tk.Label(
                self.inner,
                text=text,
                bg=self._bg,
                fg=FG_MUTED,
                font=FONT_UI,
                justify="center",
                pady=48,
            )
        else:
            self._empty_label.configure(text=text)
        self._empty_label.pack(fill="x")

    def hide_empty(self) -> None:
        """隐藏空状态提示。"""
        if self._empty_label is not None and self._empty_label.winfo_exists():
            self._empty_label.pack_forget()

    def clear(self) -> None:
        """销毁除空状态提示外的所有子控件。"""
        for child in list(self.inner.winfo_children()):
            if child is self._empty_label:
                continue
            child.destroy()
        self.hide_empty()
        self._wheel_widgets = {self.canvas, self.inner}
        self.canvas.configure(scrollregion=(0, 0, 0, 0))


# ---------------------------------------------------------------------------
# GIF 循环播放控件
# ---------------------------------------------------------------------------
class GifPlayer(tk.Label):
    """循环播放多帧 GIF 的 Label；stop() 会取消 after 回调并注销自己。"""

    def __init__(
        self,
        master: tk.Widget,
        frames: list[ImageTk.PhotoImage],
        durations: list[int],
        *,
        registry: set["GifPlayer"] | None = None,
        bg: str = CARD,
        cursor: str = "hand2",
    ):
        """创建 GIF 播放器并立即开始循环（单帧动图自动跳过播放）。"""
        super().__init__(master, image=frames[0], bd=0, bg=bg, cursor=cursor)
        self._frames = frames
        self._durations = durations or [100] * len(frames)
        self._registry = registry
        self._index = 0
        self._after_id: str | None = None
        self._running = False
        if self._registry is not None:
            self._registry.add(self)
        self.start()

    def start(self) -> None:
        """开始（或恢复）循环播放。"""
        if self._running or len(self._frames) < 2:
            return
        self._running = True
        self._schedule_next()

    def _schedule_next(self) -> None:
        """安排下一帧的 after 回调。"""
        if not self._running:
            return
        try:
            delay = self._durations[self._index % len(self._durations)]
            self._after_id = self.after(delay, self._next_frame)
        except tk.TclError:
            self._running = False

    def _next_frame(self) -> None:
        """切换到下一帧并继续循环。"""
        self._after_id = None
        if not self._running:
            return
        self._index = (self._index + 1) % len(self._frames)
        try:
            self.configure(image=self._frames[self._index])
        except tk.TclError:
            self._running = False
            return
        self._schedule_next()

    def stop(self) -> None:
        """停止播放并取消挂起的 after 回调（防止内存泄漏）。"""
        self._running = False
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        if self._registry is not None:
            self._registry.discard(self)

    def is_playing(self) -> bool:
        """返回是否正在播放。"""
        return self._running

    def destroy(self) -> None:
        """销毁前先停止动画。"""
        self.stop()
        super().destroy()


# ---------------------------------------------------------------------------
# 大图预览窗口
# ---------------------------------------------------------------------------
class ImagePreviewWindow(tk.Toplevel):
    """点击消息里的图片/表情包时弹出的非模态大图预览。"""

    def __init__(self, master: tk.Misc, image_path: str | Path, title: str = "查看大图"):
        """打开大图预览窗口（非模态，不阻塞主窗口）。"""
        super().__init__(master)
        self.withdraw()
        self.title(title or "查看大图")
        self.configure(bg=BG_DARK)
        self.resizable(False, False)
        self._photo: ImageTk.PhotoImage | None = None
        self._path = Path(image_path)

        body = tk.Frame(self, bg=BG_DARK)
        body.pack(fill="both", expand=True, padx=12, pady=12)

        info_text = self._path.name
        dimension_text = ""
        try:
            with Image.open(self._path) as im:
                im.seek(0)
                dimension_text = f"{im.width}×{im.height}"
                screen_w = max(320, self.winfo_screenwidth() - 160)
                screen_h = max(240, self.winfo_screenheight() - 220)
                preview = im.convert("RGBA")
                preview.thumbnail((screen_w, screen_h), Image.LANCZOS)
                self._photo = ImageTk.PhotoImage(preview)
            image_label = tk.Label(body, image=self._photo, bg=BG_DARK, cursor="hand2")
            image_label.pack()
            image_label.bind("<Button-1>", lambda _e: self.close())
        except Exception as exc:  # noqa: BLE001 - 预览失败不应影响主界面
            tk.Label(
                body,
                text=f"无法预览图片：\n{exc}",
                bg=BG_DARK,
                fg=ERROR,
                font=FONT_UI,
                justify="center",
                wraplength=480,
            ).pack(padx=20, pady=20)

        caption = f"{info_text}"
        if dimension_text:
            caption += f" · {dimension_text}"
        caption += "（点击图片或按 Esc 关闭）"
        tk.Label(body, text=caption, bg=BG_DARK, fg=FG_DIM, font=FONT_SMALL).pack(pady=(8, 0))

        self.bind("<Escape>", lambda _e: self.close())
        self.bind("<Button-1>", lambda _e: self.close())
        self.transient(master.winfo_toplevel() if hasattr(master, "winfo_toplevel") else master)
        self.deiconify()
        self._center_on_parent(master)

    def _center_on_parent(self, master: tk.Misc) -> None:
        """把预览窗口居中到主窗口上。"""
        self.update_idletasks()
        try:
            parent = master.winfo_toplevel()
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
        except tk.TclError:
            return
        width, height = self.winfo_width(), self.winfo_height()
        x = max(0, px + (pw - width) // 2)
        y = max(0, py + (ph - height) // 2)
        self.geometry(f"+{x}+{y}")

    def close(self) -> None:
        """关闭预览窗口。"""
        try:
            self.destroy()
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------
# 日志面板
# ---------------------------------------------------------------------------
class LogPane(tk.Frame):
    """按 info/warn/error 级别着色的滚动日志区。"""

    LEVEL_COLORS = {"info": INFO, "warn": WARN, "error": ERROR}

    def __init__(self, master: tk.Widget, height: int = 7):
        """创建日志面板，height 为可见行数。"""
        super().__init__(master, bg=BG_DARK, highlightthickness=1, highlightbackground=BORDER)
        self.text = tk.Text(
            self,
            height=height,
            bg=BG_DARK,
            fg=FG_DIM,
            insertbackground=FG,
            selectbackground=SELECTED_BG,
            relief="flat",
            bd=0,
            wrap="word",
            state="disabled",
            font=FONT_LOG,
            padx=8,
            pady=4,
            spacing1=1,
        )
        self.vsb = tk.Scrollbar(
            self,
            orient="vertical",
            command=self.text.yview,
            bg="#3a3d44",
            troughcolor="#17181b",
            activebackground="#4a4e56",
            highlightthickness=0,
            bd=0,
            relief="flat",
            width=10,
            elementborderwidth=0,
            takefocus=0,
        )
        self.text.configure(yscrollcommand=self.vsb.set)
        self.text.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")
        self.text.tag_configure("time", foreground=FG_MUTED)
        for level, color in self.LEVEL_COLORS.items():
            self.text.tag_configure(level, foreground=color)
        self.max_lines = 800

    def append(self, level: str, text: str) -> None:
        """追加一行日志，并按级别着色、自动裁剪到最大行数。"""
        level = level if level in self.LEVEL_COLORS else "info"
        stamp = time.strftime("%H:%M:%S")
        try:
            self.text.configure(state="normal")
            self.text.insert("end", f"[{stamp}] ", "time")
            self.text.insert("end", str(text).rstrip() + "\n", level)
            line_count = int(str(self.text.index("end-1c")).split(".")[0])
            if line_count > self.max_lines:
                self.text.delete("1.0", f"{line_count - self.max_lines}.0")
            self.text.see("end")
            self.text.configure(state="disabled")
        except tk.TclError:
            pass

    def clear(self) -> None:
        """清空日志内容。"""
        try:
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.configure(state="disabled")
        except tk.TclError:
            pass

    def get_text(self) -> str:
        """返回日志全文（测试用）。"""
        try:
            return self.text.get("1.0", "end-1c")
        except tk.TclError:
            return ""


# ---------------------------------------------------------------------------
# 好友行
# ---------------------------------------------------------------------------
class FriendRow(tk.Frame):
    """好友列表里的一行：复选框 + 名称 + 未读徽标，点击名称切换会话。"""

    def __init__(
        self,
        master: tk.Widget,
        name: str,
        *,
        checked: bool = False,
        on_toggle=None,
        on_select=None,
        bg: str = PANEL,
        show_checkbox: bool = True,
    ):
        """创建好友行；show_checkbox=False 时用于「全部好友」行。"""
        super().__init__(master, bg=bg, cursor="hand2")
        self.name = name
        self._on_toggle = on_toggle
        self._on_select = on_select
        self._bg = bg
        self._selected = False
        self._unread = 0
        self._show_checkbox = bool(show_checkbox)

        self._bar = tk.Frame(self, bg=bg, width=3)
        self._bar.pack(side="left", fill="y")
        self._bar.pack_propagate(False)

        self.checkbox = None
        if self._show_checkbox:
            self.checkbox = CheckBox(self, checked=checked, command=self._handle_toggle, bg=bg)
            self.checkbox.pack(side="left", padx=(8, 8), pady=7)

        self.name_label = tk.Label(
            self, text=name, bg=bg, fg=FG, font=FONT_UI, anchor="w", justify="left"
        )
        self.name_label.pack(side="left", fill="x", expand=True, padx=(8, 4), pady=6)

        self.unread_label = tk.Label(
            self,
            text="",
            bg=ACCENT,
            fg="#0d1a13",
            font=FONT_TINY,
            padx=5,
            pady=0,
        )

        for widget in (self, self.name_label):
            widget.bind("<Button-1>", self._handle_select)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _handle_toggle(self, checked: bool) -> None:
        """复选框变化时回调主窗口。"""
        if self._on_toggle is not None:
            self._on_toggle(self.name, checked)

    def _handle_select(self, _event: tk.Event) -> None:
        """点击行时切换到该好友的会话。"""
        if self._on_select is not None:
            self._on_select(self.name)

    def _on_enter(self, _event: tk.Event) -> None:
        """鼠标移入时高亮背景。"""
        if not self._selected:
            self._paint("#2a2d32")

    def _on_leave(self, _event: tk.Event) -> None:
        """鼠标移出时恢复背景。"""
        self._paint(SELECTED_BG if self._selected else self._bg)

    def _paint(self, color: str) -> None:
        """给整行（含子控件）应用背景色。"""
        try:
            self.configure(bg=color)
            for child in (self._bar, self.name_label):
                child.configure(bg=color)
            if self.checkbox is not None:
                self.checkbox._bg = color  # noqa: SLF001 - 同模块内部约定
                self.checkbox.configure(bg=color)
                self.checkbox._draw()  # noqa: SLF001
        except tk.TclError:
            pass

    def set_selected(self, selected: bool) -> None:
        """设置选中高亮（左侧绿条 + 背景）。"""
        self._selected = bool(selected)
        self._bar.configure(bg=ACCENT if self._selected else self._bg)
        self.name_label.configure(fg=FG if self._selected else FG)
        self._paint(SELECTED_BG if self._selected else self._bg)

    def set_unread(self, count: int) -> None:
        """更新未读徽标。"""
        self._unread = int(count)
        if self._unread > 0:
            self.unread_label.configure(text=f" {self._unread} ")
            self.unread_label.pack(side="right", padx=(4, 10), pady=6)
        else:
            self.unread_label.pack_forget()

    def set_checked(self, checked: bool) -> None:
        """外部设置选中状态（不触发回调）。"""
        if self.checkbox is not None:
            self.checkbox.set_checked(checked, notify=False)

    def is_checked(self) -> bool:
        """返回该好友是否被勾选。"""
        return bool(self.checkbox is not None and self.checkbox.is_checked())


# ---------------------------------------------------------------------------
# 消息卡片
# ---------------------------------------------------------------------------
class MessageCard(tk.Frame):
    """一条消息卡片：发送者/时间/类型标签 + 按类型渲染的内容。"""

    def __init__(
        self,
        master: tk.Widget,
        message,
        *,
        show_friend: bool = False,
        gif_registry: set | None = None,
        on_image_click=None,
        media_base: Path | None = None,
        bg: str | None = None,
    ):
        """创建消息卡片；gif_registry 用于统一管理 GIF 的 after 回调。"""
        self.message = message
        self._is_self = getattr(message, "sender", "friend") == "self"
        self._card_bg = bg or (CARD_SELF if self._is_self else CARD)
        super().__init__(
            master,
            bg=self._card_bg,
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER_SOFT,
            highlightcolor=BORDER_SOFT,
        )
        self._show_friend = show_friend
        self._gif_registry = gif_registry if gif_registry is not None else set()
        self._on_image_click = on_image_click
        self._media_base = Path(media_base) if media_base else ROOT
        self._gifs: list[GifPlayer] = []
        self._photos: list[ImageTk.PhotoImage] = []
        self._wrap_labels: list[tuple[tk.Label, int]] = []
        self._build()

    # ---- 构建 ----
    def _build(self) -> None:
        """构建卡片整体布局。"""
        pad = tk.Frame(self, bg=self._card_bg)
        pad.pack(fill="both", expand=True, padx=12, pady=8)
        self._build_header(pad)
        body = tk.Frame(pad, bg=self._card_bg)
        body.pack(fill="x", pady=(6, 0))

        kind = getattr(self.message, "kind", KIND_UNKNOWN)
        if kind == KIND_VOICE:
            self._build_voice(body)
        elif kind == KIND_FILE:
            self._build_file(body)
        elif kind in (KIND_IMAGE, KIND_EMOJI):
            self._build_media(body, kind)
        elif kind == KIND_SYSTEM:
            self._build_system(body)
        else:
            self._build_text(body)

    def _build_header(self, parent: tk.Widget) -> None:
        """构建卡片头部：发送者标签、好友名、时间、类型标签。"""
        row = tk.Frame(parent, bg=self._card_bg)
        row.pack(fill="x")

        sender_text = "我" if self._is_self else "对方"
        sender_bg = ACCENT_DARK if self._is_self else "#34363c"
        sender_fg = "#d9f7e6" if self._is_self else FG_DIM
        tk.Label(
            row,
            text=sender_text,
            bg=sender_bg,
            fg=sender_fg,
            font=FONT_SMALL,
            padx=6,
            pady=1,
        ).pack(side="left")

        if self._show_friend and getattr(self.message, "friend", ""):
            tk.Label(
                row,
                text=f"· {self.message.friend}",
                bg=self._card_bg,
                fg=FG_DIM,
                font=FONT_SMALL,
            ).pack(side="left", padx=(6, 0))

        kind = getattr(self.message, "kind", KIND_UNKNOWN)
        tk.Label(
            row,
            text=getattr(self.message, "kind_label", kind),
            bg=BUBBLE,
            fg=KIND_FG.get(kind, FG_DIM),
            font=FONT_SMALL,
            padx=6,
            pady=1,
        ).pack(side="right")
        tk.Label(
            row,
            text=getattr(self.message, "time_text", ""),
            bg=self._card_bg,
            fg=FG_MUTED,
            font=FONT_TINY,
        ).pack(side="right", padx=(0, 8))

    def _build_text(self, parent: tk.Widget) -> None:
        """渲染文字消息气泡（自动换行，自己发的靠右）。"""
        bubble_bg = BUBBLE_SELF if self._is_self else BUBBLE
        label = tk.Label(
            parent,
            text=getattr(self.message, "text", "") or "（空消息）",
            bg=bubble_bg,
            fg=FG,
            font=FONT_UI,
            justify="left",
            anchor="w",
            padx=12,
            pady=8,
            wraplength=320,
        )
        label.pack(side="right" if self._is_self else "left")
        self._wrap_labels.append((label, 120))
        autowrap(label, self, padding=120)

    def set_available_width(self, available_width: int) -> None:
        """按消息区 Canvas 可视宽度重算文本换行宽度（防止横向溢出）。"""
        try:
            card_width = max(180, int(available_width) - 20)
        except (TypeError, ValueError):
            return
        for label, padding in self._wrap_labels:
            try:
                label.configure(wraplength=max(140, card_width - padding))
            except tk.TclError:
                pass

    def _media_path(self, kind: str) -> Path:
        """取出并解析消息里的媒体文件路径。"""
        raw = getattr(self.message, "media_path", "") or getattr(self.message, "thumb_path", "")
        return resolve_media_path(raw, self._media_base)

    def _build_media(self, parent: tk.Widget, kind: str) -> None:
        """渲染图片/表情包：等比缩放、居中显示，GIF 循环播放，点击弹大图。"""
        path = self._media_path(kind)
        animated = bool(getattr(self.message, "extra", {}).get("animated")) or (
            path.suffix.lower() == ".gif"
        )
        # 表情包最大 160×160；普通图片最大 320 宽（不放大，等比缩放）
        max_size = (160, 160) if kind == KIND_EMOJI else (320, 260)
        holder = tk.Frame(parent, bg=self._card_bg)
        holder.pack(fill="x")

        rendered = False
        if path.is_file():
            try:
                if animated and path.suffix.lower() == ".gif":
                    frames, durations = load_gif_frames(path, max_size)
                    if len(frames) > 1:
                        player = GifPlayer(
                            holder,
                            frames,
                            durations,
                            registry=self._gif_registry,
                            bg=self._card_bg,
                        )
                        player.pack(anchor="center")
                        player.bind(
                            "<Button-1>",
                            lambda _e, p=path: self._open_preview(p, "表情包预览"),
                            add="+",
                        )
                        self._gifs.append(player)
                        rendered = True
                    else:
                        photo = load_photo(path, max_size)
                        self._photos.append(photo)
                        label = tk.Label(holder, image=photo, bg=self._card_bg, cursor="hand2")
                        label.pack(anchor="center")
                        label.bind(
                            "<Button-1>",
                            lambda _e, p=path: self._open_preview(p, "表情包预览"),
                        )
                        rendered = True
                else:
                    photo = load_photo(path, max_size)
                    self._photos.append(photo)
                    label = tk.Label(holder, image=photo, bg=self._card_bg, cursor="hand2")
                    label.pack(anchor="center")
                    label.bind(
                        "<Button-1>",
                        lambda _e, p=path: self._open_preview(
                            p, "表情包预览" if kind == KIND_EMOJI else "图片预览"
                        ),
                    )
                    rendered = True
            except Exception:  # noqa: BLE001 - 损坏媒体不应影响界面
                rendered = False

        if not rendered:
            text = (
                "🖼 图片（媒体文件缺失）"
                if path and not path.is_file()
                else "🖼 图片（无法加载预览）"
            )
            tk.Label(
                holder,
                text=text,
                bg=BUBBLE,
                fg=FG_DIM,
                font=FONT_SMALL,
                padx=12,
                pady=10,
                justify="center",
            ).pack(anchor="center")

        if rendered:
            tk.Label(
                holder,
                text="点击查看大图",
                bg=self._card_bg,
                fg=FG_MUTED,
                font=FONT_TINY,
            ).pack(anchor="center", pady=(2, 0))

    def _build_file(self, parent: tk.Widget) -> None:
        """渲染文件卡片：📄 图标 + 文件名 + 大小。"""
        path = self._media_path(KIND_FILE)
        size_text = str(getattr(self.message, "extra", {}).get("size_text", "") or "")
        if not size_text and path.is_file():
            try:
                size_text = human_size(path.stat().st_size)
            except OSError:
                size_text = ""
        name = getattr(self.message, "text", "") or "未命名文件"

        box = tk.Frame(
            parent,
            bg=BUBBLE,
            padx=12,
            pady=10,
            highlightthickness=1,
            highlightbackground=BORDER_SOFT,
        )
        box.pack(anchor="e" if self._is_self else "w")
        tk.Label(box, text="📄", bg=BUBBLE, fg=INFO, font=FONT_ICON).pack(
            side="left", padx=(0, 10)
        )
        info = tk.Frame(box, bg=BUBBLE)
        info.pack(side="left", fill="x", expand=True)
        name_label = tk.Label(
            info,
            text=name,
            bg=BUBBLE,
            fg=FG,
            font=FONT_UI_BOLD,
            anchor="w",
            justify="left",
            wraplength=320,
        )
        name_label.pack(fill="x", anchor="w")
        tk.Label(
            info,
            text=f"文件 · {size_text}" if size_text else "文件",
            bg=BUBBLE,
            fg=FG_DIM,
            font=FONT_SMALL,
            anchor="w",
        ).pack(fill="x", anchor="w", pady=(2, 0))
        self._wrap_labels.append((name_label, 180))
        autowrap(name_label, self, padding=180)

    def _build_voice(self, parent: tk.Widget) -> None:
        """渲染语音提示（只提示不接收）。"""
        box = tk.Frame(parent, bg=BUBBLE, padx=12, pady=8)
        box.pack(anchor="e" if self._is_self else "w")
        tk.Label(
            box,
            text="🎤 语音消息（未接收）",
            bg=BUBBLE,
            fg=WARN,
            font=FONT_UI,
        ).pack(side="left")
        duration_text = format_duration(getattr(self.message, "extra", {}).get("duration"))
        if duration_text:
            tk.Label(
                box,
                text=f" · {duration_text}",
                bg=BUBBLE,
                fg=FG_DIM,
                font=FONT_SMALL,
            ).pack(side="left")

    def _build_system(self, parent: tk.Widget) -> None:
        """渲染系统消息（居中弱化显示）。"""
        tk.Label(
            parent,
            text=getattr(self.message, "text", "") or "系统消息",
            bg=self._card_bg,
            fg=FG_MUTED,
            font=FONT_SMALL,
            justify="center",
            wraplength=480,
        ).pack(anchor="center")

    def _open_preview(self, path: Path, title: str) -> None:
        """点击图片时打开大图预览（非阻塞）。"""
        if self._on_image_click is not None:
            try:
                self._on_image_click(str(path), title)
            except Exception:  # noqa: BLE001 - 预览失败不影响主界面
                pass

    def destroy(self) -> None:
        """销毁卡片前停止它内部的 GIF 动画。"""
        for player in list(self._gifs):
            try:
                player.stop()
            except tk.TclError:
                pass
        self._gifs.clear()
        super().destroy()
