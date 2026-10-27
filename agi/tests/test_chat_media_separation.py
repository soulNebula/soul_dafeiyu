# -*- coding: utf-8 -*-
"""P23：聊天图片与素材库分离（落盘/扫描/迁移）。"""
from __future__ import annotations

import base64
import json
import time
from pathlib import Path

from agi.selflearn import db, media
from tools import migrate_chat_media
from tools.console import media_store


PNG = (b"\x89PNG\r\n\x1a\n" + b"chat-image")
DATA_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()


def _setup_paths(tmp_path, monkeypatch):
    chat = tmp_path / "media_chat"
    library = tmp_path / "media"
    chat.mkdir(); library.mkdir()
    monkeypatch.setattr(media, "CHAT_MEDIA_DIR", chat)
    monkeypatch.setattr(media, "MEDIA_DIR", library)
    monkeypatch.setattr(media_store, "CHAT_MEDIA_DIR", chat)
    monkeypatch.setattr(media_store, "MEDIA_DIR", library)
    monkeypatch.setenv("MEDIA_LIBRARY_DIR", str(library))
    monkeypatch.setenv("MEDIA_LIBRARY_DB", str(tmp_path / "lib.sqlite3"))
    return chat, library


def test_save_image_goes_to_chat_dir(tmp_path, monkeypatch):
    chat, library = _setup_paths(tmp_path, monkeypatch)
    path = tmp_path / "db.sqlite3"
    out = media.save_image(DATA_URL, message_id="M1", path=path)
    assert Path(out["path"]).parent == chat
    assert (chat / out["path"].split("\\")[-1]).exists() or (chat / out["path"].split("/")[-1]).exists()
    con = db.connect(path)
    row = con.execute("SELECT path FROM attachments WHERE id=?", (out["id"],)).fetchone()
    assert row and row[0] == out["path"]
    con.close()
    assert not any(library.iterdir())


def test_media_store_skips_chat_paths(tmp_path, monkeypatch):
    chat, library = _setup_paths(tmp_path, monkeypatch)
    path = tmp_path / "db.sqlite3"
    out = media.save_image(DATA_URL, message_id="M1", path=path)
    # 模拟历史误落：同一文件也存在于素材库目录，并在 attachments 表登记
    mixed = library / "mixed.png"
    mixed.write_bytes(PNG)
    con = db.connect(path)
    con.execute("INSERT INTO attachments(id,message_id,kind,mime,path,bytes,sha256,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("ATT-MIX", "M1", "image", "image/png", str(mixed),
                 len(PNG), "x", time.time()))
    con.commit(); con.close()
    result = media_store.ensure_imported(path, force=True)
    assert result["ok"] is True
    assert result["imported"] == 0
    con = db.connect(path)
    assert con.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0] == 0
    con.close()
    # 素材库扫描目录不包含 media_chat
    assert all("media_chat" not in str(item) for item in media_store._scan_dirs())


def _db_with_rows(tmp_path):
    base = tmp_path / "base.sqlite3"
    lib = tmp_path / "lib.sqlite3"
    for p in (base, lib):
        db.connect(p).close()
    return base, lib


def test_migrate_dry_run_and_apply(tmp_path, monkeypatch):
    library = tmp_path / "media"
    chat = tmp_path / "media_chat"
    library.mkdir(); chat.mkdir()
    src = library / "chat.png"
    src.write_bytes(PNG)
    base, lib = _db_with_rows(tmp_path)
    con = db.connect(base)
    con.execute("INSERT INTO attachments(id,message_id,kind,mime,path,bytes,sha256,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                ("ATT-1", "M1", "image", "image/png", str(src),
                 len(PNG), "sha", time.time()))
    con.commit(); con.close()
    con = db.connect(lib)
    con.execute("INSERT INTO media_assets(id,hash,path,type,name,caption,tags_json,enabled,"
                "builtin,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("MA-1", "sha", str(src), "image", "chat", "", "[]", 1, 1,
                 time.time(), time.time()))
    con.commit(); con.close()

    dry = migrate_chat_media.migrate(db_path=base, library_db=lib,
                                     library_dir=library, chat_dir=chat,
                                     dry_run=True, backup_dir=tmp_path / "backups")
    assert dry["dry_run"] is True
    assert dry["move_files"] == 1 and dry["delete_assets"] == 1
    assert src.exists() and not (chat / "chat.png").exists()
    assert not (tmp_path / "backups").exists()

    real = migrate_chat_media.migrate(db_path=base, library_db=lib,
                                      library_dir=library, chat_dir=chat,
                                      dry_run=False, backup_dir=tmp_path / "backups")
    assert real["move_files"] == 1 and real["delete_assets"] == 1
    assert (chat / "chat.png").exists() and not src.exists()
    backup = json.loads(open(real["backup_path"], encoding="utf-8").read())
    assert backup["deleted_assets"][0]["id"] == "MA-1"
    con = db.connect(base)
    assert con.execute("SELECT path FROM attachments WHERE id='ATT-1'").fetchone()[0] == str(chat / "chat.png")
    con.close()
    con = db.connect(lib)
    assert con.execute("SELECT COUNT(*) FROM media_assets WHERE id='MA-1'").fetchone()[0] == 0
    con.close()
    # 二次运行幂等
    again = migrate_chat_media.migrate(db_path=base, library_db=lib,
                                       library_dir=library, chat_dir=chat,
                                       dry_run=False, backup_dir=tmp_path / "backups")
    assert again["move_files"] == 0 and again["delete_assets"] == 0
