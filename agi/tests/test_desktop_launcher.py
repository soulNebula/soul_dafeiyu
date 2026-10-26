# -*- coding: utf-8 -*-
"""P7-a：零依赖桌面启动器单元测试。

全部 monkeypatch：**不起真实进程、不弹窗口、不碰 801/802 真实服务**。
launcher 是 `tools/desktop/launcher.py`（纯标准库，不是 agi 包），用 importlib 加载。
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "desktop_launcher", ROOT / "tools" / "desktop" / "launcher.py")
launcher = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(launcher)


class FakeProc:
    def __init__(self, pid: int = 4321):
        self.pid = pid

    def poll(self):
        return None


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """所有等待轮询不真睡（等待逻辑用 fake 状态驱动）。"""
    monkeypatch.setattr(launcher.time, "sleep", lambda *_a, **_k: None)


# --------------------------------------------------------------------------- #
# 1. 纯函数：浏览器参数 / CLI / 浏览器探测 / state
# --------------------------------------------------------------------------- #
def test_build_browser_argv_contract(tmp_path):
    argv = launcher.build_browser_argv(Path("msedge.exe"), launcher.CONSOLE_URL, tmp_path)
    assert argv[0] == "msedge.exe"
    assert "--app=http://127.0.0.1:802/" in argv
    assert "--window-size=1440,900" in argv
    assert f"--user-data-dir={tmp_path}" in argv


def test_parse_args_defaults_and_flags():
    args = launcher.parse_args([])
    assert args.persist is True and args.no_open is False
    assert args.stop is False and args.status is False
    assert args.start_timeout > 0
    assert launcher.parse_args(["--exit-with-window"]).persist is False
    assert launcher.parse_args(["--persist"]).persist is True
    args = launcher.parse_args(["--no-open", "--stop", "--status",
                                "--log-dir", "x", "--state-path", "y",
                                "--profile-dir", "z", "--start-timeout", "9"])
    assert args.no_open and args.stop and args.status
    assert args.log_dir == "x" and args.state_path == "y"
    assert args.profile_dir == "z" and args.start_timeout == 9.0


def test_find_browser_prefers_edge_then_chrome_then_default(tmp_path, monkeypatch):
    edge = tmp_path / "msedge.exe"
    chrome = tmp_path / "chrome.exe"
    edge.write_bytes(b"")
    chrome.write_bytes(b"")
    monkeypatch.setattr(launcher, "_candidate_browsers",
                        lambda: [(edge, "edge"), (chrome, "chrome")])
    assert launcher.find_browser() == (edge, "edge")

    edge.unlink()
    assert launcher.find_browser() == (chrome, "chrome")

    monkeypatch.setattr(launcher, "_candidate_browsers", lambda: [])
    monkeypatch.setattr(launcher.shutil, "which", lambda _name: None)
    assert launcher.find_browser() == (None, "default")


def test_state_roundtrip_drops_internal_proc(tmp_path):
    state_path = tmp_path / "state.json"
    launcher.save_state({"launcher_pid": 1, "window": {"pid": 2, "_proc": object()}},
                        state_path)
    data = launcher.load_state(state_path)
    assert data["launcher_pid"] == 1
    assert data["window"]["pid"] == 2
    assert "_proc" not in data["window"]
    # 损坏的 state 不崩
    state_path.write_text("{broken", encoding="utf-8")
    assert launcher.load_state(state_path) == {}


def test_launch_window_default_browser_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "_candidate_browsers", lambda: [])
    monkeypatch.setattr(launcher.shutil, "which", lambda _name: None)
    opened: list[str] = []
    monkeypatch.setattr(launcher, "_open_default_browser",
                        lambda url: opened.append(url) or True)
    window = launcher.launch_window(url=launcher.CONSOLE_URL, profile_dir=tmp_path)
    assert window["mode"] == "default" and window["pid"] is None
    assert opened == [launcher.CONSOLE_URL]
    assert (tmp_path).is_dir()


# --------------------------------------------------------------------------- #
# 2. 服务探测：复用 / 启动 / 超时回滚
# --------------------------------------------------------------------------- #
def _spec():
    return {"port": 801, "health": "http://127.0.0.1:801/health",
            "log": "desktop_gateway.log", "cmd": ["python", "-V"]}


def test_ensure_service_reuses_running_port(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "port_open", lambda *a, **k: True)
    monkeypatch.setattr(launcher, "http_ok", lambda *a, **k: True)

    def _boom(*_a, **_k):
        raise AssertionError("端口已在监听时不允许重复起服务")

    monkeypatch.setattr(launcher, "_spawn_service", _boom)
    result = launcher.ensure_service("gateway", _spec(), tmp_path, start_timeout=1.0)
    assert result == {"name": "gateway", "port": 801, "started": False,
                      "pid": None, "reused": True, "healthy": True}


def test_ensure_service_start_timeout_kills_and_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "port_open", lambda *a, **k: False)
    monkeypatch.setattr(launcher, "http_ok", lambda *a, **k: False)
    monkeypatch.setattr(launcher, "_spawn_service", lambda *_a, **_k: FakeProc(4321))
    terminated: list[int] = []
    monkeypatch.setattr(launcher, "_terminate_pid",
                        lambda pid, **_k: terminated.append(pid) or True)

    with pytest.raises(launcher.LauncherError) as exc:
        launcher.ensure_service("gateway", _spec(), tmp_path, start_timeout=0.01)
    assert "启动超时" in str(exc.value)
    assert terminated == [4321]


def test_ensure_service_started_and_healthy(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_port_open(*_a, **_k):
        calls["n"] += 1
        return calls["n"] > 1          # 第一次（启动前）没开，之后开了

    monkeypatch.setattr(launcher, "port_open", fake_port_open)
    monkeypatch.setattr(launcher, "http_ok", lambda *a, **k: True)
    monkeypatch.setattr(launcher, "_spawn_service", lambda *_a, **_k: FakeProc(999))
    result = launcher.ensure_service("gateway", _spec(), tmp_path, start_timeout=1.0)
    assert result["started"] is True and result["pid"] == 999
    assert result["reused"] is False and result["healthy"] is True


# --------------------------------------------------------------------------- #
# 3. main：--no-open / --status / --stop / 单实例 / --exit-with-window
# --------------------------------------------------------------------------- #
def _fake_services(monkeypatch, started: bool = True):
    pids = {"gateway": 111, "console": 222} if started else {}
    calls: list[str] = []

    def fake_ensure(name, spec, _log_dir, **_kwargs):
        calls.append(name)
        return {"name": name, "port": spec["port"], "started": started,
                "pid": pids.get(name), "reused": not started, "healthy": True}

    monkeypatch.setattr(launcher, "ensure_service", fake_ensure)
    return calls


def test_main_no_open_does_not_launch_window(tmp_path, monkeypatch):
    _fake_services(monkeypatch, started=True)
    monkeypatch.setattr(launcher, "launch_window",
                        lambda **_k: pytest.fail("--no-open 不允许开窗口"))
    state_path = tmp_path / "state.json"
    code = launcher.main(["--no-open", "--log-dir", str(tmp_path / "logs"),
                          "--state-path", str(state_path)])
    assert code == 0
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["started_pids"] == {"gateway": 111, "console": 222}
    assert state["window"] is None


def test_main_status_prints_json(tmp_path, monkeypatch, capsys):
    state_path = tmp_path / "state.json"
    launcher.save_state({"started_pids": {"console": 222},
                         "window": {"mode": "edge", "pid": 333, "url": launcher.CONSOLE_URL}},
                        state_path)
    monkeypatch.setattr(launcher, "port_open", lambda port, *a, **k: port == 802)
    monkeypatch.setattr(launcher, "http_ok", lambda *a, **k: True)
    monkeypatch.setattr(launcher, "_pid_alive", lambda pid: pid == 222)

    code = launcher.main(["--status", "--state-path", str(state_path)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["ok"] is True
    assert payload["services"]["console"]["port_open"] is True
    assert payload["services"]["gateway"]["port_open"] is False
    assert payload["services"]["console"]["owned_pid"] == 222
    assert payload["services"]["console"]["owned_alive"] is True
    assert payload["window"]["mode"] == "edge"
    assert payload["running"] is True


def test_main_stop_only_kills_state_pids(tmp_path, monkeypatch, capsys):
    state_path = tmp_path / "state.json"
    launcher.save_state({"started_pids": {"gateway": 111, "console": 222},
                         "window": {"pid": 333}}, state_path)
    # 只有 gateway 还活着；console/window 已自行退出
    monkeypatch.setattr(launcher, "_pid_alive", lambda pid: pid == 111)
    terminated: list[int] = []
    monkeypatch.setattr(launcher, "_terminate_pid",
                        lambda pid, **_k: terminated.append(pid) or True)

    code = launcher.main(["--stop", "--state-path", str(state_path)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and terminated == [111]
    assert payload["stopped"] == {"gateway": 111}
    assert set(payload["skipped"]) == {"console", "window"}
    assert not state_path.exists()                    # state 清空


def test_main_single_instance_only_reopens_window(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    launcher.save_state({"launcher_pid": os.getpid(), "started_pids": {}}, state_path)
    monkeypatch.setattr(launcher, "ensure_service",
                        lambda *a, **k: pytest.fail("已有实例时不允许再起服务"))
    opened: list[dict] = []
    monkeypatch.setattr(launcher, "launch_window",
                        lambda **kwargs: opened.append(kwargs) or {
                            "mode": "edge", "pid": 777, "url": launcher.CONSOLE_URL,
                            "argv": [], "profile_dir": str(tmp_path), "_proc": None})
    code = launcher.main(["--state-path", str(state_path),
                          "--profile-dir", str(tmp_path / "profile")])
    assert code == 0 and len(opened) == 1
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["window"]["pid"] == 777


def test_main_start_failure_rolls_back_owned_service(tmp_path, monkeypatch):
    def fake_ensure(name, spec, _log_dir, **_kwargs):
        if name == "gateway":
            return {"name": name, "port": 801, "started": True, "pid": 111,
                    "reused": False, "healthy": True}
        raise launcher.LauncherError("console 启动超时")

    monkeypatch.setattr(launcher, "ensure_service", fake_ensure)
    monkeypatch.setattr(launcher, "_pid_alive", lambda pid: pid == 111)
    terminated: list[int] = []
    monkeypatch.setattr(launcher, "_terminate_pid",
                        lambda pid, **_k: terminated.append(pid) or True)
    state_path = tmp_path / "state.json"
    code = launcher.main(["--no-open", "--log-dir", str(tmp_path / "logs"),
                          "--state-path", str(state_path)])
    assert code == 2
    assert terminated == [111]                        # 回滚自己刚起的 gateway
    assert not state_path.exists()


def test_main_exit_with_window_stops_owned_services(tmp_path, monkeypatch):
    _fake_services(monkeypatch, started=True)
    monkeypatch.setattr(launcher, "launch_window", lambda **_k: {
        "mode": "edge", "pid": 333, "url": launcher.CONSOLE_URL,
        "argv": [], "profile_dir": str(tmp_path / "profile"), "_proc": None})
    # 窗口先"开着"一次，随后"关闭"
    alive = iter([True, False])
    monkeypatch.setattr(launcher, "_window_alive", lambda _w: next(alive, False))
    monkeypatch.setattr(launcher, "_pid_alive", lambda pid: True)
    terminated: list[int] = []
    monkeypatch.setattr(launcher, "_terminate_pid",
                        lambda pid, **_k: terminated.append(pid) or True)
    state_path = tmp_path / "state.json"
    code = launcher.main(["--exit-with-window", "--log-dir", str(tmp_path / "logs"),
                          "--state-path", str(state_path)])
    assert code == 0
    assert set(terminated) == {111, 222, 333}
    assert not state_path.exists()


def test_main_persist_keeps_services_after_window(tmp_path, monkeypatch):
    _fake_services(monkeypatch, started=True)
    monkeypatch.setattr(launcher, "launch_window", lambda **_k: {
        "mode": "edge", "pid": 333, "url": launcher.CONSOLE_URL,
        "argv": [], "profile_dir": str(tmp_path / "profile"), "_proc": None})
    monkeypatch.setattr(launcher, "wait_window_closed",
                        lambda *_a, **_k: pytest.fail("--persist 不应等窗口关闭"))
    monkeypatch.setattr(launcher, "_terminate_pid",
                        lambda *_a, **_k: pytest.fail("--persist 不应杀进程"))
    state_path = tmp_path / "state.json"
    code = launcher.main(["--log-dir", str(tmp_path / "logs"),
                          "--state-path", str(state_path)])
    assert code == 0
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["started_pids"] == {"gateway": 111, "console": 222}
    assert state["launcher_pid"] is None              # 默认模式 launcher 已退出
    assert state["window"]["pid"] == 333




def test_main_exit_with_window_keeps_services_when_window_unknown(tmp_path, monkeypatch):
    """浏览器进程交出去、查不到窗口 → --exit-with-window 也不误停服务（保留 state）。"""
    _fake_services(monkeypatch, started=True)
    monkeypatch.setattr(launcher, "launch_window", lambda **_k: {
        "mode": "edge", "pid": 333, "url": launcher.CONSOLE_URL,
        "argv": [], "profile_dir": str(tmp_path / "profile"), "_proc": None})
    monkeypatch.setattr(launcher, "wait_window_closed", lambda *_a, **_k: False)
    monkeypatch.setattr(launcher, "_terminate_pid",
                        lambda *_a, **_k: pytest.fail("无法确认窗口时不允许误停服务"))
    state_path = tmp_path / "state.json"
    code = launcher.main(["--exit-with-window", "--log-dir", str(tmp_path / "logs"),
                          "--state-path", str(state_path)])
    assert code == 0
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["started_pids"] == {"gateway": 111, "console": 222}

# --------------------------------------------------------------------------- #
# 4. 窗口存活判定 / 等待
# --------------------------------------------------------------------------- #
def test_watch_window_closed_detects_close_and_grace(monkeypatch):
    # 观察不到窗口 → False（调用方应保留服务，不能误停）
    assert launcher.wait_window_closed(None, poll=0.001, startup_grace=0.0) is False

    alive = iter([True, True, False])
    monkeypatch.setattr(launcher, "_window_alive",
                        lambda _w: next(alive, False))
    assert launcher.wait_window_closed({"profile_dir": "x"}, poll=0.001) is True


def test_window_alive_uses_process_handle(monkeypatch):
    # 进程句柄还活着 → 视为窗口未关（不调 PowerShell / 不用真实进程）
    monkeypatch.setattr(launcher, "_edge_process_count", lambda _p: 0)
    monkeypatch.setattr(launcher, "_profile_locked", lambda _p: False)

    class Stick:
        def poll(self):
            return None

    assert launcher._window_alive({"profile_dir": "x", "_proc": Stick()}) is True
    assert launcher._window_alive(None) is False


def test_edge_process_count_parses_powershell(monkeypatch):
    if os.name != "nt":
        pytest.skip("_edge_process_count 只在 Windows 用 PowerShell")
    called = {}

    def fake_run(cmd, **kwargs):
        called["cmd"] = cmd
        return type("P", (), {"stdout": "2\n", "stderr": ""})()

    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    assert launcher._edge_process_count("profile-x") == 2
    assert "msedge.exe" in " ".join(called["cmd"])


def test_profile_locked_marks_window_alive(tmp_path, monkeypatch):
    (tmp_path / "SingletonLock").write_text("lock", encoding="utf-8")
    assert launcher._profile_locked(tmp_path) is True
    monkeypatch.setattr(launcher, "_edge_process_count", lambda _p: 0)
    # 没有进程句柄，但 profile 锁存在 → 活着
    assert launcher._window_alive({"profile_dir": str(tmp_path)}) is True
