# -*- coding: utf-8 -*-
"""桌面版 exe 入口（PyInstaller 打包用）。

打包后行为与 `python tools/desktop/launcher.py` 完全一致：
- 默认：确保 801/802/803 在跑 → 用 Edge app 模式打开 802 控制台；
- `--tray`：同时拉起系统托盘（tools/desktop/tray.ps1）；
- `--open-workbench`：打开 803 工作台窗口；
- `--status` / `--stop` / `--autostart install` 等参数透传。
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import launcher  # noqa: E402  （同目录模块，PyInstaller 会一起打包）


def _log_crash(exc: BaseException) -> None:
    """把未捕获异常写到仓库 .data/desktop_crash.log（打包后无控制台，便于排查）。"""
    import datetime
    import traceback
    try:
        log_dir = launcher.ROOT / ".data"
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "desktop_crash.log").open("a", encoding="utf-8") as fh:
            fh.write("\n===== " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S") + " =====\n")
            fh.write("".join(traceback.format_exception(exc)))
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    try:
        raise SystemExit(launcher.main())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001
        _log_crash(exc)
        raise
