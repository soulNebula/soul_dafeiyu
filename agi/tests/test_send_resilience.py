# -*- coding: utf-8 -*-
"""P0-4：发送路径容错（topui/ProfileWnd 非致命、句柄双验、draft 兜底、error_key）。

全部使用 fake wx 模块：测试进程不 import wxauto4、不连接真实微信、不发真实消息。
"""
from __future__ import annotations

import json
import types

import pytest

from agi.wechat_gateway import config as gw_config
from agi.wechat_gateway import drafts
from agi.wechat_gateway.sender import WxAutoSender
from agi.wechat_gateway.server import Gateway

CONTACT = "好友A"
TOPUT_ERROR = AttributeError("'ProfileWnd' object has no attribute 'topui'")
SUCCESS = {"status": "成功", "message": "发送成功", "data": None}


# --------------------------------------------------------------------------- #
# fake wxauto4
# --------------------------------------------------------------------------- #
class FakeEditBox:
    """输入框探针：GetValuePattern().Value 用于「确认上次没真发出去」。"""

    def __init__(self, value: str):
        self.value = value

    def Exists(self):
        return True

    def GetValuePattern(self):
        return types.SimpleNamespace(Value=self.value)


class FakeChatBox:
    def __init__(self, who: str = CONTACT, editbox=None, clear_error=None):
        self.who = who
        self.editbox = editbox
        self.clear_error = clear_error
        self.clear_calls = 0

    def clear_edit(self):
        self.clear_calls += 1
        if self.clear_error:
            raise self.clear_error
        return True


def make_fake(*, chat_with_errors=None, chat_with_result=None, chat_info=None,
              send_result=SUCCESS, send_error=None, chat_box=None):
    """返回 (fake module, holder)；holder.instances 记录构造出的 WeChat。"""
    holder = types.SimpleNamespace(instances=[], cfg={
        "chat_with_errors": list(chat_with_errors or []),
        "chat_with_result": chat_with_result,
        "chat_info": chat_info,
        "send_result": send_result,
        "send_error": send_error,
        "chat_box": chat_box,
        "chat_with_calls": 0,
    })

    class WeChat:
        def __init__(self, **kwargs):
            self.calls = []
            holder.instances.append(self)

        def ChatWith(self, who, exact=True):
            self.calls.append(("ChatWith", who, exact))
            errors = holder.cfg["chat_with_errors"]
            idx = holder.cfg["chat_with_calls"]
            holder.cfg["chat_with_calls"] += 1
            if errors:
                err = errors[min(idx, len(errors) - 1)]
                if err is not None:
                    raise err
            return holder.cfg["chat_with_result"] or who

        def ChatInfo(self):
            self.calls.append(("ChatInfo",))
            info = holder.cfg["chat_info"]
            if isinstance(info, Exception):
                raise info
            return {} if info is None else info

        def SendMsg(self, text):
            self.calls.append(("SendMsg", text))
            error = holder.cfg["send_error"]
            if error is not None:
                raise error
            return holder.cfg["send_result"]

        @property
        def ChatBox(self):
            box = holder.cfg["chat_box"]
            return box() if callable(box) else box

    return types.SimpleNamespace(WeChat=WeChat), holder


def _sent(wx):
    return [call for call in wx.calls if call[0] == "SendMsg"]


def _log_events(path):
    if not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))
    return events


def _sender(module, tmp_path, **cfg_over):
    cfg = {"send_probe_enabled": True, "send_retry_max": 1}
    cfg.update(cfg_over)
    return WxAutoSender(module=module, cfg=cfg,
                        log_path=tmp_path / "gateway.log", retry_delay=0)


# --------------------------------------------------------------------------- #
# 1. topui/ProfileWnd 非致命：ChatInfo 确认会话名 → 继续发
# --------------------------------------------------------------------------- #
def test_topui_tolerated_when_chat_info_confirms(tmp_path):
    module, holder = make_fake(
        chat_with_errors=[TOPUT_ERROR],           # 每次都抛 topui
        chat_info={"chat_name": CONTACT},
        chat_box=FakeChatBox(),
    )
    out = _sender(module, tmp_path).send(CONTACT, "在吗")

    assert out["ok"] is True and out["status"] == "sent"
    assert _sent(holder.instances[-1]) == [("SendMsg", "在吗")]
    # 最多重试 1 次切换（send_retry_max=1），随后仍以 ChatInfo 为准继续发送
    assert out["attempts"] == 2
    assert out["topui_tolerated"] is True
    probe = [e for e in _log_events(tmp_path / "gateway.log") if e["kind"] == "send_probe"]
    assert probe and probe[0]["friend"] == CONTACT
    assert probe[0]["chat_name"] == CONTACT
    # 容错本身要留证据（生产日志能看出"topui 被容错后发送成功"）
    stage = [e for e in _log_events(tmp_path / "gateway.log") if e["kind"] == "send_stage"]
    assert stage and stage[0]["tolerated"] is True and stage[0]["stage"] == "chat_with"


def test_topui_tolerated_with_retry_disabled(tmp_path):
    module, holder = make_fake(
        chat_with_errors=[TOPUT_ERROR],
        chat_info={"chat_name": CONTACT},
        chat_box=FakeChatBox(),
    )
    out = _sender(module, tmp_path, send_retry_max=0).send(CONTACT, "在吗")

    assert out["ok"] is True and out["status"] == "sent"
    assert out["attempts"] == 1
    assert len(holder.instances) == 1


# --------------------------------------------------------------------------- #
# 2. topui 容错但确认不了会话 → fail-closed，绝不发
# --------------------------------------------------------------------------- #
def test_topui_with_mismatching_chat_info_fails_closed(tmp_path):
    module, holder = make_fake(
        chat_with_errors=[TOPUT_ERROR],
        chat_info={"chat_name": "别人"},
    )
    out = _sender(module, tmp_path).send(CONTACT, "在吗")

    assert out["ok"] is False and out["status"] == "failed"
    assert "不匹配" in out["error"] and "topui" in out["error"]
    assert _sent(holder.instances[-1]) == []


def test_topui_with_empty_chat_info_fails_closed(tmp_path):
    module, holder = make_fake(chat_with_errors=[TOPUT_ERROR], chat_info={})
    out = _sender(module, tmp_path).send(CONTACT, "在吗")

    assert out["ok"] is False
    assert "无法确认" in out["error"] and "stage=verify" in out["error"]
    assert _sent(holder.instances[-1]) == []


def test_chat_info_exception_fails_closed_with_stage(tmp_path):
    module, holder = make_fake(
        chat_with_errors=[TOPUT_ERROR],
        chat_info=RuntimeError("UI 读取失败"),
    )
    out = _sender(module, tmp_path).send(CONTACT, "在吗")

    assert out["ok"] is False
    assert "chat_info" in out["error"] and "UI 读取失败" in out["error"]
    assert _sent(holder.instances[-1]) == []


def test_non_topui_chat_with_error_fails_closed(tmp_path):
    module, holder = make_fake(chat_with_errors=[RuntimeError("找不到会话窗口")],
                               chat_info={"chat_name": CONTACT})
    out = _sender(module, tmp_path).send(CONTACT, "在吗")

    assert out["ok"] is False
    assert "chat_with" in out["error"] and "找不到会话窗口" in out["error"]
    assert _sent(holder.instances[-1]) == []


# --------------------------------------------------------------------------- #
# 3. SendMsg 失败：不盲重试；确认没发出去才允许重试；清输入框
# --------------------------------------------------------------------------- #
def test_send_failure_does_not_blindly_retry(tmp_path):
    module, holder = make_fake(send_error=OSError("剪贴板不可用"),
                               chat_info={"chat_name": CONTACT})
    out = _sender(module, tmp_path).send(CONTACT, "在吗")

    assert out["ok"] is False and out["status"] == "failed"
    assert out["stage"] == "send"
    assert _sent(holder.instances[-1]) == [("SendMsg", "在吗")]   # 只尝试一次
    assert out["error_key"].startswith("OSError")
    events = _log_events(tmp_path / "gateway.log")
    failed = [e for e in events if e["kind"] == "send_failed"]
    assert failed and failed[-1]["error_key"] == out["error_key"]
    assert failed[-1]["stage"] == "send"
    # 没有 clear_edit 能力时要明确记录 input_clear_failed，不能静默
    assert any(e["kind"] == "input_clear_failed" for e in events)


def test_send_failure_retries_once_when_confirmed_not_sent(tmp_path):
    box = FakeChatBox(who=CONTACT, editbox=FakeEditBox("在吗"))
    module, holder = make_fake(send_error=OSError("剪贴板不可用"),
                               chat_info={"chat_name": CONTACT},
                               chat_box=box)
    out = _sender(module, tmp_path).send(CONTACT, "在吗")

    assert out["ok"] is False
    # 确认输入框里还是原文 → 上一次确实没发出去 → 允许重试 1 次
    sends = sum(len(_sent(wx)) for wx in holder.instances)
    assert sends == 2
    assert out["attempts"] == 2


def test_send_failure_result_is_not_a_sent_status(tmp_path):
    module, _ = make_fake(send_result={"ok": False, "status": "失败"},
                          chat_info={"chat_name": CONTACT})
    out = _sender(module, tmp_path).send(CONTACT, "在吗")
    assert out["ok"] is False and out["status"] == "failed"


# --------------------------------------------------------------------------- #
# 4. 错误归一化 → error_key（address / 空白）
# --------------------------------------------------------------------------- #
def test_error_key_normalizes_object_addresses(tmp_path):
    errors = [OSError(f"boom at 0x{i:08x}") for i in (0x7F0011, 0xDEADBEEF)]
    keys = []
    for i, error in enumerate(errors):
        module, _ = make_fake(send_error=error, chat_info={"chat_name": CONTACT},
                              chat_box=FakeChatBox())
        out = _sender(module, tmp_path, send_retry_max=0).send(CONTACT, f"第{i}句")
        keys.append(out["error_key"])
    assert keys[0] == keys[1]
    assert "0xADDR" in keys[0]
    assert "0x7f0011" not in keys[0].lower()
    assert len(keys[0]) <= 60


def test_proactive_breaker_accepts_sender_error_key_as_same_key(tmp_path):
    from agi.wechat_gateway import proactive_worker
    module, _ = make_fake(send_error=OSError("boom at 0x7f0011"),
                          chat_info={"chat_name": CONTACT}, chat_box=FakeChatBox())
    out = _sender(module, tmp_path, send_retry_max=0).send(CONTACT, "在吗")
    assert proactive_worker.error_key(out) == out["error_key"]


# --------------------------------------------------------------------------- #
# 5. 句柄双验 / send_probe 配置
# --------------------------------------------------------------------------- #
def test_probe_logged_with_input_box_state(tmp_path):
    module, _ = make_fake(chat_info={"chat_name": CONTACT},
                          chat_box=FakeChatBox(editbox=FakeEditBox("")))
    _sender(module, tmp_path).send(CONTACT, "在吗")
    probes = [e for e in _log_events(tmp_path / "gateway.log") if e["kind"] == "send_probe"]
    assert probes and probes[-1]["input_box_exists"] is True
    assert probes[-1]["chat_name"] == CONTACT


def test_probe_disabled_does_not_log(tmp_path):
    module, holder = make_fake(chat_info={"chat_name": CONTACT},
                               chat_box=FakeChatBox())
    out = _sender(module, tmp_path, send_probe_enabled=False).send(CONTACT, "在吗")
    assert out["ok"] is True
    assert [e for e in _log_events(tmp_path / "gateway.log") if e["kind"] == "send_probe"] == []


# --------------------------------------------------------------------------- #
# 6. 失败兜底 = draft（Gateway 层，复用 drafts.py；调用方拿到 status/draft_id）
# --------------------------------------------------------------------------- #
def _gw(tmp_path, **overrides):
    cfg = {
        "enabled": True,
        "dry_run": False,
        "allowed_contacts": [CONTACT],
        "send_mode": "auto",
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
    }
    cfg.update(overrides)
    return Gateway(cfg)


def test_send_failure_returns_draft_status_and_id(tmp_path):
    g = _gw(tmp_path)
    out = g._on_send_failure(
        CONTACT, "发不出去的一句",
        "AttributeError: 'ProfileWnd' object has no attribute 'topui'")

    assert out["ok"] is False
    assert out["status"] == "draft"
    assert str(out["draft_id"]).startswith("D-")
    assert out["send_failed"] is True
    assert out["error_key"].startswith("AttributeError")
    rows = drafts.list_all(path=g.draft_path)
    assert len(rows) == 1 and rows[0]["id"] == out["draft_id"]
    assert rows[0]["reason"] == "send_failed"
    assert any(e["kind"] == "draft" for e in _log_events(tmp_path / "gateway.log"))


def test_send_failure_draft_is_not_duplicated(tmp_path):
    g = _gw(tmp_path)
    g._on_send_failure(CONTACT, "同一句", "wxauto boom")
    g._on_send_failure(CONTACT, "同一句", "wxauto boom")
    rows = drafts.list_all(path=g.draft_path)
    # 两次失败各落一条草稿即可，但一次失败绝不能落两条
    assert len(rows) == 2


# --------------------------------------------------------------------------- #
# 7. 配置项
# --------------------------------------------------------------------------- #
def test_resilience_config_defaults():
    assert gw_config.DEFAULTS["send_probe_enabled"] is True
    assert gw_config.DEFAULTS["send_retry_max"] == 1
