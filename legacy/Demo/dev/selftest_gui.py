"""开发自测：用真实后端(数据库直读) + GUI 跑一遍，截图验证图片预览。"""
import sys
import time
import tkinter as tk

sys.path.insert(0, ".")

from app import (_enable_dpi_awareness, capture_window, load_backend,
                 load_config)
from ui.main_window import MainWindow


def main() -> int:
    _enable_dpi_awareness()
    config = load_config()
    config["db_direct"] = True
    config["auto_switch"] = False      # 自测不抢你正在用的微信窗口
    config["emit_history_on_start"] = True
    config["db_history_count"] = 12
    backend, reason, is_fake = load_backend(config, force_fake=False)
    print("backend fake =", is_fake, "reason =", reason)
    root = tk.Tk()
    root.withdraw()
    window = MainWindow(root, backend, config=config, demo_mode=is_fake, demo_note=reason)
    window.start()
    root.geometry("1220x900")
    root.deiconify()
    backend.start()

    def start_monitor():
        try:
            backend.set_monitored(["妈妈"])
            print("monitor started")
        except Exception as exc:  # noqa: BLE001
            print("start fail:", exc)

    def sync_history():
        try:
            res = backend.sync_history(["妈妈"], 15)
            print("sync_history:", {k: res.get(k) for k in ("requested", "available", "synced", "error")})
        except Exception as exc:  # noqa: BLE001
            print("sync fail:", exc)

    root.after(800, start_monitor)
    root.after(3000, sync_history)
    root.after(15000, lambda: capture_window(root, window, "recon/gui_history.png"))
    root.after(16500, window.close)
    root.after(17500, root.destroy)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
