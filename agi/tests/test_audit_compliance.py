# -*- coding: utf-8 -*-
"""P0-1：合规审计的发送路径与文档一致性检查（tmp 文件，不联网）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import audit_compliance as audit  # noqa: E402


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_send_paths_split_allowed_and_unguarded(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    allowed = _write(tmp_path / "agi" / "wechat_gateway" / "server.py",
                     "def _send(self, contact, text):\n    pass\n")
    bad = _write(tmp_path / "agi" / "tools" / "bad_send.py",
                 "s = WxAutoSender()\ns.send_text('x')\n")
    allowed_map = {"agi/wechat_gateway/server.py": "Gateway._send"}
    findings = audit.scan_send_paths([allowed, bad], allowed_files=allowed_map)
    levels = {(f.path, f.level) for f in findings}
    assert ("agi/wechat_gateway/server.py", "INFO") in levels
    assert any(f.path.endswith("bad_send.py") and f.level == "FAIL" for f in findings)
    assert any("未受控发送路径" in f.message for f in findings)


def test_gateway_call_is_allowed_even_from_worker(tmp_path):
    worker = _write(tmp_path / "agi" / "wechat_gateway" / "proactive_worker.py",
                    "status = gateway._send(contact, text, proactive=True)\n")
    findings = audit.scan_send_paths([worker], allowed_files={})
    assert findings and findings[0].level == "INFO"
    assert "Gateway._send" in findings[0].message


def test_doc_claims_conflict_with_send_paths(tmp_path):
    doc = _write(tmp_path / "docs" / "风控.md",
                 "本工具不发送任何消息。\n")
    send_findings = [audit.Finding("INFO", "x.py", 1, "受控", "")]
    findings = audit.scan_doc_conflicts(send_findings, docs=[doc])
    assert len(findings) == 1 and findings[0].level == "FAIL"
    assert "P0" in findings[0].message


def test_no_send_paths_no_doc_conflict(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "ROOT", tmp_path)
    plain = _write(tmp_path / "agi" / "plain.py", "x = 1\n")
    findings = audit.scan_send_paths([plain], allowed_files={})
    assert findings == []
    doc = _write(tmp_path / "docs" / "风控.md", "没有发送能力的代码。\n")
    assert audit.scan_doc_conflicts([], docs=[doc]) == []
