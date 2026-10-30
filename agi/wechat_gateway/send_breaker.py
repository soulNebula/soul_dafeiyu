# -*- coding: utf-8 -*-
"""风控：连续环境类发送失败自动熔断（跨重启保持，需人工 reset）。

判定范围：只统计 ``send_timeout`` / ``sender_unavailable`` 这类"微信侧异常"；
内容检查、闸门拦截、普通发送失败都不计入（避免正常业务被误熔断）。

状态落盘：``<gateway.log 同目录>/send_breaker.json``，重启后仍保持 open，
必须人工 ``reset()``（802/CLI/API）才会清除 —— 这是风控停止线的一部分。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

DEFAULT_THRESHOLD = 2


class SendBreaker:
    def __init__(self, path: Path | str, *, enabled: bool = True,
                 threshold: int = DEFAULT_THRESHOLD) -> None:
        self.path = Path(path)
        self.enabled = bool(enabled)
        try:
            self.threshold = max(1, int(threshold))
        except (TypeError, ValueError):
            self.threshold = DEFAULT_THRESHOLD
        #: 原地更新的状态 dict（外部持有引用不会因 reset 失效）
        self.data: dict[str, Any] = {"open": False, "consecutive": 0,
                                     "opened_at": 0.0, "reason": "", "count": 0}
        self._load()

    # ------------------------------------------------------------- 持久化
    def _load(self) -> None:
        try:
            raw = self.path.read_text(encoding="utf-8")
            payload = json.loads(raw)
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        for key in self.data:
            if key in payload:
                self.data[key] = payload[key]

    def _persist(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.data, ensure_ascii=False,
                                            indent=1) + "\n", encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------- 状态
    @property
    def open(self) -> bool:
        return bool(self.enabled and self.data.get("open"))

    def snapshot(self) -> dict:
        return dict(self.data)

    # ------------------------------------------------------------- 动作
    def note_success(self) -> None:
        if self.data.get("consecutive"):
            self.data["consecutive"] = 0

    def note_failure(self, reason: str) -> dict:
        """记一次环境类失败；达到阈值则打开熔断并落盘。返回快照。"""
        if not self.enabled:
            return self.snapshot()
        self.data["consecutive"] = int(self.data.get("consecutive") or 0) + 1
        self.data["count"] = int(self.data.get("count") or 0) + 1
        if not self.data.get("open") and self.data["consecutive"] >= self.threshold:
            self.data.update({"open": True, "opened_at": time.time(),
                              "reason": f"{reason} x{self.data['consecutive']}"})
            self._persist()
        return self.snapshot()

    def reset(self, source: str = "manual") -> dict:
        """人工恢复：清空熔断并删除落盘标记。"""
        self.data.update({"open": False, "consecutive": 0, "opened_at": 0.0,
                          "reason": "", "count": 0})
        try:
            self.path.unlink()
        except OSError:
            pass
        return self.snapshot()
