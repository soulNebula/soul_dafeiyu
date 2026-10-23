# -*- coding: utf-8 -*-
"""801 微信网关发送适配器。

真实发送只允许走 ``wxauto4``，默认不导入：只有非 dry-run 的 ``send``
才会延迟加载。发送前先用 ``ChatWith(..., exact=True)`` 切换会话，再用
``ChatInfo()`` 核对会话名；无法确认或结果不明确时一律 fail-closed。
"""
from __future__ import annotations

import importlib
from typing import Any

MAX_CHARS = 500


class WxAutoSender:
    """wxauto4 发送适配器；``module`` 可注入 fake 模块，测试不碰真实微信。"""

    MAX_CHARS = MAX_CHARS

    # WxResponse.status 的明确成功/失败标记（大小写不敏感）。
    _SUCCESS_STATUS = frozenset({"成功", "sent", "ok", "success"})
    _FAILURE_STATUS = frozenset({"失败", "错误", "failed", "error", "failure"})
    _STATUS_TEXT = _SUCCESS_STATUS | _FAILURE_STATUS

    # ChatInfo()/ChatWith() 返回值里可能承载会话名的字段。
    _NAME_KEYS = ("chat_name", "name", "nickname", "who")

    def __init__(self, module: Any = None, *, module_name: str = "wxauto4") -> None:
        self._module = module
        self.module_name = str(module_name)
        self._wx = None

    # ------------------------------------------------------------------
    # wxauto4 延迟加载
    # ------------------------------------------------------------------
    def _load(self) -> Any:
        module = self._module
        if module is None:
            module = importlib.import_module(self.module_name)
        if not hasattr(module, "WeChat"):
            raise RuntimeError(f"模块 {self.module_name} 缺少 WeChat")
        self._module = module
        return module

    def _instance(self) -> Any:
        if self._wx is None:
            wx_cls = getattr(self._load(), "WeChat")
            # 按 wxauto4 的约定关闭广告初始化。
            self._wx = wx_cls(ads=False)
        return self._wx

    # ------------------------------------------------------------------
    # 返回值解析
    # ------------------------------------------------------------------
    @staticmethod
    def _text(value: Any, limit: int = 300) -> str:
        try:
            if isinstance(value, str):
                return value[:limit]
            return repr(value)[:limit]
        except Exception:  # noqa: BLE001 - 诊断信息不能反过来打断流程
            return f"<{type(value).__name__}>"

    @classmethod
    def _response_text(cls, response: Any) -> str:
        if isinstance(response, dict):
            status = response.get("status")
            message = response.get("message")
            if status is not None and message:
                return cls._text(f"{status}: {message}")
        return cls._text(response)

    @classmethod
    def _send_succeeded(cls, response: Any) -> tuple[bool, str]:
        """严格解析 SendMsg 返回值，只有明确成功才算成功。"""
        if isinstance(response, bool):
            return response, cls._text(response)
        if response is None:
            return False, "None"

        if isinstance(response, str):
            status = response.strip().lower()
            if status in cls._SUCCESS_STATUS:
                return True, response
            return False, response or "空字符串"

        if isinstance(response, dict):
            ok_value = response.get("ok")
            success_value = response.get("success")
            status_raw = response.get("status")
            status = str(status_raw).strip().lower() if status_raw is not None else ""
            # 先看明确失败；相互矛盾的返回值也按失败处理。
            if (
                ok_value is False
                or success_value is False
                or status in cls._FAILURE_STATUS
            ):
                return False, cls._response_text(response)
            if (
                ok_value is True
                or success_value is True
                or status in cls._SUCCESS_STATUS
            ):
                return True, cls._response_text(response)
            if getattr(response, "is_success", None) is True:
                return True, cls._response_text(response)
            return False, f"未明确成功: {cls._response_text(response)}"

        # 非 dict 的自定义结果对象：只认显式的成功属性，不认“非 None 即成功”。
        ok_value = getattr(response, "ok", None)
        success_value = getattr(response, "success", None)
        status_raw = getattr(response, "status", None)
        status = str(status_raw).strip().lower() if status_raw is not None else ""
        if ok_value is False or success_value is False or status in cls._FAILURE_STATUS:
            return False, cls._response_text(response)
        if ok_value is True or success_value is True or status in cls._SUCCESS_STATUS:
            return True, cls._response_text(response)
        is_success = getattr(response, "is_success", None)
        if callable(is_success):
            try:
                is_success = is_success()
            except Exception:  # noqa: BLE001 - 解析失败时继续 fail-closed
                is_success = None
        if is_success is True:
            return True, cls._response_text(response)
        return False, f"未明确成功: {cls._response_text(response)}"

    # ------------------------------------------------------------------
    # 会话名校对
    # ------------------------------------------------------------------
    @classmethod
    def _is_failure_response(cls, value: Any) -> bool:
        """判断 ChatWith 之类的返回值是否明确表示失败。"""
        if isinstance(value, dict):
            if value.get("ok") is False or value.get("success") is False:
                return True
            status = value.get("status")
            return isinstance(status, str) and status.strip().lower() in cls._FAILURE_STATUS
        if value is None or isinstance(value, (str, bool)):
            return False
        if getattr(value, "ok", None) is False or getattr(value, "success", None) is False:
            return True
        status = getattr(value, "status", None)
        return isinstance(status, str) and status.strip().lower() in cls._FAILURE_STATUS

    @classmethod
    def _chat_name(cls, value: Any, *, fallback: bool = False, _depth: int = 0) -> str:
        """从 ChatInfo()/ChatWith() 返回值里提取会话名。

        ChatInfo 为空时才会用 ChatWith 的返回值兜底；兜底路径会跳过
        "成功" 之类的状态词，避免把状态文本误当成联系人名。
        """
        if _depth > 3 or value is None or isinstance(value, bool):
            return ""

        if isinstance(value, str):
            name = value.strip()
            if fallback and name.lower() in cls._STATUS_TEXT:
                return ""
            return name

        if isinstance(value, dict):
            for key in cls._NAME_KEYS:
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
            # WxResponse 常把结果放在 data 里。
            data = value.get("data")
            if data is not None and data is not value:
                name = cls._chat_name(data, fallback=True, _depth=_depth + 1)
                if name:
                    return name
            if fallback:
                chat = value.get("chat")
                if chat is not None and chat is not value:
                    name = cls._chat_name(chat, fallback=True, _depth=_depth + 1)
                    if name:
                        return name
                message = value.get("message")
                if isinstance(message, str):
                    name = message.strip()
                    if name and name.lower() not in cls._STATUS_TEXT:
                        return name
            return ""

        # Chat 子窗口对象（有 who 属性）或其他带名称属性的结果对象。
        for key in cls._NAME_KEYS:
            candidate = getattr(value, key, None)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        if fallback:
            for key in ("data", "chat"):
                nested = getattr(value, key, None)
                if nested is not None and nested is not value:
                    name = cls._chat_name(nested, fallback=True, _depth=_depth + 1)
                    if name:
                        return name
            message = getattr(value, "message", None)
            if isinstance(message, str):
                name = message.strip()
                if name and name.lower() not in cls._STATUS_TEXT:
                    return name
        return ""

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    @staticmethod
    def _failure(error: str) -> dict[str, Any]:
        return {"ok": False, "status": "failed", "error": error}

    def send(self, contact: str, text: str, *, dry_run: bool = False) -> dict[str, Any]:
        """发送一条文本。

        ``dry_run=True`` 只返回计划，不导入/实例化 wxauto4，也不发送。
        真发前必须核对会话名；任何异常或不明确的 SendMsg 结果都返回
        ``{"ok": False, "status": "failed", "error": ...}``。
        """
        try:
            contact_name = str(contact or "").strip()
            message = "" if text is None else str(text)

            if not contact_name:
                return self._failure("contact 不能为空")
            if not message.strip():
                return self._failure("消息不能为空")
            if len(message) > MAX_CHARS:
                return self._failure(
                    f"消息超过 {MAX_CHARS} 字上限（实际 {len(message)} 字）"
                )

            if dry_run:
                return {
                    "ok": True,
                    "status": "planned",
                    "contact": contact_name,
                    "text": message,
                }

            wx = self._instance()
            switched = wx.ChatWith(contact_name, exact=True)
            chat_info = getattr(wx, "ChatInfo", None)
            info = chat_info() if callable(chat_info) else {}

            got = self._chat_name(info)
            if not got and not self._is_failure_response(switched):
                got = self._chat_name(switched, fallback=True)

            if not got:
                return self._failure(
                    "无法确认当前会话名（ChatInfo 为空，ChatWith 未返回可用名称）"
                )
            if got != contact_name:
                return self._failure(
                    f"会话不匹配：期望 {contact_name!r}，实际 {got!r}"
                )

            response = wx.SendMsg(message)
            ok, detail = self._send_succeeded(response)
            if not ok:
                return self._failure(f"发送结果不明确或失败：{detail}")

            return {
                "ok": True,
                "status": "sent",
                "contact": contact_name,
                "text": message,
                "response": detail,
                "module": self.module_name,
            }
        except Exception as exc:  # noqa: BLE001 - 适配器边界必须返回失败而不是抛出
            return self._failure(f"{type(exc).__name__}: {str(exc)[:300]}")
