# -*- coding: utf-8 -*-
"""M3-A TrainLab 后端：数据集/资产发现、后台训练任务、导出链（merge→GGUF）、状态/日志。

只提供纯函数（路由由 Lead 接 main.py）。安全边界：
- 数据集只在 SoulAgent 与 AGENT_HOME 下；
- 训练/合并/转换统一用 E:/agent/venv312/Scripts/python.exe（可 env 覆盖）；
- 所有子进程后台起，日志落 ``idea/.data/trainjobs/<id>.log``；
- 测试不会真训练（Popen/run 可 mock）。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import audit, paths

TRAIN_PYTHON = os.environ.get("IDEA_TRAIN_PYTHON", r"E:/agent/venv312/Scripts/python.exe")
EXPORT_PYTHON = os.environ.get("IDEA_EXPORT_PYTHON", r"C:/Python314/python.exe")
TRAIN_FORMATS = ("auto", "messages", "instruction", "soul")
TRAIN_MODES = ("lora", "full-ft")
DEFAULT_MODEL = "E:/agent/models/Qwen2.5-0.5B-Instruct"
DEFAULT_GGUF_DIR = "E:/agent/output/gguf"
DEFAULT_OUTPUT_ROOT = "E:/agent/output"
_NAME_RE = re.compile(r"^[A-Za-z0-9_\-.]+$")
_JOBS: dict[str, dict] = {}
_PROCS: dict[str, Any] = {}
_LOCK = threading.Lock()


def _jobs_dir() -> Path:
    target = Path(paths.DATA_DIR) / "trainjobs"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _meta_path(job_id: str) -> Path:
    return _jobs_dir() / f"{job_id}.json"


def _log_path(job_id: str) -> Path:
    return _jobs_dir() / f"{job_id}.log"


def _write_job(job: dict) -> None:
    try:
        _meta_path(job["id"]).write_text(
            json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _read_job(job_id: str) -> dict | None:
    with _LOCK:
        if job_id in _JOBS:
            return dict(_JOBS[job_id])
    path = _meta_path(job_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _active_bot_root() -> Path | None:
    try:
        import sys as _sys
        repo = Path(__file__).resolve().parents[2]
        if str(repo) not in _sys.path:
            _sys.path.insert(0, str(repo))
        from agi import bot_runtime
        active = bot_runtime.read_active()
        if active and active.get("path"):
            return Path(active["path"]).resolve()
    except Exception:  # noqa: BLE001
        pass
    return None


def _allowed_roots() -> list[Path]:
    roots = [Path(paths.soulagent_home()).resolve(), Path(paths.agent_home()).resolve()]
    bot = _active_bot_root()
    if bot is not None:
        roots.append(bot)
    result = []
    for root in roots:
        if root not in result:
            result.append(root)
    return result


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def resolve_dataset(path: str) -> tuple[Path | None, str]:
    raw = str(path or "").strip()
    if not raw:
        return None, "dataset_required"
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path(paths.soulagent_home()) / raw
    try:
        target = candidate.resolve()
    except OSError:
        return None, "bad_path"
    if not target.is_file() or target.suffix.lower() not in (".jsonl", ".json"):
        return None, "not_a_dataset_file"
    if not any(_is_under(target, root) for root in _allowed_roots()):
        return None, "path_out_of_allowed_roots"
    return target, ""


def dataset_info(path: str) -> dict:
    target, error = resolve_dataset(path)
    if not target:
        return {"ok": False, "error": error}
    lines = 0
    sample = None
    try:
        with target.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                lines += 1
                if sample is None and lines <= 3:
                    try:
                        sample = json.loads(line)
                    except json.JSONDecodeError:
                        sample = {"_raw": line[:160]}
        stat = target.stat()
    except OSError as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": True, "path": str(target), "name": target.name,
            "size": stat.st_size, "mtime": stat.st_mtime,
            "rows": lines, "sample": sample}


def _dataset_kind(path: Path) -> str:
    name = path.name.lower()
    if "dpo" in name or "preference" in name:
        return "dpo"
    if "raw" in name or "corpus" in name or "subtitle" in name:
        return "raw"
    return "sft"


def _detect_format(rows: list[dict]) -> str:
    """从样本行猜数据集格式：messages / instruction / soul / unknown。"""
    for row in rows:
        if not isinstance(row, dict):
            continue
        if isinstance(row.get("messages"), list):
            return "messages"
        if "instruction" in row or ("input" in row and "output" in row):
            return "instruction"
        if "state_profile" in row or "soul" in row or "chosen" in row:
            return "soul"
        if "user_msg" in row and ("assistant_msg" in row or "response" in row):
            return "instruction"
    return "unknown"


def preview_dataset(path: str, *, limit: int = 20) -> dict:
    target, error = resolve_dataset(path)
    if not target:
        return {"ok": False, "error": error}
    limit = max(1, min(100, int(limit)))
    rows = []
    try:
        with target.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    rows.append({"_raw": line[:300]})
                if len(rows) >= limit:
                    break
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "path": str(target), "rows": rows, "count": len(rows),
            "format": _detect_format(rows)}


def _row_text_len(row: dict) -> int:
    """过滤用的正文长度：优先 user_msg / messages 里的 user+response，其次 text。"""
    if isinstance(row.get("user_msg"), str) and row["user_msg"].strip():
        return len(row["user_msg"].strip())
    messages = row.get("messages")
    if isinstance(messages, list):
        text_len = sum(len(str(m.get("content") or "")) for m in messages
                       if isinstance(m, dict))
        response = len(str(row.get("response") or ""))
        return max(text_len, response)
    for key in ("text", "content", "input", "prompt"):
        if isinstance(row.get(key), str) and row[key].strip():
            return len(row[key].strip())
    return len(json.dumps(row, ensure_ascii=False))


def filter_dataset(path: str, *, name: str, min_chars: int = 0, max_chars: int = 0,
                   blacklist: list[str] | None = None, dedupe: bool = True) -> dict:
    """按长度/关键词去重过滤，生成 ``agi/data/train_work/<name>.jsonl``。"""
    target, error = resolve_dataset(path)
    if not target:
        return {"ok": False, "error": error}
    if not _NAME_RE.match(str(name or "")):
        return {"ok": False, "error": "invalid_name"}
    out_dir = Path(paths.soulagent_home()) / "agi" / "data" / "train_work"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{name}.jsonl"
    if not any(_is_under(out_path, root) for root in _allowed_roots()):
        return {"ok": False, "error": "path_out_of_allowed_roots"}
    banned = [str(item) for item in (blacklist or []) if str(item).strip()]
    seen = set()
    kept = removed = 0
    stats = {"too_short": 0, "too_long": 0, "blacklist": 0, "duplicate": 0, "parse": 0}
    try:
        with target.open(encoding="utf-8", errors="replace") as src,                 out_path.open("w", encoding="utf-8") as dst:
            for line in src:
                text = line.strip()
                if not text:
                    continue
                try:
                    row = json.loads(text)
                except json.JSONDecodeError:
                    stats["parse"] += 1
                    removed += 1
                    continue
                length = _row_text_len(row)
                if min_chars and length < int(min_chars):
                    stats["too_short"] += 1
                    removed += 1
                    continue
                if max_chars and length > int(max_chars):
                    stats["too_long"] += 1
                    removed += 1
                    continue
                if any(word in text for word in banned):
                    stats["blacklist"] += 1
                    removed += 1
                    continue
                if dedupe:
                    if text in seen:
                        stats["duplicate"] += 1
                        removed += 1
                        continue
                    seen.add(text)
                dst.write(text + "\n")
                kept += 1
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "input": str(target), "output": str(out_path),
            "kept": kept, "removed": removed, "stats": stats}


def _scan_datasets(root: Path, label: str) -> list[dict]:
    result = []
    if not root.is_dir():
        return result
    for path in root.rglob("*.jsonl"):
        try:
            if not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        result.append({"path": str(path.resolve()),
                       "rel": path.relative_to(root).as_posix(),
                       "name": path.name, "root": label,
                       "kind": _dataset_kind(path),
                       "size": stat.st_size, "mtime": stat.st_mtime})
    return result


def list_datasets() -> dict:
    soul = Path(paths.soulagent_home())
    agent = Path(paths.agent_home())
    items = _scan_datasets(soul / "agi" / "data" / "train", "soulagent/train")
    items += _scan_datasets(soul / "agi" / "eval_data", "soulagent/eval")
    for extra in (agent / "datasets", agent / "output" / "datasets"):
        items += _scan_datasets(extra, "agent")
    bot = _active_bot_root()
    if bot is not None:
        items += _scan_datasets(bot / "data" / "train" / "packages", "active_bot/packages")
        items += _scan_datasets(bot / "data" / "train" / "daily", "active_bot/daily")
    # 模型根目录下临时/训练语料（限制浅层，不全盘扫 models）
    for base in (agent,):
        try:
            for path in base.glob("*.jsonl"):
                if path.is_file():
                    stat = path.stat()
                    items.append({"path": str(path.resolve()), "rel": path.name,
                                  "name": path.name, "root": "agent",
                                  "size": stat.st_size, "mtime": stat.st_mtime})
        except OSError:
            pass
    seen = set()
    unique = []
    for item in sorted(items, key=lambda x: x["mtime"], reverse=True):
        if item["path"] in seen:
            continue
        seen.add(item["path"])
        try:
            item["rows"] = sum(1 for line in Path(item["path"]).read_text(
                encoding="utf-8", errors="replace").splitlines() if line.strip())
            item["lines"] = item["rows"]
        except OSError:
            item["rows"] = None
        unique.append(item)
    return {"ok": True, "items": unique, "count": len(unique)}


def list_assets() -> dict:
    agent = Path(paths.agent_home())
    soul = Path(paths.soulagent_home())
    adapters = []
    for base in (agent / "output", soul / "agi" / "data" / "train"):
        if not base.is_dir():
            continue
        for path in base.rglob("adapter_config.json"):
            try:
                adapter_dir = path.parent.resolve()
                adapters.append({"path": str(adapter_dir), "name": adapter_dir.name,
                                 "mtime": adapter_dir.stat().st_mtime})
            except OSError:
                continue
    ggufs = []
    for base in (agent / "output" / "gguf", agent / "models"):
        if not base.is_dir():
            continue
        for path in base.glob("*.gguf"):
            try:
                stat = path.stat()
                ggufs.append({"path": str(path.resolve()), "name": path.name,
                              "size": stat.st_size, "mtime": stat.st_mtime})
            except OSError:
                continue
    bases = []
    for base in (agent / "models",):
        if base.is_dir():
            for path in base.iterdir():
                if path.is_dir() and (path / "config.json").is_file():
                    bases.append({"path": str(path.resolve()), "name": path.name})
    seen = set()
    adapter_unique = []
    for item in sorted(adapters, key=lambda x: x["mtime"], reverse=True):
        if item["path"] in seen:
            continue
        seen.add(item["path"])
        adapter_unique.append(item)
    return {"ok": True, "adapters": adapter_unique,
            "gguf": sorted(ggufs, key=lambda x: x["mtime"], reverse=True),
            "bases": sorted(bases, key=lambda x: x["name"])}


def discover() -> dict:
    """TrainLab 首屏：数据集 + adapter + GGUF + base 模型。"""
    datasets = list_datasets()
    assets = list_assets()
    return {"ok": bool(datasets.get("ok") and assets.get("ok")),
            "datasets": datasets.get("items", []),
            "adapters": assets.get("adapters", []),
            "ggufs": assets.get("gguf", []),
            "bases": assets.get("bases", [])}


def list_sources() -> dict:
    """可导出的聊天库：联系人 memory sqlite + 主 selflearn memory sqlite。"""
    soul = Path(paths.soulagent_home())
    roots = [soul / "agi" / "data" / "selflearn" / "wechat_contacts",
             soul / "agi" / "data" / "selflearn"]
    items: list[dict] = []
    seen: set[str] = set()
    for root in roots:
        if not root.is_dir():
            continue
        pattern = "*.memory.sqlite3" if "wechat_contacts" in str(root) else "selflearn.memory.sqlite3"
        try:
            found = sorted(root.glob(pattern))
        except OSError:
            found = []
        for db in found:
            key = str(db.resolve())
            if key in seen:
                continue
            seen.add(key)
            rows = 0
            last = 0.0
            try:
                con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
                try:
                    rows = int(con.execute("SELECT count(*) FROM messages").fetchone()[0])
                    last = float(con.execute(
                        "SELECT coalesce(max(ts),0) FROM messages").fetchone()[0] or 0)
                finally:
                    con.close()
            except sqlite3.Error:
                continue
            items.append({"db": str(db.resolve()), "contact": db.name.split(".")[0],
                          "rows": rows, "last": last, "mtime": db.stat().st_mtime})
    items.sort(key=lambda row: row.get("mtime") or 0, reverse=True)
    return {"ok": True, "items": items, "count": len(items)}


def export_chat(source_db: str, *, out_name: str = "", exclude_test: bool = True,
                max_rows: int = 2000, window: int = 6, split: float = 0.0) -> dict:
    """聊天库 → messages jsonl（子进程 tools/train/export_chat.py）。"""
    raw = str(source_db or "").strip()
    if not raw:
        return {"ok": False, "error": "source_db_required"}
    db = Path(raw).expanduser()
    if not db.is_absolute():
        db = Path(paths.soulagent_home()) / db
    try:
        db = db.resolve()
    except OSError:
        return {"ok": False, "error": "bad_path"}
    if not db.is_file() or db.suffix.lower() not in (".sqlite3", ".db"):
        return {"ok": False, "error": "not_a_sqlite_file", "path": str(db)}
    if not any(_is_under(db, root) for root in _allowed_roots()):
        return {"ok": False, "error": "path_out_of_allowed_roots"}
    root = Path(paths.soulagent_home())
    script = root / "tools" / "train" / "export_chat.py"
    if not script.is_file():
        return {"ok": False, "error": "export_script_missing", "path": str(script)}
    out_root = Path(paths.agent_home()) / "datasets"
    name = str(out_name or "").strip()
    if name and not _NAME_RE.match(name):
        return {"ok": False, "error": "invalid_name"}
    if not name:
        name = "chat_" + time.strftime("%Y%m%d_%H%M%S")
    target_out = out_root / f"{name}.jsonl"
    cmd = [str(EXPORT_PYTHON), str(script), "--db", str(db),
           "--out", str(target_out),
           "--max-rows", str(max(1, int(max_rows))),
           "--window", str(max(1, int(window))),
           "--split", str(max(0.0, min(0.9, float(split))))]
    if exclude_test:
        cmd.append("--exclude-test")
    try:
        result = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=300)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "export_timeout", "hint": ">300s"}
    except OSError as exc:
        return {"ok": False, "error": f"export_failed:{type(exc).__name__}: {exc}"}
    if result.returncode != 0:
        return {"ok": False, "error": "export_failed",
                "hint": (result.stderr or result.stdout or "")[-800:]}
    payload = {}
    for line in reversed((result.stdout or "").splitlines()):
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    try:
        candidates = sorted(
            [p for p in out_root.glob(f"{name}*.jsonl") if not p.stem.endswith("_val")],
            key=lambda p: p.stat().st_mtime, reverse=True)
        out_path = candidates[0] if candidates else None
        rows = int(payload.get("samples") or payload.get("rows") or 0) if isinstance(payload, dict) else 0
        if out_path and out_path.is_file() and not rows:
            rows = sum(1 for line in out_path.read_text(
                encoding="utf-8", errors="replace").splitlines() if line.strip())
        val_path = out_root / f"{name}.val.jsonl"
    except OSError:
        out_path, rows, val_path = None, 0, out_root / f"{name}.val.jsonl"
    audit.record("train.export_chat", str(db), actor="workbench",
                 meta={"out": str(out_path) if out_path else "", "rows": rows,
                       "max_rows": max_rows, "window": window, "split": split})
    return {"ok": True, "out": str(out_path) if out_path else "",
            "rows": int(payload.get("rows") or rows),
            "val_out": str(val_path) if val_path.is_file() else "",
            "error": ""}


def training_plan(dataset: str, *, model_path: str = DEFAULT_MODEL,
                  output: str = "", max_seq_len: int = 512, epochs: int = 1,
                  lr: float = 2e-4, lora_r: int = 16, lora_alpha: int = 32,
                  batch_size: int = 1, grad_accum: int = 1,
                  max_steps: int = 0, load_4bit: bool = False,
                  dry_run: bool = False, format: str = "auto",
                  mode: str = "lora") -> dict:
    target, error = resolve_dataset(dataset)
    if not target:
        return {"ok": False, "error": error}
    fmt = str(format or "auto").strip().lower()
    if fmt not in TRAIN_FORMATS:
        return {"ok": False, "error": "invalid_format", "allowed": list(TRAIN_FORMATS)}
    run_mode = str(mode or "lora").strip().lower()
    if run_mode not in TRAIN_MODES:
        return {"ok": False, "error": "invalid_mode", "allowed": list(TRAIN_MODES)}
    if fmt == "auto":
        try:
            sample = preview_dataset(str(target), limit=5)
            detected = str(sample.get("format") or "unknown")
        except Exception:  # noqa: BLE001
            detected = "unknown"
    else:
        detected = fmt
    model_path = str(model_path or DEFAULT_MODEL)
    root = Path(paths.soulagent_home())
    adapter = Path(output) if output else (
        Path(paths.agent_home()) / "output" / f"idea_lora_{time.strftime('%Y%m%d_%H%M%S')}")
    if not adapter.is_absolute():
        adapter = root / adapter
    use_smoke = (run_mode == "lora" and fmt == "auto"
                 and detected in ("soul", "unknown"))  # 兼容旧 unknown 数据集
    script = root / "tools" / "train" / ("smoke_lora.py" if use_smoke else "sft_train.py")
    cmd = [str(TRAIN_PYTHON), str(script), "--data", str(target),
           "--model-path", str(model_path)]
    if use_smoke:
        cmd.extend(["--save-adapter", str(adapter),
                    "--lora-r", str(int(lora_r)),
                    "--lora-alpha", str(int(lora_alpha))])
        if load_4bit:
            cmd.append("--load-4bit")
    else:
        # sft_train.py 的 LoRA 输出是 --save-adapter，全参微调输出是 --out（10-29 修）
        if run_mode == "full-ft":
            cmd.extend(["--out", str(adapter)])
        else:
            cmd.extend(["--save-adapter", str(adapter)])
        cmd.extend(["--format",
                    detected if detected in TRAIN_FORMATS[1:] else "messages"])
        if run_mode == "full-ft":
            cmd.append("--full-ft")
        else:
            cmd.extend(["--lora-r", str(int(lora_r)),
                        "--lora-alpha", str(int(lora_alpha))])
            if load_4bit:
                cmd.append("--load-4bit")
    cmd.extend(["--max-seq-len", str(int(max_seq_len)),
                "--epochs", str(int(epochs)), "--lr", str(float(lr)),
                "--batch-size", str(int(batch_size)),
                "--grad-accum", str(int(grad_accum))])
    if int(max_steps) > 0:
        cmd.extend(["--max-steps", str(int(max_steps))])
    if dry_run:
        cmd.append("--dry-run")
    return {"ok": True, "cmd": cmd, "cwd": str(root), "dataset": str(target),
            "adapter": str(adapter), "python": TRAIN_PYTHON,
            "format": detected, "mode": run_mode,
            "engine": "smoke_lora" if use_smoke else "sft_train"}


def _json_dumps(value) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(value)


def _new_job(kind: str, **fields) -> dict:
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "kind": kind, "status": "running",
           "started_at": time.time(), "ended_at": None, "returncode": None,
           "pid": None, "log": str(_log_path(job_id))}
    job.update(fields)
    with _LOCK:
        _JOBS[job_id] = job
    _write_job(job)
    return job


def _finish_job(job_id: str, returncode: int | None, status: str) -> None:
    with _LOCK:
        job = _JOBS.get(job_id)
        if job:
            if job.get("status") == "stopped":
                return
            job["returncode"] = returncode
            job["status"] = status
            job["ended_at"] = time.time()
            _write_job(job)
        _PROCS.pop(job_id, None)


def _launch_training(job: dict) -> None:
    log_path = Path(job["log"])

    def worker():
        try:
            with log_path.open("ab") as log:
                proc = subprocess.Popen(
                    job.get("cmd") or [], cwd=job.get("cwd") or "",
                    stdout=log, stderr=subprocess.STDOUT,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8"})
                with _LOCK:
                    _PROCS[job["id"]] = proc
                    if job["id"] in _JOBS:
                        _JOBS[job["id"]]["pid"] = proc.pid
                _write_job(_read_job(job["id"]) or job)
                code = proc.wait()
            _finish_job(job["id"], code, "done" if code == 0 else "failed")
            audit.record("train.run", job.get("dataset") or "", actor="workbench",
                         meta={"job": job["id"], "adapter": job.get("adapter"),
                               "returncode": code})
        except Exception as exc:  # noqa: BLE001
            try:
                log_path.open("a", encoding="utf-8").write("[异常] " + str(exc))
            except OSError:
                pass
            _finish_job(job["id"], None, "failed")
            audit.record("train.error", job.get("dataset") or "", actor="workbench",
                         meta={"job": job["id"], "error": str(exc)})

    threading.Thread(target=worker, name=f"train-{job['id']}", daemon=True).start()


def create_training_job(dataset: str, *, model_path: str = DEFAULT_MODEL,
                        output: str = "", out_name: str = "",
                        steps: int = 0, lr: float = 2e-4,
                        lora_r: int = 16, lora_alpha: int = 32,
                        load_4bit: bool = False, max_len: int = 512,
                        epochs: int = 1, format: str = "auto",
                        mode: str = "lora") -> dict:
    model_path = str(model_path or DEFAULT_MODEL)
    if out_name and not output:
        if not _NAME_RE.match(str(out_name)):
            return {"ok": False, "error": "invalid_name"}
        output = str(Path(paths.agent_home()) / "output" / str(out_name))
    plan = training_plan(dataset, model_path=model_path, output=output,
                         max_seq_len=max_len, epochs=epochs, lr=lr,
                         lora_r=lora_r, lora_alpha=lora_alpha,
                         max_steps=steps, load_4bit=load_4bit,
                         format=format, mode=mode)
    if not plan.get("ok"):
        return plan
    job = _new_job("train", status="created", cmd=plan["cmd"], cwd=plan["cwd"],
                   dataset=plan["dataset"], adapter=plan["adapter"],
                   params={"steps": steps, "lr": lr, "lora_r": lora_r,
                           "lora_alpha": lora_alpha, "load_4bit": load_4bit,
                           "max_len": max_len, "epochs": epochs,
                           "format": plan.get("format") or format,
                           "mode": plan.get("mode") or mode,
                           "engine": plan.get("engine")})
    _write_job(job)
    audit.record("train.create", plan["dataset"], actor="workbench",
                 meta={"job": job["id"], "adapter": plan["adapter"]})
    job_ref = {"id": job["id"], "status": "created", "kind": "train",
               "log": job["log"], "adapter": plan["adapter"]}
    return {"ok": True, "job": job_ref, "job_id": job["id"],
            "status": "created", "cmd": plan["cmd"], "log": job["log"]}


create_job = create_training_job


def start_job(job_id: str) -> dict:
    job = _read_job(job_id)
    if not job:
        return {"ok": False, "error": "job_not_found"}
    if job.get("kind") != "train":
        return {"ok": False, "error": "not_a_train_job"}
    if job.get("status") not in ("created", "stopped"):
        return {"ok": False, "error": "job_not_startable", "status": job.get("status")}
    with _LOCK:
        if job_id in _JOBS:
            _JOBS[job_id]["status"] = "running"
            _JOBS[job_id]["started_at"] = time.time()
            _write_job(_JOBS[job_id])
    _launch_training(job)
    audit.record("train.start", job.get("dataset") or "", actor="workbench",
                 meta={"job": job_id, "adapter": job.get("adapter")})
    return {"ok": True, "job": job_id, "status": "running"}


def stop_job(job_id: str) -> dict:
    job = _read_job(job_id)
    if not job:
        return {"ok": False, "error": "job_not_found"}
    if job.get("status") in ("done", "failed"):
        return {"ok": False, "error": "job_already_finished", "status": job.get("status")}
    with _LOCK:
        proc = _PROCS.get(job_id)
        if job_id in _JOBS:
            _JOBS[job_id]["status"] = "stopped"
            _JOBS[job_id]["ended_at"] = time.time()
            _write_job(_JOBS[job_id])
    if proc is not None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                proc.kill()
        except Exception:  # noqa: BLE001
            pass
    audit.record("train.stop", job.get("dataset") or "", actor="workbench",
                 meta={"job": job_id})
    return {"ok": True, "job": job_id, "status": "stopped"}


def start_training(dataset: str, *, model_path: str = DEFAULT_MODEL,
                   output: str = "", **params) -> dict:
    plan = training_plan(dataset, model_path=model_path, output=output, **params)
    if not plan.get("ok"):
        return plan
    job = _new_job("train", cmd=plan["cmd"], cwd=plan["cwd"],
                   dataset=plan["dataset"], adapter=plan["adapter"])
    log_path = Path(job["log"])

    def worker():
        try:
            with log_path.open("ab") as log:
                proc = subprocess.Popen(plan["cmd"], cwd=plan["cwd"],
                                        stdout=log, stderr=subprocess.STDOUT,
                                        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
                with _LOCK:
                    if job["id"] in _JOBS:
                        _JOBS[job["id"]]["pid"] = proc.pid
                _write_job(_read_job(job["id"]) or job)
                code = proc.wait()
            _finish_job(job["id"], code, "done" if code == 0 else "failed")
            audit.record("train.run", plan["dataset"], actor="workbench",
                         meta={"job": job["id"], "adapter": plan["adapter"],
                               "returncode": code})
        except Exception as exc:  # noqa: BLE001
            try:
                log_path.open("a", encoding="utf-8").write(f"\n[异常] {exc}\n")
            except OSError:
                pass
            _finish_job(job["id"], None, "failed")
            audit.record("train.error", plan["dataset"], actor="workbench",
                         meta={"job": job["id"], "error": str(exc)})

    threading.Thread(target=worker, name=f"train-{job['id']}", daemon=True).start()
    return {"ok": True, "job": {"id": job["id"], "status": "running",
                                "kind": "train", "log": job["log"],
                                "adapter": plan["adapter"]},
            "job_id": job["id"], "cmd": plan["cmd"], "log": job["log"]}


def export_plan(adapter: str, base: str, name: str, *, out_dir: str = DEFAULT_GGUF_DIR,
                merged_dir: str = "", quant: str = "Q4_K_M",
                convert_dir: str = "E:/agent/llama/convert",
                llama_bin: str = "E:/agent/llama/cuda") -> dict:
    adapter_path = Path(adapter)
    base_path = Path(base)
    if not _NAME_RE.match(str(name or "")):
        return {"ok": False, "error": "invalid_name"}
    if not (adapter_path / "adapter_config.json").is_file():
        return {"ok": False, "error": "adapter_config_missing", "path": str(adapter_path)}
    if not base_path.is_dir():
        return {"ok": False, "error": "base_missing", "path": str(base_path)}
    root = Path(paths.soulagent_home())
    merged = Path(merged_dir) if merged_dir else (
        Path(paths.agent_home()) / "output" / f"merged_{name}")
    merge_cmd = [str(TRAIN_PYTHON), str(root / "tools" / "soul_serve" / "merge_adapter.py"),
                 "--base", str(base_path), "--adapter", str(adapter_path),
                 "--out", str(merged)]
    convert_cmd = [str(TRAIN_PYTHON), str(root / "tools" / "soul_serve" / "convert.py"),
                   "--hf-dir", str(merged), "--name", str(name),
                   "--out-dir", str(out_dir), "--quant", str(quant),
                   "--llama-bin", str(llama_bin), "--convert-dir", str(convert_dir)]
    return {"ok": True, "merge_cmd": merge_cmd, "convert_cmd": convert_cmd,
            "merged_dir": str(merged), "out_dir": str(out_dir),
            "gguf": str(Path(out_dir) / f"{name}-{str(quant).lower()}.gguf"),
            "cwd": str(root)}


def register_export_model(*, name: str, gguf: str, base: str, adapter: str) -> dict:
    """把导出成功的 GGUF 注册进当前 Bot（或默认）的 local_models.json。"""
    import json as _json
    try:
        bot_root = None
        import sys as _sys
        repo = Path(__file__).resolve().parents[2]
        if str(repo) not in _sys.path:
            _sys.path.insert(0, str(repo))
        from agi import bot_runtime
        active = bot_runtime.read_active()
        if active and active.get("path"):
            bot_root = Path(active["path"])
    except Exception:  # noqa: BLE001
        bot_root = None
    if bot_root is not None:
        path = bot_root / "data" / "local_models.json"
    else:
        path = Path(paths.soulagent_home()) / "agi" / "data" / "local_models.json"
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        data = {"llama_bin": str(Path(paths.agent_home()) / "llama" / "cuda"),
                "torch_lib": str(Path(paths.agent_home()) / "venv312" / "Lib" / "site-packages" / "torch" / "lib"),
                "models": [], "extra_adapters": []}
    models = data.get("models")
    if not isinstance(models, list):
        models = []
    used = {int(m.get("port") or 0) for m in models if isinstance(m, dict)}
    port = next((p for p in range(8126, 8150) if p not in used), 8126)
    entry = {"name": name, "base": base, "adapter": adapter, "gguf": gguf,
             "port": port, "role": "soul", "status": "ready",
             "source": "train_export", "purpose": "训练导出（803 自动注册）"}
    models = [m for m in models if not (isinstance(m, dict) and m.get("name") == name)]
    models.append(entry)
    data["models"] = models
    data.setdefault("extra_adapters", [])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(data, ensure_ascii=False, indent=2) + chr(10), encoding="utf-8")
    return {"ok": True, "path": str(path), "model": entry}


def start_export(adapter: str, base: str, name: str, **params) -> dict:
    plan = export_plan(adapter, base, name, **params)
    if not plan.get("ok"):
        return plan
    job = _new_job("export", cmd=[plan["merge_cmd"], plan["convert_cmd"]],
                   cwd=plan["cwd"], merged_dir=plan["merged_dir"],
                   out_dir=plan["out_dir"], gguf=plan["gguf"],
                   name=name, base=str(base), adapter=str(adapter))
    log_path = Path(job["log"])

    def worker():
        code = 0
        try:
            with log_path.open("ab") as log:
                for step in (plan["merge_cmd"], plan["convert_cmd"]):
                    log.write(("[export] $ " + " ".join(step) + "\n").encode("utf-8"))
                    log.flush()
                    proc = subprocess.run(step, cwd=plan["cwd"], stdout=log,
                                          stderr=subprocess.STDOUT)
                    code = int(proc.returncode)
                    if code != 0:
                        break
            if code == 0 and not Path(plan["gguf"]).is_file():
                log_path.open("a", encoding="utf-8").write("[导出] 产物缺失: " + plan["gguf"])
                code = 1
                with _LOCK:
                    if job["id"] in _JOBS:
                        _JOBS[job["id"]]["product_missing"] = plan["gguf"]
            if code == 0:
                try:
                    reg = register_export_model(name=name, gguf=plan["gguf"],
                                                base=str(base), adapter=str(adapter))
                    with log_path.open("a", encoding="utf-8") as log:
                        log.write("[注册] " + _json_dumps(reg) + chr(10))
                    audit.record("train.export_register", plan["gguf"], actor="workbench",
                                 meta={"job": job["id"], "registry": reg.get("path")})
                except Exception as exc:  # noqa: BLE001
                    with log_path.open("a", encoding="utf-8") as log:
                        log.write("[注册失败] " + str(exc) + chr(10))
            _finish_job(job["id"], code, "done" if code == 0 else "failed")
            audit.record("train.export", plan["gguf"], actor="workbench",
                         meta={"job": job["id"], "returncode": code})
        except Exception as exc:  # noqa: BLE001
            _finish_job(job["id"], None, "failed")
            audit.record("train.export_error", plan["gguf"], actor="workbench",
                         meta={"job": job["id"], "error": str(exc)})

    threading.Thread(target=worker, name=f"export-{job['id']}", daemon=True).start()
    job_ref = {"id": job["id"], "status": "running", "kind": "export",
               "log": job["log"], "gguf": plan["gguf"]}
    return {"ok": True, "job": job_ref, "job_id": job["id"],
            "cmd": [plan["merge_cmd"], plan["convert_cmd"]],
            "log": job["log"], "gguf": plan["gguf"]}


def job_status(job_id: str, *, tail: int = 80) -> dict:
    job = _read_job(job_id)
    if not job:
        return {"ok": False, "error": "job_not_found"}
    log_path = Path(job.get("log") or _log_path(job_id))
    lines = []
    size = 0
    if log_path.exists():
        try:
            raw = log_path.read_text(encoding="utf-8", errors="replace")
            size = len(raw)
            lines = raw.splitlines()[-max(1, int(tail)):]
        except OSError:
            lines = []
    result = dict(job)
    metrics = None
    adapter = job.get("adapter")
    if adapter:
        metrics_path = Path(adapter) / "train_metrics.json"
        if metrics_path.is_file():
            try:
                metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                metrics = None
    if metrics is None:
        # smoke_lora 不写 metrics 文件，但从日志末尾的 [smoke] SUMMARY 读
        for line in reversed(lines):
            marker = "[smoke] SUMMARY "
            if marker in line:
                try:
                    metrics = json.loads(line.split(marker, 1)[1])
                except json.JSONDecodeError:
                    metrics = None
                break
    result.update({"ok": True, "log_size": size, "tail": lines,
                   "log": str(log_path), "metrics": metrics})
    return result


def tail_log(job_id: str, *, lines: int = 100) -> dict:
    job = _read_job(job_id)
    if not job:
        return {"ok": False, "error": "job_not_found"}
    path = Path(job.get("log") or _log_path(job_id))
    if not path.exists():
        return {"ok": True, "id": job_id, "lines": [], "size": 0, "status": job.get("status")}
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "id": job_id, "status": job.get("status"),
            "lines": raw.splitlines()[-max(1, int(lines)):], "size": len(raw)}


def list_jobs(limit: int = 30) -> dict:
    jobs = []
    seen = set()
    with _LOCK:
        for job in _JOBS.values():
            seen.add(job["id"])
            jobs.append(dict(job))
    try:
        for path in _jobs_dir().glob("*.json"):
            job_id = path.stem
            if job_id in seen:
                continue
            try:
                jobs.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    except OSError:
        pass
    jobs.sort(key=lambda item: item.get("started_at") or 0, reverse=True)
    return {"ok": True, "items": jobs[:max(1, int(limit))], "count": len(jobs)}
