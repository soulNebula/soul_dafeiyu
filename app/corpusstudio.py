# -*- coding: utf-8 -*-
"""语料清洗：上传聊天记录（文本/截图）→ 解析出「你/我」→ 勾选保存为训练数据集。

流程
----
1. ``new_job()`` 建任务；``save_upload()`` 收文件（文本 / 截图）；
2. ``parse_job()`` 清洗：
   - 文本：``我:`` 前缀 / 微信导出「昵称 + 时间」/ 无标签双人对话（交替）
   - JSON/JSONL：messages、{user,assistant}、[{role,text}]
   - 图片：子进程调 ``tools/ocr_image.py``（RapidOCR/Windows OCR），按气泡左右判角色
3. ``save_dataset()``：勾选条目 → messages / instruction JSONL → ``E:/agent/datasets``。

条目统一结构: ``{id, role: "me"|"you", text, source, ts?}``。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from . import audit, paths

TEXT_EXTS = {".txt", ".md", ".json", ".jsonl", ".csv"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_ITEMS = 5000
MAX_TEXT_CHARS = 4000

_PREFIX_RE = re.compile(
    r"^\s*(我|你|用户|对方|AI|ai|她|他|小鱼|me|you|assistant|user|机器人)\s*[:：]\s*(.+)$")
_WECHAT_HEAD_RE = re.compile(
    r"^\s*(.{1,40}?)\s+(\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)\s*$")
_SYS_RE = re.compile(r"撤回了一条消息|拍了拍|加入了群聊|领取了|邀请.*加入|消息已发出|以上是打招呼")
_URL_RE = re.compile(r"^https?://\S+$")
_TIME_PREFIX_RE = re.compile(r"^\s*\[?\d{1,2}:\d{2}(?::\d{2})?\]?\s*")
_SAFE_NAME_RE = re.compile(r"[^\w\u4e00-\u9fff.\-]+")

ROLE_ME = "me"
ROLE_YOU = "you"


# --------------------------------------------------------------------------- #
# 任务 / 上传
# --------------------------------------------------------------------------- #
def _corpus_root(root: Path | str | None = None) -> Path:
    base = Path(root) if root is not None else Path(paths.DATA_DIR) / "corpus"
    return base


def _job_dir(job_id: str, root: Path | str | None = None) -> Path:
    return _corpus_root(root) / str(job_id)


def _job_meta_path(job_id: str, root: Path | str | None = None) -> Path:
    return _corpus_root(root) / "jobs" / f"{job_id}.json"


def _read_meta(job_id: str, root: Path | str | None = None) -> dict | None:
    path = _job_meta_path(job_id, root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_meta(meta: dict, root: Path | str | None = None) -> None:
    path = _job_meta_path(str(meta["id"]), root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def new_job(*, root: Path | str | None = None, name: str = "") -> dict:
    job_id = "c" + uuid.uuid4().hex[:10]
    job_dir = _job_dir(job_id, root)
    (job_dir / "raw").mkdir(parents=True, exist_ok=True)
    meta = {"id": job_id, "created": time.time(), "name": str(name or "")[:40],
            "files": [], "parsed": False, "counts": {}}
    _write_meta(meta, root)
    return {"ok": True, "job": meta, "id": job_id}


def list_jobs(*, root: Path | str | None = None) -> dict:
    jobs_dir = _corpus_root(root) / "jobs"
    items: list[dict] = []
    if jobs_dir.is_dir():
        for path in jobs_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("id"):
                data.pop("items", None)
                items.append(data)
    items.sort(key=lambda d: float(d.get("created") or 0), reverse=True)
    out = []
    for data in items:
        files = [f for f in (data.get("files") or []) if isinstance(f, dict)]
        out.append({"id": data["id"], "name": data.get("name") or "",
                    "created": data.get("created"),
                    # 前端按 job.files.length 显示文件数 → 这里返回完整列表
                    "files": files, "file_count": len(files),
                    "parsed": bool(data.get("parsed")),
                    "counts": data.get("counts") or {}})
    return {"ok": True, "items": out, "count": len(out)}


def get_job(job_id: str, *, root: Path | str | None = None) -> dict:
    meta = _read_meta(job_id, root)
    if not meta:
        return {"ok": False, "error": "job_not_found"}
    items = load_items(job_id, root=root)
    meta["has_items"] = bool(items)
    return {"ok": True, "job": meta}


def delete_job(job_id: str, *, root: Path | str | None = None) -> dict:
    meta = _read_meta(job_id, root)
    if not meta:
        return {"ok": False, "error": "job_not_found"}
    job_dir = _job_dir(job_id, root)
    try:
        shutil.rmtree(job_dir, ignore_errors=True)
    except OSError as exc:  # noqa: BLE001
        return {"ok": False, "error": f"delete_failed:{exc}"}
    try:
        _job_meta_path(job_id, root).unlink(missing_ok=True)
    except OSError:
        pass
    return {"ok": True, "id": job_id}


def _safe_filename(name: str) -> str:
    base = Path(str(name or "")).name
    base = _SAFE_NAME_RE.sub("_", base).strip("._ ") or f"upload_{int(time.time())}"
    return base[:120]


def save_upload(job_id: str, filename: str, data: bytes, *,
                root: Path | str | None = None) -> dict:
    meta = _read_meta(job_id, root)
    if not meta:
        return {"ok": False, "error": "job_not_found"}
    if not isinstance(data, (bytes, bytearray)) or not data:
        return {"ok": False, "error": "empty_file"}
    if len(data) > MAX_UPLOAD_BYTES:
        return {"ok": False, "error": "too_large", "limit": MAX_UPLOAD_BYTES}
    safe = _safe_filename(filename)
    ext = Path(safe).suffix.lower()
    if ext in TEXT_EXTS:
        kind = "text"
    elif ext in IMAGE_EXTS:
        kind = "image"
    else:
        return {"ok": False, "error": "bad_ext", "allowed":
                sorted(TEXT_EXTS | IMAGE_EXTS)}
    raw_dir = _job_dir(job_id, root) / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    target = raw_dir / safe
    stem, suffix = target.stem, target.suffix
    index = 1
    while target.exists():
        target = raw_dir / f"{stem}-{index}{suffix}"
        index += 1
    try:
        target.write_bytes(bytes(data))
    except OSError as exc:  # noqa: BLE001
        return {"ok": False, "error": f"write_failed:{exc}"[:200]}
    files = [f for f in (meta.get("files") or []) if isinstance(f, dict)]
    files.append({"name": target.name, "kind": kind, "size": len(data),
                  "path": str(target)})
    meta["files"] = files
    meta["parsed"] = False
    _write_meta(meta, root)
    return {"ok": True, "name": target.name, "kind": kind, "size": len(data)}


# --------------------------------------------------------------------------- #
# 清洗解析
# --------------------------------------------------------------------------- #
def _clean_text(text: Any) -> str:
    value = str(text or "").strip()
    value = _TIME_PREFIX_RE.sub("", value).strip()
    if not value:
        return ""
    if _URL_RE.match(value):
        return ""
    if _SYS_RE.search(value):
        return ""
    return value[:MAX_TEXT_CHARS]


def _is_me(name: str, me_names: set[str]) -> bool:
    return name.strip().lower() in me_names


def _me_you_names(me_names: Any, you_names: Any) -> tuple[set[str], set[str]]:
    def _parse(value: Any) -> set[str]:
        if isinstance(value, str):
            parts = re.split(r"[,，、\s]+", value)
        elif isinstance(value, Iterable):
            parts = [str(v) for v in value]
        else:
            parts = []
        return {str(p).strip().lower() for p in parts if str(p).strip()}
    me = _parse(me_names) or {"我", "me", "assistant", "小鱼", "她"}
    you = _parse(you_names) or {"你", "you", "user", "用户", "对方"}
    return me, you


def _items_from_prefix_dicts(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        role = str(row.get("role") or "").strip().lower()
        text = row.get("text")
        if text is None:
            text = row.get("content")
        if not isinstance(text, str):
            continue
        if role in ("assistant", "me", "self", "ai", "bot"):
            mapped = ROLE_ME
        elif role in ("user", "you", "human", "对方"):
            mapped = ROLE_YOU
        else:
            continue
        cleaned = _clean_text(text)
        if cleaned:
            out.append({"role": mapped, "text": cleaned})
    return out


def _parse_json_text(content: str) -> list[dict]:
    """JSON / JSONL → 条目；不是 JSON 返回 []。"""
    def _from_obj(obj: Any) -> list[dict]:
        if isinstance(obj, list):
            return _items_from_prefix_dicts(obj)
        if not isinstance(obj, dict):
            return []
        if isinstance(obj.get("messages"), list):
            return _items_from_prefix_dicts(obj["messages"])
        if obj.get("user") is not None and obj.get("assistant") is not None:
            out = []
            for role, key in ((ROLE_YOU, "user"), (ROLE_ME, "assistant")):
                text = _clean_text(obj.get(key))
                if text:
                    out.append({"role": role, "text": text})
            return out
        return []

    stripped = content.strip()
    if not stripped:
        return []
    # 整文件 JSON
    if stripped[0] in "[{":
        try:
            return _from_obj(json.loads(stripped))
        except ValueError:
            pass
    # JSONL
    out: list[dict] = []
    for line in stripped.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            return []
        out.extend(_from_obj(obj))
    return out


def _parse_wechat_style(lines: list[str], me_names: set[str],
                        you_names: set[str]) -> list[dict]:
    items: list[dict] = []
    current_role: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal buffer, current_role
        if current_role and buffer:
            text = _clean_text("\n".join(buffer))
            if text:
                items.append({"role": current_role, "text": text})
        buffer = []

    found_head = False
    for line in lines:
        match = _WECHAT_HEAD_RE.match(line)
        if match:
            found_head = True
            flush()
            speaker = str(match.group(1)).strip()
            if _is_me(speaker, me_names):
                current_role = ROLE_ME
            elif _is_me(speaker, you_names):
                current_role = ROLE_YOU
            else:
                current_role = ROLE_YOU
            continue
        if found_head:
            buffer.append(line)
    flush()
    return items if found_head else []


def _parse_prefix_style(lines: list[str]) -> list[dict]:
    items: list[dict] = []
    for line in lines:
        match = _PREFIX_RE.match(line)
        if not match:
            continue
        name = match.group(1).strip().lower()
        text = _clean_text(match.group(2))
        if not text:
            continue
        if name in ("我", "me", "assistant", "ai", "小鱼", "她", "机器人"):
            role = ROLE_ME
        else:
            role = ROLE_YOU
        items.append({"role": role, "text": text})
    return items


def _parse_alternating(lines: list[str]) -> list[dict]:
    cleaned = [_clean_text(line) for line in lines]
    cleaned = [line for line in cleaned if line]
    if len(cleaned) < 2:
        return []
    flag = ROLE_YOU
    items: list[dict] = []
    for text in cleaned:
        items.append({"role": flag, "text": text})
        flag = ROLE_ME if flag == ROLE_YOU else ROLE_YOU
    return items


def _parse_text_file(path: Path, me_names: set[str], you_names: set[str]) -> list[dict]:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    ext = path.suffix.lower()
    if ext in (".json", ".jsonl"):
        items = _parse_json_text(content)
        if items:
            return items
    lines = content.splitlines()
    for parser in (
        lambda: _parse_prefix_style(lines),
        lambda: _parse_wechat_style(lines, me_names, you_names),
    ):
        items = parser()
        if items:
            return items
    return _parse_alternating(lines)


# --------------------------------------------------------------------------- #
# 图片 OCR
# --------------------------------------------------------------------------- #
def _ocr_python() -> str:
    return os.environ.get("IDEA_OCR_PYTHON") or r"C:\Python314\python.exe"


def _ocr_script() -> Path:
    return Path(paths.soulagent_home()) / "tools" / "ocr_image.py"


def _parse_image_file(path: Path, *, timeout: float = 120.0) -> list[dict]:
    script = _ocr_script()
    if not script.is_file():
        return []
    cmd = [_ocr_python(), str(script), "--image", str(path), "--infer-role"]
    try:
        proc = subprocess.run(cmd, cwd=str(Path(paths.soulagent_home())),
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return []
    payload: dict[str, Any] = {}
    for line in reversed((proc.stdout or "").splitlines()):
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    items: list[dict] = []
    for result in payload.get("results") or []:
        if not isinstance(result, dict):
            continue
        bubbles = [b for b in (result.get("bubble_lines") or []) if isinstance(b, dict)]
        if bubbles:
            for bubble in bubbles:
                role = ROLE_ME if str(bubble.get("role")).lower() == "me" else ROLE_YOU
                text = _clean_text(bubble.get("text"))
                if text:
                    items.append({"role": role, "text": text})
            continue
        for line in result.get("lines") or []:
            if not isinstance(line, dict):
                continue
            role = ROLE_ME if str(line.get("role")).lower() == "me" else ROLE_YOU
            text = _clean_text(line.get("text"))
            if text:
                items.append({"role": role, "text": text})
    return items


# --------------------------------------------------------------------------- #
# parse / items
# --------------------------------------------------------------------------- #
def items_path(job_id: str, root: Path | str | None = None) -> Path:
    return _job_dir(job_id, root) / "items.json"


def load_items(job_id: str, *, root: Path | str | None = None) -> list[dict] | None:
    path = items_path(job_id, root)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    items = data.get("items") if isinstance(data, dict) else None
    return items if isinstance(items, list) else None


def parse_job(job_id: str, *, me_names: Any = "我,me,assistant,小鱼,她",
              you_names: Any = "你,user,用户,对方",
              root: Path | str | None = None) -> dict:
    """同步清洗（CLI/测试用）；HTTP 层用 ``start_parse`` 异步 + ``parse_status`` 轮询。"""
    return _parse_impl(job_id, me_names=me_names, you_names=you_names, root=root)


def _parse_impl(job_id: str, *, me_names: Any = "我,me,assistant,小鱼,她",
                you_names: Any = "你,user,用户,对方",
                root: Path | str | None = None,
                progress: Any = None) -> dict:
    meta = _read_meta(job_id, root)
    if not meta:
        return {"ok": False, "error": "job_not_found"}
    me_set, you_set = _me_you_names(me_names, you_names)
    items: list[dict] = []
    skipped = 0
    files = 0
    file_list = [f for f in (meta.get("files") or []) if isinstance(f, dict)]
    total_files = len(file_list)
    for order, record in enumerate(file_list):
        if callable(progress):
            try:
                progress(order, total_files, str(record.get("name") or ""))
            except Exception:  # noqa: BLE001 - 进度回调失败不影响清洗
                pass
        path = Path(str(record.get("path") or ""))
        if not path.is_file():
            # 任务目录可能迁移：按名字在 raw/ 里找
            candidate = _job_dir(job_id, root) / "raw" / str(record.get("name") or "")
            path = candidate if candidate.is_file() else path
        if not path.is_file():
            skipped += 1
            continue
        files += 1
        kind = record.get("kind")
        ext = path.suffix.lower()
        parsed: list[dict] = []
        if kind == "image" or ext in IMAGE_EXTS:
            parsed = _parse_image_file(path)
        else:
            parsed = _parse_text_file(path, me_set, you_set)
        for row in parsed:
            if len(items) >= MAX_ITEMS:
                break
            items.append({"id": f"i{len(items) + 1:04d}",
                          "role": row["role"], "text": row["text"],
                          "source": path.name})
    if not items:
        return {"ok": False, "error": "no_items", "files": files}
    stats = {"total": len(items),
             "me": sum(1 for i in items if i["role"] == ROLE_ME),
             "you": sum(1 for i in items if i["role"] == ROLE_YOU),
             "files": files, "skipped": skipped}
    payload = {"ok": True, "job_id": job_id, "items": items,
               "stats": stats, "parsed_at": time.time()}
    target = items_path(job_id, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                      encoding="utf-8")
    meta["parsed"] = True
    meta["counts"] = stats
    _write_meta(meta, root)
    return payload


def items_view(job_id: str, *, root: Path | str | None = None) -> dict:
    meta = _read_meta(job_id, root)
    if not meta:
        return {"ok": False, "error": "job_not_found"}
    items = load_items(job_id, root=root)
    if items is None:
        return {"ok": False, "error": "not_parsed"}
    return {"ok": True, "job_id": job_id, "items": items,
            "stats": meta.get("counts") or {}}


# --------------------------------------------------------------------------- #
# 保存数据集
# --------------------------------------------------------------------------- #
def _datasets_dir() -> Path:
    return Path(paths.agent_home()) / "datasets"


def _split_rows(rows: list[dict], split: float) -> tuple[list[dict], list[dict]]:
    if len(rows) <= 1 or split <= 0:
        return rows, []
    count = int(round(len(rows) * min(max(split, 0.0), 0.9)))
    count = max(1, count)
    count = min(count, len(rows) - 1)
    return rows[:len(rows) - count], rows[len(rows) - count:]


def save_dataset(job_id: str, selected_ids: Any, *, format: str = "messages",
                 window: int = 6, split: float = 0.1, out_name: str = "",
                 system: str = "", root: Path | str | None = None) -> dict:
    fmt = str(format or "messages").strip().lower()
    if fmt not in ("messages", "instruction"):
        return {"ok": False, "error": "invalid_format"}
    items = load_items(job_id, root=root)
    if not items:
        return {"ok": False, "error": "not_parsed"}
    selected = {str(x) for x in (selected_ids or []) if str(x)}
    if not selected:
        return {"ok": False, "error": "empty_selection"}
    picked = [item for item in items if item.get("id") in selected]
    if not picked:
        return {"ok": False, "error": "empty_selection"}
    window = max(1, min(20, int(window or 6)))
    system_text = str(system or "").strip()

    # 勾选决定"哪些回复进训练集"；上下文仍从整段清洗结果里取（否则只勾 me 会没有 user）。
    rows: list[dict] = []
    if fmt == "instruction":
        for index, item in enumerate(items):
            if item.get("id") not in selected or item.get("role") != ROLE_YOU:
                continue
            for follow in items[index + 1:index + 2]:
                if follow.get("role") == ROLE_ME:
                    rows.append({"instruction": item["text"],
                                 "output": follow["text"]})
                    break
    else:
        for index, item in enumerate(items):
            if item.get("id") not in selected or item.get("role") != ROLE_ME:
                continue
            context_items = items[max(0, index - window):index]
            messages = []
            if system_text:
                messages.append({"role": "system", "content": system_text})
            for row in context_items:
                messages.append({"role": "user" if row["role"] == ROLE_YOU
                                 else "assistant", "content": row["text"]})
            if not any(m["role"] == "user" for m in messages):
                continue  # 没有用户输入，样本无效
            messages.append({"role": "assistant", "content": item["text"]})
            rows.append({"messages": messages})
    if not rows:
        return {"ok": False, "error": "empty_selection"}

    name = _SAFE_NAME_RE.sub("_", str(out_name or "")).strip("._ ") or f"corpus_{job_id}"
    out_dir = _datasets_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{name}.jsonl"
    if out_path.exists():
        out_path = out_dir / f"{name}_{int(time.time())}.jsonl"
    train_rows, val_rows = _split_rows(rows, float(split or 0.0))

    def _write(path: Path, data_rows: list[dict]) -> None:
        with path.open("w", encoding="utf-8") as fh:
            for row in data_rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    _write(out_path, train_rows)
    val_path = ""
    if val_rows:
        val_path = str(out_path.with_name(out_path.stem + "_val.jsonl"))
        _write(Path(val_path), val_rows)
    audit.record("corpus.save", str(out_path), actor="workbench")
    return {"ok": True, "out": str(out_path), "val_out": val_path,
            "rows": len(train_rows), "val_rows": len(val_rows),
            "format": fmt, "selected": len(picked), "window": window}

# --------------------------------------------------------------------------- #
# 异步清洗 + 进度
# --------------------------------------------------------------------------- #
_PARSE_LOCK = __import__("threading").Lock()
_PARSE_STATE: dict[str, dict] = {}


def _set_parse_state(job_id: str, root, **fields) -> None:
    state = {"status": "idle", "total": 0, "done": 0, "current": "",
             "percent": 0, "error": "", "stats": {}, "started_at": 0,
             "finished_at": 0}
    state.update(_PARSE_STATE.get(job_id) or {})
    state.update(fields)
    total = int(state.get("total") or 0)
    done = int(state.get("done") or 0)
    state["percent"] = int(round(done * 100.0 / total)) if total else 0
    _PARSE_STATE[job_id] = state
    meta = _read_meta(job_id, root)
    if meta:
        meta["parse"] = state
        meta["parsed"] = state.get("status") == "done"
        if state.get("stats"):
            meta["counts"] = state["stats"]
        _write_meta(meta, root)


def _parse_worker(job_id: str, me_names, you_names, root) -> None:
    def progress(order: int, total: int, name: str) -> None:
        _set_parse_state(job_id, root, status="running", total=total,
                         done=order, current=name)

    try:
        meta = _read_meta(job_id, root) or {}
        total = len([f for f in (meta.get("files") or []) if isinstance(f, dict)])
        _set_parse_state(job_id, root, status="running", total=total, done=0,
                         current="", error="", started_at=time.time())
        result = _parse_impl(job_id, me_names=me_names, you_names=you_names,
                             root=root, progress=progress)
        if result.get("ok"):
            _set_parse_state(job_id, root, status="done", total=total, done=total,
                             percent=100, current="", error="",
                             stats=result.get("stats") or {}, finished_at=time.time())
        else:
            _set_parse_state(job_id, root, status="failed", total=total,
                             done=result.get("files") or 0,
                             error=str(result.get("error") or "parse_failed"),
                             finished_at=time.time())
    except Exception as exc:  # noqa: BLE001
        _set_parse_state(job_id, root, status="failed", error=f"{type(exc).__name__}: {exc}"[:200],
                         finished_at=time.time())


def start_parse(job_id: str, *, me_names: Any = "我,me,assistant,小鱼,她",
                you_names: Any = "你,user,用户,对方",
                root: Path | str | None = None) -> dict:
    """启动后台清洗线程，立即返回；前端轮询 ``parse_status``。"""
    meta = _read_meta(job_id, root)
    if not meta:
        return {"ok": False, "error": "job_not_found"}
    with _PARSE_LOCK:
        state = _PARSE_STATE.get(job_id) or {}
        if state.get("status") == "running":
            return {"ok": True, "started": True, "already": True,
                    "total": state.get("total") or 0}
    files = [f for f in (meta.get("files") or []) if isinstance(f, dict)]
    if not files:
        return {"ok": False, "error": "no_files"}
    _set_parse_state(job_id, root, status="running",
                     total=len(files), done=0, current="", error="",
                     stats={}, started_at=time.time(), finished_at=0)
    thread = __import__("threading").Thread(
        target=_parse_worker, args=(job_id, me_names, you_names, root),
        name=f"corpus-parse-{job_id}", daemon=True)
    try:
        thread.start()
    except Exception as exc:  # noqa: BLE001
        _set_parse_state(job_id, root, status="failed", error=str(exc)[:200])
        return {"ok": False, "error": f"thread_failed:{exc}"[:200]}
    return {"ok": True, "started": True, "total": len(files)}


def parse_status(job_id: str, *, root: Path | str | None = None) -> dict:
    meta = _read_meta(job_id, root)
    if not meta:
        return {"ok": False, "error": "job_not_found"}
    state = _PARSE_STATE.get(job_id) or meta.get("parse") or {
        "status": "idle", "total": 0, "done": 0, "percent": 0, "current": "",
        "error": "", "stats": {}}
    has_items = load_items(job_id, root=root) is not None
    out = {"ok": True, "job_id": job_id, "has_items": has_items}
    out.update(state)
    return out

