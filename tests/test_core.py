"""核心模块离线测试（Lead 维护）。

全部用真实微信截图样本 + 本地 OCR（RapidOCR 已装载时）跑，不依赖微信在线、不点击微信窗口。
"""
from __future__ import annotations

import queue
from collections import Counter
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "core"
FRAME = FIXTURES / "wechat_frame.png"
CONTACTS = FIXTURES / "wechat_contacts.png"
FONT = r"C:\Windows\Fonts\msyh.ttc"
#: 合成消息用例需要用 Windows 中文字体绘制气泡；非 Windows/CI 上没有这个 font → 精确 skip
#: （不是整文件 ignore：其它不依赖字体的用例照常跑）
REQUIRES_WINDOWS_FONT = pytest.mark.skipif(
    not Path(FONT).is_file(),
    reason=f"需要 Windows 中文字体 {FONT} 绘制合成消息（非 Windows/CI 跳过）",
)

DPI = 120  # 样本截图来自 125% 缩放


def _load(name: Path) -> Image.Image:
    if not name.exists():
        pytest.skip(f"缺少样本 {name}")
    return Image.open(name).convert("RGB")


@pytest.fixture(scope="module")
def ocr():
    try:
        from wx_core.ocr import OCRWorker

        worker = OCRWorker()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"OCR 不可用: {exc}")
    yield worker
    try:
        worker.close()
    except Exception:
        pass


@pytest.fixture(scope="module")
def frame():
    return _load(FRAME)


@pytest.fixture(scope="module")
def layout(frame):
    from wx_core import layout as L

    lay = L.analyze(frame, dpi=DPI, content=(9, 0, 1109, 800))
    return lay


# ---------------- 布局 ----------------
def test_layout_boxes(layout, frame):
    w, h = frame.size
    for name in ("content", "sidebar", "conv_list", "chat", "header", "message_area", "input_area", "search_box"):
        box = getattr(layout, name)
        assert 0 <= box[0] < box[2] <= w, f"{name} 横向越界: {box}"
        assert 0 <= box[1] < box[3] <= h, f"{name} 纵向越界: {box}"
    assert layout.sidebar[2] < layout.conv_list[2] < layout.chat[0] + 5
    assert layout.header[3] <= layout.message_area[1] <= layout.input_area[1] <= h
    assert layout.message_area[3] > layout.message_area[1] + 100
    assert layout.scale == pytest.approx(DPI / 96, 0.01)
    assert layout.dark_theme is True
    assert layout.chat_bg == (30, 30, 31)


def test_layout_bubble_colors(layout):
    assert layout.incoming_bubble[0] == layout.incoming_bubble[1]  # 灰色系
    assert layout.outgoing_bubble[1] > 150  # 绿色系


# ---------------- OCR 与消息解析 ----------------
def test_ocr_reads_chat(layout, ocr):
    from wx_core import extract as E

    lines = E.merge_ocr_rows(ocr.ocr(_crop_message(layout)))
    texts = [ln.text for ln in lines]
    joined = " ".join(texts)
    assert "你去看看" in joined, texts
    assert "参数的构造还在做" in joined, texts


def _crop_message(layout):
    img = _load(FRAME)
    return img.crop(layout.message_area)


def test_parse_frame_text_and_sender(layout, ocr, frame):
    from wx_core import extract as E

    blocks = E.parse_frame(frame, layout, ocr, "白云一片去悠悠")
    texts = [(b.kind, b.sender, b.text) for b in blocks]
    assert any(b.sender == "friend" and "你去看看" in b.text for b in blocks), texts
    assert any(b.sender == "friend" and "参数的构造还在做" in b.text for b in blocks), texts
    assert any(b.sender == "self" and "剩下你cp过来就行" in b.text for b in blocks), texts
    assert any(b.kind == "system" and "16:34" in b.text for b in blocks), texts
    assert any(b.kind in ("image", "emoji") for b in blocks), texts


def _synth_new_message(base: Image.Image, layout, text: str, y_center: int) -> Image.Image:
    """在聊天区画一个"收到的文字气泡"，模拟对方发来新消息。"""
    img = base.copy()
    d = ImageDraw.Draw(img)
    mx0, my0, mx1, my1 = layout.message_area
    font = ImageFont.truetype(FONT, 16)
    tw = int(d.textlength(text, font=font))
    y = y_center
    ax = mx0 + 30
    d.rounded_rectangle((ax, y - 20, ax + 40, y + 20), radius=8, fill=(90, 90, 95))
    bx0 = ax + 52
    d.rounded_rectangle((bx0, y - 18, bx0 + tw + 24, y + 18), radius=6, fill=layout.incoming_bubble)
    d.text((bx0 + 12, y - 10), text, font=font, fill=(225, 225, 226))
    return img


@REQUIRES_WINDOWS_FONT
def test_synth_new_message_detected(layout, ocr, frame):
    from wx_core import extract as E

    base_blocks = E.parse_frame(frame, layout, ocr, "白云一片去悠悠")
    baseline = Counter(f"{b.kind}|{b.sender}|{E.visual_hash_of(b)}" for b in base_blocks)

    frame2 = _synth_new_message(frame, layout, "这是一条测试新消息", layout.message_area[3] - 130)
    blocks2 = E.parse_frame(frame2, layout, ocr, "白云一片去悠悠")
    seen: Counter = Counter()
    fresh = []
    for b in blocks2:
        sig = f"{b.kind}|{b.sender}|{E.visual_hash_of(b)}"
        seen[sig] += 1
        if seen[sig] > baseline.get(sig, 0):
            fresh.append(b)
    assert any("测试新消息" in (b.text or "") for b in fresh), [(b.kind, b.text) for b in blocks2]


def test_same_frame_no_duplicate(layout, ocr, frame):
    from wx_core import extract as E

    blocks = E.parse_frame(frame, layout, ocr, "x")
    baseline = Counter(f"{b.kind}|{b.sender}|{E.visual_hash_of(b)}" for b in blocks)
    seen: Counter = Counter()
    fresh = []
    for b in blocks:
        sig = f"{b.kind}|{b.sender}|{E.visual_hash_of(b)}"
        seen[sig] += 1
        if seen[sig] > baseline.get(sig, 0):
            fresh.append(b)
    assert fresh == []


# ---------------- 列表行 ----------------
def test_list_rows_conv(layout, ocr, frame):
    from wx_core import extract as E

    rows = E.read_list_rows(frame, layout, ocr)
    names = [r["name"] for r in rows]
    assert any("文件传输助手" in n for n in names), names
    assert any("微信团队" in n for n in names), names
    for r in rows:
        assert r["center_y"] > 0


def test_list_rows_contacts(ocr):
    from wx_core import extract as E
    from wx_core import layout as L

    img = _load(CONTACTS)
    lay = L.analyze(img, dpi=DPI, content=(9, 0, 1109, 800))
    rows = E.read_list_rows(img, lay, ocr)
    names = [r["name"] for r in rows]
    assert any("白云一片去悠悠" in n for n in names), names


# ---------------- 工具函数 ----------------
def test_norm_name_and_title_matches():
    from wx_core.monitor import norm_name, title_matches

    assert norm_name("白云一片去悠悠") == "白云一片去悠悠"
    assert norm_name(" 微信 团队 (3)") == "微信团队"
    assert title_matches("白云一片去悠悠", "白云一片去悠悠")
    assert title_matches("文件传输助手(2)", "文件传输助手")
    assert not title_matches("张三", "李四")


def test_store_save_image(tmp_path, monkeypatch):
    from wx_core import store

    monkeypatch.setattr(store, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(store, "LOG_DIR", tmp_path / "log")
    monkeypatch.setattr(store, "MESSAGES_LOG", tmp_path / "log" / "messages.jsonl")
    img = Image.new("RGB", (40, 30), (10, 20, 30))
    rel = store.save_image("测试好友", "image", img)
    assert rel.endswith(".png")
    assert (ROOT / rel).exists() or Path(rel).exists()
    frames = [Image.new("RGB", (20, 20), (i * 20, 0, 0)) for i in range(3)]
    rel2 = store.save_image("测试好友", "emoji", frames[0], frames)
    assert rel2.endswith(".gif")


def test_models_message_dict_roundtrip():
    from wx_core.models import Message

    m = Message(friend="a", kind="text", text="hi", extra={"visual_hash": "abc"})
    d = m.to_dict()
    m2 = Message.from_dict(d)
    assert m2.friend == m.friend and m2.kind == m.kind and m2.text == m.text
    assert d["kind_label"] == "文字"


def test_monitor_baseline_and_emit(layout, ocr, frame, tmp_path, monkeypatch):
    """模拟 monitor 的基线/去重行为：首帧只建基线，同帧重复不推送。"""
    from wx_core import monitor, store

    monkeypatch.setattr(store, "MEDIA_DIR", tmp_path / "media")
    monkeypatch.setattr(store, "LOG_DIR", tmp_path / "log")
    monkeypatch.setattr(store, "MESSAGES_LOG", tmp_path / "log" / "messages.jsonl")
    be = monitor.MonitorBackend({"monitored": ["白云一片去悠悠"], "friends_cache": ["白云一片去悠悠"]})
    be._ocr = ocr
    be._parse_and_emit(None, layout, frame, ocr, "白云一片去悠悠")
    assert be.events.empty() or all(e["type"] != "message" for e in list(be.events.queue))
    be._parse_and_emit(None, layout, frame, ocr, "白云一片去悠悠")
    msgs = [e for e in list(be.events.queue) if e["type"] == "message"]
    assert msgs == []


def test_diff_frames_fuzzy_ocr_jitter():
    """OCR 抖动（多一个标点）不应被当成新消息。"""
    from wx_core import monitor
    from wx_core.extract import ParsedBlock

    def blk(text, sender="friend"):
        return ParsedBlock(box=(0, 0, 10, 10), kind="text", sender=sender, text=text)

    base = [(blk("何工，参数这边客户没问题，剩下你cp过来就行"), "16:34", "sig1")]
    same_jitter = [(blk("何工，：参数这边客户没问题，剩下你cp过来就行"), "16:34", "sig1b")]
    new = monitor.MonitorBackend._diff_frames(base, same_jitter)
    assert new == [], new

    genuinely_new = base + [(blk("这是一条全新的消息"), "16:35", "sig2")]
    new2 = monitor.MonitorBackend._diff_frames(base, genuinely_new)
    assert len(new2) == 1 and "全新" in new2[0][0].text

    # 完全相同的两条重复消息仍要各推一次（精确匹配优先，第二条无旧块可配 → 推送）
    base2 = [(blk("在吗"), "", "sig1")]
    twice2 = [(blk("在吗"), "", "sig1"), (blk("在吗"), "", "sig1b")]
    out = monitor.MonitorBackend._diff_frames(base2, twice2)
    assert len(out) == 1, [x[0].text for x in out]


# ---------------- 纯文本聊天记录 / Emoji 中文名 ----------------
def test_emoji_normalize():
    from wx_core import textlog

    assert textlog.normalize_emoji("😊哈哈") == "[微笑]哈哈"
    assert textlog.normalize_emoji("😭😭") == "[大哭][大哭]"
    assert textlog.normalize_emoji("正常中文") == "正常中文"
    # 已经是微信中文名的保持原样
    assert textlog.normalize_emoji("[微笑]") == "[微笑]"


def test_textlog_line_format():
    from wx_core import textlog
    from wx_core.models import Message

    m = Message(friend="白云一片去悠悠", kind="text", sender="friend", text="测试语句1")
    assert textlog.line_for(m) == "白云一片去悠悠: 测试语句1"

    me = Message(friend="白云一片去悠悠", kind="text", sender="self", text="测试语句1")
    assert textlog.line_for(me) == "我: 测试语句1"

    emoji_msg = Message(friend="白云", kind="text", sender="friend", text="😊")
    assert textlog.line_for(emoji_msg) == "白云: [微笑]"

    img = Message(friend="白云", kind="image", sender="friend", media_path="media/a.png")
    assert textlog.line_for(img).startswith("白云: [图片]")


def test_dat_decrypt_v3_and_v4():
    import os

    from wx_core import dat_decrypt as D

    # V3：整文件异或
    png = b"\x89PNG\r\n\x1a\n" + os.urandom(200) + b"GIF89a" + os.urandom(100)
    xor_key = 0x5A
    enc = bytes(b ^ xor_key for b in png)
    plain, ext = D.decrypt_auto(enc, xor_key, None)
    assert plain == png and ext == "png"

    # V4/V2：AES-128-ECB 头 + 明文段 + 异或尾
    dat = D.build_v2_dat(png, b"0123456789abcdef", xor_key=xor_key, xor_size=32, raw_size=64)
    plain2, ext2 = D.decrypt_auto(dat, xor_key, "0123456789abcdef")
    assert plain2 == png and ext2 == "png"


# ---------------- 图片缓存解密选择（宽高比匹配） ----------------
def test_image_decrypt_selection(tmp_path, monkeypatch):
    import io as _io

    from PIL import Image

    from wx_core import dat_decrypt, media_cache

    xor, aes = 0x52, "0669b78f42c9a7d7"

    def make_png(w, h, color):
        buf = _io.BytesIO()
        Image.new("RGB", (w, h), color).save(buf, format="PNG")
        return buf.getvalue()

    # 两张图：竖图 3:4 与横图 4:3，分别构造 V2 .dat
    p_wide = tmp_path / "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.dat"
    p_tall = tmp_path / "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb_h.dat"
    p_wide.write_bytes(dat_decrypt.build_v2_dat(make_png(400, 300, (10, 20, 30)), aes.encode(), xor_key=xor))
    p_tall.write_bytes(dat_decrypt.build_v2_dat(make_png(300, 400, (30, 20, 10)), aes.encode(), xor_key=xor))

    def fake_recent(max_age=180.0, limit=40):
        import os
        now = os.path.getmtime(p_wide)
        return [media_cache.CacheFile(p_wide, now + 2, p_wide.stat().st_size),
                media_cache.CacheFile(p_tall, now + 1, p_tall.stat().st_size)]

    monkeypatch.setattr(media_cache, "_recent_encrypted_images", fake_recent)
    monkeypatch.setattr(media_cache, "_DEC_CACHE", {})
    used: set[str] = set()
    out = media_cache.decrypt_newest_image(xor, aes, tmp_path / "out", used=used, aspect=4 / 3, max_age=86400)
    assert out is not None and out.exists()
    im = Image.open(out)
    assert im.size == (400, 300)  # 宽高比匹配上了横图
    assert used


def test_wechat_keys_aes_check():
    import io as _io

    from PIL import Image

    from wx_core import dat_decrypt, wechat_keys

    buf = _io.BytesIO()
    Image.new("RGB", (64, 64), (1, 2, 3)).save(buf, format="PNG")
    png = buf.getvalue()
    aes = "0123456789abcdef"
    dat = dat_decrypt.build_v2_dat(png, aes.encode(), xor_key=0x33)
    probe = dat[15:31]
    assert wechat_keys._aes_ok(aes, [probe]) is True
    assert wechat_keys._aes_ok("ffffffffffffffff", [probe]) is False


# ---------------- 表情包（贴纸）原文件解密 ----------------
def test_emoticon_key_and_decrypt():
    import hashlib
    import os

    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad

    from wx_core import emoticon

    seed, wxid = 3908286546, "wxid_test"
    key = emoticon.emoticon_key(seed, wxid)
    assert key == hashlib.md5(f"{seed}{wxid}EMOTICON".encode()).digest()[:16]

    plain = b"GIF89a" + os.urandom(100)
    enc = AES.new(key, AES.MODE_CBC, key).encrypt(pad(plain, 16))
    assert emoticon.decrypt_bytes(enc, key) == plain
    # 错的 key 解出来不是图片魔数
    wrong = emoticon.decrypt_bytes(enc, b"0" * 16)
    assert wrong != plain


def test_builtin_emoji_match_synthetic():
    from pathlib import Path

    from PIL import Image

    from wx_core import builtin_emoji

    asset_dir = Path(builtin_emoji.__file__).resolve().parent / "emoji_assets"
    if not (asset_dir / "微笑.png").exists():
        pytest.skip("缺少 emoji 素材")
    a = Image.open(asset_dir / "微笑.png").convert("RGBA").resize((36, 36), Image.LANCZOS)
    base = Image.new("RGB", (56, 50), (53, 210, 141))
    base.paste(a, (10, 7), a)
    assert builtin_emoji.match(base) == "微笑"


def test_textlog_media_lines():
    from wx_core import textlog
    from wx_core.models import Message

    voice = Message(friend="好友", kind="voice", sender="friend", extra={"duration": "2秒"})
    assert textlog.line_for(voice) == "好友: [语音] 2秒"

    sticker = Message(friend="好友", kind="emoji", sender="self",
                      media_path="media/stickers/abc.gif", extra={"original": True})
    line = textlog.line_for(sticker)
    assert line.startswith("我: [表情包]") and "abc.gif" in line


# ---------------- 贴纸匹配策略（视觉 + 时间戳 + 歧义保护） ----------------
def _mk_sticker(md5, color, mtime=None, animated=True):
    import time as _t

    import numpy as np
    from PIL import Image

    from wx_core.emoticon import Sticker

    img = Image.new("RGB", (32, 32), color)
    frames = [np.asarray(img).astype(np.float32)]
    return Sticker(md5=md5, fmt="gif", media_path=Path("/tmp") / f"{md5}.gif",
                   mtime=mtime if mtime is not None else _t.time() - 3600,
                   frames=frames, animated=animated, size=(300, 300))


def test_sticker_match_visual():
    from PIL import Image

    from wx_core import emoticon

    red = _mk_sticker("red", (220, 30, 30))
    blue = _mk_sticker("blue", (30, 30, 220))
    lib = {"red": red, "blue": blue}
    block = Image.new("RGB", (100, 100), (30, 30, 31))
    block.paste(Image.new("RGB", (60, 60), (215, 35, 35)), (20, 20))
    m = emoticon.match_sticker(block, (30, 30, 31), lib)
    assert m is not None and m.md5 == "red"


def test_sticker_match_fresh_timestamp_binding():
    import time as _t

    from PIL import Image

    from wx_core import emoticon

    old = _mk_sticker("old", (220, 30, 30), mtime=_t.time() - 3600)
    new = _mk_sticker("new", (30, 30, 220), mtime=_t.time())  # 刚写入的缓存文件
    lib = {"old": old, "new": new}
    block = Image.new("RGB", (100, 100), (30, 30, 31))
    block.paste(Image.new("RGB", (60, 60), (215, 35, 35)), (20, 20))  # 视觉更像 old
    m = emoticon.match_sticker(block, (30, 30, 31), lib, recent_within=180)
    # 最近只有一个新文件时，按"时间戳绑定"认定它（实时 1:1 策略）
    assert m is not None and m.md5 == "new"


def test_sticker_match_ambiguous_returns_none():
    from PIL import Image

    from wx_core import emoticon

    a = _mk_sticker("a", (200, 40, 40), animated=False)
    b = _mk_sticker("b", (202, 42, 42), animated=False)
    lib = {"a": a, "b": b}
    block = Image.new("RGB", (100, 100), (30, 30, 31))
    block.paste(Image.new("RGB", (60, 60), (201, 41, 41)), (20, 20))
    assert emoticon.match_sticker(block, (30, 30, 31), lib, recent_within=0) is None


def test_sticker_used_set_prevents_reuse():
    from PIL import Image

    from wx_core import emoticon

    red = _mk_sticker("red", (220, 30, 30))
    lib = {"red": red}
    block = Image.new("RGB", (100, 100), (30, 30, 31))
    block.paste(Image.new("RGB", (60, 60), (215, 35, 35)), (20, 20))
    assert emoticon.match_sticker(block, (30, 30, 31), lib, used=set()) is not None
    assert emoticon.match_sticker(block, (30, 30, 31), lib, used={"red"}) is None


# ---------------- 数据库直读（WCDB） ----------------
def test_wcdb_match_and_decrypt_page(tmp_path):
    import os
    import struct

    from Crypto.Cipher import AES

    from wx_core import wcdb

    page = bytearray(os.urandom(wcdb.PAGE_SZ))
    key = os.urandom(32)
    salt = os.urandom(16)
    page[:16] = salt
    payload = b"SQLite format 3\x00" + os.urandom(wcdb.PAGE_SZ - 16 - 80 - 16)
    iv = os.urandom(16)
    enc = AES.new(key, AES.MODE_CBC, iv).encrypt(payload)
    page[16:16 + len(enc)] = enc
    page[wcdb.PAGE_SZ - 80:wcdb.PAGE_SZ - 64] = iv
    mac_key = __import__("hashlib").pbkdf2_hmac("sha512", key, bytes(b ^ 0x3A for b in salt), 2, 32)
    import hmac as hm
    mac = hm.new(mac_key, bytes(page[16:wcdb.PAGE_SZ - 80 + 16]), __import__("hashlib").sha512)
    mac.update(struct.pack("<I", 1))
    page[wcdb.PAGE_SZ - 64:] = mac.digest()

    cand = key + salt
    hit = wcdb.match_db(bytes(page), {cand})
    assert hit is not None
    k32, s16, plaintext = hit
    assert k32 == key and s16 == salt and plaintext is False

    tmp = tmp_path / "x.db"
    tmp.write_bytes(bytes(page))
    data = wcdb.decrypt_db(tmp, key, False)
    assert data.startswith(b"SQLite format 3")


def test_wcdb_decode_content_zstd():
    from compression import zstd

    from wx_core import wcdb

    raw = "<msg><emoji md5=\"d8a0d235d854ab3fbe7694add4631b25\"/></msg>".encode()
    packed = zstd.compress(raw)
    assert wcdb._decode_content(packed, 4) == raw
    assert wcdb._decode_content("plain text", 0) == "plain text"


def test_db_source_parse_sticker_and_voice():
    from wx_core import db_source

    bridge = object.__new__(db_source.DbBridge)
    bridge.config = {}
    bridge.log = lambda *a, **k: None
    bridge.xor_key = 0
    bridge.aes_key = ""
    bridge.emo_seed = None
    bridge.wxid = "wxid_self"
    bridge.sources = {}
    bridge._used = set()

    voice = bridge._convert("好友", "wxid_friend", {
        "local_id": 1, "local_type": 34, "sender": "wxid_friend", "self": False,
        "create_time": 1700000000,
        "content": '<msg><voicemsg voicelength="1645" /></msg>', "raw_flag": 4})
    assert voice.kind == "voice" and voice.extra.get("duration") == "2秒"

    sticker = bridge._convert("好友", "wxid_friend", {
        "local_id": 2, "local_type": 47, "sender": "wxid_friend", "self": False,
        "create_time": 1700000001,
        "content": '<msg><emoji md5="d8a0d235d854ab3fbe7694add4631b25"/></msg>',
        "raw_flag": 4})
    assert sticker.kind == "emoji"
    assert sticker.extra.get("md5") == "d8a0d235d854ab3fbe7694add4631b25"

    text = bridge._convert("好友", "wxid_friend", {
        "local_id": 3, "local_type": 1, "sender": "wxid_self", "self": True,
        "create_time": 1700000002, "content": "[微笑]", "raw_flag": 0})
    assert text.kind == "text" and text.text == "[微笑]" and text.sender == "self"


# ---------------- 历史同步：临时数据不落盘 ----------------
def test_store_rel_temp_path_absolute(tmp_path):
    from wx_core import store

    outside = tmp_path / "history" / "x.png"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"x")
    rel = store.rel(outside)
    assert Path(rel).is_absolute()


def test_publish_message_persist_false(monkeypatch):
    from wx_core import monitor, store, textlog
    from wx_core.models import Message

    called = {"store": 0, "textlog": 0}
    monkeypatch.setattr(store, "append_message", lambda m: called.__setitem__("store", called["store"] + 1))
    monkeypatch.setattr(textlog, "append", lambda m: called.__setitem__("textlog", called["textlog"] + 1))

    cfg = {"monitored": [], "db_direct": False}
    b = monitor.MonitorBackend(cfg)
    msg = Message(friend="好友", kind="text", sender="self", text="历史消息", ts=1700000000)
    b._publish_message(msg, silent=True, persist=False)
    assert called == {"store": 0, "textlog": 0}
    b._publish_message(msg, silent=True, persist=True)
    assert called == {"store": 1, "textlog": 1}


# ---------------- 红包 / 转账 消息解析 ----------------
def _fake_bridge():
    from wx_core import db_source

    b = object.__new__(db_source.DbBridge)
    b.config = {}
    b.log = lambda *a, **k: None
    b.xor_key = 0
    b.aes_key = ""
    b.emo_seed = None
    b.wxid = "wxid_self"
    b.sources = {}
    b._used = set()
    return b


def test_redpacket_system_message_plain_text():
    b = _fake_bridge()
    xml = ('你领取了妈妈的<wc_custom_link href="weixin://x">红包</wc_custom_link>'
           '<img src="SystemMessages_HongbaoIcon.png"/>')
    msg = b._convert("好友", "wxid_f", {
        "local_id": 1, "local_type": 10000, "sender": "wxid_f", "self": False,
        "create_time": 1700000000, "content": xml, "raw_flag": 4})
    assert msg.kind == "redpacket"
    assert msg.text == "你领取了妈妈的红包"


def test_transfer_appmsg():
    b = _fake_bridge()
    xml = ('<msg><appmsg><title><![CDATA[微信转账]]></title>'
           '<des><![CDATA[收到转账9.90元。如需收钱，请点此升级至最新版本]]></des>'
           '<type><![CDATA[2000]]></type></appmsg></msg>')
    msg = b._convert("好友", "wxid_f", {
        "local_id": 2, "local_type": 49, "sender": "wxid_f", "self": False,
        "create_time": 1700000001, "content": xml, "raw_flag": 4})
    assert msg.kind == "transfer"
    assert "微信转账" in msg.text and "9.90" in msg.text


def test_plain_from_xml():
    from wx_core import db_source

    plain = db_source._plain_from_xml(
        'abc<img src="x.png"/>你领取了<wc_custom_link href="y">红包</wc_custom_link>')
    assert plain == "abc你领取了红包"


# ---------------- 语音/视频通话记录（voipmsg） ----------------
def test_voip_voice_call_message():
    b = _fake_bridge()
    xml = ('<voipmsg type="VoIPBubbleMsg"><VoIPBubbleMsg><msg><![CDATA[对方已取消]]></msg>'
           '<room_type>1</room_type><red_dot>true</red_dot></VoIPBubbleMsg></voipmsg>')
    msg = b._convert("好友", "wxid_f", {
        "local_id": 1, "local_type": 50, "sender": "wxid_f", "self": False,
        "create_time": 1700000000, "content": xml, "raw_flag": 4})
    assert msg.kind == "call"
    assert msg.text == "语音通话：对方已取消"


def test_voip_duration_message():
    b = _fake_bridge()
    xml = ('<voipmsg type="VoIPBubbleMsg"><VoIPBubbleMsg><msg><![CDATA[通话时长 01:31]]></msg>'
           '<room_type>1</room_type></VoIPBubbleMsg></voipmsg>')
    msg = b._convert("好友", "wxid_f", {
        "local_id": 2, "local_type": 50, "sender": "self", "self": True,
        "create_time": 1700000001, "content": xml, "raw_flag": 4})
    assert msg.kind == "call"
    assert msg.text == "语音通话 通话时长 01:31"


def test_voip_video_call_message():
    b = _fake_bridge()
    xml = ('<voipmsg type="VoIPBubbleMsg"><VoIPBubbleMsg><msg><![CDATA[通话时长 00:07]]></msg>'
           '<room_type>2</room_type></VoIPBubbleMsg></voipmsg>')
    msg = b._convert("好友", "wxid_f", {
        "local_id": 3, "local_type": 50, "sender": "self", "self": True,
        "create_time": 1700000002, "content": xml, "raw_flag": 4})
    assert msg.kind == "call"
    assert msg.text == "视频通话 通话时长 00:07"
