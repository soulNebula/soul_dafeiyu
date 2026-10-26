# -*- coding: utf-8 -*-
"""P9：心情判定（移植「鱼的心情」）测试。

覆盖：
1. JSON 容错解析 / 缺字段默认 / 越界 clamp / 脏输出 None；
2. confidence 门槛：< 阈值 → 只落 timeline、不注入；达标 → 注入一次后不再重复；
3. 全 0 不记账；apply 的 mood 1/3 步进 + dims 即时 + mood_min 下限；
4. 衰减纯函数化：多次小步 == 一次大步；mood 半衰期 3h、dims 90min；
5. 口径 prompt 差异（user / speak-up / duty）+ runtime-context 跳过；
6. judge 的规则回退与 crisis 不调模型；judge 关闭时不调模型；
7. chat 接线：回复后 judge(mode=user) + apply 落 persona 库；crisis 不判；
8. mood_inject=false 时 chat system prompt 逐字节不变；true 时只注入一次；
9. console snapshot 字段（心情/情绪/时间线/下一轮注入预览）。
"""
from __future__ import annotations

import json
import random
import time

import pytest

from agi.selflearn import db, mood_judge
from agi.selflearn.chat import SelfLearnAgent

VALID = json.dumps({
    "moodDelta": -6,
    "dims": {"happy": 0, "calm": 0, "sweet": 0, "heartache": 5,
             "worry": 3, "sad": 2, "jealous": 0},
    "reason": "用户说最近很累，我心里沉了一下，先问了一句要不要歇会儿",
    "confidence": 82,
}, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _isolate_mood(monkeypatch, tmp_path):
    """不读生产 mood.json；清 MOOD_* 环境变量与 client 缓存。"""
    for name in ("MOOD_JUDGE_ENABLED", "MOOD_INJECT", "MOOD_JUDGE_ASYNC",
                 "MOOD_JUDGE_BACKEND", "MOOD_JUDGE_CONFIDENCE_MIN", "MOOD_MIN",
                 "MOOD_HALF_LIFE_H", "MOOD_DIM_HALF_LIFE_MIN", "MOOD_SOUL_URL",
                 "MOOD_SOUL_TIMEOUT_S"):
        monkeypatch.delenv(name, raising=False)
    # 判定默认异步（不拖慢主链）；单测强制同步以保证断言确定
    monkeypatch.setenv("MOOD_JUDGE_ASYNC", "0")
    # 既有用例沿用 local 后端语义；后端选择用例自己覆盖
    monkeypatch.setenv("MOOD_JUDGE_BACKEND", "local")
    monkeypatch.setenv("MOOD_CONFIG_PATH", str(tmp_path / "mood_missing.json"))
    mood_judge._CLIENTS.clear()
    mood_judge._DEEPSEEK_CLIENTS.clear()
    yield
    mood_judge._CLIENTS.clear()
    mood_judge._DEEPSEEK_CLIENTS.clear()


def _path(tmp_path, name="mood"):
    path = tmp_path / f"{name}.sqlite3"
    db.connect(path).close()
    return path


def _timeline(path):
    con = db.connect(path)
    try:
        return [dict(row) for row in con.execute(
            "SELECT * FROM mood_timeline ORDER BY at").fetchall()]
    finally:
        con.close()


def _delta(**overrides):
    data = {
        "moodDelta": -6,
        "dims": {"happy": 0, "calm": 0, "sweet": 0, "heartache": 5,
                 "worry": 3, "sad": 2, "jealous": 0},
        "reason": "对方说很累，我心里沉了一下，问了一句先歇会儿",
        "confidence": 80,
    }
    data.update(overrides)
    return data


# --------------------------------------------------------------------------- #
# 1. 解析 / 归一化
# --------------------------------------------------------------------------- #
def test_parse_delta_tolerates_markdown_and_extra_text():
    wrapped = ("好的，这是判断：\n```json\n" + VALID + "\n```\n以上。")
    parsed = mood_judge.parse_delta(wrapped)
    assert parsed is not None
    assert parsed["moodDelta"] == -6
    assert parsed["dims"]["heartache"] == 5
    assert parsed["confidence"] == 82
    assert parsed["reason"].startswith("用户说最近很累")
    assert mood_judge.parse_delta("抱歉，我无法判断") is None
    assert mood_judge.parse_delta("") is None


def test_parse_delta_missing_fields_and_clamp():
    parsed = mood_judge.parse_delta('{"moodDelta": 99, "dims": {"happy": -99}}')
    assert parsed is not None
    assert parsed["moodDelta"] == 10
    assert parsed["dims"]["happy"] == -10
    assert parsed["dims"]["sad"] == 0
    assert parsed["confidence"] == 70          # 缺 confidence 按 70
    assert parsed["reason"] == ""

    clean = mood_judge.parse_delta(json.dumps({
        "moodDelta": -3, "reason": "此刻心里有点酸，一直放不下", "confidence": 120,
    }))
    assert clean["confidence"] == 100
    assert "此刻" not in clean["reason"] and "一直" not in clean["reason"]


# --------------------------------------------------------------------------- #
# 2. confidence 门槛 / 一次只说一次 / 全 0 不记账
# --------------------------------------------------------------------------- #
def test_low_confidence_records_but_does_not_inject(monkeypatch, tmp_path):
    path = _path(tmp_path)
    monkeypatch.setenv("MOOD_INJECT", "1")
    monkeypatch.setenv("MOOD_JUDGE_CONFIDENCE_MIN", "60")
    mood_judge.apply(path, _delta(confidence=55), now=1000.0)

    assert len(_timeline(path)) == 1                       # 只记录
    assert mood_judge.context_line(path, now=1001.0) == ""  # 不注入


def test_high_confidence_injects_exactly_once(monkeypatch, tmp_path):
    path = _path(tmp_path)
    monkeypatch.setenv("MOOD_INJECT", "1")
    mood_judge.apply(path, _delta(confidence=82), now=1000.0)

    line = mood_judge.context_line(path, now=1001.0)
    assert "<mood_context>" in line
    assert "（刚才）" in line and "用户说很累" in line
    assert "别把这段念出来" in line
    # 同一条变化只说一次
    assert mood_judge.context_line(path, now=1002.0) == ""
    assert mood_judge.context_line(path, now=9999.0) == ""


def test_inject_off_returns_empty_without_reading_state(monkeypatch, tmp_path):
    path = _path(tmp_path)
    mood_judge.apply(path, _delta(), now=1000.0)
    assert mood_judge.context_line(path, now=1001.0) == ""
    # 注入关闭时不应把 last_injected_at 标掉
    assert mood_judge.load_state(path)["last_injected_at"] == 0


def test_no_change_is_not_recorded(tmp_path):
    path = _path(tmp_path)
    zero = _delta(moodDelta=0,
                  dims={name: 0 for name in mood_judge.DIMS},
                  reason="", confidence=50)
    result = mood_judge.apply(path, zero, now=1000.0)
    assert result["changed"] is False
    assert _timeline(path) == []
    # 无变化不更新 last_reason/last_at
    st = mood_judge.load_state(path)
    assert st["last_reason"] == "" and st["last_at"] == 0
    assert mood_judge.context_line(path, now=1001.0) == ""


# --------------------------------------------------------------------------- #
# 3. apply：1/3 步进、dims 即时、mood_min
# --------------------------------------------------------------------------- #
def test_apply_uses_one_third_mood_step_and_instant_dims(tmp_path):
    path = _path(tmp_path)
    result = mood_judge.apply(path, _delta(moodDelta=-9), now=1000.0)
    assert result["changed"] is True
    assert result["mood_delta"] == -3                     # round(-9/3)
    assert result["mood"] == pytest.approx(67.0)
    assert result["dims"]["heartache"] == pytest.approx(5.0)
    assert result["dims"]["worry"] == pytest.approx(3.0)
    row = _timeline(path)[0]
    assert row["mode"] == "user"
    assert row["confidence"] == 80
    assert json.loads(row["dims_delta_json"])["heartache"] == 5


def test_mood_min_clamps_lower_bound(monkeypatch, tmp_path):
    path = _path(tmp_path)
    monkeypatch.setenv("MOOD_MIN", "30")
    for index in range(20):
        mood_judge.apply(path, _delta(moodDelta=-10), now=1000.0 + index)
    assert mood_judge.load_state(path)["mood"] >= 30.0


# --------------------------------------------------------------------------- #
# 4. 衰减：小步 == 大步；半衰期
# --------------------------------------------------------------------------- #
def test_decay_small_steps_equal_one_big_step():
    start = 1_000_000.0
    mood, dims, _ = mood_judge.decay_values(100.0, {"happy": 40.0}, start, start)
    for step in range(1, 271):                 # 270 次 × 20s = 90 分钟
        mood, dims, _ = mood_judge.decay_values(
            mood, dims, start + (step - 1) * 20.0, start + step * 20.0)
    big_mood, big_dims, _ = mood_judge.decay_values(100.0, {"happy": 40.0},
                                                    start, start + 90 * 60.0)
    assert mood == pytest.approx(big_mood, rel=1e-9)
    assert dims["happy"] == pytest.approx(big_dims["happy"], rel=1e-9)


def test_half_lives():
    start = 0.0
    mood, dims, _ = mood_judge.decay_values(100.0, {"happy": 40.0}, start,
                                            start + 3 * 3600.0)
    assert mood == pytest.approx(85.0)          # 基线 70：(100-70)/2 + 70
    assert dims["happy"] == pytest.approx(10.0)  # 90 分钟半衰期，3h=4 个半衰期 → 40/16=2.5? 见下
    # 实际是 3h / 90min = 2 个半衰期：40 * (1/2)^2 = 10

    mood0, dims0, elapsed = mood_judge.decay_values(100.0, {"happy": 40.0}, 0, 0)
    assert elapsed == 0.0 and mood0 == 100.0 and dims0["happy"] == 40.0


def test_decay_writes_last_decay_at_and_is_idempotent(tmp_path):
    path = _path(tmp_path)
    mood_judge.decay(path, now=10_000.0)
    first = mood_judge.load_state(path)["last_decay_at"]
    assert first == 10_000.0
    mood_judge.decay(path, now=10_000.0)
    assert mood_judge.load_state(path)["last_decay_at"] == 10_000.0


# --------------------------------------------------------------------------- #
# 5. 口径 prompt / runtime-context 跳过
# --------------------------------------------------------------------------- #
def test_judge_prompt_has_core_rules_and_mode_differences():
    user_msgs = mood_judge.judge_messages("user", "今天好累", "先歇会儿吧")
    speak_msgs = mood_judge.judge_messages("speak-up", "闹钟", "在忙吗？")
    duty_msgs = mood_judge.judge_messages("duty", "刷B站", "刷完了，没啥意思")

    user_system = user_msgs[0]["content"]
    assert "心情下降只能因为" in user_system
    assert "心疼" in user_system and "担心" in user_system
    assert "绝不许因为「对方惹你生气」" in user_system
    assert "两段式" in user_system and "过去式" in user_system
    assert "confidence" in user_system
    assert "不许替对方补细节" in user_system
    # 三种口径 prompt 不同
    assert speak_msgs[0]["content"] != user_system
    assert duty_msgs[0]["content"] != user_system
    assert "自己主动去找对方搭话" in speak_msgs[0]["content"]
    assert "定时任务把你叫起来干活" in duty_msgs[0]["content"]
    assert "对方这轮说" in user_msgs[1]["content"]
    assert "只看这句" in speak_msgs[1]["content"]


class _FakeMoodClient:
    def __init__(self, reply=VALID, error=""):
        self.reply = reply
        self.error = error
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.error:
            return {"ok": False, "text": "", "error": self.error}
        return {"ok": True, "text": self.reply}


def test_judge_runtime_context_is_skipped(monkeypatch, tmp_path):
    path = _path(tmp_path)
    monkeypatch.setenv("MOOD_JUDGE_ENABLED", "1")
    client = _FakeMoodClient()
    assert mood_judge.judge(path, mode="runtime-context", user_text="宿主注入",
                            our_reply="", client=client) is None
    assert client.calls == []
    assert _timeline(path) == []


def test_judge_uses_model_then_falls_back_to_rule(monkeypatch, tmp_path):
    path = _path(tmp_path)
    monkeypatch.setenv("MOOD_JUDGE_ENABLED", "1")

    client = _FakeMoodClient(VALID)
    delta = mood_judge.judge(path, mode="user", user_text="今天好累",
                             our_reply="先歇会儿", client=client)
    assert delta["source"] == "local"
    assert delta["moodDelta"] == -6 and delta["mode"] == "user"
    assert client.calls and client.calls[0]["kwargs"]["json_mode"] is True

    bad = _FakeMoodClient("不是 JSON")
    fallback = mood_judge.judge(path, mode="user", user_text="今天好累，没睡好",
                                our_reply="先歇会儿", client=bad)
    assert fallback["source"] == "rule"
    assert fallback["moodDelta"] < 0 and fallback["dims"]["heartache"] > 0
    assert fallback["reason"]

    raising = _FakeMoodClient(error="TimeoutError: timed out")
    timeout_delta = mood_judge.judge(path, mode="user", user_text="今天好累",
                                     our_reply="先歇会儿", client=raising)
    assert timeout_delta["source"] == "rule"


def test_judge_disabled_does_not_call_model(monkeypatch, tmp_path):
    path = _path(tmp_path)
    client = _FakeMoodClient()
    assert mood_judge.judge(path, mode="user", user_text="今天好累",
                            our_reply="", client=client) is None
    assert client.calls == []
    assert _timeline(path) == []


def test_crisis_skips_model_and_uses_rule(monkeypatch, tmp_path):
    path = _path(tmp_path)
    monkeypatch.setenv("MOOD_JUDGE_ENABLED", "1")
    client = _FakeMoodClient()
    delta = mood_judge.judge(path, mode="user", user_text="我不想活了",
                             our_reply="先别一个人扛着", client=client)
    assert delta["source"] == "rule"
    assert client.calls == []
    assert delta["moodDelta"] < 0
    assert delta["dims"]["heartache"] > 0 and delta["dims"]["worry"] > 0


# --------------------------------------------------------------------------- #
# 6. 落库位置：心情表在 persona 库
# --------------------------------------------------------------------------- #
def test_mood_tables_live_in_persona_db(tmp_path):
    path = _path(tmp_path, "split")
    mood_judge.apply(path, _delta(), now=1000.0)

    persona = db.persona_connect(path)
    try:
        row = persona.execute("SELECT mood FROM mood_state WHERE id='self'").fetchone()
        count = persona.execute("SELECT count(*) FROM mood_timeline").fetchone()[0]
    finally:
        persona.close()
    assert row is not None and count == 1

    memory = db.memory_connect(path)
    try:
        tables = {r[0] for r in memory.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    finally:
        memory.close()
    assert "mood_state" not in tables and "mood_timeline" not in tables


# --------------------------------------------------------------------------- #
# 7. chat 接线：回复后 judge + apply / crisis 不判
# --------------------------------------------------------------------------- #
class _ChatFakeClient:
    def __init__(self, reply="嗯，听着呢。今天怎么个累法？"):
        self.reply = reply
        self.calls: list[dict] = []

    def available(self):
        return True

    def chat(self, messages, **kwargs):
        text = "\n".join(str(m.get("content") or "") for m in messages)
        self.calls.append({"messages": messages, "kwargs": kwargs, "text": text})
        if "心情判定" in text or "心情发生了什么变化" in text:
            return {"ok": True, "text": VALID}
        return {"ok": True, "text": self.reply}


def _agent(tmp_path, name="chat", *, client=None):
    path = tmp_path / f"{name}.sqlite3"
    return SelfLearnAgent(path, client=client or _ChatFakeClient(),
                          rng=random.Random(7)), path


def _system_of(client) -> str:
    """取本轮主生成 system（多个抽取器 system 里最长的那个）。"""
    systems = []
    for call in client.calls:
        message = call["messages"][0]
        if isinstance(message, dict) and message.get("role") == "system":
            content = message.get("content")
            if isinstance(content, str):
                systems.append(content)
    if not systems:
        raise AssertionError("没有捕获到 system prompt")
    return max(systems, key=len)


def test_chat_judges_mood_after_reply(monkeypatch, tmp_path):
    monkeypatch.setenv("MOOD_JUDGE_ENABLED", "1")
    mood_client = _FakeMoodClient(VALID)
    monkeypatch.setattr(mood_judge, "_default_client", lambda cfg: mood_client)
    client = _ChatFakeClient()
    agent, path = _agent(tmp_path, "judged", client=client)

    result = agent.send("今天好累，没睡好")
    assert result["ok"] is True
    assert result["mood"] is not None and result["mood"]["changed"] is True
    assert result["mood"]["mode"] == "user"
    assert mood_client.calls, "chat 应该调用心情判定模型"
    rows = _timeline(path)
    assert len(rows) == 1
    assert rows[0]["mode"] == "user" and rows[0]["confidence"] == 82
    assert "用户说" in rows[0]["reason"]


def test_chat_does_not_judge_crisis(monkeypatch, tmp_path):
    monkeypatch.setenv("MOOD_JUDGE_ENABLED", "1")
    mood_client = _FakeMoodClient(VALID)
    monkeypatch.setattr(mood_judge, "_default_client", lambda cfg: mood_client)
    agent, path = _agent(tmp_path, "crisis")
    result = agent.send("我不想活了")
    assert result["crisis"] is True and result["mood"] is None
    assert _timeline(path) == []
    assert mood_client.calls == []


def test_chat_judge_off_keeps_prompt_byte_identical(monkeypatch, tmp_path):
    """mood_inject=false（默认）时 chat system prompt 与改动前逐字节一致。"""
    monkeypatch.delenv("MOOD_INJECT", raising=False)
    client_off = _ChatFakeClient()
    agent_off, _ = _agent(tmp_path, "off_a", client=client_off)
    assert agent_off.send("今天有点累")["ok"] is True
    off_system = _system_of(client_off)
    assert "<mood_context>" not in off_system

    # 即使库里有待注入的心情变化，inject=false 也不改变 prompt
    path = tmp_path / "off_a.sqlite3"
    mood_judge.apply(path, _delta(), now=1000.0)
    client2 = _ChatFakeClient()
    agent2, _ = _agent(tmp_path, "off_b", client=client2)
    assert agent2.send("今天有点累")["ok"] is True
    assert _system_of(client2) == off_system


def test_chat_injects_mood_once_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("MOOD_INJECT", "1")
    path = tmp_path / "inject.sqlite3"
    db.connect(path).close()
    mood_judge.apply(path, _delta(confidence=85), now=time.time())
    client = _ChatFakeClient()
    agent = SelfLearnAgent(path, client=client, rng=random.Random(7))

    assert agent.send("今天有点累")["ok"] is True
    first_system = _system_of(client)
    assert "<mood_context>" in first_system
    assert "用户说很累" in first_system

    client2 = _ChatFakeClient()
    agent.client = client2
    assert agent.send("刚才说到哪了")["ok"] is True
    second_system = _system_of(client2)
    assert "<mood_context>" not in second_system


# --------------------------------------------------------------------------- #
# 8. console snapshot
# --------------------------------------------------------------------------- #
def test_snapshot_shape_for_console(monkeypatch, tmp_path):
    path = _path(tmp_path)
    monkeypatch.setenv("MOOD_INJECT", "1")
    mood_judge.apply(path, _delta(confidence=88), now=1000.0)
    snap = mood_judge.snapshot(path, now=1100.0)
    assert snap["mood"] == pytest.approx(68.0, abs=0.2)
    assert snap["mood_label"] in ("松快", "一般")
    assert set(snap["dims"]) == set(mood_judge.DIMS)
    assert snap["timeline"] and snap["timeline"][0]["reason"].startswith("用户说很累")
    assert snap["pending_inject"] is True
    assert "<mood_context>" in snap["injected_preview"]
    assert snap["settings"]["judge_enabled"] is False
    assert snap["settings"]["async"] is False
    assert snap["settings"]["judge_backend"] == "local"
    assert snap["last_confidence"] == 88


def test_chat_async_judge_does_not_block_and_records(monkeypatch, tmp_path):
    """默认异步判定：send 立刻返回 queued，后台线程稍后落库。"""
    monkeypatch.setenv("MOOD_JUDGE_ENABLED", "1")
    monkeypatch.setenv("MOOD_JUDGE_ASYNC", "1")
    mood_client = _FakeMoodClient(VALID)
    monkeypatch.setattr(mood_judge, "_default_client", lambda cfg: mood_client)
    client = _ChatFakeClient()
    agent, path = _agent(tmp_path, "async_judge", client=client)

    result = agent.send("今天好累，没睡好")
    assert result["ok"] is True
    assert result["mood"] == {"queued": True, "mode": "user"}

    deadline = time.time() + 5.0
    rows = []
    while time.time() < deadline:
        rows = _timeline(path)
        if rows:
            break
        time.sleep(0.05)
    assert rows, "后台判定应在 5s 内落库"
    assert rows[0]["mode"] == "user" and rows[0]["confidence"] == 82


# --------------------------------------------------------------------------- #
# 9. P9-b：判定后端选择（deepseek teacher / local / none）与回退链
# --------------------------------------------------------------------------- #
def test_settings_backend_default_env_and_file(monkeypatch, tmp_path):
    for name in ("MOOD_JUDGE_BACKEND",):
        monkeypatch.delenv(name, raising=False)
    assert mood_judge.settings()["judge_backend"] == "deepseek"   # 默认 teacher
    monkeypatch.setenv("MOOD_JUDGE_BACKEND", "local")
    assert mood_judge.settings()["judge_backend"] == "local"
    monkeypatch.setenv("MOOD_JUDGE_BACKEND", "乱写")
    assert mood_judge.settings()["judge_backend"] == "deepseek"
    # mood.json 读取
    cfg = tmp_path / "mood.json"
    cfg.write_text(json.dumps({"judge_backend": "none"}), encoding="utf-8")
    monkeypatch.delenv("MOOD_JUDGE_BACKEND", raising=False)
    monkeypatch.setenv("MOOD_CONFIG_PATH", str(cfg))
    assert mood_judge.settings()["judge_backend"] == "none"


def _judge_ready(monkeypatch):
    monkeypatch.setenv("MOOD_JUDGE_ENABLED", "1")


def test_judge_deepseek_backend_uses_teacher(monkeypatch, tmp_path):
    _judge_ready(monkeypatch)
    monkeypatch.setenv("MOOD_JUDGE_BACKEND", "deepseek")
    teacher = _FakeMoodClient(VALID)
    monkeypatch.setattr(mood_judge, "_deepseek_client", lambda cfg: teacher)
    monkeypatch.setattr(mood_judge, "_default_client",
                        lambda cfg: pytest.fail("teacher 成功时不该调 local"))

    delta = mood_judge.judge(_path(tmp_path), mode="user",
                             user_text="今天被老板骂了，有点难受",
                             our_reply="被骂了确实憋屈，先缓一缓")
    assert delta["source"] == "deepseek"
    assert delta["moodDelta"] == -6
    assert teacher.calls and teacher.calls[0]["kwargs"]["purpose"] == "mood_judge"
    assert teacher.calls[0]["kwargs"]["max_tokens"] <= 200
    assert teacher.calls[0]["kwargs"]["temperature"] <= 0.3
    assert teacher.calls[0]["kwargs"]["json_mode"] is True


def test_judge_deepseek_failure_falls_back_to_local(monkeypatch, tmp_path):
    _judge_ready(monkeypatch)
    monkeypatch.setenv("MOOD_JUDGE_BACKEND", "deepseek")
    teacher = _FakeMoodClient(error="HTTPError: 500")
    local = _FakeMoodClient(VALID)
    monkeypatch.setattr(mood_judge, "_deepseek_client", lambda cfg: teacher)
    monkeypatch.setattr(mood_judge, "_default_client", lambda cfg: local)

    delta = mood_judge.judge(_path(tmp_path), mode="user", user_text="今天好累",
                             our_reply="先歇会儿")
    assert delta["source"] == "local"
    assert teacher.calls and local.calls          # teacher 试过、local 兜住


def test_judge_local_failure_falls_back_to_rule(monkeypatch, tmp_path):
    _judge_ready(monkeypatch)
    monkeypatch.setenv("MOOD_JUDGE_BACKEND", "local")
    local = _FakeMoodClient(error="TimeoutError: timed out")
    monkeypatch.setattr(mood_judge, "_default_client", lambda cfg: local)
    delta = mood_judge.judge(_path(tmp_path), mode="user",
                             user_text="今天被老板骂了，有点难受",
                             our_reply="先缓一缓")
    assert delta["source"] == "rule"
    assert delta["moodDelta"] < 0


def test_judge_backend_none_uses_rule_without_model(monkeypatch, tmp_path):
    _judge_ready(monkeypatch)
    monkeypatch.setenv("MOOD_JUDGE_BACKEND", "none")
    local = _FakeMoodClient(VALID)
    monkeypatch.setattr(mood_judge, "_default_client", lambda cfg: local)
    delta = mood_judge.judge(_path(tmp_path), mode="user", user_text="今天好累",
                             our_reply="先歇会儿")
    assert delta["source"] == "rule"
    assert local.calls == []


def test_prompt_has_sign_and_pronoun_hard_constraints():
    system = mood_judge.judge_messages("user", "被骂了", "先缓一缓")[0]["content"]
    assert "符号硬约束" in system
    assert "heartache" in system and "worry" in system and "sad" in system
    assert "必须 > 0" in system
    assert "正例" in system and "反例" in system
    assert "不许出现「他 / 她 / 对方」" in system
    assert "元叙述" in system and "我慢点答" in system
    assert "心里揪了一下" in system


def test_judge_deepseek_retries_with_bigger_budget(monkeypatch, tmp_path):
    """deepseek-flash 的 reasoning 会占 max_tokens：200 预算偶发空正文 → 放宽一次重试。"""
    _judge_ready(monkeypatch)
    monkeypatch.setenv("MOOD_JUDGE_BACKEND", "deepseek")

    class _FlakyTeacher:
        def __init__(self):
            self.calls = []

        def available(self):
            return True

        def chat(self, messages, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return {"ok": False, "text": "", "error": "空正文（预算被 reasoning 吃光）"}
            return {"ok": True, "text": VALID}

    teacher = _FlakyTeacher()
    monkeypatch.setattr(mood_judge, "_deepseek_client", lambda cfg: teacher)
    delta = mood_judge.judge(_path(tmp_path), mode="user",
                             user_text="今天被老板骂了，有点难受",
                             our_reply="先缓一缓")
    assert delta["source"] == "deepseek"
    assert len(teacher.calls) == 2
    assert teacher.calls[0]["max_tokens"] == 200
    assert teacher.calls[1]["max_tokens"] > teacher.calls[0]["max_tokens"]
