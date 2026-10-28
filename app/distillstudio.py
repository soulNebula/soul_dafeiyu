# -*- coding: utf-8 -*-
"""蒸馏语义数据工作室：在网页上创建「灵魂 InnerState 蒸馏数据集」。

调用 ``SoulAgent`` 的 ``python -m agi.soul.dataset``（teacher 生成语义），后台跑、
写日志、可查进度；真实生成要确认短语（会调用 DeepSeek 花钱）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import paths

DATA_SUBDIR = Path("agi") / "soul" / "data"
JOBS_DIR = paths.DATA_DIR / "distill_jobs"
#: 单样本成本估算（v1 实测：prompt≈788 tok、completion≈170 tok；1 元/百万入、4 元/百万出）
COST_PER_SAMPLE_CNY = (788 * 1.0 + 170 * 4.0) / 1_000_000.0
CONFIRM_REAL = "确认生成"
MAX_LIMIT = 5000
_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,60}$")
_JOB_RE = re.compile(r"[0-9A-Za-z_\-]{6,40}")


def _home() -> Path:
    return Path(paths.soulagent_home())


def _data_dir(root: Path | str | None = None) -> Path:
    base = Path(root) if root is not None else _home()
    return base / DATA_SUBDIR


def _jobs_dir(root: Path | str | None = None) -> Path:
    if root is None:
        return JOBS_DIR
    return Path(root) / ".data" / "distill_jobs"


def _python() -> str:
    return os.environ.get("SOULAGENT_PYTHON") or r"C:\Python314\python.exe"


def _usage_of(path: Path) -> dict:
    usage_path = path.with_name(path.name + ".usage.json")
    try:
        return json.loads(usage_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _rows_of(path: Path) -> int:
    try:
        with path.open("rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def list_datasets(root: Path | str | None = None) -> dict:
    """列出已有蒸馏数据集（含派生 train/val/norm 与 usage 统计）。"""
    data_dir = _data_dir(root)
    items: list[dict] = []
    if data_dir.is_dir():
        for path in sorted(data_dir.glob("*.jsonl")):
            name = path.name
            derived = any(marker in name for marker in
                          ("_train", "_val", "_norm", ".failures"))
            usage = _usage_of(path) if not derived else {}
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append({
                "name": name, "path": str(path), "derived": derived,
                "rows": _rows_of(path), "size": stat.st_size, "mtime": stat.st_mtime,
                "sources": usage.get("by_source") or {},
                "calls": usage.get("calls") or 0,
                "tokens": {"in": usage.get("prompt_tokens") or 0,
                           "out": usage.get("completion_tokens") or 0},
                "plan": usage.get("plan") or {},
            })
    items.sort(key=lambda it: (it["derived"], -it["mtime"]))
    return {"ok": True, "items": items, "count": len(items),
            "data_dir": str(data_dir),
            "cost_per_sample": round(COST_PER_SAMPLE_CNY, 6)}


def estimate(limit: int, real: bool) -> dict:
    limit = max(1, min(MAX_LIMIT, int(limit or 300)))
    cost = round(limit * COST_PER_SAMPLE_CNY, 2) if real else 0.0
    return {"limit": limit, "real": bool(real), "cost_cny": cost,
            "cost_per_sample": round(COST_PER_SAMPLE_CNY, 6)}


def _write_meta(meta: dict, root: Path | str | None) -> None:
    jobs = _jobs_dir(root)
    jobs.mkdir(parents=True, exist_ok=True)
    tmp = jobs / (meta["id"] + ".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(jobs / (meta["id"] + ".json"))


def _read_meta(job_id: str, root: Path | str | None) -> dict | None:
    path = _jobs_dir(root) / (job_id + ".json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _tail(path: Path, n: int = 100) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def _run_job(meta: dict, root: Path | str | None) -> None:
    out = Path(meta["out"])
    log_path = Path(meta["log"])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_python(), "-m", "agi.soul.dataset",
           "--limit", str(meta["limit"]),
           "--out", str(out),
           "--budget-cny", str(meta["budget_cny"]),
           "--max-variants", str(meta["max_variants"])]
    if meta.get("real"):
        cmd.append("--real")
        if not meta.get("profile_check", True):
            cmd.append("--no-profile-check")
    else:
        cmd.append("--mock")
    if meta.get("split"):
        cmd.append("--split")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    started = time.time()
    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as log:
            log.write("[distill] " + " ".join(cmd) + "\n")
            log.flush()
            proc = subprocess.Popen(cmd, cwd=str(_home()), stdout=log,
                                    stderr=subprocess.STDOUT, env=env)
            meta["pid"] = proc.pid
            _write_meta(meta, root)
            rc = proc.wait()
        meta.update({"status": "done" if rc == 0 else "failed", "returncode": rc,
                     "finished_at": time.time(), "seconds": round(time.time() - started, 1),
                     "rows": _rows_of(out)})
        if rc != 0:
            meta["error"] = (_tail(log_path, 12) or f"exit {rc}")[:2000]
    except Exception as exc:  # noqa: BLE001
        meta.update({"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:2000],
                     "finished_at": time.time()})
    _write_meta(meta, root)


def create_job(*, limit: int = 300, real: bool = False, out_name: str = "",
               budget_cny: float = 3.05, split: bool = False, max_variants: int = 7,
               confirm: str = "", overwrite: bool = False,
               root: Path | str | None = None,
               runner: Any = None) -> dict:
    """创建并启动蒸馏数据生成任务（默认 mock 免费；real 需确认短语）。"""
    try:
        limit = max(1, min(MAX_LIMIT, int(limit)))
    except (TypeError, ValueError):
        return {"ok": False, "error": "limit_invalid"}
    if real and str(confirm or "").strip() != CONFIRM_REAL:
        return {"ok": False, "error": "confirm_required",
                "hint": f"真实生成会调用 DeepSeek 花钱，请输入确认短语「{CONFIRM_REAL}」"}
    name = str(out_name or "").strip() or (
        f"soul_sft_web_{time.strftime('%m%d_%H%M')}.jsonl")
    if not name.endswith(".jsonl"):
        name += ".jsonl"
    stem = name[:-len(".jsonl")]
    if not _NAME_RE.match(stem):
        return {"ok": False, "error": "name_invalid",
                "hint": "输出名只能包含字母/数字/下划线/短横线"}
    data_dir = _data_dir(root)
    data_dir.mkdir(parents=True, exist_ok=True)
    out = data_dir / name
    if out.exists() and not overwrite:
        return {"ok": False, "error": "out_exists",
                "hint": f"已存在 {name}，换个名字或允许覆盖"}
    job_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    jobs = _jobs_dir(root)
    jobs.mkdir(parents=True, exist_ok=True)
    meta = {"id": job_id, "created": time.time(), "status": "running",
            "limit": limit, "real": bool(real), "out": str(out),
            "budget_cny": float(budget_cny or 3.05), "split": bool(split),
            "max_variants": max(1, min(7, int(max_variants or 7))),
            "profile_check": True,
            "log": str(jobs / (job_id + ".log")),
            "estimate_cost_cny": estimate(limit, real)["cost_cny"]}
    _write_meta(meta, root)
    if runner is not None:
        runner(meta, root)
    else:
        threading.Thread(target=_run_job, args=(meta, root),
                         name=f"distill-{job_id}", daemon=True).start()
    return {"ok": True, "job": meta, "estimate": estimate(limit, real)}


def get_job(job_id: str, root: Path | str | None = None) -> dict:
    job_id = str(job_id or "").strip()
    if not _JOB_RE.fullmatch(job_id):
        return {"ok": False, "error": "job_id_invalid"}
    meta = _read_meta(job_id, root)
    if not meta:
        return {"ok": False, "error": "job_not_found"}
    return {"ok": True, "job": meta}


def list_jobs(limit: int = 20, root: Path | str | None = None) -> dict:
    jobs = _jobs_dir(root)
    items = []
    if jobs.is_dir():
        for path in jobs.glob("*.json"):
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            items.append(meta)
    items.sort(key=lambda it: it.get("created") or 0, reverse=True)
    return {"ok": True, "items": items[:max(1, min(100, limit))], "count": len(items)}


def read_log(job_id: str, tail: int = 200, root: Path | str | None = None) -> dict:
    meta = _read_meta(str(job_id or "").strip(), root)
    if not meta:
        return {"ok": False, "error": "job_not_found"}
    text = _tail(Path(meta.get("log") or ""), max(1, min(2000, int(tail or 200))))
    return {"ok": True, "job_id": meta["id"], "status": meta.get("status"),
            "lines": text.splitlines() if text else []}


def cancel_job(job_id: str, root: Path | str | None = None) -> dict:
    meta = _read_meta(str(job_id or "").strip(), root)
    if not meta:
        return {"ok": False, "error": "job_not_found"}
    if meta.get("status") != "running":
        return {"ok": False, "error": "not_running", "status": meta.get("status")}
    pid = int(meta.get("pid") or 0)
    if not pid:
        return {"ok": False, "error": "pid_unknown", "hint": "任务刚启动，稍后再取消"}
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": f"kill_failed: {exc}"}
    meta.update({"status": "cancelled", "finished_at": time.time()})
    _write_meta(meta, root)
    return {"ok": True, "job": meta}
