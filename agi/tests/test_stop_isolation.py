# -*- coding: utf-8 -*-
"""P0-22b：STOP 开关不能污染测试套件（生产路径必须隔离）。

事故（2026-10-25）：运维为保险在 `agi/data/wechat/STOP` 建了停止文件，结果
`test_wechat_draft_flow.py` 4 条全挂——测试里的 Gateway 读的是**生产 STOP 路径**，
被真实文件拦成 `blocked`。生产文件的存在不应该影响任何测试。

本文件锁住：
- `conftest.py` 的 autouse fixture 已把 STOP 路径指向 tmp（生产路径永不读）；
- `Gateway(..., stop_path=...)` 显式注入优先；
- `config.STOP_PATH` / `set_stop_path()` 模块级覆盖生效；
- STOP 命中返回 `blocked` / `reason="stopped"`，**不计 failed、不动熔断**；
- `/status` 的 `proactive.stopped` 正确。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agi.selflearn import ticker
from agi.wechat_gateway import config as gw_config
from agi.wechat_gateway import proactive_worker
from agi.wechat_gateway.server import Gateway


def _cfg(tmp_path, **overrides):
    cfg = {
        "enabled": True,
        "port": 801,
        "dry_run": True,
        "allowed_contacts": ["好友A"],
        "proactive_contacts": ["好友A"],
        "send_mode": "auto",
        "max_chars": 500,
        "proactive_enabled": True,
        "proactive_probability_min": 1.0,
        "proactive_probability_max": 1.0,
        "quiet_hours": [0, 0],
        "drafts_path": str(tmp_path / "drafts.jsonl"),
        "log_path": str(tmp_path / "gateway.log"),
    }
    cfg.update(overrides)
    return cfg


def _gateway(tmp_path, **overrides) -> Gateway:
    gw = Gateway(_cfg(tmp_path, **overrides))
    gw._config_hot_reload = False       # 本文件不测热加载，避免读生产 config.json
    return gw


def _events(gateway, kind):
    return [e for e in gateway.recent if e.get("kind") == kind]


# --------------------------------------------------------------------------- #
# 1. autouse fixture：默认 STOP 路径落到 tmp，生产路径不可达
# --------------------------------------------------------------------------- #
def test_autouse_fixture_points_stop_path_to_tmp(tmp_path):
    production = gw_config.default_stop_path()          # agi/data/wechat/STOP
    assert gw_config.stop_path() == tmp_path / "STOP"
    assert gw_config.stop_path() != production
    assert gw_config.stopped() is False                 # tmp 下没有 STOP

    # 默认 STOP 路径锚定在 CONFIG_PATH 同目录，且落在生产数据目录下
    assert production == Path(gw_config.CONFIG_PATH).parent / "STOP"
    production.resolve().relative_to(Path(gw_config.DATA_DIR).resolve())


def test_gateway_under_isolation_is_not_blocked(tmp_path):
    gw = _gateway(tmp_path)
    out = gw._send("好友A", "在吗")
    assert out["status"] != "blocked" and out.get("error") != "stop_file"
    assert out["status"] == "planned"                   # dry_run 正常走到发送环节


# --------------------------------------------------------------------------- #
# 2. 模拟"生产 STOP 存在"：路径被重定向，测试仍全绿
# --------------------------------------------------------------------------- #
def test_production_stop_file_is_never_read(tmp_path, monkeypatch):
    fake_prod = tmp_path / "prod_data"
    fake_prod.mkdir()
    (fake_prod / "STOP").write_text("stopped\n", encoding="utf-8")
    monkeypatch.setattr(gw_config, "DATA_DIR", fake_prod)
    monkeypatch.setattr(gw_config, "CONFIG_PATH", fake_prod / "config.json")

    # 默认路径解析到 fake 生产目录 → 被 fixture 重定向到 tmp
    assert gw_config.default_stop_path() == fake_prod / "STOP"
    assert gw_config.stop_path() == tmp_path / "STOP"
    assert gw_config.stopped() is False

    gw = _gateway(tmp_path)
    assert gw.stop_present() is False
    assert gw._send("好友A", "在吗")["status"] == "planned"


def test_config_managed_by_test_keeps_stop_next_to_it(tmp_path, monkeypatch):
    """测试自己重定向 CONFIG_PATH 时，STOP 仍与 config.json 同目录（语义不变）。"""
    cfg_path = tmp_path / "wechat" / "config.json"
    cfg_path.parent.mkdir(parents=True)
    monkeypatch.setattr(gw_config, "CONFIG_PATH", cfg_path)

    assert gw_config.stop_path() == cfg_path.parent / "STOP"
    (cfg_path.parent / "STOP").write_text("stopped\n", encoding="utf-8")
    gw = _gateway(tmp_path)
    assert gw.stop_present() is True
    assert gw._send("好友A", "在吗")["status"] == "blocked"


# --------------------------------------------------------------------------- #
# 3. Gateway 显式 stop_path 注入优先于任何默认/模块覆盖
# --------------------------------------------------------------------------- #
def test_explicit_gateway_stop_path_wins(tmp_path, monkeypatch):
    fake_prod = tmp_path / "prod_data"
    fake_prod.mkdir()
    (fake_prod / "STOP").write_text("stopped\n", encoding="utf-8")
    monkeypatch.setattr(gw_config, "DATA_DIR", fake_prod)
    monkeypatch.setattr(gw_config, "CONFIG_PATH", fake_prod / "config.json")

    explicit = tmp_path / "explicit" / "STOP"
    gw = Gateway(_cfg(tmp_path), stop_path=explicit)
    gw._config_hot_reload = False

    # 生产 STOP 存在，但显式路径没有 → 不拦
    assert gw._send("好友A", "在吗")["status"] == "planned"
    # 显式路径出现 → 只认它
    explicit.parent.mkdir(parents=True, exist_ok=True)
    explicit.write_text("stopped\n", encoding="utf-8")
    out = gw._send("好友A", "在吗")
    assert out == {"ok": False, "status": "blocked", "error": "stop_file"}
    assert gw.status()["proactive"]["stopped"] is True
    # 删掉显式文件 → 恢复（尽管生产 STOP 还在）
    explicit.unlink()
    assert gw.status()["proactive"]["stopped"] is False
    assert gw._send("好友A", "在吗")["status"] == "planned"


def test_set_stop_uses_injected_path(tmp_path):
    injected = tmp_path / "injected" / "STOP"
    gw = Gateway(_cfg(tmp_path), stop_path=injected)
    gw._config_hot_reload = False

    assert gw.set_stop(True) == {"ok": True, "stopped": True}
    assert injected.exists()                                 # 落在注入路径
    assert not (tmp_path / "STOP").exists()                  # 没落默认/隔离路径
    assert gw.status()["proactive"]["stopped"] is True
    toggled = _events(gw, "proactive_stop_toggled")
    assert toggled and toggled[-1]["path"] == str(injected)

    assert gw.set_stop(False) == {"ok": True, "stopped": False}
    assert not injected.exists()
    assert gw.status()["proactive"]["stopped"] is False


# --------------------------------------------------------------------------- #
# 4. 模块级覆盖：config.STOP_PATH / set_stop_path()
# --------------------------------------------------------------------------- #
def test_module_level_stop_path_override(tmp_path):
    custom = tmp_path / "custom" / "STOP"
    gw_config.set_stop_path(custom)
    try:
        assert gw_config.stop_path() == custom
        assert gw_config.stopped() is False
        custom.parent.mkdir(parents=True, exist_ok=True)
        custom.write_text("stopped\n", encoding="utf-8")
        assert gw_config.stopped() is True

        gw = _gateway(tmp_path)                              # 未显式传 stop_path
        assert gw.stop_present() is True
        assert gw._send("好友A", "在吗")["error"] == "stop_file"
    finally:
        gw_config.set_stop_path(None)
    # 清除覆盖 → 回到隔离后的默认（tmp/STOP，不存在）
    assert gw_config.stop_path() == tmp_path / "STOP"
    assert gw_config.stopped() is False


# --------------------------------------------------------------------------- #
# 5. blocked 口径：不计 failed、不动熔断；run_cycle 返回 stopped
# --------------------------------------------------------------------------- #
def test_blocked_by_stop_is_not_failure_and_does_not_open_breaker(tmp_path, monkeypatch):
    stop = tmp_path / "STOP"
    stop.write_text("stopped\n", encoding="utf-8")
    gw = _gateway(tmp_path)

    # 直接发送被拦：状态是 blocked，不是 failed
    blocked = gw._send("好友A", "在吗")
    assert blocked["status"] == "blocked" and blocked["error"] == "stop_file"
    assert proactive_worker.is_send_failure(blocked) is False   # 不参与熔断计数
    assert gw.failed == 0                                       # 网关 failed 计数不增

    # run_cycle 最优先判 STOP：不调 tick、不发送、不计 sent/drafts
    monkeypatch.setattr(ticker, "run_once",
                        lambda *a, **k: pytest.fail("STOP 期间不应调用 ticker"))
    for i in range(3):
        out = proactive_worker.run_cycle(gw, client=object(), now=100.0 + i)
        assert out["ran"] is False and out["reason"] == "stopped"
        assert out["stopped"] is True
        assert out["sent"] == 0 and out["drafts"] == 0 and out["clipboard"] == 0

    snapshot = proactive_worker.breaker_snapshot(gw, now=200.0)
    assert snapshot["open"] is False and snapshot["consecutive"] == 0
    assert _events(gw, "proactive_breaker_open") == []
    assert _events(gw, "send_blocked")[-1]["reason"] == "stop_file"
    assert gw.status()["proactive"]["stopped"] is True

    # 删除 STOP → 恢复发送能力
    stop.unlink()
    monkeypatch.setattr(ticker, "run_once",
                        lambda *a, **k: {"ok": True, "ran": False, "reason": "gate"})
    out = proactive_worker.run_cycle(gw, client=object(), now=300.0)
    assert out["ran"] is True and out.get("reason") != "stopped"


def test_run_cycle_uses_gateway_stop_present_not_hardcoded_path(tmp_path, monkeypatch):
    """proactive_worker 必须走 Gateway 的注入路径，而不是硬编码生产路径。"""

    class FakeGateway:
        def __init__(self, stopped: bool):
            self.cfg = {"proactive_enabled": True, "proactive_contacts": ["好友A"],
                        "proactive_probability_min": 1.0,
                        "proactive_probability_max": 1.0}
            self.records = []
            self._stopped = stopped

        def stop_present(self):
            return self._stopped

        def _record(self, kind, data):
            self.records.append((kind, data))

    opened = FakeGateway(True)
    out = proactive_worker.run_cycle(opened, client=object(), now=100.0)
    assert out["reason"] == "stopped"
    assert any(k == "proactive_stopped" for k, _ in opened.records)

    running = FakeGateway(False)
    assert running.stop_present() is False                 # 注入点说了算


# --------------------------------------------------------------------------- #
# 6. 事故复盘：三个曾受影响的测试文件在当前隔离下不依赖生产 STOP
# --------------------------------------------------------------------------- #
def test_incident_repro_gateway_default_is_isolated(tmp_path):
    """等价于事故现场：生产 STOP 存在时，测试里默认 Gateway 不会被拦。"""
    # 本机生产 STOP 是否存在都不影响断言：路径已隔离到 tmp
    assert gw_config.stop_path() == tmp_path / "STOP"
    gw = _gateway(tmp_path)
    assert gw.stop_present() is False
    assert gw._send("好友A", "在吗")["status"] == "planned"
    assert not (tmp_path / "STOP").exists()
