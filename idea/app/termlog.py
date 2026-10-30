# -*- coding: utf-8 -*-
"""终端面板的**只读数据源**：聚合 803 托管服务的日志尾部。

安全红线（不要放宽）：
- 只读文件，**不执行任何 shell / 系统命令**，也不接受调用方传任意路径；
- 来源只允许 ``services.SERVICES`` 里声明的 ``log`` 路径（801/802/8123/8125）；
- 读取量固定上限：每个来源只读文件末尾 64KB、只回最多 40 行，避免大日志拖垮页面。

服务自身的 stdout/stderr 已由 ``services.start()`` 用 ``open(log, "ab")`` 落盘
（见 ``idea/app/services.py`` 的 ``_defs()``），所以面板展示的是真实运行日志，
不是演示文本。终端面板不提供写操作，也不提供历史滚动以外的任何能力。
"""
from __future__ import annotations

import time
from pathlib import Path

from . import services

DEFAULT_LINES = 120
MAX_LINES = 500
MIN_LINES = 10
_PER_SOURCE_MAX = 40
_READ_BYTES = 64 * 1024


def _tail(path: Path, lines: int) -> tuple[bool, str]:
    """读文件末尾 ``lines`` 行；文件不存在返回 ``(False, "")``。"""
    try:
        if not path.is_file():
            return False, ""
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _READ_BYTES:
                fh.seek(size - _READ_BYTES)
                fh.readline()  # 丢掉被截断的半行，避免第一行是乱码
            raw = fh.read()
        text = raw.decode("utf-8", "replace")
        rows = text.splitlines()[-max(1, int(lines)):]
        return True, "\n".join(rows)
    except OSError as exc:
        return False, f"(读取失败：{type(exc).__name__}: {exc})"


def collect(lines: int = DEFAULT_LINES) -> dict:
    """返回各托管服务的日志尾部（只读、来源固定）。"""
    try:
        want = int(lines)
    except (TypeError, ValueError):
        want = DEFAULT_LINES
    want = max(MIN_LINES, min(MAX_LINES, want))
    sources = list(services.SERVICES.items())
    per_source = min(_PER_SOURCE_MAX, max(5, want // max(1, len(sources))))
    items = []
    for name, cfg in sources:
        path = Path(str(cfg.get("log") or ""))
        exists, text = _tail(path, per_source)
        items.append({
            "name": name,
            "label": str(cfg.get("label") or name),
            "port": cfg.get("port"),
            "path": str(path),
            "exists": bool(exists),
            "text": text,
        })
    return {
        "ok": True,
        "mode": "readonly",
        "generated_at": time.strftime("%H:%M:%S"),
        "lines_per_source": per_source,
        "items": items,
    }
