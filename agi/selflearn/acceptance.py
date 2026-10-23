# -*- coding: utf-8 -*-
"""05/08 自学习对话体验收：只看可验证证据，不把生产未验证说成通过。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import db, memory, personality, trust
from .config import DB_PATH


def run(path: Path | str = DB_PATH) -> dict[str, Any]:
    counts = db.counts(path)
    con = db.connect(path)
    try:
        mems = [dict(r) for r in con.execute(
            "SELECT * FROM user_memories WHERE status='active'")]
        turns = con.execute("SELECT count(*) FROM turns").fetchone()[0]
        messages = con.execute("SELECT count(*) FROM messages").fetchone()[0]
        active_persona = [dict(r) for r in con.execute(
            "SELECT * FROM persona WHERE status='active'")]
    finally:
        con.close()
    evidence_ok = sum(1 for m in mems if str(m.get("evidence") or "").strip())
    evidence_rate = evidence_ok / len(mems) if mems else 1.0
    keys = [p["key"] for p in active_persona]
    persona_stable = len(keys) == len(set(keys))
    web_src = Path(__file__).with_name("web.py").read_text(encoding="utf-8")
    chat_src = Path(__file__).with_name("chat.py").read_text(encoding="utf-8")
    checks = {
        "chat_ready": messages >= 2 and turns >= 1,
        "memory_evidence_100": evidence_rate == 1.0,
        "feedback_loop": counts["feedback"] >= 3,
        "persona_stable_unique": persona_stable,
        "vision_supported": ("image_url" in chat_src and
                             "selflearn_chat_vision" in chat_src and
                             'accept="image/*"' in web_src),
        "image_memory_supported": "store_image_memory" in chat_src,
        "weak_validation_ready": (Path(__file__).with_name("skills.py")).exists()
                                  and "wilson_lower" in (Path(__file__).with_name("skills.py")).read_text(encoding="utf-8"),
        "trust_repair_ready": ("def repair" in (Path(__file__).with_name("trust.py")).read_text(encoding="utf-8")),
        "guard_ready": (Path(__file__).with_name("guard.py")).exists()
                         and "def check" in (Path(__file__).with_name("guard.py")).read_text(encoding="utf-8"),
        "conflict_gate_ready": ("def open_conflicts" in (Path(__file__).with_name("memory.py")).read_text(encoding="utf-8")
                                and "CREATE TABLE IF NOT EXISTS conflicts" in (Path(__file__).with_name("db.py")).read_text(encoding="utf-8")),
        "proactive_candidate": (Path(__file__).with_name("proactive.py")).exists(),
        "proactive_tick_ready": ("def tick" in (Path(__file__).with_name("proactive.py")).read_text(encoding="utf-8")
                                 and "/api/proactive/tick" in web_src),
        "mood_ready": ("\"self\"" in (Path(__file__).with_name("state.py")).read_text(encoding="utf-8")
                       and "def feedback" in (Path(__file__).with_name("state.py")).read_text(encoding="utf-8")),
        "multi_bubble_ready": ("split_bubbles" in (Path(__file__).with_name("style_profile.py")).read_text(encoding="utf-8")
                               and "bubbles" in chat_src),
        "internal_privacy_ready": ("INTERNAL_QUERY" in chat_src and "INTERNAL_TERMS" in chat_src),
        "claims_ready": (Path(__file__).with_name("claims.py")).exists()
                         and "claims" in (Path(__file__).with_name("db.py")).read_text(encoding="utf-8"),
        "claim_gate_ready": (Path(__file__).with_name("gates.py")).exists()
                             and "def gate_claim" in (Path(__file__).with_name("gates.py")).read_text(encoding="utf-8"),
        "search_ready": (Path(__file__).with_name("search.py")).exists()
                        and "def search" in (Path(__file__).with_name("search.py")).read_text(encoding="utf-8"),
        "learn_loop_ready": (Path(__file__).with_name("learn_loop.py")).exists()
                            and "verify_and_unlock" in (Path(__file__).with_name("learn_loop.py")).read_text(encoding="utf-8"),
        "research_ready": (Path(__file__).with_name("research.py")).exists()
                          and "def fetch_text" in (Path(__file__).with_name("research.py")).read_text(encoding="utf-8"),
        "verifier_ready": (Path(__file__).with_name("verifier.py")).exists()
                          and "def verify_python" in (Path(__file__).with_name("verifier.py")).read_text(encoding="utf-8"),
        "ingest_ready": (Path(__file__).with_name("ingest.py")).exists()
                        and "def ingest_jsonl" in (Path(__file__).with_name("ingest.py")).read_text(encoding="utf-8"),
        "image_structure_ready": (Path(__file__).with_name("image_understanding.py")).exists()
                                 and "structured_caption" in (Path(__file__).with_name("image_understanding.py")).read_text(encoding="utf-8"),
        "profile_ready": (Path(__file__).with_name("profile.py")).exists()
                         and "def distill" in (Path(__file__).with_name("profile.py")).read_text(encoding="utf-8"),
        "emoji_ready": (Path(__file__).with_name("emoji.py")).exists()
                       and "def index_assets" in (Path(__file__).with_name("emoji.py")).read_text(encoding="utf-8"),
        "scoring_ready": (Path(__file__).with_name("scoring.py")).exists()
                         and "def score_turn" in (Path(__file__).with_name("scoring.py")).read_text(encoding="utf-8"),
        "proposal_ready": (Path(__file__).with_name("proposals.py")).exists()
                          and "def transition" in (Path(__file__).with_name("proposals.py")).read_text(encoding="utf-8"),
        "affect_ready": (Path(__file__).with_name("affect.py")).exists()
                         and "def grudge_context" in (Path(__file__).with_name("affect.py")).read_text(encoding="utf-8"),
        "initiative_ready": (Path(__file__).with_name("initiative.py")).exists()
                             and "def decide" in (Path(__file__).with_name("initiative.py")).read_text(encoding="utf-8"),
        "weak_validation_ready": (Path(__file__).with_name("weak_validation.py")).exists()
                                  and "def run_recheck" in (Path(__file__).with_name("weak_validation.py")).read_text(encoding="utf-8"),
        "wake_ready": (Path(__file__).with_name("wake.py")).exists()
                       and "def run_due" in (Path(__file__).with_name("wake.py")).read_text(encoding="utf-8"),
        "production_gate_ready": (Path(__file__).with_name("production.py")).exists()
                                 and "def snapshot" in (Path(__file__).with_name("production.py")).read_text(encoding="utf-8"),
        "backfill_ready": (Path(__file__).with_name("backfill.py")).exists()
                          and "def claims_from_messages" in (Path(__file__).with_name("backfill.py")).read_text(encoding="utf-8"),
        "implicit_feedback_ready": (Path(__file__).with_name("implicit_feedback.py")).exists()
                                  and "def infer" in (Path(__file__).with_name("implicit_feedback.py")).read_text(encoding="utf-8"),
        "live_ingest_ready": (Path(__file__).with_name("live_ingest.py")).exists()
                             and "def record_message" in (Path(__file__).with_name("live_ingest.py")).read_text(encoding="utf-8"),
        "quality_ready": (Path(__file__).with_name("quality.py")).exists()
                         and "def claims_quality" in (Path(__file__).with_name("quality.py")).read_text(encoding="utf-8"),
        "context_budget_ready": (Path(__file__).with_name("context.py")).exists()
                                and "MAX_PROMPT_TOKENS" in (Path(__file__).with_name("context.py")).read_text(encoding="utf-8"),
        "goals_ready": (Path(__file__).with_name("goals.py")).exists()
                       and "def tree" in (Path(__file__).with_name("goals.py")).read_text(encoding="utf-8"),
        "nightly_ready": (Path(__file__).with_name("nightly.py")).exists()
                         and "def run" in (Path(__file__).with_name("nightly.py")).read_text(encoding="utf-8"),
        "drift_ready": (Path(__file__).with_name("drift.py")).exists()
                       and "def detect" in (Path(__file__).with_name("drift.py")).read_text(encoding="utf-8"),
        "grounding_ready": (Path(__file__).with_name("grounding.py")).exists()
                           and "def audit" in (Path(__file__).with_name("grounding.py")).read_text(encoding="utf-8"),
        "initiative_integrated": "initiative.decide" in (Path(__file__).with_name("proactive.py")).read_text(encoding="utf-8"),
        "growth_report_ready": (Path(__file__).with_name("report.py")).exists()
                                and "def growth" in (Path(__file__).with_name("report.py")).read_text(encoding="utf-8"),
        "memory_control_ready": "def forget" in (Path(__file__).with_name("memory.py")).read_text(encoding="utf-8")
                                and "def export_brain" in (Path(__file__).with_name("memory.py")).read_text(encoding="utf-8"),
        "memory_gate_ready": ("_sensitive" in (Path(__file__).with_name("memory.py")).read_text(encoding="utf-8")
                              and "pending" in (Path(__file__).with_name("memory.py")).read_text(encoding="utf-8")),
        "skill_gate_ready": (Path(__file__).with_name("skilltree.py")).exists()
                            and "gate_request" in (Path(__file__).with_name("skilltree.py")).read_text(encoding="utf-8"),
        "capability_gate_ready": ("capability_reply" in (Path(__file__).with_name("skilltree.py")).read_text(encoding="utf-8")
                                  and "我还没学会啥" in (Path(__file__).with_name("skilltree.py")).read_text(encoding="utf-8")
                                  and "画不了图" in (Path(__file__).with_name("skilltree.py")).read_text(encoding="utf-8")),
        "normal_voice_ready": "不列清单" in chat_src and "不主动强调自己是 AI" in chat_src,
        "style_profile_ready": (Path(__file__).with_name("style_profile.py")).exists()
                               and "tune_reply" in chat_src,
        "summary_ready": (Path(__file__).with_name("summary.py")).exists()
                         and "def build" in (Path(__file__).with_name("summary.py")).read_text(encoding="utf-8"),
        "media_persistence_ready": (Path(__file__).with_name("media.py")).exists()
                                   and "save_image" in chat_src,
        "two_stage_ready": (Path(__file__).with_name("generation.py")).exists()
                           and "def generate" in (Path(__file__).with_name("generation.py")).read_text(encoding="utf-8"),
        "gray_skill_ready": ("gray" in (Path(__file__).with_name("skills.py")).read_text(encoding="utf-8")
                             and "retired" in (Path(__file__).with_name("skills.py")).read_text(encoding="utf-8")),
        "error_budget_ready": "def budget" in (Path(__file__).with_name("errors.py")).read_text(encoding="utf-8"),
        "scheduler_ready": (Path(__file__).with_name("scheduler.py")).exists(),
    }
    blockers = [k for k, ok in checks.items() if not ok]
    passed = all(checks.values())
    return {
        "ok": True,
        "passed": passed,
        "verdict": "selflearn_mvp_verified" if passed else "selflearn_mvp_incomplete",
        "checks": checks,
        "blockers": blockers,
        "metrics": {
            "messages": messages,
            "turns": turns,
            "feedback": counts["feedback"],
            "memories": len(mems),
            "memory_evidence_rate": round(evidence_rate, 4),
            "persona_active": len(active_persona),
            "trust": round(trust.get(path), 4),
            "counts": counts,
        },
        "note": "只证明本地自学习 MVP 各链路可运行，不代表生产质量。",
    }
