# -*- coding: utf-8 -*-
"""RouterChatClient：识图请求按用途路由到本地 VL / 线上 API 的适配层测试。"""


class FakeRouter:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result
        self.error = error

    def chat(self, purpose, messages, **kwargs):
        self.calls.append((purpose, messages, kwargs))
        if self.error:
            raise self.error
        return self.result


def test_router_chat_client_forwards_purpose_and_messages():
    from agi.llm.vision import RouterChatClient
    fake = FakeRouter({"ok": True, "text": "{\"caption\": \"a cat\"}",
                       "backend": "local", "usage": {"tokens_in": 10, "tokens_out": 5}})
    client = RouterChatClient(router=fake)
    messages = [{"role": "user", "content": [{"type": "text", "text": "看图"}]}]
    out = client.chat(messages, max_tokens=300, temperature=0.2, json_mode=True)
    assert out["ok"] is True and "cat" in out["text"]
    assert out["model"] == "local"
    purpose, sent, kwargs = fake.calls[0]
    assert purpose == "selflearn_image_understanding"
    assert sent == messages
    assert kwargs["max_tokens"] == 300 and kwargs["json_mode"] is True


def test_router_chat_client_maps_failure():
    from agi.llm.vision import RouterChatClient
    fake = FakeRouter({"ok": False, "error": "all_backends_failed", "text": ""})
    out = RouterChatClient(router=fake).chat([{"role": "user", "content": "x"}])
    assert out["ok"] is False and "all_backends_failed" in out["error"]


def test_router_chat_client_handles_exception():
    from agi.llm.vision import RouterChatClient
    out = RouterChatClient(router=FakeRouter(error=RuntimeError("boom"))).chat([])
    assert out["ok"] is False and "boom" in out["error"]


def test_image_understanding_accepts_router_client(tmp_path, monkeypatch):
    from agi.selflearn import image_understanding
    from PIL import Image
    path = tmp_path / "pic.png"
    Image.new("RGB", (32, 32), "#336699").save(path)
    fake = FakeRouter({"ok": True, "backend": "local", "usage": {"tokens_in": 3, "tokens_out": 4},
                       "text": '{"caption": "纯蓝色方块", "tags": ["蓝色"], "confidence": 0.8}'})
    from agi.llm.vision import RouterChatClient
    client = RouterChatClient(router=fake)
    monkeypatch.setattr(image_understanding, "DB_PATH", tmp_path / "m.sqlite3")
    out = image_understanding.describe_image(path, client, db_path=tmp_path / "m.sqlite3")
    assert out["ok"] is True
    assert out["caption"] == "纯蓝色方块"
    assert fake.calls and fake.calls[0][0] == "selflearn_image_understanding"
