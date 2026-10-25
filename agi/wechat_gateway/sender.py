# -*- coding: utf-8 -*-
"""801 微信网关发送适配器。

真实发送只允许走 ``wxauto4``，默认不导入：只有非 dry-run 的 ``send``
才会延迟加载。发送前先用 ``ChatWith(..., exact=True)`` 切换会话，再用
``ChatInfo()`` 核对会话名；无法确认或结果不明确时一律 fail-closed。

P0-4 发送容错（2026-10-25）：
- ``AttributeError('ProfileWnd' ... 'topui')`` 是 wxauto4 关资料卡时的内部
  异常，**不代表切换失败**：只要 ``ChatInfo`` 能独立确认会话名 == 目标就继续发；
  确认不了就 fail-closed（底线），错误信息写清卡在哪一步。
- 发送前句柄双验：会话名 + 输入框存在性探测，并记 ``send_probe`` 审计事件。
- SendMsg 失败先确认"上次确实没发出去"才允许重试一次；清空输入框失败记
  ``input_clear_failed``。草稿落盘由网关层（server._on_send_failure）复用 drafts.py。
- 错误归一化：``0x...`` 对象地址 → ``0xADDR``，得到 ``error_key``（与
  proactive_worker.normalize_error 同口径，供熔断按同类错误计数）。

P5-a 发送层多媒体（2026-10-26）：
- ``send_bubbles(contact, items)`` 支持 text/emoji_text/image/gif/sticker 与 mixed
  多气泡；items 字段规范见 ``agi/wechat_gateway/media.py``（对外契约，不要改名）。
- image/gif 走 ``SendFiles``；sticker 无原生 API → 贴纸当图片 → emoji → 纯文本三级降级。
- mixed：按 items 顺序逐条发，气泡间隔钳制在 80–150ms；单条 media 失败不影响后续文本。
- 现有单条文本 ``send()`` 行为逐字节不变。
"""
from __future__ import annotations

import importlib
import json
import re
import time
from pathlib import Path
from typing import Any

from . import media as media_mod

MAX_CHARS = 500

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOG_PATH = ROOT / "agi" / "data" / "wechat" / "gateway.log"

#: 错误归一化：与 agi/wechat_gateway/proactive_worker.py 的 normalize_error 同口径
_ADDR_RE = re.compile(r"0x[0-9a-fA-F]+")
_WS_RE = re.compile(r"\s+")

#: P0-4/P5-a 默认值（config.DEFAULTS 里有同名键时以配置为准）
_RESILIENCE_DEFAULTS = {
    "send_probe_enabled": True,
    "send_retry_max": 1,
    "media_enabled": False,
    "media_dirs": [],
    "media_max_mb": 10,
    "media_send_interval_ms": 120,
    "sticker_fallback_emoji": "😄",
    "media_audit": True,
}


class WxAutoSender:
    """wxauto4 发送适配器；``module`` 可注入 fake 模块，测试不碰真实微信。"""

    MAX_CHARS = MAX_CHARS

    # WxResponse.status 的明确成功/失败标记（大小写不敏感）。
    _SUCCESS_STATUS = frozenset({"成功", "sent", "ok", "success"})
    _FAILURE_STATUS = frozenset({"失败", "错误", "failed", "error", "failure"})
    _STATUS_TEXT = _SUCCESS_STATUS | _FAILURE_STATUS

    # ChatInfo()/ChatWith() 返回值里可能承载会话名的字段。
    _NAME_KEYS = ("chat_name", "name", "nickname", "who")

    def __init__(self, module: Any = None, *, module_name: str = "wxauto4",
                 cfg: dict | None = None, log_path: Path | str | None = None,
                 retry_delay: float = 0.8) -> None:
        self._module = module
        self.module_name = str(module_name)
        self._wx = None
        defaults = dict(_RESILIENCE_DEFAULTS)
        try:
            from . import config as gw_config
            for key in defaults:
                if key in gw_config.DEFAULTS:
                    defaults[key] = gw_config.DEFAULTS[key]
        except Exception:  # noqa: BLE001 - 配置读不到时用内置默认
            pass
        if isinstance(cfg, dict):
            for key in defaults:
                if key in cfg:
                    defaults[key] = cfg[key]
        self.cfg = defaults
        self.log_path = Path(log_path) if log_path else None
        try:
            self.retry_delay = max(0.0, float(retry_delay))
        except (TypeError, ValueError):
            self.retry_delay = 0.8

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
    # P0-4：审计 / 归一化 / 容错小工具
    # ------------------------------------------------------------------
    @staticmethod
    def normalize_error(error: Any) -> str:
        """错误文本归一化：去掉 0x… 对象地址、压空白，取前 60 字符当 error_key。"""
        value = _ADDR_RE.sub("0xADDR", str(error or ""))
        value = _WS_RE.sub(" ", value).strip()
        return value[:60]

    @staticmethod
    def _is_profile_wnd_error(exc: BaseException) -> bool:
        """topui/ProfileWnd 的 AttributeError：关资料卡失败，不代表会话切换失败。"""
        if not isinstance(exc, AttributeError):
            return False
        text = str(exc or "")
        return "topui" in text or "ProfileWnd" in text

    @staticmethod
    def _is_transient(error: str) -> bool:
        return any(k in str(error or "") for k in ("ProfileWnd", "topui", "Timeout", "timeout"))

    def _log_event(self, kind: str, data: dict) -> None:
        """把审计事件追加到网关日志；日志失败绝不能影响发送。"""
        if self.log_path is None:
            return
        entry = {"ts": time.time(), **data}
        # kind 以事件名为准，防止 payload 里的同名字段把它覆盖掉（P0-7.1 实测踩到）
        entry["kind"] = kind
        try:
            target = Path(self.log_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except Exception:  # noqa: BLE001
            pass

    def _sleep(self) -> None:
        if self.retry_delay > 0:
            time.sleep(self.retry_delay)

    def _probe_handle(self, wx: Any) -> dict:
        """发送前第二重校验：输入框存在性（探测不到就记录，不 abort）。"""
        probe: dict[str, Any] = {"enabled": True, "chat_box": None,
                                 "input_box_exists": None, "source": ""}
        try:
            box = getattr(wx, "ChatBox", None)
            if box is None:
                probe["source"] = "no_chatbox"
                return probe
            probe["chat_box"] = type(box).__name__
            edit = getattr(box, "editbox", None)
            if edit is None:
                edit = getattr(box, "EditBox", None)
            if edit is not None:
                probe["source"] = "chatbox.editbox"
                exists = getattr(edit, "Exists", None)
                probe["input_box_exists"] = bool(exists()) if callable(exists) else True
                return probe
            who = getattr(box, "who", None)
            probe["source"] = "chatbox.who"
            probe["input_box_exists"] = (bool(str(who).strip())
                                         if isinstance(who, str) else None)
        except Exception as exc:  # noqa: BLE001 - 探测失败只记录
            probe["source"] = "probe_error"
            probe["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return probe

    def _clear_input(self, wx: Any, contact: str) -> bool:
        """失败兜底第一步：清空输入框；清不了必须留下 input_clear_failed。"""
        error = "发送窗口未暴露 ChatBox.clear_edit，无法清空输入框"
        try:
            box = getattr(wx, "ChatBox", None)
            clear = getattr(box, "clear_edit", None) if box is not None else None
            if callable(clear):
                clear()
                return True
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
        self._log_event("input_clear_failed", {"friend": contact, "error": str(error)[:200]})
        return False

    def _confirm_not_sent(self, wx: Any, message: str) -> bool:
        """SendMsg 异常/不明确后确认"上一次确实没发出去"。

        只认一种明确证据：输入框里仍完整保留着原消息（说明按 Enter 没生效）。
        读不到输入框内容一律返回 False —— 宁可落草稿，也不冒重复发送的险。
        """
        try:
            box = getattr(wx, "ChatBox", None)
            if box is None:
                return False
            edit = getattr(box, "editbox", None)
            if edit is None:
                edit = getattr(box, "EditBox", None)
            if edit is None:
                return False
            get_value = getattr(edit, "GetValuePattern", None)
            if not callable(get_value):
                return False
            pattern = get_value()
            value = getattr(pattern, "Value", None)
            if value is None and isinstance(pattern, dict):
                value = pattern.get("Value") or pattern.get("value")
            if not isinstance(value, str) or not value.strip():
                return False
            return value.strip() == str(message).strip()
        except Exception:  # noqa: BLE001 - 探测失败 → 不重试
            return False

    def _verify_chat_name(self, wx: Any, switched: Any) -> tuple[str, str, str]:
        """独立校验当前会话名；返回 (会话名, 阶段, 错误文本)。"""
        try:
            chat_info = getattr(wx, "ChatInfo", None)
            info = chat_info() if callable(chat_info) else {}
        except Exception as exc:  # noqa: BLE001
            return "", "chat_info", (
                "ChatInfo 读取失败（stage=chat_info，未发送）："
                f"{type(exc).__name__}: {str(exc)[:200]}")
        got = self._chat_name(info)
        if not got and not self._is_failure_response(switched):
            got = self._chat_name(switched, fallback=True)
        if not got:
            return "", "verify", (
                "无法确认当前会话名（stage=verify，未发送）："
                "ChatInfo 为空，ChatWith 未返回可用名称")
        return got, "verify", ""

    def _send_failure(self, contact: str, text: str, error: str, *, stage: str,
                      attempts: int, probe: dict | None = None,
                      send_attempted: bool = False, input_cleared: bool = False,
                      topui_tolerated: bool = False) -> dict[str, Any]:
        error = str(error or "发送失败")
        key = self.normalize_error(error)
        self._log_event("send_failed", {
            "friend": contact,
            "text": str(text or "")[:120],
            "stage": stage,
            "error": error[:300],
            "error_key": key,
            "attempts": int(attempts),
            "send_attempted": bool(send_attempted),
            "input_cleared": bool(input_cleared),
            "topui_tolerated": bool(topui_tolerated),
        })
        result: dict[str, Any] = {
            "ok": False, "status": "failed", "error": error, "error_key": key,
            "stage": stage, "attempts": int(attempts),
            "send_attempted": bool(send_attempted),
            "input_cleared": bool(input_cleared),
            "topui_tolerated": bool(topui_tolerated),
        }
        if probe is not None:
            result["probe"] = probe
        return result

    # ------------------------------------------------------------------
    # 一次发送尝试（send 的重试循环调用它）
    # ------------------------------------------------------------------
    def _attempt(self, contact: str, message: str, *, attempt: int,
                 attempts_allowed: int, probe_enabled: bool) -> tuple[dict | None, bool]:
        """返回 (结果, 是否重试)。结果 None 表示本次没走到结论、需要重试。"""
        try:
            wx = self._instance()
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            if attempt < attempts_allowed and self._is_transient(last_error):
                return None, True
            return self._send_failure(contact, message, last_error,
                                      stage="instance", attempts=attempt), False

        # 步骤 1：切换会话。SwitchToChat 只做导航，失败不致命（ChatWith 才是关键）。
        switch = getattr(wx, "SwitchToChat", None)
        if callable(switch):
            try:
                switch()
            except Exception:  # noqa: BLE001 - 关资料卡/切页失败留给 ChatWith 暴露
                pass

        tolerated = False
        switched = None
        try:
            switched = wx.ChatWith(contact, exact=True)
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            if self._is_profile_wnd_error(exc):
                # 关键容错：关资料卡失败 ≠ 切换失败，交给 ChatInfo 独立确认。
                tolerated = True
                self._log_event("send_stage", {
                    "friend": contact, "stage": "chat_with", "tolerated": True,
                    "attempt": attempt, "error": last_error[:200]})
            elif attempt < attempts_allowed and self._is_transient(last_error):
                return None, True
            else:
                return self._send_failure(
                    contact, message,
                    f"{last_error}（stage=chat_with，切换会话失败，未发送）",
                    stage="chat_with", attempts=attempt), False

        # 步骤 2：ChatInfo 独立确认会话名（topui 容错后以它为准）。
        got, stage, verify_error = self._verify_chat_name(wx, switched)
        if verify_error:
            if tolerated and attempt < attempts_allowed:
                return None, True
            if tolerated:
                verify_error += ("；topui/ProfileWnd 已容错：切换可能已成功，"
                                 "但无法确认当前会话 → fail-closed 不发送")
            return self._send_failure(contact, message, verify_error, stage=stage,
                                      attempts=attempt, topui_tolerated=tolerated), False
        if got != contact:
            return self._send_failure(
                contact, message,
                f"会话不匹配：期望 {contact!r}，实际 {got!r}"
                f"（stage=verify, topui_tolerated={tolerated} → fail-closed 不发送）",
                stage="verify", attempts=attempt, topui_tolerated=tolerated), False
        if tolerated and attempt < attempts_allowed:
            # 资料卡清理失败：按预算重试一次切换换干净句柄；重试后仍以 ChatInfo 为准。
            return None, True

        # 步骤 3：发送前句柄双验（会话名已在步骤 2 确认，这里探输入框）+ send_probe 审计。
        probe = self._probe_handle(wx) if probe_enabled else None
        if probe_enabled and probe is not None:
            self._log_event("send_probe", {
                "friend": contact,
                "contact": contact,
                "chat_name": got,
                "chat_name_ok": True,
                "probe": probe,
                "input_box_exists": probe.get("input_box_exists"),
                "source": probe.get("source"),
                "topui_tolerated": tolerated,
            })

        # 步骤 4：发送。失败先清输入框，且只有确证"没发出去"才允许重试一次。
        try:
            response = wx.SendMsg(message)
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            confirmed_not_sent = self._confirm_not_sent(wx, message)
            cleared = self._clear_input(wx, contact)
            if confirmed_not_sent and attempt < attempts_allowed:
                return None, True
            verified = "已确认未发出" if confirmed_not_sent else "无法确认是否已发出"
            return self._send_failure(
                contact, message,
                f"{last_error}（stage=send，{verified}）",
                stage="send", attempts=attempt, probe=probe,
                send_attempted=True, input_cleared=cleared,
                topui_tolerated=tolerated), False

        ok, detail = self._send_succeeded(response)
        if not ok:
            confirmed_not_sent = self._confirm_not_sent(wx, message)
            cleared = self._clear_input(wx, contact)
            if confirmed_not_sent and attempt < attempts_allowed:
                return None, True
            return self._send_failure(
                contact, message,
                f"发送结果不明确或失败：{detail}（stage=send）",
                stage="send", attempts=attempt, probe=probe,
                send_attempted=True, input_cleared=cleared,
                topui_tolerated=tolerated), False
        return {"ok": True, "status": "sent", "contact": contact,
                "text": message, "response": detail, "module": self.module_name,
                "attempts": attempt, "chat_name": got, "probe": probe,
                "topui_tolerated": tolerated}, False

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
        真发前必须核对会话名；无法确认时 fail-closed，任何异常都返回
        ``{"ok": False, "status": "failed", "error": ...}``（含 error_key/stage）。
        """
        contact_name = str(contact or "").strip()
        message = "" if text is None else str(text)
        if not contact_name:
            return self._failure("contact 不能为空")
        if not message.strip():
            return self._failure("消息不能为空")
        if len(message) > MAX_CHARS:
            return self._failure(f"消息超过 {MAX_CHARS} 字上限（实际 {len(message)} 字）")
        if dry_run:
            return {"ok": True, "status": "planned", "contact": contact_name, "text": message}

        try:
            retry_max = max(0, int(self.cfg.get("send_retry_max") or 0))
        except (TypeError, ValueError):
            retry_max = 0
        probe_enabled = bool(self.cfg.get("send_probe_enabled", True))
        attempts_allowed = 1 + retry_max

        attempt = 0
        last_error = ""
        while attempt < attempts_allowed:
            attempt += 1
            try:
                result, retry = self._attempt(contact_name, message, attempt=attempt,
                                              attempts_allowed=attempts_allowed,
                                              probe_enabled=probe_enabled)
            except Exception as exc:  # noqa: BLE001 - 兜底：任何异常都 fail-closed
                last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                if attempt < attempts_allowed and self._is_transient(last_error):
                    self._wx = None
                    self._sleep()
                    continue
                return self._send_failure(contact_name, message, last_error,
                                          stage="unknown", attempts=attempt)
            if retry:
                self._wx = None
                self._sleep()
                continue
            return result if result is not None else self._failure(last_error or "发送失败")
        return self._send_failure(contact_name, message, last_error or "发送失败",
                                  stage="unknown", attempts=attempt)

    # ------------------------------------------------------------------
    # P5-a：多媒体多气泡（text / emoji_text / image / gif / sticker）
    # ------------------------------------------------------------------
    def _media_cfg(self, cfg: dict | None = None) -> dict:
        """合并媒体发送用配置：sender 自身 cfg ← 调用方显式 cfg（只认已知键）。"""
        merged = dict(self.cfg)
        if isinstance(cfg, dict):
            for key in _RESILIENCE_DEFAULTS:
                if key in cfg:
                    merged[key] = cfg[key]
        return merged

    def _open_chat(self, contact: str) -> tuple[Any, str, str]:
        """切到目标会话并核对；返回 ``(wx, chat_name, error)``，失败时 wx=None。

        P0-4 容错沿用：topui/ProfileWnd 异常不致命，但必须由 ChatInfo 独立确认；
        确认不了就 fail-closed（一个气泡都不发）。
        """
        try:
            retry_max = max(0, int(self.cfg.get("send_retry_max") or 0))
        except (TypeError, ValueError):
            retry_max = 0
        attempts_allowed = 1 + retry_max
        attempt = 0
        last_error = ""
        while attempt < attempts_allowed:
            attempt += 1
            try:
                wx = self._instance()
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                if attempt < attempts_allowed and self._is_transient(last_error):
                    self._wx = None
                    self._sleep()
                    continue
                return None, "", f"{last_error}（stage=instance，切换会话失败，未发送）"
            switch = getattr(wx, "SwitchToChat", None)
            if callable(switch):
                try:
                    switch()
                except Exception:  # noqa: BLE001 - 导航失败留给 ChatWith 暴露
                    pass
            tolerated = False
            switched = None
            try:
                switched = wx.ChatWith(contact, exact=True)
            except Exception as exc:  # noqa: BLE001
                last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                if self._is_profile_wnd_error(exc):
                    tolerated = True
                elif attempt < attempts_allowed and self._is_transient(last_error):
                    self._wx = None
                    self._sleep()
                    continue
                else:
                    return None, "", (f"{last_error}"
                                      "（stage=chat_with，切换会话失败，未发送）")
            got, stage, verify_error = self._verify_chat_name(wx, switched)
            if verify_error:
                if tolerated and attempt < attempts_allowed:
                    self._wx = None
                    self._sleep()
                    continue
                suffix = ("；topui/ProfileWnd 已容错：切换可能已成功，但无法确认当前会话"
                          " → fail-closed 不发送") if tolerated else ""
                return None, "", verify_error + suffix
            if got != contact:
                return None, "", (f"会话不匹配：期望 {contact!r}，实际 {got!r}"
                                  f"（stage=verify, topui_tolerated={tolerated}"
                                  " → fail-closed 不发送）")
            if tolerated and attempt < attempts_allowed:
                self._wx = None
                self._sleep()
                continue
            return wx, got, ""
        return None, "", last_error or "切换会话失败"

    def _send_text_raw(self, wx: Any, text: str) -> tuple[bool, str]:
        """发一条文本（SendMsg）；返回 (ok, 错误)。"""
        try:
            response = wx.SendMsg(text)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:300]}"
        ok, detail = self._send_succeeded(response)
        return ok, "" if ok else f"发送结果不明确或失败：{detail}"

    def _send_file_raw(self, wx: Any, path: str) -> tuple[bool, str]:
        """发一个文件（SendFiles，图片/GIF）；返回 (ok, 错误)。"""
        send_files = getattr(wx, "SendFiles", None)
        if not callable(send_files):
            return False, "当前 wxauto4 模块不支持 SendFiles"
        try:
            response = send_files(path)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {str(exc)[:300]}"
        ok, detail = self._send_succeeded(response)
        return ok, "" if ok else f"发送结果不明确或失败：{detail}"

    def _log_media(self, cfg: dict, data: dict) -> None:
        """每条 item 的 send_media 审计；Gateway 走 _record 时可用 env 关掉本层。"""
        if not bool(cfg.get("media_audit", True)):
            return
        self._log_event("send_media", data)

    @staticmethod
    def _bubble_result(item: dict, ok: bool, status: str, error: str = "",
                       fallback_used: list[str] | None = None,
                       used: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": bool(ok), "status": status,
            "type": str(item.get("type") or ""),
            "fallback_used": list(fallback_used or []),
            "used": used,
            "error": str(error or "")[:300],
            "error_key": WxAutoSender.normalize_error(error) if error else "",
        }
        if item.get("text"):
            result["text"] = str(item.get("text"))[:MAX_CHARS]
        if item.get("path"):
            result["path"] = str(item.get("path"))
        if item.get("name"):
            result["name"] = str(item.get("name"))
        return result

    def _sticker_bubbles(self, wx: Any, item: dict,
                         cfg: dict) -> tuple[bool, str, str | None, list[str]]:
        """贴纸三级降级：贴纸当图片 → emoji_text → 纯文本。

        返回 ``(ok, error, used, fallback_used)``；``fallback_used`` 是按顺序尝试过的
        策略列表（image/emoji_text/text），``used`` 是最终成功的那一级（全失败 None）。
        """
        tries: list[str] = []
        raw = item.get("path") or item.get("name")
        sticker_path = media_mod.resolve_sticker(raw, cfg)
        errors: list[str] = []
        if sticker_path:
            tries.append("image")
            ok, error = self._send_file_raw(wx, sticker_path)
            if ok:
                return True, "", "image", tries
            errors.append(error)
        elif raw:
            # 说得清为什么没走图片这一步（.wxgf 不可发 / 不在白名单 / 找不到）
            _found = media_mod.find_file(raw, cfg)
            if _found is not None and _found.suffix.lower() in media_mod.UNSENDABLE_EXTS:
                errors.append("wxgf_unsupported")
            else:
                errors.append("sticker_not_found")
        # emoji 降级（Unicode emoji 是普通文本，客户端一定渲染）
        emoji = media_mod.fallback_emoji(cfg)
        if emoji:
            tries.append("emoji_text")
            ok, error = self._send_text_raw(wx, emoji)
            if ok:
                return True, "", "emoji_text", tries
            errors.append(error)
        # 纯文本兜底：绝不因为贴纸失败把整条消息吞掉
        plain = str(item.get("text") or "").strip()
        if not plain:
            stem = Path(str(raw or "")).stem
            plain = f"[{stem}]" if stem and stem not in (".", "") else "[表情包]"
        tries.append("text")
        ok, error = self._send_text_raw(wx, plain)
        if ok:
            return True, "", "text", tries
        errors.append(error)
        return False, " / ".join(x for x in errors if x) or "sticker_failed", None, tries

    def send_bubbles(self, contact: str, items: list[dict] | tuple[dict, ...], *,
                     dry_run: bool = False, cfg: dict | None = None) -> list[dict[str, Any]]:
        """按顺序发送多个气泡（文字夹杂表情/图片），返回每条结果。

        items 规范见 :mod:`agi.wechat_gateway.media`（对外契约）：
        ``{type: text|emoji_text|image|gif|sticker, text?, path?, name?}``。

        - ``media_enabled=false`` → 整体 blocked，**不发送任何东西**；
        - 会话名无法确认 → 所有气泡 fail-closed（不发送）；
        - 单条 media 失败不影响后续文本（mixed 按顺序继续）；
        - 气泡之间 sleep 80–150ms（配置 ``media_send_interval_ms`` 钳制）。
        """
        contact_name = str(contact or "").strip()
        raw_items = [item for item in (items or []) if isinstance(item, dict)]
        effective = self._media_cfg(cfg)
        if not contact_name or not raw_items:
            return [self._bubble_result(item, False, "failed", "空的 contact/items")
                    for item in raw_items]
        if not media_mod.media_enabled(effective):
            self._log_media(effective, {
                "friend": contact_name, "status": "blocked", "type": "batch",
                "error": "media_disabled", "items": len(raw_items)})
            return [self._bubble_result(item, False, "blocked", "media_disabled")
                    for item in raw_items]

        interval = media_mod.send_interval_s(effective)
        results: list[dict | None] = [None] * len(raw_items)
        pending: list[int] = []
        for index, item in enumerate(raw_items):
            kind = str(item.get("type") or "").strip().lower()
            # sticker 的定位失败也可能降级成 emoji/文本，交给 _sticker_bubbles 处理，
            # 不在这里直接判死（.wxgf 就是典型：不能发文件，但可以降级）。
            if kind == "sticker":
                pending.append(index)
                continue
            valid, code = media_mod.validate_item(item, cfg=effective)
            if valid:
                pending.append(index)
                continue
            results[index] = self._bubble_result(item, False, "failed", code)
            self._log_media(effective, {
                "friend": contact_name, "type": str(item.get("type") or ""),
                "name": media_mod.item_label(item), "status": "failed",
                "fallback": [], "error": code, "error_key": code})
        if not pending:
            return [result if result is not None else
                    self._bubble_result(raw_items[i], False, "failed", "invalid_item")
                    for i, result in enumerate(results)]

        if dry_run:
            for index in pending:
                results[index] = self._bubble_result(raw_items[index], True, "planned")
            return [result if result is not None else
                    self._bubble_result(raw_items[i], False, "failed", "invalid_item")
                    for i, result in enumerate(results)]

        wx, chat_name, switch_error = self._open_chat(contact_name)
        if wx is None:
            for index in pending:
                results[index] = self._bubble_result(
                    raw_items[index], False, "failed", f"未发送：{switch_error}")
                self._log_media(effective, {
                    "friend": contact_name,
                    "type": str(raw_items[index].get("type") or ""),
                    "name": media_mod.item_label(raw_items[index]),
                    "status": "failed", "fallback": [],
                    "error": switch_error[:200],
                    "error_key": self.normalize_error(switch_error)})
            return [result if result is not None else
                    self._bubble_result(raw_items[i], False, "failed", "invalid_item")
                    for i, result in enumerate(results)]

        probe_enabled = bool(effective.get("send_probe_enabled", True))
        if probe_enabled:
            probe = self._probe_handle(wx)
            self._log_event("send_probe", {
                "friend": contact_name, "contact": contact_name,
                "chat_name": chat_name, "chat_name_ok": True, "mode": "media",
                "probe": probe, "input_box_exists": probe.get("input_box_exists"),
                "source": probe.get("source")})

        attempted_any = False
        for index in pending:
            if attempted_any:
                time.sleep(interval)          # 80–150ms：避免后续气泡抢焦点
            attempted_any = True
            item = raw_items[index]
            kind = str(item.get("type") or "").strip().lower()
            fallback_used: list[str] = []
            used: str | None = None
            if kind in media_mod.TEXT_TYPES:
                ok, error = self._send_text_raw(wx, str(item.get("text")))
            elif kind == "sticker":
                ok, error, used, fallback_used = self._sticker_bubbles(
                    wx, item, effective)
            else:                             # image / gif
                found = media_mod.find_file(item.get("path") or item.get("name"),
                                            effective)
                if found is None:
                    ok, error = False, "not_found"
                else:
                    ok, error = self._send_file_raw(wx, str(found))
            status = "sent" if ok else "failed"
            results[index] = self._bubble_result(
                item, ok, status, error, fallback_used=fallback_used, used=used)
            self._log_media(effective, {
                "friend": contact_name, "type": kind,
                "name": media_mod.item_label(item), "status": status,
                "fallback": fallback_used, "used": used,
                "error": str(error or "")[:200],
                "error_key": self.normalize_error(error) if error else ""})
        return [result if result is not None else
                self._bubble_result(raw_items[i], False, "failed", "invalid_item")
                for i, result in enumerate(results)]
