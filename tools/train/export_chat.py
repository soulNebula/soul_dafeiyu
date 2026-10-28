#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""聊天记录导出器：微信 selflearn 库 / 任意 JSONL / 任意 SQLite → SFT messages JSONL。

支持的输入：
1. 微信库：``agi/data/selflearn/wechat_contacts/*.memory.sqlite3`` + 主库
   ``agi/data/selflearn/selflearn.memory.sqlite3``（messages: role/text/ts/proactive）
2. 通用 JSONL：OpenAI ``messages`` / ``{user,assistant}`` / ``{q,a}`` /
   ``{instruction,input,output}``
3. 任意 SQLite：``--from-sqlite db --table messages --role-col role --text-col text``

输出统一为 SFT 格式：``{"messages":[{"role":"system",...},{"role":"user",...},{"role":"assistant",...}]}``。
只读源库；产物默认写 ``E:/agent/datasets``。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from agi import textguard
except Exception:  # noqa: BLE001 - 独立运行兜底
    textguard = None

DEFAULT_OUT_DIR = Path(os.environ.get("CHAT_EXPORT_DIR", r"E:/agent/datasets"))
DEFAULT_SYSTEM = (
    "你是微信里的一个 AI 朋友。语气平和，不讨好、不客服腔；先接住对方的话，"
    "再决定要不要追问；不列清单、不说教；有情绪或危机信号时以对方安全为先。"
)
DATA_DIR = ROOT / "agi" / "data" / "selflearn"
CONTACT_DIR = DATA_DIR / "wechat_contacts"
MAIN_DB = DATA_DIR / "selflearn.memory.sqlite3"
WECHAT_CONFIG = ROOT / "agi" / "data" / "wechat" / "config.json"
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?$")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE = re.compile(r"https?://[^\s，。；、）)\]]+")
_INTERNAL_ID_RE = re.compile(
    r"wxid_[0-9A-Za-z_-]{4,}:\d{6,}(?::\d+)*:[0-9a-fA-F]{8,}(?:::\d+)?"
    r"|(?<![\w])\d{10,}:\d{1,3}:[0-9a-fA-F]{16,}(?:::\d+)?"
    r"|\bwxid_[0-9a-z]{6,}\b"
)


# ------------------------------------------------------------------ 基础
def parse_time(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    try:
        return float(text)
    except (TypeError, ValueError):
        pass
    if TS_RE.match(text):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return time.mktime(time.strptime(text.replace("T", " "), fmt))
            except ValueError:
                continue
    raise argparse.ArgumentTypeError(f"无法解析时间：{value}（支持 Unix 秒或 YYYY-MM-DD）")


def looks_internal(text: str) -> bool:
    if textguard is not None:
        try:
            return bool(textguard.looks_like_internal_id(text))
        except Exception:  # noqa: BLE001
            pass
    return bool(_INTERNAL_ID_RE.search(str(text or "")))


def redact_text(text: str) -> str:
    """去掉内部 ID / 手机号 / 邮箱 / URL；保留正文其余部分。"""
    value = str(text or "")
    if textguard is not None:
        try:
            value = textguard.strip_internal_ids(value)
        except Exception:  # noqa: BLE001
            value = _INTERNAL_ID_RE.sub("", value)
    else:
        value = _INTERNAL_ID_RE.sub("", value)
    value = _URL_RE.sub("[链接]", value)
    value = _EMAIL_RE.sub("[邮箱]", value)
    value = _PHONE_RE.sub("[手机号]", value)
    value = re.sub(r"[ \t]{2,}", " ", value).strip()
    return value


def _clean_text(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def normalize_role(role: Any) -> str:
    value = str(role or "").strip().lower()
    if value in ("user", "human", "customer", "我", "用户"):
        return "user"
    if value in ("assistant", "bot", "ai", "model", "gpt", "她", "助手"):
        return "assistant"
    if value in ("system", "系统"):
        return "system"
    return ""


# ------------------------------------------------------------------ 联系人/源
def contact_names() -> dict[str, str]:
    try:
        raw = json.loads(WECHAT_CONFIG.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        raw = {}
    names: list[str] = []
    for key in ("monitored_contacts", "allowed_contacts", "proactive_contacts"):
        for item in (raw.get(key) or []):
            text = str(item).strip()
            if text and text not in names:
                names.append(text)
    return {hashlib.sha1(name.encode("utf-8")).hexdigest()[:14]: name for name in names}


def resolve_db(spec: str) -> Path | None:
    """--db 支持绝对/相对路径，或联系人名/hash 前缀。"""
    raw = str(spec or "").strip()
    if not raw:
        return None
    candidate = Path(os.path.expanduser(os.path.expandvars(raw)))
    if candidate.is_file():
        return candidate
    names = contact_names()
    # 名字精确/模糊 → hash
    matches = [name for name in names.values() if raw.casefold() in name.casefold()]
    for name in matches:
        prefix = hashlib.sha1(name.encode("utf-8")).hexdigest()[:14]
        path = CONTACT_DIR / f"{prefix}.memory.sqlite3"
        if path.is_file():
            return path
    # hash 前缀直接找
    for path in sorted(CONTACT_DIR.glob("*.memory.sqlite3")):
        if path.name.startswith(raw):
            return path
    if candidate.is_file():
        return candidate
    return None


def _is_test_contact(name: str, path: Path) -> bool:
    value = (str(name or "") + " " + path.name).casefold()
    return any(word in value for word in ("测试", "test", "文件传输助手"))


def source_list(*, db: str = "", all_contacts: bool = False,
                contact_name: str = "", exclude_test: bool = False) -> list[tuple[str, Path]]:
    sources: list[tuple[str, Path]] = []
    if db:
        path = resolve_db(db)
        if path is not None:
            name = db
            for candidate_name, prefix in contact_names().items():
                if prefix and path.name.startswith(candidate_name):
                    name = prefix
                    break
            if not (exclude_test and _is_test_contact(name, path)):
                sources.append((name, path))
        return sources
    names = contact_names()
    if all_contacts:
        if MAIN_DB.is_file():
            sources.append(("主库", MAIN_DB))
        for path in sorted(CONTACT_DIR.glob("*.memory.sqlite3")):
            prefix = path.name.split(".", 1)[0]
            name = names.get(prefix, f"联系人-{prefix[:6]}")
            if contact_name and contact_name.casefold() not in name.casefold() \
                    and contact_name.casefold() not in prefix.casefold():
                continue
            if exclude_test and _is_test_contact(name, path):
                continue
            sources.append((name, path))
        return sources
    # 只给 --contact-name：匹配联系人库
    for path in sorted(CONTACT_DIR.glob("*.memory.sqlite3")):
        prefix = path.name.split(".", 1)[0]
        name = names.get(prefix, f"联系人-{prefix[:6]}")
        if contact_name.casefold() in name.casefold() or contact_name.casefold() in prefix.casefold():
            if not (exclude_test and _is_test_contact(name, path)):
                sources.append((name, path))
    return sources


# ------------------------------------------------------------------ SQLite 读取
def _table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})").fetchall()}


def load_sqlite_messages(path: Path, *, table: str = "messages",
                         role_col: str = "role", text_col: str = "text",
                         ts_col: str = "ts", since: float | None = None,
                         until: float | None = None, min_chars: int = 0,
                         max_rows: int = 0) -> tuple[list[dict], dict]:
    stats = {"rows": 0, "kept": 0, "skip_empty": 0, "skip_role": 0,
             "skip_internal": 0, "skip_short": 0, "skip_time": 0,
             "skip_bad": 0}
    for value in (table, role_col, text_col, ts_col):
        if not _IDENT_RE.match(value):
            raise ValueError(f"非法 SQL 标识符：{value}")
    if not path.is_file():
        return [], stats
    uri = f"file:{path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10.0)
    con.row_factory = sqlite3.Row
    try:
        columns = _table_columns(con, table)
        if text_col not in columns or role_col not in columns:
            raise ValueError(f"表 {table} 缺少列 {role_col}/{text_col}")
        ts_expr = ts_col if ts_col in columns else "NULL"
        sql = f"SELECT {role_col} AS role, {text_col} AS text, {ts_expr} AS ts FROM {table}"
        sql += " ORDER BY " + (ts_col if ts_col in columns else "rowid")
        rows = con.execute(sql).fetchall()
    finally:
        con.close()
    out: list[dict] = []
    for row in rows:
        stats["rows"] += 1
        try:
            role = normalize_role(row["role"])
            text = _clean_text(row["text"])
            ts = row["ts"]
            ts = float(ts) if ts is not None else 0.0
        except Exception:  # noqa: BLE001
            stats["skip_bad"] += 1
            continue
        if role not in ("user", "assistant"):
            stats["skip_role"] += 1
            continue
        if not text:
            stats["skip_empty"] += 1
            continue
        if since is not None and ts and ts < since:
            stats["skip_time"] += 1
            continue
        if until is not None and ts and ts > until:
            stats["skip_time"] += 1
            continue
        if looks_internal(text):
            stats["skip_internal"] += 1
            continue
        if len(text) < int(min_chars):
            stats["skip_short"] += 1
            continue
        out.append({"role": role, "text": text, "ts": ts})
        stats["kept"] += 1
        if max_rows and len(out) >= int(max_rows):
            break
    return out, stats


# ------------------------------------------------------------------ 组装 SFT
def merge_consecutive(messages: Iterable[dict]) -> list[dict]:
    out: list[dict] = []
    for item in messages:
        role = str(item.get("role") or "")
        text = _clean_text(item.get("text") or item.get("content") or "")
        if role not in ("user", "assistant", "system") or not text:
            continue
        if out and out[-1]["role"] == role:
            out[-1]["text"] += "\n" + text
            continue
        out.append({"role": role, "text": text,
                    "ts": float(item.get("ts") or 0.0)})
    return out


def build_samples(messages: Iterable[dict], *, window: int = 6,
                  system: str = DEFAULT_SYSTEM, redact: bool = True) -> list[dict]:
    """每个 assistant 回复生成一个样本，携带前 window 轮上下文。"""
    merged = merge_consecutive(messages)
    samples: list[dict] = []
    for index, item in enumerate(merged):
        if item["role"] != "assistant" or index == 0:
            continue
        if merged[index - 1]["role"] != "user":
            continue
        start = max(0, index - 1 - 2 * max(0, int(window)))
        context = merged[start:index + 1]
        if not context or context[-1]["role"] != "assistant":
            continue
        out_messages = [{"role": "system", "content": _clean_text(system)}] if system else []
        for row in context:
            text = redact_text(row["text"]) if redact else row["text"]
            if not text:
                continue
            out_messages.append({"role": row["role"], "content": text})
        if not any(row["role"] == "assistant" for row in out_messages):
            continue
        samples.append({"messages": out_messages})
    return samples


# ------------------------------------------------------------------ 通用输入转换
def record_to_messages(record: dict) -> list[dict]:
    if not isinstance(record, dict):
        return []
    if isinstance(record.get("messages"), list):
        out = []
        for item in record["messages"]:
            if not isinstance(item, dict):
                continue
            role = normalize_role(item.get("role"))
            content = item.get("content")
            if role not in ("system", "user", "assistant"):
                continue
            text = _clean_text(content)
            if text:
                out.append({"role": role, "content": text})
        return out
    if "user" in record and "assistant" in record:
        user = _clean_text(record.get("user"))
        assistant = _clean_text(record.get("assistant"))
        return ([{"role": "system", "content": _clean_text(record["system"])}] if record.get("system") else []) + \
               ([{"role": "user", "content": user}] if user else []) + \
               ([{"role": "assistant", "content": assistant}] if assistant else [])
    if "q" in record and "a" in record:
        q, a = _clean_text(record.get("q")), _clean_text(record.get("a"))
        return ([{"role": "user", "content": q}] if q else []) + \
               ([{"role": "assistant", "content": a}] if a else [])
    if "instruction" in record and "output" in record:
        instruction = _clean_text(record.get("instruction"))
        input_text = _clean_text(record.get("input"))
        user = instruction
        if input_text:
            user = (instruction + "\n" + input_text).strip() if instruction else input_text
        output = _clean_text(record.get("output"))
        return ([{"role": "user", "content": user}] if user else []) + \
               ([{"role": "assistant", "content": output}] if output else [])
    return []


def load_jsonl(path: Path, *, min_chars: int = 0, redact: bool = True) -> tuple[list[dict], dict]:
    stats = {"rows": 0, "kept": 0, "skip_bad": 0, "skip_empty": 0, "skip_short": 0}
    samples: list[dict] = []
    if not path.is_file():
        return samples, stats
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return samples, stats
    with handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            stats["rows"] += 1
            try:
                record = json.loads(text)
            except json.JSONDecodeError:
                stats["skip_bad"] += 1
                continue
            messages = record_to_messages(record)
            if not messages:
                stats["skip_empty"] += 1
                continue
            out = []
            for item in messages:
                value = redact_text(item["content"]) if redact else item["content"]
                if value:
                    out.append({"role": item["role"], "content": value})
            if len(out) < 2 or not any(x["role"] == "assistant" for x in out):
                stats["skip_empty"] += 1
                continue
            if min_chars and sum(len(x["content"]) for x in out) < int(min_chars):
                stats["skip_short"] += 1
                continue
            samples.append({"messages": out})
            stats["kept"] += 1
    return samples, stats


def load_sqlite_generic(path: Path, *, table: str, role_col: str, text_col: str,
                        ts_col: str = "ts", **kwargs) -> tuple[list[dict], dict]:
    return load_sqlite_messages(path, table=table, role_col=role_col,
                                text_col=text_col, ts_col=ts_col, **kwargs)


# ------------------------------------------------------------------ 输出/切分
def write_jsonl(path: Path, samples: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False) + "\n")


def split_samples(samples: list[dict], split: float, *, seed: int = 42) -> tuple[list[dict], list[dict]]:
    if split <= 0 or not samples:
        return samples, []
    ratio = min(0.9, max(0.0, float(split)))
    shuffled = list(samples)
    random.Random(seed).shuffle(shuffled)
    val_size = max(1, int(round(len(shuffled) * ratio)))
    val = shuffled[-val_size:]
    train = shuffled[:-val_size]
    return train, val


def token_estimate(samples: list[dict]) -> int:
    chars = sum(len(str(m.get("content") or "")) for s in samples for m in s.get("messages", []))
    return int(chars / 1.8)  # 中文粗估


def _count_messages(path: Path, *, table: str = "messages",
                    role_col: str = "role", text_col: str = "text") -> int:
    if not path.is_file():
        return 0
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            row = con.execute(f"SELECT count(*) FROM {table}").fetchone()
            return int(row[0] or 0)
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return 0


def export_wechat(*, db: str = "", all_contacts: bool = False, contact_name: str = "",
                  exclude_test: bool = False, since: float | None = None,
                  until: float | None = None, min_chars: int = 2,
                  max_rows: int = 0, window: int = 6,
                  system: str = DEFAULT_SYSTEM, redact: bool = True) -> tuple[list[dict], dict]:
    sources = source_list(db=db, all_contacts=all_contacts,
                          contact_name=contact_name, exclude_test=exclude_test)
    stats = {"sources": [], "rows": 0, "messages": 0, "samples": 0,
             "skipped": {}, "avg_length": 0, "tokens_est": 0}
    remaining = int(max_rows) if max_rows else 0
    all_samples: list[dict] = []
    source_stats: list[dict] = []
    quotas: dict[str, int] = {}
    if max_rows and len(sources) > 1:
        counts = {str(path): max(0, _count_messages(path)) for _name, path in sources}
        total_count = sum(counts.values())
        if total_count:
            allocated = 0
            for index, (_name, path) in enumerate(sources):
                if index == len(sources) - 1:
                    quota = max(0, int(max_rows) - allocated)
                else:
                    quota = int(round(int(max_rows) * counts[str(path)] / total_count))
                    quota = min(max(0, quota), max(0, int(max_rows) - allocated))
                quotas[str(path)] = quota
                allocated += quota
        else:
            quotas = {str(path): 0 for _name, path in sources}
    for name, path in sources:
        per_limit = quotas.get(str(path), (int(max_rows) if max_rows else 0))
        if remaining:
            per_limit = min(per_limit, remaining)
        messages, read_stats = load_sqlite_messages(
            path, since=since, until=until, min_chars=min_chars,
            max_rows=per_limit)
        if remaining:
            remaining = max(0, remaining - len(messages))
        samples = build_samples(messages, window=window, system=system, redact=redact)
        source_stats.append({"name": name, "path": str(path),
                             "messages": len(messages), "samples": len(samples),
                             "read": read_stats})
        stats["messages"] += len(messages)
        all_samples.extend(samples)
        for key, value in read_stats.items():
            if key.startswith("skip_"):
                stats["skipped"][key] = stats["skipped"].get(key, 0) + int(value)
        if remaining == 0 and max_rows:
            break
    stats["sources"] = source_stats
    stats["samples"] = len(all_samples)
    total_chars = sum(len(str(m.get("content") or ""))
                      for sample in all_samples for m in sample.get("messages", []))
    stats["avg_length"] = round(total_chars / max(1, len(all_samples)), 1)
    stats["tokens_est"] = token_estimate(all_samples)
    return all_samples, stats


# ------------------------------------------------------------------ CLI
def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="微信/JSONL/SQLite → SFT messages JSONL")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--db", default="", help="微信 memory.sqlite3 路径或联系人名/hash")
    source.add_argument("--all-contacts", action="store_true", help="主库 + 所有联系人库")
    source.add_argument("--from-jsonl", default="", help="通用 JSONL 输入")
    source.add_argument("--from-sqlite", default="", help="任意 SQLite 输入")
    parser.add_argument("--contact-name", default="", help="联系人名字模糊匹配")
    parser.add_argument("--since", default="", help="起始时间（Unix 秒或 YYYY-MM-DD）")
    parser.add_argument("--until", default="", help="结束时间")
    parser.add_argument("--min-chars", type=int, default=2)
    parser.add_argument("--max-rows", type=int, default=0, help="最多读取多少条原始消息（0=不限）")
    parser.add_argument("--exclude-test", action="store_true", help="跳过测试/文件传输助手等联系人")
    parser.add_argument("--window", type=int, default=6, help="上下文窗口（前 N 轮）")
    parser.add_argument("--system", default=DEFAULT_SYSTEM)
    parser.add_argument("--no-redact", action="store_true", help="关闭脱敏（默认开启）")
    parser.add_argument("--split", type=float, default=0.0, help="验证集比例，如 0.1")
    parser.add_argument("--out", default="", help="输出路径；默认 E:/agent/datasets/chat_export_<时间>.jsonl")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--table", default="messages")
    parser.add_argument("--role-col", default="role")
    parser.add_argument("--text-col", default="text")
    parser.add_argument("--ts-col", default="ts")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    redact = not args.no_redact
    since = parse_time(args.since) if args.since else None
    until = parse_time(args.until) if args.until else None
    if args.from_jsonl:
        samples, stats = load_jsonl(Path(args.from_jsonl), min_chars=args.min_chars, redact=redact)
    elif args.from_sqlite:
        raw, stats = load_sqlite_generic(
            Path(args.from_sqlite), table=args.table, role_col=args.role_col,
            text_col=args.text_col, ts_col=args.ts_col, since=since, until=until,
            min_chars=args.min_chars, max_rows=args.max_rows)
        samples = build_samples(raw, window=args.window, system=args.system, redact=redact)
        stats["samples"] = len(samples)
        stats["messages"] = len(raw)
    elif args.all_contacts or args.db or args.contact_name:
        samples, stats = export_wechat(
            db=args.db, all_contacts=args.all_contacts,
            contact_name=args.contact_name, exclude_test=args.exclude_test,
            since=since, until=until, min_chars=args.min_chars,
            max_rows=args.max_rows, window=args.window,
            system=args.system, redact=redact)
    else:
        print("请指定 --db / --all-contacts / --from-jsonl / --from-sqlite", file=sys.stderr)
        return 2
    out = Path(args.out) if args.out else (
        DEFAULT_OUT_DIR / f"chat_export_{time.strftime('%Y%m%d_%H%M%S')}.jsonl")
    train, val = split_samples(samples, args.split, seed=args.seed)
    write_jsonl(out, train)
    val_path = ""
    if val:
        val_path = str(out.with_name(out.stem + "_val" + out.suffix))
        write_jsonl(Path(val_path), val)
    stats["output"] = str(out)
    stats["train"] = len(train)
    stats["val"] = len(val)
    stats["val_path"] = val_path
    stats["redacted"] = redact
    stats["window"] = args.window
    print(json.dumps(stats, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
