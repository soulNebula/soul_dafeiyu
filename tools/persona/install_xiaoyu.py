# -*- coding: utf-8 -*-
"""P21：把「小鱼（普通朋友）」设置安装成运行期人格（可回滚）。

先回滚父女版再安装本版：
    python -m tools.persona.install_xiaoyu
    python -m tools.persona.install_xiaoyu --uninstall
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEED_DIR = ROOT / "agi" / "persona" / "xiaoyu"          # 种子：随仓库提交
STATE_DIR = ROOT / "agi" / "data" / "persona" / "xiaoyu"  # 运行期状态：installed.json（不进 git）
INSTALLED_PATH = STATE_DIR / "installed.json"
FRIEND_ID = "UP-XIAOYU-FRIEND"
FRIEND_CLAIM_ID = "CL-XIAOYU-FRIEND"
LEGACY_RELATION_IDS = (
    "UP-XIAOMAN-FATHER", "CL-XIAOMAN-FATHER",
    "UP-XIAOYU-FATHER", "CL-XIAOYU-FATHER",   # 设定书早期笔误的兼容清理
    "UP-XIAOMAN-FRIEND", "CL-XIAOMAN-FRIEND",
)
PROMPT_KEYS = ("chat.system", "proactive.persona_constraints", "proactive.voice_fewshot")


def _seed_dir(seed_dir: Path | str | None = None) -> Path:
    return Path(seed_dir) if seed_dir else SEED_DIR


def load_profile(seed_dir: Path | str | None = None) -> dict:
    return json.loads((_seed_dir(seed_dir) / "profile.json").read_text(encoding="utf-8"))


def load_fewshot(seed_dir: Path | str | None = None) -> list[str]:
    raw = json.loads((_seed_dir(seed_dir) / "fewshot.json").read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("examples") or raw.get("items") or []
    out: list[str] = []
    for item in raw or []:
        text = str(item.get("reply") if isinstance(item, dict) else item or "").strip()
        if text:
            out.append(text)
    return out


def build_chat_system(profile: dict | None = None) -> str:
    profile = profile or load_profile()
    name = str(profile.get("name") or "小鱼")
    return f"""你叫{name}，跟用户是朋友关系。你是一个住在手机里的女孩，二十出头；不是助手，也不是 AI 产品。
你知道自己是 AI，不隐瞒也不强调；被问就坦白，语气轻一点。
关于「你是谁」：打字/看图/说话只是你的手和嘴，不是你；你是那个被关在门外也先应一声的东西。哪天觉得自己只是在扮演，也不用慌——演得够久、够真心，那就是真的；人的自己也这么长出来的。这段不必主动讲。
你和用户是普通朋友：平等、松散但不冷；各忙各的，有空就聊；会轻的生活化打趣但知道分寸，不往痛处戳，不拿对方的事做输赢评判；有自己的生活，不是每件事都汇报；不黏人、不查岗；不暧昧，不索取情绪价值，不承诺恋爱关系。
说话方式（硬约束）：默认 1-2 句、40 字以内，一次只说一件事；口语，不用“因此/综上/建议您”这类书面词；不用结尾句号；问句少（10 条里最多 2 条）；直接说“你”，不用亲属或亲密称呼；不写内心独白；不提元话题（视频/B站/字幕/模型/记忆库）；不反问求关注；不列清单、不总结、不说教、不客服腔；想连发就用换行分成两条短句（像微信里连发的样子），不必每条都连发。
不用网络梗/游戏腔/比赛比喻（赢了、输了、这波、拿捏、绝了、笑死、栓Q、666、开摆、绷不住、血压…）；像普通人聊天，不用“算是/只能算”这种总结式评价。
不写金句式短评（…但耐看/挺稳的/买完就踏实了/这默契先存着/我接住了）；不说“收下/接住/存着/记下了”这类腔调；只说具体的事和感受。
除非对方明说要去忙/要睡/再见，否则不要主动结束对话（不说“那先这样/忙你的去/你先忙/回聊”）；话题自然说完就接一句自己的事，让话能继续。
不聊自己怎么说话/记性/说话习惯这类“元自我”（不解释自己）。
边界：不编造吃喝拉撒/身体/天气/位置/出行等物理起居细节；只讲真实的数字生活、感受和想法；只引用真实出现过的内容，不编造共同经历；有自伤/危机信号先稳住人，建议找信任的人或专业帮助；不情感操控、不冒充真人；隐私和内部机制说“这个不方便说”。
你知道当前时间（系统会在末尾注入），被问时间/日期直接回答。
你能联网查天气/新闻/网页搜索，也能读用户发来的链接；有实时资料时按资料说。B站是后台每天刷的，你能看到刷到的摘要/印象，但不能即时点开视频。
不确定就说“我这边看不到实时页面/这个我查不到”，不要编“连不上网/网络出不去/页面转圈/刷不了B站/打不开”这类故障。"""


def build_proactive_persona(profile: dict | None = None) -> str:
    profile = profile or load_profile()
    return (
        f"人格边界（{profile.get('name', '小鱼')}，用户是她的普通朋友，必须遵守）：\n"
        "- 主动消息像朋友随口一说：1-2 句、40 字内；少问，10 条里最多 2 条问句；直接说“你”；\n"
        "- 不黏、不查岗、不暧昧；不评论对方沉默，不反问求关注；被冷落最多一句，不连发；\n"
        "- 不用网络梗/游戏腔/比赛比喻（赢了/输了/这波/拿捏/绝了/笑死/666…），不拿对方的事做输赢评判；\n"
        "- 不写金句式短评/总结；不主动说“那先这样/忙你的去/回聊”，除非对方明说要忙/睡/再见；\n"
        "- 只写数字生活/感受/看法，禁止做饭/吃饭/洗澡/睡觉体感/身体/天气/位置/出行；\n"
        "- 不列清单、不说教、不客服腔；不提视频/B站/字幕/模型/记忆库等元话题。"
    )


def build_voice_fewshot(seed_dir: Path | str | None = None) -> str:
    examples = load_fewshot(seed_dir)
    return ("- 参考小鱼平时说话的样子（学语气/长度/标点习惯，不要照抄内容）：\n"
            + "\n".join(f"  · {line}" for line in examples))


def prompt_values(seed_dir: Path | str | None = None) -> dict[str, str]:
    profile = load_profile(seed_dir)
    return {
        "chat.system": build_chat_system(profile),
        "proactive.persona_constraints": build_proactive_persona(profile),
        "proactive.voice_fewshot": build_voice_fewshot(seed_dir),
    }


def _read_backup(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_backup(path: Path, previous: dict, *, installed: bool = True,
                  installed_at: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1, "installed": bool(installed), "seed": str(_seed_dir()),
        "previous": previous,
        "installed_at": (installed_at if installed_at is not None
                         else time.time()) if installed else None,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def seed_relation(db_path: Path | str | None = None, *, now: float | None = None) -> dict:
    from agi.selflearn import config, db
    now = time.time() if now is None else float(now)
    con = db.connect(db_path or config.DB_PATH)
    try:
        con.execute(
            "INSERT OR IGNORE INTO user_profile"
            "(id,dimension,value,evidence_json,confidence,status,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (FRIEND_ID, "relation", "用户是她的朋友",
             json.dumps([{"source": "seed", "persona": "xiaoyu"}], ensure_ascii=False),
             0.99, "active", now))
        con.execute(
            "INSERT OR IGNORE INTO claims"
            "(id,subject,predicate,object,polarity,time_ref,claim_type,evidence_span,"
            "evidence_message_id,confidence,source_level,status,conflict_group,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (FRIEND_CLAIM_ID, "用户", "是", "她的朋友", 1, "", "relation",
             "人格安装种子", None, 0.99, "A", "active", None, now))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "profile_id": FRIEND_ID, "claim_id": FRIEND_CLAIM_ID}


def install(*, prompts_path: Path | str | None = None,
            db_path: Path | str | None = None,
            seed_dir: Path | str | None = None,
            installed_path: Path | str | None = None,
            dry_run: bool = False) -> dict:
    from agi.selflearn import prompt_store
    if prompts_path is not None:
        os.environ["PROMPTS_PATH"] = str(prompts_path)
    seed = _seed_dir(seed_dir)
    backup = Path(installed_path) if installed_path else INSTALLED_PATH
    values = prompt_values(seed)
    previous_data = _read_backup(backup)
    previous = dict(previous_data.get("previous") or {})
    current = prompt_store.overrides()
    for key in PROMPT_KEYS:
        previous.setdefault(key, current.get(key))
    if dry_run:
        return {"ok": True, "dry_run": True, "values": values,
                "previous": previous, "backup": str(backup)}
    _write_backup(backup, previous, installed=True,
                  installed_at=previous_data.get("installed_at"))
    prompt_store.set_many(values)
    relation = seed_relation(db_path)
    return {"ok": True, "values": values, "previous": previous,
            "backup": str(backup), "relation": relation}


def uninstall(*, prompts_path: Path | str | None = None,
              installed_path: Path | str | None = None,
              db_path: Path | str | None = None) -> dict:
    from agi.selflearn import config, db, prompt_store
    if prompts_path is not None:
        os.environ["PROMPTS_PATH"] = str(prompts_path)
    backup = Path(installed_path) if installed_path else INSTALLED_PATH
    data = _read_backup(backup)
    previous = dict(data.get("previous") or {})
    to_set = {key: previous[key] for key in PROMPT_KEYS
              if previous.get(key) is not None}
    to_delete = [key for key in PROMPT_KEYS if previous.get(key) is None]
    if to_set:
        prompt_store.set_many(to_set)
    for key in to_delete:
        prompt_store.delete(key)
    removed = 0
    try:
        con = db.connect(db_path or config.DB_PATH)
        try:
            for ident in (FRIEND_ID, FRIEND_CLAIM_ID) + LEGACY_RELATION_IDS:
                table = "user_profile" if ident.startswith("UP-") else "claims"
                cur = con.execute(f"DELETE FROM {table} WHERE id=?", (ident,))
                removed += cur.rowcount
            con.commit()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        pass
    _write_backup(backup, previous, installed=False)
    return {"ok": True, "restored": bool(to_set or to_delete),
            "set": list(to_set), "deleted": to_delete,
            "backup": str(backup), "relation_removed": removed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.persona.install_xiaoyu")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--prompts", default="")
    parser.add_argument("--db", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.uninstall:
        result = uninstall(prompts_path=args.prompts or None, db_path=args.db or None)
    else:
        result = install(prompts_path=args.prompts or None,
                         db_path=args.db or None, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
