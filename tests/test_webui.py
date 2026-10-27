"""网页端离线测试（qa-docs 维护）。

覆盖 `webui.bridge` / `webui.llm` / `webui.server` 的离线行为：
* 一律用 `ui.dev_backend.DevBackend` 产生真实事件流，不连接微信、不依赖 Tk、不联网；
* HTTP 服务一律绑定 `127.0.0.1:0`（随机空闲端口），不占用 8080；
* DeepSeek 用本机 `http.server` 假接口替身，绝不访问 api.deepseek.com；
* 配置全部写 `tmp_path`，不碰项目根 `web_config.json`。

运行：``C:\\Python314\\python.exe -m pytest tests/test_webui.py -q``
"""
from __future__ import annotations

import http.client
import json
import queue
import socket
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------
# 通用小工具
# ----------------------------------------------------------------------
def _wait_until(predicate, timeout: float = 8.0, interval: float = 0.02) -> bool:
    """轮询等待 predicate 返回真；超时返回 False（不抛异常，便于写清晰断言）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001 - 轮询期间的瞬时异常不视为失败
            pass
        time.sleep(interval)
    return False


# ----------------------------------------------------------------------
# 公共 fixture：假后端 / 事件桥
# ----------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _clean_deepseek_env(monkeypatch):
    """隔离宿主机环境变量：默认没有 DEEPSEEK_API_KEY（需要时测试内自行 set）。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


@pytest.fixture()
def backend():
    """每 0.15 秒产出一条示例消息的假后端（测试结束停止线程）。"""
    from ui.dev_backend import DevBackend

    item = DevBackend({}, interval=0.15)
    item.start()
    yield item
    item.stop()


@pytest.fixture()
def bridge(backend):
    """消费假后端事件的事件桥（测试结束停止消费线程，不 stop backend）。"""
    from webui.bridge import WebBridge

    item = WebBridge(backend, mode="fake")
    item.start()
    yield item
    item.stop()


def _snapshot_with_messages(bridge, minimum: int = 4, timeout: float = 8.0) -> dict:
    """等待 bridge 至少收到 minimum 条消息后返回 snapshot（保证断言数据充足）。"""
    assert _wait_until(
        lambda: len(bridge.snapshot()["messages"]) >= minimum, timeout
    ), f"等待假后端消息超时（当前 {len(bridge.snapshot()['messages'])} 条）"
    return bridge.snapshot()


# ----------------------------------------------------------------------
# WebBridge：快照 / 排序 / 去重
# ----------------------------------------------------------------------
def test_bridge_snapshot_has_all_sections(bridge):
    """snapshot() 含 state/friends/messages/logs，且字段形状符合契约 4.2。"""
    snapshot = _snapshot_with_messages(bridge)
    assert {"state", "friends", "messages", "logs"} <= set(snapshot)

    state = snapshot["state"]
    assert state["running"] is True
    assert state["wechat_found"] is True
    assert isinstance(state["monitoring"], list)
    assert state["message_total"] >= len(snapshot["messages"])
    assert isinstance(state["per_friend_counts"], dict)

    friends = snapshot["friends"]
    assert friends, "假后端应推送好友列表"
    for friend in friends:
        assert {"name", "wxid", "letter", "count", "last_ts"} <= set(friend)
    assert [f["name"] for f in friends] == sorted(f["name"] for f in friends), "好友按名字升序"

    message = snapshot["messages"][0]
    assert {"id", "friend", "sender", "kind", "text", "ts", "kind_label", "time_text"} <= set(message)

    assert snapshot["logs"], "假后端应推送日志"
    assert {"ts", "level", "text"} <= set(snapshot["logs"][0])


def test_bridge_messages_sorted_and_unique(bridge):
    """消息按时间升序、id 不重复（去重生效）。"""
    snapshot = _snapshot_with_messages(bridge, minimum=6)
    messages = snapshot["messages"]
    assert messages == sorted(messages, key=lambda m: m["ts"]), "messages 必须按 ts 升序"
    ids = [m["id"] for m in messages]
    assert len(ids) == len(set(ids)), f"消息 id 重复：{ids}"


def test_bridge_duplicate_message_broadcast_once(bridge):
    """同一条消息事件重复进入 backend.events 时，快照与广播都只保留一次。"""
    from wx_core.models import KIND_TEXT, SENDER_FRIEND, Message, event_message

    message = Message(friend="去重测试好友", kind=KIND_TEXT, sender=SENDER_FRIEND,
                      text="重复消息去重", ts=time.time())
    sub, sub_id = bridge.subscribe()
    try:
        event = event_message(message)
        bridge.backend.events.put(event)
        bridge.backend.events.put(event)
        assert _wait_until(
            lambda: any(m["id"] == message.id for m in bridge.snapshot()["messages"])
        ), "重复消息没有进入快照"
        time.sleep(0.4)  # 给消费线程处理第二条重复事件的时间

        matched = [m for m in bridge.snapshot()["messages"] if m["id"] == message.id]
        assert len(matched) == 1, f"重复 id 的消息应只保留一条，实际 {len(matched)}"

        received = []
        while True:
            try:
                received.append(sub.get_nowait())
            except queue.Empty:
                break
        duplicate_events = [
            e for e in received
            if e.get("type") == "message" and e.get("data", {}).get("id") == message.id
        ]
        assert len(duplicate_events) == 1, "重复消息不应向订阅者广播两次"
    finally:
        bridge.unsubscribe(sub_id)


def test_bridge_normalizes_redpacket_transfer_and_db_ids(backend):
    """V5：红包/转账 XML 清洗成人话气泡；带 db_local_id 的消息 id 统一为 db_<sha1>。"""
    from webui.bridge import WebBridge
    from webui.wechat_text import db_message_id
    from wx_core.models import (
        KIND_REDPACKET, KIND_TEXT, SENDER_FRIEND, Message, event_message,
    )

    bridge = WebBridge(backend, mode="fake")
    bridge.start()
    try:
        now = time.time()
        redpacket = Message(
            friend="测试群", kind=KIND_TEXT, sender=SENDER_FRIEND,
            text='<msg><appmsg><type>2001</type><title>恭喜发财，大吉大利</title></appmsg></msg>',
            extra={"db_local_id": 501}, ts=now,
        )
        transfer = Message(
            friend="测试群", kind=KIND_TEXT, sender=SENDER_FRIEND,
            text='<msg><appmsg><type>2000</type><title>转账给你</title></appmsg></msg>',
            extra={"db_local_id": 502}, ts=now + 0.001,
        )
        plain_red = Message(
            friend="测试群", kind=KIND_REDPACKET, sender=SENDER_FRIEND,
            text="🧧 测试红包", extra={"db_local_id": 503}, ts=now + 0.002,
        )
        for message in (redpacket, transfer, plain_red):
            backend.events.put(event_message(message))

        expected_ids = {
            db_message_id("测试群", 501),
            db_message_id("测试群", 502),
            db_message_id("测试群", 503),
        }
        assert _wait_until(
            lambda: expected_ids <= {
                message["id"] for message in bridge.snapshot()["messages"]
            },
            timeout=8,
        ), "红包/转账消息没有进入快照"
        stored = {
            message["id"]: message for message in bridge.snapshot()["messages"]
            if message["id"] in expected_ids
        }
        assert all(str(message_id).startswith("db_") for message_id in stored)

        red = stored[db_message_id("测试群", 501)]
        assert red["kind"] == "system"
        assert "恭喜发财" in red["text"]
        assert "红包" in red["text"] or "🧧" in red["text"], "应显示红包气泡文案"
        assert "<" not in red["text"], "红包 XML 标签应被清洗"
        assert red["extra"]["app_type"] == "hongbao"

        transfer_msg = stored[db_message_id("测试群", 502)]
        assert transfer_msg["kind"] == "system"
        assert "转账" in transfer_msg["text"] and "转账给你" in transfer_msg["text"]
        assert "<" not in transfer_msg["text"]
        assert transfer_msg["extra"]["app_type"] == "transfer"

        plain = stored[db_message_id("测试群", 503)]
        assert plain["kind"] == "redpacket", "核心已识别的红包消息类型应保留"
        assert plain["text"] == "🧧 测试红包"
    finally:
        bridge.stop()


def test_bridge_log_ring_buffer_capped(bridge):
    """log 事件只保留最近若干条（契约上限 600），不会无限增长。"""
    from wx_core.models import event_log

    for index in range(700):
        bridge.backend.events.put(event_log(f"压测日志 {index}", "info"))

    assert _wait_until(
        lambda: any(log["text"] == "压测日志 699" for log in bridge.snapshot()["logs"]),
        timeout=10,
    ), "最新日志没有进入快照"
    logs = bridge.snapshot()["logs"]
    texts = [log["text"] for log in logs]
    assert len(logs) == 600, f"日志上限应为 600，实际 {len(logs)}"
    assert "压测日志 699" in texts, "应保留最新日志"
    assert "压测日志 0" not in texts, "最旧日志应被环形缓冲淘汰"


# ----------------------------------------------------------------------
# WebBridge：订阅 / 广播信封
# ----------------------------------------------------------------------
def test_bridge_subscriber_receives_events(bridge):
    """subscribe() 的队列能收到 bridge 广播的事件，且带 ts 信封字段。"""
    assert _wait_until(lambda: len(bridge.snapshot()["messages"]) >= 3)
    sub, sub_id = bridge.subscribe()
    try:
        event = None
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                candidate = sub.get(timeout=0.2)
            except queue.Empty:
                continue
            if candidate.get("type") in ("status", "message", "friends", "log"):
                event = candidate
                break
        assert event is not None, "订阅者 5 秒内没有收到任何事件"
        assert event["type"] in ("status", "message", "friends", "log")
        assert isinstance(event.get("ts"), float), "事件必须补 ts 信封字段"
        assert "data" in event
    finally:
        bridge.unsubscribe(sub_id)


def test_bridge_broadcast_adds_ts_and_keeps_given(bridge):
    """broadcast() 给缺失 ts 的事件补信封字段；已有 ts 则保留原值。"""
    sub, sub_id = bridge.subscribe()
    try:
        bridge.broadcast({"type": "status", "data": {"tick": 1}})
        bridge.broadcast({"type": "status", "data": {"tick": 2}, "ts": 123.5})
        first = sub.get(timeout=2)
        second = sub.get(timeout=2)
        assert isinstance(first["ts"], float) and first["ts"] > 0
        assert second["ts"] == 123.5
    finally:
        bridge.unsubscribe(sub_id)


def test_bridge_start_is_idempotent(bridge):
    """重复 start() 不会产生第二个消费线程（消息总量不翻倍）。"""
    snapshot = _snapshot_with_messages(bridge, minimum=4)
    total_before = snapshot["state"]["message_total"]
    for _ in range(3):
        bridge.start()
    time.sleep(0.35)
    total_after = bridge.snapshot()["state"]["message_total"]
    # 0.35 秒最多再产生 2~3 条新消息；若重复启动线程，会明显多于此
    assert total_after - total_before <= 5, (
        f"重复 start() 后消息增量异常（{total_before} -> {total_after}），疑似启动了多次消费线程"
    )


def test_bridge_stop_does_not_stop_backend(backend):
    """bridge.stop() 只停消费线程，backend 应仍在运行。"""
    from webui.bridge import WebBridge

    item = WebBridge(backend, mode="fake")
    item.start()
    assert _wait_until(lambda: item.snapshot()["state"]["message_total"] >= 1)
    item.stop()
    assert backend.is_running() is True, "bridge.stop() 不应停止 backend"


def test_bridge_stop_notifies_subscribers_bye(backend):
    """bridge.stop() 会给订阅者推一条 bye 事件，SSE 线程据此退出。"""
    from webui.bridge import WebBridge

    item = WebBridge(backend, mode="fake")
    item.start()
    sub, sub_id = item.subscribe()
    try:
        item.stop()
        events = []
        while True:
            try:
                events.append(sub.get_nowait())
            except queue.Empty:
                break
        assert any(
            event.get("type") == "bye"
            and event.get("data", {}).get("reason") == "server_stopped"
            for event in events
        ), f"没有收到 bye 事件：{[e.get('type') for e in events]}"
        assert item.is_running() is False
    finally:
        item.unsubscribe(sub_id)


def test_bridge_slow_subscriber_gets_resync(bridge):
    """订阅队列溢出时给慢客户端补发 resync，而不是阻塞广播。"""
    sub, sub_id = bridge.subscribe()
    try:
        for index in range(700):
            bridge.broadcast({"type": "status", "data": {"tick": index}})
        drained = []
        while True:
            try:
                drained.append(sub.get_nowait())
            except queue.Empty:
                break
        assert any(event.get("type") == "resync" for event in drained), (
            "队列溢出后应补发 resync 事件"
        )
        assert len(drained) <= 520, "溢出时应丢弃积压，队列不应无限增长"
    finally:
        bridge.unsubscribe(sub_id)


# ----------------------------------------------------------------------
# WebBridge：监听名单与 scope_messages
# ----------------------------------------------------------------------
def test_bridge_set_monitored_updates_state(bridge):
    """set_monitored() 更新监听名单并返回契约形状。"""
    result = bridge.set_monitored(["白云一片去悠悠", "张三"])
    assert result == {"ok": True, "monitoring": ["白云一片去悠悠", "张三"]}
    assert _wait_until(
        lambda: bridge.snapshot()["state"]["monitoring"] == ["白云一片去悠悠", "张三"]
    ), "state.monitoring 未同步"

    bridge.set_monitored([])
    assert result["ok"] is True
    assert _wait_until(lambda: bridge.snapshot()["state"]["monitoring"] == [])


def test_scope_messages_all_and_limit(bridge):
    """scope=all 返回最近 limit 条，按时间升序且恰好是快照的最后 limit 条。"""
    snapshot = _snapshot_with_messages(bridge, minimum=6)
    messages = snapshot["messages"]

    recent = bridge.scope_messages({"type": "all"}, 3)
    assert len(recent) == 3
    assert recent == sorted(recent, key=lambda m: m["ts"])
    assert [m["id"] for m in recent] == [m["id"] for m in messages[-3:]]

    everything = bridge.scope_messages({"type": "all"}, 300)
    assert [m["id"] for m in everything] == [m["id"] for m in messages[-300:]]


def test_scope_messages_friend_filter(bridge):
    """scope=friend 只返回该好友的消息，且按时间升序。"""
    _snapshot_with_messages(bridge, minimum=8)
    bridge.set_monitored(["张三"])  # 让假后端后续只推"张三"，保证样本充足
    assert _wait_until(
        lambda: any(m["friend"] == "张三" for m in bridge.snapshot()["messages"])
    )
    deadline = time.time() + 5
    while time.time() < deadline:
        candidates = [m for m in bridge.snapshot()["messages"] if m["friend"] == "张三"]
        if len(candidates) >= 2:
            break
        time.sleep(0.1)

    friend_messages = bridge.scope_messages({"type": "friend", "friend": "张三"}, 100)
    assert friend_messages, "张三应至少有一条消息"
    assert all(m["friend"] == "张三" for m in friend_messages)
    assert friend_messages == sorted(friend_messages, key=lambda m: m["ts"])

    empty = bridge.scope_messages({"type": "friend", "friend": "不存在的朋友"}, 100)
    assert empty == []


def test_scope_messages_monitored_filter(bridge):
    """scope=monitored 只返回监听好友的消息；friend_names 可再限定。"""
    bridge.set_monitored(["白云一片去悠悠", "张三"])
    assert _wait_until(
        lambda: {m["friend"] for m in bridge.snapshot()["messages"]} & {"白云一片去悠悠", "张三"}
    )
    time.sleep(0.6)  # 让假后端按监听名单再产几条

    monitored = bridge.scope_messages({"type": "monitored"}, 100)
    assert monitored, "监听好友应有消息"
    assert all(m["friend"] in {"白云一片去悠悠", "张三"} for m in monitored)
    assert monitored == sorted(monitored, key=lambda m: m["ts"])

    narrowed = bridge.scope_messages(
        {"type": "monitored", "friend_names": ["张三"]}, 100
    )
    assert all(m["friend"] == "张三" for m in narrowed)


# ----------------------------------------------------------------------
# LLMConfigStore：默认值 / 落盘 / 脱敏 / 环境变量兜底
# ----------------------------------------------------------------------
def _new_config_store(tmp_path):
    from webui.llm import LLMConfigStore

    return LLMConfigStore(tmp_path / "web_config.json")


def test_llm_config_store_defaults(tmp_path):
    """默认配置：模型只有 DeepSeek V4.1 Flash 1017、未配置 key、只读不落盘。"""
    from webui.llm import MODEL_CATALOG

    store = _new_config_store(tmp_path)
    assert store.load() == {
        "api_key": "",
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com",
        "thinking": True,
        "attach_images": True,
    }
    public = store.public()
    assert public["configured"] is False
    assert public["api_key_masked"] == ""
    assert public["model"] == "deepseek-flash"
    assert public["model_label"] == "DeepSeek V4.1 Flash 1017"
    assert [item["id"] for item in public["models"]] == ["deepseek-flash"]
    assert MODEL_CATALOG[0]["label"] == "DeepSeek V4.1 Flash 1017"
    assert public["busy"] is False
    assert not store.path.exists(), "只读配置不应创建文件"


def test_llm_config_store_save_writes_and_masks(tmp_path):
    """保存后写盘；public() 只给脱敏 key（不含完整明文）。"""
    key = "sk-" + "a" * 24 + "abcd"
    store = _new_config_store(tmp_path)
    public = store.save({"api_key": key, "thinking": False, "attach_images": False})

    assert store.path.is_file(), "保存后应写入配置文件"
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert raw["llm"]["api_key"] == key
    assert raw["llm"]["model"] == "deepseek-flash"
    assert raw["llm"]["thinking"] is False
    assert raw["llm"]["attach_images"] is False

    assert public["configured"] is True
    assert public["api_key_masked"] == "sk-…abcd"
    assert key not in json.dumps(public, ensure_ascii=False), "public() 泄露了完整 key"
    assert store.load()["api_key"] == key


def test_llm_config_store_empty_key_keeps_saved(tmp_path):
    """api_key 为空/空白时保留已保存的 key（允许只改其它项）。"""
    store = _new_config_store(tmp_path)
    store.save({"api_key": "sk-keep-1234567890xyz"})
    store.save({"api_key": "   ", "thinking": False})
    assert store.load()["api_key"] == "sk-keep-1234567890xyz"
    assert store.load()["thinking"] is False
    assert store.public()["api_key_masked"] == "sk-…0xyz"


def test_llm_config_store_clear_key(tmp_path):
    """api_key="clear" 清空已保存的 key。"""
    store = _new_config_store(tmp_path)
    store.save({"api_key": "sk-clear-me-123456789"})
    public = store.save({"api_key": "clear"})
    assert public["configured"] is False
    assert public["api_key_masked"] == ""
    assert store.load()["api_key"] == ""


def test_llm_config_store_rejects_invalid_values(tmp_path):
    """非法模型 / 过短 key / 非 http(s) base_url 一律拒绝，且不写盘。"""
    store = _new_config_store(tmp_path)
    with pytest.raises(ValueError):
        store.save({"model": "gpt-4o"})
    with pytest.raises(ValueError):
        store.save({"api_key": "short"})
    with pytest.raises(ValueError):
        store.save({"base_url": "ftp://example.com"})
    assert store.public()["model"] == "deepseek-flash"
    assert not store.path.exists(), "非法配置不应写盘"


def test_llm_config_store_env_fallback(tmp_path, monkeypatch):
    """没有保存 key 时用 DEEPSEEK_API_KEY 兜底，且环境变量不写盘、不回显明文。"""
    store = _new_config_store(tmp_path)
    store.save({"thinking": True})  # 落盘一个空 key
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env-key-9876wxyz")

    assert store.load()["api_key"] == "sk-env-key-9876wxyz"
    public = store.public()
    assert public["configured"] is True
    assert public["api_key_masked"] == "sk-…wxyz"
    assert "sk-env-key-9876wxyz" not in json.dumps(public, ensure_ascii=False)
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert raw["llm"]["api_key"] == "", "环境变量里的 key 不应被写进配置文件"


def test_llm_config_store_tolerates_corrupt_file(tmp_path):
    """配置文件损坏时回退默认值，不抛异常。"""
    store = _new_config_store(tmp_path)
    store.path.write_text("{这不是合法 JSON", encoding="utf-8")
    config = store.load()
    assert config["api_key"] == "" and config["model"] == "deepseek-flash"


# ----------------------------------------------------------------------
# 假 DeepSeek 接口（本机 http.server，绝不联网）
# ----------------------------------------------------------------------
def _quiet_log(*_args, **_kwargs):
    """静默日志函数（避免测试输出被后台线程日志刷屏）。"""


class _FakeDeepSeekServer:
    """本机假 DeepSeek：``GET /models`` + ``POST /chat/completions``（SSE 流）。

    支持三种模式：``ok``（正常流）、``unauthorized``（401）、``slow``（慢速流，供取消测试）。
    """

    def __init__(self, mode: str = "ok"):
        import http.server

        self.mode = mode
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self._delay = 0.3 if mode == "slow" else 0.0
        owner = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args):  # noqa: D401 - 标准库钩子
                pass

            def _send_json(self, status: int, payload: dict) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802 - 标准库约定
                if owner.mode == "unauthorized":
                    self._send_json(401, {"error": {"message": "Authentication Fails"}})
                    return
                self._send_json(
                    200,
                    {"object": "list", "data": [{"id": "deepseek-flash", "object": "model"}]},
                )

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    body = {}
                if not isinstance(body, dict):
                    body = {}
                with owner._lock:
                    owner.requests.append(
                        {
                            "path": self.path,
                            "authorization": self.headers.get("Authorization") or "",
                            "body": body,
                        }
                    )
                if owner.mode == "unauthorized":
                    self._send_json(401, {"error": {"message": "Authentication Fails"}})
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    for chunk in owner.stream_chunks:
                        self.wfile.write(
                            f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode("utf-8")
                        )
                        self.wfile.flush()
                        if owner._delay:
                            time.sleep(owner._delay)
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    # 取消/关闭客户端时 Windows 可能报 WinError 10053（ConnectionAbortedError），
                    # 假服务直接忽略即可，避免把 traceback 打到 pytest 输出里。
                    pass
                self.close_connection = True

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self.host, self.port = self._server.server_address[:2]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.02},
            name="wxbox-fake-deepseek",
            daemon=True,
        )
        self._thread.start()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def stream_chunks(self) -> list[dict]:
        if self.mode == "slow":
            return [
                {"choices": [{"delta": {"reasoning_content": "慢速思考"}}]},
                {"choices": [{"delta": {"content": "第一段"}}]},
                {"choices": [{"delta": {"content": "第二段"}}]},
                {"choices": [{"delta": {"content": "第三段"}}]},
            ]
        return [
            {"choices": [{"delta": {"reasoning_content": "先想一下"}}]},
            {"choices": [{"delta": {"reasoning_content": "，再回答。"}}]},
            {"choices": [{"delta": {"content": "你好，"}}]},
            {"choices": [{"delta": {"content": "这是分析结果。"}, "finish_reason": "stop"}]},
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                    "reasoning_tokens": 5,
                },
            },
        ]

    def close(self) -> None:
        try:
            self._server.shutdown()
        finally:
            self._server.server_close()
        self._thread.join(timeout=2)


@pytest.fixture()
def fake_deepseek():
    server = _FakeDeepSeekServer("ok")
    yield server
    server.close()


class _RecordingBridge:
    """只实现 LLMService 用到的 ``broadcast`` / ``scope_messages`` 的假事件桥。"""

    def __init__(self, messages: list[dict] | None = None):
        self.messages = list(messages or [])
        self.events: list[dict] = []
        self._lock = threading.Lock()

    def broadcast(self, event: dict) -> None:
        with self._lock:
            self.events.append(event)

    def scope_messages(self, scope: dict, limit: int = 80) -> list[dict]:
        scope = dict(scope or {})
        if scope.get("type") == "friend":
            picked = [m for m in self.messages if m.get("friend") == scope.get("friend")]
        else:
            picked = list(self.messages)
        return picked[-int(limit):]

    def types(self) -> list[str]:
        with self._lock:
            return [str(event.get("type")) for event in self.events]

    def wait_event(self, event_type: str, timeout: float = 10.0) -> bool:
        return _wait_until(lambda: event_type in self.types(), timeout)

    def last_event(self, event_type: str) -> dict | None:
        with self._lock:
            for event in reversed(self.events):
                if event.get("type") == event_type:
                    return event
        return None


def _sample_messages() -> list[dict]:
    now = time.time()
    return [
        {
            "id": "m1", "friend": "白云一片去悠悠", "sender": "friend",
            "sender_label": "对方", "kind": "text", "kind_label": "文字",
            "text": "今天的需求文档放群里了", "ts": now - 30, "extra": {},
        },
        {
            "id": "m2", "friend": "白云一片去悠悠", "sender": "self",
            "sender_label": "我", "kind": "text", "kind_label": "文字",
            "text": "收到，我下午看", "ts": now - 20, "extra": {},
        },
        {
            "id": "m3", "friend": "张三", "sender": "friend",
            "sender_label": "对方", "kind": "image", "kind_label": "图片",
            "text": "", "ts": now - 10, "extra": {"size_text": "216.0 KB"},
        },
    ]


def _make_llm_service(tmp_path, fake_server, *, bridge=None, moments=None):
    """构造指向假 DeepSeek 的 LLMService（配置写 tmp_path，不碰项目 web_config.json）。"""
    from webui.llm import LLMConfigStore, LLMService

    store = LLMConfigStore(tmp_path / "web_config.json")
    store.save(
        {
            "api_key": "sk-test-1234567890abcd",
            "base_url": fake_server.base_url if fake_server else "http://127.0.0.1:9",
            "thinking": True,
            "attach_images": False,
        }
    )
    recorder = bridge if bridge is not None else _RecordingBridge(_sample_messages())
    service = LLMService(store, recorder, moments=moments, logger=_quiet_log)
    return service, store, recorder


# ----------------------------------------------------------------------
# LLMService：流式解析 / usage / 错误 / 取消
# ----------------------------------------------------------------------
def test_llm_service_streaming_success(tmp_path, fake_deepseek):
    """流式片段解析：llm_start/reasoning/delta/usage/done，content 拼接与 usage 正确。"""
    service, _store, bridge = _make_llm_service(tmp_path, fake_deepseek)
    result = service.start_analysis(
        scope={"type": "all"},
        scope_label="全部消息",
        question="请总结一下",
        history=[{"role": "user", "content": "上一轮问题"}],
        options={"thinking": True, "attach_images": False, "limit": 20},
    )
    assert result["ok"] is True and result["job_id"]
    assert service.is_busy() is True

    assert bridge.wait_event("llm_done"), f"没有收到 llm_done：{bridge.types()}"
    types = bridge.types()
    assert types[0] == "llm_start"
    assert "llm_reasoning" in types and "llm_delta" in types and "llm_usage" in types

    start = bridge.last_event("llm_start")["data"]
    assert start["model"] == "deepseek-flash"
    assert start["model_label"] == "DeepSeek V4.1 Flash 1017"
    assert start["scope_label"] == "全部消息"
    assert start["message_count"] == 3
    assert start["thinking"] is True

    reasoning = "".join(
        event["data"]["delta"] for event in bridge.events if event["type"] == "llm_reasoning"
    )
    content = "".join(
        event["data"]["delta"] for event in bridge.events if event["type"] == "llm_delta"
    )
    done = bridge.last_event("llm_done")["data"]
    assert content == "你好，这是分析结果。"
    assert done["content"] == content
    assert done["reasoning"] == "先想一下，再回答。"
    assert done["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
        "reasoning_tokens": 5,
    }
    assert isinstance(done["elapsed_ms"], int) and done["elapsed_ms"] >= 0
    assert done["cancelled"] is False
    assert done["finish_reason"] == "stop"
    assert done["job_id"] == result["job_id"]
    assert _wait_until(lambda: not service.is_busy())

    # 请求体：模型 / 流式 / 思考开关 / usage / system prompt / 转写稿
    assert len(fake_deepseek.requests) == 1
    request = fake_deepseek.requests[0]
    assert request["path"] == "/chat/completions"
    assert "Bearer sk-test-1234567890abcd" == request["authorization"]
    body = request["body"]
    assert body["model"] == "deepseek-flash"
    assert body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["thinking"] == {"type": "enabled"}
    assert body["max_tokens"] == 16384
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1] == {"role": "user", "content": "上一轮问题"}
    user_text = body["messages"][-1]["content"]
    assert isinstance(user_text, str)
    for fragment in ("## 我的问题", "请总结一下", "今天的需求文档放群里了", "收到，我下午看"):
        assert fragment in user_text, f"转写稿缺少 {fragment}"


def test_llm_service_attach_images_data_url(tmp_path, fake_deepseek):
    """attach_images=True 时把图片作为 image_url 内容块附加（base64 data URL）。"""
    demo = ROOT / "media" / "demo_image.png"
    if not demo.is_file():
        pytest.skip("缺少演示图片 media/demo_image.png")

    messages = [
        {
            "id": "img-1", "friend": "张三", "sender": "friend",
            "sender_label": "对方", "kind": "image", "kind_label": "图片",
            "text": "", "media_path": "media/demo_image.png", "ts": time.time(),
            "extra": {"size_text": "216.0 KB"},
        }
    ]
    bridge = _RecordingBridge(messages)
    service, _store, _bridge = _make_llm_service(
        tmp_path, fake_deepseek, bridge=bridge, moments=None
    )
    service.start_analysis(
        scope={"type": "friend", "friend": "张三"},
        scope_label="张三",
        question="图里是什么",
        history=[],
        options={"thinking": False, "attach_images": True, "limit": 5},
    )
    assert bridge.wait_event("llm_done"), bridge.types()

    start = bridge.last_event("llm_start")["data"]
    assert start["image_count"] == 1
    content = fake_deepseek.requests[0]["body"]["messages"][-1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text" and "图里是什么" in content[0]["text"]
    image_blocks = [block for block in content if block.get("type") == "image_url"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["image_url"]["url"].startswith("data:image/png;base64,")


def test_llm_service_test_connection_ok(tmp_path):
    """test_connection 用 GET /models 探活，返回延迟与模型列表。"""
    server = _FakeDeepSeekServer("ok")
    try:
        service, _store, _bridge = _make_llm_service(tmp_path, server)
        result = service.test_connection()
        assert result["ok"] is True
        assert isinstance(result["latency_ms"], int) and result["latency_ms"] >= 0
        assert result["error"] is None
        assert any(item["id"] == "deepseek-flash" for item in result["models"])
    finally:
        server.close()


def test_llm_service_401_maps_to_chinese_error(tmp_path):
    """HTTP 401 映射成中文「API Key 无效」并推 llm_error 事件。"""
    server = _FakeDeepSeekServer("unauthorized")
    try:
        service, _store, bridge = _make_llm_service(tmp_path, server)
        service.start_analysis(
            scope={"type": "all"}, scope_label="全部消息", question="测试 401",
            history=[], options={"thinking": False, "attach_images": False, "limit": 5},
        )
        assert bridge.wait_event("llm_error"), bridge.types()
        error = bridge.last_event("llm_error")["data"]
        assert error["code"] == "llm_error"
        assert "API Key" in error["error"] or "无效" in error["error"]
        assert _wait_until(lambda: not service.is_busy())
    finally:
        server.close()


def test_llm_service_cancel_and_busy(tmp_path):
    """同一时刻只允许一个分析（busy）；cancel 后发 cancelled=True 的 llm_done。"""
    server = _FakeDeepSeekServer("slow")
    try:
        service, _store, bridge = _make_llm_service(tmp_path, server)
        first = service.start_analysis(
            scope={"type": "all"}, scope_label="全部消息", question="慢速分析",
            history=[], options={"thinking": True, "attach_images": False, "limit": 5},
        )
        with pytest.raises(ValueError) as busy_info:
            service.start_analysis(
                scope={"type": "all"}, scope_label="全部消息", question="并发分析",
                history=[], options={"thinking": False, "attach_images": False, "limit": 5},
            )
        assert getattr(busy_info.value, "code", "") == "busy"

        assert bridge.wait_event("llm_delta", timeout=8), "慢速流没有产生增量"
        assert service.cancel(first["job_id"]) is True
        assert bridge.wait_event("llm_done", timeout=8), bridge.types()
        done = bridge.last_event("llm_done")["data"]
        assert done["cancelled"] is True
        assert done["job_id"] == first["job_id"]
        assert _wait_until(lambda: not service.is_busy()), "取消后仍显示忙碌"
        assert service.cancel(first["job_id"]) is False, "没有任务时 cancel 应返回 False"
    finally:
        server.close()


def test_llm_service_not_configured_and_validation(tmp_path, fake_deepseek):
    """未配置 key / 空问题 / 非法范围都要抛带 code 的 ValueError，且不发起请求。"""
    from webui.llm import LLMConfigStore, LLMService

    store = LLMConfigStore(tmp_path / "web_config.json")
    bridge = _RecordingBridge(_sample_messages())
    service = LLMService(store, bridge, logger=_quiet_log)

    with pytest.raises(ValueError) as info:
        service.start_analysis(
            scope={"type": "all"}, scope_label="全部消息", question="你好",
            history=[], options={},
        )
    assert getattr(info.value, "code", "") == "not_configured"
    assert not bridge.events, "未配置时不应广播事件"
    assert not fake_deepseek.requests, "未配置时不应发起网络请求"

    store.save({"api_key": "sk-test-1234567890abcd"})
    for scope, question, code in (
        ({"type": "all"}, "  ", "bad_request"),
        ({"type": "unknown"}, "问题", "bad_request"),
        ({"type": "moment"}, "问题", "bad_request"),
    ):
        with pytest.raises(ValueError) as info:
            service.start_analysis(
                scope=scope, scope_label="", question=question, history=[], options={}
            )
        assert getattr(info.value, "code", "") == code, (scope, question)
    assert not fake_deepseek.requests


# ----------------------------------------------------------------------
# LLMService：朋友圈范围（moments / moment）
# ----------------------------------------------------------------------
def _sample_moments() -> list[dict]:
    return [
        {
            "id": "15034350704629592685",
            "username": "wxid_lisi",
            "author": "李四",
            "avatar": "media/moments/avatars/lisi.jpg",
            "create_time": 1760000000,
            "time_text": "10-17 18:51",
            "content": "周末去爬山了，风景很好",
            "location": "杭州·西湖",
            "link": {"title": "一篇关于爬山的文章", "url": "https://example.com/hike"},
            "media": [{"type": "image", "path": "media/moments/images/a.jpg",
                       "width": 1280, "height": 990}],
            "comments": [{"author": "张三", "content": "羡慕！"}],
            "likes": ["张三", "王五"],
            "raw_comment_count": 1,
        },
        {
            "id": "15034350704629592686",
            "username": "wxid_zhangsan",
            "author": "张三",
            "avatar": "",
            "create_time": 1760000100,
            "time_text": "10-17 19:01",
            "content": "晚上吃火锅",
            "location": "",
            "link": None,
            "media": [],
            "comments": [],
            "likes": [],
            "raw_comment_count": 0,
        },
    ]


class _FakeMoments:
    """只实现 LLMService 朋友圈范围用到的 ``scope_items``。"""

    def __init__(self, items: list[dict] | None = None):
        self.items_data = list(items or _sample_moments())
        self.calls: list[tuple[dict, int]] = []

    def scope_items(self, scope: dict, limit: int = 50) -> list[dict]:
        scope = dict(scope or {})
        self.calls.append((scope, int(limit)))
        if scope.get("type") == "moments":
            return [dict(item) for item in self.items_data[: int(limit)]]
        if scope.get("type") == "moment":
            return [dict(item) for item in self.items_data if item["id"] == scope.get("id")]
        return []


def test_llm_service_moments_scope(tmp_path, fake_deepseek):
    """scope=moments：朋友圈转写稿含作者/正文/位置/点赞/评论，scope_label 默认「全部朋友圈」。"""
    moments = _FakeMoments()
    service, _store, bridge = _make_llm_service(tmp_path, fake_deepseek, moments=moments)
    service.start_analysis(
        scope={"type": "moments"}, scope_label="", question="总结一下朋友圈",
        history=[], options={"thinking": False, "attach_images": False, "limit": 10},
    )
    assert bridge.wait_event("llm_done"), bridge.types()

    start = bridge.last_event("llm_start")["data"]
    assert start["scope_label"] == "全部朋友圈"
    assert start["message_count"] == 2
    assert start["image_count"] == 0
    assert moments.calls and moments.calls[0][0] == {"type": "moments"}

    body = fake_deepseek.requests[0]["body"]
    assert body["thinking"] == {"type": "disabled"}
    assert body["max_tokens"] == 8192
    user_text = body["messages"][-1]["content"]
    for fragment in (
        "周末去爬山了，风景很好", "李四", "杭州·西湖", "点赞（2）：张三、王五",
        "羡慕！", "一篇关于爬山的文章", "## 我的问题", "总结一下朋友圈",
    ):
        assert fragment in user_text, f"朋友圈转写稿缺少 {fragment}"


def test_llm_service_single_moment_scope(tmp_path, fake_deepseek):
    """scope=moment：只分析指定 id 的那条，缺省标签用「作者 的朋友圈」。"""
    moments = _FakeMoments()
    service, _store, bridge = _make_llm_service(tmp_path, fake_deepseek, moments=moments)
    service.start_analysis(
        scope={"type": "moment", "id": "15034350704629592685"},
        scope_label="",
        question="这条动态什么意思",
        history=[],
        options={"thinking": False, "attach_images": False, "limit": 10},
    )
    assert bridge.wait_event("llm_done"), bridge.types()

    start = bridge.last_event("llm_start")["data"]
    assert start["scope_label"] == "李四 的朋友圈"
    assert start["message_count"] == 1
    user_text = fake_deepseek.requests[0]["body"]["messages"][-1]["content"]
    assert "周末去爬山了，风景很好" in user_text
    assert "晚上吃火锅" not in user_text, "单条朋友圈不应混入其它动态"


def test_llm_service_moments_unavailable(tmp_path, fake_deepseek):
    """朋友圈服务为 None 时，朋友圈范围分析抛可读错误（code=server_error）。"""
    service, _store, _bridge = _make_llm_service(tmp_path, fake_deepseek, moments=None)
    with pytest.raises(ValueError) as info:
        service.start_analysis(
            scope={"type": "moments"}, scope_label="全部朋友圈", question="总结",
            history=[], options={},
        )
    assert getattr(info.value, "code", "") == "server_error"
    assert "朋友圈模块不可用" in str(info.value)
    assert not fake_deepseek.requests


def test_llm_service_moments_attach_images_from_memory(tmp_path, fake_deepseek):
    """V2：朋友圈附图片从 moments.media(thumb_key) 内存取，转成 data URL 内容块。"""

    class _FakeMomentsWithMedia(_FakeMoments):
        def __init__(self):
            super().__init__(
                [
                    {
                        "id": "15034350704629592700",
                        "username": "wxid_lisi",
                        "author": "李四",
                        "create_time": 1760000000,
                        "content": "发了一张图",
                        "location": "",
                        "link": None,
                        "media": [
                            {"type": "image", "thumb_key": "t_1_0",
                             "thumb_mime": "image/jpeg", "path": "", "thumb": ""}
                        ],
                        "comments": [],
                        "likes": [],
                        "raw_comment_count": 0,
                    }
                ]
            )
            self.blobs = {"t_1_0": (b"\xff\xd8\xff\xe0memory-jpeg", "image/jpeg")}
            self.media_calls: list[str] = []

        def media(self, key: str):
            self.media_calls.append(str(key))
            return self.blobs.get(str(key))

    moments = _FakeMomentsWithMedia()
    service, _store, bridge = _make_llm_service(tmp_path, fake_deepseek, moments=moments)
    service.start_analysis(
        scope={"type": "moments"}, scope_label="全部朋友圈", question="图里是什么",
        history=[], options={"thinking": False, "attach_images": True, "limit": 10},
    )
    assert bridge.wait_event("llm_done"), bridge.types()

    start = bridge.last_event("llm_start")["data"]
    assert start["image_count"] == 1
    assert moments.media_calls == ["t_1_0"], "应从内存媒体接口取图"
    content = fake_deepseek.requests[0]["body"]["messages"][-1]["content"]
    assert isinstance(content, list)
    image_blocks = [block for block in content if block.get("type") == "image_url"]
    assert len(image_blocks) == 1
    assert image_blocks[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_llm_service_group_transcript(tmp_path, fake_deepseek):
    """V3 群聊转写稿：群名/成员名 + 非文字类型标记；自己与私聊保持原格式。"""
    now = time.time()
    messages = [
        {
            "id": "g-1", "friend": "测试群", "sender": "friend", "sender_label": "对方",
            "kind": "text", "kind_label": "文字", "text": "大家好",
            "ts": now - 40, "extra": {"is_group": True, "sender_name": "张三"},
        },
        {
            "id": "g-2", "friend": "测试群", "sender": "friend", "sender_label": "对方",
            "kind": "image", "kind_label": "图片", "text": "",
            "ts": now - 30, "extra": {"is_group": True, "sender_name": "李四",
                                       "size_text": "216.0 KB"},
        },
        {
            "id": "g-3", "friend": "测试群", "sender": "self", "sender_label": "我",
            "kind": "text", "kind_label": "文字", "text": "收到",
            "ts": now - 20, "extra": {"is_group": True, "sender_name": "张三"},
        },
        {
            "id": "g-4", "friend": "白云一片去悠悠", "sender": "friend",
            "sender_label": "对方", "kind": "text", "kind_label": "文字",
            "text": "今天的需求文档放群里了", "ts": now - 10, "extra": {},
        },
    ]
    bridge = _RecordingBridge(messages)
    service, _store, _bridge = _make_llm_service(tmp_path, fake_deepseek, bridge=bridge)
    service.start_analysis(
        scope={"type": "all"}, scope_label="全部消息", question="总结一下群聊",
        history=[], options={"thinking": False, "attach_images": False, "limit": 20},
    )
    assert bridge.wait_event("llm_done"), bridge.types()
    user_text = fake_deepseek.requests[0]["body"]["messages"][-1]["content"]
    assert "测试群/张三（对方）：大家好" in user_text, user_text
    assert "测试群/李四（对方 · 图片）：[图片]" in user_text
    assert "测试群 · 我（文字）：收到" in user_text, "自己消息应保持「群名 · 我」格式"
    assert "白云一片去悠悠 · 对方（文字）：今天的需求文档放群里了" in user_text


# ----------------------------------------------------------------------
# WebBridge + 假后端：服务端 HTTP / SSE / 鉴权 / 路径安全
# ----------------------------------------------------------------------
def _http_request(web_server, method: str, path: str, *, body=None, token=None,
                  headers: dict | None = None):
    """发一个本机 HTTP 请求，返回 ``(status, headers_dict, json_data, raw_bytes)``。

    Windows 本机偶发 ``ConnectionAbortedError(WinError 10053)``（客户端 send 阶段
    被系统瞬时中止，非服务端断言失败），这里对这类瞬时 socket 中断重试一次，
    避免基线随机红；业务断言仍只针对重试后的结果。
    """
    request_headers = dict(headers or {})
    # 每个请求用独立连接并显式要求关闭：避免测试客户端关闭 keep-alive 连接时
    # 与 HTTPServer 的下一轮 request-line 解析竞争（会产生无害但吵的 stderr）。
    request_headers.setdefault("Connection", "close")
    if token is not None:
        request_headers["X-WXBOX-Token"] = token
    payload = None
    if body is not None:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    for attempt in (0, 1):
        conn = http.client.HTTPConnection("127.0.0.1", web_server.port, timeout=8)
        try:
            conn.request(method, path, body=payload, headers=request_headers)
            response = conn.getresponse()
            status = int(response.status)
            response_headers = {name.lower(): value for name, value in response.getheaders()}
            raw = response.read()
            break
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as exc:
            if attempt == 0:
                time.sleep(0.05)
                continue
            raise
        finally:
            conn.close()
    data = None
    if raw and "application/json" in response_headers.get("content-type", ""):
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None
    return status, response_headers, data, raw


@pytest.fixture(scope="module")
def web_server(tmp_path_factory):
    """起一个绑定 127.0.0.1:0 的真实 HTTP 服务（假后端 + 事件桥 + LLMService）。"""
    from types import SimpleNamespace

    from ui.dev_backend import DevBackend
    from webui.bridge import WebBridge
    from webui.llm import LLMConfigStore, LLMService
    from webui.server import WebContext, create_server

    base = tmp_path_factory.mktemp("webui_server")
    backend = DevBackend({}, interval=0.15)
    backend.start()
    bridge = WebBridge(backend, mode="fake")
    bridge.start()
    store = LLMConfigStore(base / "web_config.json")
    llm = LLMService(store, bridge, logger=_quiet_log)
    token = "0123456789abcdef" * 4
    context = WebContext(
        bridge=bridge,
        llm=llm,
        token=token,
        host="127.0.0.1",
        port=0,
        mode="fake",
        version="1.0.0",
        backend_note="演示模式（测试）",
    )
    server = create_server(context, "127.0.0.1", 0)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.02},
        name="wxbox-test-server",
        daemon=True,
    )
    thread.start()

    info = SimpleNamespace(
        server=server, context=context, bridge=bridge, backend=backend,
        store=store, token=token, host="127.0.0.1", port=int(context.port),
    )
    assert info.port and info.port not in (8080, 8081), "测试服务不应占用 8080/8081"
    assert _wait_until(lambda: len(bridge.snapshot()["messages"]) >= 2, timeout=10), "假后端数据未就绪"
    assert _wait_until(lambda: (ROOT / "media" / "demo_image.png").is_file(), timeout=10)

    yield info

    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    bridge.stop()
    backend.stop()


def test_server_health_no_token(web_server):
    """GET /api/health 不校验令牌，返回版本/模式/运行时长，且不返回 CORS 头。"""
    status, headers, data, _raw = _http_request(web_server, "GET", "/api/health")
    assert status == 200
    assert data is not None and data["ok"] is True
    assert data["version"] == "1.0.0"
    assert data["mode"] == "fake"
    assert isinstance(data["uptime_s"], (int, float))
    assert "application/json" in headers["content-type"]
    assert "access-control-allow-origin" not in headers, "不应返回 CORS 头"


def test_server_bootstrap_token_required(web_server):
    """无令牌 / 错令牌 401；带令牌返回 4.2 全量字段。"""
    status, _headers, data, _raw = _http_request(web_server, "GET", "/api/bootstrap")
    assert status == 401
    assert data is not None and data["ok"] is False and data["code"] == "unauthorized"

    status, _headers, data, _raw = _http_request(
        web_server, "GET", "/api/bootstrap", token="wrong-token"
    )
    assert status == 401

    status, _headers, data, _raw = _http_request(
        web_server, "GET", "/api/bootstrap", token=web_server.token
    )
    assert status == 200 and data is not None
    assert {"server", "state", "friends", "messages", "logs", "llm", "moments"} <= set(data)
    assert data["server"]["port"] == web_server.port
    assert data["server"]["mode"] == "fake"
    assert data["server"]["backend_note"] == "演示模式（测试）"
    assert isinstance(data["messages"], list) and data["messages"], "应包含假后端消息"
    assert data["messages"] == sorted(data["messages"], key=lambda m: m["ts"])
    assert len({m["id"] for m in data["messages"]}) == len(data["messages"])
    assert data["state"]["message_total"] >= len(data["messages"])

    llm_config = data["llm"]
    assert {"configured", "model", "model_label", "api_key_masked", "models"} <= set(llm_config)
    assert llm_config["model"] == "deepseek-flash"
    assert llm_config["model_label"] == "DeepSeek V4.1 Flash 1017"
    assert [item["id"] for item in llm_config["models"]] == ["deepseek-flash"]

    moments = data["moments"]
    assert "status" in moments and "count" in moments and "account" in moments
    assert moments["status"]["available"] is False, "测试上下文未注入朋友圈服务"
    assert moments["status"]["limit"] == 10, "V2 朋友圈状态应带 limit"
    assert data["history"]["available"] is False, "测试上下文未注入 HistoryService"
    assert data["groups"]["available"] is False, "测试上下文未注入 GroupsService"
    assert data["contacts"]["available"] is False, "测试上下文未注入 ContactsService"
    assert data["server"]["host"] == "127.0.0.1"


def test_server_token_via_query(web_server):
    """令牌也可以用 ?token= 传（<img>/EventSource 场景）。"""
    status, _headers, data, _raw = _http_request(
        web_server, "GET", f"/api/bootstrap?token={web_server.token}"
    )
    assert status == 200
    assert "server" in data and "state" in data and "messages" in data


def test_server_host_header_validation(web_server):
    """Host 头非法（非回环 / 端口不符）返回 403；localhost 允许。"""
    status, _headers, data, _raw = _http_request(
        web_server, "GET", "/api/health",
        headers={"Host": f"evil.example:{web_server.port}"},
    )
    assert status == 403 and data["ok"] is False

    status, _headers, _data, _raw = _http_request(
        web_server, "GET", "/api/health",
        headers={"Host": f"127.0.0.1:{web_server.port + 1}"},
    )
    assert status == 403, "Host 端口不匹配应 403"

    status, _headers, _data, _raw = _http_request(
        web_server, "GET", "/api/health",
        headers={"Host": f"localhost:{web_server.port}"},
    )
    assert status == 200


def test_server_monitored_post(web_server):
    """POST /api/monitored 生效（过滤空名字/去重），无令牌 401。"""
    status, _headers, data, _raw = _http_request(
        web_server, "POST", "/api/monitored", body={"names": ["张三"]}
    )
    assert status == 401

    status, _headers, data, _raw = _http_request(
        web_server, "POST", "/api/monitored", token=web_server.token,
        body={"names": ["张三", "  ", "张三"]},
    )
    assert status == 200 and data["ok"] is True
    assert data["monitoring"] == ["张三"]
    assert _wait_until(
        lambda: web_server.bridge.snapshot()["state"]["monitoring"] == ["张三"]
    ), "监听名单没有同步到事件桥"

    _http_request(
        web_server, "POST", "/api/monitored", token=web_server.token, body={"names": []}
    )
    assert _wait_until(lambda: web_server.bridge.snapshot()["state"]["monitoring"] == [])


def test_server_missing_route_and_methods(web_server):
    """未知路由 404；错误方法 405（/api/health 只 GET、未知方法整体 405）。"""
    status, _headers, data, _raw = _http_request(
        web_server, "GET", f"/api/nope?token={web_server.token}"
    )
    assert status == 404 and data["code"] == "not_found"

    status, _headers, data, _raw = _http_request(
        web_server, "POST", "/api/health", token=web_server.token, body={}
    )
    assert status == 405 and data["code"] == "bad_request"

    status, _headers, _data, _raw = _http_request(
        web_server, "DELETE", "/api/bootstrap", token=web_server.token
    )
    assert status == 405

    status, _headers, _data, _raw = _http_request(
        web_server, "GET", "/api/monitored", token=web_server.token
    )
    assert status == 405, "只接受 POST 的接口用 GET 应 405"

    status, _headers, _data, _raw = _http_request(
        web_server, "POST", "/api/events", token=web_server.token, body={}
    )
    assert status == 405, "SSE 只接受 GET"


def test_server_media_access_and_traversal(web_server):
    """媒体要令牌；合法文件 200；路径穿越/绝对路径 403/404。"""
    status, _headers, _data, _raw = _http_request(
        web_server, "GET", "/media/demo_image.png"
    )
    assert status == 401, "媒体缺令牌应 401"

    status, headers, _data, raw = _http_request(
        web_server, "GET", f"/media/demo_image.png?token={web_server.token}"
    )
    assert status == 200
    assert headers["content-type"] == "image/png"
    assert raw.startswith(b"\x89PNG")

    for path in (
        f"/media/../../config.json?token={web_server.token}",
        f"/media/%2e%2e/%2e%2e/config.json?token={web_server.token}",
        f"/media/..%5c..%5cconfig.json?token={web_server.token}",
        f"/media/C:/Windows/win.ini?token={web_server.token}",
    ):
        status, _headers, _data, _raw = _http_request(web_server, "GET", path)
        assert status in (403, 404), f"路径穿越未被拒绝：{path} -> {status}"


def test_server_static_index_injects_token(web_server):
    """GET / 返回页面且把 __WXBOX_TOKEN__ 替换成会话令牌（带 CSP 头）。"""
    status, headers, _data, raw = _http_request(web_server, "GET", "/")
    assert status == 200
    assert "text/html" in headers["content-type"]
    page = raw.decode("utf-8")
    assert web_server.token in page, "页面没有注入会话令牌"
    assert "__WXBOX_TOKEN__" not in page
    assert "content-security-policy" in headers
    assert headers.get("referrer-policy") == "no-referrer"


def test_server_llm_test_without_key(web_server):
    """未配置 key 时 POST /api/llm/test 不发网络请求，返回可读错误。"""
    web_server.store.save({"api_key": "clear"})
    status, _headers, data, _raw = _http_request(
        web_server, "POST", "/api/llm/test", token=web_server.token, body={}
    )
    assert status == 200 and data["ok"] is True, "接口本身成功，连接结果看 error"
    assert data["latency_ms"] is None
    assert data["error"], "未配置 key 时应给出中文错误"
    assert data["models"] == []


def test_server_llm_config_api(web_server):
    """GET/POST /api/llm/config：保存后返回脱敏配置；非法模型 400；无令牌 401。"""
    status, _headers, config, _raw = _http_request(
        web_server, "GET", "/api/llm/config", token=web_server.token
    )
    assert status == 200 and config is not None
    assert "model" in config and "api_key_masked" in config and "models" in config

    key = "sk-server-test-1234567890"
    status, _headers, data, _raw = _http_request(
        web_server, "POST", "/api/llm/config", token=web_server.token,
        body={"api_key": key, "model": "deepseek-flash",
              "base_url": "https://api.deepseek.com"},
    )
    assert status == 200 and data["ok"] is True
    assert data["config"]["configured"] is True
    assert data["config"]["api_key_masked"] == "sk-…7890"
    assert key not in json.dumps(data, ensure_ascii=False)

    status, _headers, data, _raw = _http_request(
        web_server, "POST", "/api/llm/config", token=web_server.token,
        body={"model": "not-a-real-model"},
    )
    assert status == 400 and data["code"] == "bad_request"

    status, _headers, _data, _raw = _http_request(
        web_server, "POST", "/api/llm/config", body={"model": "deepseek-flash"}
    )
    assert status == 401


def test_server_llm_analyze_and_cancel_validation(web_server):
    """分析参数校验走 400；cancel 无任务时也是 200 + cancelled=false（不联网）。"""
    web_server.store.save(
        {"api_key": "sk-validate-1234567890", "base_url": "http://127.0.0.1:9"}
    )
    try:
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/llm/analyze", token=web_server.token,
            body={"scope": {"type": "all"}, "question": "   ",
                  "history": [], "limit": 10},
        )
        assert status == 400 and data["code"] == "bad_request"

        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/llm/analyze", token=web_server.token,
            body={"scope": {"type": "bogus"}, "question": "问题", "history": []},
        )
        assert status == 400 and data["code"] == "bad_request"

        status, _headers, _data, _raw = _http_request(
            web_server, "POST", "/api/llm/analyze",
            body={"scope": {"type": "all"}, "question": "问题"},
        )
        assert status == 401

        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/llm/cancel", token=web_server.token,
            body={"job_id": "not-a-job"},
        )
        assert status == 200 and data["ok"] is True
        assert data["cancelled"] is False
    finally:
        web_server.store.save({"api_key": "clear"})


def test_server_backend_control_endpoints(web_server):
    """好友重新同步 / 后端启动停止接口返回契约形状。"""
    status, _headers, data, _raw = _http_request(
        web_server, "POST", "/api/friends/sync", token=web_server.token, body={}
    )
    assert status == 200 and data["ok"] is True

    status, _headers, data, _raw = _http_request(
        web_server, "POST", "/api/backend/start", token=web_server.token, body={}
    )
    assert status == 200 and data["ok"] is True and data["running"] is True

    status, _headers, data, _raw = _http_request(
        web_server, "POST", "/api/backend/stop", token=web_server.token, body={}
    )
    assert status == 200 and data["ok"] is True and data["running"] is False
    assert _wait_until(
        lambda: web_server.bridge.snapshot()["state"].get("running") is False
    ), "停止后状态没有更新"
    assert web_server.backend.is_running() is False, "后端线程应已停止"

    total_before_restart = web_server.bridge.snapshot()["state"]["message_total"]
    status, _headers, data, _raw = _http_request(
        web_server, "POST", "/api/backend/start", token=web_server.token, body={}
    )
    assert status == 200 and data["running"] is True
    assert web_server.backend.is_running() is True, "后端线程应已重新启动"
    assert _wait_until(
        lambda: web_server.bridge.snapshot()["state"].get("running") is True
    ), "重新启动后状态没有更新"
    assert _wait_until(
        lambda: web_server.bridge.snapshot()["state"]["message_total"] > total_before_restart,
        timeout=5,
    ), "停止后再次开始应能继续读取新消息"


class _FakeMomentsListening:
    """记录 set_listening 的假朋友圈服务（不启动真实自动检查线程）。"""

    def __init__(self, *, exc: BaseException | None = None):
        self.calls: list[bool] = []
        self.exc = exc

    def set_listening(self, listening: bool) -> dict:
        self.calls.append(bool(listening))
        if self.exc is not None:
            raise self.exc
        return {"ok": True, "listening": bool(listening)}

    def status(self) -> dict:
        return {
            "available": True,
            "auto": True,
            "auto_interval": 30,
            "listening": bool(self.calls and self.calls[-1]),
            "count": 0,
        }


def test_server_backend_start_stop_links_moments_listening(web_server, monkeypatch):
    """/api/backend/start → set_listening(True)，stop → set_listening(False)；响应带 moments_listening。"""
    fake = _FakeMomentsListening()
    monkeypatch.setattr(web_server.context, "moments", fake)
    monkeypatch.setattr(
        web_server.bridge, "start_backend", lambda: {"ok": True, "running": True}
    )
    monkeypatch.setattr(
        web_server.bridge, "stop_backend", lambda: {"ok": True, "running": False}
    )

    status, _headers, _data, _raw = _http_request(
        web_server, "POST", "/api/backend/start", body={}
    )
    assert status == 401, "启动监听接口需要令牌"
    status, _headers, data, _raw = _http_request(
        web_server, "GET", "/api/backend/start", token=web_server.token
    )
    assert status == 405 and data["code"] == "bad_request"

    status, _headers, data, _raw = _http_request(
        web_server, "POST", "/api/backend/start", token=web_server.token, body={}
    )
    assert status == 200 and data["ok"] is True and data["running"] is True
    assert data["moments_listening"] is True
    assert fake.calls == [True], "start 后应调用一次 set_listening(True)"

    status, _headers, data, _raw = _http_request(
        web_server, "POST", "/api/backend/stop", token=web_server.token, body={}
    )
    assert status == 200 and data["ok"] is True and data["running"] is False
    assert data["moments_listening"] is False
    assert fake.calls == [True, False], "stop 后应调用 set_listening(False)"
    assert fake.status()["listening"] is False


def test_server_backend_linkage_skipped_on_missing_or_failure(web_server, monkeypatch):
    """moments=None/旧版/异常/后端失败时静默跳过联动，start/stop 状态码不变。"""
    # moments=None：200 + moments_listening=false，且不会抛 500
    old_moments = web_server.context.moments
    try:
        monkeypatch.setattr(web_server.context, "moments", None)
        monkeypatch.setattr(
            web_server.bridge, "start_backend", lambda: {"ok": True, "running": True}
        )
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/backend/start", token=web_server.token, body={}
        )
        assert status == 200 and data["moments_listening"] is False

        # 旧版朋友圈服务没有 set_listening：同样静默跳过
        class _LegacyMoments:
            pass

        monkeypatch.setattr(web_server.context, "moments", _LegacyMoments())
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/backend/start", token=web_server.token, body={}
        )
        assert status == 200 and data["moments_listening"] is False

        # set_listening 抛异常：被静默吞掉，接口仍 200
        raising = _FakeMomentsListening(exc=RuntimeError("朋友圈联动失败"))
        monkeypatch.setattr(web_server.context, "moments", raising)
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/backend/start", token=web_server.token, body={}
        )
        assert status == 200 and data["moments_listening"] is False
        assert raising.calls == [True], "调用过 set_listening，但异常被吞掉"

        # bridge 启动失败 → 500，且绝不调用 set_listening
        fake = _FakeMomentsListening()
        monkeypatch.setattr(web_server.context, "moments", fake)
        monkeypatch.setattr(
            web_server.bridge, "start_backend",
            lambda: {"ok": False, "error": "后端启动失败（测试）"},
        )
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/backend/start", token=web_server.token, body={}
        )
        assert status == 500 and fake.calls == []

        # bridge 抛异常 → 500，且不联动
        monkeypatch.setattr(
            web_server.bridge, "start_backend",
            lambda: (_ for _ in ()).throw(RuntimeError("启动异常")),
        )
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/backend/start", token=web_server.token, body={}
        )
        assert status == 500 and fake.calls == []

        # 停止失败也不联动
        monkeypatch.setattr(
            web_server.bridge, "stop_backend",
            lambda: (_ for _ in ()).throw(RuntimeError("停止异常")),
        )
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/backend/stop", token=web_server.token, body={}
        )
        assert status == 500 and fake.calls == []
    finally:
        web_server.context.moments = old_moments


def test_server_moments_without_service(web_server):
    """未注入朋友圈服务时：列表可用性 false、同步/清空 503、内存媒体 404。"""
    status, _headers, data, _raw = _http_request(
        web_server, "GET", "/api/moments", token=web_server.token
    )
    assert status == 200 and data["ok"] is True
    assert data["status"]["available"] is False
    assert data["status"]["limit"] == 10
    assert data["items"] == [] and data["count"] == 0
    assert data["status"]["reason"], "应说明朋友圈不可用原因"

    for path in ("/api/moments/sync", "/api/moments/clear"):
        status, _headers, data, _raw = _http_request(
            web_server, "POST", path, token=web_server.token, body={}
        )
        assert status == 503, f"{path} 在服务缺失时应 503"
        assert data["ok"] is False and data["code"] == "server_error"

    status, _headers, data, _raw = _http_request(
        web_server, "GET", "/api/moments/media/t_1_0", token=web_server.token
    )
    assert status == 404 and data["code"] == "not_found"


def _many_moments(count: int) -> list[dict]:
    """造 count 条最小可用的 V2 朋友圈 dict（服务端接口测试用）。"""
    return [
        {
            "id": f"m-{index:03d}",
            "username": "wxid_test",
            "author": "测试作者",
            "create_time": 1760000000 + index,
            "content": f"第 {index} 条",
            "media": [],
            "comments": [],
            "likes": [],
            "raw_comment_count": 0,
        }
        for index in range(count)
    ]


class _FakeMomentsAPI:
    """服务端朋友圈接口 V2 测试替身（status/items/sync_async(limit)/clear/media）。"""

    def __init__(self, items: list[dict] | None = None):
        self.items_data = list(items if items is not None else _sample_moments())
        self.sync_limits: list[int] = []
        self.clear_calls = 0
        self.limit = 10
        self.media_data: dict[str, tuple[bytes, str]] = {
            "t_1_0": (b"\xff\xd8\xff\xe0thumb-bytes", "image/jpeg"),
            "a_1": (b"avatar-bytes", "image/png"),
        }

    def status(self) -> dict:
        return {
            "available": True,
            "reason": "",
            "syncing": False,
            "limit": self.limit,
            "count": len(self.items_data),
            "synced_at": 1760000000.0,
            "account": {"wxid": "wxid_self", "nick_name": "测试号"},
            "error": None,
            "python": "C:/fake/python.exe",
            "moments_dir": "C:/fake/WechatMoments",
        }

    def items(self, limit: int = 30) -> list[dict]:
        return [dict(item) for item in self.items_data[: max(0, int(limit))]]

    def scope_items(self, scope: dict, limit: int = 50) -> list[dict]:
        return self.items(limit)

    def sync_async(self, limit: int = 10) -> dict:
        self.sync_limits.append(int(limit))
        self.limit = int(limit)
        return {"ok": True, "syncing": True}

    def clear(self) -> dict:
        self.clear_calls += 1
        self.items_data = []
        return {"ok": True, "count": 0}

    def media(self, key: str) -> tuple[bytes, str] | None:
        return self.media_data.get(str(key or ""))


def test_server_moments_with_service(web_server):
    """注入朋友圈服务：列表/状态/bootstap/sync(limit) 按 V2 语义工作。"""
    fake = _FakeMomentsAPI(items=_many_moments(40))
    old = web_server.context.moments
    web_server.context.moments = fake
    try:
        status, _headers, data, _raw = _http_request(
            web_server, "GET", "/api/moments?limit=2", token=web_server.token
        )
        assert status == 200 and data["ok"] is True
        assert len(data["items"]) == 2 and data["count"] == 2, "?limit= 生效"
        assert data["status"]["available"] is True
        assert data["status"]["limit"] == 10
        assert data["status"]["count"] == 40
        assert data["items"][0]["id"] == fake.items_data[0]["id"]

        # V2：页面最多展示 30 条，?limit= 超出按 30 处理
        status, _headers, data, _raw = _http_request(
            web_server, "GET", "/api/moments?limit=100", token=web_server.token
        )
        assert status == 200
        assert data["count"] == 30, "V2 最多返回 30 条朋友圈"

        status, _headers, boot, _raw = _http_request(
            web_server, "GET", "/api/bootstrap", token=web_server.token
        )
        assert status == 200
        assert boot["moments"]["count"] == 40
        assert boot["moments"]["status"]["available"] is True
        assert boot["moments"]["status"]["limit"] == 10
        assert boot["moments"]["account"]["nick_name"] == "测试号"

        # sync limit：5/10/20 生效，非法值按 10
        for body, expected in (({"limit": 5}, 5), ({"limit": 7}, 10), ({}, 10)):
            status, _headers, sync, _raw = _http_request(
                web_server, "POST", "/api/moments/sync",
                token=web_server.token, body=body,
            )
            assert status == 200 and sync["ok"] is True and sync["syncing"] is True
            assert sync["limit"] == expected
            assert fake.sync_limits[-1] == expected
        assert fake.sync_limits == [5, 10, 10]
    finally:
        web_server.context.moments = old


def test_server_moments_clear_broadcasts_event(web_server):
    """POST /api/moments/clear 清空并广播 SSE moments(cleared=true)；无令牌 401。"""
    fake = _FakeMomentsAPI(items=_many_moments(5))
    old = web_server.context.moments
    web_server.context.moments = fake
    event_queue, sub_id = web_server.bridge.subscribe()
    try:
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/moments/clear", body={}
        )
        assert status == 401

        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/moments/clear", token=web_server.token, body={}
        )
        assert status == 200 and data["ok"] is True
        assert data["count"] == 0 and data["cleared"] is True
        assert fake.clear_calls == 1

        events = []
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                events.append(event_queue.get(timeout=0.1))
            except queue.Empty:
                continue
            if any(
                e.get("type") == "moments"
                and e.get("data", {}).get("cleared") is True
                for e in events
            ):
                break
        cleared = [
            e for e in events
            if e.get("type") == "moments" and e.get("data", {}).get("cleared") is True
        ]
        assert cleared, f"清空后应广播 moments 事件：{[e.get('type') for e in events]}"
        payload = cleared[0]["data"]
        assert payload["count"] == 0 and payload["limit"] == 10
        assert payload["error"] is None

        status, _headers, data, _raw = _http_request(
            web_server, "GET", "/api/moments", token=web_server.token
        )
        assert data["count"] == 0, "清空后列表应为空"
    finally:
        web_server.bridge.unsubscribe(sub_id)
        web_server.context.moments = old


def test_server_moments_media(web_server):
    """朋友圈内存媒体：token 校验、200+no-store、HEAD、非法 key/不存在 404。"""
    fake = _FakeMomentsAPI()
    old = web_server.context.moments
    web_server.context.moments = fake
    try:
        status, _headers, _data, _raw = _http_request(
            web_server, "GET", "/api/moments/media/t_1_0"
        )
        assert status == 401, "媒体缺令牌应 401"

        status, headers, _data, raw = _http_request(
            web_server, "GET", f"/api/moments/media/t_1_0?token={web_server.token}"
        )
        assert status == 200
        assert headers["content-type"] == "image/jpeg"
        assert headers.get("cache-control") == "no-store"
        assert raw == b"\xff\xd8\xff\xe0thumb-bytes"

        # 头像 key 与 MIME 也应原样返回
        status, headers, _data, raw = _http_request(
            web_server, "GET", f"/api/moments/media/a_1?token={web_server.token}"
        )
        assert status == 200
        assert headers["content-type"] == "image/png"
        assert raw == b"avatar-bytes"

        # HEAD：头 200、无 body
        status, headers, _data, raw = _http_request(
            web_server, "HEAD", f"/api/moments/media/t_1_0?token={web_server.token}"
        )
        assert status == 200 and raw == b""
        assert headers.get("content-type") == "image/jpeg"

        for path in (
            f"/api/moments/media/nope?token={web_server.token}",
            f"/api/moments/media/a%2Fb?token={web_server.token}",
            f"/api/moments/media/a%5Cb?token={web_server.token}",
            f"/api/moments/media/a%00b?token={web_server.token}",
            f"/api/moments/media/a%0D%0Ab?token={web_server.token}",
            f"/api/moments/media/{'x' * 300}?token={web_server.token}",
        ):
            status, _headers, data, _raw = _http_request(web_server, "GET", path)
            assert status == 404, f"非法/不存在的 key 应 404：{path[:60]}"
            assert data["code"] == "not_found"

        # 旧版朋友圈模块没有 media()：404 而不是 500
        class _LegacyMoments:
            def status(self):
                return {"available": True, "limit": 10, "count": 0, "account": {}}

            def items(self, limit=30):
                return []

        web_server.context.moments = _LegacyMoments()
        status, _headers, _data, _raw = _http_request(
            web_server, "GET", f"/api/moments/media/t_1_0?token={web_server.token}"
        )
        assert status == 404
    finally:
        web_server.context.moments = old



class _FakeMomentsAuto:
    """POST /api/moments/auto 测试替身：记录 set_auto，fake status（不启真实轮询）。"""

    def __init__(self, *, status_data: dict | None = None,
                 status_exc: BaseException | None = None,
                 set_auto_exc: BaseException | None = None):
        self.calls: list[tuple[bool, int]] = []
        self.status_data = dict(status_data) if status_data is not None else {
            "available": True,
            "auto": False,
            "auto_interval": 30,
            "auto_latest": None,
            "auto_checked_at": None,
            "count": 0,
        }
        self.status_exc = status_exc
        self.set_auto_exc = set_auto_exc

    def set_auto(self, enabled: bool, interval: int = 30) -> dict:
        self.calls.append((bool(enabled), int(interval)))
        if self.set_auto_exc is not None:
            raise self.set_auto_exc
        self.status_data["auto"] = bool(enabled)
        self.status_data["auto_interval"] = int(interval)
        return {"ok": True, "enabled": bool(enabled), "interval": int(interval)}

    def status(self) -> dict:
        if self.status_exc is not None:
            raise self.status_exc
        return dict(self.status_data)


def test_server_moments_auto_success(web_server, monkeypatch):
    """POST /api/moments/auto：401、开关、enabled 多种写法、status 回传、GET 405。"""
    fake = _FakeMomentsAuto()
    monkeypatch.setattr(web_server.context, "moments", fake)

    status, _headers, _data, _raw = _http_request(
        web_server, "POST", "/api/moments/auto", body={"enabled": True}
    )
    assert status == 401, "自动监听接口需要令牌"

    status, _headers, data, _raw = _http_request(
        web_server, "POST", "/api/moments/auto", token=web_server.token,
        body={"enabled": True},
    )
    assert status == 200 and data["ok"] is True
    assert data["enabled"] is True and data["interval"] == 30
    assert data["status"]["auto"] is True
    assert data["status"]["auto_interval"] == 30
    assert fake.calls == [(True, 30)]

    # 关闭 + 自定义 interval；enabled 支持 bool / "true" / "false" / 1 / 0
    for body, expected_enabled in (
        ({"enabled": False, "interval": 60}, False),
        ({"enabled": "true", "interval": 60}, True),
        ({"enabled": "false"}, False),
        ({"enabled": 1}, True),
        ({"enabled": 0}, False),
        ({"enabled": "abc"}, False),  # 无法识别按默认 False
    ):
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/moments/auto", token=web_server.token,
            body=body,
        )
        assert status == 200, body
        assert data["enabled"] is expected_enabled, body
        assert fake.calls[-1][0] is expected_enabled, body

    status, _headers, data, _raw = _http_request(
        web_server, "GET", "/api/moments/auto", token=web_server.token
    )
    assert status == 405 and data["code"] == "bad_request"


def test_server_moments_auto_interval_normalization(web_server, monkeypatch):
    """interval 归一：缺省/非法 30、<10 收敛 10、>300 收敛 300、10..300 原样。"""
    fake = _FakeMomentsAuto()
    monkeypatch.setattr(web_server.context, "moments", fake)

    cases = [
        ({}, 30),
        ({"interval": None}, 30),
        ({"interval": ""}, 30),
        ({"interval": "abc"}, 30),
        ({"interval": 5}, 10),
        ({"interval": -20}, 10),
        ({"interval": 1000}, 300),
        ({"interval": "999"}, 300),
        ({"interval": 10}, 10),
        ({"interval": 20}, 20),
        ({"interval": 300}, 300),
    ]
    for extra, expected in cases:
        body = {"enabled": True, **extra}
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/moments/auto", token=web_server.token,
            body=body,
        )
        assert status == 200, body
        assert data["interval"] == expected, body
        assert fake.calls[-1][1] == expected, body
        assert data["status"]["auto_interval"] == expected, body


def test_server_moments_auto_validation_and_errors(web_server, monkeypatch):
    """缺 enabled 400 且不调用；None/旧版/异常 503；status 失败仍 200；失败隔离。"""
    fake = _FakeMomentsAuto()
    monkeypatch.setattr(web_server.context, "moments", fake)

    status, _headers, data, _raw = _http_request(
        web_server, "POST", "/api/moments/auto", token=web_server.token, body={}
    )
    assert status == 400 and data["code"] == "bad_request"
    assert "enabled" in data["error"]
    assert fake.calls == [], "缺少 enabled 时不应调用 set_auto"

    old_moments = web_server.context.moments
    try:
        monkeypatch.setattr(web_server.context, "moments", None)
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/moments/auto", token=web_server.token,
            body={"enabled": True},
        )
        assert status == 503 and "朋友圈模块未启用" in data["error"]

        # 旧版 moments 没有 set_auto
        class _Legacy:
            def status(self):
                return {"available": True}

        monkeypatch.setattr(web_server.context, "moments", _Legacy())
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/moments/auto", token=web_server.token,
            body={"enabled": True},
        )
        assert status == 503 and "不支持自动监听" in data["error"]

        # set_auto 抛异常 → 503 中文透传
        monkeypatch.setattr(
            web_server.context, "moments",
            _FakeMomentsAuto(set_auto_exc=RuntimeError("sns.db 被占用")),
        )
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/moments/auto", token=web_server.token,
            body={"enabled": True},
        )
        assert status == 503 and data["code"] == "server_error"
        assert "设置朋友圈自动监听失败" in data["error"]
        assert "sns.db 被占用" in data["error"]

        # status() 抛异常：设置仍成功 200，status 兜底 {}
        boom_status = _FakeMomentsAuto(status_exc=RuntimeError("status 失败"))
        monkeypatch.setattr(web_server.context, "moments", boom_status)
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/moments/auto", token=web_server.token,
            body={"enabled": True, "interval": 15},
        )
        assert status == 200 and data["ok"] is True
        assert data["enabled"] is True and data["interval"] == 15
        assert data["status"] == {}
        assert boom_status.calls == [(True, 15)]

        # 失败隔离
        status, _headers, _data, _raw = _http_request(web_server, "GET", "/api/health")
        assert status == 200
        status, _headers, boot, _raw = _http_request(
            web_server, "GET", "/api/bootstrap", token=web_server.token
        )
        assert status == 200
    finally:
        web_server.context.moments = old_moments


class _FakeMomentsVideo:
    """POST/GET /api/moments/video 测试替身：返回内存 bytes，记录 (id, index)。"""

    def __init__(self, data: bytes | None = None, mime: str = "video/mp4",
                 exc: BaseException | None = None):
        self.data = data
        self.mime = mime
        self.exc = exc
        self.calls: list[tuple[str, int]] = []

    def video(self, moment_id: str, index: int):
        self.calls.append((str(moment_id), int(index)))
        if self.exc is not None:
            raise self.exc
        if self.data is None:
            return None
        return (self.data, self.mime)


def _moments_video_bytes() -> bytes:
    return bytes(index % 251 for index in range(500))


def test_server_moments_video_full_head_and_tokens(web_server, monkeypatch):
    """朋友圈视频：401、双 token、200 头/body、HEAD、HEAD+Range、id 原样与 int index。"""
    data = _moments_video_bytes()
    fake = _FakeMomentsVideo(data=data)
    monkeypatch.setattr(web_server.context, "moments", fake)

    status, _headers, _data, _raw = _http_request(
        web_server, "GET", "/api/moments/video/123/0"
    )
    assert status == 401, "视频接口需要令牌"

    status, _headers, _data, _raw = _http_request(
        web_server, "GET", "/api/moments/video/123/0?token=wrong"
    )
    assert status == 401

    status, headers, _data, raw = _http_request(
        web_server, "GET", f"/api/moments/video/123/0?token={web_server.token}"
    )
    assert status == 200 and raw == data
    assert headers["content-type"] == "video/mp4"
    assert headers["content-length"] == str(len(data))
    assert headers["accept-ranges"] == "bytes"
    assert headers["cache-control"] == "no-store"
    assert headers.get("x-content-type-options") == "nosniff"
    assert fake.calls == [("123", 0)]
    assert isinstance(fake.calls[0][1], int)

    # 字母 id 原样传参 + header 令牌
    status, headers, _data, raw = _http_request(
        web_server, "GET", "/api/moments/video/moment_abc/2",
        token=web_server.token,
    )
    assert status == 200 and headers["content-length"] == str(len(data))
    assert fake.calls[-1] == ("moment_abc", 2)

    # HEAD：同头无 body
    status, headers, _data, raw = _http_request(
        web_server, "HEAD", "/api/moments/video/123/0", token=web_server.token
    )
    assert status == 200 and raw == b""
    assert headers["content-length"] == str(len(data))
    assert headers["accept-ranges"] == "bytes"

    # HEAD + Range：206 头无 body
    status, headers, _data, raw = _http_request(
        web_server, "HEAD", "/api/moments/video/123/0", token=web_server.token,
        headers={"Range": "bytes=0-99"},
    )
    assert status == 206 and raw == b""
    assert headers["content-range"] == f"bytes 0-99/{len(data)}"
    assert headers["content-length"] == "100"


def test_server_moments_video_range_requests(web_server, monkeypatch):
    """朋友圈视频 Range：bytes=0-99 / 400- / -50 / 越界尾部截断 → 206 精确片段。"""
    data = _moments_video_bytes()
    monkeypatch.setattr(
        web_server.context, "moments", _FakeMomentsVideo(data=data)
    )
    cases = [
        ("bytes=0-99", 0, 99),
        ("bytes=400-", 400, 499),
        ("bytes=-50", 450, 499),
        ("bytes=450-9999", 450, 499),
    ]
    for range_value, start, end in cases:
        status, headers, _data, raw = _http_request(
            web_server, "GET",
            f"/api/moments/video/123/0?token={web_server.token}",
            headers={"Range": range_value},
        )
        assert status == 206, range_value
        assert headers["content-range"] == f"bytes {start}-{end}/{len(data)}"
        assert headers["content-length"] == str(end - start + 1)
        assert headers["accept-ranges"] == "bytes"
        assert headers["cache-control"] == "no-store"
        assert raw == data[start:end + 1], range_value


def test_server_moments_video_range_416(web_server, monkeypatch):
    """越界/多段/单位错/`-0`/end<start → 416 + Content-Range: bytes */total。"""
    data = _moments_video_bytes()
    monkeypatch.setattr(
        web_server.context, "moments", _FakeMomentsVideo(data=data)
    )
    for range_value in (
        "bytes=500-",       # start 越界
        "bytes=0-1,3-4",    # 多段
        "items=0-1",        # 单位错
        "bytes=-0",         # 后缀 0
        "bytes=50-10",      # end < start
        "bytes=abc",        # 格式错
    ):
        status, headers, payload, _raw = _http_request(
            web_server, "GET",
            f"/api/moments/video/123/0?token={web_server.token}",
            headers={"Range": range_value},
        )
        assert status == 416, range_value
        assert headers.get("content-range") == f"bytes */{len(data)}", range_value
        assert headers.get("accept-ranges") == "bytes", range_value
        assert payload["ok"] is False


def test_server_moments_video_bad_path_400(web_server, monkeypatch):
    """缺 id/序号、非整数、负数、空 id → 400 且不调用 video()。"""
    fake = _FakeMomentsVideo(data=_moments_video_bytes())
    monkeypatch.setattr(web_server.context, "moments", fake)

    for path in (
        "/api/moments/video/",          # 缺 id 和序号
        "/api/moments/video/123",       # 缺序号
        "/api/moments/video/123/abc",   # 序号非整数
        "/api/moments/video/123/-1",    # 负数
        "/api/moments/video//0",        # 空 id
        "/api/moments/video/123/1.5",   # 非整数
    ):
        status, _headers, payload, _raw = _http_request(
            web_server, "GET", f"{path}?token={web_server.token}"
        )
        assert status == 400, path
        assert payload["code"] == "bad_request"
    assert fake.calls == [], "参数非法时不应调用 video()"


def test_server_moments_video_not_found_and_errors(web_server, monkeypatch):
    """404（None/空 bytes）、500（video 抛异常）、503（None/旧版无 video）。"""
    old_moments = web_server.context.moments
    try:
        for fake in (
            _FakeMomentsVideo(data=None),
            _FakeMomentsVideo(data=b""),
        ):
            monkeypatch.setattr(web_server.context, "moments", fake)
            status, _headers, payload, _raw = _http_request(
                web_server, "GET",
                f"/api/moments/video/123/0?token={web_server.token}",
            )
            assert status == 404 and payload["code"] == "not_found"

        monkeypatch.setattr(
            web_server.context, "moments",
            _FakeMomentsVideo(exc=RuntimeError("CDN 下载超时")),
        )
        status, _headers, payload, _raw = _http_request(
            web_server, "GET",
            f"/api/moments/video/123/0?token={web_server.token}",
        )
        assert status == 500 and payload["code"] == "server_error"
        assert "读取朋友圈视频失败" in payload["error"]
        assert "CDN 下载超时" in payload["error"]

        # moments=None → 503（在路径解析之前判断）
        monkeypatch.setattr(web_server.context, "moments", None)
        status, _headers, payload, _raw = _http_request(
            web_server, "GET",
            f"/api/moments/video/123/0?token={web_server.token}",
        )
        assert status == 503 and "朋友圈模块未启用" in payload["error"]

        # 旧版 moments 没有 video 方法 → 503 可读提示
        class _LegacyMoments:
            pass

        monkeypatch.setattr(web_server.context, "moments", _LegacyMoments())
        status, _headers, payload, _raw = _http_request(
            web_server, "GET",
            f"/api/moments/video/123/0?token={web_server.token}",
        )
        assert status == 503 and "webui/moments.py" in payload["error"]
    finally:
        web_server.context.moments = old_moments


class _FakeVideoAuto:
    """实验性视频自动缓存接口测试替身（只记录 set_enabled，不碰真实微信）。"""

    def __init__(self, *, status_data: dict | None = None,
                 status_exc: BaseException | None = None,
                 set_exc: BaseException | None = None,
                 set_result: dict | None = None):
        self.status_data = dict(status_data) if status_data is not None else {
            "enabled": False,
            "last_result": "",
            "last_error": "",
            "disabled": False,
        }
        self.status_exc = status_exc
        self.set_exc = set_exc
        self.set_result = set_result
        self.calls: list[bool] = []

    def status(self) -> dict:
        if self.status_exc is not None:
            raise self.status_exc
        return dict(self.status_data)

    def set_enabled(self, enabled: bool):
        self.calls.append(bool(enabled))
        if self.set_exc is not None:
            raise self.set_exc
        self.status_data["enabled"] = bool(enabled)
        self.status_data["last_result"] = "已开启" if enabled else "已关闭"
        if self.set_result is not None:
            return dict(self.set_result)
        return dict(self.status_data)


def test_server_video_auto_get_head_status(web_server, monkeypatch):
    """GET/HEAD /api/video/auto：401、200 status、HEAD 无 body、缺失/异常 503。"""
    status, _headers, _data, _raw = _http_request(
        web_server, "GET", "/api/video/auto"
    )
    assert status == 401

    fake = _FakeVideoAuto()
    monkeypatch.setattr(web_server.context, "video_auto", fake)
    status, _headers, data, _raw = _http_request(
        web_server, "GET", "/api/video/auto", token=web_server.token
    )
    assert status == 200 and data["ok"] is True
    assert data["status"]["enabled"] is False
    assert data["status"]["last_result"] == ""

    status, _headers, _data, _raw = _http_request(
        web_server, "GET", "/api/video/auto",
        headers={"X-WXBOX-Token": "wrong"},
    )
    assert status == 401

    status, headers, data, raw = _http_request(
        web_server, "HEAD", "/api/video/auto", token=web_server.token
    )
    assert status == 200 and raw == b""
    assert "application/json" in headers["content-type"]

    # video_auto=None → 503
    monkeypatch.setattr(web_server.context, "video_auto", None)
    status, _headers, data, _raw = _http_request(
        web_server, "GET", "/api/video/auto", token=web_server.token
    )
    assert status == 503 and "实验性视频缓存未启用" in data["error"]

    # 旧版没有 status 方法 → 503
    class _NoStatus:
        pass

    monkeypatch.setattr(web_server.context, "video_auto", _NoStatus())
    status, _headers, data, _raw = _http_request(
        web_server, "GET", "/api/video/auto", token=web_server.token
    )
    assert status == 503 and "webui/video_auto.py" in data["error"]

    # status() 抛异常 → 503 + 中文原因
    monkeypatch.setattr(
        web_server.context, "video_auto",
        _FakeVideoAuto(status_exc=RuntimeError("缓存线程异常")),
    )
    status, _headers, data, _raw = _http_request(
        web_server, "GET", "/api/video/auto", token=web_server.token
    )
    assert status == 503
    assert "读取视频缓存状态失败" in data["error"] and "缓存线程异常" in data["error"]


def test_server_video_auto_post_toggle(web_server, monkeypatch):
    """POST：enabled 六种写法、响应 enabled/status、缺 enabled 400 不调用、PUT 405。"""
    fake = _FakeVideoAuto()
    monkeypatch.setattr(web_server.context, "video_auto", fake)

    status, _headers, _data, _raw = _http_request(
        web_server, "POST", "/api/video/auto", body={"enabled": True}
    )
    assert status == 401

    cases = [
        ({"enabled": True}, True),
        ({"enabled": False}, False),
        ({"enabled": "true"}, True),
        ({"enabled": "false"}, False),
        ({"enabled": 1}, True),
        ({"enabled": 0}, False),
    ]
    for body, expected in cases:
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/video/auto", token=web_server.token,
            body=body,
        )
        assert status == 200 and data["ok"] is True, body
        assert data["enabled"] is expected, body
        assert data["status"]["enabled"] is expected, body
        assert fake.calls[-1] is expected, body

    before = list(fake.calls)
    status, _headers, data, _raw = _http_request(
        web_server, "POST", "/api/video/auto", token=web_server.token, body={}
    )
    assert status == 400 and data["code"] == "bad_request"
    assert fake.calls == before, "缺 enabled 时不应调用 set_enabled"

    status, _headers, data, _raw = _http_request(
        web_server, "PUT", "/api/video/auto", token=web_server.token, body={}
    )
    assert status == 405 and data["code"] == "bad_request"


def test_server_video_auto_post_errors(web_server, monkeypatch):
    """None/旧版无 set_enabled/set 抛异常 → 503；status 异常不影响开关 200。"""
    old = web_server.context.video_auto
    try:
        monkeypatch.setattr(web_server.context, "video_auto", None)
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/video/auto", token=web_server.token,
            body={"enabled": True},
        )
        assert status == 503 and "实验性视频缓存未启用" in data["error"]

        class _NoSetEnabled:
            def status(self):
                return {"enabled": False}

        monkeypatch.setattr(web_server.context, "video_auto", _NoSetEnabled())
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/video/auto", token=web_server.token,
            body={"enabled": True},
        )
        assert status == 503 and "webui/video_auto.py" in data["error"]

        monkeypatch.setattr(
            web_server.context, "video_auto",
            _FakeVideoAuto(set_exc=RuntimeError("切窗口失败")),
        )
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/video/auto", token=web_server.token,
            body={"enabled": True},
        )
        assert status == 503 and data["code"] == "server_error"
        assert "设置实验性视频缓存失败" in data["error"]
        assert "切窗口失败" in data["error"]

        # set_enabled 成功但 status() 抛异常：仍 200，enabled 按开关值兜底
        monkeypatch.setattr(
            web_server.context, "video_auto",
            _FakeVideoAuto(status_exc=RuntimeError("状态抖动"),
                           set_result={}),
        )
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/video/auto", token=web_server.token,
            body={"enabled": True},
        )
        assert status == 200 and data["ok"] is True
        assert data["enabled"] is True
        assert data["status"] == {}
    finally:
        web_server.context.video_auto = old


# ---------------------------------------------------------------------------
# 8081 已移除的 AGI core 旧链路：页面/API 一律显式 410 Gone（不再 skip）
# ---------------------------------------------------------------------------
_AGI_CORE_GONE = {
    "error": "agi_core_removed",
    "hint": "8081 旧接口已移除，请用 802 控制台",
}


@pytest.mark.parametrize("path", ["/agi/core", "/agi/core.html"])
def test_server_agi_core_pages_gone(web_server, path):
    """旧 /agi/core 页面已移除：410 + agi_core_removed，不再返回 legacy HTML。"""
    status, headers, data, raw = _http_request(web_server, "GET", path)
    assert status == 410
    assert data == _AGI_CORE_GONE
    assert "application/json" in headers.get("content-type", "")
    assert headers.get("cache-control") == "no-store"
    assert b"<html" not in raw.lower()

    status, _headers, _data, raw = _http_request(web_server, "HEAD", path)
    assert status == 410 and raw == b""


@pytest.mark.parametrize("path", ["/agi", "/agi.html"])
def test_server_legacy_agi_page_still_served(web_server, path):
    """旧 /agi 草稿页仍保留（LEGACY AGI 页，不属于 agi.core）；只验兼容与令牌注入。"""
    status, _headers, _data, raw = _http_request(web_server, "GET", path)
    assert status == 200 and web_server.token in raw.decode("utf-8")


_AGI_CORE_REMOVED_ROUTES = [
    ("GET", "/api/agi/core"),
    ("GET", "/api/agi/core/status"),
    ("POST", "/api/agi/core/status"),
    ("GET", "/api/agi/core/drafts"),
    ("POST", "/api/agi/core/cycle"),
    ("POST", "/api/agi/core/feedback"),
    ("GET", "/api/agi/core/memories"),
    ("POST", "/api/agi/core/memories/approve"),
    ("POST", "/api/agi/core/memories/reject"),
    ("GET", "/api/agi/core/profile"),
    ("GET", "/api/agi/core/persona"),
    ("GET", "/api/agi/core/persona/proposals"),
    ("POST", "/api/agi/core/persona/promote"),
    ("POST", "/api/agi/core/persona/reject"),
    ("GET", "/api/agi/core/knowledge"),
    ("GET", "/api/agi/core/knowledge/proposals"),
    ("POST", "/api/agi/core/knowledge/promote"),
    ("POST", "/api/agi/core/knowledge/reject"),
    ("GET", "/api/agi/core/consent"),
    ("POST", "/api/agi/core/consent"),
    ("GET", "/api/agi/core/acceptance"),
    ("POST", "/api/agi/core/acceptance/demo"),
]


@pytest.mark.parametrize("method,path", _AGI_CORE_REMOVED_ROUTES)
def test_server_agi_core_api_gone(web_server, method, path):
    """旧 /api/agi/core/* 已移除：带不带令牌、方法对不对都直接 410，不再 401/405/503。"""
    body = {} if method == "POST" else None
    for token in (None, web_server.token):
        status, _headers, data, _raw = _http_request(
            web_server, method, path, body=body, token=token
        )
        assert status == 410, (method, path, token)
        assert data == _AGI_CORE_GONE, (method, path, token)


def _sse_read_event(resp):
    """从 SSE 响应读取一条事件，返回 ``(event_type, data_text)``；流结束返回 None。"""
    event_type: str | None = None
    data_lines: list[str] = []
    while True:
        line = resp.fp.readline()
        if not line:
            return (event_type, "\n".join(data_lines)) if (event_type or data_lines) else None
        text = line.decode("utf-8", "replace").rstrip("\r\n")
        if not text:
            if event_type is not None or data_lines:
                return event_type, "\n".join(data_lines)
            continue
        if text.startswith(":"):
            continue
        if text.startswith("event:"):
            event_type = text[6:].strip()
        elif text.startswith("data:"):
            data_lines.append(text[5:].strip())


def test_server_sse_stream(web_server):
    """SSE：先收 hello，再收 status/message；无令牌 401；关闭连接不挂测试。"""
    conn = http.client.HTTPConnection("127.0.0.1", web_server.port, timeout=8)
    try:
        conn.request("GET", "/api/events")
        response = conn.getresponse()
        assert response.status == 401
        response.read()
    finally:
        conn.close()

    conn = http.client.HTTPConnection("127.0.0.1", web_server.port, timeout=10)
    response = None
    try:
        conn.request(
            "GET", "/api/events", headers={"X-WXBOX-Token": web_server.token}
        )
        response = conn.getresponse()
        assert response.status == 200
        assert "text/event-stream" in (response.getheader("Content-Type") or "")

        hello = _sse_read_event(response)
        assert hello is not None, "SSE 没有先发 hello"
        event_type, data_text = hello
        assert event_type == "hello"
        payload = json.loads(data_text)
        assert payload["type"] == "hello"
        assert payload["data"]["version"] == "1.0.0"

        # 注入一条 moments 事件，验证 server 会把任意 bridge 事件原样转发成 SSE
        web_server.bridge.broadcast(
            {"type": "moments", "data": {"ok": True, "count": 0, "synced_at": None,
                                          "error": None, "account": {}}}
        )

        seen = {"realtime": False, "moments": False}
        samples: dict[str, dict] = {}
        deadline = time.time() + 8
        while time.time() < deadline and not all(seen.values()):
            item = _sse_read_event(response)
            if item is None:
                break
            event_type, data_text = item
            payload = json.loads(data_text)
            assert payload["type"] == event_type
            assert "data" in payload
            assert isinstance(payload.get("ts"), (int, float)), "事件应带 ts 信封字段"
            if event_type in ("status", "message", "friends", "log"):
                seen["realtime"] = True
                samples["realtime"] = payload
            elif event_type == "moments":
                seen["moments"] = True
                samples["moments"] = payload
        assert seen["realtime"], "8 秒内没有收到 status/message 等实时事件"
        assert seen["moments"], "SSE 没有转发 moments 事件"
        assert samples["realtime"]["data"], "实时事件应带 data"
        assert samples["moments"]["data"]["ok"] is True
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001 - 清理阶段忽略
                pass
        conn.close()


def _history_items() -> list[dict]:
    """构造两条历史聊天 Message dict（只覆盖接口关心的字段）。"""
    now = time.time()
    return [
        {
            "id": "h-1", "friend": "白云一片去悠悠", "sender": "friend",
            "sender_label": "对方", "kind": "text", "kind_label": "文字",
            "text": "这是历史消息一", "media_path": "", "thumb_path": "",
            "extra": {"db_local_id": 1, "history": True}, "ts": now - 60,
            "time_text": "10:00:00",
        },
        {
            "id": "h-2", "friend": "白云一片去悠悠", "sender": "self",
            "sender_label": "我", "kind": "text", "kind_label": "文字",
            "text": "这是历史消息二", "media_path": "", "thumb_path": "",
            "extra": {"db_local_id": 2, "history": True}, "ts": now - 30,
            "time_text": "10:00:30",
        },
    ]


class _EchoHistoryService:
    """按 limit 切片返回历史消息的假 HistoryService（不读微信数据库）。"""

    def __init__(self, items: list[dict] | None = None):
        self.items = list(items if items is not None else _history_items())
        self.calls: list[tuple[str, int]] = []

    def load(self, friend: str, limit: int) -> dict:
        self.calls.append((str(friend), int(limit)))
        selected = self.items if int(limit) == 0 else self.items[-int(limit):]
        return {
            "ok": True,
            "friend": str(friend),
            "requested": "all" if int(limit) == 0 else int(limit),
            "loaded": len(selected),
            "available": len(self.items),
            "notice": None,
            "items": [dict(item) for item in selected],
        }

    def close(self) -> None:
        pass


def test_server_history_load_success(web_server):
    """POST /api/history/load：成功返回结构、items 进 bridge 内存、bootstrap 标记可用。"""
    fake = _EchoHistoryService()
    old = web_server.context.history
    web_server.context.history = fake
    try:
        status, _headers, boot, _raw = _http_request(
            web_server, "GET", "/api/bootstrap", token=web_server.token
        )
        assert status == 200 and boot["history"]["available"] is True

        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/history/load", token=web_server.token,
            body={"friend": "白云一片去悠悠", "limit": 5},
        )
        assert status == 200 and data["ok"] is True
        assert data["friend"] == "白云一片去悠悠"
        assert data["requested"] == 5
        assert data["loaded"] == 2 and data["available"] == 2
        assert data["notice"] is None
        assert [item["id"] for item in data["items"]] == ["h-1", "h-2"]
        assert fake.calls == [("白云一片去悠悠", 5)]

        # 历史消息应进入 bridge 内存；bridge 会把带 db_local_id 的 id 统一成 db_<sha1>
        # （API 的 items 保持原始 id 不变，方便 fake/旧版兼容）
        from webui.wechat_text import db_message_id

        expected_ids = {
            db_message_id("白云一片去悠悠", 1),
            db_message_id("白云一片去悠悠", 2),
        }
        snapshot = web_server.bridge.snapshot()
        stored = [
            message for message in snapshot["messages"]
            if str(message.get("id") or "") in expected_ids
        ]
        assert len(stored) == 2, "历史消息没有写入 bridge 内存"
        assert all(str(message["id"]).startswith("db_") for message in stored)
        assert {message["text"] for message in stored} == {
            "这是历史消息一", "这是历史消息二",
        }

        # limit=0（全部）与缺省 limit=20
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/history/load", token=web_server.token,
            body={"friend": "白云一片去悠悠", "limit": 0},
        )
        assert status == 200 and data["requested"] == "all"
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/history/load", token=web_server.token,
            body={"friend": "白云一片去悠悠"},
        )
        assert status == 200 and data["requested"] == 20
        assert fake.calls[-2:] == [("白云一片去悠悠", 0), ("白云一片去悠悠", 20)]

        # 重复同步历史不应在 bridge 里出现重复消息（统一 db_ id 去重）
        repeated = [
            message for message in web_server.bridge.snapshot()["messages"]
            if message.get("text") in ("这是历史消息一", "这是历史消息二")
        ]
        assert len(repeated) == 2, f"重复同步历史出现了重复消息：{len(repeated)}"
    finally:
        web_server.context.history = old


def test_server_history_load_validation_and_error_mapping(web_server):
    """friend/limit 校验 400；HistoryService 的 code 映射 400/404/503；异常 400/500。"""
    fake = _EchoHistoryService()
    old = web_server.context.history
    web_server.context.history = fake
    try:
        status, _headers, _data, _raw = _http_request(
            web_server, "POST", "/api/history/load",
            body={"friend": "白云一片去悠悠", "limit": 5},
        )
        assert status == 401, "历史接口需要令牌"

        for body in (
            {"friend": "", "limit": 5},
            {"friend": "白云一片去悠悠", "limit": 7},
            {"friend": "白云一片去悠悠", "limit": "abc"},
            {"friend": "白云一片去悠悠", "limit": -1},
        ):
            status, _headers, data, _raw = _http_request(
                web_server, "POST", "/api/history/load", token=web_server.token, body=body
            )
            assert status == 400, body
            assert data["code"] == "bad_request"
        assert fake.calls == [], "参数非法时不应调用 HistoryService"

        for raw_code, expected_status, expected_code in (
            ("bad_request", 400, "bad_request"),
            ("not_found", 404, "not_found"),
            ("history_error", 503, "server_error"),
        ):
            class _FailingHistory:
                def __init__(self, code):
                    self.code = code

                def load(self, friend, limit):
                    return {"ok": False, "error": "历史读取失败（测试）", "code": self.code}

            web_server.context.history = _FailingHistory(raw_code)
            status, _headers, data, _raw = _http_request(
                web_server, "POST", "/api/history/load", token=web_server.token,
                body={"friend": "白云一片去悠悠", "limit": 5},
            )
            assert status == expected_status, raw_code
            assert data["code"] == expected_code
            assert data["ok"] is False

        class _BoomHistory:
            def __init__(self, exc):
                self.exc = exc

            def load(self, friend, limit):
                raise self.exc

        web_server.context.history = _BoomHistory(ValueError("参数错误"))
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/history/load", token=web_server.token,
            body={"friend": "白云一片去悠悠", "limit": 5},
        )
        assert status == 400 and data["code"] == "bad_request"

        web_server.context.history = _BoomHistory(RuntimeError("读取失败"))
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/history/load", token=web_server.token,
            body={"friend": "白云一片去悠悠", "limit": 5},
        )
        assert status == 500 and data["code"] == "server_error"
    finally:
        web_server.context.history = old


def test_server_history_load_without_service(web_server):
    """history=None 时 /api/history/load 返回 503，bootstrap.history.available=false。"""
    status, _headers, boot, _raw = _http_request(
        web_server, "GET", "/api/bootstrap", token=web_server.token
    )
    assert boot["history"]["available"] is False
    status, _headers, data, _raw = _http_request(
        web_server, "POST", "/api/history/load", token=web_server.token,
        body={"friend": "白云一片去悠悠", "limit": 5},
    )
    assert status == 503 and data["code"] == "server_error"


def test_server_history_load_notice(web_server):
    """历史条数不足时原样回传 notice（前端 toast 用）。"""
    class _ShortHistory:
        def load(self, friend, limit):
            return {
                "ok": True, "friend": friend, "requested": 50, "loaded": 2,
                "available": 2,
                "notice": f"「{friend}」在本机只缓存了 2 条聊天记录，不足 50 条，已同步全部 2 条",
                "items": _history_items(),
            }

    old = web_server.context.history
    web_server.context.history = _ShortHistory()
    try:
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/history/load", token=web_server.token,
            body={"friend": "白云一片去悠悠", "limit": 50},
        )
        assert status == 200
        assert data["loaded"] == 2 and data["available"] == 2
        assert "不足 50 条" in data["notice"]
    finally:
        web_server.context.history = old


class _FakeGroupsAPI:
    """服务端群聊接口测试替身（GroupsService.list 的离线版本）。"""

    def __init__(self, groups: list[dict] | None = None, *, ok: bool = True,
                 exc: BaseException | None = None):
        self.groups_data = list(groups) if groups is not None else [
            {"name": "测试群", "username": "123@chatroom", "kind": "group",
             "member_hint": ""},
            {"name": "产品群", "username": "456@chatroom", "kind": "group",
             "member_hint": "12 人"},
        ]
        self.ok = ok
        self.exc = exc
        self.refresh_calls: list[bool] = []

    def list(self, refresh: bool = False) -> dict:
        self.refresh_calls.append(bool(refresh))
        if self.exc is not None:
            raise self.exc
        if not self.ok:
            return {"ok": False, "error": "群聊列表读取失败（测试）", "count": 0, "groups": []}
        return {
            "ok": True,
            "count": len(self.groups_data),
            "groups": [dict(group) for group in self.groups_data],
            "cached": not refresh,
        }


def test_server_groups_success(web_server):
    """/api/groups：令牌、返回结构、refresh 参数、bootstrap 可用性、群名监听。"""
    status, _headers, data, _raw = _http_request(web_server, "GET", "/api/groups")
    assert status == 401, "群聊接口需要令牌"

    fake = _FakeGroupsAPI()
    old = web_server.context.groups
    web_server.context.groups = fake
    try:
        status, _headers, data, _raw = _http_request(
            web_server, "GET", "/api/groups", token=web_server.token
        )
        assert status == 200 and data["ok"] is True
        assert data["count"] == 2
        assert data["cached"] is True, "无 refresh 时应返回缓存标记"
        group = data["groups"][0]
        assert {"name", "username", "kind", "member_hint"} <= set(group)
        assert group["kind"] == "group"
        assert group["name"] == "测试群"
        assert group["username"] == "123@chatroom"
        assert fake.refresh_calls[-1] is False

        # refresh 接受 1/true/yes/on/y（大小写不敏感），其它视为 false
        for value in ("1", "true", "yes", "on", "y", "TRUE", "Yes"):
            status, _headers, data, _raw = _http_request(
                web_server, "GET", f"/api/groups?refresh={value}",
                token=web_server.token,
            )
            assert status == 200
            assert fake.refresh_calls[-1] is True, value
        for value in ("0", "false", "abc", ""):
            status, _headers, data, _raw = _http_request(
                web_server, "GET", f"/api/groups?refresh={value}",
                token=web_server.token,
            )
            assert status == 200
            assert fake.refresh_calls[-1] is False, value

        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/groups", token=web_server.token, body={}
        )
        assert status == 405, "群聊列表只接受 GET"

        status, _headers, boot, _raw = _http_request(
            web_server, "GET", "/api/bootstrap", token=web_server.token
        )
        assert boot["groups"]["available"] is True

        # 群名可直接用于监听（server 只透传）
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/monitored", token=web_server.token,
            body={"names": ["测试群"]},
        )
        assert status == 200 and data["monitoring"] == ["测试群"]
        _http_request(
            web_server, "POST", "/api/monitored", token=web_server.token,
            body={"names": []},
        )
    finally:
        web_server.context.groups = old


def test_server_groups_unavailable_and_failures(web_server):
    """groups=None / list 抛异常 / ok:false 均 503 可读错误，且不影响其它接口。"""
    status, _headers, boot, _raw = _http_request(
        web_server, "GET", "/api/bootstrap", token=web_server.token
    )
    assert boot["groups"]["available"] is False, "默认上下文未注入群聊服务"

    status, _headers, data, _raw = _http_request(
        web_server, "GET", "/api/groups", token=web_server.token
    )
    assert status == 503 and data["code"] == "server_error"
    assert "群聊列表不可用" in data["error"]

    old = web_server.context.groups
    try:
        web_server.context.groups = _FakeGroupsAPI(exc=RuntimeError("contact.db 被占用"))
        status, _headers, data, _raw = _http_request(
            web_server, "GET", "/api/groups", token=web_server.token
        )
        assert status == 503 and data["code"] == "server_error"
        assert "加载群聊失败" in data["error"] or "contact.db" in data["error"]

        web_server.context.groups = _FakeGroupsAPI(ok=False)
        status, _headers, data, _raw = _http_request(
            web_server, "GET", "/api/groups", token=web_server.token
        )
        assert status == 503
        assert "群聊列表读取失败（测试）" in data["error"]

        # 群聊失败不影响其它接口
        status, _headers, _data, _raw = _http_request(web_server, "GET", "/api/health")
        assert status == 200
        status, _headers, moments_data, _raw = _http_request(
            web_server, "GET", "/api/moments", token=web_server.token
        )
        assert status == 200 and moments_data["ok"] is True
    finally:
        web_server.context.groups = old


class _FakeContactsAPI:
    """服务端联系人接口 V4 测试替身（list(refresh, include_all)）。"""

    def __init__(self, *, ok: bool = True, exc: BaseException | None = None,
                 payload: dict | None = None):
        self.ok = ok
        self.exc = exc
        self.calls: list[tuple[bool, bool]] = []
        self.payload = dict(payload or {
            "ok": True,
            "include_all": False,
            "friends": [
                {"name": "张三", "username": "wxid_zs", "kind": "friend",
                 "initial": "Z", "has_history": True},
                {"name": "李四", "username": "wxid_ls", "kind": "friend",
                 "initial": "L", "has_history": True},
            ],
            "groups": [
                {"name": "测试群", "username": "123@chatroom", "kind": "group",
                 "initial": "C", "has_history": True},
            ],
            "count": 3,
            "friend_count": 2,
            "group_count": 1,
            "initials": {"张三": "Z", "李四": "L", "测试群": "C"},
            "history_names": ["张三", "李四", "测试群"],
            "cached": False,
            "source": "db",
        })

    def list(self, refresh: bool = False, include_all: bool = False) -> dict:
        self.calls.append((bool(refresh), bool(include_all)))
        if self.exc is not None:
            raise self.exc
        if not self.ok:
            return {"ok": False, "error": "联系人读取失败（测试）", "friends": [],
                    "groups": [], "history_names": [], "initials": {}}
        result = dict(self.payload)
        result["include_all"] = bool(include_all)
        return result


class _LegacyContactsAPI:
    """旧版 ContactsService：list() 只接受 refresh（用于测 TypeError 回退）。"""

    def __init__(self):
        self.calls: list[bool] = []
        self.payload = {
            "ok": True,
            "friends": [{"name": "张三", "username": "wxid_zs", "kind": "friend"}],
            "groups": [],
        }

    def list(self, refresh: bool = False) -> dict:
        self.calls.append(bool(refresh))
        return dict(self.payload)


def test_server_contacts_success(web_server):
    """/api/contacts：令牌、原结构返回、refresh/all 布尔解析、bootstrap 可用性。"""
    status, _headers, data, _raw = _http_request(web_server, "GET", "/api/contacts")
    assert status == 401, "联系人接口需要令牌"

    fake = _FakeContactsAPI()
    old = web_server.context.contacts
    web_server.context.contacts = fake
    try:
        status, _headers, data, _raw = _http_request(
            web_server, "GET", "/api/contacts", token=web_server.token
        )
        assert status == 200 and data["ok"] is True
        assert data["source"] == "db", "service 额外字段应保留"
        assert data["count"] == 3
        assert data["friend_count"] == 2 and data["group_count"] == 1
        assert [friend["name"] for friend in data["friends"]] == ["张三", "李四"]
        assert data["groups"][0]["kind"] == "group"
        assert data["initials"]["张三"] == "Z"
        assert set(data["history_names"]) == {"张三", "李四", "测试群"}
        assert data["include_all"] is False
        assert data["cached"] is False
        assert fake.calls[-1] == (False, False)

        # refresh / all 的布尔解析（大小写不敏感）
        for value in ("1", "true", "yes", "on", "y", "TRUE", "Yes"):
            status, _headers, data, _raw = _http_request(
                web_server, "GET", f"/api/contacts?refresh={value}&all={value}",
                token=web_server.token,
            )
            assert status == 200
            assert data["include_all"] is True
            assert fake.calls[-1] == (True, True), value
        for value in ("0", "false", "abc", ""):
            status, _headers, data, _raw = _http_request(
                web_server, "GET", f"/api/contacts?refresh={value}&all={value}",
                token=web_server.token,
            )
            assert status == 200
            assert data["include_all"] is False
            assert fake.calls[-1] == (False, False), value

        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/contacts", token=web_server.token, body={}
        )
        assert status == 405 and data["code"] == "bad_request"

        status, _headers, boot, _raw = _http_request(
            web_server, "GET", "/api/bootstrap", token=web_server.token
        )
        assert boot["contacts"]["available"] is True
    finally:
        web_server.context.contacts = old


def test_server_contacts_fills_missing_fields(web_server):
    """server 只补缺失字段：最小 payload 也能返回完整结构（不出现 undefined）。"""
    fake = _FakeContactsAPI(
        payload={"ok": True, "friends": [{"name": "张三"}]}
    )
    old = web_server.context.contacts
    web_server.context.contacts = fake
    try:
        status, _headers, data, _raw = _http_request(
            web_server, "GET", "/api/contacts", token=web_server.token
        )
        assert status == 200
        assert data["ok"] is True
        assert data["groups"] == [] and data["history_names"] == []
        assert data["initials"] == {}
        assert data["friend_count"] == 1 and data["group_count"] == 0
        assert data["count"] == 1
        assert data["include_all"] is False and data["cached"] is False
    finally:
        web_server.context.contacts = old


def test_server_contacts_legacy_list_fallback(web_server):
    """旧版 list(refresh) 不收 include_all：自动 TypeError 回退，仍 200。"""
    legacy = _LegacyContactsAPI()
    old = web_server.context.contacts
    web_server.context.contacts = legacy
    try:
        status, _headers, data, _raw = _http_request(
            web_server, "GET", "/api/contacts?refresh=1&all=1",
            token=web_server.token,
        )
        assert status == 200 and data["ok"] is True
        assert legacy.calls == [True], "回退时仍应把 refresh 传进去"
        assert data["include_all"] is True, "旧版无 include_all 时按 query 兜底"
        assert data["friend_count"] == 1 and data["count"] == 1
    finally:
        web_server.context.contacts = old


def test_server_contacts_unavailable_and_failures(web_server):
    """contacts=None / 异常 / ok:false 均 503 可读错误，且不影响其它接口。"""
    status, _headers, boot, _raw = _http_request(
        web_server, "GET", "/api/bootstrap", token=web_server.token
    )
    assert boot["contacts"]["available"] is False

    status, _headers, data, _raw = _http_request(
        web_server, "GET", "/api/contacts", token=web_server.token
    )
    assert status == 503 and data["code"] == "server_error"
    assert "联系人列表不可用" in data["error"]

    old_contacts = web_server.context.contacts
    old_groups = web_server.context.groups
    try:
        # 注入群聊服务，验证联系人失败时群聊接口仍 200
        web_server.context.groups = _FakeGroupsAPI()
        web_server.context.contacts = _FakeContactsAPI(exc=RuntimeError("contact.db 被占用"))
        status, _headers, data, _raw = _http_request(
            web_server, "GET", "/api/contacts", token=web_server.token
        )
        assert status == 503
        assert "读取联系人失败" in data["error"] and "contact.db" in data["error"]

        web_server.context.contacts = _FakeContactsAPI(ok=False)
        status, _headers, data, _raw = _http_request(
            web_server, "GET", "/api/contacts", token=web_server.token
        )
        assert status == 503
        assert "联系人读取失败（测试）" in data["error"]

        # 失败隔离：health / bootstrap / 群聊仍正常
        status, _headers, _data, _raw = _http_request(web_server, "GET", "/api/health")
        assert status == 200
        status, _headers, boot, _raw = _http_request(
            web_server, "GET", "/api/bootstrap", token=web_server.token
        )
        assert status == 200 and boot["contacts"]["available"] is True
        status, _headers, groups_data, _raw = _http_request(
            web_server, "GET", "/api/groups", token=web_server.token
        )
        assert status == 200 and groups_data["ok"] is True
    finally:
        web_server.context.contacts = old_contacts
        web_server.context.groups = old_groups


def _contacts_refresh_payload(*, account_changed: bool = False,
                              account_wxid: str = "") -> dict:
    """刷新联系人接口的 service 返回样例（好友/群聊 + 账号切换标记）。"""
    return {
        "ok": True,
        "include_all": False,
        "friends": [
            {"name": "张三", "username": "wxid_zs", "kind": "friend",
             "initial": "Z", "has_history": True},
            {"name": "测试群", "username": "123@chatroom", "kind": "group",
             "initial": "C", "has_history": True},
        ],
        "groups": [],
        "count": 2,
        "friend_count": 2,
        "group_count": 0,
        "initials": {"张三": "Z", "测试群": "C"},
        "history_names": ["张三", "测试群"],
        "cached": False,
        "account_changed": account_changed,
        "account_wxid": account_wxid,
    }


class _FakeContactsRefresh(_FakeContactsAPI):
    """V6 刷新接口用假 ContactsService（带 clear_cache）。"""

    def __init__(self, payload: dict | None = None, *, ok: bool = True,
                 exc: BaseException | None = None):
        super().__init__(ok=ok, exc=exc,
                         payload=payload if payload is not None else _contacts_refresh_payload())
        self.clear_cache_calls = 0

    def clear_cache(self) -> None:
        self.clear_cache_calls += 1


class _FakeGroupsCache:
    """只有 clear_cache 的假群聊服务。"""

    def __init__(self) -> None:
        self.clear_cache_calls = 0

    def clear_cache(self) -> None:
        self.clear_cache_calls += 1


def _drain_events(event_queue, *, timeout: float = 3.0) -> list[dict]:
    """在 timeout 内持续收集事件，直到队列短暂空闲。"""
    events: list[dict] = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            events.append(event_queue.get(timeout=0.1))
        except queue.Empty:
            if events:
                break
    return events


def test_server_contacts_refresh_no_account_change(web_server, monkeypatch):
    """POST /api/contacts/refresh（未切号）：替换好友列表，消息/监听不动，不写配置。"""
    fake = _FakeContactsRefresh()
    monkeypatch.setattr(web_server.context, "contacts", fake)
    reset_calls: list[str] = []
    monkeypatch.setattr(
        web_server.bridge, "reset_backend_db",
        lambda account_wxid="": reset_calls.append(str(account_wxid)),
    )
    save_calls: list[dict] = []
    monkeypatch.setattr("wx_core.config.save", lambda cfg: save_calls.append(dict(cfg)))

    status, _headers, _data, _raw = _http_request(
        web_server, "POST", "/api/contacts/refresh", body={}
    )
    assert status == 401, "刷新联系人需要令牌"

    # 先设置一个监听名单，确认未切号时不会被清空
    _http_request(
        web_server, "POST", "/api/monitored", token=web_server.token,
        body={"names": ["张三"]},
    )
    messages_before = web_server.bridge.snapshot()["state"]["message_total"]

    status, _headers, data, _raw = _http_request(
        web_server, "POST", "/api/contacts/refresh", token=web_server.token, body={}
    )
    assert status == 200 and data["ok"] is True
    assert data["account_changed"] is False
    assert data["replaced_friends"] == 2
    assert data["monitoring"] == []
    assert data["contacts"]["account_changed"] is False
    assert fake.calls == [(True, False)], "刷新接口应以 refresh=True 调 contacts.list"

    friends = web_server.bridge.snapshot()["friends"]
    names = {friend["name"] for friend in friends}
    assert {"张三", "测试群"} <= names, "新联系人应替换进 bridge 列表"
    assert any(friend.get("wxid") == "wxid_zs" for friend in friends)

    assert web_server.bridge.snapshot()["state"]["message_total"] >= messages_before, (
        "未切号刷新不应清空消息"
    )
    assert web_server.bridge.snapshot()["state"]["monitoring"] == ["张三"], (
        "未切号刷新不应清空监听名单"
    )
    assert reset_calls == [], "未切号不应重置后端数据库桥"
    assert save_calls == [], "未切号不应写 config.json"

    _http_request(
        web_server, "POST", "/api/monitored", token=web_server.token, body={"names": []}
    )


def test_server_contacts_refresh_account_changed(web_server, monkeypatch):
    """切号刷新：清空消息+监听、重置 DB 桥、更新 config（save 次数/内容/项目文件未改）。"""
    fake = _FakeContactsRefresh(
        _contacts_refresh_payload(account_changed=True, account_wxid="wxid_new")
    )
    monkeypatch.setattr(web_server.context, "contacts", fake)
    reset_calls: list[str] = []
    monkeypatch.setattr(
        web_server.bridge, "reset_backend_db",
        lambda account_wxid="": reset_calls.append(str(account_wxid)),
    )
    save_calls: list[dict] = []
    monkeypatch.setattr("wx_core.config.save", lambda cfg: save_calls.append(dict(cfg)))
    # 非空配置才会触发落盘（空配置 server 只改内存、跳过 save）
    monkeypatch.setattr(
        web_server.backend, "config",
        {"friends_cache": ["旧好友"], "image_wxid": "wxid_old"},
    )
    config_file = ROOT / "config.json"
    config_before = config_file.read_bytes() if config_file.is_file() else b""

    # 停掉假后端，避免清空后立刻又收到新消息导致断言不稳定
    _http_request(
        web_server, "POST", "/api/backend/stop", token=web_server.token, body={}
    )
    assert _wait_until(
        lambda: web_server.bridge.snapshot()["state"].get("running") is False
    )
    _http_request(
        web_server, "POST", "/api/monitored", token=web_server.token,
        body={"names": ["张三"]},
    )
    messages_before = len(web_server.bridge.snapshot()["messages"])
    assert messages_before > 0
    event_queue, sub_id = web_server.bridge.subscribe()
    try:
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/contacts/refresh", token=web_server.token, body={}
        )
        assert status == 200 and data["ok"] is True
        assert data["account_changed"] is True
        assert data["replaced_friends"] == 2
        assert data["monitoring"] == [], "切号后监听名单应清空"

        snapshot = web_server.bridge.snapshot()
        assert snapshot["messages"] == [], "切号后旧账号消息应清空"
        assert snapshot["state"]["message_total"] == 0
        assert snapshot["state"]["monitoring"] == []
        assert reset_calls == ["wxid_new"], "切号应重置后端 DB 桥并带新 wxid"

        backend_config = web_server.backend.config
        assert backend_config["friends_cache"] == ["张三", "测试群"]
        assert backend_config["image_wxid"] == "wxid_new"
        assert len(save_calls) == 1, "非空配置应落盘一次"
        assert save_calls[0]["friends_cache"] == ["张三", "测试群"]
        assert save_calls[0]["image_wxid"] == "wxid_new"

        events = _drain_events(event_queue)
        cleared_events = [e for e in events if e.get("type") == "messages_cleared"]
        assert cleared_events, "切号应广播 messages_cleared 事件"
        assert cleared_events[0]["data"]["cleared"] == messages_before
        assert any(e.get("type") == "friends" for e in events)

        config_after = config_file.read_bytes() if config_file.is_file() else b""
        assert config_after == config_before, "测试不应改写项目根 config.json"
    finally:
        web_server.bridge.unsubscribe(sub_id)
        _http_request(
            web_server, "POST", "/api/backend/start", token=web_server.token, body={}
        )
        assert _wait_until(
            lambda: web_server.bridge.snapshot()["state"].get("running") is True
        )
        _http_request(
            web_server, "POST", "/api/monitored", token=web_server.token,
            body={"names": []},
        )


def test_server_contacts_refresh_empty_config_skips_save(web_server, monkeypatch):
    """空配置（测试/演示后端）切号只改内存 config，不调用 wx_core.config.save。"""
    fake = _FakeContactsRefresh(
        _contacts_refresh_payload(account_changed=True, account_wxid="wxid_new")
    )
    monkeypatch.setattr(web_server.context, "contacts", fake)
    monkeypatch.setattr(web_server.bridge, "reset_backend_db", lambda account_wxid="": None)
    save_calls: list[dict] = []
    monkeypatch.setattr("wx_core.config.save", lambda cfg: save_calls.append(dict(cfg)))
    monkeypatch.setattr(web_server.backend, "config", {})

    status, _headers, data, _raw = _http_request(
        web_server, "POST", "/api/contacts/refresh", token=web_server.token, body={}
    )
    assert status == 200 and data["account_changed"] is True
    assert save_calls == [], "空配置不应落盘（避免覆盖项目 config.json）"
    assert web_server.backend.config["friends_cache"] == ["张三", "测试群"]
    assert web_server.backend.config["image_wxid"] == "wxid_new"


def test_server_contacts_refresh_errors(web_server, monkeypatch):
    """刷新接口：contacts=None / 异常 / ok:false 均 503；401、405；失败隔离。"""
    status, _headers, data, _raw = _http_request(
        web_server, "POST", "/api/contacts/refresh", token=web_server.token, body={}
    )
    assert status == 503, "默认上下文未注入 ContactsService"
    assert "联系人列表不可用" in data["error"]

    old_contacts = web_server.context.contacts
    try:
        monkeypatch.setattr(
            web_server.context, "contacts",
            _FakeContactsRefresh(exc=RuntimeError("contact.db 被占用")),
        )
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/contacts/refresh", token=web_server.token, body={}
        )
        assert status == 503 and "刷新联系人失败" in data["error"]
        assert "contact.db" in data["error"]

        monkeypatch.setattr(
            web_server.context, "contacts",
            _FakeContactsRefresh(ok=False),
        )
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/contacts/refresh", token=web_server.token, body={}
        )
        assert status == 503
        assert "联系人读取失败（测试）" in data["error"]

        status, _headers, data, _raw = _http_request(
            web_server, "GET", "/api/contacts/refresh", token=web_server.token
        )
        assert status == 405 and data["code"] == "bad_request"

        # 失败隔离
        status, _headers, _data, _raw = _http_request(web_server, "GET", "/api/health")
        assert status == 200
        status, _headers, boot, _raw = _http_request(
            web_server, "GET", "/api/bootstrap", token=web_server.token
        )
        assert status == 200 and boot["contacts"]["available"] is True
    finally:
        web_server.context.contacts = old_contacts


def test_server_contacts_clear(web_server, monkeypatch):
    """POST /api/contacts/clear：清 contacts/groups 缓存、好友列表、config.friends_cache。"""
    contacts = _FakeContactsRefresh()
    groups = _FakeGroupsCache()
    monkeypatch.setattr(web_server.context, "contacts", contacts)
    monkeypatch.setattr(web_server.context, "groups", groups)
    clear_calls: list[int] = []
    monkeypatch.setattr(
        web_server.bridge, "clear_friends",
        lambda: clear_calls.append(1) or 7,
    )
    save_calls: list[dict] = []
    monkeypatch.setattr("wx_core.config.save", lambda cfg: save_calls.append(dict(cfg)))
    monkeypatch.setattr(
        web_server.backend, "config",
        {"friends_cache": ["张三"], "image_wxid": "wxid_old"},
    )

    status, _headers, _data, _raw = _http_request(
        web_server, "POST", "/api/contacts/clear", body={}
    )
    assert status == 401

    status, _headers, data, _raw = _http_request(
        web_server, "POST", "/api/contacts/clear", token=web_server.token, body={}
    )
    assert status == 200 and data["ok"] is True and data["cleared"] == 7
    assert contacts.clear_cache_calls == 1
    assert groups.clear_cache_calls == 1
    assert clear_calls == [1]
    assert web_server.backend.config["friends_cache"] == []
    assert len(save_calls) == 1 and save_calls[0]["friends_cache"] == []

    status, _headers, data, _raw = _http_request(
        web_server, "GET", "/api/contacts/clear", token=web_server.token
    )
    assert status == 405 and data["code"] == "bad_request"

    status, _headers, _data, _raw = _http_request(web_server, "GET", "/api/health")
    assert status == 200


def test_server_contacts_clear_errors(web_server, monkeypatch):
    """清空接口：contacts=None 503；clear_cache 抛异常 503；groups=None 也能工作。"""
    old_contacts = web_server.context.contacts
    try:
        monkeypatch.setattr(web_server.context, "contacts", None)
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/contacts/clear", token=web_server.token, body={}
        )
        assert status == 503 and "联系人列表不可用" in data["error"]

        class _BoomClear(_FakeContactsRefresh):
            def clear_cache(self) -> None:
                raise RuntimeError("缓存清理失败")

        monkeypatch.setattr(web_server.context, "contacts", _BoomClear())
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/contacts/clear", token=web_server.token, body={}
        )
        assert status == 503 and "清空联系人缓存失败" in data["error"]

        # 没有 groups 服务时仍可清空
        monkeypatch.setattr(web_server.context, "contacts", _FakeContactsRefresh())
        monkeypatch.setattr(web_server.context, "groups", None)
        monkeypatch.setattr(web_server.bridge, "clear_friends", lambda: 0)
        monkeypatch.setattr("wx_core.config.save", lambda cfg: None)
        monkeypatch.setattr(web_server.backend, "config", {})
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/contacts/clear", token=web_server.token, body={}
        )
        assert status == 200 and data["cleared"] == 0
    finally:
        web_server.context.contacts = old_contacts


def _make_video_file(tmp_path, size: int = 1000):
    """造一个固定内容的视频文件（只用于测试流式读取，不是真视频）。"""
    data = bytes(index % 251 for index in range(size))
    path = tmp_path / "clip.mp4"
    path.write_bytes(data)
    return path, data


class _FakeVideos:
    """V7 视频接口测试替身（不读微信 WCDB/msg/video）。"""

    def __init__(self, path=None, mime: str = "video/mp4",
                 info: dict | None = None, exc: BaseException | None = None):
        self.path = path
        self.mime = mime
        self.info = info
        self.exc = exc
        self.calls: list[tuple[str, int]] = []

    def resolve(self, friend: str, local_id: int):
        self.calls.append((str(friend), int(local_id)))
        if self.exc is not None:
            raise self.exc
        if self.info is not None:
            return dict(self.info)
        if self.path is None:
            return None
        return {"path": self.path, "mime": self.mime}


def test_server_video_full_and_head(web_server, tmp_path, monkeypatch):
    """/api/video：全量 200 头/body、HEAD、HEAD+Range；query/header 两种令牌。"""
    path, data = _make_video_file(tmp_path)
    fake = _FakeVideos(path=path)
    monkeypatch.setattr(web_server.context, "videos", fake)

    status, headers, _data, raw = _http_request(
        web_server, "GET",
        f"/api/video?friend=%E5%BC%A0%E4%B8%89&local_id=42&token={web_server.token}",
    )
    assert status == 200
    assert raw == data
    assert headers["content-type"] == "video/mp4"
    assert headers["content-length"] == str(len(data))
    assert headers["accept-ranges"] == "bytes"
    assert headers["cache-control"] == "no-store"
    assert headers.get("x-content-type-options") == "nosniff"
    assert fake.calls == [("张三", 42)]

    # header 令牌 + HEAD：同头无 body
    status, headers, _data, raw = _http_request(
        web_server, "HEAD", "/api/video?friend=%E5%BC%A0%E4%B8%89&local_id=42",
        token=web_server.token,
    )
    assert status == 200 and raw == b""
    assert headers["content-length"] == str(len(data))
    assert headers["accept-ranges"] == "bytes"

    # HEAD + Range：206 头，无 body
    status, headers, _data, raw = _http_request(
        web_server, "HEAD", "/api/video?friend=%E5%BC%A0%E4%B8%89&local_id=42",
        token=web_server.token, headers={"Range": "bytes=0-99"},
    )
    assert status == 206 and raw == b""
    assert headers["content-range"] == f"bytes 0-99/{len(data)}"
    assert headers["content-length"] == "100"


def test_server_video_range_requests(web_server, tmp_path, monkeypatch):
    """Range 三种写法：bytes=0-99 / bytes=900- / bytes=-50，及越界尾部截断。"""
    path, data = _make_video_file(tmp_path)
    monkeypatch.setattr(web_server.context, "videos", _FakeVideos(path=path))

    cases = [
        ("bytes=0-99", 0, 99),
        ("bytes=900-", 900, 999),
        ("bytes=-50", 950, 999),
        ("bytes=990-5000", 990, 999),  # end 超过总长时截断
    ]
    for range_value, start, end in cases:
        status, headers, _data, raw = _http_request(
            web_server, "GET", f"/api/video?friend=%E5%BC%A0%E4%B8%89&local_id=7&token={web_server.token}",
            headers={"Range": range_value},
        )
        assert status == 206, range_value
        assert headers["content-range"] == f"bytes {start}-{end}/{len(data)}"
        assert headers["content-length"] == str(end - start + 1)
        assert raw == data[start:end + 1], range_value


def test_server_video_range_invalid_416(web_server, tmp_path, monkeypatch):
    """越界/多段/单位错/格式错 → 416 + Content-Range: bytes */total。"""
    path, data = _make_video_file(tmp_path)
    monkeypatch.setattr(web_server.context, "videos", _FakeVideos(path=path))

    for range_value in (
        "bytes=1000-",       # start 越界
        "bytes=0-1,3-4",     # 多段
        "bytes=50-10",       # end < start
        "items=0-1",         # 单位错
        "bytes=abc",         # 格式错
        "bytes=-0",          # 后缀为 0
    ):
        status, headers, data_json, _raw = _http_request(
            web_server, "GET", f"/api/video?friend=%E5%BC%A0%E4%B8%89&local_id=7&token={web_server.token}",
            headers={"Range": range_value},
        )
        assert status == 416, range_value
        assert headers.get("content-range") == f"bytes */{len(data)}", range_value
        assert data_json["ok"] is False

    assert len(data) == 1000


def test_server_video_validation_400_and_401(web_server, tmp_path, monkeypatch):
    """缺参/非法 local_id → 400；无/错令牌 → 401。"""
    path, _data = _make_video_file(tmp_path)
    fake = _FakeVideos(path=path)
    monkeypatch.setattr(web_server.context, "videos", fake)

    status, _headers, _data, _raw = _http_request(
        web_server, "GET", "/api/video?friend=%E5%BC%A0%E4%B8%89&local_id=7"
    )
    assert status == 401, "视频接口需要令牌"

    status, _headers, _data, _raw = _http_request(
        web_server, "GET",
        f"/api/video?friend=%E5%BC%A0%E4%B8%89&local_id=7&token={web_server.token}",
        headers={"X-WXBOX-Token": "wrong"},
    )
    assert status == 401

    for query in (
        "local_id=7",                 # 缺 friend
        "friend=%E5%BC%A0%E4%B8%89",                 # 缺 local_id
        "friend=%E5%BC%A0%E4%B8%89&local_id=abc",    # 非整数
        "friend=%E5%BC%A0%E4%B8%89&local_id=0",      # 非正
        "friend=%E5%BC%A0%E4%B8%89&local_id=-3",
    ):
        status, _headers, data, _raw = _http_request(
            web_server, "GET",
            f"/api/video?{query}&token={web_server.token}",
        )
        assert status == 400, query
        assert data["code"] == "bad_request"
    assert fake.calls == [], "参数非法时不应调用 VideoService.resolve"


def test_server_video_not_found_and_500(web_server, tmp_path, monkeypatch):
    """videos=None / resolve=None / 文件不存在 → 404；resolve 抛异常 → 500 中文错误。"""
    status, _headers, data, _raw = _http_request(
        web_server, "GET",
        f"/api/video?friend=%E5%BC%A0%E4%B8%89&local_id=7&token={web_server.token}",
    )
    assert status == 404 and data["code"] == "not_found"
    assert "视频服务不可用" in data["error"]

    old_videos = web_server.context.videos
    try:
        monkeypatch.setattr(web_server.context, "videos", _FakeVideos(path=None))
        status, _headers, data, _raw = _http_request(
            web_server, "GET",
            f"/api/video?friend=%E5%BC%A0%E4%B8%89&local_id=7&token={web_server.token}",
        )
        assert status == 404 and data["code"] == "not_found"

        monkeypatch.setattr(
            web_server.context, "videos",
            _FakeVideos(path=tmp_path / "missing.mp4"),
        )
        status, _headers, data, _raw = _http_request(
            web_server, "GET",
            f"/api/video?friend=%E5%BC%A0%E4%B8%89&local_id=7&token={web_server.token}",
        )
        assert status == 404 and "视频文件不存在" in data["error"]

        monkeypatch.setattr(
            web_server.context, "videos",
            _FakeVideos(exc=RuntimeError("WCDB 密钥不可用")),
        )
        status, _headers, data, _raw = _http_request(
            web_server, "GET",
            f"/api/video?friend=%E5%BC%A0%E4%B8%89&local_id=7&token={web_server.token}",
        )
        assert status == 500 and data["code"] == "server_error"
        assert "解析视频失败" in data["error"] and "WCDB 密钥不可用" in data["error"]
    finally:
        web_server.context.videos = old_videos


def test_server_video_ignores_url_path_params(web_server, tmp_path, monkeypatch):
    """防穿越：URL 里的 path/filename 被忽略，只按 service 返回的 Path 读。"""
    path, data = _make_video_file(tmp_path)
    fake = _FakeVideos(path=path)
    monkeypatch.setattr(web_server.context, "videos", fake)

    status, headers, _data, raw = _http_request(
        web_server, "GET",
        "/api/video?friend=%E5%BC%A0%E4%B8%89&local_id=9"
        "&path=C:/Windows/win.ini&filename=../../config.json"
        f"&token={web_server.token}",
    )
    assert status == 200 and raw == data
    assert headers["content-type"] == "video/mp4"
    assert fake.calls == [("张三", 9)], "只应把 friend/local_id 传给 service"


def test_server_history_group_items_passthrough(web_server):
    """群聊历史：extra.is_group/sender_name 原样透传；自己消息不带 sender_name。"""
    now = time.time()
    group_items = [
        {
            "id": "gh-1", "friend": "测试群", "sender": "friend", "sender_label": "对方",
            "kind": "text", "kind_label": "文字", "text": "大家好",
            "media_path": "", "thumb_path": "",
            "extra": {"is_group": True, "sender_name": "张三", "history": True},
            "ts": now - 20, "time_text": "19:00:00",
        },
        {
            "id": "gh-2", "friend": "测试群", "sender": "self", "sender_label": "我",
            "kind": "text", "kind_label": "文字", "text": "收到",
            "media_path": "", "thumb_path": "",
            "extra": {"is_group": True, "history": True},
            "ts": now - 10, "time_text": "19:00:10",
        },
    ]

    class _GroupHistory:
        def load(self, friend, limit):
            return {
                "ok": True, "friend": friend, "requested": 20, "loaded": 2,
                "available": 2, "notice": None, "items": [dict(item) for item in group_items],
            }

    old = web_server.context.history
    web_server.context.history = _GroupHistory()
    try:
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/history/load", token=web_server.token,
            body={"friend": "测试群", "limit": 20},
        )
        assert status == 200 and data["friend"] == "测试群"
        assert [item["id"] for item in data["items"]] == ["gh-1", "gh-2"]
        first, second = data["items"]
        assert first["extra"]["is_group"] is True
        assert first["extra"]["sender_name"] == "张三"
        assert second["extra"].get("sender_name") is None, "自己的消息不应带 sender_name"

        snapshot = web_server.bridge.snapshot()
        stored = {message["id"]: message for message in snapshot["messages"]}
        assert stored["gh-1"]["extra"]["sender_name"] == "张三"
        assert stored["gh-1"]["extra"]["is_group"] is True
        assert "sender_name" not in stored["gh-2"]["extra"]
    finally:
        web_server.context.history = old


def test_server_messages_clear(web_server):
    """/api/messages/clear：清空 bridge 内存消息/计数/日志并广播 messages_cleared。"""
    # 先停掉假后端，避免清空后立刻又有新消息进来（结果不确定）
    status, _headers, data, _raw = _http_request(
        web_server, "POST", "/api/backend/stop", token=web_server.token, body={}
    )
    assert status == 200 and data["running"] is False
    assert _wait_until(
        lambda: web_server.bridge.snapshot()["state"].get("running") is False
    )

    snapshot = web_server.bridge.snapshot()
    count_before = len(snapshot["messages"])
    assert count_before > 0, "清空前应已有消息"
    assert snapshot["logs"], "清空前应已有日志"

    event_queue, sub_id = web_server.bridge.subscribe()
    try:
        status, _headers, data, _raw = _http_request(
            web_server, "POST", "/api/messages/clear", token=web_server.token, body={}
        )
        assert status == 200 and data["ok"] is True
        assert data["cleared"] == count_before

        after = web_server.bridge.snapshot()
        assert after["messages"] == [], "消息应被清空"
        assert after["state"]["message_total"] == 0
        assert after["state"]["per_friend_counts"] == {}
        assert after["logs"] == [], "清空聊天时日志也属于临时数据，应一起清空"

        events = []
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                events.append(event_queue.get(timeout=0.1))
            except queue.Empty:
                continue
            if any(e.get("type") == "messages_cleared" for e in events):
                break
        cleared_events = [e for e in events if e.get("type") == "messages_cleared"]
        assert cleared_events, "应广播 messages_cleared 事件"
        assert cleared_events[0]["data"]["cleared"] == count_before
    finally:
        web_server.bridge.unsubscribe(sub_id)
        # 恢复后端，后续 SSE 用例还需要实时事件
        _http_request(
            web_server, "POST", "/api/backend/start", token=web_server.token, body={}
        )
        assert _wait_until(
            lambda: web_server.bridge.snapshot()["state"].get("running") is True
        )


def test_server_messages_clear_without_support(web_server, monkeypatch):
    """旧版 bridge 没有 clear_messages() 时返回 503 而不是 500。"""
    monkeypatch.setattr(web_server.bridge, "clear_messages", None)
    status, _headers, data, _raw = _http_request(
        web_server, "POST", "/api/messages/clear", token=web_server.token, body={}
    )
    assert status == 503 and data["code"] == "server_error"


def test_default_web_port_is_8081():
    """V2：默认端口改为 8081（8080 被其他项目占用）；run_web/web_app 参数一致。"""
    import web_app

    assert web_app.DEFAULT_PORT == 8081
    assert web_app.DEFAULT_PORT != 8080
    args = web_app.build_arg_parser().parse_args([])
    assert args.port == 8081


def test_create_server_reports_port_in_use(web_server):
    """端口被占用时 create_server 抛 OSError（契约 3.4）。"""
    from webui.server import create_server

    with pytest.raises(OSError):
        create_server(web_server.context, "127.0.0.1", web_server.port)


def test_server_error_closes_connection_but_success_keeps_alive(web_server):
    """错误响应显式 Connection: close（防未读 body 串包）；成功响应仍复用连接。"""
    status, headers, _data, _raw = _http_request(
        web_server, "POST", "/api/llm/config",
        body={"model": "deepseek-flash"},  # 故意不带令牌 -> 401
    )
    assert status == 401
    assert headers.get("connection", "").lower() == "close", "错误响应应显式关闭连接"

    conn = http.client.HTTPConnection("127.0.0.1", web_server.port, timeout=8)
    try:
        body = json.dumps({"names": []}, ensure_ascii=False).encode("utf-8")
        conn.request(
            "POST", "/api/monitored", body=body,
            headers={
                "X-WXBOX-Token": web_server.token,
                "Content-Type": "application/json",
            },
        )
        first = conn.getresponse()
        first.read()
        assert first.status == 200
        assert (first.getheader("Connection") or "").lower() != "close", (
            "成功响应不应强制关闭 keep-alive 连接"
        )

        conn.request("GET", "/api/health")
        second = conn.getresponse()
        second.read()
        assert second.status == 200, "同一连接上的第二个请求应正常处理"
    finally:
        conn.close()


# ----------------------------------------------------------------------
# MomentsService V2：注入 runner 的离线单元测试（不依赖 venv/微信/落盘）
# ----------------------------------------------------------------------
def _make_moments_service(tmp_path, runner=None, bridge=None, *, python=None):
    """构造 V2 MomentsService：worker/moments 目录用 tmp 伪造，runner 注入，不碰真实微信。

    V2 起 ``cache_path`` / ``media_dir`` 只是兼容参数，不会创建/写入；这里仍传 tmp 路径，
    方便测试断言「确实没有落盘」。
    """
    import sys

    from webui.moments import MomentsService

    moments_dir = tmp_path / "WechatMoments"
    moments_dir.mkdir(exist_ok=True)
    worker = tmp_path / "webui" / "moments_worker.py"
    worker.parent.mkdir(exist_ok=True)
    worker.write_text("# 测试用假 worker，不会被真正执行", encoding="utf-8")
    return MomentsService(
        ROOT,
        bridge=bridge,
        python=python or sys.executable,
        worker=worker,
        moments_dir=moments_dir,
        media_dir=tmp_path / "media" / "moments",
        cache_path=tmp_path / "cache" / "moments.json",
        runner=runner or (lambda _args: (1, "", "no runner")),
        logger=_quiet_log,
    )


def _jpeg_b64(data: bytes = b"\xff\xd8\xff\xe0fake-jpeg-bytes") -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


def _v2_moment(moment_id: str, create_time: int, *, content: str = "",
               author: str = "张三", media: list[dict] | None = None,
               avatar_key: str = "", **extra) -> dict:
    """构造一条 V2 worker 输出格式的朋友圈（媒体/头像内联 base64）。"""
    item: dict = {
        "id": moment_id,
        "username": f"wxid_{moment_id}",
        "author": author,
        "create_time": create_time,
        "time_text": "10-17 18:51",
        "content": content,
        "location": "",
        "link": None,
        "media": media or [],
        "comments": [],
        "likes": [],
        "raw_comment_count": 0,
    }
    if avatar_key:
        item["avatar_key"] = avatar_key
        item["avatar_mime"] = "image/jpeg"
        item["avatar_b64"] = _jpeg_b64()
    item.update(extra)
    return item


def _v2_sample_moments() -> list[dict]:
    """两条样例朋友圈：李四带正文/位置/分享/评论/点赞/图片/头像，张三只有文字+头像。"""
    return [
        _v2_moment(
            "15034350704629592685",
            1760000000,
            author="李四",
            content="周末去爬山了，风景很好",
            location="杭州·西湖",
            link={"title": "一篇关于爬山的文章", "url": "https://example.com/hike"},
            media=[{
                "type": "image",
                "thumb_key": "t_15034350704629592685_0",
                "thumb_mime": "image/jpeg",
                "thumb_b64": _jpeg_b64(b"\xff\xd8\xff\xe0LI-SI-IMAGE"),
                "width": 1280,
                "height": 990,
            }],
            avatar_key="a_li4",
            comments=[{"author": "张三", "content": "羡慕！"}],
            likes=["张三", "王五"],
            raw_comment_count=1,
        ),
        _v2_moment(
            "15034350704629592686",
            1760000100,
            author="张三",
            content="晚上吃火锅",
            avatar_key="a_zhangsan",
        ),
    ]


def _v2_moments_payload(synced_at: float | None = None,
                        items: list[dict] | None = None) -> dict:
    return {
        "ok": True,
        "account": {"wxid": "wxid_self", "nick_name": "测试号"},
        "synced_at": time.time() if synced_at is None else float(synced_at),
        "moments": _v2_sample_moments() if items is None else items,
    }


def test_moments_initial_status(tmp_path):
    """V2 初始状态：可用、无数据、不是同步中、limit=10、count=0；items/get 为空。"""
    service = _make_moments_service(tmp_path)
    status = service.status()
    assert status["available"] is True
    assert status["reason"] == ""
    assert status["syncing"] is False
    assert status["limit"] == 10, "V2 status 应带同步条数（默认 10）"
    assert status["count"] == 0
    assert status["synced_at"] is None
    assert status["error"] is None
    assert status["account"] == {"wxid": "", "nick_name": ""}
    assert status["python"]
    assert str(service.moments_dir) == status["moments_dir"]
    assert service.items() == []
    assert service.get("不存在") is None
    assert service.media("t_x") is None


def test_moments_sync_async_success(tmp_path):
    """V2 同步：--limit/--inline-media 参数、items 倒序、内存媒体、事件带 limit、不落盘。"""
    bridge = _RecordingBridge()
    calls: list[list[str]] = []

    def runner(args):
        calls.append(list(args))
        return (0, json.dumps(_v2_moments_payload(), ensure_ascii=False), "")

    service = _make_moments_service(tmp_path, runner=runner, bridge=bridge)
    result = service.sync_async()
    assert result == {"ok": True, "syncing": True}
    assert service.wait_sync(5) is True

    status = service.status()
    assert status["syncing"] is False
    assert status["count"] == 2
    assert status["limit"] == 10
    assert status["error"] is None
    assert status["account"]["nick_name"] == "测试号"
    assert status["synced_at"] and status["synced_at"] > 0

    items = service.items()
    assert [item["id"] for item in items] == [
        "15034350704629592686",
        "15034350704629592685",
    ], "朋友圈应按 create_time 倒序"
    assert items[0]["author"] == "张三"
    image_entry = items[1]["media"][0]
    assert image_entry["thumb_key"] == "t_15034350704629592685_0"
    assert image_entry["thumb_mime"] == "image/jpeg"
    assert "thumb_b64" not in image_entry, "API 返回的媒体条目不应带 base64"
    assert "b64" not in json.dumps(items, ensure_ascii=False)

    # V2：图片/头像字节只在内存，media(key) 可取到
    data, mime = service.media("t_15034350704629592685_0")
    assert data == b"\xff\xd8\xff\xe0LI-SI-IMAGE" and mime == "image/jpeg"
    avatar = service.media(items[0]["avatar_key"])
    assert avatar is not None and avatar[1] == "image/jpeg"

    types = [event["type"] for event in bridge.events]
    assert "moments_status" in types and "moments" in types
    moments_event = next(event for event in bridge.events if event["type"] == "moments")
    assert moments_event["data"]["ok"] is True
    assert moments_event["data"]["count"] == 2
    assert moments_event["data"]["limit"] == 10, "SSE moments 事件 data 应带 limit"
    assert moments_event["data"]["error"] is None
    status_events = [e for e in bridge.events if e["type"] == "moments_status"]
    assert status_events[0]["data"]["syncing"] is True
    assert status_events[-1]["data"]["syncing"] is False

    assert calls, "runner 没有被调用"
    args = calls[0]
    assert "--moments-dir" in args
    assert "--limit" in args and args[args.index("--limit") + 1] == "10"
    assert "--inline-media" in args
    assert "--media-out" not in args, "V2 不再写媒体目录"

    # V2：数据只在内存，同步后不产生缓存/媒体文件
    assert not service.cache_path.exists(), "V2 不应写 webui/cache/moments.json"
    assert not service.media_dir.exists(), "V2 不应创建 media/moments/"


def test_moments_sync_limit_normalization(tmp_path):
    """sync limit 只允许 5/10/20，其它值按 10；args/status/事件三处一致。"""
    limits: list[str] = []
    bridge = _RecordingBridge()

    def runner(args):
        limits.append(args[args.index("--limit") + 1])
        return (0, json.dumps(_v2_moments_payload(), ensure_ascii=False), "")

    service = _make_moments_service(tmp_path, runner=runner, bridge=bridge)
    for requested, expected in ((7, 10), (20, 20), (5, 5), ("abc", 10)):
        service.sync_async(requested)
        assert service.wait_sync(5) is True
        assert service.status()["limit"] == expected, requested
        events = [e for e in bridge.events if e["type"] == "moments"]
        assert events[-1]["data"]["limit"] == expected
    assert limits == ["10", "20", "5", "10"]


def test_moments_media_and_clear(tmp_path):
    """V2 clear() 清空 items/媒体/状态；清空后可重新同步。"""
    bridge = _RecordingBridge()
    service = _make_moments_service(
        tmp_path,
        runner=lambda _args: (0, json.dumps(_v2_moments_payload(), ensure_ascii=False), ""),
        bridge=bridge,
    )
    service.sync_async()
    assert service.wait_sync(5) is True
    assert service.media("t_15034350704629592685_0") is not None
    assert service.status()["count"] == 2

    result = service.clear()
    assert result == {"ok": True, "count": 0}
    status = service.status()
    assert status["count"] == 0
    assert status["synced_at"] is None
    assert service.items() == []
    assert service.get("15034350704629592685") is None
    assert service.media("t_15034350704629592685_0") is None, "clear() 应清空内存媒体字节"

    # 清空后可以重新同步
    service.sync_async()
    assert service.wait_sync(5) is True
    assert service.status()["count"] == 2


def test_moments_clear_discards_inflight_sync(tmp_path):
    """同步进行中 clear()：迟到结果应被丢弃，不能把已清空的页面又填满。"""
    bridge = _RecordingBridge()

    def slow_runner(_args):
        time.sleep(0.4)
        return (0, json.dumps(_v2_moments_payload(), ensure_ascii=False), "")

    service = _make_moments_service(tmp_path, runner=slow_runner, bridge=bridge)
    service.sync_async()
    time.sleep(0.1)  # 让后台线程进入 runner
    service.clear()
    assert service.wait_sync(5) is True
    status = service.status()
    assert status["count"] == 0, "clear() 后迟到同步结果不应写入"
    assert status["syncing"] is False
    assert not any(event["type"] == "moments" for event in bridge.events), (
        "被清空丢弃的同步不应广播 moments 完成事件"
    )


def test_moments_memory_only_and_stop_clears(tmp_path):
    """V2 数据只在内存：start() 不自动同步、不落盘；stop() 清空内存。"""
    started_runner_calls: list[list[str]] = []

    def forbidden_runner(args):
        started_runner_calls.append(list(args))
        raise AssertionError("start() 不应自动同步（V2 只能由用户点按钮触发）")

    service = _make_moments_service(tmp_path, runner=forbidden_runner)
    service.start()  # 只做可用性检查
    time.sleep(0.3)
    assert started_runner_calls == []
    assert service.status()["count"] == 0
    assert not service.cache_path.exists()
    assert not service.media_dir.exists()

    synced = _make_moments_service(
        tmp_path,
        runner=lambda _args: (0, json.dumps(_v2_moments_payload(), ensure_ascii=False), ""),
    )
    synced.start()
    synced.sync_async()
    assert synced.wait_sync(5) is True
    assert synced.status()["count"] == 2
    synced.stop()
    assert synced.status()["count"] == 0, "stop() 应清空内存数据（进程退出即消失）"
    assert synced.media("t_15034350704629592685_0") is None


def test_moments_failure_payload_sets_error(tmp_path):
    """worker 返回 ok=false 时记录中文 error、count 不变、广播失败事件。"""
    bridge = _RecordingBridge()

    def runner(_args):
        return (
            1,
            json.dumps(
                {"ok": False, "error": "微信未登录，请先登录微信电脑版",
                 "code": "wechat_not_ready"},
                ensure_ascii=False,
            ),
            "",
        )

    service = _make_moments_service(tmp_path, runner=runner, bridge=bridge)
    service.sync_async()
    assert service.wait_sync(5) is True
    status = service.status()
    assert status["syncing"] is False
    assert status["count"] == 0
    assert status["error"] and "微信未登录" in status["error"]

    moments_event = next(event for event in bridge.events if event["type"] == "moments")
    assert moments_event["data"]["ok"] is False
    assert "微信未登录" in moments_event["data"]["error"]
    assert moments_event["data"]["limit"] == 10


def test_moments_process_failure_without_json(tmp_path):
    """子进程没有合法 JSON 输出时，错误信息包含退出码与 stderr 摘要。"""
    service = _make_moments_service(
        tmp_path, runner=lambda _args: (2, "", "TypeError: database is locked")
    )
    service.sync_async()
    assert service.wait_sync(5) is True
    error = service.status()["error"]
    assert error and "进程异常退出" in error
    assert "database is locked" in error


def test_moments_unavailable_without_interpreter(tmp_path):
    """找不到朋友圈解释器时 available=false、可读原因；同步立即返回失败。"""
    service = _make_moments_service(
        tmp_path, python=str(tmp_path / "no-such-python.exe")
    )
    status = service.status()
    assert status["available"] is False
    assert "解释器" in status["reason"]
    assert service.items() == []
    result = service.sync_async()
    assert result["ok"] is False and result["error"]
    assert "解释器" in service.status()["error"]


def test_moments_limit_get_and_scope_items(tmp_path):
    """items(limit) 截断、get 按 id、scope_items 支持 moments/moment。"""
    payload = _v2_moments_payload(
        items=_v2_sample_moments()
        + [
            _v2_moment("dup-1", 1759999999, author="王五", content="重复 id 只保留一条"),
            _v2_moment("15034350704629592685", 1, author="重复",
                       content="重复 id 只保留一条"),
        ]
    )
    service = _make_moments_service(
        tmp_path, runner=lambda _args: (0, json.dumps(payload, ensure_ascii=False), "")
    )
    service.sync_async()
    assert service.wait_sync(5) is True

    items = service.items()
    assert len(items) == 3, "重复 id 应去重"
    assert len({item["id"] for item in items}) == 3
    assert len(service.items(1)) == 1
    assert service.get("15034350704629592686")["author"] == "张三"
    assert service.get("缺失") is None

    latest = service.scope_items({"type": "moments"}, 2)
    assert len(latest) == 2
    single = service.scope_items(
        {"type": "moment", "id": "15034350704629592685"}, 10
    )
    assert [item["id"] for item in single] == ["15034350704629592685"]
    assert service.scope_items({"type": "chat"}, 10) == []


def test_moments_items_capped_at_30(tmp_path):
    """V2：内存最多保留/返回 30 条（同步 20 条也最多展示 30）。"""
    many = [
        _v2_moment(f"m-{index:03d}", 1760000000 + index, content=f"第 {index} 条")
        for index in range(35)
    ]
    service = _make_moments_service(
        tmp_path,
        runner=lambda _args: (
            0, json.dumps(_v2_moments_payload(items=many), ensure_ascii=False), ""
        ),
    )
    service.sync_async(20)
    assert service.wait_sync(5) is True
    assert len(service.items(100)) == 30, "items() 最多返回 30 条"
    assert len(service.scope_items({"type": "moments"}, 100)) == 30
    assert service.status()["count"] == 30



# ----------------------------------------------------------------------
# GroupsService：群聊列表（只读、内存缓存、不落盘）
# ----------------------------------------------------------------------
def test_groups_query_filters_chatrooms(tmp_path):
    """_query_groups 只返回 @chatroom、未删除、local_type=2 的群聊。"""
    import sqlite3

    from webui.groups import _query_groups

    db_path = tmp_path / "contact.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE contact (username TEXT, nick_name TEXT, remark TEXT, "
            "delete_flag INTEGER, local_type INTEGER)"
        )
        connection.executemany(
            "INSERT INTO contact VALUES (?,?,?,?,?)",
            [
                ("123@chatroom", "测试群", "工作群", 0, 2),
                ("456@chatroom", "产品群", "", 0, 2),
                ("789@chatroom", "已删群", "", 1, 2),
                ("abc@chatroom", "类型不对", "", 0, 1),
                ("wxid_friend", "张三", "好友备注", 0, 1),
            ],
        )
        connection.commit()
    finally:
        connection.close()

    rows = _query_groups(db_path)
    usernames = {str(row[0]) for row in rows}
    assert "123@chatroom" in usernames and "456@chatroom" in usernames
    assert "789@chatroom" not in usernames, "delete_flag=1 的群应过滤"
    assert "abc@chatroom" not in usernames, "local_type!=2 的应过滤"
    assert "wxid_friend" not in usernames, "好友不应出现在群聊列表"
    assert all(username.endswith("@chatroom") for username in usernames)


def test_groups_service_cache_and_refresh(tmp_path, monkeypatch):
    """GroupsService：首次读库、TTL 内缓存、refresh=True 强制刷新、is_group 判断。"""
    from webui import groups as groups_mod

    calls: list[str] = []

    def fake_query(path):
        calls.append(str(path))
        return [
            ("123@chatroom", "测试群", "工作群"),
            ("456@chatroom", "产品群", ""),
            ("789@chatroom", "重名群", "工作群"),  # 与 123 显示名重复，应去重
        ]

    class _FakeReader:
        def __init__(self, *_args, **_kwargs):
            pass

        def _materialize(self, _relative, _attribute):
            return str(tmp_path / "contact.db")

        def close(self):
            pass

    monkeypatch.setattr(groups_mod, "_query_groups", fake_query)
    monkeypatch.setattr("wx_core.wcdb.SessionReader", _FakeReader)
    # V6：GroupsService 会检测当前微信账号，账号变化会强制刷新缓存；测试固定账号
    monkeypatch.setattr("webui.account.detect_active_account", lambda: (None, "wxid_self"))

    service = groups_mod.GroupsService({}, log=_quiet_log, ttl=60.0)
    monkeypatch.setattr(service._history, "_account", lambda: (tmp_path, {}, "wxid_self"))
    first = service.list()
    assert first["ok"] is True and first["count"] == 2
    assert first["cached"] is False
    # 显示名 remark 优先、其次 nick_name；按名字升序
    assert [group["name"] for group in first["groups"]] == ["产品群", "工作群"]
    assert [group["username"] for group in first["groups"]] == [
        "456@chatroom",
        "123@chatroom",
    ]
    assert all(group["kind"] == "group" for group in first["groups"])
    assert calls == [str(tmp_path / "contact.db")]

    second = service.list()
    assert second["cached"] is True, "TTL 内应命中内存缓存"
    assert len(calls) == 1, "缓存命中不应重新读库"

    refreshed = service.list(refresh=True)
    assert refreshed["cached"] is False
    assert len(calls) == 2, "refresh=True 应强制刷新"

    assert service.is_group("工作群") is True
    assert service.is_group("测试群") is False, "有备注时显示名是备注，昵称不参与匹配"
    assert service.is_group("不存在的好友") is False
    assert service.is_group("") is False
    assert not (tmp_path / "groups.json").exists(), "群聊列表不应落盘"


def test_history_group_row_strips_prefix_and_sender_name():
    """群聊历史行：extra.is_group/sender_name；剥掉 "wxid_xxx:" 发送人前缀；自己不填成员名。"""
    from webui.history import _row_to_message

    friend_msg = _row_to_message(
        "测试群",
        {
            "local_id": 1, "local_type": 1, "sender": "wxid_zhangsan", "self": False,
            "create_time": 1760000000, "content": "wxid_zhangsan:\n大家好", "raw_flag": 4,
        },
        is_group=True,
        sender_names={"wxid_zhangsan": "张三"},
    )
    assert friend_msg is not None
    assert friend_msg.kind == "text"
    assert friend_msg.text == "大家好", "群聊文本的 wxid 前缀应被剥掉"
    assert friend_msg.extra["is_group"] is True
    assert friend_msg.extra["sender_name"] == "张三"

    self_msg = _row_to_message(
        "测试群",
        {
            "local_id": 2, "local_type": 1, "sender": "wxid_self", "self": True,
            "create_time": 1760000001, "content": "收到", "raw_flag": 0,
        },
        is_group=True,
        sender_names={"wxid_self": "我自己"},
    )
    assert self_msg is not None
    assert self_msg.sender == "self"
    assert self_msg.extra["is_group"] is True
    assert "sender_name" not in self_msg.extra, "自己的消息不应带群成员名"

    private_msg = _row_to_message(
        "张三",
        {
            "local_id": 3, "local_type": 1, "sender": "wxid_zhangsan", "self": False,
            "create_time": 1760000002, "content": "私聊消息", "raw_flag": 0,
        },
    )
    assert private_msg is not None
    assert "is_group" not in private_msg.extra
    assert private_msg.text == "私聊消息"


def test_groups_service_failure_is_readable(tmp_path, monkeypatch):
    """读库失败返回 ok:false + 中文原因（不抛异常、不影响其它功能）。"""
    from webui import groups as groups_mod

    service = groups_mod.GroupsService({}, log=_quiet_log)
    monkeypatch.setattr(
        service._history,
        "_account",
        lambda: (_ for _ in ()).throw(RuntimeError("找不到微信数据目录")),
    )
    result = service.list()
    assert result["ok"] is False
    assert "加载群聊失败" in result["error"]
    assert "找不到微信数据目录" in result["error"]
    assert result["groups"] == [] and result["count"] == 0


# ----------------------------------------------------------------------
# ContactsService 纯函数：按历史记录过滤 + 拼音首字母（V4）
# ----------------------------------------------------------------------
def test_contacts_build_lists_filters_and_initials(tmp_path):
    """_query_contacts/_build_lists：只保留有历史记录的联系人，并带拼音首字母。"""
    import sqlite3

    from webui.contacts import _build_lists, _query_contacts
    from webui.pinyin import pinyin_initial

    db_path = tmp_path / "contact.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE contact (username TEXT, nick_name TEXT, remark TEXT, "
            "local_type INTEGER, delete_flag INTEGER)"
        )
        connection.executemany(
            "INSERT INTO contact VALUES (?,?,?,?,?)",
            [
                ("wxid_liu", "刘", "", 1, 0),
                ("wxid_he", "何", "何备注", 1, 0),
                ("wxid_none", "无记录", "", 1, 0),
                ("123@chatroom", "测试群", "", 2, 0),
                ("999@chatroom", "已删群", "", 2, 1),
            ],
        )
        connection.commit()
    finally:
        connection.close()

    rows = _query_contacts(db_path)
    usernames = {row[0] for row in rows}
    assert "wxid_liu" in usernames and "123@chatroom" in usernames
    assert "999@chatroom" not in usernames, "delete_flag=1 应过滤"

    history = {"wxid_liu", "123@chatroom"}
    friends, groups = _build_lists(rows, history)
    assert [item["name"] for item in friends] == ["刘"], "无历史记录的好友应被过滤"
    assert [item["name"] for item in groups] == ["测试群"]
    assert friends[0]["kind"] == "friend" and friends[0]["has_history"] is True
    assert friends[0]["initial"] == "L", "刘 的拼音首字母应为 L"
    assert groups[0]["initial"] == "C" and groups[0]["kind"] == "group"

    friends_all, groups_all = _build_lists(rows, history, include_all=True)
    all_names = {item["name"] for item in friends_all} | {item["name"] for item in groups_all}
    assert {"刘", "何备注", "无记录", "测试群"} <= all_names, "include_all=True 返回全部"
    no_history = {item["name"]: item["has_history"] for item in friends_all}
    assert no_history["何备注"] is False and no_history["无记录"] is False

    assert pinyin_initial("刘") == "L"
    assert pinyin_initial("何") == "H"
    assert pinyin_initial("Alice") == "A"
    assert pinyin_initial("alice") == "A"
    assert pinyin_initial("3") == "#"


# ----------------------------------------------------------------------
# ReplayBackend：只读跟随模式（桌面版正在监听时用）
# ----------------------------------------------------------------------
def _drain_backend_queue(backend) -> list[dict]:
    """非阻塞取完后端事件队列里的所有事件。"""
    events: list[dict] = []
    while True:
        try:
            events.append(backend.events.get_nowait())
        except queue.Empty:
            return events


def _collect_backend_events(backend, predicate, timeout: float = 6.0) -> list[dict]:
    """持续从 backend.events 取事件，直到 predicate 为真或超时。"""
    collected: list[dict] = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            event = backend.events.get(timeout=0.2)
        except queue.Empty:
            continue
        collected.append(event)
        if predicate(collected):
            return collected
    return collected


def test_replay_backend_follows_log(tmp_path):
    """只读跟随：读历史 + tail 追加行；stop 后不再发；set_monitored 不写回 config。"""
    from webui.replay import ReplayBackend

    log_path = tmp_path / "messages.jsonl"
    history = [
        {"id": "r1", "friend": "张三", "sender": "friend", "kind": "text",
         "text": "历史消息一", "ts": time.time() - 10, "extra": {}},
        {"id": "r2", "friend": "李四", "sender": "self", "kind": "text",
         "text": "历史消息二", "ts": time.time() - 5, "extra": {}},
    ]
    log_path.write_text(
        json.dumps(history[0], ensure_ascii=False) + "\n"
        + "这不是合法 JSON，应被跳过\n"
        + json.dumps(history[1], ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    config = {"friends_cache": ["张三", "李四"], "monitored": ["张三"]}
    backend = ReplayBackend(config, log_path=log_path, interval=0.2)
    try:
        backend.start()
        events = _collect_backend_events(
            backend,
            lambda collected: len(
                [e for e in collected if e["type"] == "message"]
            ) >= 2,
        )
        messages = [e["data"] for e in events if e["type"] == "message"]
        assert [m["id"] for m in messages] == ["r1", "r2"], messages
        assert messages[0]["kind_label"] == "文字", "应补齐展示字段"
        assert messages[1]["sender_label"] == "我"
        assert any(e["type"] == "friends" for e in events)
        assert any(e["type"] == "status" for e in events)

        state = backend.get_state()
        assert state["running"] is True
        assert state["monitor_state"] == "following"
        assert state["friends_count"] == 2
        assert state["monitoring"] == ["张三"]

        # 桌面版追加 → 1 秒内出现新的 message 事件
        appended = {"id": "r3", "friend": "张三", "sender": "friend",
                    "kind": "text", "text": "桌面版刚收到的新消息",
                    "ts": time.time(), "extra": {}}
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(appended, ensure_ascii=False) + "\n")
        tailed = _collect_backend_events(
            backend,
            lambda collected: any(
                e["type"] == "message" and e["data"].get("id") == "r3"
                for e in collected
            ),
        )
        assert any(
            e["type"] == "message" and e["data"].get("id") == "r3" for e in tailed
        ), "追加的日志行没有变成 message 事件"
        assert backend.get_state()["chat_title"] == "张三"

        # 只读：set_monitored 只影响本页状态，不写回 config
        backend.set_monitored(["张三", "李四"])
        assert backend.get_state()["monitoring"] == ["张三", "李四"]
        assert config["monitored"] == ["张三"], "跟随模式不应修改传入的配置/监听名单"

        # 跟随模式不写日志文件（内容只来自测试自己追加的那行）
        content = log_path.read_text(encoding="utf-8")
        assert content.count("\n") == 4, "跟随模式不应向消息日志写任何内容"

        backend.stop()
        _drain_backend_queue(backend)  # 清掉停止前的残留事件
        time.sleep(0.5)
        leftover = [
            e for e in _drain_backend_queue(backend) if e["type"] == "message"
        ]
        assert leftover == [], "stop 后不应再产生 message 事件"
        assert backend.get_state()["running"] is False
    finally:
        backend.stop()



