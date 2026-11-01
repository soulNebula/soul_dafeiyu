# -*- coding: utf-8 -*-
"""V1 PR9：`agi/integrations/bili` 入库（扫码/推荐流/关键词/展示级弹幕评论/刷一轮）。

全部 tmp 路径 + monkeypatch 假 HTTP，不联网、不写真实数据目录。
"""
from __future__ import annotations

import json
import zlib
from pathlib import Path

import pytest

from agi.integrations.bili import auth, common, feed, keywords, video, worker

DNM_XML = ('<?xml version="1.0" encoding="UTF-8"?><i><d p="1.5,1,25,16777215,'
           '0,0,0,1">第一条弹幕</d><d p="9.9,1,25,16777215,0,0,0,2">第二条</d></i>')


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.delenv("SOULAGENT_BOT_ROOT", raising=False)
    monkeypatch.setenv("BILI_CREDENTIAL_PATH", str(tmp_path / "bili.json"))
    common.configure_rate_limit(0)
    common.reset_rate_limit()
    common.configure_rate_limit(0)
    yield
    common.configure_rate_limit(None)
    common.reset_rate_limit()


# --------------------------------------------------------------------------- #
# 凭证
# --------------------------------------------------------------------------- #
def test_credential_roundtrip_is_encrypted(tmp_path):
    path = common.save_credential({"SESSDATA": "sess-1", "bili_jct": "jct-1",
                                   "DedeUserID": "42", "buvid3": "buv-1"})
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    assert raw["scheme"] in ("dpapi", "plain")
    assert "sess-1" not in json.dumps(raw)
    cred = common.load_credential()
    assert cred["sessdata"] == "sess-1" and cred["dedeuserid"] == "42"
    assert "SESSDATA=sess-1" in common.cookie_header(cred)


def test_credential_requires_sessdata():
    with pytest.raises(common.BiliError):
        common.save_credential({"bili_jct": "only-jct"})


def test_import_legacy_credential(tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"SESSDATA": "old", "bili_jct": "j"}),
                      encoding="utf-8")
    result = common.import_legacy_credential(path=legacy)
    assert result["ok"] is True
    assert common.load_credential()["sessdata"] == "old"


def test_credential_path_prefers_bot_root(tmp_path, monkeypatch):
    bot = tmp_path / "bots" / "x"
    bot.mkdir(parents=True)
    monkeypatch.setenv("SOULAGENT_BOT_ROOT", str(bot))
    monkeypatch.delenv("BILI_CREDENTIAL_PATH", raising=False)
    assert common.credential_path() == bot / "secrets" / "bili.json"


# --------------------------------------------------------------------------- #
# 扫码登录
# --------------------------------------------------------------------------- #
def test_qr_generate(monkeypatch):
    monkeypatch.setattr(auth, "http_json", lambda *a, **k: {
        "code": 0, "data": {"url": "https://passport.bilibili.com/x", "qrcode_key": "K1"}})
    result = auth.qr_generate()
    assert result["ok"] is True and result["key"] == "K1"


@pytest.mark.parametrize("code,state", [(86101, "waiting"), (86090, "scanned"),
                                        (86038, "expired")])
def test_qr_poll_states(monkeypatch, code, state):
    monkeypatch.setattr(auth, "http_json", lambda *a, **k: {"code": 0, "data": {"code": code}})
    result = auth.qr_poll("K1")
    assert result["state"] == state and result["ok"] is False


def test_qr_poll_ok_returns_credential(monkeypatch):
    url = ("https://passport.bilibili.com/login?SESSDATA=s1&bili_jct=j1"
           "&DedeUserID=9&buvid3=b1")
    monkeypatch.setattr(auth, "http_json",
                        lambda *a, **k: {"code": 0, "data": {"code": 0, "url": url}})
    result = auth.qr_poll("K1")
    assert result["ok"] is True
    assert result["credential"]["sessdata"] == "s1"
    saved = auth.save_login(result["credential"])
    assert saved["ok"] is True and Path(saved["credential_path"]).is_file()


def test_login_status_without_credential():
    result = auth.login_status({})
    assert result["logged_in"] is False and result["reason"] == "no_credential"


def test_login_status_logged_in(monkeypatch):
    monkeypatch.setattr(auth, "http_json",
                        lambda *a, **k: {"data": {"isLogin": True, "uname": "UP",
                                                  "mid": 7, "face": "http://f"}})
    result = auth.login_status({"sessdata": "s", "bili_jct": "j"})
    assert result["logged_in"] is True and result["uname"] == "UP"


# --------------------------------------------------------------------------- #
# 推荐流 / 搜索
# --------------------------------------------------------------------------- #
def test_popular_maps_items(monkeypatch):
    monkeypatch.setattr(feed, "http_json", lambda *a, **k: {
        "code": 0, "data": {"list": [
            {"bvid": "BV1", "title": "T1", "owner": {"name": "UP1"},
             "duration": 60, "view": 100},
        ]}})
    rows = feed.popular(5)
    assert rows[0]["bvid"] == "BV1" and rows[0]["url"].endswith("BV1")
    assert rows[0]["owner"] == "UP1" and rows[0]["source"] == "popular"


def test_search_strips_highlight_tags(monkeypatch):
    monkeypatch.setattr(feed, "http_json", lambda *a, **k: {
        "code": 0, "data": {"result": [
            {"bvid": "BV2", "title": '前<em class="keyword">测试</em>后',
             "author": "UP2", "duration": 30, "play": 5},
        ]}})
    rows = feed.search("测试", 5)
    assert rows[0]["title"] == "前测试后" and rows[0]["source"] == "search"


def test_search_empty_keyword_no_request(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not call")
    monkeypatch.setattr(feed, "http_json", boom)
    assert feed.search("  ", 5) == []


def test_recommend_falls_back_to_popular(monkeypatch):
    def fake(url, **kwargs):
        if "rcmd" in url:
            return {"code": -352, "data": {}}
        return {"code": 0, "data": {"list": [{"bvid": "BV3", "title": "热门"}]}}
    monkeypatch.setattr(feed, "http_json", fake)
    result = feed.recommend(5)
    assert result["ok"] is True and result["fallback"] is True
    assert result["source"] == "popular" and result["items"][0]["bvid"] == "BV3"


# --------------------------------------------------------------------------- #
# 关键词
# --------------------------------------------------------------------------- #
def test_keywords_config_wins():
    result = keywords.resolve_keywords(["AI 绘画"], ["随便一个标题"])
    assert result == {"keywords": ["AI 绘画"], "source": "config"}


def test_keywords_llm_json():
    def fake_chat(purpose, messages, **kwargs):
        assert purpose == "keyword_extract"
        return {"ok": True, "text": '{"keywords": ["显卡", "装机"]}'}
    result = keywords.resolve_keywords([], ["显卡装机教程", "装机避坑"], llm_chat=fake_chat)
    assert result["source"] == "llm" and result["keywords"] == ["显卡", "装机"]


def test_keywords_rules_when_llm_down():
    def fake_chat(purpose, messages, **kwargs):
        return {"ok": False, "error": "down"}
    result = keywords.resolve_keywords([], ["显卡装机教程", "显卡装机避坑"],
                                       llm_chat=fake_chat)
    assert result["source"] == "rule" and "显卡" in result["keywords"]


# --------------------------------------------------------------------------- #
# 展示级评论/弹幕
# --------------------------------------------------------------------------- #
def test_display_disabled_makes_no_request(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not call")
    monkeypatch.setattr(video, "video_info", boom)
    result = video.display("BV1")
    assert result["ok"] is False and result["error"] == "display_disabled"


def test_comments_and_danmaku(monkeypatch):
    monkeypatch.setattr(video, "video_info", lambda *a, **k: {
        "bvid": "BV1", "aid": 11, "cid": 22, "title": "T", "owner": "UP"})

    def fake_json(url, **kwargs):
        return {"code": 0, "data": {"replies": [
            {"member": {"uname": "甲"}, "content": {"message": "好看"}, "like": 3}]}}
    monkeypatch.setattr(video, "http_json", fake_json)
    monkeypatch.setattr(video, "http_bytes",
                        lambda *a, **k: zlib.compress(DNM_XML.encode("utf-8")))
    result = video.display("BV1", show_comments=True, show_danmaku=True)
    assert result["ok"] is True
    assert result["comments"][0]["text"] == "好看"
    assert [item["text"] for item in result["danmaku"]] == ["第一条弹幕", "第二条"]
    assert result["danmaku"][0]["time"] == 1.5


def test_comments_failure_is_reported(monkeypatch):
    monkeypatch.setattr(video, "video_info", lambda *a, **k: {"aid": 1, "cid": 2})
    def boom(*a, **k):
        raise common.BiliError("denied")
    monkeypatch.setattr(video, "http_json", boom)
    result = video.display("BV1", show_comments=True)
    assert result["ok"] is False
    assert any("comments:denied" in item for item in result["errors"])


# --------------------------------------------------------------------------- #
# 刷一轮
# --------------------------------------------------------------------------- #
def test_worker_disabled_skips(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("should not call")
    monkeypatch.setattr(worker, "recommend", boom)
    result = worker.run_once(enabled=False)
    assert result["skipped"] == "disabled"


def test_worker_picks_keyword_matches_and_remembers(tmp_path, monkeypatch):
    items = [
        {"bvid": "BV1", "title": "显卡装机教程", "desc": ""},
        {"bvid": "BV2", "title": "美食探店", "desc": ""},
        {"bvid": "BV3", "title": "显卡实测", "desc": ""},
    ]
    monkeypatch.setattr(worker, "recommend",
                        lambda *a, **k: {"ok": True, "items": items, "source": "popular",
                                         "fallback": False})
    state = tmp_path / "state.json"
    result = worker.run_once(enabled=True, keywords=["显卡"], limit=2,
                             path=state, now=100.0)
    assert result["ok"] is True and {i["bvid"] for i in result["items"]} == {"BV1", "BV3"}
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["last_run"] == 100.0 and set(saved["seen"]) == {"BV1", "BV3"}
    # 第二轮（同一批次）里没有「新鲜」的了
    again = worker.run_once(enabled=True, keywords=["显卡"], limit=2,
                            path=state, now=200.0)
    assert again["fresh"] == 0


def test_worker_uses_llm_keywords(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "recommend",
                        lambda *a, **k: {"ok": True, "items": [
                            {"bvid": "BV9", "title": "新游戏首发", "desc": ""}],
                            "source": "popular", "fallback": False})
    def fake_chat(purpose, messages, **kwargs):
        return {"ok": True, "text": '{"keywords": ["新游戏"]}'}
    result = worker.run_once(enabled=True, keywords=[], limit=3,
                             path=tmp_path / "s.json", llm_chat=fake_chat)
    assert result["keywords_source"] == "llm"
    assert result["items"][0]["bvid"] == "BV9"

def test_credential_state_reports_legacy_source(tmp_path, monkeypatch):
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"SESSDATA": "old"}), encoding="utf-8")
    monkeypatch.setattr(common, "LEGACY_CREDENTIAL", legacy)
    assert common.credential_state()["source"] == "legacy"
    common.save_credential({"sessdata": "new"})
    assert common.credential_state()["source"] == "repo"


def test_save_credential_plain_fallback_roundtrip(tmp_path, monkeypatch):
    """PR9: encode_secrets 不可用时的 plain 回退必须仍能被 load_credential 读回。"""
    monkeypatch.setattr(common, "credential_path", lambda: tmp_path / "cred.json")
    import agi.bot_runtime as bot_runtime
    def boom(_payload):
        raise RuntimeError("dpapi unavailable")
    monkeypatch.setattr(bot_runtime, "encode_secrets", boom)
    common.save_credential({"sessdata": "S1", "bili_jct": "J1"})
    raw = json.loads((tmp_path / "cred.json").read_text(encoding="utf-8"))
    assert raw["scheme"] == "plain" and isinstance(raw["data"], str)
    loaded = common.load_credential()
    assert loaded["sessdata"] == "S1" and loaded["bili_jct"] == "J1"
