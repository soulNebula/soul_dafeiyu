# -*- coding: utf-8 -*-
"""P7-d：桌面增强（托盘 / 桌面通知 / 开机自启）单元测试。

原则：**不执行 PowerShell、不弹通知、不写真实注册表/启动目录、不联网**。
- 托盘/自启的 PS 脚本只做静态检查（关键字/契约）；
- 通知逻辑调用 launcher.py 里的纯函数（collect_notifications 等）；
- 自启用 monkeypatch 的临时「启动目录」+ 假注册表。
"""
from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "desktop_launcher", ROOT / "tools" / "desktop" / "launcher.py")
launcher = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(launcher)

DESKTOP = ROOT / "tools" / "desktop"
TRAY_PS1 = DESKTOP / "tray.ps1"
AUTOSTART_PS1 = DESKTOP / "autostart.ps1"
ICON_ICO = DESKTOP / "icon.ico"


def _ps_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _ts(hour: int, minute: int = 0, day: int = 26) -> float:
    return time.mktime((2026, 10, day, hour, minute, 0, 0, 0, -1))


DAY = _ts(12)       # 白天（非静默）
NIGHT = _ts(23, 30)  # 夜间静默


def _line(event: dict) -> str:
    return json.dumps(event, ensure_ascii=False)


def _send_sent(ts: float = DAY, text: str = "今天下雨了，记得带伞") -> str:
    return _line({"ts": ts, "kind": "proactive_send", "status": "sent",
                  "friend": "好友A", "text": text})


def _send_blocked(ts: float = DAY) -> str:
    return _line({"ts": ts, "kind": "send_blocked", "friend": "好友A",
                  "reason": "quiet_hours"})


def _send_failed(ts: float = DAY) -> str:
    return _line({"ts": ts, "kind": "send_failed", "friend": "好友A",
                  "error_key": "AttributeError: ProfileWnd.topui"})


def _discarded(ts: float = DAY) -> str:
    return _line({"ts": ts, "kind": "proactive_discarded", "friend": "好友A",
                  "reason": "grounding"})


def _stale(ts: float = DAY) -> str:
    return _line({"ts": ts, "kind": "gateway_stale", "idle_s": 8640.1})


def _breaker(ts: float = DAY) -> str:
    return _line({"ts": ts, "kind": "proactive_breaker_open", "consecutive": 3,
                  "error_key": "ProfileWnd.topui"})


# --------------------------------------------------------------------------- #
# 1. 托盘 / 自启脚本：静态检查（不执行 PS）
# --------------------------------------------------------------------------- #
def test_tray_script_static_contract():
    text = _ps_text(TRAY_PS1)
    # 单实例 + WinForms 托盘
    assert "System.Windows.Forms" in text
    assert "NotifyIcon" in text
    assert "Mutex" in text and "SoulCompanionTray" in text
    assert "ShowBalloonTip" in text
    assert "add_MouseDoubleClick" in text and "Open-Console" in text
    # 菜单项（task 要求的全部）
    for label in ("打开控制台", "启动主动消息", "停止主动消息",
                  "紧急停止 STOP", "恢复", "查看状态", "打开日志目录", "退出托盘"):
        assert label in text, f"托盘菜单缺少：{label}"
    # HTTP 端点 + token 来源（从 config.json 读 admin_token）
    assert "127.0.0.1:802" in text
    assert "/api/config" in text
    assert "/api/proactive/stop" in text and "/api/proactive/resume" in text
    assert "/api/status" in text
    assert "X-Admin-Token" in text
    assert "config.json" in text and "admin_token" in text
    # 通知：由 Python 纯函数产出，PS 只显示
    assert "--notify-once" in text
    assert "notifications" in text
    assert "NoNotify" in text
    assert "icon.ico" in text


def test_autostart_script_static_contract():
    text = _ps_text(AUTOSTART_PS1)
    for word in ("install", "uninstall", "status", "UseRegistry",
                 "autostart", "launcher.py"):
        assert word in text, f"autostart.ps1 缺少：{word}"


def test_icon_file_is_valid_ico():
    data = ICON_ICO.read_bytes()
    assert data[:4] == b"\x00\x00\x01\x00"      # ICO magic
    count = int.from_bytes(data[4:6], "little")
    assert count >= 1


# --------------------------------------------------------------------------- #
# 2. 通知纯函数：5 类事件 / 去重 / 夜间只报错误
# --------------------------------------------------------------------------- #
def test_collect_notifications_covers_five_event_types():
    lines = [_send_sent(DAY), _send_blocked(DAY + 1), _send_failed(DAY + 2),
             _discarded(DAY + 3), _stale(DAY + 4), _breaker(DAY + 5)]
    notifications, state = launcher.collect_notifications(
        {}, lines, now=DAY + 100, quiet_hours=(23, 8))

    kinds = [n["kind"] for n in notifications]
    assert kinds == ["proactive_send", "send_blocked", "send_failed",
                     "proactive_discarded", "gateway_stale",
                     "proactive_breaker_open"]
    by_kind = {n["kind"]: n for n in notifications}
    assert by_kind["proactive_send"]["level"] == "info"
    assert "已发：" in by_kind["proactive_send"]["body"]
    assert "今天下雨了" in by_kind["proactive_send"]["body"]
    assert by_kind["send_blocked"]["level"] == "error"
    assert "未发送" in by_kind["send_blocked"]["body"]
    assert "quiet_hours" in by_kind["send_blocked"]["body"]
    assert "topui" in by_kind["send_failed"]["body"]
    assert "丢弃" in by_kind["proactive_discarded"]["title"]
    assert by_kind["gateway_stale"]["level"] == "warn"
    assert "8640.1" in by_kind["gateway_stale"]["body"]
    assert "熔断" in by_kind["proactive_breaker_open"]["title"]
    assert by_kind["proactive_breaker_open"]["level"] == "error"
    assert len(state["seen"]) == 6
    assert state["updated_at"] == DAY + 100
    assert state["quiet_hours"] == [23, 8]


def test_collect_notifications_dedupes_same_event_within_60s():
    first = launcher.collect_notifications(
        {}, [_send_sent(DAY)], now=DAY, quiet_hours=(23, 8))
    notifications, state = first
    assert len(notifications) == 1

    same = launcher.collect_notifications(
        state, [_send_sent(DAY + 30)], now=DAY + 30, quiet_hours=(23, 8))
    assert same[0] == []                       # 60s 内同事件去重
    later = launcher.collect_notifications(
        same[1], [_send_sent(DAY + 61)], now=DAY + 61, quiet_hours=(23, 8))
    assert len(later[0]) == 1                  # 超过 60s 再报


def test_collect_notifications_night_only_errors():
    lines = [
        _send_sent(NIGHT),                     # 正常发送 → 夜间不报
        _send_blocked(NIGHT + 1),              # 错误 → 夜间照报
        _stale(NIGHT + 2),                     # 错误 → 夜间照报
    ]
    notifications, _state = launcher.collect_notifications(
        {}, lines, now=NIGHT + 100, quiet_hours=(23, 8))
    kinds = [n["kind"] for n in notifications]
    assert "proactive_send" not in kinds
    assert "send_blocked" in kinds and "gateway_stale" in kinds

    # 白天同一个正常发送要报
    day_notifications, _ = launcher.collect_notifications(
        {}, [_send_sent(DAY)], now=DAY, quiet_hours=(23, 8))
    assert len(day_notifications) == 1


def test_night_rule_handles_cross_midnight_and_disabled():
    assert launcher._is_night(_ts(23, 30), (23, 8)) is True
    assert launcher._is_night(_ts(0, 30), (23, 8)) is True
    assert launcher._is_night(_ts(7, 59), (23, 8)) is True
    assert launcher._is_night(_ts(8, 0), (23, 8)) is False
    assert launcher._is_night(_ts(12), (23, 8)) is False
    assert launcher._is_night(_ts(12), (0, 0)) is False
    assert launcher._is_night(_ts(12), None) is False


def test_collect_notifications_ignores_bad_and_irrelevant_lines():
    lines = ["", "not-json", _line({"ts": DAY, "kind": "inbound"}),
             _line({"ts": DAY, "kind": "proactive_schedule"}),
             _line({"k ind": "x"}),
             _line({"ts": DAY, "kind": "proactive_send", "status": "failed",
                    "error": "boom"})]
    notifications, _ = launcher.collect_notifications(
        {}, lines, now=DAY, quiet_hours=(23, 8))
    assert len(notifications) == 1            # 只有 failed 的 proactive_send
    assert notifications[0]["level"] == "error"


def test_collect_notifications_prunes_old_seen_entries():
    state = {"seen": {"old-key": DAY - 7200, "keep-key": DAY - 10}}
    _notifications, new_state = launcher.collect_notifications(
        state, [], now=DAY, quiet_hours=(23, 8))
    assert "old-key" not in new_state["seen"]
    assert "keep-key" in new_state["seen"]


def test_quiet_hours_from_config(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"quiet_hours": [1, 5]}), encoding="utf-8")
    monkeypatch.setattr(launcher, "GATEWAY_CONFIG_PATH", cfg)
    assert launcher._quiet_hours_from_config() == (1, 5)

    cfg.write_text(json.dumps({"quiet_hours": []}), encoding="utf-8")
    assert launcher._quiet_hours_from_config() == (23, 8)   # 非法 → 默认

    monkeypatch.setattr(launcher, "GATEWAY_CONFIG_PATH", tmp_path / "missing.json")
    assert launcher._quiet_hours_from_config() == (23, 8)


# --------------------------------------------------------------------------- #
# 3. 日志增量读取 + notifications_once
# --------------------------------------------------------------------------- #
def test_read_new_log_lines_incremental(tmp_path):
    log = tmp_path / "gateway.log"
    log.write_text(_send_sent(DAY) + "\n", encoding="utf-8")
    lines, state = launcher.read_new_log_lines(log, {})
    assert len(lines) == 1 and state["offset"] == log.stat().st_size

    lines, state = launcher.read_new_log_lines(log, state)
    assert lines == [] and state["offset"] == log.stat().st_size

    with log.open("a", encoding="utf-8") as handle:
        handle.write(_stale(DAY + 10) + "\n")
    lines, state = launcher.read_new_log_lines(log, state)
    assert len(lines) == 1 and "gateway_stale" in lines[0]

    # 文件被截断/轮转：offset > size → 从头重新读
    log.write_text(_send_blocked(DAY) + "\n", encoding="utf-8")
    lines, _state = launcher.read_new_log_lines(log, state)
    assert lines and "send_blocked" in lines[0]


def test_read_new_log_lines_skips_history_tail(tmp_path):
    log = tmp_path / "gateway.log"
    log.write_text(("x" * 100 + "\n") * 50, encoding="utf-8")
    lines, state = launcher.read_new_log_lines(log, {}, max_bytes=200)
    # 从近尾部开始读：半行会被丢掉，只返回完整行，offset 不倒退
    assert 0 < state["offset"] <= log.stat().st_size
    assert lines and all(line.startswith("x") for line in lines)
    assert not lines[0].endswith(chr(13))


def test_notifications_once_writes_state_and_dedupes(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "_quiet_hours_from_config", lambda: (23, 8))
    log = tmp_path / "gateway.log"
    log.write_text(_send_sent(DAY) + "\n", encoding="utf-8")
    state_path = tmp_path / "notify.json"

    first = launcher.notifications_once(log_path=log, state_path=state_path, now=DAY)
    assert first["ok"] is True and len(first["notifications"]) == 1
    assert state_path.is_file()
    saved = json.loads(state_path.read_text(encoding="utf-8"))
    assert saved["offset"] == log.stat().st_size

    second = launcher.notifications_once(log_path=log, state_path=state_path, now=DAY + 1)
    assert second["lines"] == 0 and second["notifications"] == []


# --------------------------------------------------------------------------- #
# 4. launcher 集成：--tray / 单实例 / --status 托盘 / 通知开关
# --------------------------------------------------------------------------- #
def test_parse_args_new_desktop_flags():
    args = launcher.parse_args([])
    assert args.tray is False and args.no_notify is False
    assert args.notify_once is False and args.autostart is None
    args = launcher.parse_args(["--tray", "--no-notify", "--notify-once",
                                "--notify-log", "x.log", "--notify-state", "s.json",
                                "--autostart", "install", "--autostart-registry"])
    assert args.tray and args.no_notify and args.notify_once
    assert args.notify_log == "x.log" and args.notify_state == "s.json"
    assert args.autostart == "install" and args.autostart_registry is True


def test_spawn_tray_argv_and_no_notify(tmp_path, monkeypatch):
    fake_tray = tmp_path / "tray.ps1"
    fake_tray.write_text("# fake", encoding="utf-8")
    monkeypatch.setattr(launcher, "TRAY_SCRIPT", fake_tray)
    captured: dict = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            self.pid = 4242

    monkeypatch.setattr(launcher.subprocess, "Popen", FakePopen)
    proc = launcher._spawn_tray(no_notify=True)
    assert proc.pid == 4242
    argv = captured["argv"]
    assert str(fake_tray) in argv
    assert "-NoNotify" in argv
    assert "--notify-once" not in " ".join(argv)     # 托盘自己定时调用

    launcher._spawn_tray(no_notify=False)
    assert "-NoNotify" not in captured["argv"]


def test_ensure_tray_single_instance_and_spawn(monkeypatch):
    monkeypatch.setattr(launcher, "_pid_alive", lambda pid: pid == os.getpid())
    result = launcher.ensure_tray({"tray_pid": os.getpid()})
    assert result == {"started": False, "pid": os.getpid()}

    class FakeProc:
        pid = 555

    monkeypatch.setattr(launcher, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(launcher, "_spawn_tray", lambda **_k: FakeProc())
    result = launcher.ensure_tray({})
    assert result == {"started": True, "pid": 555}


def test_main_tray_no_open_starts_tray_and_keeps_services(tmp_path, monkeypatch):
    started: list[str] = []
    monkeypatch.setattr(launcher, "ensure_service",
                        lambda name, spec, log_dir, **kw: started.append(name) or {
                            "name": name, "port": spec["port"], "started": False,
                            "pid": None, "reused": True, "healthy": True})
    tray_kwargs: dict = {}

    def fake_ensure_tray(state, *, no_notify=False):
        tray_kwargs["no_notify"] = no_notify
        state["tray_pid"] = 777
        return {"started": True, "pid": 777}

    monkeypatch.setattr(launcher, "ensure_tray", fake_ensure_tray)
    monkeypatch.setattr(launcher, "launch_window",
                        lambda **_k: pytest.fail("--no-open 不允许开窗"))
    monkeypatch.setattr(launcher, "resolve_log_dir", lambda explicit=None: tmp_path)
    state_path = tmp_path / "desktop_state.json"
    code = launcher.main(["--tray", "--no-open", "--no-notify",
                          "--state-path", str(state_path)])
    assert code == 0
    assert started == ["gateway", "console", "workbench"]
    assert tray_kwargs["no_notify"] is True
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["tray_pid"] == 777 and state["tray_no_notify"] is True


def test_status_report_includes_tray(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    launcher.save_state({"tray_pid": 888, "tray_no_notify": True}, state_path)
    monkeypatch.setattr(launcher, "port_open", lambda *_a, **_k: False)
    monkeypatch.setattr(launcher, "http_ok", lambda *_a, **_k: False)
    monkeypatch.setattr(launcher, "_pid_alive", lambda pid: pid == 888)
    report = launcher.status_report(launcher.load_state(state_path), state_path)
    assert report["tray"] == {"pid": 888, "alive": True, "no_notify": True}
    assert report["notify"]["log_path"].endswith("gateway.log")


def test_main_notify_once_prints_json(tmp_path, monkeypatch, capsys):
    log = tmp_path / "gateway.log"
    log.write_text(_send_sent(time.time() - 10) + "\n", encoding="utf-8")
    # 静默窗口必须"不含当前小时"，否则白天/夜间跑会得到不同结果（wall-clock flake）
    hour = time.localtime().tm_hour
    monkeypatch.setattr(launcher, "_quiet_hours_from_config",
                        lambda: ((hour + 1) % 24, (hour + 2) % 24))
    code = launcher.main(["--notify-once", "--notify-log", str(log),
                          "--notify-state", str(tmp_path / "notify.json")])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["ok"] is True
    assert len(payload["notifications"]) == 1


def test_notifications_once_first_run_skips_old_history(tmp_path, monkeypatch):
    """第一次轮询只通知近 10 分钟事件，避免托盘刚启动就弹一堆历史。"""
    monkeypatch.setattr(launcher, "_quiet_hours_from_config", lambda: (23, 8))
    log = tmp_path / "gateway.log"
    log.write_text(_send_sent(DAY) + "\n", encoding="utf-8")   # DAY 是很久以前
    result = launcher.notifications_once(log_path=log,
                                         state_path=tmp_path / "notify.json")
    assert result["lines"] == 1
    assert result["notifications"] == []
    assert (tmp_path / "notify.json").is_file()                 # offset 已推进


# --------------------------------------------------------------------------- #
# 5. 开机自启（假注册表 + 临时启动目录，绝不碰真实注册表/启动目录）
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_registry(monkeypatch):
    store: dict[str, str] = {}

    def _set(command):
        store[launcher.AUTOSTART_NAME] = str(command)

    def _get():
        return store.get(launcher.AUTOSTART_NAME)

    def _delete():
        if launcher.AUTOSTART_NAME in store:
            del store[launcher.AUTOSTART_NAME]
            return True
        return False

    monkeypatch.setattr(launcher, "_reg_set", _set)
    monkeypatch.setattr(launcher, "_reg_get", _get)
    monkeypatch.setattr(launcher, "_reg_delete", _delete)
    return store


def test_autostart_startup_bat_install_status_uninstall(tmp_path, fake_registry):
    startup = tmp_path / "Startup"
    status = launcher.autostart_status(startup_dir=startup)
    assert status["bat_installed"] is False and status["ok"] is True

    result = launcher.autostart_install(startup_dir=startup)
    assert result["ok"] is True and result["method"] == "startup"
    bat = startup / launcher.AUTOSTART_BAT
    assert bat.is_file()
    text = bat.read_text(encoding="utf-8")
    assert "launcher.py" in text and "--tray" in text and "--no-open" in text
    assert launcher.autostart_status(startup_dir=startup)["bat_installed"] is True
    assert fake_registry == {}                    # 默认不写注册表

    removed = launcher.autostart_uninstall(startup_dir=startup)
    assert removed["removed_bat"] is True
    assert not bat.exists()


def test_autostart_registry_install_status_uninstall(tmp_path, fake_registry):
    startup = tmp_path / "Startup"
    result = launcher.autostart_install(use_registry=True, startup_dir=startup)
    assert result["ok"] is True and result["method"] == "registry"
    status = launcher.autostart_status(startup_dir=startup)
    assert status["registry_installed"] is True
    assert "launcher.py" in status["registry_value"]
    assert not (startup / launcher.AUTOSTART_BAT).exists()

    removed = launcher.autostart_uninstall(startup_dir=startup)
    assert removed["removed_registry"] is True
    assert fake_registry == {}


def test_main_autostart_commands(tmp_path, monkeypatch, fake_registry, capsys):
    monkeypatch.setenv("WXBOX_STARTUP_DIR", str(tmp_path / "Startup"))
    code = launcher.main(["--autostart", "install"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["ok"] is True and payload["bat_installed"] is True

    code = launcher.main(["--autostart", "status"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["bat_installed"] is True

    code = launcher.main(["--autostart", "uninstall"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["bat_installed"] is False

def test_workbench_service_spec_and_cwd(tmp_path, monkeypatch):
    specs = launcher.service_commands()
    wb = specs["workbench"]
    assert wb["port"] == 803
    assert wb["cwd"] == str(launcher.ROOT / "idea")
    assert wb["cmd"][1:4] == ["-m", "uvicorn", "app.main:app"]
    assert "--port" in wb["cmd"] and "803" in wb["cmd"]
    # 子进程 spawn 使用 spec 里的 cwd（不落成仓库根）
    captured = {}
    monkeypatch.setattr(launcher, "_spawn_service",
                        lambda cmd, log, cwd=None: captured.update(cmd=cmd, cwd=cwd) or type("P", (), {"pid": 1})())
    monkeypatch.setattr(launcher, "port_open", lambda *_a, **_k: False)
    monkeypatch.setattr(launcher, "wait_for", lambda fn, timeout=0: True)
    monkeypatch.setattr(launcher, "http_ok", lambda *_a, **_k: True)
    launcher.ensure_service("workbench", wb, tmp_path)
    assert captured["cwd"] == str(launcher.ROOT / "idea")
