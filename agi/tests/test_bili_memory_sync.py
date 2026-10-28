# -*- coding: utf-8 -*-
"""B站日报 → 记忆库（bili_impressions）同步测试。"""
import sqlite3
from pathlib import Path

from tools import bili_memory_sync as sync_mod

NL = chr(10)


def _digest(home: Path, lines: str) -> None:
    (home / "digest").mkdir(parents=True, exist_ok=True)
    (home / "digest" / "2026-10.md").write_text(lines, encoding="utf-8")


def _line(date: str, bvid: str, title: str, owner: str, reason: str) -> str:
    return (f"- {date} · {bvid} · {title} · {owner} · {reason} · "
            f"https://www.bilibili.com/video/{bvid}")


def _sample(*rows) -> str:
    return NL.join(rows)


def test_parse_digest_and_dedupe(tmp_path):
    _digest(tmp_path, _sample(
        _line("2026-10-27", "BV1aaa", "视频A", "UP甲", "挑了：讲得很清楚"),
        _line("2026-10-29", "BV1aaa", "视频A（重登）", "UP甲", "挑了：更完整的一版"),
        _line("2026-10-29", "BV1bbb", "视频B", "UP乙", "跳过（太长）"),
    ))
    entries = sync_mod.parse_digest(tmp_path)
    assert len(entries) == 2
    a = next(e for e in entries if e["bvid"] == "BV1aaa")
    assert a["title"] == "视频A（重登）" and a["picked"] is True
    b = next(e for e in entries if e["bvid"] == "BV1bbb")
    assert b["picked"] is False


def test_sync_writes_impressions_and_is_idempotent(tmp_path, monkeypatch):
    home = tmp_path / "duty"
    _digest(home, _sample(
        _line("2026-10-29", "BV1ccc", "视频C", "UP丙", "挑了：细节很足"),
        _line("2026-10-29", "BV1ddd", "视频D", "UP丁", "跳过（重复）"),
    ))
    (home / "reports").mkdir()
    report_text = _sample("# 报告", "", "## 结论", "这条讲了折叠屏折一万次的实测过程。", "")
    (home / "reports" / "BV1ccc-2026-10-29.md").write_text(report_text, encoding="utf-8")
    contact_dir = tmp_path / "contacts"
    contact_dir.mkdir()
    db_path = contact_dir / "x.memory.sqlite3"
    db_path.touch()
    monkeypatch.setattr(sync_mod, "CONTACT_DIR", contact_dir)
    result = sync_mod.sync(home)
    assert result["ok"] and result["inserted"] == 2
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT bvid,title,summary,my_take,value FROM bili_impressions ORDER BY bvid"
    ).fetchall()
    assert len(rows) == 2
    assert rows[0][0] == "BV1ccc" and "折叠屏" in (rows[0][3] or "")
    assert rows[1][4] < rows[0][4]
    con.close()
    again = sync_mod.sync(home)
    assert again["ok"] and again["inserted"] == 2
    con = sqlite3.connect(db_path)
    assert con.execute("SELECT count(*) FROM bili_impressions").fetchone()[0] == 2
    con.close()


def test_sync_dry_run_does_not_write(tmp_path, monkeypatch):
    home = tmp_path / "duty"
    _digest(home, _line("2026-10-29", "BV1eee", "视频E", "UP戊", "挑了：看看"))
    contact_dir = tmp_path / "contacts"
    contact_dir.mkdir()
    db_path = contact_dir / "y.memory.sqlite3"
    monkeypatch.setattr(sync_mod, "CONTACT_DIR", contact_dir)
    result = sync_mod.sync(home, dry=True)
    assert result["ok"] and result["parsed"] == 1
    assert not db_path.exists()


def test_sync_empty_digest(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_mod, "CONTACT_DIR", tmp_path / "none")
    assert sync_mod.sync(tmp_path) == {"ok": False, "error": "digest_empty",
                                       "home": str(tmp_path)}
