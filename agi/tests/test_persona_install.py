# -*- coding: utf-8 -*-
"""P21：小鱼（普通朋友）人格安装、备份与回滚。"""
from __future__ import annotations

import json

from agi.selflearn import db, prompt_store
from tools.persona import install_xiaoyu


def _seed_profile(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "profile.json").write_text(json.dumps({
        "name": "小鱼", "relation": "朋友", "user_title": "",
        "age_feel": "20+", "self_aware_ai": True,
        "taboos": ["爸/父女", "暧昧"], "style": {"sentences": "1-2句"},
    }, ensure_ascii=False), encoding="utf-8")
    (seed / "fewshot.json").write_text(json.dumps({
        "version": 1, "examples": [f"样例{i}" for i in range(20)],
    }, ensure_ascii=False), encoding="utf-8")
    (seed / "values.md").write_text("# values", encoding="utf-8")
    return seed


def _setup(tmp_path, monkeypatch):
    seed = _seed_profile(tmp_path)
    prompts = tmp_path / "prompts.json"
    monkeypatch.setenv("PROMPTS_PATH", str(prompts))
    prompt_store.set_many({"chat.system": "旧 SYSTEM",
                           "proactive.persona_constraints": "旧 persona",
                           "proactive.voice_fewshot": "旧 fewshot"})
    return seed, prompts, tmp_path / "installed.json", tmp_path / "db.sqlite3"


def test_install_writes_prompts_backup_and_relation(tmp_path, monkeypatch):
    seed, prompts, installed, db_path = _setup(tmp_path, monkeypatch)
    con = db.connect(db_path)
    con.execute("INSERT INTO user_profile(id,dimension,value,evidence_json,confidence,"
                "status,updated_at) VALUES('OTHER','job','工程师','[]',0.8,'active',1)")
    con.execute("INSERT INTO user_profile(id,dimension,value,evidence_json,confidence,"
                "status,updated_at) VALUES('UP-XIAOMAN-FATHER','relation','用户是她的父亲','[]',0.9,'active',1)")
    con.commit(); con.close()
    result = install_xiaoyu.install(seed_dir=seed, installed_path=installed,
                                    db_path=db_path)
    assert result["ok"] is True
    values = prompt_store.overrides()
    assert "小鱼" in values["chat.system"] and "跟用户是朋友关系" in values["chat.system"]
    assert "爸" not in values["chat.system"] and "父女" not in values["chat.system"]
    assert "网络梗" in values["chat.system"] and "赢了" in values["chat.system"]
    assert "生活化打趣" in values["chat.system"] and "输赢评判" in values["chat.system"]
    assert "普通朋友" in values["proactive.persona_constraints"]
    assert "样例0" in values["proactive.voice_fewshot"]
    backup = json.loads(installed.read_text(encoding="utf-8"))
    assert backup["previous"]["chat.system"] == "旧 SYSTEM"
    assert backup["installed"] is True

    con = db.connect(db_path)
    assert con.execute("SELECT COUNT(*) FROM user_profile WHERE id="
                       "'UP-XIAOYU-FRIEND'").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM claims WHERE id="
                       "'CL-XIAOYU-FRIEND'").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM user_profile WHERE id="
                       "'UP-XIAOMAN-FATHER'").fetchone()[0] == 1  # 安装不覆盖/不清其它（清理只在卸载）
    assert con.execute("SELECT value FROM user_profile WHERE id='OTHER'").fetchone()[0] == "工程师"
    con.close()


def test_install_idempotent_and_uninstall_restores(tmp_path, monkeypatch):
    seed, prompts, installed, db_path = _setup(tmp_path, monkeypatch)
    install_xiaoyu.install(seed_dir=seed, installed_path=installed, db_path=db_path)
    first_backup = installed.read_text(encoding="utf-8")
    install_xiaoyu.install(seed_dir=seed, installed_path=installed, db_path=db_path)
    assert installed.read_text(encoding="utf-8") == first_backup
    con = db.connect(db_path)
    assert con.execute("SELECT COUNT(*) FROM user_profile WHERE id="
                       "'UP-XIAOYU-FRIEND'").fetchone()[0] == 1
    con.close()
    out = install_xiaoyu.uninstall(installed_path=installed, db_path=db_path)
    assert out["ok"] is True
    values = prompt_store.overrides()
    assert values["chat.system"] == "旧 SYSTEM"
    assert values["proactive.persona_constraints"] == "旧 persona"
    assert values["proactive.voice_fewshot"] == "旧 fewshot"
    con = db.connect(db_path)
    assert con.execute("SELECT COUNT(*) FROM user_profile WHERE id="
                       "'UP-XIAOYU-FRIEND'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM user_profile WHERE id="
                       "'UP-XIAOMAN-FATHER'").fetchone()[0] == 0  # 旧父女残留清掉
    con.close()


def test_uninstall_cleans_legacy_father_ids(tmp_path, monkeypatch):
    seed, prompts, installed, db_path = _setup(tmp_path, monkeypatch)
    con = db.connect(db_path)
    for ident in ("UP-XIAOMAN-FATHER", "CL-XIAOMAN-FATHER"):
        table = "user_profile" if ident.startswith("UP-") else "claims"
        if table == "user_profile":
            con.execute("INSERT INTO user_profile(id,dimension,value,evidence_json,"
                        "confidence,status,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (ident, "relation", "用户是她的父亲", "[]", 0.9, "active", 1))
        else:
            con.execute("INSERT INTO claims(id,subject,predicate,object,polarity,time_ref,"
                        "claim_type,evidence_span,confidence,source_level,status,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (ident, "用户", "是", "她的父亲", 1, "", "relation", "seed",
                         0.9, "A", "active", 1))
    con.commit(); con.close()
    install_xiaoyu.uninstall(installed_path=installed, db_path=db_path)
    con = db.connect(db_path)
    assert con.execute("SELECT COUNT(*) FROM user_profile WHERE id="
                       "'UP-XIAOMAN-FATHER'").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM claims WHERE id="
                       "'CL-XIAOMAN-FATHER'").fetchone()[0] == 0
    con.close()


def test_dry_run_does_not_write(tmp_path, monkeypatch):
    seed, prompts, installed, db_path = _setup(tmp_path, monkeypatch)
    before = prompts.read_text(encoding="utf-8")
    result = install_xiaoyu.install(seed_dir=seed, installed_path=installed,
                                    db_path=db_path, dry_run=True)
    assert result["dry_run"] is True
    assert prompts.read_text(encoding="utf-8") == before
    assert not installed.exists()


def test_seed_files_and_fewshot(tmp_path):
    seed = _seed_profile(tmp_path)
    assert len(install_xiaoyu.load_fewshot(seed)) == 20
    assert "小鱼" in install_xiaoyu.build_chat_system()
    assert "朋友" in install_xiaoyu.build_proactive_persona()
    assert "样例19" in install_xiaoyu.build_voice_fewshot(seed)
