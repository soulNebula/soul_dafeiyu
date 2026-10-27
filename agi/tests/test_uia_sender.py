# -*- coding: utf-8 -*-
"""P0-send：.NET UIA 发送后端测试（mock ps1/subprocess/窗口，不碰真实微信）。"""

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agi.wechat_gateway import uia_sender


# --------------------------------------------------------------------------- #
# 基础/ps1
# --------------------------------------------------------------------------- #
def test_available_requires_script(monkeypatch, tmp_path):
    monkeypatch.setattr(uia_sender, "POWERSHELL", "powershell.exe")
    monkeypatch.setattr(uia_sender, "SCRIPT", tmp_path / "missing.ps1")
    assert uia_sender.available() is False
    script = tmp_path / "q.ps1"
    script.write_text("", encoding="utf-8")
    monkeypatch.setattr(uia_sender, "SCRIPT", script)
    monkeypatch.setattr(uia_sender.shutil, "which",
                        lambda exe: "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")
    assert uia_sender.available() is True


def test_query_parses_last_json_line(monkeypatch):
    payload = ('{"ok":true,"window":{"name":"微信"},'
               '"contact":{"found":true,"x":1,"y":2,"w":3,"h":4},'
               '"input":{"found":false},"search_box":{"found":false},"error":""}')
    monkeypatch.setattr(uia_sender, "SCRIPT", Path("C:/fake/uia_query.ps1"))
    monkeypatch.setattr(uia_sender.Path, "is_file", lambda self: True)

    def fake_run(cmd, **kwargs):
        return SimpleNamespace(returncode=0, stdout="warning line\n" + payload + "\n",
                               stderr="")

    result = uia_sender._query("-Contact", "文件传输助手", runner=fake_run)
    assert result["ok"] is True and result["contact"]["found"] is True


def test_query_invalid_json_and_timeout(monkeypatch):
    monkeypatch.setattr(uia_sender, "SCRIPT", Path("C:/fake/uia_query.ps1"))
    monkeypatch.setattr(uia_sender.Path, "is_file", lambda self: True)
    bad = lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="not json", stderr="")
    result = uia_sender._query(runner=bad)
    assert result["ok"] is False and result["error"] == "invalid_json"

    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 15)

    result = uia_sender._query(runner=boom)
    assert result["ok"] is False and result["error"] == "timeout"


def test_query_script_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(uia_sender, "SCRIPT", tmp_path / "nope.ps1")
    result = uia_sender._query()
    assert result["ok"] is False and result["error"] == "script_missing"


def test_find_contact_flat_rect(monkeypatch):
    monkeypatch.setattr(uia_sender, "_query", lambda *a, **k: {
        "ok": True, "window": {"name": "微信"},
        "contact": {"found": True, "x": 10, "y": 20, "w": 100, "h": 40},
        "search_box": {"found": True, "x": 1, "y": 2, "w": 3, "h": 4},
        "input": {"found": False}, "error": ""})
    result = uia_sender.find_contact("文件传输助手")
    assert result["ok"] is True
    assert uia_sender._rect_center(result["contact"]) == (60, 40)
    # 兼容旧接口 find_session 的嵌套 rect
    legacy = uia_sender.find_session("文件传输助手")
    assert legacy["contact"]["rect"] == {"x": 10, "y": 20, "w": 100, "h": 40}


# --------------------------------------------------------------------------- #
# 窗口/点击 fake
# --------------------------------------------------------------------------- #
class FakeWin:
    def __init__(self, capture_size=(1000, 800)):
        self.clicks: list[tuple[int, int]] = []
        self.keys: list[int] = []
        self.capture_size = capture_size

    def find_wechat_window(self):
        return object()

    def bring_to_front(self, info):
        return True

    def is_foreground(self, info):
        return True

    def capture(self, info):
        return SimpleNamespace(size=self.capture_size)

    def real_click(self, info, x, y):
        self.clicks.append((int(x), int(y)))
        return True

    def post_click(self, info, x, y):
        self.clicks.append((int(x), int(y)))
        return True

    def press_key(self, vk):
        self.keys.append(int(vk))


class FakeClipboard:
    def __init__(self):
        self.texts: list[str] = []

    def set_text(self, value):
        self.texts.append(str(value))
        return True


@pytest.fixture
def send_env(monkeypatch):
    win = FakeWin()
    clipboard = FakeClipboard()
    monkeypatch.setattr(uia_sender, "available", lambda: True)
    monkeypatch.setattr(uia_sender.win_send, "_load",
                        lambda: ((win, clipboard), ""))
    monkeypatch.setattr(uia_sender.win_send, "_verify_chat_open",
                        lambda win_, info, contact: "ok")
    monkeypatch.setattr(uia_sender.win_send, "_ctrl_v", lambda: None)
    monkeypatch.setattr(uia_sender.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(uia_sender, "find_contact", lambda contact: {
        "ok": True, "window": {"name": "微信"},
        "contact": {"found": True, "x": 90, "y": 100, "w": 300, "h": 80},
        "search_box": {"found": True, "x": 130, "y": 55, "w": 180, "h": 25},
        "input": {"found": False}, "error": ""})
    monkeypatch.setattr(uia_sender, "find_input", lambda: {
        "ok": True, "contact": {"found": False},
        "input": {"found": True, "x": 410, "y": 940, "w": 760, "h": 70},
        "search_box": {"found": False}, "error": ""})
    return {"win": win, "clipboard": clipboard,
            "cfg": {"fallback_enabled": True},
            "contact_rect": {"found": True, "x": 90, "y": 100, "w": 300, "h": 80}}


# --------------------------------------------------------------------------- #
# 发送路径
# --------------------------------------------------------------------------- #
def test_send_success_uses_contact_and_input_rects(send_env):
    out = uia_sender.send_to_contact("文件传输助手", "hello", send_env["cfg"])
    assert out["ok"] is True and out["status"] == "sent"
    assert out["method"] == "uia" and out["verified"] is True
    win = send_env["win"]
    assert win.clicks[0] == (240, 140)       # 联系人矩形中心
    assert win.clicks[1] == (790, 975)       # 输入框矩形中心
    assert send_env["clipboard"].texts == ["hello"]
    assert win.keys == [0x0D]                # 回车发送
    assert out["opened_by"] == "contact_rect"


def test_send_aborts_on_title_mismatch(send_env, monkeypatch):
    monkeypatch.setattr(uia_sender.win_send, "_verify_chat_open",
                        lambda win_, info, contact: "mismatch")
    out = uia_sender.send_to_contact("目标", "hi", send_env["cfg"])
    assert out["ok"] is False and out["sent"] is False
    assert "verify_mismatch" in out["error"]
    assert send_env["clipboard"].texts == []   # 没粘贴正文
    assert send_env["win"].keys == []          # 没回车


def test_send_aborts_when_verify_unknown(send_env, monkeypatch):
    monkeypatch.setattr(uia_sender.win_send, "_verify_chat_open",
                        lambda win_, info, contact: "unknown")
    out = uia_sender.send_to_contact("目标", "hi", send_env["cfg"])
    assert out["ok"] is False and "verify_unknown" in out["error"]


def test_send_contact_not_found_no_search_aborts(send_env):
    send_env["cfg"]["fallback_input_box"] = None
    out = uia_sender.send_to_contact(
        "目标", "hi", send_env["cfg"])
    # 默认 fake find_contact 找到了；这里改成未找到 + 无搜索框
    send_env["win"].clicks.clear()
    send_env["clipboard"].texts.clear()
    uia_sender.find_contact = lambda contact: {
        "ok": True, "window": {"name": "微信"},
        "contact": {"found": False}, "search_box": {"found": False},
        "input": {"found": False}, "error": ""}
    out = uia_sender.send_to_contact("目标", "hi", send_env["cfg"])
    assert out["ok"] is False and out["error"] == "contact_not_found"
    assert send_env["clipboard"].texts == []


def test_send_falls_back_to_search_box(send_env, monkeypatch):
    monkeypatch.setattr(uia_sender, "find_contact", lambda contact: {
        "ok": True, "window": {"name": "微信"},
        "contact": {"found": False},
        "search_box": {"found": True, "x": 130, "y": 55, "w": 180, "h": 25},
        "input": {"found": False}, "error": ""})
    out = uia_sender.send_to_contact("目标", "hi", send_env["cfg"])
    assert out["ok"] is True and out["opened_by"] == "search"
    # 先粘贴联系人名，再粘贴正文
    assert send_env["clipboard"].texts == ["目标", "hi"]
    assert send_env["win"].keys == [0x0D, 0x0D]


def test_send_input_fallback_ratio(send_env, monkeypatch):
    monkeypatch.setattr(uia_sender, "find_input", lambda: {
        "ok": True, "contact": {"found": False}, "input": {"found": False},
        "search_box": {"found": False}, "error": ""})
    send_env["cfg"]["fallback_input_box"] = [0.5, 0.9]
    out = uia_sender.send_to_contact("文件传输助手", "hi", send_env["cfg"])
    assert out["ok"] is True and out["input_source"] == "fallback_ratio"
    assert send_env["win"].clicks[1] == (500, 720)


def test_send_requires_fallback_enabled():
    out = uia_sender.send_to_contact("目标", "hi", {"fallback_enabled": False})
    assert out["ok"] is False and out["status"] == "disabled"


def test_send_unavailable(monkeypatch):
    monkeypatch.setattr(uia_sender, "available", lambda: False)
    out = uia_sender.send_to_contact("目标", "hi", {"fallback_enabled": True})
    assert out["ok"] is False and out["error"] == "uia_unavailable"


def test_send_window_not_found(monkeypatch):
    class _Win:
        def find_wechat_window(self):
            return None

    monkeypatch.setattr(uia_sender, "available", lambda: True)
    monkeypatch.setattr(uia_sender.win_send, "_load",
                        lambda: ((_Win(), FakeClipboard()), ""))
    out = uia_sender.send_to_contact("目标", "hi", {"fallback_enabled": True})
    assert out["ok"] is False and out["error"] == "没找到微信窗口"


def test_smoke_is_read_only(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(uia_sender, "available", lambda: True)
    monkeypatch.setattr(uia_sender, "find_contact",
                        lambda contact: calls.append("contact") or {"ok": True,
                                                                    "contact": {}})
    monkeypatch.setattr(uia_sender, "find_input",
                        lambda: calls.append("input") or {"ok": True, "input": {}})
    result = uia_sender.smoke("文件传输助手")
    assert result["ok"] is True and result["note"].startswith("只读")
    assert calls == ["contact", "input"]

# -*- coding: utf-8 -*-
"""P0-2：媒体发送 + 常驻 UIA worker 测试（全 mock，不碰真实微信）。"""

import io
import json
import sys
import time
import types
from pathlib import Path

import pytest

from agi.wechat_gateway import uia_sender


# --------------------------------------------------------------------------- #
# send_media：图片 / GIF / 输入框缺失 / 标题不匹配
# --------------------------------------------------------------------------- #
def _media_env(tmp_path, monkeypatch):
    win = FakeWin()
    clipboard = FakeClipboard()
    monkeypatch.setattr(uia_sender, "available", lambda: True)
    monkeypatch.setattr(uia_sender.win_send, "_load",
                        lambda: ((win, clipboard), ""))
    monkeypatch.setattr(uia_sender.win_send, "_verify_chat_open",
                        lambda win_, info, contact: "ok")
    monkeypatch.setattr(uia_sender.win_send, "_ctrl_v", lambda: None)
    monkeypatch.setattr(uia_sender.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(uia_sender, "find_contact", lambda contact: {
        "ok": True, "window": {"name": "微信"},
        "contact": {"found": True, "x": 90, "y": 100, "w": 300, "h": 80},
        "search_box": {"found": False}, "input": {"found": False}, "error": ""})
    monkeypatch.setattr(uia_sender, "find_input", lambda: {
        "ok": True, "contact": {"found": False},
        "input": {"found": True, "x": 410, "y": 940, "w": 760, "h": 70},
        "search_box": {"found": False}, "error": ""})
    copied: list[str] = []
    monkeypatch.setattr(uia_sender, "_copy_image_to_clipboard",
                        lambda path: copied.append(str(path)) or True)
    image = tmp_path / "pic.png"
    image.write_bytes(b"png")
    return {"win": win, "clipboard": clipboard, "copied": copied,
            "cfg": {"fallback_enabled": True}, "image": image}


def test_send_media_image_success(tmp_path, monkeypatch):
    env = _media_env(tmp_path, monkeypatch)
    out = uia_sender.send_media(
        "文件传输助手", {"type": "image", "path": str(env["image"])}, env["cfg"])
    assert out["ok"] is True and out["status"] == "sent"
    assert out["type"] == "image" and out["animated"] is False
    assert out["verified"] is True and out["sent"] is True
    assert env["copied"] == [str(env["image"])]
    assert env["clipboard"].texts == []          # 图片走 CF_DIB，不写文本剪贴板
    assert env["win"].keys == [0x0D]             # 只回车发送
    assert env["win"].clicks[0] == (240, 140)
    assert env["win"].clicks[1] == (790, 975)


def test_send_media_gif_marks_static(tmp_path, monkeypatch):
    env = _media_env(tmp_path, monkeypatch)
    gif = tmp_path / "a.gif"
    gif.write_bytes(b"gif")
    out = uia_sender.send_media(
        "文件传输助手", {"type": "gif", "path": str(gif)}, env["cfg"])
    assert out["ok"] is True and out["type"] == "gif"
    assert out["animated"] is False
    assert "静态" in out["warning"]


def test_send_media_input_not_found_aborts(tmp_path, monkeypatch):
    env = _media_env(tmp_path, monkeypatch)
    monkeypatch.setattr(uia_sender, "find_input", lambda: {
        "ok": True, "contact": {"found": False}, "input": {"found": False},
        "search_box": {"found": False}, "error": ""})
    out = uia_sender.send_media(
        "文件传输助手", {"type": "image", "path": str(env["image"])}, env["cfg"])
    assert out["ok"] is False and out["error"] == "input_not_found"
    assert env["copied"] == [] and env["win"].keys == []


def test_send_media_title_mismatch_does_not_paste(tmp_path, monkeypatch):
    env = _media_env(tmp_path, monkeypatch)
    monkeypatch.setattr(uia_sender.win_send, "_verify_chat_open",
                        lambda win_, info, contact: "mismatch")
    out = uia_sender.send_media(
        "目标", {"type": "image", "path": str(env["image"])}, env["cfg"])
    assert out["ok"] is False and "verify_mismatch" in out["error"]
    assert env["copied"] == [] and env["win"].keys == []


def test_send_media_missing_path_and_disabled(tmp_path):
    item = {"type": "image", "path": str(tmp_path / "missing.png")}
    out = uia_sender.send_media("目标", item, {"fallback_enabled": True})
    assert out["ok"] is False and out["error"] == "media_path_missing"
    disabled = uia_sender.send_media("目标", item, {"fallback_enabled": False})
    assert disabled["status"] == "disabled"


def test_copy_image_to_clipboard_uses_cf_dib(tmp_path, monkeypatch):
    image = tmp_path / "x.png"
    image.write_bytes(b"fake")

    class FakeImage:
        mode = "RGBA"

        def __init__(self, path):
            self.path = path

        def convert(self, mode):
            self.mode = mode
            return self

        def save(self, buf, fmt):
            assert fmt == "BMP"
            buf.write(b"BMPHEADER12345" + b"DIBDATA")

    pil = types.ModuleType("PIL")
    pil.Image = types.SimpleNamespace(open=lambda path: FakeImage(path))
    win32clipboard = types.ModuleType("win32clipboard")
    recorded: dict = {}
    win32clipboard.OpenClipboard = lambda: recorded.setdefault("open", True)
    win32clipboard.EmptyClipboard = lambda: recorded.setdefault("empty", True)
    win32clipboard.SetClipboardData = lambda fmt, data: recorded.update(
        {"fmt": fmt, "data": data})
    win32clipboard.CloseClipboard = lambda: recorded.setdefault("close", True)
    win32con = types.ModuleType("win32con")
    win32con.CF_DIB = 8
    monkeypatch.setitem(sys.modules, "PIL", pil)
    monkeypatch.setitem(sys.modules, "win32clipboard", win32clipboard)
    monkeypatch.setitem(sys.modules, "win32con", win32con)

    assert uia_sender._copy_image_to_clipboard(image) is True
    assert recorded["fmt"] == 8 and recorded["data"] == b"DIBDATA"
    assert recorded["open"] and recorded["empty"] and recorded["close"]


# --------------------------------------------------------------------------- #
# UIAClient：正常 / 超时 / 崩溃重启
# --------------------------------------------------------------------------- #
class _FakeStdin(io.StringIO):
    def flush(self):
        super().flush()


class _FakeProc:
    def __init__(self, lines: list[bytes | str], *, hang: bool = False):
        self.stdin = _FakeStdin()
        self.stdout = io.StringIO("".join(
            line.decode("utf-8") if isinstance(line, bytes) else line
            for line in lines))
        self._hang = hang
        self.killed = False
        self._reads = 0

    def poll(self):
        return None

    def kill(self):
        self.killed = True

    def readline(self):  # 兼容真实 Popen 接口（client 内部通过 stdout.readline）
        return self.stdout.readline()


class _BlockingStdout:
    def __init__(self, delay: float):
        self.delay = delay

    def readline(self):
        time.sleep(self.delay)
        return ""


class _HangingProc(_FakeProc):
    def __init__(self):
        super().__init__([])
        self.stdout = _BlockingStdout(5.0)


def test_uiaclient_normal_request():
    payload = '{"ok":true,"input":{"found":true,"x":1,"y":2,"w":3,"h":4}}\n'
    proc = _FakeProc([payload])
    client = uia_sender.UIAClient(script=Path("fake.ps1"),
                                  spawn=lambda: proc, timeout=1.0)
    result = client.find_input()
    assert result["ok"] is True and result["input"]["found"] is True
    assert client.starts == 1
    assert json.loads(proc.stdin.getvalue().splitlines()[-1]) == {"cmd": "input"}
    client.close()


def test_uiaclient_timeout_kills_and_errors():
    proc = _HangingProc()
    client = uia_sender.UIAClient(script=Path("fake.ps1"),
                                  spawn=lambda: proc, timeout=0.05)
    result = client.find_input()
    assert result["ok"] is False and result["error"] == "timeout"
    assert proc.killed is True
    client.close()


def test_uiaclient_restarts_once_after_crash():
    good = _FakeProc(
        ['{"ok":true,"input":{"found":true,"x":9,"y":9,"w":9,"h":9}}\n'])
    procs = [_FakeProc([""]), good]      # 第一次 EOF → 第二次成功
    client = uia_sender.UIAClient(script=Path("fake.ps1"),
                                  spawn=lambda: procs.pop(0), timeout=1.0)
    result = client.find_input()
    assert result["ok"] is True and result["input"]["x"] == 9
    assert client.starts == 2 and client.restarts == 1
    client.close()


# --------------------------------------------------------------------------- #
# server：媒体批失败 → UIA 逐条兜底
# --------------------------------------------------------------------------- #
def test_server_media_batch_uses_uia_fallback(tmp_path, monkeypatch):
    from agi.wechat_gateway.server import Gateway
    from agi.wechat_gateway import server as server_mod

    image = tmp_path / "pic.png"
    image.write_bytes(b"png")
    py = tmp_path / "py.exe"
    py.write_text("", encoding="utf-8")
    monkeypatch.setattr(server_mod, "WXAUTO_PYTHON", py)

    class _Proc:
        stdout = ('{"items":[{"ok":false,"status":"failed","type":"image",'
                  '"error":"wxauto boom"}]}')
        stderr = ""

    monkeypatch.setattr(server_mod.subprocess, "run",
                        lambda *a, **k: _Proc())
    media_calls: list[tuple] = []

    def fake_send_media(contact, item, cfg):
        media_calls.append((contact, dict(item)))
        return {"ok": True, "status": "sent", "type": "image",
                "animated": False, "sent": True, "method": "uia"}

    monkeypatch.setattr(uia_sender, "send_media", fake_send_media)
    gw = Gateway({
        "enabled": True, "port": 801, "dry_run": False, "send_mode": "auto",
        "media_enabled": True, "allowed_contacts": ["好友A"],
        "monitored_contacts": ["好友A"], "admin_token": "tok_secure_123",
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
        "quiet_hours": [0, 0], "min_interval_s": 0,
        "fallback_enabled": True, "fallback_send_mode": "uia",
    }, stop_path=tmp_path / "STOP", config_path=tmp_path / "config.json")

    batch = gw.send_media_batch("好友A", [{"type": "image", "path": str(image)}])
    assert batch["ok"] is True and batch["status"] == "sent"
    assert batch["sent"] == 1 and media_calls and media_calls[0][0] == "好友A"
    item = batch["items"][0]
    assert item["ok"] is True and "uia_media" in (item.get("fallback_used") or [])
    assert any(e.get("kind") == "fallback" and e.get("method") == "uia_media"
               for e in gw.recent)
