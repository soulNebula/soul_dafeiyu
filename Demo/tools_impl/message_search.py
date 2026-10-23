"""全库消息检索（移植自参考项目 wechat-4x-decrypt 的 Skill 思路，直接读本机 WCDB）。

能力：
* 在所有会话里按关键词 / 时间范围 / 联系人检索历史消息
* 会话与发送者用 Name2Id + contact.db 还原成显示名
* 结果可导出为 txt（每行 `时间  联系人: 内容`）或 json

数据来源：`message_fts.db` 的 `message_fts_v4_N` 全文索引表
（`acontent` 已是解码后的纯文本，含 [微笑] 这类表情名）。

用法见 `tools/search_messages.py`。
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from wx_core import media_cache, wcdb

TYPE_LABEL = {
    1: "文字", 3: "图片", 34: "语音", 43: "视频", 47: "表情包",
    49: "文件/应用消息", 10000: "系统", 10002: "系统",
}


def _parse_day(text: str, end: bool = False) -> float | None:
    """把 YYYY-MM-DD / YYYY-MM-DD HH:MM 解析成时间戳（本地时区）。"""
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            st = time.strptime(text, fmt)
            ts = time.mktime(st)
            if fmt in ("%Y-%m-%d", "%Y/%m/%d") and end:
                ts += 86400 - 1
            return ts
        except ValueError:
            continue
    return None


def _fmt_time(ts: int) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except (ValueError, OSError, TypeError):
        return str(ts)


def _contact_names(config: dict, reader: wcdb.SessionReader) -> dict[str, str]:
    """username -> 显示名（remark 优先，其次昵称）。"""
    out: dict[str, str] = {}
    path = reader._materialize("db_storage/contact/contact.db", "_contact_path")
    if not path:
        return out
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        for u, n in con.execute(
                "select username, coalesce(nullif(remark,''), nick_name) from contact"):
            if u:
                out[u] = n or u
        con.close()
    except sqlite3.Error:
        pass
    return out


def search_messages(config: dict, keyword: str = "", since: str = "", until: str = "",
                    contact: str = "", limit: int = 100, log=None) -> list[dict]:
    """全库检索消息，返回按时间排序的命中列表。"""
    log = log or (lambda *a, **k: None)
    acc = media_cache.active_account_dir()
    if acc is None:
        raise RuntimeError("找不到微信数据目录")
    keys = wcdb.ensure_keys(acc, config, log=log)
    if not keys:
        raise RuntimeError("数据库密钥不可用（请先运行一次监听程序提取密钥）")
    wxid = str(config.get("image_wxid") or "")
    reader = wcdb.SessionReader(acc, keys, wxid)
    try:
        fts_path = reader._materialize("db_storage/message/message_fts.db", "_fts_db")
        if not fts_path:
            raise RuntimeError("无法解密 message_fts.db")
        names = _contact_names(config, reader)
        t_from = _parse_day(since)
        t_to = _parse_day(until, end=True)
        kws = [k.strip().lower() for k in re.split(r"[,，]", keyword or "") if k.strip()]
        contact = (contact or "").strip()

        con = sqlite3.connect(f"file:{fts_path}?mode=ro", uri=True)
        # 分片全文索引表：message_fts_v4_0 / _1 / ...
        tables = [r[0] for r in con.execute(
            "select name from sqlite_master where type='table' and name like 'message_fts_v4_%'")]
        # 只取正文分片 message_fts_v4_<数字>，排除 _data/_idx/_content/_aux/_range 等辅助表
        tables = [t for t in tables if re.fullmatch(r"message_fts_v4_\d+", t)]
        # name2id：rowid -> username
        id2name: dict[int, str] = {}
        try:
            for rowid, uname in con.execute("select rowid, username from name2id"):
                id2name[int(rowid)] = str(uname)
        except sqlite3.Error:
            pass
        rows: list[dict] = []
        for tbl in tables:
            sql = (f'select acontent, session_id, sender_id, create_time, local_type '
                   f'from "{tbl}"')
            where, params = [], []
            if t_from is not None:
                where.append("create_time >= ?")
                params.append(int(t_from))
            if t_to is not None:
                where.append("create_time <= ?")
                params.append(int(t_to))
            if where:
                sql += " where " + " and ".join(where)
            try:
                data = con.execute(sql, params).fetchall()
            except sqlite3.Error as exc:
                log(f"跳过 {tbl}: {exc}", "warn")
                continue
            for content, sid, send_id, ctime, ltype in data:
                if not content:
                    continue
                text = str(content)
                if kws and not any(k in text.lower() for k in kws):
                    continue
                username = id2name.get(int(sid or 0), f"?sid{sid}")
                if contact and contact not in (username, names.get(username, "")):
                    continue
                sender = id2name.get(int(send_id or 0), "") if send_id else ""
                rows.append({
                    "ts": int(ctime or 0),
                    "time": _fmt_time(ctime or 0),
                    "username": username,
                    "session": names.get(username, username),
                    "sender": names.get(sender, sender) if sender else "",
                    "local_type": int(ltype or 0),
                    "kind": TYPE_LABEL.get(int(ltype or 0), f"类型{ltype}"),
                    "text": text,
                })
        con.close()
        rows.sort(key=lambda r: r["ts"], reverse=True)
        if limit and limit > 0:
            rows = rows[:limit]
        log(f"检索完成：命中 {len(rows)} 条（关键词={keyword or '无'}，"
            f"时间={since or '最早'}~{until or '现在'}）")
        return rows
    finally:
        try:
            reader.close()
        except Exception:
            pass


def export_txt(rows: list[dict], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            who = f"{r['session']}" + (f"（{r['sender']}）" if r["sender"] and r["sender"] != r["session"] else "")
            f.write(f"{r['time']}  {who}: {r['text']}\n")
    return path


def export_json(rows: list[dict], path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
