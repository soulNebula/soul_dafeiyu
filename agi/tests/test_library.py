# -*- coding: utf-8 -*-
"""P17 素材库：media_assets CRUD / 迁移 / 软删 / hard delete / media_choice 适配。"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "console_media_store", ROOT / "tools" / "console" / "media_store.py")
store = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(store)


def _png(color=(20, 40, 60)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
    return buffer.getvalue()


def _gif() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buffer, format="GIF")
    return buffer.getvalue()


@pytest.fixture
def env(tmp_path, monkeypatch):
    from agi import media_acquisition
    from agi.selflearn import media_choice

    media_dir = tmp_path / "selflearn_media"
    media_dir.mkdir()
    (media_dir / "cat.png").write_bytes(_png((10, 20, 30)))
    (media_dir / "dog.gif").write_bytes(_gif())
    sources = tmp_path / "media_sources.json"
    sources.write_text(json.dumps({
        "abc": {"name": "猫猫", "tags": ["可爱"], "path": str(media_dir / "cat.png")},
    }, ensure_ascii=False), encoding="utf-8")
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    db_path = tmp_path / "library.sqlite3"
    monkeypatch.setenv("MEDIA_LIBRARY_DB", str(db_path))
    monkeypatch.setenv("MEDIA_LIBRARY_DIR", str(media_dir))
    monkeypatch.setenv("MEDIA_TRASH_DIR", str(tmp_path / "trash"))
    monkeypatch.setenv("MEDIA_SOURCES_FILE", str(sources))
    monkeypatch.setattr(media_acquisition, "IMPORT_DIR", tmp_path / "imported")
    monkeypatch.setattr(media_acquisition, "IMPORT_INDEX_DB", tmp_path / "index.sqlite3")
    monkeypatch.setattr(media_acquisition, "MEDIA_SOURCES_PATH", sources)
    monkeypatch.setattr(media_acquisition, "IMPORT_PATH_ROOTS", (desktop, media_dir))
    monkeypatch.setattr(media_choice, "ASSET_DIRS", (media_dir,))
    return {"db": db_path, "media": media_dir, "sources": sources,
            "desktop": desktop, "tmp": tmp_path, "media_choice": media_choice}


def _ids(items) -> set:
    return {item["id"] for item in items}


def test_import_idempotent_and_types(env):
    first = store.ensure_imported()
    assert first["ok"] is True and first["imported"] == 2 and first["total"] == 2
    second = store.ensure_imported()
    assert second["imported"] == 0 and second["cached"] is True and second["total"] == 2
    data = store.list_assets()
    assert data["total"] == 2
    names = {item["name"]: item for item in data["items"]}
    assert "猫猫" in names and names["猫猫"]["builtin"] is True
    assert names["猫猫"]["type"] == "sticker" and names["猫猫"]["tags"] == ["可爱"]
    gif = next(item for item in data["items"] if item["type"] == "gif")
    assert gif["name"] == "dog"
    assert gif["thumb"].startswith("/api/media/file?path=")


def test_list_filters_and_pagination(env):
    store.ensure_imported()
    assert store.list_assets(type_="gif")["total"] == 1
    assert store.list_assets(type_="image")["total"] == 0
    assert store.list_assets(q="猫")["total"] == 1
    assert store.list_assets(q="可爱")["total"] == 1
    page1 = store.list_assets(limit=1, offset=0)
    page2 = store.list_assets(limit=1, offset=1)
    assert page1["total"] == 2 and len(page1["items"]) == 1
    assert page1["items"][0]["id"] != page2["items"][0]["id"]
    assert store.list_assets(type_="bogus")["ok"] is False


def test_create_duplicate_validation_and_path(env):
    created = store.create_asset(data=_png((1, 2, 3)), type_="image",
                                 name="新图", caption="测试含义", tags=["a", "b"])
    assert created["ok"] is True and created["duplicate"] is False
    asset = created["asset"]
    assert asset["type"] == "image" and asset["name"] == "新图"
    assert asset["caption"] == "测试含义" and asset["tags"] == ["a", "b"]
    assert Path(asset["path"]).is_file()
    duplicate = store.create_asset(data=_png((1, 2, 3)), type_="sticker", name="重复")
    assert duplicate["ok"] is True and duplicate["duplicate"] is True
    assert duplicate["asset"]["id"] == asset["id"]

    assert store.create_asset(data=_png(), type_="video", name="x")["ok"] is False
    assert store.create_asset(data=_png(), type_="image", name="")["ok"] is False
    assert store.create_asset(data=_png(), type_="image", name="x" * 31)["ok"] is False
    assert store.create_asset(data=_png(), type_="image", name="x",
                              tags=[f"t{i}" for i in range(11)])["ok"] is False
    big = _png() + b"\x00" * (2 * 1024 * 1024)
    assert store.create_asset(data=big, type_="image", name="big")["ok"] is False

    source = env["desktop"] / "desk.png"
    source.write_bytes(_png((9, 8, 7)))
    path_created = store.create_asset(file_path=str(source), type_="sticker", name="桌面图")
    assert path_created["ok"] is True and path_created["duplicate"] is False
    outside = env["tmp"] / "outside.png"
    outside.write_bytes(_png((5, 5, 5)))
    assert store.create_asset(file_path=str(outside), type_="sticker", name="外")["ok"] is False


def test_update_validation_and_fields(env):
    store.ensure_imported()
    target = next(item for item in store.list_assets()["items"] if item["type"] == "gif")
    updated = store.update_asset(target["id"], {
        "type": "image", "name": "改过的名字", "caption": "新含义",
        "tags": ["情绪", "日常"], "enabled": False,
    })
    assert updated["ok"] is True
    assert updated["asset"]["type"] == "image"
    assert updated["asset"]["name"] == "改过的名字"
    assert updated["asset"]["caption"] == "新含义"
    assert updated["asset"]["enabled"] is False
    assert store.update_asset(target["id"], {"type": "video"})["ok"] is False
    assert store.update_asset(target["id"], {"name": "x" * 31})["ok"] is False
    assert store.update_asset(target["id"], {"caption": "x" * 201})["ok"] is False
    assert store.update_asset(target["id"], {"unknown": 1})["ok"] is False
    assert store.update_asset("missing", {"name": "x"})["ok"] is False


def test_soft_delete_restore_and_hard_delete(env):
    store.ensure_imported()
    target = next(item for item in store.list_assets()["items"] if item["type"] == "gif")
    assert store.delete_asset(target["id"], confirm="错误")["ok"] is False
    soft = store.delete_asset(target["id"], confirm="删除素材")
    assert soft["ok"] is True and soft["mode"] == "soft"
    assert target["id"] not in _ids(store.list_assets(enabled="1")["items"])
    assert target["id"] in _ids(store.list_assets(enabled="0")["items"])
    assert store.restore_asset(target["id"])["ok"] is True
    assert target["id"] in _ids(store.list_assets(enabled="1")["items"])

    hard = store.delete_asset(target["id"], hard=True, confirm="删除素材")
    assert hard["ok"] is True and hard["mode"] == "hard"
    assert Path(hard["moved_to"]).is_file()
    assert not Path(target["path"]).exists()
    assert target["id"] not in _ids(store.list_assets()["items"])
    assert store.get_asset(target["id"])["ok"] is False

    stats = store.stats()
    assert stats["total"] == 1 and stats["enabled"] == 1


def test_stats_and_media_choice_adapter(env):
    store.ensure_imported()
    stats = store.stats()
    assert stats["total"] == 2 and stats["builtin"] == 2
    assert stats["by_type"]["sticker"] == 1 and stats["by_type"]["gif"] == 1

    media_choice = env["media_choice"]
    assets = media_choice.list_assets(env["db"])
    names = {item["name"] for item in assets}
    assert "猫猫" in names

    target = next(item for item in store.list_assets()["items"] if item["name"] == "猫猫")
    store.update_asset(target["id"], {"name": "新猫猫", "tags": ["开心"]})
    assert "新猫猫" in {item["name"] for item in media_choice.list_assets(env["db"])}
    materials = media_choice.media_materials(env["db"])
    assert any("新猫猫（开心）" in item for item in materials)

    store.update_asset(target["id"], {"enabled": False})
    assert "新猫猫" not in {item["name"] for item in media_choice.list_assets(env["db"])}
