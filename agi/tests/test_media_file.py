# -*- coding: utf-8 -*-
"""P0-144：文件（PDF/Word/压缩包）发送链路测试（mock SendFiles，不碰真实微信）。"""
from __future__ import annotations

import pytest

from agi.wechat_gateway import media
from agi.wechat_gateway.sender import WxAutoSender


def _pdf(tmp_path, name="doc.pdf", size: int | None = None):
    target = tmp_path / name
    target.write_bytes(b"%PDF-1.4\nfake pdf\n")
    if size:
        with target.open("r+b") as fh:
            fh.truncate(size)
    return target


def _cfg(tmp_path, **extra):
    cfg = {"media_enabled": True, "media_dirs": [str(tmp_path)],
           "media_max_mb": 10, "media_file_max_mb": 50,
           "media_send_interval_ms": 80, "media_audit": False}
    cfg.update(extra)
    return cfg


# --------------------------------------------------------------------------- #
# media.validate_item：file 类型
# --------------------------------------------------------------------------- #
def test_validate_file_pdf_ok(tmp_path):
    pdf = _pdf(tmp_path)
    ok, code = media.validate_item(
        {"type": "file", "path": str(pdf), "name": pdf.name}, cfg=_cfg(tmp_path))
    assert ok is True and code == ""
    assert media.item_label({"type": "file", "name": pdf.name}) == pdf.name


def test_validate_file_rejects_bad_ext(tmp_path):
    exe = tmp_path / "evil.exe"
    exe.write_bytes(b"MZ")
    ok, code = media.validate_item({"type": "file", "path": str(exe)},
                                   cfg=_cfg(tmp_path))
    assert ok is False and code == "bad_ext"


def test_validate_file_rejects_too_large(tmp_path):
    huge = _pdf(tmp_path, "big.pdf", size=51 * 1024 * 1024)
    ok, code = media.validate_item({"type": "file", "path": str(huge)},
                                   cfg=_cfg(tmp_path))
    assert ok is False and code == "too_large"


def test_validate_file_rejects_outside_media_dirs(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    pdf = _pdf(other)
    cfg = _cfg(tmp_path)
    cfg["media_dirs"] = [str(tmp_path / "allowed")]
    ok, code = media.validate_item({"type": "file", "path": str(pdf)}, cfg=cfg)
    assert ok is False and code == "outside_dirs"


def test_validate_file_uses_file_max_config(tmp_path):
    pdf = _pdf(tmp_path, size=2048)
    ok, code = media.validate_item({"type": "file", "path": str(pdf)},
                                   cfg=_cfg(tmp_path, media_file_max_mb=0.001))
    assert ok is False and code == "too_large"


# --------------------------------------------------------------------------- #
# sender.send_bubbles：file → SendFiles，失败不降级图片
# --------------------------------------------------------------------------- #
class FakeWx:
    def __init__(self, *, with_sendfiles=True, fail_file=False, ads=False):
        self.sent_files: list[str] = []
        self.sent_texts: list[str] = []
        self._with = with_sendfiles
        self._fail = fail_file
        self.ChatBox = None

    def ChatWith(self, contact, exact=True):
        return contact

    def ChatInfo(self):
        return {"chat_name": "文件传输助手"}

    def SendMsg(self, text):
        self.sent_texts.append(str(text))
        return {"status": "成功"}

    def SendFiles(self, path):
        if not getattr(self, "_with", True):
            raise AttributeError("SendFiles")
        self.sent_files.append(str(path))
        if getattr(self, "_fail", False):
            return {"status": "失败"}
        return {"status": "成功"}


class FakeModule:
    def __init__(self, wx):
        self.WeChat = lambda ads=False: wx


def test_send_bubbles_file_calls_sendfiles(tmp_path):
    pdf = _pdf(tmp_path)
    wx = FakeWx()
    sender = WxAutoSender(FakeModule(wx), cfg=_cfg(tmp_path))
    results = sender.send_bubbles("文件传输助手",
                                  [{"type": "file", "path": str(pdf),
                                    "name": pdf.name}],
                                  cfg=_cfg(tmp_path))
    assert len(results) == 1
    out = results[0]
    assert out["ok"] is True and out["status"] == "sent"
    assert out["type"] == "file" and out["used"] == "file"
    assert wx.sent_files == [str(pdf)]
    assert wx.sent_texts == []


def test_send_bubbles_file_dry_run(tmp_path):
    pdf = _pdf(tmp_path)
    sender = WxAutoSender(FakeModule(FakeWx()), cfg=_cfg(tmp_path))
    results = sender.send_bubbles("文件传输助手",
                                  [{"type": "file", "path": str(pdf)}],
                                  dry_run=True, cfg=_cfg(tmp_path))
    assert results[0]["ok"] is True and results[0]["status"] == "planned"


def test_send_bubbles_file_without_sendfiles_fails_no_image_fallback(tmp_path):
    pdf = _pdf(tmp_path)
    wx = FakeWx(with_sendfiles=False)
    sender = WxAutoSender(FakeModule(wx), cfg=_cfg(tmp_path))
    results = sender.send_bubbles("文件传输助手",
                                  [{"type": "file", "path": str(pdf)}],
                                  cfg=_cfg(tmp_path))
    out = results[0]
    assert out["ok"] is False and out["status"] == "failed"
    assert out["used"] is None and out["fallback_used"] == []
    assert wx.sent_files == [] and wx.sent_texts == []


def test_send_bubbles_file_sendfailure_stays_failed(tmp_path):
    pdf = _pdf(tmp_path)
    wx = FakeWx(fail_file=True)
    sender = WxAutoSender(FakeModule(wx), cfg=_cfg(tmp_path))
    results = sender.send_bubbles("文件传输助手",
                                  [{"type": "file", "path": str(pdf)}],
                                  cfg=_cfg(tmp_path))
    out = results[0]
    assert out["ok"] is False and out["status"] == "failed"
    assert wx.sent_files == [str(pdf)]
    assert wx.sent_texts == []          # 不降级 emoji/文本
