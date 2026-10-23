"""AGI 草稿服务（只读 + dry-run）。

职责：
1. 扫描 ``agi/data/outbox*`` 下的草稿 md，解析成结构化条目；
2. 触发一次 ``pulse --once --dry-run``（后台线程，前台轮询状态）；
3. 汇报库内计数。

**本模块不包含任何发送能力**——没有 send / 网络推送代码，
所有产出只能由用户手工复制。红线见 ``agi/RUNBOOK.md`` §1 第 9 条。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AGI_DIR = ROOT / "agi"
DATA_DIR = AGI_DIR / "data"
DB_PATH = DATA_DIR / "agi.sqlite3"

# 优先看最新一版产物目录，其次回落到通用 outbox
OUTBOX_CANDIDATES = ("outbox_w13", "outbox_v1", "outbox")

_HEADER_RE = re.compile(r"^#\s*(?P<id>[A-Za-z0-9\-]+)\s*·\s*(?P<type>[^·]+?)\s*·\s*(?P<topic>.+?)\s*$")
_TEXT_RE = re.compile(r"\*\*文本\*\*：\s*(?P<text>.+?)\s*$", re.M)
_OK_RE = re.compile(r"7\s*规则通过：\s*`?\s*(?P<ok>True|False)\s*`?", re.I)
_ERR_RE = re.compile(r"-\s*错误：\s*`(?P<err>.*?)`\s*$", re.M)
_STRUCT_RE = re.compile(r"-\s*结构：\s*`(?P<json>\{.*\})`\s*$", re.M)


class AgiService:
    """草稿读取 + 生成触发。线程安全由 ``_lock`` 保证。"""

    def __init__(self, outbox_dirs: tuple[str, ...] = OUTBOX_CANDIDATES) -> None:
        self._lock = threading.Lock()
        self._job: dict = {"running": False}
        self._outbox_dirs = outbox_dirs

    # ---------- 读取 ----------

    def outbox_path(self) -> Path | None:
        for name in self._outbox_dirs:
            path = DATA_DIR / name
            if path.is_dir() and any(path.glob("*.md")):
                return path
        return None

    def _parse(self, path: Path) -> dict:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return {}
        item: dict = {"id": path.stem, "file": path.name, "mtime": path.stat().st_mtime}
        m = _HEADER_RE.search(raw.splitlines()[0] if raw else "")
        if m:
            item.update(id=m.group("id"), type=m.group("type").strip(), topic=m.group("topic").strip())
        t = _TEXT_RE.search(raw)
        if t:
            item["text"] = t.group("text").strip()
        o = _OK_RE.search(raw)
        if o:
            item["qualified"] = o.group("ok").lower() == "true"
        e = _ERR_RE.search(raw)
        if e:
            item["errors"] = [x.strip() for x in e.group("err").split(",") if x.strip()]
        s = _STRUCT_RE.search(raw)
        if s:
            try:
                item["structure"] = json.loads(s.group("json"))
            except (ValueError, TypeError):
                pass
        item.setdefault("text", "")
        item.setdefault("qualified", False)
        return item

    def drafts(self, only_qualified: bool = False) -> dict:
        """返回当前 outbox 里全部草稿（按文件名排序）。"""
        folder = self.outbox_path()
        if folder is None:
            return {"ok": True, "folder": "", "items": [], "note": "还没有产物，先点「生成 10 条」"}
        items = [self._parse(p) for p in sorted(folder.glob("*.md"))]
        items = [i for i in items if i.get("text")]
        total = len(items)
        if only_qualified:
            items = [i for i in items if i.get("qualified")]
        return {
            "ok": True,
            "folder": str(folder),
            "total": total,
            "qualified": sum(1 for i in items if i.get("qualified")),
            "items": items,
        }

    # ---------- 状态 ----------

    def status(self) -> dict:
        info: dict = {
            "ok": True,
            "db": str(DB_PATH),
            "db_exists": DB_PATH.exists(),
            "job": dict(self._job),
            "outbox": str(self.outbox_path() or ""),
        }
        if not DB_PATH.exists():
            return info
        try:
            import sqlite3
            con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            try:
                info["raw_rows"] = con.execute("select count(*) from raw_item").fetchone()[0]
                rows = con.execute(
                    "select status, count(*) from mem_episodic group by status"
                ).fetchall()
                info["mem"] = {str(k): int(v) for k, v in rows}
            finally:
                con.close()
        except Exception as exc:  # noqa: BLE001 - 状态查询失败不影响页面
            info["db_error"] = f"{type(exc).__name__}: {exc}"
        return info

    # ---------- 生成 ----------

    def generate(self, limit: int = 10) -> dict:
        with self._lock:
            if self._job.get("running"):
                return {"ok": False, "error": "已有任务在跑", "job": dict(self._job)}
            self._job = {
                "running": True,
                "started": time.time(),
                "finished": None,
                "ok": None,
                "tail": "",
            }
        threading.Thread(target=self._run, args=(limit,), daemon=True).start()
        return {"ok": True, "job": dict(self._job)}

    def _run(self, limit: int) -> None:
        cmd = [
            sys.executable, "-m", "agi.p1.cli", "pulse",
            "--once", "--dry-run", "--allow-cloud", "--limit", str(limit),
        ]
        tail = ""
        ok = False
        try:
            proc = subprocess.run(
                cmd, cwd=str(ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=1800,
            )
            tail = ((proc.stdout or "") + (proc.stderr or ""))[-4000:]
            ok = proc.returncode == 0
        except subprocess.TimeoutExpired:
            tail = "生成超时（>30 分钟）"
        except Exception as exc:  # noqa: BLE001
            tail = f"{type(exc).__name__}: {exc}"
        with self._lock:
            self._job = {
                "running": False,
                "started": self._job.get("started"),
                "finished": time.time(),
                "ok": ok,
                "tail": tail,
            }


_service: AgiService | None = None


def get_service() -> AgiService:
    global _service
    if _service is None:
        _service = AgiService()
    return _service
