# -*- coding: utf-8 -*-
"""selflearn CLI。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import acceptance, db, emoji, errors, image_understanding, ingest, learn_loop, media, memory, personality, proactive, profile, proposals, report, research, scheduler, scoring, skills, state, summary, trust
from .chat import SelfLearnAgent
from .config import DB_PATH
from .llm import Client


def _out(data):
    try:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    except UnicodeEncodeError:
        print(json.dumps(data, ensure_ascii=True, indent=2, default=str))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agi.selflearn.cli")
    parser.add_argument("--db", default=str(DB_PATH))
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_chat = sub.add_parser("chat"); p_chat.add_argument("text", nargs="?")
    sub.add_parser("status")
    p_hist = sub.add_parser("history"); p_hist.add_argument("--limit", type=int, default=30)
    p_mem = sub.add_parser("memories"); p_mem.add_argument("--limit", type=int, default=30)
    sub.add_parser("persona"); sub.add_parser("skills"); sub.add_parser("reflect")
    p_fb = sub.add_parser("feedback")
    p_fb.add_argument("turn_id"); p_fb.add_argument("reaction", choices=["good", "ok", "bad"])
    p_fb.add_argument("--note", default="")
    p_serve = sub.add_parser("serve"); p_serve.add_argument("--port", type=int, default=8082)
    sub.add_parser("state"); sub.add_parser("trust"); sub.add_parser("proactive")
    sub.add_parser("errors"); sub.add_parser("acceptance")
    sub.add_parser("conflicts")
    sub.add_parser("media")
    p_sum = sub.add_parser("summarize"); p_sum.add_argument("--force", action="store_true")
    sub.add_parser("summary")
    p_forget = sub.add_parser("forget"); p_forget.add_argument("memory_id")
    p_report = sub.add_parser("report")
    p_report.add_argument("--markdown", action="store_true")
    p_resolve = sub.add_parser("resolve-conflict")
    p_resolve.add_argument("conflict_id"); p_resolve.add_argument("winner_id")
    p_intent = sub.add_parser("intent")
    p_intent.add_argument("action", choices=["add", "due", "list"])
    p_intent.add_argument("--kind", default="check_in")
    p_intent.add_argument("--text", default="")
    p_intent.add_argument("--after", type=float, default=3600)
    p_learn_status = sub.add_parser("learn-status")
    p_learn_status.add_argument("--skill", default="")
    p_learn_gap = sub.add_parser("learn-gap")
    p_learn_gap.add_argument("--skill", required=True); p_learn_gap.add_argument("--goal", default="")
    p_learn_source = sub.add_parser("learn-source")
    p_learn_source.add_argument("--skill", required=True)
    p_learn_source.add_argument("--url", required=True)
    p_learn_source.add_argument("--keyword", default="")
    p_learn_source.add_argument("--quote", default="")
    p_learn_source.add_argument("--title", default="")
    p_learn_card = sub.add_parser("learn-card")
    p_learn_card.add_argument("--skill", required=True)
    p_learn_card.add_argument("--content", required=True)
    p_learn_card.add_argument("--example", default="")
    p_learn_practice = sub.add_parser("learn-practice")
    p_learn_practice.add_argument("--task", required=True)
    p_learn_practice.add_argument("--passed", action="store_true")
    p_learn_practice.add_argument("--failed", action="store_true")
    p_learn_practice.add_argument("--attempt", default="")
    p_learn_practice.add_argument("--error", default="")
    p_learn_verify = sub.add_parser("learn-verify")
    p_learn_verify.add_argument("--task", required=True)
    p_learn_verify.add_argument("--passed", action="store_true")
    p_learn_verify.add_argument("--note", default="")
    p_learn_fetch = sub.add_parser("learn-fetch")
    p_learn_fetch.add_argument("--url", required=True); p_learn_fetch.add_argument("--keyword", default="")
    p_ingest = sub.add_parser("ingest-jsonl")
    p_ingest.add_argument("--file", required=True); p_ingest.add_argument("--source", default="jsonl")
    p_sessions = sub.add_parser("sessions"); p_sessions.add_argument("--friend", default="")
    p_segment = sub.add_parser("segment")
    p_segment.add_argument("--session", required=True); p_segment.add_argument("--max-chars", type=int, default=1200)
    sub.add_parser("ingest-stats")
    p_img = sub.add_parser("image-describe")
    p_img.add_argument("--path", required=True); p_img.add_argument("--attachment", default="")
    p_img.add_argument("--caption", default="")
    sub.add_parser("profile")
    sub.add_parser("profile-distill")
    p_emoji_index = sub.add_parser("emoji-index"); p_emoji_index.add_argument("--dir", required=True)
    sub.add_parser("emoji-stats"); sub.add_parser("emoji-list")
    p_emoji_use = sub.add_parser("emoji-use"); p_emoji_use.add_argument("--name", required=True)
    p_props = sub.add_parser("proposals"); p_props.add_argument("--status", default="")
    p_trans = sub.add_parser("proposal-transition")
    p_trans.add_argument("proposal_id"); p_trans.add_argument("action")
    p_bucket = sub.add_parser("score-bucket")
    p_bucket.add_argument("--stage", default="陌生"); p_bucket.add_argument("--mood", default="neutral")
    p_bucket.add_argument("--topic", default="生活")
    args = parser.parse_args(argv)
    path = Path(args.db)
    if args.cmd == "serve":
        from . import web
        return web.main(["--db", str(path), "--port", str(args.port)])
    agent = SelfLearnAgent(path)
    if args.cmd == "chat":
        if args.text:
            _out(agent.send(args.text)); return 0
        while True:
            try: text = input("你：").strip()
            except EOFError: break
            if text in ("/exit", "/quit"): break
            if not text: continue
            r = agent.send(text)
            print("AI：", r.get("reply") or r.get("error"))
        return 0
    if args.cmd == "status":
        _out({"ok": True, "db": str(path), "counts": db.counts(path),
              "persona": personality.list_persona(path),
              "variants": skills.variant_stats(path)}); return 0
    if args.cmd == "history":
        con = db.connect(path)
        try:
            rows = [dict(r) for r in con.execute(
                "SELECT role,text,ts FROM messages ORDER BY ts DESC,id DESC LIMIT ?",
                (args.limit,))]
        finally: con.close()
        _out(rows[::-1]); return 0
    if args.cmd == "memories":
        _out(memory.list_user_memories(path, args.limit)); return 0
    if args.cmd == "persona":
        _out(personality.list_persona(path)); return 0
    if args.cmd == "skills":
        _out(skills.variant_stats(path)); return 0
    if args.cmd == "reflect":
        _out(personality.reflect(path, Client())); return 0
    if args.cmd == "feedback":
        _out(agent.feedback(args.turn_id, args.reaction, args.note)); return 0
    if args.cmd == "state":
        _out(state.load(path)); return 0
    if args.cmd == "trust":
        _out(trust.snapshot(path)); return 0
    if args.cmd == "proactive":
        _out(proactive.propose(path)); return 0
    if args.cmd == "errors":
        _out(errors.list_errors(path)); return 0
    if args.cmd == "acceptance":
        _out(acceptance.run(path)); return 0
    if args.cmd == "conflicts":
        _out(memory.open_conflicts(path)); return 0
    if args.cmd == "media":
        _out(media.list_attachments(path)); return 0
    if args.cmd == "forget":
        _out(memory.forget(path, args.memory_id)); return 0
    if args.cmd == "summary":
        _out(summary.latest(path)); return 0
    if args.cmd == "summarize":
        _out(summary.build(path, Client(), force=args.force)); return 0
    if args.cmd == "report":
        if args.markdown:
            print(report.markdown(path))
        else:
            _out(report.growth(path))
        return 0
    if args.cmd == "resolve-conflict":
        _out(memory.resolve_conflict(path, args.conflict_id, args.winner_id)); return 0
    if args.cmd == "ingest-jsonl":
        _out(ingest.ingest_jsonl(path, args.file, source=args.source)); return 0
    if args.cmd == "sessions":
        _out(ingest.build_sessions(path, friend=args.friend or None)); return 0
    if args.cmd == "segment":
        _out(ingest.segment_session(path, args.session, max_chars=args.max_chars)); return 0
    if args.cmd == "ingest-stats":
        _out(ingest.stats(path)); return 0
    if args.cmd == "emoji-index":
        _out(emoji.index_assets(path, args.dir)); return 0
    if args.cmd == "emoji-stats":
        _out(emoji.stats(path)); return 0
    if args.cmd == "emoji-list":
        _out(emoji.list_emojis(path)); return 0
    if args.cmd == "emoji-use":
        _out(emoji.record_use(path, args.name)); return 0
    if args.cmd == "proposals":
        _out(proposals.list_proposals(path, args.status or None)); return 0
    if args.cmd == "proposal-transition":
        _out(proposals.transition(path, args.proposal_id, args.action)); return 0
    if args.cmd == "score-bucket":
        _out({"ok": True, "bucket": scoring.bucket_of(stage=args.stage, mood=args.mood, topic=args.topic),
              "buckets": scoring.buckets()}); return 0
    if args.cmd == "image-describe":
        _out(image_understanding.describe_image(args.path, Client(), args.attachment,
                                                 args.caption, db_path=path)); return 0
    if args.cmd == "profile":
        _out({"ok": True, "profile": profile.get_profile(path),
              "render": profile.render_profile(path)}); return 0
    if args.cmd == "profile-distill":
        _out(profile.distill(path, Client())); return 0
    if args.cmd == "learn-status":
        _out(learn_loop.status(path, args.skill or None)); return 0
    if args.cmd == "learn-gap":
        _out({"ok": True, "task_id": learn_loop.open_gap(path, args.skill, args.goal)}); return 0
    if args.cmd == "learn-source":
        fetched = research.fetch_text(args.url)
        if not fetched.get("ok"):
            _out(fetched); return 1
        quote = args.quote or research.extract_quote(fetched.get("text", ""), args.keyword)
        if not quote:
            _out({"ok": False, "error": "没有可用 quote；给 --quote 或 --keyword"}); return 1
        _out(learn_loop.record_source(path, args.skill, fetched.get("url") or args.url,
                                       args.title or fetched.get("title", ""), quote)); return 0
    if args.cmd == "learn-card":
        _out(learn_loop.write_card(path, args.skill, args.content, args.example)); return 0
    if args.cmd == "learn-practice":
        passed = bool(args.passed) and not bool(args.failed)
        _out(learn_loop.record_practice(path, args.task, args.attempt or "manual", passed,
                                        args.error)); return 0
    if args.cmd == "learn-verify":
        _out(learn_loop.verify_and_unlock(path, args.task, bool(args.passed), args.note)); return 0
    if args.cmd == "learn-fetch":
        fetched = research.fetch_text(args.url)
        if fetched.get("ok") and args.keyword:
            fetched["quote"] = research.extract_quote(fetched.get("text", ""), args.keyword)
        _out(fetched); return 0 if fetched.get("ok") else 1
    if args.cmd == "intent":
        if args.action == "add":
            wake = __import__("time").time() + args.after
            _out({"ok": True, "id": scheduler.add(path, args.kind, args.text or "check in", wake)})
        elif args.action == "due":
            _out(scheduler.due(path))
        else:
            _out(scheduler.list_intents(path))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
