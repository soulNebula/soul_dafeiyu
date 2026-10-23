import os
import queue
import threading
import tkinter
import tkinter.font
import tkinter.messagebox
import tkinter.ttk
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
import tkcalendar
from decrypter.db_decrypt import DatabaseDecrypter
from decrypter.image_decrypt import ImageDecrypter
from decrypter.video_decrypt import VideoDecrypter
from gui.auto_scroll_guide import AutoScrollGuide
from gui.auto_scrolls_single_guide import AutoScrollSingleGuide
from gui.tool_tip import ToolTip
from entity.contact import Contact
from exporter.html_exporter import HtmlExporter
from gui.listbox_with_search import ListboxWithSearch

# 4.x 安全开关：是否允许「自动浏览」（pyautogui 模拟点击微信窗口）。
# 3.x 的实现依赖旧版 UI 截图定位，在 4.x 上会点错位置；且自动操作微信客户端有账号
# 风控风险，因此 4.x 版默认关闭，只保留只读的数据导出能力。
AUTO_BROWSE_ENABLED = False


class Gui:
    """Tkinter 界面。

    所有跨线程操作（主线程更新连接状态、导出线程更新进度条）都通过
    :meth:`call_in_ui` 投递到 Tk 主循环执行，避免原版 ``waiting_label`` 尚未创建就被
    ``config()`` 导致的 ``AttributeError`` 闪退，也避免在非 Tk 线程里直接操作控件。
    """

    def __init__(self):

        self.restart_note1 = None
        self.restart_note2 = None
        self.auto_scroll_single_guide = None
        self.auto_scroll_guide = None
        self.auto_scroll_frame = None
        self.search_username = None
        self.open_search_guide_image = None
        self.open_search_guide = None
        self.auto_scroll_button_single = None
        self.auto_scroll_button_single_text = None
        self.convert_video = None
        self.convert_video_var = None
        self.html_exporter_thread = None
        self.confirm_button_text = None
        self.succeed_label_2 = None
        self.succeed_label = None
        self.auto_scroll_button_text = None
        self.warning_label = None
        self.root = None
        self.waiting_label = None
        self.listbox = None
        self.begin_calendar = None
        self.end_calendar = None
        self.end_calendar_label = None
        self.begin_calendar_label = None
        self.confirm_button = None
        self.decrypt_progressbar = None
        self.export_progressbar = None
        self.next_step_button = None
        self.decrypter = None
        self.auto_scroll_button = None
        self.auto_scrolling_thread = None
        self.decrypt_note = None
        self.decrypt_note_text = None
        self.account_info = None
        self.video_decrypter = None
        self.image_decrypter = None
        self.export_dir_name = None
        self.exporting = False
        # 1: 自动滚动数据 2: 准备 4.x 数据层 3: 导出页面
        self.page_stage = 1
        # GUI 是否已建好（主线程等待它，避免竞态）
        self._ready = threading.Event()
        # GUI 是否已关闭（主线程据此退出重试循环）
        self._closed = threading.Event()
        # 需要 Tk 主线程执行的回调
        self._ui_queue: "queue.Queue" = queue.Queue()
        # Tk 事件循环所在线程（mainloop 跑在独立线程里，不能用 threading.main_thread 判断）
        self._tk_thread_id = None

    # ------------------------------------------------------------------
    # Tk 线程
    # ------------------------------------------------------------------
    def run_gui(self):
        self._tk_thread_id = threading.get_ident()
        try:
            self.root = tkinter.Tk()
            self.root.geometry('650x650')
            self.root.title('朋友圈导出')

            self.waiting_label = tkinter.ttk.Label(self.root, text="正在连接微信....",
                                                   font=("微软雅黑", 16), anchor='center')
            self.waiting_label.place(relx=0.5, rely=0.05, anchor='center')

            self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        except Exception:
            traceback.print_exc()
            self._closed.set()
            return
        finally:
            # 控件准备好后才放行主线程（原版竞态的根因）
            self._ready.set()

        self.root.after(50, self._drain_ui_queue)
        self.root.mainloop()

    def _drain_ui_queue(self):
        """在 Tk 主线程里执行其它线程投递过来的回调。"""
        try:
            while True:
                callback = self._ui_queue.get_nowait()
                try:
                    callback()
                except Exception:
                    traceback.print_exc()
        except queue.Empty:
            pass
        if not self._closed.is_set():
            try:
                self.root.after(50, self._drain_ui_queue)
            except tkinter.TclError:
                pass

    def _on_close(self):
        self._closed.set()
        try:
            self.root.destroy()
        except tkinter.TclError:
            pass

    # ------------------------------------------------------------------
    # 线程安全桥
    # ------------------------------------------------------------------
    def wait_ready(self, timeout: Optional[float] = None) -> bool:
        return self._ready.wait(timeout)

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def call_in_ui(self, callback) -> None:
        """从任意线程投递一个回调到 Tk 主线程执行。"""
        self._ui_queue.put(callback)

    def set_status(self, text: str) -> None:
        """更新等待提示文案（线程安全）。"""
        def apply():
            if self.waiting_label is not None:
                self.waiting_label.config(text=text)

        self.call_in_ui(apply)

    # ------------------------------------------------------------------
    # 登录成功
    # ------------------------------------------------------------------
    def wechat_logged_in(self, account_info):

        self.account_info = account_info
        nick = ""
        if isinstance(account_info, dict):
            nick = account_info.get("nick_name") or account_info.get("wxid") or ""
        if self.waiting_label is not None:
            self.waiting_label.config(text=f"微信已登录：{nick}" if nick else "微信已登录")

        # 重复回调时不要重复建控件
        if self.auto_scroll_button is not None:
            return

        # 4.x 安全策略：**不模拟点击微信窗口**。
        # 3.x 那套「自动浏览」是 pyautogui 按截图找按钮再点/输入，截图样本是旧版 UI，
        # 在 4.x 上会点错位置（可能误发消息/误操作），而且自动操作微信客户端本身有
        # 账号风控风险。4.x 版直接关闭该入口，改为提示用户手动浏览。
        self.auto_scroll_button_text = tkinter.StringVar()
        self.auto_scroll_button_text.set("自动浏览（4.x 已停用）")
        self.auto_scroll_button = tkinter.ttk.Button(self.root, textvariable=self.auto_scroll_button_text,
                                                     command=self.open_auto_scroll_guide, state="disabled")
        self.auto_scroll_button.place(relx=0.35, rely=0.15, anchor='center')

        self.auto_scroll_button_single_text = tkinter.StringVar()
        self.auto_scroll_button_single_text.set("自动浏览单个朋友（已停用）")
        self.auto_scroll_button_single = tkinter.ttk.Button(self.root, textvariable=self.auto_scroll_button_single_text,
                                                            command=self.switch_auto_scroll_single, state="disabled")
        self.auto_scroll_button_single.place(relx=0.655, rely=0.15, anchor='center')

        # 说明文案：告诉用户为什么停用、导出前该做什么（避免误以为功能坏了）
        self.safe_mode_hint = tkinter.ttk.Label(
            self.root,
            text="安全模式：只读本地微信数据，不模拟点击微信（规避风控）。\n"
                 "导出前请在微信里手动浏览朋友圈、点开想导出的图片，图片才会被缓存。",
            font=("微软雅黑", 9), foreground="#666666", justify="center", anchor='center')
        self.safe_mode_hint.place(relx=0.5, rely=0.245, anchor='center')

        self.next_step_button = tkinter.ttk.Button(self.root, text="下一步", command=self.next_step)
        self.next_step_button.place(relx=0.65, rely=0.8)

    def open_auto_scroll_guide(self):

        if not AUTO_BROWSE_ENABLED:  # 4.x：模拟点击微信有风控风险，入口已停用
            tkinter.messagebox.showinfo(
                "自动浏览已停用（4.x 安全模式）",
                "本版本不模拟点击微信窗口，避免误操作与账号风控。\n\n"
                "导出前请手动在微信里浏览朋友圈、点开想导出的图片，"
                "图片缓存到本地后即可正常导出。")
            return

        if self.auto_scroll_single_guide and self.auto_scroll_single_guide.frame:
            self.auto_scroll_single_guide.frame.place_forget()

        self.auto_scroll_guide = AutoScrollGuide(self.root)
        self.auto_scroll_guide.frame.place(relx=0.5, rely=0.5, anchor='center')

    def switch_auto_scroll_single(self):

        if not AUTO_BROWSE_ENABLED:  # 同上
            tkinter.messagebox.showinfo(
                "自动浏览已停用（4.x 安全模式）",
                "本版本不模拟点击微信窗口，避免误操作与账号风控。\n\n"
                "请手动在微信里浏览朋友圈并点开图片，再回到本工具导出。")
            return

        if self.auto_scroll_guide and self.auto_scroll_guide.frame:
            self.auto_scroll_guide.frame.place_forget()

        self.auto_scroll_single_guide = AutoScrollSingleGuide(self.root)
        self.auto_scroll_single_guide.frame.place(relx=0.5, rely=0.5, anchor='center')

    # ------------------------------------------------------------------
    # 步骤流转
    # ------------------------------------------------------------------
    def next_step(self):

        if self.page_stage == 1:

            if self.auto_scroll_guide and self.auto_scroll_guide.auto_thread:
                self.auto_scroll_guide.auto_thread.set_scrolling(False)

            if self.auto_scroll_single_guide and self.auto_scroll_single_guide.auto_thread:
                self.auto_scroll_guide.auto_thread.set_scrolling(False)

            self.auto_scroll_button.place_forget()
            self.auto_scroll_button_single.place_forget()
            hint = getattr(self, "safe_mode_hint", None)
            if hint is not None:
                hint.place_forget()

            if self.auto_scroll_guide and self.auto_scroll_guide.frame:
                self.auto_scroll_guide.frame.place_forget()

            if self.auto_scroll_single_guide and self.auto_scroll_single_guide.frame:
                self.auto_scroll_single_guide.frame.place_forget()

            # 4.x 在线读取，无需关闭微信
            self.restart_note1 = tkinter.Label(self.root, text="无需关闭微信（4.x 在线读取数据）", fg="red")
            self.restart_note1.place(relx=0.5, rely=0.2, anchor='center')
            self.restart_note2 = tkinter.Label(self.root, text="点击下一步准备数据")
            self.restart_note2.place(relx=0.5, rely=0.3, anchor='center')

            self.page_stage = 2
            return

        if self.page_stage == 2:

            if self.restart_note1 is not None:
                self.restart_note1.place_forget()
            if self.restart_note2 is not None:
                self.restart_note2.place_forget()
            if self.waiting_label is not None:
                self.waiting_label.place_forget()

            if self.decrypt_note is None:
                self.decrypt_note_text = tkinter.StringVar()
                self.decrypt_note_text.set("正在准备数据.....")
                self.decrypt_note = tkinter.Label(self.root, textvariable=self.decrypt_note_text)
                self.decrypt_note.place(relx=0.5, rely=0.2, anchor='center')

                self.decrypt_progressbar = tkinter.ttk.Progressbar(self.root)
                self.decrypt_progressbar.place(relx=0.5, rely=0.3, anchor='center')
                # 进度值最大值
                self.decrypt_progressbar['maximum'] = 100
                # 进度值初始值
                self.decrypt_progressbar['value'] = 0

            if self.decrypter is None:
                account_dir = None
                if isinstance(self.account_info, dict):
                    account_dir = self.account_info.get("filePath")
                self.decrypter = DatabaseDecrypter(self, account_dir,
                                                   self.account_info.get("key") if isinstance(self.account_info, dict) else None)

            # 准备过程禁用下一步按钮（失败后可重试）
            self.next_step_button.config(state=tkinter.DISABLED)
            ok = self.decrypter.decrypt()

            if ok:
                self.next_step_button.config(state=tkinter.NORMAL, text="下一步")
                self.page_stage = 3
            else:
                self.next_step_button.config(state=tkinter.NORMAL, text="重试准备")
            return

        if self.page_stage == 3:
            if self.decrypt_note is not None:
                self.decrypt_note.place_forget()
            if self.decrypt_progressbar is not None:
                self.decrypt_progressbar.place_forget()
            self.init_export_page()
            # 不再有下一步按钮
            self.next_step_button.place_forget()
            # 初始化视频导出器
            account_dir = None
            if isinstance(self.account_info, dict):
                account_dir = self.account_info.get("filePath")
            self.video_decrypter = VideoDecrypter(self, account_dir)
            # 初始化图片导出器
            self.image_decrypter = ImageDecrypter(self, account_dir)
            self.page_stage = 4
            return

    def init_export_page(self):

        from app.DataBase import micro_msg_db
        contact_datas = micro_msg_db.get_contact() or []

        contacts = []
        for c in contact_datas:
            if not c or len(c) < 12:
                continue
            contact = Contact(c[0], c[1], c[2], c[3], c[4], c[5], c[6], c[7], c[8], c[9], c[10], c[11])
            contacts.append(contact)

        def validate_contact(this_contact: Contact):
            c_type = this_contact.type
            user_name: str = this_contact.userName

            # 不是其他号码
            is_misc_account = c_type == 1 or c_type == 33 or c_type == 513
            # 不是公众号
            is_gh_account = user_name.startswith("gh_")
            # 不是聊天群
            is_chatroom = user_name.endswith("@chatroom")
            # 不是文件传输助手
            is_filehelper = this_contact.userName == "filehelper"
            return (not is_misc_account) and (not is_gh_account) and (not is_chatroom) and (not is_filehelper)

        filtered = filter(validate_contact, contacts)
        contacts = list(filtered)

        self.listbox = ListboxWithSearch(self.root, contacts)
        self.listbox.frame.place(relx=0.05, rely=0.03)

        self.begin_calendar_label = tkinter.ttk.Label(text="开始日期")
        self.begin_calendar_label.place(relx=0.65, rely=0.15)

        # 默认开始时间是100天前
        current_date = datetime.now()
        half_year_ago = current_date - timedelta(days=100)
        self.begin_calendar = tkcalendar.DateEntry(master=self.root, locale="zh_CN", year=half_year_ago.year,
                                                   month=half_year_ago.month, day=half_year_ago.day,
                                                   maxdate=datetime.now())
        self.begin_calendar.place(relx=0.65, rely=0.2)

        self.end_calendar_label = tkinter.ttk.Label(text="截止日期")
        self.end_calendar_label.place(relx=0.65, rely=0.25)
        self.end_calendar = tkcalendar.DateEntry(master=self.root, locale="zh_CN", maxdate=datetime.now())
        self.end_calendar.place(relx=0.65, rely=0.3)


        self.convert_video_var = tkinter.IntVar(value=0)
        self.convert_video = tkinter.ttk.Checkbutton(self.root, text='视频转码', variable=self.convert_video_var)
        self.convert_video.place(relx=0.65, rely=0.45)
        ToolTip(self.convert_video,
                "视频原始格式为H265,只支持\nChrome浏览器播放，勾选后\n将视频转码为H264,支持大\n部分浏览器，但导出速度变慢")

        self.confirm_button_text = tkinter.StringVar()
        self.confirm_button_text.set("开始导出")

        self.confirm_button = tkinter.ttk.Button(self.root, textvariable=self.confirm_button_text,
                                                 command=self.confirm_export)
        self.confirm_button.place(relx=0.65, rely=0.6)

        # 导出成功的提示
        self.succeed_label = tkinter.Label(self.root, text="导出结束")
        self.succeed_label_2 = tkinter.Label(self.root, text="打开文件夹", fg="#0000FF", cursor="hand2")
        self.succeed_label_2.bind("<Button-1>", self.open_target_folder)

        # 进度条
        self.export_progressbar = tkinter.ttk.Progressbar(self.root, length=150)

    def confirm_export(self):

        if self.html_exporter_thread and not self.html_exporter_thread.stop_flag:
            self.html_exporter_thread.stop()
        else:
            if not self.warning_label:
                self.warning_label = tkinter.Label(self.root, fg="red")
                self.warning_label.place(relx=0.65, rely=0.55)

            self.warning_label.config(text="")
            contacts = self.listbox.get_contacts()
            if not contacts:
                self.warning_label.config(text=f"请选择至少一个联系人")
                return

            if self.begin_calendar.get_date() > self.end_calendar.get_date():
                self.warning_label.config(text=f"开始时间必须小于截止时间")
                return

            self.export_progressbar.place(relx=0.64, rely=0.68)
            # 进度值最大值
            self.export_progressbar['maximum'] = 100
            # 进度值初始值
            self.export_progressbar['value'] = 0

            current_time = datetime.now()
            self.export_dir_name = current_time.strftime("%Y_%m_%d_%H%M%S")
            contact_map = {contact.userName: contact for contact in contacts}

            self.confirm_button_text.set("停止导出")
            self.succeed_label.place_forget()
            self.succeed_label_2.place_forget()

            # 导出线程
            self.html_exporter_thread = HtmlExporter(self, self.export_dir_name, contact_map,
                                                     self.begin_calendar.get_date(), self.end_calendar.get_date(),
                                                     self.convert_video_var.get())
            self.html_exporter_thread.start()

    def update_decrypt_progressbar(self, progress):
        """准备数据阶段（在 Tk 线程里同步执行，可直接刷新）。"""
        try:
            if self.decrypt_progressbar is not None:
                self.decrypt_progressbar['value'] = progress
                self.root.update()
        except tkinter.TclError:
            pass

    def update_export_progressbar(self, progress):
        """导出线程调用：投递到 Tk 主线程更新进度条。"""
        def apply():
            try:
                if self.export_progressbar is not None:
                    self.export_progressbar['value'] = progress
            except tkinter.TclError:
                pass

        self.call_in_ui(apply)

    def export_succeed(self):
        """导出线程调用：投递到 Tk 线程更新界面。"""
        if threading.get_ident() != self._tk_thread_id:
            self.call_in_ui(self.export_succeed)
            return
        self.confirm_button_text.set("开始导出")
        self.succeed_label.place(relx=0.64, rely=0.75)
        self.succeed_label_2.place(relx=0.76, rely=0.75)

    def open_target_folder(self, event):
        folder_path = Path(f"output/{self.export_dir_name}/")
        # 转换为绝对路径
        absolute_path = folder_path.resolve()
        os.startfile(absolute_path)
