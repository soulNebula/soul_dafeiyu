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

if __name__ == "__main__":
    raise SystemExit(launcher.main())
