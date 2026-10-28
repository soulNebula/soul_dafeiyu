# -*- coding: utf-8 -*-
"""Batch 4 / Agent A：本地表情包索引、使用计数、统计与列表。

全部使用临时资产目录 + 临时 SQLite；小图片由 Pillow 现场生成，
不联网、不读取任何旧客户数据。
"""
from __future__ import annotations

import json

import pytest
from PIL import Image

from agi.selflearn import db, emoji


@pytest.fixture
def temp_db(tmp_path):
    return tmp_path / "selflearn.sqlite3"


@pytest.fixture
def assets(tmp_path):
    """四种支持格式各一张，再混入两个不支持的文件。"""
    root = tmp_path / "assets"
    root.mkdir()
    Image.new("RGB", (4, 4), (255, 80, 80)).save(root / "cat.png")
    Image.new("RGB", (4, 4), (80, 80, 255)).save(root / "dog.jpg")
    Image.new("RGB", (4, 4), (80, 255, 80)).save(root / "wave.gif")
    Image.new("RGB", (4, 4), (255, 255, 80)).save(root / "frog.webp")
    (root / "notes.txt").write_text("不是图片", encoding="utf-8")
    (root / "ignore.bmp").write_bytes(b"BM")
    return root


def _rows(path, table):
    con = db.connect(path)
    try:
        return [dict(row) for row in con.execute(f"SELECT * FROM {table}")]
    finally:
        con.close()


def test_index_assets_scans_local_images(temp_db, assets):
    result = emoji.index_assets(temp_db, assets)

    assert result["ok"] is True, result
    assert result["indexed"] == 4
    assert result["added"] == 4
    assert result["updated"] == 0
    assert result["count"] == 4
    assert result["total"] == 4
    assert result["scanned"] == 4
    assert result["skipped"] == 2
    assert result["remaining"] == 0
    assert {item["name"] for item in result["items"]} == {
        "cat", "dog", "wave", "frog"}

    rows = _rows(temp_db, "emoji_archive")
    assert len(rows) == 4
    for row in rows:
        assert row["id"].startswith("EMO-")
        assert row["name"] in {"cat", "dog", "wave", "frog"}
        assert row["tags_json"] == "[]"
        assert row["usage_count"] == 0
        assert json.loads(row["tags_json"]) == []
        assert row["created_at"] > 0

    paths = {row["path"] for row in rows}
    assert paths == {
        str((assets / "cat.png").resolve()),
        str((assets / "dog.jpg").resolve()),
        str((assets / "wave.gif").resolve()),
        str((assets / "frog.webp").resolve()),
    }
    # 不支持的文件没有被写进档案
    assert not any(name.endswith((".txt", ".bmp")) for name in
                   {row["name"] for row in rows})


def test_index_assets_is_idempotent(temp_db, assets):
    first = emoji.index_assets(temp_db, assets)
    assert first["ok"] is True and first["indexed"] == 4
    before = {row["name"]: row for row in _rows(temp_db, "emoji_archive")}

    second = emoji.index_assets(temp_db, assets)
    assert second["ok"] is True, second
    assert second["indexed"] == 0
    assert second["added"] == 0
    assert second["updated"] == 4
    assert second["total"] == 4

    after = {row["name"]: row for row in _rows(temp_db, "emoji_archive")}
    assert set(after) == set(before)
    assert len(after) == 4
    for name, row in after.items():
        # 同一路径复用同一 id/created_at，不会重复插入
        assert row["id"] == before[name]["id"]
        assert row["created_at"] == before[name]["created_at"]
        assert row["usage_count"] == 0


def test_index_assets_limit_and_incremental_index(temp_db, assets):
    limited = emoji.index_assets(temp_db, assets, limit=2)
    assert limited["ok"] is True, limited
    assert limited["indexed"] == 2
    assert limited["matched"] == 2
    assert limited["remaining"] == 2
    assert limited["total"] == 2
    assert len(limited["items"]) == 2

    # 同样 limit 再跑一次仍然幂等，不会把“剩余候选”偷偷插入
    again = emoji.index_assets(temp_db, assets, limit=2)
    assert again["indexed"] == 0
    assert again["updated"] == 2
    assert again["total"] == 2

    # 提高 limit 后补齐剩余资产
    rest = emoji.index_assets(temp_db, assets, limit=20)
    assert rest["ok"] is True
    assert rest["indexed"] == 2
    assert rest["updated"] == 2
    assert rest["total"] == 4

    zero = emoji.index_assets(temp_db, assets, limit=0)
    assert zero["ok"] is True
    assert zero["indexed"] == 0
    assert zero["matched"] == 0
    assert zero["total"] == 4


def test_index_assets_missing_directory_fails_closed(temp_db, tmp_path):
    result = emoji.index_assets(temp_db, tmp_path / "not-there")
    assert result["ok"] is False
    assert result["code"] == "asset_dir_not_found"
    # 不创建档案行
    assert _rows(temp_db, "emoji_archive") == []


def test_record_use_writes_usage_and_count(temp_db, assets):
    emoji.index_assets(temp_db, assets)

    first = emoji.record_use(temp_db, "cat", message_id="m-1", context="打招呼")
    assert first["ok"] is True, first
    assert first["emoji_id"].startswith("EMO-")
    assert first["usage_id"].startswith("EU-")
    assert first["name"] == "cat"
    assert first["message_id"] == "m-1"
    assert first["context"] == "打招呼"
    assert first["usage_count"] == 1
    assert first["count"] == 1

    second = emoji.record_use(temp_db, "cat", message_id="m-2", context="接梗")
    assert second["ok"] is True
    assert second["usage_count"] == 2
    assert second["usage_id"] != first["usage_id"]

    dog = emoji.record_use(temp_db, "dog")
    assert dog["ok"] is True
    assert dog["usage_count"] == 1
    assert dog["message_id"] == ""
    assert dog["context"] == ""

    usages = _rows(temp_db, "emoji_usage")
    assert len(usages) == 3
    cat_usages = [row for row in usages if row["emoji_id"] == first["emoji_id"]]
    assert len(cat_usages) == 2
    assert {row["message_id"] for row in cat_usages} == {"m-1", "m-2"}
    assert {row["context"] for row in cat_usages} == {"打招呼", "接梗"}

    by_name = {row["name"]: row for row in _rows(temp_db, "emoji_archive")}
    assert by_name["cat"]["usage_count"] == 2
    assert by_name["dog"]["usage_count"] == 1
    assert by_name["wave"]["usage_count"] == 0


def test_record_use_unknown_or_empty_name_is_rejected(temp_db, assets):
    emoji.index_assets(temp_db, assets)

    missing = emoji.record_use(temp_db, "不存在")
    assert missing["ok"] is False
    assert missing["code"] == "not_found"

    empty = emoji.record_use(temp_db, "   ")
    assert empty["ok"] is False
    assert empty["code"] == "invalid_name"
    assert _rows(temp_db, "emoji_usage") == []


def test_stats_counts_archive_and_usage(temp_db, assets):
    emoji.index_assets(temp_db, assets)
    emoji.record_use(temp_db, "cat", context="第一次")
    emoji.record_use(temp_db, "cat", context="第二次")
    emoji.record_use(temp_db, "dog")

    result = emoji.stats(temp_db)

    assert result["ok"] is True
    assert result["total"] == 4
    assert result["total_emojis"] == 4
    assert result["count"] == 4
    assert result["uses"] == 3
    assert result["usage"] == 3
    assert result["usage_count"] == 3
    assert result["used"] == 2
    assert result["unused"] == 2
    assert result["top"][0]["name"] == "cat"
    assert result["top"][0]["usage_count"] == 2
    assert result["top"][0]["tags"] == []
    assert len(result["top"]) == 2

    empty = emoji.stats(temp_db.with_name("empty.sqlite3"))
    assert empty["ok"] is True, empty
    assert empty["total"] == 0
    assert empty["usage_count"] == 0
    assert empty["top"] == []


def test_default_db_path_uses_runtime_db(temp_db, assets, monkeypatch):
    """不传 path 时使用运行时的 DB_PATH（测试里替换成临时库）。"""
    monkeypatch.setattr(emoji, "DB_PATH", temp_db)

    result = emoji.index_assets(asset_dir=assets)
    assert result["ok"] is True and result["indexed"] == 4
    used = emoji.record_use(emoji_name="cat")
    assert used["ok"] is True and used["usage_count"] == 1
    assert emoji.stats()["usage_count"] == 1
    assert len(emoji.list_emojis()) == 4
    assert len(_rows(temp_db, "emoji_usage")) == 1


def test_list_emojis_orders_and_limits(temp_db, assets):
    emoji.index_assets(temp_db, assets)
    emoji.record_use(temp_db, "cat")
    emoji.record_use(temp_db, "cat")
    emoji.record_use(temp_db, "dog")

    # 给 cat 加 tags，确认列表会把 tags_json 解析成 tags
    con = db.connect(temp_db)
    try:
        con.execute(
            "UPDATE emoji_archive SET tags_json = ? WHERE name = 'cat'",
            (json.dumps(["猫", "开心"], ensure_ascii=False),))
        con.commit()
    finally:
        con.close()

    items = emoji.list_emojis(temp_db, limit=2)
    assert len(items) == 2
    assert [item["name"] for item in items] == ["cat", "dog"]
    assert items[0]["usage_count"] == 2
    assert items[0]["tags"] == ["猫", "开心"]
    assert items[0]["tags_json"] == json.dumps(["猫", "开心"], ensure_ascii=False)
    assert items[0]["extension"] == ".png"
    assert items[0]["filename"] == "cat.png"
    assert items[1]["usage_count"] == 1

    assert emoji.list_emojis(temp_db, limit=0) == []
    assert len(emoji.list_emojis(temp_db)) == 4
