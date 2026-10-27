# -*- coding: utf-8 -*-
"""Batch 3 / Agent B：图片结构化理解与 media_understanding 落库。

全部使用临时 1x1 PNG + 注入的 fake client，不联网、不读旧数据库。
"""
from __future__ import annotations

import base64
import json

import pytest

from agi.selflearn import db
from agi.selflearn import image_understanding as iu

# 合法 1x1 RGBA PNG
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
    "DwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

VALID = {
    "caption": "一只橘猫趴在键盘上",
    "tags": ["猫", "键盘", "橘色"],
    "emotion": "happy",
    "confidence": 0.92,
}


class FakeClient:
    """可注入的假图片理解 client：记录调用并返回固定 JSON。"""

    def __init__(self, payload=None, *, ok: bool = True, available: bool = True,
                 raises: BaseException | None = None):
        self.payload = payload
        self.ok = ok
        self._available = available
        self.raises = raises
        self.calls: list[dict] = []

    def available(self) -> bool:
        return self._available

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if self.raises is not None:
            raise self.raises
        if not self.ok:
            return {"ok": False, "text": "", "error": "fake client down"}
        if isinstance(self.payload, (dict, list)):
            text = json.dumps(self.payload, ensure_ascii=False)
        else:
            text = str(self.payload or "")
        return {"ok": True, "text": text}


class MinimalClient:
    """最简单的 fake：只有 chat，没有 available()。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return {"ok": True, "text": json.dumps(self.payload, ensure_ascii=False)}


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    path = tmp_path / "selflearn.sqlite3"
    monkeypatch.setattr(iu, "DB_PATH", path)
    return path


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "pixel.png"
    path.write_bytes(PNG_1X1)
    return path


def _insert_media(path, media_id, created_at, *, tags=None, caption="旧记录"):
    con = db.connect(path)
    try:
        con.execute(
            "INSERT INTO media_understanding"
            "(id,attachment_id,path,caption,tags_json,emotion,confidence,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (media_id, "", f"C:/media/{media_id}.png", caption,
             json.dumps(tags or [], ensure_ascii=False), "neutral", 0.5, "active",
             created_at),
        )
        con.commit()
    finally:
        con.close()


def test_describe_image_writes_structured_media(temp_db, image):
    fake = FakeClient(VALID)
    result = iu.describe_image(str(image), fake, attachment_id="ATT-0001")

    assert result["ok"] is True, result
    assert result["id"].startswith("MU-")
    assert result["path"] == str(image.resolve())
    assert result["caption"] == VALID["caption"]
    assert result["tags"] == VALID["tags"]
    assert result["emotion"] == "happy"
    assert result["confidence"] == pytest.approx(0.92)
    assert result["status"] == "active"

    rows = iu.list_media(temp_db)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == result["id"]
    assert row["attachment_id"] == "ATT-0001"
    assert row["caption"] == VALID["caption"]
    assert row["tags"] == VALID["tags"]
    assert json.loads(row["tags_json"]) == VALID["tags"]
    assert row["emotion"] == "happy"
    assert row["confidence"] == pytest.approx(0.92)
    assert row["status"] == "active"

    call = fake.calls[0]
    assert call["kwargs"]["purpose"] == iu.PURPOSE
    assert call["kwargs"]["json_mode"] is True
    assert call["messages"][0]["role"] == "system"
    content = call["messages"][1]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    url = content[1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == PNG_1X1


def test_describe_image_is_idempotent_and_updates(temp_db, image):
    first = iu.describe_image(str(image), FakeClient(VALID), attachment_id="ATT-9")
    second_payload = dict(VALID, caption="同一张图的新描述")
    second = iu.describe_image(str(image), FakeClient(second_payload),
                               attachment_id="ATT-9")

    assert first["ok"] is True and second["ok"] is True
    assert first["id"] == second["id"]
    rows = iu.list_media(temp_db)
    assert len(rows) == 1
    assert rows[0]["caption"] == "同一张图的新描述"
    assert rows[0]["created_at"] == second["created_at"]


def test_describe_image_dedupes_by_path_without_attachment(temp_db, image):
    first = iu.describe_image(str(image), FakeClient(VALID))
    second = iu.describe_image(str(image), FakeClient(VALID))

    assert first["id"] == second["id"]
    rows = iu.list_media(temp_db)
    assert len(rows) == 1
    assert rows[0]["attachment_id"] == ""


def test_existing_caption_is_passed_as_context(temp_db, image):
    fake = FakeClient(VALID)
    result = iu.describe_image(str(image), fake, attachment_id="ATT-2",
                               existing_caption="旧描述：一只白猫")

    assert result["ok"] is True
    text = fake.calls[0]["messages"][1]["content"][0]["text"]
    assert "旧描述：一只白猫" in text
    # 最终 caption 仍来自本次模型输出，而不是直接复制旧描述
    assert result["caption"] == VALID["caption"]


def test_structured_caption_returns_fields_without_writing(temp_db, image):
    result = iu.structured_caption(str(image), FakeClient(VALID))

    assert result["ok"] is True, result
    assert result["caption"] == VALID["caption"]
    assert result["tags"] == VALID["tags"]
    assert result["emotion"] == "happy"
    assert result["confidence"] == pytest.approx(0.92)
    assert result["path"] == str(image.resolve())
    assert result["mime"] == "image/png"
    assert iu.list_media(temp_db) == []


def test_minimal_client_without_available_is_supported(temp_db, image):
    result = iu.structured_caption(str(image), MinimalClient(VALID))
    assert result["ok"] is True
    assert result["caption"] == VALID["caption"]


def test_no_client_fails_closed(temp_db, image):
    for client in (None, FakeClient(VALID, available=False)):
        assert iu.describe_image(str(image), client)["ok"] is False
        assert iu.structured_caption(str(image), client)["ok"] is False
    assert iu.list_media(temp_db) == []

    class AvailableBoom(FakeClient):
        def available(self):
            raise RuntimeError("availability check broken")

    boom = AvailableBoom(VALID)
    assert iu.describe_image(str(image), boom)["ok"] is False
    assert boom.calls == []

    # 即便已有 caption，也不能在没有 client 时假装成功
    no_client = iu.describe_image(str(image), None, existing_caption="已有描述")
    assert no_client["ok"] is False
    assert no_client["code"] == "client_unavailable"


def test_rejects_non_image_and_remote_paths(temp_db, tmp_path, image):
    text_file = tmp_path / "note.txt"
    text_file.write_text("这不是图片", encoding="utf-8")
    fake_png = tmp_path / "fake.png"
    fake_png.write_bytes(b"not really a png")
    folder = tmp_path / "folder"
    folder.mkdir()
    missing = tmp_path / "missing.png"

    fake = FakeClient(VALID)
    bad_paths = [
        missing,
        text_file,
        fake_png,
        folder,
        "https://example.com/cat.png",
        "data:image/png;base64," + base64.b64encode(PNG_1X1).decode(),
        "",
        "   ",
    ]
    for candidate in bad_paths:
        result = iu.describe_image(candidate, fake)
        assert result["ok"] is False, (candidate, result)
        assert result["code"]

    assert fake.calls == []
    assert iu.list_media(temp_db) == []

    # 合法图片仍然可用，确保上面的失败不是环境问题
    assert iu.describe_image(str(image), FakeClient(VALID))["ok"] is True


def test_image_is_sniffed_by_content_not_extension(temp_db, tmp_path):
    extensionless = tmp_path / "pixel"
    extensionless.write_bytes(PNG_1X1)
    result = iu.structured_caption(str(extensionless), FakeClient(VALID))
    assert result["ok"] is True
    assert result["mime"] == "image/png"


@pytest.mark.parametrize(("payload", "ok", "raises"), [
    (None, False, None),
    (None, True, RuntimeError("client exploded")),
    ("not json at all", True, None),
    ({"caption": "   ", "tags": ["空"]}, True, None),
    ({"tags": ["缺少 caption"]}, True, None),
])
def test_client_failures_fail_closed(temp_db, image, payload, ok, raises):
    fake = FakeClient(payload, ok=ok, raises=raises)
    result = iu.describe_image(str(image), fake, attachment_id="ATT-bad")

    assert result["ok"] is False
    assert result["code"] in {"llm_failed", "client_error", "invalid_output",
                              "empty_caption"}
    assert len(fake.calls) == 1
    assert iu.list_media(temp_db) == []


def test_normalises_fuzzy_payload(temp_db, image):
    payload = {
        "caption": " 一只猫\n在键盘上 ",
        "tags": "猫, 可爱 #萌 猫",
        "emotion": "",
        "confidence": "2",
    }
    result = iu.describe_image(str(image), FakeClient(payload))

    assert result["ok"] is True
    assert result["caption"] == "一只猫 在键盘上"
    assert result["tags"] == ["猫", "可爱", "萌"]
    assert result["emotion"] == "neutral"
    assert result["confidence"] == 1.0


def test_fenced_json_is_parsed(temp_db, image):
    payload = "```json\n" + json.dumps(VALID, ensure_ascii=False) + "\n```"
    result = iu.describe_image(str(image), FakeClient(payload))
    assert result["ok"] is True
    assert result["caption"] == VALID["caption"]


def test_list_media_order_limit_and_tags(temp_db):
    assert iu.list_media(temp_db) == []
    _insert_media(temp_db, "MU-old", 100.0, tags=["旧"])
    _insert_media(temp_db, "MU-new", 300.0, tags=["新"])
    _insert_media(temp_db, "MU-mid", 200.0, tags=["中"])

    rows = iu.list_media(temp_db)
    assert [row["id"] for row in rows] == ["MU-new", "MU-mid", "MU-old"]
    assert rows[0]["tags"] == ["新"]
    assert rows[0]["tags_json"] == '["新"]'

    assert [row["id"] for row in iu.list_media(temp_db, limit=2)] == ["MU-new", "MU-mid"]
    assert iu.list_media(temp_db, limit=0) == []
    assert iu.list_media(temp_db, limit=-3) == []
    assert iu.list_media(temp_db, limit="not-a-number") != []
