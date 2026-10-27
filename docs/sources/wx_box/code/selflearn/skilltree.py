# -*- coding: utf-8 -*-
"""06 技能树与闸门：初始技能 locked，未到 L3 不交付代码。"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from . import db
from .config import DB_PATH

NODES = (
    ("SK-CODE-HTML", "手写 HTML/CSS 页面", 0),
    ("SK-CODE-PY", "编写 Python 脚本", 0),
)
CODE_PATTERNS = (
    re.compile(r"```(?:html|css|js|javascript|python|py)?", re.I),
    re.compile(r"<!doctype\s+html|<html[\s>]|<div[\s>]|<canvas[\s>]", re.I),
    re.compile(r"\b(function|const|let|def)\s+\w+\s*\(", re.I),
    re.compile(r"<style[\s>]|<script[\s>]", re.I),
)
HTML_REQUEST = re.compile(r"(html|css|网页|页面|前端|界面|按钮|布局|动画)", re.I)
PY_REQUEST = re.compile(r"(python|脚本|函数|程序|代码|编程|javascript|js)", re.I)
WRITE_REQUEST = re.compile(r"(写|做|生成|实现|帮我|给我|改)", re.I)
DELIVERABLE_HINT = re.compile(r"(完整|可运行|代码|脚本|文件|页面|界面|程序)", re.I)


def ensure_nodes(path: Path | str = DB_PATH) -> None:
    con = db.connect(path)
    try:
        for sid, name, level in NODES:
            con.execute(
                "INSERT OR IGNORE INTO skill_nodes(id,name,level,status,provenance,"
                "practice_log,fail_count,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (sid, name, level, "locked", "[]", "[]", 0, time.time()))
        con.commit()
    finally:
        con.close()


CAP_QUERY = re.compile(r"(你会什么|都会什么|会些什么|能做什么|有什么能力|会做什么|都会哪些|会哪些)")
IMAGE_QUERY = re.compile(r"(生成|画|做|弄).{0,4}(图|图片|图像)|(图片|图像).{0,4}(吗|能|会)")
CODE_QUERY = re.compile(r"(写|做|编).{0,4}(代码|程序)|编程|Python|HTML|JS|JavaScript", re.I)
CODE_ABILITY_QUERY = re.compile(r"(你会|你能|会不会|能不能).{0,8}(写|做|编).{0,4}(代码|程序)|(会|能).{0,4}(编程|写代码|做程序)")

IMAGE_REPLIES = (
    "不会，我画不了图，只能看图和聊天。",
    "图我画不出来，只能看。",
    "还不会生成图，只能聊聊。",
)
CODE_REPLIES = (
    "写完整代码还没解锁。报错和思路可以一起看。",
    "代码交付还不行；你把报错贴来，我帮你看。",
    "完整脚本写不了，但报错我能看。",
)
GENERIC_REPLIES = (
    "我还没学会啥，现在就会聊天。你想聊啥？",
    "真不会什么，只会跟你瞎聊。",
    "现在啥也不会，你教我也行。",
    "我还在学，暂时就会说话。",
)


def _recent_replies(path: Path | str, needles) -> list[str]:
    con = db.connect(path)
    try:
        rows = con.execute(
            "SELECT text FROM messages WHERE role='assistant' ORDER BY ts DESC LIMIT 40"
        ).fetchall()
    finally:
        con.close()
    out = []
    for row in rows:
        text = str(row["text"] or "")
        if any(n in text for n in needles):
            out.append(text)
    return out


def _pick(replies, count: int) -> str:
    return replies[count % len(replies)]


def capability_reply(path: Path | str = DB_PATH, text: str = "") -> str | None:
    """能力问题按具体情况口语回答；重复问会换说法。"""
    value = str(text or "")
    # 这两个问题优先于通用能力问题，避免被“你会什么”路径吞掉
    if re.search(r"(怎么测试|怎么试你|如何测试|测试你)", value):
        return "随便聊，或者发张图给我看看。答不上来我会说。"
    if re.search(r"(你的边界|边界在哪|有什么边界|能做什么不能)", value):
        return "能读字、看图、看报错；完整代码和脚本还没解锁。"
    if not (CAP_QUERY.search(value) or IMAGE_QUERY.search(value) or
            CODE_ABILITY_QUERY.search(value)):
        return None
    if IMAGE_QUERY.search(value):
        return _pick(IMAGE_REPLIES, len(_recent_replies(path, IMAGE_REPLIES)))
    if CODE_ABILITY_QUERY.search(value):
        return _pick(CODE_REPLIES, len(_recent_replies(path, CODE_REPLIES)))
    unlocked = [n for n in list_nodes(path) if int(n["level"]) >= 3 and n["status"] != "locked"]
    if not unlocked:
        return _pick(GENERIC_REPLIES, len(_recent_replies(path, GENERIC_REPLIES)))
    return "我现在会一点：" + "、".join(n["name"] for n in unlocked) + "。"

def detect_followup(path: Path | str, text: str) -> str | None:
    """“那你写/继续/补上”这类追问，继承上一条代码需求。"""
    value = str(text or "").strip()
    if not re.match(r"^(那|就|好|行|可以)?(你)?(写|做|来|继续|补上|开始|写吧)", value):
        return None
    con = db.connect(path)
    try:
        rows = con.execute(
            "SELECT text FROM messages WHERE role='user' ORDER BY ts DESC LIMIT 6").fetchall()
    finally:
        con.close()
    context = "\n".join(str(r["text"]) for r in rows)
    if HTML_REQUEST.search(context) or "HTML" in context:
        return "SK-CODE-HTML"
    if PY_REQUEST.search(context) and DELIVERABLE_HINT.search(context):
        return "SK-CODE-PY"
    return None


def detect_skill(text: str) -> str | None:
    value = str(text or "")
    # “我喜欢/平时写代码”“记得我写代码”是偏好陈述，不是交付请求
    if re.search(r"(喜欢|平时|经常|习惯|记得|在学|想学|学过|经常|写过|谈论)", value):
        if not re.search(r"(帮我|给我|写一个|写个|生成|做一|实现|做一|画一)", value):
            return None
    if HTML_REQUEST.search(value) and WRITE_REQUEST.search(value):
        return "SK-CODE-HTML"
    if PY_REQUEST.search(value) and WRITE_REQUEST.search(value) and DELIVERABLE_HINT.search(value):
        return "SK-CODE-PY"
    return None


def get(path: Path | str, skill_id: str) -> dict | None:
    ensure_nodes(path)
    con = db.connect(path)
    try:
        row = con.execute("SELECT * FROM skill_nodes WHERE id=?", (skill_id,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def list_nodes(path: Path | str = DB_PATH) -> list[dict]:
    ensure_nodes(path)
    con = db.connect(path)
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM skill_nodes ORDER BY id").fetchall()]
    finally:
        con.close()


def _id(prefix: str, *parts) -> str:
    return prefix + hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:14]


def create_gap(path: Path | str, skill_id: str, goal: str) -> str:
    tid = _id("LT-", skill_id, goal, time.time())
    con = db.connect(path)
    try:
        con.execute("INSERT INTO learn_tasks(id,skill_id,goal,status,attempts,last_error,created_at) "
                    "VALUES(?,?,?,?,?,?,?)", (tid, skill_id, goal[:500], "open", 0, "", time.time()))
        con.commit()
    finally:
        con.close()
    return tid


LOCKED_REPLIES = (
    "完整代码/页面我还没解锁。需求可以说，报错也可以贴，我帮你看原因。",
    "写成品还不行；你先说说想做成什么样，或者把报错发来。",
    "这个交付还没解锁，但思路和报错我能一起看。",
)


def missing_message(path: Path | str, skill_id: str) -> str:
    count = len(_recent_replies(path, ("还没学会", "还没学", "代码暂时写不了")))
    return _pick(LOCKED_REPLIES, count)


def gate_request(path: Path | str, text: str) -> dict | None:
    skill_id = detect_skill(text) or detect_followup(path, text)
    if not skill_id:
        return None
    node = get(path, skill_id)
    if node and int(node["level"]) >= 3 and node["status"] != "locked":
        return None
    tid = create_gap(path, skill_id, text)
    return {"locked": True, "skill_id": skill_id, "task_id": tid,
            "level": int(node["level"]) if node else 0,
            "message": missing_message(path, skill_id)}


def gate_output(path: Path | str, reply: str) -> dict | None:
    """模型仍然可能自己吐代码；没有 L3 就把代码交付替换掉。"""
    if not any(p.search(str(reply or "")) for p in CODE_PATTERNS):
        return None
    for node in list_nodes(path):
        if node["status"] == "locked" or int(node["level"]) < 3:
            tid = create_gap(path, node["id"], "模型尝试输出代码，被输出闸门拦截")
            return {"locked": True, "skill_id": node["id"], "task_id": tid,
                    "level": int(node["level"]),
                    "message": missing_message(path, node["id"])}
    return None


def unlock(path: Path | str, skill_id: str, level: int = 3) -> dict:
    ensure_nodes(path)
    level = max(0, min(5, int(level)))
    con = db.connect(path)
    try:
        cur = con.execute("UPDATE skill_nodes SET level=?,status=?,updated_at=? WHERE id=?",
                          (level, "unlocked" if level >= 3 else "learning", time.time(), skill_id))
        con.commit()
        return {"ok": cur.rowcount > 0, "skill_id": skill_id, "level": level}
    finally:
        con.close()
