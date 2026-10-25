# -*- coding: utf-8 -*-
"""P5-a：发送层多媒体（text/emoji_text/image/gif/sticker 降级 + mixed 多气泡）。

全部使用 fake wx 模块：测试进程不 import wxauto4、不连接真实微信、不发真实消息。
items 契约见 ``agi/wechat_gateway/media.py``：
``{type: text|emoji_text|image|gif|sticker, text?, path?, name?}``。
"""
from __future__ import annotations

import json
import types

import pytest

from agi.wechat_gateway import config as gw_config
from agi.wechat_gateway import drafts, media
from agi.wechat_gateway import sender as sender_mod
from agi.wechat_gateway import server as server_mod
from agi.wechat_gateway.sender import WxAutoSender
from agi.wechat_gateway.server import Gateway

CONTACT = "好友A"
SUCCESS = {"status": "成功", "message": "发送成功", "data": None}


# --------------------------------------------------------------------------- #
# 素材 / fake wxauto4
# --------------------------------------------------------------------------- #
@pytest.fixture
def media_dir(tmp_path):
    directory = tmp_path / "assets"
    directory.mkdir()
    (directory / "p5a_666.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    (directory / "p5a_qinqin.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 64)
    (directory / "p5a_happy.gif").write_bytes(b"GIF89a" + b"2" * 64)
    (directory / "p5a_sticker.gif").write_bytes(b"GIF89a" + b"3" * 64)
    (directory / "p5a_bad.wxgf").write_bytes(b"WXGF" + b"4" * 64)
    (directory / "p5a_bad.gif").write_bytes(b"GIF89a" + b"5" * 64)
    (directory / "p5a_big.png").write_bytes(b"6" * 4096)
    return directory


class FakeWx:
    def __init__(self, holder):
        self.holder = holder
        self.calls: list[tuple] = []
        holder.instances.append(self)

    def ChatWith(self, who, exact=True):
        self.calls.append(("ChatWith", who, exact))
        if self.holder.chat_with_error is not None:
            raise self.holder.chat_with_error
        return who

    def ChatInfo(self):
        self.calls.append(("ChatInfo",))
        return dict(self.holder.chat_info)

    def SendMsg(self, text):
        self.calls.append(("SendMsg", text))
        self.holder.msg_calls.append(text)
        return _next_result(self.holder.msg_results, "msg")

    def SendFiles(self, path):
        self.calls.append(("SendFiles", path))
        self.holder.file_calls.append(path)
        return _next_result(self.holder.file_results, "file")


def _next_result(queue: list, kind: str):
    if not queue:
        return dict(SUCCESS)
    item = queue.pop(0)
    if isinstance(item, Exception):
        raise item
    return item


def make_fake(*, contact=CONTACT, msg_results=None, file_results=None,
              chat_info=None, chat_with_error=None):
    holder = types.SimpleNamespace(
        instances=[], msg_calls=[], file_calls=[],
        msg_results=list(msg_results or []),
        file_results=list(file_results or []),
        chat_info={"chat_name": contact} if chat_info is None else chat_info,
        chat_with_error=chat_with_error,
    )

    class WeChat:
        def __init__(self, **kwargs):
            self._impl = FakeWx(holder)
            self.calls = self._impl.calls

        def ChatWith(self, who, exact=True):
            return self._impl.ChatWith(who, exact)

        def ChatInfo(self):
            return self._impl.ChatInfo()

        def SendMsg(self, text):
            return self._impl.SendMsg(text)

        def SendFiles(self, path):
            return self._impl.SendFiles(path)

    return types.SimpleNamespace(WeChat=WeChat), holder


def _sender_cfg(media_dir, **overrides):
    cfg = {
        "media_enabled": True,
        "media_dirs": [str(media_dir)],
        "media_max_mb": 10,
        "media_send_interval_ms": 120,
        "sticker_fallback_emoji": "😄",
        "send_probe_enabled": True,
        "send_retry_max": 1,
    }
    cfg.update(overrides)
    return cfg


def _sender(module, tmp_path, media_dir, **overrides):
    return WxAutoSender(module=module, cfg=_sender_cfg(media_dir, **overrides),
                        log_path=tmp_path / "gateway.log", retry_delay=0)


def _events(path, kind):
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            event = json.loads(line)
            if event.get("kind") == kind:
                out.append(event)
    return out


@pytest.fixture
def sleeps(monkeypatch):
    recorded: list[float] = []
    monkeypatch.setattr(sender_mod.time, "sleep", lambda seconds: recorded.append(seconds))
    return recorded


# --------------------------------------------------------------------------- #
# 1. 能力矩阵
# --------------------------------------------------------------------------- #
def test_capabilities_matrix():
    caps = media.capabilities()
    assert caps["text"] is True and caps["unicode_emoji"] is True
    assert caps["image"] is True and caps["gif"] is True
    assert caps["sticker_native"] is False            # wxauto4 没有原生贴纸 API
    assert caps["sticker_as_image"] is True
    assert caps["mixed"] is True
    assert caps["media_enabled"] is False             # 默认关闭
    assert media.capabilities({"media_enabled": True})["media_enabled"] is True


def test_media_config_defaults():
    assert gw_config.DEFAULTS["media_enabled"] is False
    assert gw_config.DEFAULTS["media_dirs"] == []
    assert gw_config.DEFAULTS["media_max_mb"] == 10
    assert gw_config.DEFAULTS["media_send_interval_ms"] == 120
    assert gw_config.DEFAULTS["sticker_fallback_emoji"] == "😄"


# --------------------------------------------------------------------------- #
# 2. 四种基础类型：text / emoji_text / image / gif
# --------------------------------------------------------------------------- #
def test_text_emoji_image_gif_each_use_expected_api(tmp_path, media_dir, sleeps):
    module, holder = make_fake()
    sender = _sender(module, tmp_path, media_dir)
    results = sender.send_bubbles(CONTACT, [
        {"type": "text", "text": "你好"},
        {"type": "emoji_text", "text": "哈哈😄"},
        {"type": "image", "path": "p5a_666.png"},
        {"type": "gif", "path": "p5a_happy.gif"},
    ])

    assert [r["ok"] for r in results] == [True, True, True, True]
    assert [r["status"] for r in results] == ["sent"] * 4
    calls = holder.instances[0].calls
    sends = [c for c in calls if c[0] == "SendMsg"]
    files = [c for c in calls if c[0] == "SendFiles"]
    assert [c[1] for c in sends] == ["你好", "哈哈😄"]
    assert [c[1].replace("\\", "/").split("/")[-1] for c in files] == ["p5a_666.png", "p5a_happy.gif"]
    # mixed 间隔：4 条气泡 → 3 次 sleep，且都在 80–150ms
    assert len(sleeps) == 3
    assert all(0.08 <= value <= 0.15 for value in sleeps), sleeps
    # 每条都有 send_media 审计
    events = _events(tmp_path / "gateway.log", "send_media")
    assert len(events) == 4
    assert {e["type"] for e in events} == {"text", "emoji_text", "image", "gif"}


# --------------------------------------------------------------------------- #
# 3. 贴纸降级链：SendFiles(图) → emoji_text → 纯文本
# --------------------------------------------------------------------------- #
def test_sticker_falls_back_to_emoji_when_image_fails(tmp_path, media_dir, sleeps):
    module, holder = make_fake(file_results=[OSError("SendFiles 崩了")])
    sender = _sender(module, tmp_path, media_dir)
    results = sender.send_bubbles(CONTACT, [
        {"type": "sticker", "path": "p5a_sticker.gif", "name": "sticker"}])

    assert results[0]["ok"] is True
    assert results[0]["used"] == "emoji_text"
    assert results[0]["fallback_used"] == ["image", "emoji_text"]
    assert holder.msg_calls == ["😄"]
    assert holder.file_calls and holder.file_calls[0].endswith("p5a_sticker.gif")


def test_sticker_falls_back_to_plain_text_when_emoji_also_fails(tmp_path, media_dir, sleeps):
    module, holder = make_fake(
        file_results=[OSError("SendFiles 崩了")],
        msg_results=[OSError("SendMsg 崩了"), dict(SUCCESS)],   # 先 emoji 失败，再文本成功
    )
    sender = _sender(module, tmp_path, media_dir)
    results = sender.send_bubbles(CONTACT, [
        {"type": "sticker", "path": "p5a_sticker.gif", "name": "sticker"}])

    assert results[0]["ok"] is True
    assert results[0]["used"] == "text"
    assert results[0]["fallback_used"] == ["image", "emoji_text", "text"]
    assert holder.msg_calls == ["😄", "[p5a_sticker]"]


def test_wxgf_sticker_skips_image_step_and_falls_back(tmp_path, media_dir, sleeps):
    module, holder = make_fake()
    sender = _sender(module, tmp_path, media_dir)
    results = sender.send_bubbles(CONTACT, [
        {"type": "sticker", "path": "p5a_bad.wxgf"}])

    # .wxgf 不能作为文件发 → 直接走 emoji
    assert results[0]["ok"] is True
    assert results[0]["used"] == "emoji_text"
    assert results[0]["fallback_used"] == ["emoji_text"]
    assert holder.file_calls == []
    assert holder.msg_calls == ["😄"]


def test_resolve_sticker_rejects_wxgf_and_suggests(tmp_path, media_dir, sleeps):
    cfg = _sender_cfg(media_dir)
    assert media.resolve_sticker("p5a_sticker.gif", cfg) is not None
    assert media.resolve_sticker("p5a_bad.wxgf", cfg) is None
    suggestion = media.suggest_sticker_alternative("p5a_bad.wxgf", cfg)
    assert suggestion is not None and suggestion.endswith("p5a_bad.gif")


# --------------------------------------------------------------------------- #
# 4. mixed：顺序 + 间隔 + 单条 media 失败不影响后续文本
# --------------------------------------------------------------------------- #
def test_mixed_media_failure_does_not_drop_later_text(tmp_path, media_dir, sleeps):
    module, holder = make_fake(file_results=[OSError("图片发不出去")])
    sender = _sender(module, tmp_path, media_dir)
    results = sender.send_bubbles(CONTACT, [
        {"type": "text", "text": "第一句"},
        {"type": "image", "path": "p5a_666.png"},
        {"type": "text", "text": "第三句"},
    ])

    assert [r["ok"] for r in results] == [True, False, True]
    assert results[1]["status"] == "failed"
    calls = holder.instances[0].calls
    sends = [c[1] for c in calls if c[0] == "SendMsg"]
    assert sends == ["第一句", "第三句"]                 # 顺序不变，媒体失败不吞后续文本
    assert len(sleeps) == 2 and all(0.08 <= v <= 0.15 for v in sleeps)


def test_interval_is_clamped_to_80_150ms(tmp_path, media_dir, sleeps):
    for interval, low, high in ((1, 0.08, 0.08), (999, 0.15, 0.15), (120, 0.12, 0.12)):
        sleeps.clear()
        module, _holder = make_fake()
        sender = _sender(module, tmp_path, media_dir, media_send_interval_ms=interval)
        sender.send_bubbles(CONTACT, [{"type": "text", "text": "a"},
                                      {"type": "text", "text": "b"}])
        assert sleeps == [low], (interval, sleeps)


# --------------------------------------------------------------------------- #
# 5. validate_item
# --------------------------------------------------------------------------- #
def test_validate_item_rules(tmp_path, media_dir):
    cfg = _sender_cfg(media_dir, media_max_mb=0.001)      # ≈1KB
    good = tmp_path / "outside.png"
    good.write_bytes(b"x" * 16)

    assert media.validate_item({"type": "text", "text": "你好"}, cfg=cfg) == (True, "")
    assert media.validate_item({"type": "emoji_text", "text": "😄"}, cfg=cfg)[0] is True
    assert media.validate_item({"type": "text", "text": "  "}, cfg=cfg) == (False, "missing_text")
    assert media.validate_item({"type": "video", "path": "p5a_666.png"}, cfg=cfg) == (False, "bad_type")
    assert media.validate_item({"type": "image", "path": "p5a_666.png"}, cfg=cfg)[0] is True
    assert media.validate_item({"type": "image", "path": "p5a_missing.png"}, cfg=cfg) == (False, "not_found")
    assert media.validate_item({"type": "image", "path": "p5a_bad.wxgf"}, cfg=cfg) == (False, "wxgf_unsupported")
    assert media.validate_item({"type": "image", "path": "p5a_big.png"}, cfg=cfg) == (False, "too_large")
    assert media.validate_item({"type": "image", "path": "a.txt"}, cfg=cfg)[1] in ("not_found", "bad_ext")
    # 白名单外（绝对路径但不在 media_dirs 里）
    assert media.validate_item({"type": "image", "path": str(good)}, cfg=cfg) == (False, "outside_dirs")
    assert media.validate_item("not-a-dict", cfg=cfg) == (False, "not_a_dict")


# --------------------------------------------------------------------------- #
# 6. media_enabled=false：整体 blocked，不实例化 wxauto、不发任何东西
# --------------------------------------------------------------------------- #
def test_media_disabled_sends_nothing(tmp_path, media_dir, sleeps):
    module, holder = make_fake()
    sender = _sender(module, tmp_path, media_dir, media_enabled=False)
    results = sender.send_bubbles(CONTACT, [
        {"type": "text", "text": "你好"}, {"type": "image", "path": "p5a_666.png"}])

    assert [r["status"] for r in results] == ["blocked", "blocked"]
    assert all(r["error"] == "media_disabled" for r in results)
    assert holder.instances == [] and holder.msg_calls == [] and holder.file_calls == []
    events = _events(tmp_path / "gateway.log", "send_media")
    assert events and events[-1]["status"] == "blocked"


def test_chat_not_confirmed_fails_closed(tmp_path, media_dir, sleeps):
    module, holder = make_fake(chat_info={"chat_name": "别人"})
    sender = _sender(module, tmp_path, media_dir)
    results = sender.send_bubbles(CONTACT, [
        {"type": "text", "text": "你好"}, {"type": "image", "path": "p5a_666.png"}])

    assert all(r["ok"] is False for r in results)
    assert all("不匹配" in r["error"] for r in results)
    assert holder.msg_calls == [] and holder.file_calls == []


# --------------------------------------------------------------------------- #
# 7. Gateway 闸门：media_enabled / STOP / 静默 / 白名单 / dry-run
# --------------------------------------------------------------------------- #
def _gw(tmp_path, media_dir, **overrides):
    cfg = {
        "enabled": True, "port": 801, "dry_run": True,
        "allowed_contacts": [CONTACT], "send_mode": "auto",
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
        "proactive_enabled": True,
        "media_enabled": True,
        "media_dirs": [str(media_dir)],
        "media_send_interval_ms": 120,
        "sticker_fallback_emoji": "😄",
        "quiet_hours": [0, 0],
    }
    cfg.update(overrides)
    return Gateway(cfg, stop_path=overrides.pop("stop_path", None))


def _items():
    return [{"type": "text", "text": "来啦"}, {"type": "emoji_text", "text": "😄"}]


def test_gateway_media_disabled_blocked(tmp_path, media_dir):
    g = _gw(tmp_path, media_dir, media_enabled=False)
    out = g.send_media_batch(CONTACT, _items())
    assert out["status"] == "blocked" and out["error"] == "media_disabled"
    assert any(e.get("kind") == "send_blocked" and e.get("reason") == "media_disabled"
               for e in g.recent)


def test_gateway_stop_blocks_media(tmp_path, media_dir):
    stop = tmp_path / "STOP"
    stop.write_text("stopped\n", encoding="utf-8")
    g = _gw(tmp_path, media_dir, stop_path=stop)
    out = g.send_media_batch(CONTACT, _items())
    assert out["status"] == "blocked" and out["error"] == "stop_file"
    assert g.status()["proactive"]["stopped"] is True


def test_gateway_quiet_hours_blocks_proactive_media(tmp_path, media_dir):
    g = _gw(tmp_path, media_dir, quiet_hours=[0, 24])
    blocked = g.send_media_batch(CONTACT, _items(), proactive=True)
    assert blocked["status"] == "blocked" and blocked["error"] == "quiet_hours"
    # 被动回复必须回（P0-25 语义）：静默不拦 reactive
    reactive = g.send_media_batch(CONTACT, _items(), reactive=True)
    assert reactive["status"] == "planned"


def test_gateway_whitelist_blocks_media(tmp_path, media_dir):
    g = _gw(tmp_path, media_dir, allowed_contacts=[CONTACT])
    out = g.send_media_batch("陌生人", _items())
    assert out["status"] == "blocked" and out["error"] == "not_allowed"


def test_gateway_dry_run_returns_planned_and_audits(tmp_path, media_dir):
    g = _gw(tmp_path, media_dir)                     # dry_run=True 默认
    out = g.send_media_batch(CONTACT, _items())
    assert out["status"] == "planned" and out["ok"] is True
    assert [i["status"] for i in out["items"]] == ["planned", "planned"]
    events = [e for e in g.recent if e.get("kind") == "send_media"]
    assert len(events) == 2
    assert g.sent == 0 and g.failed == 0


def test_gateway_confirm_mode_drafts_only_text(tmp_path, media_dir):
    g = _gw(tmp_path, media_dir, send_mode="confirm", dry_run=False)
    out = g.send_media_batch(CONTACT, [
        {"type": "text", "text": "先落草稿"}, {"type": "image", "path": "p5a_666.png"}])
    assert out["status"] == "draft" and out["requires_confirm"] is True
    assert len(out["draft_ids"]) == 1
    rows = drafts.list_all(path=g.draft_path)
    assert len(rows) == 1 and rows[0]["text"] == "先落草稿"
    assert out["items"][1]["status"] == "skipped"


def test_gateway_real_path_parses_results_and_drafts_failed_text(tmp_path, media_dir, monkeypatch):
    g = _gw(tmp_path, media_dir, dry_run=False)
    fake_python = tmp_path / "python.exe"
    fake_python.write_bytes(b"")
    monkeypatch.setattr(server_mod, "WXAUTO_PYTHON", fake_python)
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env") or {}
        payload = {"ok": False, "items": [
            {"ok": True, "status": "sent", "type": "text", "fallback_used": [], "used": None},
            {"ok": False, "status": "failed", "type": "text",
             "error": "SendMsg 崩了", "error_key": "SendMsg 崩了",
             "fallback_used": [], "used": None},
        ]}
        return types.SimpleNamespace(stdout=json.dumps(payload, ensure_ascii=False),
                                     stderr="", returncode=1)

    monkeypatch.setattr(server_mod.subprocess, "run", fake_run)
    out = g.send_media_batch(CONTACT, [{"type": "text", "text": "第一句"},
                                       {"type": "text", "text": "第二句"}])

    assert "--items" in captured["cmd"]
    assert captured["env"]["WECHAT_MEDIA_ENABLED"] == "1"
    assert out["status"] == "partial" and out["sent"] == 1 and out["failed"] == 1
    assert len(out["draft_ids"]) == 1
    rows = drafts.list_all(path=g.draft_path)
    assert len(rows) == 1 and rows[0]["text"] == "第二句"
    assert g.sent == 1 and g.failed == 1
    events = [e for e in g.recent if e.get("kind") == "send_media"]
    assert len(events) == 2


# --------------------------------------------------------------------------- #
# 8. send_cli --items 契约（一行 JSON）
# --------------------------------------------------------------------------- #
def test_send_cli_items_outputs_single_line_json(monkeypatch, capsys, tmp_path, media_dir):
    from agi.wechat_gateway import send_cli

    def fake_send_bubbles(self, contact, items, *, dry_run=False, cfg=None):
        return [{"ok": True, "status": "sent", "type": "text"} for _ in items]

    monkeypatch.setattr(WxAutoSender, "send_bubbles", fake_send_bubbles)
    code = send_cli.main(["--contact", CONTACT,
                          "--items", json.dumps([{"type": "text", "text": "hi"}])])
    line = capsys.readouterr().out.strip()
    payload = json.loads(line)
    assert code == 0 and payload["ok"] is True and payload["items"][0]["status"] == "sent"

# --------------------------------------------------------------------------- #
# 9. 决策层（task-60）集成契约：Gateway.send_bubbles 返回逐条结果 list
# --------------------------------------------------------------------------- #
def test_gateway_send_bubbles_returns_per_item_list(tmp_path, media_dir):
    g = _gw(tmp_path, media_dir)                      # dry_run=True
    results = g.send_bubbles(CONTACT, _items())
    assert isinstance(results, list) and len(results) == 2
    assert [r["status"] for r in results] == ["planned", "planned"]
    assert all({"ok", "status", "type", "fallback_used", "used"} <= set(r) for r in results)
    assert [r["type"] for r in results] == ["text", "emoji_text"]

