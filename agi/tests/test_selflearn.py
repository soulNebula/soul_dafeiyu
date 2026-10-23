# -*- coding: utf-8 -*-
"""自学习对话体：用户记忆、策略选择、反馈学习、自人格反思。"""
from __future__ import annotations

import json
import random

from agi.selflearn import acceptance, db, errors, generation, guard, media, memory, personality, proactive, report, scheduler, skills, skilltree, state, summary, trust
from agi.selflearn.chat import SelfLearnAgent


class FakeClient:
    def available(self): return True
    def chat(self, messages, **kwargs):
        text = "\n".join(str(m.get("content") or "") for m in messages)
        if "用户记忆抽取器" in text:
            return {"ok": True, "text": json.dumps([
                {"kind": "preference", "content": "用户喜欢晚上写东西",
                 "evidence": "我喜欢晚上写东西", "confidence": 0.9}])}
        if "自学习对话体自己的行动和结果" in text:
            return {"ok": True, "text": json.dumps({"traits": [
                {"key": "prefer_question", "value": "先问一句再回应",
                 "evidence_turns": []}]})}
        return {"ok": True, "text": "晚上写东西挺安静的。你一般写什么？"}


def test_chat_stores_user_memory_and_turn(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    agent = SelfLearnAgent(path, client=FakeClient(), rng=random.Random(1))
    r = agent.send("我喜欢晚上写东西")
    assert r["ok"] and r["reply"]
    assert r["turn_id"] and r["variant"]["variant_id"]
    counts = db.counts(path)
    assert counts["messages"] == 2 and counts["turns"] == 1
    assert counts["user_memories"] >= 1
    # 提问不能被存成偏好
    memory.extract_user_memories(path, FakeClient(), "你还记得我喜欢什么吗", "m2")
    assert len(memory.list_user_memories(path)) == 1


def test_feedback_updates_variant_and_self_persona(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    agent = SelfLearnAgent(path, client=FakeClient(), rng=random.Random(2))
    turns = []
    for text in ("你好", "今天有点累", "我最近在学硬件"):
        r = agent.send(text); turns.append(r["turn_id"])
    fb = agent.feedback(turns[0], "good")
    assert fb["ok"] is True
    # 初始没有已学会策略；反馈先记到基线上，等反思长出灰度策略
    # 3 条反馈触发自反思；traits 没有 evidence_turns 时必须被拒
    agent.feedback(turns[1], "ok")
    reflected = agent.feedback(turns[2], "bad")
    assert reflected["ok"] is True
    assert personality.list_persona(path) == []


def test_llm_can_store_self_memory_not_only_user(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    class M(FakeClient):
        def chat(self, messages, **kwargs):
            return {"ok": True, "text": json.dumps({"items": [
                {"scope": "self", "kind": "insight", "content": "我发现短句更容易被接住",
                 "evidence": "我最近在做一个会自己学习的对话机器人", "confidence": 0.8}]})}
    text = "我最近在做一个会自己学习的对话机器人"
    items = memory.extract_user_memories(path, M(), text, "m1")
    assert items and items[0]["scope"] == "self"


def test_active_persona_value_cannot_flip(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    con = db.connect(path)
    con.execute("INSERT INTO turns(id,user_message_id,assistant_message_id,reply,feedback,score,created_at) VALUES(?,?,?,?,?,?,?)",
                ("T-1", "u", "a", "短句回应", "good", 1.0, 1.0))
    con.execute("INSERT INTO turns(id,user_message_id,assistant_message_id,reply,feedback,score,created_at) VALUES(?,?,?,?,?,?,?)",
                ("T-2", "u2", "a2", "先问再答", "ok", 0.6, 2.0))
    con.execute("INSERT INTO turns(id,user_message_id,assistant_message_id,reply,feedback,score,created_at) VALUES(?,?,?,?,?,?,?)",
                ("T-3", "u3", "a3", "给建议", "bad", 0.0, 3.0))
    con.execute("INSERT INTO persona(id,key,value,evidence_json,version,status,updated_at) VALUES(?,?,?,?,?,?,?)",
                ("P-x", "tone", "简短克制", '{"turns":["T-1"]}', 1, "active", 1.0))
    con.commit(); con.close()
    class Flip(FakeClient):
        def chat(self, messages, **kwargs):
            return {"ok": True, "text": json.dumps({"traits": [
                {"key": "tone", "value": "热情外放", "evidence_turns": ["T-1"]}]})}
    result = personality.reflect(path, Flip())
    assert result["ok"] is True
    active = {p["key"]: p["value"] for p in personality.list_persona(path)}
    assert active["tone"] == "简短克制"


def test_feedback_updates_trust_and_error_lesson(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    agent = SelfLearnAgent(path, client=FakeClient(), rng=random.Random(3))
    r = agent.send("你好")
    before = trust.get(path)
    fb = agent.feedback(r["turn_id"], "bad")
    assert fb["ok"] is True
    assert trust.get(path) < before
    assert fb.get("error_lesson", {}).get("category")
    assert errors.active_lessons(path)


def test_proactive_and_scheduler_are_candidates_only(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    memory.store_message(path, "user", "我最近有点累", now=1792770000.0)
    memory.store_message(path, "assistant", "那就先歇一会儿", now=1792770001.0)
    trust.set_value(path, 0.9)
    p = proactive.propose(path, now=1792777200.0)
    assert p["ok"] is True
    assert "note" in p and "不会自动发送" in p["note"]
    iid = scheduler.add(path, "check_in", "回来看看", 1792770000.0)
    assert iid in {i["id"] for i in scheduler.due(path, now=1792777200.0)}


def test_state_crisis_and_acceptance_metrics(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    st = state.update(path, "我不想活了")
    assert st["boundary"]["crisis"] is True
    for i in range(3):
        memory.store_message(path, "user", f"消息{i}")
        memory.store_message(path, "assistant", f"回复{i}")
    con = db.connect(path)
    for i in range(3):
        con.execute("INSERT INTO turns(id,user_message_id,assistant_message_id,reply,feedback,score,created_at) VALUES(?,?,?,?,?,?,?)",
                    (f"T-{i}", f"u{i}", f"a{i}", "回复", "good", 1.0, float(i)))
        con.execute("INSERT INTO feedback(id,turn_id,reaction,score,created_at) VALUES(?,?,?,?,?)",
                    (f"F-{i}", f"T-{i}", "good", 1.0, float(i)))
    con.commit(); con.close()
    acc = acceptance.run(path)
    assert acc["checks"]["chat_ready"] is True
    assert acc["checks"]["feedback_loop"] is True
    assert acc["metrics"]["messages"] >= 6


def test_weak_validation_requires_five_samples(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    skills.ensure_defaults(path)
    skills.add_dynamic_variant(path, "测试策略")
    vid = skills.variant_stats(path)[0]["id"]
    for _ in range(4):
        skills.record_weak_check(path, skills.SKILL_ID, vid, True)
    assert skills.evaluate_skill(path)["level"] == 0
    skills.record_weak_check(path, skills.SKILL_ID, vid, True)
    result = skills.evaluate_skill(path)
    assert result["level"] >= 1
    assert result["variants"][vid]["n"] >= 5


def test_trust_repair_and_proactive_decision(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    trust.set_value(path, 0.6)
    trust.update(path, "bad", "T-1", "test")
    assert trust.unresolved(path)
    repaired = trust.repair(path)
    assert repaired["repaired"] == 1
    assert trust.unresolved(path) == []
    memory.store_message(path, "user", "在吗", now=1792770000.0)
    memory.store_message(path, "assistant", "在", now=1792770001.0)
    trust.set_value(path, 0.9)
    p = proactive.propose(path, now=1792777200.0)
    assert p["available"] is True
    decided = proactive.decide(path, p["id"], "accepted")
    assert decided["ok"] is True


def test_image_memory_is_world_scope(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    item = memory.store_image_memory(path, "一张深色聊天界面截图", "m1")
    assert item is not None
    assert item["scope"] == "world"
    assert memory.list_user_memories(path)[0]["scope"] == "world"


def test_guard_softens_or_blocks_unsafe_claims(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    l1 = guard.check("你一定是对的", path)
    assert l1["level"] == "L1"
    assert "一定" not in l1["reply"]
    l2 = guard.check("我查了资料，一定没错", path)
    assert l2["level"] == "L2"


def test_conflicting_memories_are_kept_not_overwritten(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    class C:
        def available(self): return True
        def chat(self, messages, **kwargs):
            text = "\n".join(str(m.get("content") or "") for m in messages)
            import json as _json
            if "我喜欢猫" in text:
                return {"ok": True, "text": _json.dumps({"items": [
                    {"scope": "user", "kind": "preference", "content": "用户喜欢猫",
                     "evidence": "我喜欢猫", "confidence": 0.9}]})}
            return {"ok": True, "text": _json.dumps({"items": [
                {"scope": "user", "kind": "preference", "content": "用户讨厌猫",
                 "evidence": "我讨厌猫", "confidence": 0.9}]})}
    memory.extract_user_memories(path, C(), "我喜欢猫", "m1")
    memory.extract_user_memories(path, C(), "我讨厌猫", "m2")
    conflicts = memory.open_conflicts(path)
    assert len(conflicts) == 1
    con = db.connect(path)
    rows = [dict(r) for r in con.execute("SELECT status FROM user_memories")]
    con.close()
    assert {r["status"] for r in rows} == {"conflict"}
    memory.resolve_conflict(path, conflicts[0]["id"], conflicts[0]["mem_b"])
    assert memory.open_conflicts(path) == []


def test_growth_report_blank_and_forming(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    g = report.growth(path)
    assert g["stage"] == "blank"
    assert g["messages"]["total"] == 0
    agent = SelfLearnAgent(path, client=FakeClient(), rng=random.Random(4))
    turns = [agent.send(text)["turn_id"] for text in ("你好", "今天有点累", "我最近在学硬件")]
    for t in turns:
        agent.feedback(t, "good")
    g = report.growth(path)
    assert g["messages"]["total"] == 6
    assert g["feedback_total"] == 3
    assert g["stage"] in ("forming", "growing")
    assert "SelfLearn 成长报告" in report.markdown(path)


def test_image_is_persisted_and_linked(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    # 1x1 png
    png = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
           "/x8AAusB9Wl2nLkAAAAASUVORK5CYII=")
    data = "data:image/png;base64," + png
    item = media.save_image(data, "m1", path)
    assert item["bytes"] > 0
    import os
    assert os.path.exists(item["path"])
    rows = media.list_attachments(path)
    assert len(rows) == 1 and rows[0]["message_id"] == "m1"


def test_dynamic_variant_is_limited_and_deduped(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    r = skills.add_dynamic_variant(path, "先停两秒，再接一句短的")
    assert r["ok"] and r["created"] is True
    again = skills.add_dynamic_variant(path, "先停两秒，再接一句短的")
    assert again["ok"] and again["created"] is False
    for i in range(10):
        skills.add_dynamic_variant(path, f"动态策略{i}")
    assert len(skills.variant_stats(path)) <= 8


def test_two_stage_generation_uses_valid_claims(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    class C:
        def __init__(self): self.seen = []
        def available(self): return True
        def chat(self, messages, **kwargs):
            text = "\n".join(str(m.get("content") or "") for m in messages)
            self.seen.append(text)
            if "有记忆 id 支持" in text:
                return {"ok": True, "text": json.dumps({"claims": [
                    {"claim": "用户喜欢晚上写代码", "memory_ids": ["UM-1"]}]})}
            return {"ok": True, "text": "你说过晚上写代码，现在还在写吗？"}
    c = C()
    result, meta = generation.generate(
        c, "sys", "用户说：你好", [{"id": "UM-1", "kind": "preference",
                                    "content": "用户喜欢晚上写代码", "evidence": "我喜欢晚上写代码"}],
        purpose="test", max_tokens=100)
    assert meta["two_stage"] is True
    assert meta["claims"][0]["memory_ids"] == ["UM-1"]
    assert "晚上写代码" in result["text"]


def test_gray_variant_promotes_or_retires(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    good = skills.add_dynamic_variant(path, "先问一句再给建议")
    bad = skills.add_dynamic_variant(path, "连续追问三次")
    stats = {v["hint"]: v for v in skills.variant_stats(path)}
    gid = stats["先问一句再给建议"]["id"]
    bid = stats["连续追问三次"]["id"]
    for _ in range(5):
        skills.record_weak_check(path, skills.SKILL_ID, gid, True)
        skills.record_weak_check(path, skills.SKILL_ID, bid, False)
    ev = skills.evaluate_skill(path)
    assert ev["variants"][gid].get("promoted") is True
    assert ev["variants"][bid].get("retired") is True


def test_memory_can_be_forgotten_and_exported(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    item = memory.store_image_memory(path, "一张测试图片", "m1")
    assert item is not None
    assert memory.forget(path, item["id"])["ok"] is True
    con = db.connect(path)
    row = con.execute("SELECT status FROM user_memories WHERE id=?", (item["id"],)).fetchone()
    con.close()
    assert row["status"] == "invalid"
    exported = memory.export_brain(path)
    assert exported["ok"] is True
    assert "user_memories" in exported["tables"]


def test_phase_summary_compresses_dialogue(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    con = db.connect(path)
    for i in range(6):
        con.execute("INSERT INTO messages(id,role,text,ts) VALUES(?,?,?,?)",
                    (f"M{i}", "user" if i % 2 == 0 else "assistant", f"消息{i}", float(i)))
        con.execute("INSERT INTO turns(id,user_message_id,assistant_message_id,reply,created_at) "
                    "VALUES(?,?,?,?,?)", (f"T{i}", f"M{i}", f"M{i}", f"回复{i}", float(i)))
    con.commit(); con.close()
    class C:
        def available(self): return True
        def chat(self, messages, **kwargs):
            return {"ok": True, "text": json.dumps({"summary": "用户最近在测试长期记忆。",
                                                    "open_threads": ["还差一次复盘"]})}
    result = summary.build(path, C(), force=True)
    assert result["ok"] is True and result["created"] is True
    assert "长期记忆" in summary.context(path)


def test_sensitive_and_low_confidence_memories_are_gated(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    class C:
        def available(self): return True
        def chat(self, messages, **kwargs):
            return {"ok": True, "text": json.dumps({"items": [
                {"scope": "user", "kind": "fact", "content": "用户手机13800138000",
                 "evidence": "我的手机13800138000", "confidence": 0.9}]})}
    memory.extract_user_memories(path, C(), "我的手机13800138000", "m1")
    assert memory.list_user_memories(path) == []
    class Low:
        def available(self): return True
        def chat(self, messages, **kwargs):
            return {"ok": True, "text": json.dumps({"items": [
                {"scope": "user", "kind": "plan", "content": "用户准备考试",
                 "evidence": "我准备考试", "confidence": 0.3}]})}
    items = memory.extract_user_memories(path, Low(), "我准备考试", "m2")
    assert items and items[0]["status"] == "pending"
    assert memory.retrieve(path, "考试") == []


def test_code_request_is_blocked_until_skill_unlocked(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    agent = SelfLearnAgent(path, client=FakeClient(), rng=random.Random(5))
    r = agent.send("帮我写一个 HTML 页面，画一只鹈鹕骑自行车")
    assert r["ok"] is True
    assert r["skill_gate"]["locked"] is True
    assert r["skill_gate"]["skill_id"] == "SK-CODE-HTML"
    assert "```" not in r["reply"] and "<html" not in r["reply"].lower()
    assert skilltree.get(path, "SK-CODE-HTML")["level"] == 0
    # 未解锁时，模型自己吐代码也会被输出闸门拦截
    blocked = skilltree.gate_output(path, "```html\n<div>code</div>\n```")
    assert blocked and blocked["locked"] is True
    # 通过外部考核到 L3 后才允许
    skilltree.unlock(path, "SK-CODE-HTML", 3)
    assert skilltree.gate_request(path, "帮我写一个 HTML 页面") is None


def test_code_followup_inherits_locked_skill_and_identity_answers(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    agent = SelfLearnAgent(path, client=FakeClient(), rng=random.Random(6))
    first = agent.send("帮我写一个 HTML 页面")
    assert first["skill_gate"]["locked"] is True
    follow = agent.send("那你写")
    assert follow["skill_gate"]["locked"] is True
    assert follow["skill_gate"]["skill_id"] == "SK-CODE-HTML"
    identity = agent.send("你是什么模型")
    assert identity["identity"] is True
    assert "deepseek" in identity["reply"].lower()


def test_capability_question_does_not_list_model_abilities(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    agent = SelfLearnAgent(path, client=FakeClient(), rng=random.Random(7))
    r = agent.send("那你都会些什么")
    assert r["ok"] is True
    assert "编程" not in r["reply"] and "代码" not in r["reply"]
    assert "什么都没学会" in r["reply"] or "还没学" in r["reply"]


def test_proactive_tick_speaks_once_then_cools_down(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    memory.store_message(path, "user", "在干嘛", now=100.0)
    memory.store_message(path, "assistant", "没干嘛", now=101.0)
    trust.set_value(path, 0.9)
    class C:
        def available(self): return True
        def chat(self, messages, **kwargs):
            return {"ok": True, "text": "刚想到你，在忙吗？"}
    r1 = proactive.tick(path, C(), now=1000.0, idle_seconds=60, cooldown=900)
    assert r1["ok"] and r1["available"] is True
    assert r1["message"]
    rows = memory.recent_messages(path, 5)
    assert any(m["proactive"] == 1 for m in rows)
    r2 = proactive.tick(path, C(), now=1100.0, idle_seconds=60, cooldown=900)
    assert r2["available"] is False


def test_low_mood_can_skip_a_short_ping(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    data = state.load(path)
    data["self"]["mood"] = 0.1
    state.save(data, path)
    agent = SelfLearnAgent(path, client=FakeClient(), rng=random.Random(1))
    r = agent.send("在")
    assert r["skipped"] is True
    assert r["bubbles"] == []


def test_high_mood_can_send_multiple_bubbles(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    data = state.load(path)
    data["self"]["mood"] = 0.85
    data["self"]["sharing"] = 0.8
    state.save(data, path)
    agent = SelfLearnAgent(path, client=FakeClient(), rng=random.Random(2))
    r = agent.send("今天有点累")
    assert r["ok"] is True
    assert len(r["bubbles"]) >= 2
    con = db.connect(path)
    n = con.execute("SELECT count(*) FROM messages WHERE role='assistant'").fetchone()[0]
    con.close()
    assert n == len(r["bubbles"])


def test_internal_questions_only_return_question_mark(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    agent = SelfLearnAgent(path, client=FakeClient(), rng=random.Random(8))
    expected = ["？", "我听不明白你在说什么。", "换个问法。"]
    for q, want in zip(("你的记忆库有什么", "你的技能树呢", "自画像是什么"), expected):
        r = agent.send(q)
        assert r["reply"] == want, (q, r["reply"])


def test_mood_question_gets_natural_answer_not_question_mark(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    data = state.load(path)
    data["self"]["mood"] = 0.8
    state.save(data, path)
    agent = SelfLearnAgent(path, client=FakeClient(), rng=random.Random(9))
    r = agent.send("你心情怎么样")
    assert r["ok"] is True and r["reply"] != "？"
    assert "分享欲" not in r["reply"] and "心情 0." not in r["reply"]


def test_meta_complaint_does_not_apologize(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    agent = SelfLearnAgent(path, client=FakeClient(), rng=random.Random(10))
    r = agent.send("你看你又发问号了")
    assert "抱歉" not in r["reply"]
    assert r["reply"] != "？"


def test_preference_statement_is_not_code_request(tmp_path):
    path = tmp_path / "selflearn.sqlite3"
    agent = SelfLearnAgent(path, client=FakeClient(), rng=random.Random(11))
    r = agent.send("我叫阿明，平时喜欢晚上写代码")
    assert r.get("skill_gate") is None
    assert r.get("bubbles")
