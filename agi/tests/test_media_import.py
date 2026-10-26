# -*- coding: utf-8 -*-
"""P5-e-B 本地导入：校验/去重/脱 EXIF/索引/愿望满足（全部 tmp_path，不联网）。"""
from __future__ import annotations

import json
import os
import sqlite3
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from agi import media_acquisition as acq


def _png_bytes(size=(8, 8), color=(220, 80, 80)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _gif_bytes() -> bytes:
    buffer = BytesIO()
    frames = [Image.new("RGB", (8, 8), color) for color in ((255, 0, 0), (0, 0, 255))]
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:])
    return buffer.getvalue()


def _jpeg_with_exif() -> bytes:
    image = Image.new("RGB", (8, 8), (10, 200, 10))
    exif = Image.Exif()
    exif[0x010E] = "secret-description"
    exif[0x0131] = "test-software"
    buffer = BytesIO()
    image.save(buffer, format="JPEG", exif=exif.tobytes())
    return buffer.getvalue()


@pytest.fixture
def env(tmp_path, monkeypatch):
    media_dir = tmp_path / "media"
    desktop = tmp_path / "desktop"
    repo_media = tmp_path / "repo_media"
    media_dir.mkdir()
    desktop.mkdir()
    repo_media.mkdir()
    wishes = tmp_path / "media_wishes.jsonl"
    wishes.write_text(
        json.dumps({"wish": "想发一只布偶猫的表情", "source": "model",
                    "created_at": 1800000000.0}, ensure_ascii=False) + "\n"
        + json.dumps({"wish": "想发「晚安」贴纸"}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(acq, "IMPORT_DIR", media_dir)
    monkeypatch.setattr(acq, "MEDIA_SOURCES_PATH", tmp_path / "media_sources.json")
    monkeypatch.setattr(acq, "WISH_STATUS_FILE", tmp_path / "media_wishes_status.json")
    monkeypatch.setattr(acq, "IMPORT_INDEX_DB", tmp_path / "index.sqlite3")
    monkeypatch.setattr(acq, "IMPORT_PATH_ROOTS", (media_dir, desktop, repo_media))
    monkeypatch.setattr(acq, "wishes_path", lambda: wishes)
    return {"media": media_dir, "desktop": desktop, "repo_media": repo_media,
            "wishes": wishes, "tmp": tmp_path}


def _archive_rows(index_db: Path) -> list[dict]:
    candidates = [index_db, index_db.with_name(index_db.stem + ".memory.sqlite3")]
    for candidate in candidates:
        if not candidate.is_file():
            continue
        con = sqlite3.connect(candidate)
        try:
            con.row_factory = sqlite3.Row
            return [dict(row) for row in con.execute("SELECT * FROM emoji_archive").fetchall()]
        except sqlite3.OperationalError:
            continue
        finally:
            con.close()
    return []


def test_import_png_lands_and_indexes(env):
    result = acq.import_local(_png_bytes(), "cat.png", name="猫猫", tags=["可爱"])
    assert result["ok"] is True and result["duplicate"] is False
    assert result["status"] == "imported" and result["indexed"] is True
    target = Path(result["path"])
    assert target.is_file() and target.parent == env["media"].resolve()
    assert target.name == f"{result['hash']}.png"
    assert target.stat().st_size <= 2 * 1024 * 1024
    sources = json.loads((env["tmp"] / "media_sources.json").read_text(encoding="utf-8"))
    record = sources[result["hash"]]
    assert record["name"] == "猫猫" and record["source"] == "local_import"
    assert "可爱" in record["tags"] and record["indexed"] is True
    rows = _archive_rows(env["tmp"] / "index.sqlite3")
    assert any(row["path"].endswith(target.name) for row in rows)


def test_import_gif_and_exif_stripped(env):
    gif = acq.import_local(_gif_bytes(), "happy.gif", name="开心")
    assert gif["ok"] is True and gif["ext"] == ".gif"
    assert Path(gif["path"]).is_file()
    assert Image.open(gif["path"]).format == "GIF"

    jpeg = acq.import_local(_jpeg_with_exif(), "photo.jpg", name="照片")
    assert jpeg["ok"] is True and jpeg["ext"] == ".jpg"
    saved = Image.open(jpeg["path"])
    assert not dict(saved.getexif())            # EXIF 被剥离
    assert saved.format == "JPEG"


def test_import_duplicate_does_not_write_twice(env):
    data = _png_bytes(color=(1, 2, 3))
    first = acq.import_local(data, "a.png", name="A")
    second = acq.import_local(data, "b.png", name="B")
    assert first["ok"] and first["duplicate"] is False
    assert second["ok"] and second["duplicate"] is True
    assert second["path"] == first["path"]
    files = list(env["media"].glob("*.png"))
    assert len(files) == 1
    sources = json.loads((env["tmp"] / "media_sources.json").read_text(encoding="utf-8"))
    assert len(sources) == 1


def test_import_rejections(env):
    assert acq.import_local(b"<svg></svg>", "evil.svg")["reason"] == "suffix_not_allowed"
    assert acq.import_local(b"<html></html>", "page.png")["reason"] == "not_an_image"
    assert acq.import_local(b"", "empty.png")["reason"] == "empty_file"
    big = os.urandom(2 * 1024 * 1024 + 10)
    assert acq.import_local(big, "big.png")["reason"] == "too_large"
    assert acq.import_local(_png_bytes(), "only.bmp")["reason"] == "suffix_not_allowed"
    assert list(env["media"].glob("*")) == []


def test_import_local_file_whitelist(env):
    source = env["desktop"] / "cat.png"
    source.write_bytes(_png_bytes(color=(9, 9, 9)))
    result = acq.import_local_file(source, name="桌面猫")
    assert result["ok"] is True and result["status"] == "imported"

    outside = env["tmp"] / "outside.png"
    outside.write_bytes(_png_bytes(color=(4, 4, 4)))
    rejected = acq.import_local_file(outside)
    assert rejected["ok"] is False and rejected["reason"] == "path_not_allowed"
    assert "Desktop" in rejected["hint"]

    repo_file = env["repo_media"] / "sticker.png"
    repo_file.write_bytes(_png_bytes(color=(7, 7, 7)))
    assert acq.import_local_file(repo_file)["ok"] is True


def test_wishes_list_and_fulfill(env):
    wishes = acq.list_wishes()
    assert len(wishes) == 2 and all(item["status"] == "pending" for item in wishes)
    original = env["wishes"].read_text(encoding="utf-8")
    target = wishes[0]
    result = acq.fulfill_wish(target["wish_id"], data=_png_bytes(color=(11, 22, 33)),
                              name="满足图", tags=["布偶猫"])
    assert result["ok"] is True and result["wish"]["status"] == "fulfilled"
    after = acq.list_wishes()
    assert after[0]["status"] == "fulfilled"
    assert after[0]["fulfilled_asset"] == result["path"]
    assert after[1]["status"] == "pending"
    assert env["wishes"].read_text(encoding="utf-8") == original   # 原记录不动
    statuses = json.loads((env["tmp"] / "media_wishes_status.json").read_text(encoding="utf-8"))
    assert statuses[target["wish_id"]]["status"] == "fulfilled"

    again = acq.fulfill_wish(target["wish_id"], data=_png_bytes(color=(44, 55, 66)))
    assert again["ok"] is False and again["reason"] == "already_fulfilled"
    missing = acq.fulfill_wish("WISH-nope", data=_png_bytes())
    assert missing["reason"] == "wish_not_found"

    no_image = acq.fulfill_wish(after[1]["wish_id"])
    assert no_image["ok"] is False and no_image["reason"] == "missing_image"

    path_source = env["desktop"] / "wish.png"
    path_source.write_bytes(_png_bytes(color=(21, 22, 23)))
    path_result = acq.fulfill_wish(after[1]["wish_id"], path=path_source)
    assert path_result["ok"] is True and path_result["wish"]["status"] == "fulfilled"
