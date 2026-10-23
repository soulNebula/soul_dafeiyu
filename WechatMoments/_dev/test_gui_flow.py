# -*- coding: utf-8 -*-
"""真实 Tk GUI 冒烟测试（窗口自动隐藏，不需要人工操作）。

覆盖 main.py 之外的 GUI 主流程：

1. 复现原版竞态：GUI 线程还没建好控件时就 ``set_status()``，不应抛 AttributeError；
2. ``wechat_logged_in`` 建控件；「下一步」→ 准备 4.x 数据层 → 导出页面；
3. 全选联系人、设置日期、点「开始导出」，等导出线程跑完；
4. 断言 ``output/朋友圈_*/index.html`` 生成且有条目，然后关闭窗口。

用法::

    .venv\\Scripts\\python.exe _dev\\test_gui_flow.py
"""
from __future__ import annotations

import datetime
import logging
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
# PIL 解码调试日志太吵（root logger DEBUG 会全打出来）
logging.getLogger("PIL").setLevel(logging.WARNING)

from wx4.db import get_db  # noqa: E402


def _wait(predicate, timeout=30.0, interval=0.1, label=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    raise TimeoutError(f"等待超时：{label}")


def _ui_read(gui, getter, timeout=10.0, label=""):
    """在 Tk 线程里读控件属性（避免跨线程直接操作 Tk 控件）。"""
    box = {}
    done = threading.Event()

    def wrapper():
        try:
            box["value"] = getter()
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc
        finally:
            done.set()

    gui.call_in_ui(wrapper)
    if not done.wait(timeout):
        raise TimeoutError(f"读取控件超时：{label}")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def main() -> int:
    from gui.gui import Gui

    gui = Gui()

    # 1) 原版竞态复现：GUI 线程尚未启动、waiting_label 还是 None 时就更新状态
    gui.set_status("请启动并登录微信 4.x")

    gui_thread = threading.Thread(target=gui.run_gui, daemon=True)
    gui_thread.start()
    assert gui.wait_ready(15), "GUI 15 秒内未就绪"
    print("[1] GUI 已就绪；竞态下的 set_status 没有崩溃")

    # 隐藏窗口，避免干扰桌面
    gui.call_in_ui(lambda: gui.root.withdraw())

    # 状态文案应被 Tk 线程应用
    _wait(lambda: _ui_read(gui, lambda: "4.x" in gui.waiting_label.cget("text"), label="状态文案"),
          timeout=10, label="状态文案")
    print(f"[2] 未登录状态文案: {_ui_read(gui, lambda: gui.waiting_label.cget('text'))!r}")

    # 2) 登录成功 → 建控件
    db = get_db()
    info = {
        "wxid": db.wxid,
        "nick_name": db.self_info().get("nick_name", ""),
        "filePath": str(db.account_dir),
        "key": "",
    }
    gui.call_in_ui(lambda: gui.wechat_logged_in(info))
    _wait(lambda: gui.next_step_button is not None, timeout=10, label="wechat_logged_in 建控件")
    print(f"[3] 微信已登录：{info['nick_name']}，已创建自动浏览/下一步按钮")

    # 3) 下一步 → 准备 4.x 数据层
    gui.call_in_ui(gui.next_step)
    _wait(lambda: gui.page_stage == 2, timeout=10, label="进入准备数据阶段")
    gui.call_in_ui(gui.next_step)  # 同步执行数据层准备
    _wait(lambda: gui.page_stage == 3, timeout=60, label="准备 4.x 数据层")
    print(f"[4] 数据准备完成（进度 {_ui_read(gui, lambda: gui.decrypt_progressbar['value'])}）："
          f"{_ui_read(gui, gui.decrypt_note_text.get)!r}")

    # 4) 下一步 → 导出页面
    gui.call_in_ui(gui.next_step)
    _wait(lambda: gui.page_stage == 4 and gui.listbox is not None, timeout=15,
          label="进入导出页面")
    print("[5] 导出页面已创建")

    # 选择全部联系人、覆盖全部可见朋友圈的日期
    def prepare_export():
        gui.listbox.select_all()
        gui.begin_calendar.set_date(datetime.date(2024, 8, 8))
        gui.end_calendar.set_date(datetime.date(2026, 10, 17))

    gui.call_in_ui(prepare_export)
    time.sleep(0.3)
    selected = _ui_read(gui, lambda: len(gui.listbox.get_contacts()), label="已选联系人")
    print(f"[6] 已选联系人 {selected} 个，日期 2024-08-08 ~ 2026-10-17")
    assert selected > 0, "联系人全选失败"

    gui.call_in_ui(gui.confirm_export)

    _wait(lambda: gui.html_exporter_thread is not None, timeout=10, label="启动导出线程")
    _wait(lambda: not gui.html_exporter_thread.is_alive(), timeout=180, label="导出线程结束")

    dir_name = gui.export_dir_name
    html_path = ROOT / "output" / dir_name / "index.html"
    assert html_path.is_file(), f"导出失败，index.html 不存在: {html_path}"
    html = html_path.read_text(encoding="utf-8")
    items = html.count('class="row item"')
    print(f"[7] 导出完成：output/{dir_name}/index.html，条目 {items}")
    assert items > 0, "导出的 HTML 没有朋友圈条目"

    # 导出结束提示（由 Tk 线程设置）
    _wait(lambda: _ui_read(gui, lambda: gui.succeed_label.winfo_manager() != "",
                           label="导出成功提示"),
          timeout=5, label="导出成功提示")
    succeed_text = _ui_read(gui, lambda: gui.succeed_label.cget("text"))
    button_text = _ui_read(gui, gui.confirm_button_text.get)
    print(f"[8] 界面提示已更新：{succeed_text!r}，开始导出按钮文案={button_text!r}")

    gui.call_in_ui(gui._on_close)
    gui_thread.join(timeout=5)
    print("GUI_FLOW TEST: PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback

        traceback.print_exc()
        print("GUI_FLOW TEST: FAILED")
        sys.exit(1)
