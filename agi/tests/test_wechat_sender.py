# -*- coding: utf-8 -*-
"""WxAutoSender 单元测试。

全部使用注入的 fake wxauto 模块：测试进程不导入 wxauto4，不启动微信，
也不会发送任何真实消息。
"""
from __future__ import annotations

import types

import pytest

from agi.wechat_gateway.sender import MAX_CHARS, WxAutoSender

CONTACT = "张三"
SUCCESS = {"status": "成功", "message": "发送成功", "data": None}


class FakeWeChatBase:
    """模拟 wxauto4 WeChat；记录构造参数和调用顺序。"""

    def __init__(self, holder, **kwargs):
        self._holder = holder
        self.ctor_kwargs = kwargs
        self.calls = []
        holder.instances.append(self)

    def ChatWith(self, who, exact=False):
        self.calls.append(("ChatWith", who, exact))
        if self.chat_with_error:
            raise self.chat_with_error
        return self.chat_with_result

    def ChatInfo(self):
        self.calls.append(("ChatInfo",))
        if self.chat_info_error:
            raise self.chat_info_error
        return self.chat_info

    def SendMsg(self, text):
        self.calls.append(("SendMsg", text))
        if self.send_error:
            raise self.send_error
        return self.send_result


def make_fake(**overrides):
    """返回 (fake module, holder)；holder.instances 记录构造出的 WeChat。"""
    holder = types.SimpleNamespace(instances=[])
    config = {
        "chat_info": {"chat_name": CONTACT, "chat_type": "friend"},
        "chat_with_result": None,
        "send_result": dict(SUCCESS),
        "chat_with_error": None,
        "chat_info_error": None,
        "send_error": None,
    }
    config.update(overrides)

    class WeChat(FakeWeChatBase):
        def __init__(self, **kwargs):
            super().__init__(holder, **kwargs)
            for key, value in config.items():
                setattr(self, key, value)

    module = types.SimpleNamespace(WeChat=WeChat)
    return module, holder


def _sent_calls(wx):
    return [call for call in wx.calls if call[0] == "SendMsg"]


def test_send_success_verifies_chat_then_sends():
    module, holder = make_fake()
    sender = WxAutoSender(module=module)

    out = sender.send(CONTACT, "你好")

    assert out["ok"] is True
    assert out["status"] == "sent"
    assert out["contact"] == CONTACT
    assert out["text"] == "你好"
    assert len(holder.instances) == 1

    wx = holder.instances[0]
    assert wx.ctor_kwargs == {"ads": False}
    assert wx.calls == [
        ("ChatWith", CONTACT, True),
        ("ChatInfo",),
        ("SendMsg", "你好"),
    ]


def test_chat_name_mismatch_fails_without_sending():
    # ChatInfo 有名字时是权威结果，不能被 ChatWith 的返回值覆盖。
    module, holder = make_fake(
        chat_info={"chat_name": "李四", "chat_type": "friend"},
        chat_with_result=CONTACT,
    )
    out = WxAutoSender(module=module).send(CONTACT, "你好")

    assert out["ok"] is False
    assert out["status"] == "failed"
    assert "会话不匹配" in out["error"]
    assert "李四" in out["error"]
    wx = holder.instances[0]
    assert _sent_calls(wx) == []


@pytest.mark.parametrize(
    "chat_info",
    [
        {},
        None,
        {"chat_name": ""},
        {"chat_type": "friend"},
    ],
)
def test_empty_chat_info_falls_back_to_chat_with(chat_info):
    module, holder = make_fake(chat_info=chat_info, chat_with_result=CONTACT)
    out = WxAutoSender(module=module).send(CONTACT, "你好")

    assert out["ok"] is True
    assert out["status"] == "sent"
    assert _sent_calls(holder.instances[0]) == [("SendMsg", "你好")]


@pytest.mark.parametrize(
    "switched",
    [
        CONTACT,
        {"chat_name": CONTACT},
        {"who": CONTACT},
        {"data": {"chat_name": CONTACT}},
        {"data": {"who": CONTACT}},
        types.SimpleNamespace(who=CONTACT),
        types.SimpleNamespace(chat_name=CONTACT),
    ],
)
def test_chat_with_fallback_accepts_common_return_shapes(switched):
    module, holder = make_fake(chat_info={}, chat_with_result=switched)
    out = WxAutoSender(module=module).send(CONTACT, "你好")

    assert out["status"] == "sent"
    assert len(_sent_calls(holder.instances[0])) == 1


def test_chat_info_empty_and_no_fallback_name_fails_closed():
    module, holder = make_fake(chat_info={}, chat_with_result=None)
    out = WxAutoSender(module=module).send(CONTACT, "你好")

    assert out["ok"] is False
    assert out["status"] == "failed"
    assert "无法确认当前会话名" in out["error"]
    assert _sent_calls(holder.instances[0]) == []


def test_chat_info_status_word_is_not_used_as_chat_name():
    # ChatWith 返回 WxResponse 的 "成功" 不能被当成联系人名。
    module, holder = make_fake(
        chat_info={},
        chat_with_result={"status": "成功", "message": "成功", "data": None},
    )
    out = WxAutoSender(module=module).send(CONTACT, "你好")

    assert out["status"] == "failed"
    assert _sent_calls(holder.instances[0]) == []


def test_chat_with_failure_response_is_not_used_as_fallback_name():
    module, holder = make_fake(
        chat_info={},
        chat_with_result={"ok": False, "message": CONTACT},
    )
    out = WxAutoSender(module=module).send(CONTACT, "你好")

    assert out["ok"] is False
    assert out["status"] == "failed"
    assert _sent_calls(holder.instances[0]) == []


def test_chat_with_exception_returns_failed():
    module, holder = make_fake(chat_with_error=RuntimeError("找不到会话窗口"))
    out = WxAutoSender(module=module).send(CONTACT, "你好")

    assert out["ok"] is False
    assert out["status"] == "failed"
    assert "RuntimeError" in out["error"]
    assert "找不到会话窗口" in out["error"]
    assert _sent_calls(holder.instances[0]) == []


def test_send_exception_returns_failed():
    module, holder = make_fake(send_error=OSError("剪贴板不可用"))
    out = WxAutoSender(module=module).send(CONTACT, "你好")

    assert out["ok"] is False
    assert out["status"] == "failed"
    assert "OSError" in out["error"]
    assert "剪贴板不可用" in out["error"]
    assert _sent_calls(holder.instances[0]) == [("SendMsg", "你好")]


def test_chat_info_exception_returns_failed():
    module, holder = make_fake(chat_info_error=RuntimeError("UI 读取失败"))
    out = WxAutoSender(module=module).send(CONTACT, "你好")

    assert out["status"] == "failed"
    assert "UI 读取失败" in out["error"]
    assert _sent_calls(holder.instances[0]) == []


def test_constructor_exception_returns_failed():
    class BrokenWeChat:
        def __init__(self, **kwargs):
            raise RuntimeError("未找到已登录的客户端主窗口")

    module = types.SimpleNamespace(WeChat=BrokenWeChat)
    out = WxAutoSender(module=module).send(CONTACT, "你好")

    assert out["ok"] is False
    assert out["status"] == "failed"
    assert "未找到已登录" in out["error"]


def test_dry_run_does_not_touch_wxauto():
    module, holder = make_fake()
    sender = WxAutoSender(module=module)

    out = sender.send(CONTACT, "你好", dry_run=True)

    assert out["ok"] is True
    assert out["status"] == "planned"
    assert out["contact"] == CONTACT
    assert out["text"] == "你好"
    assert holder.instances == []


def test_dry_run_does_not_import_missing_wxauto():
    sender = WxAutoSender(module_name="__wxauto4_missing_for_test__")

    out = sender.send(CONTACT, "你好", dry_run=True)

    assert out["status"] == "planned"
    # 真发路径才延迟导入；导入失败必须变成 failed，而不是抛异常。
    failed = sender.send(CONTACT, "你好")
    assert failed["ok"] is False
    assert failed["status"] == "failed"
    assert "ModuleNotFoundError" in failed["error"] or "No module named" in failed["error"]


def test_injected_module_is_used_without_importing():
    module, holder = make_fake()
    out = WxAutoSender(module, module_name="__should_not_import__").send(CONTACT, "你好")

    assert out["status"] == "sent"
    assert len(holder.instances) == 1


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_empty_text_is_rejected(text):
    module, holder = make_fake()
    out = WxAutoSender(module=module).send(CONTACT, text)

    assert out == {"ok": False, "status": "failed", "error": "消息不能为空"}
    assert holder.instances == []


@pytest.mark.parametrize("contact", ["", "   ", None])
def test_empty_contact_is_rejected(contact):
    module, holder = make_fake()
    out = WxAutoSender(module=module).send(contact, "你好")

    assert out == {"ok": False, "status": "failed", "error": "contact 不能为空"}
    assert holder.instances == []


def test_too_long_text_is_rejected():
    module, holder = make_fake()
    text = "字" * (MAX_CHARS + 1)
    out = WxAutoSender(module=module).send(CONTACT, text)

    assert out["ok"] is False
    assert out["status"] == "failed"
    assert str(MAX_CHARS) in out["error"]
    assert holder.instances == []


def test_exact_max_length_is_allowed():
    module, holder = make_fake()
    text = "字" * MAX_CHARS
    out = WxAutoSender(module=module).send(CONTACT, text, dry_run=True)

    assert out["status"] == "planned"
    assert out["text"] == text
    assert holder.instances == []


def test_message_text_is_preserved_not_stripped():
    module, holder = make_fake()
    out = WxAutoSender(module=module).send(CONTACT, "  你好  ")

    assert out["status"] == "sent"
    assert _sent_calls(holder.instances[0]) == [("SendMsg", "  你好  ")]


@pytest.mark.parametrize(
    "response",
    [
        None,
        False,
        {"status": "失败", "message": "发送失败"},
        {"status": "错误", "message": "未知错误"},
        {"ok": False},
        {"success": False},
        {"message": "bad"},
        {"status": "unexpected"},
        {},
        object(),
        0,
        1,
    ],
)
def test_unclear_or_failed_send_result_is_failed(response):
    module, holder = make_fake(send_result=response)
    out = WxAutoSender(module=module).send(CONTACT, "你好")

    assert out["ok"] is False
    assert out["status"] == "failed"
    assert "error" in out
    assert len(_sent_calls(holder.instances[0])) == 1


@pytest.mark.parametrize(
    "response",
    [
        True,
        "成功",
        {"status": "成功", "message": "成功", "data": None},
        {"status": "SENT"},
        {"status": "ok"},
        {"ok": True},
        {"success": True},
    ],
)
def test_explicit_success_send_result_is_sent(response):
    module, holder = make_fake(send_result=response)
    out = WxAutoSender(module=module).send(CONTACT, "你好")

    assert out["ok"] is True
    assert out["status"] == "sent"


@pytest.mark.parametrize(
    "response",
    [
        {"status": "成功", "ok": False},
        {"status": "失败", "ok": True},
        {"status": "成功", "success": False},
    ],
)
def test_contradictory_send_result_is_failed(response):
    module, holder = make_fake(send_result=response)
    out = WxAutoSender(module=module).send(CONTACT, "你好")

    assert out["ok"] is False
    assert out["status"] == "failed"
