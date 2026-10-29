"""数据库直读消息源：把微信加密消息库里的消息（文字/图片/表情包/文件/语音）
转成统一的 Message，并解析出**原始媒体文件**（本地解密），与消息精确对应。

相比 OCR + 截图方案：
* 文字是精确原文（含 [微笑] 这类表情名），不用 OCR/复制；
* 图片按消息里记录的 md5/尺寸指纹精确找到本地 .dat 并解密成原图；
* 表情包按消息里的 md5 精确找到缓存文件，解密成原 GIF/wxgf；
* 语音时长、文件名直接来自消息 XML。
"""
from __future__ import annotations

import hashlib
import math
import re
import time
from pathlib import Path

from . import dat_decrypt, emoticon, media_cache, store, wcdb
from .models import (KIND_CALL, KIND_EMOJI, KIND_FILE, KIND_IMAGE, KIND_REDPACKET,
                     KIND_SYSTEM, KIND_TEXT, KIND_TRANSFER, KIND_UNKNOWN, KIND_VOICE,
                     SENDER_FRIEND, SENDER_SELF, Message, stable_id)

def _image_ok(path: Path) -> bool:
    """确认文件是 PIL 能打开的图片（防止把坏图/HEVC 塞给 GUI）。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.load()
        return True
    except Exception:
        return False


ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
VOICE_MS_RE = re.compile(r'voicelength="(\d+)"')
BASE_MASK = 0xFFFFFFFF


def _attrs(text: str, limit: int = 4000) -> dict[str, str]:
    return {k: v for k, v in ATTR_RE.findall(text[:limit])}


def _xml_tag(tag: str, text: str) -> str:
    """从 XML/带 CDATA 的内容里取某个标签的纯文本。"""
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", text, re.S)
    if not m:
        return ""
    raw = m.group(1).strip()
    if raw.startswith("<![CDATA["):
        raw = raw[9:]
    if raw.endswith("]]>"):
        raw = raw[:-3]
    return raw.replace("]]>", "").strip()


def _plain_from_xml(text: str) -> str:
    """系统消息里的 XML/HTML → 可读纯文本（红包、转账等系统提示会带一段 XML）。"""
    if not isinstance(text, str) or "<" not in text:
        return (text or "").strip().replace("]]>", "").replace("<![CDATA[", "").strip()
    s = re.sub(r"<img[^>]*>", "", text, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
          .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    s = s.replace("]]>", "").replace("<![CDATA[", "")
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _parse_voip(text: str):
    """语音/视频通话记录（voipmsg）→ (kind, 可读文本)；不是通话消息返回 None。"""
    if not isinstance(text, str):
        return None
    low = text
    if "<voipmsg" not in low and "VoIPBubbleMsg" not in low:
        return None
    msg = _xml_tag("msg", low)
    m_room = re.search(r"<room_type>\s*(\d+)", low)
    room = 0
    if m_room:
        try:
            room = int(m_room.group(1))
        except ValueError:
            room = 0
    label = {1: "语音通话", 2: "视频通话"}.get(room, "通话")
    if not msg:
        msg = "通话记录"
    if msg.startswith("通话时长"):
        out = f"{label} {msg}"
    else:
        out = f"{label}：{msg}"
    return KIND_CALL, out


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    try:
        return content.decode("utf-8", "replace")
    except Exception:
        return ""


def _fmt_seconds(ms: int) -> str:
    try:
        sec = int(math.ceil(ms / 1000.0))
    except Exception:
        sec = 0
    return f"{max(1, sec)}秒"


class DbBridge:
    """一个账号的数据库直读桥：解析会话、读消息、解密媒体。"""

    def __init__(self, config: dict, log=None, media_dir=None):
        self.config = config
        self.media_dir = Path(media_dir) if media_dir else store.MEDIA_DIR
        self.log = log or (lambda *a, **k: None)
        acc = media_cache.active_account_dir()
        self.wxid = str(config.get("image_wxid") or "").strip()
        if not self.wxid:
            # 没缓存账号时从数据目录名推导（<wxid>_<suffix>）
            cand = acc.name if acc is not None else ""
            if cand.startswith("wxid_"):
                self.wxid = cand.rsplit("_", 1)[0] if cand.count("_") >= 2 else cand
        if not self.wxid:
            raise RuntimeError("缺少 image_wxid（且无法从数据目录推导）")
        if acc is None:
            for d in media_cache.account_dirs():
                if d.name == self.wxid or d.name.startswith(self.wxid + "_"):
                    acc = d
                    break
        if acc is None:
            raise RuntimeError("找不到微信数据目录")
        self.account_dir = acc
        keys = wcdb.ensure_keys(self.account_dir, config, log=self.log)
        if not keys:
            raise RuntimeError("数据库密钥提取失败")
        self.reader = wcdb.SessionReader(self.account_dir, keys, self.wxid)
        self.sources: dict[str, dict] = {}
        self.xor_key = int(str(config.get("image_xor_key") or "0"), 0) or 0
        self.aes_key = str(config.get("image_aes_key") or "").strip()
        seed = config.get("image_cfg_dword")
        self.emo_seed = int(seed) if seed not in (None, "") else None
        self._used: set[str] = set()

    def close(self) -> None:
        try:
            self.reader.close()
        except Exception:
            pass

    # ---------- 会话注册 ----------
    def add_friend(self, display_name: str) -> bool:
        username = self.reader.resolve_username(display_name)
        if not username:
            self.log(f"联系人库里找不到「{display_name}」（可能是群/未同步），该好友改用 OCR 模式")
            return False
        self.sources[display_name] = {"username": username, "last_id": 0, "loaded": False}
        self.log(f"数据库直读就绪：{display_name} ↔ {username}")
        return True

    # ---------- 轮询 ----------
    def poll(self) -> list[Message]:
        out: list[Message] = []
        for friend, state in self.sources.items():
            try:
                if not state["loaded"]:
                    rows = self.reader.read_messages(state["username"], 0, limit=400)
                    state["loaded"] = True
                    if not self.config.get("emit_history_on_start", False):
                        state["last_id"] = max((r["local_id"] for r in rows), default=0)
                        rows = []
                    else:
                        keep = int(self.config.get("db_history_count", 20))
                        rows = rows[-keep:] if keep > 0 else rows
                else:
                    rows = self.reader.read_messages(
                        state["username"], state["last_id"],
                        limit=int(self.config.get("db_poll_limit", 50)))
                for row in rows:
                    state["last_id"] = max(state["last_id"], row["local_id"])
                    msg = self._convert(friend, state["username"], row)
                    if msg is not None:
                        out.append(msg)
            except Exception as exc:  # noqa: BLE001
                self.log(f"读数据库消息失败（{friend}）: {exc}")
        return out

    # ---------- 单条消息转换 ----------
    def _convert(self, friend: str, username: str, row: dict) -> Message | None:
        ltype = int(row["local_type"]) & BASE_MASK
        content = row["content"]
        text = _text_of(content)
        sender = SENDER_SELF if row["self"] else SENDER_FRIEND
        ts = float(row["create_time"] or time.time())
        extra: dict = {"db_local_id": row["local_id"]}
        kind = KIND_UNKNOWN
        media_path = ""
        msg_text = ""

        if ltype in (1, 10000, 10002) or (isinstance(content, str) and ltype < 40 and ltype not in (3, 34, 43, 47, 49)):
            if ltype in (10000, 10002):
                plain = _plain_from_xml(text)
                if "红包" in plain or "HongbaoIcon" in text:
                    kind = KIND_REDPACKET
                    msg_text = plain if "红包" in plain else "微信红包"
                elif "转账" in plain:
                    kind = KIND_TRANSFER
                    msg_text = plain
                else:
                    kind = KIND_SYSTEM
                    msg_text = plain or text.strip()
            else:
                kind = KIND_TEXT
                msg_text = text.strip()
        elif ltype == 3:
            kind = KIND_IMAGE
            attrs = _attrs(text)
            extra.update({k: attrs.get(k, "") for k in ("md5", "length", "hdlength", "aeskey")})
            md5 = attrs.get("md5", "")
            if md5:
                extra["md5"] = md5
            media_path = self._resolve_image(username, attrs, ts)
        elif ltype == 47:
            kind = KIND_EMOJI
            attrs = _attrs(text)
            md5 = attrs.get("md5") or attrs.get("androidmd5") or ""
            extra["md5"] = md5
            extra.update({k: attrs.get(k, "") for k in ("productid", "type")})
            media_path = self._resolve_sticker(md5)
        elif ltype == 34:
            kind = KIND_VOICE
            m = VOICE_MS_RE.search(text)
            extra["duration"] = _fmt_seconds(int(m.group(1))) if m else ""
            msg_text = ""
        elif ltype == 43:
            kind = KIND_UNKNOWN
            msg_text = "[视频]"
        elif ltype == 49:
            kind, msg_text, media_path = self._resolve_appmsg(attrs_text=text, ts=ts)
        else:
            voip = _parse_voip(text)
            if voip is not None:
                kind, msg_text = voip
            else:
                kind = KIND_UNKNOWN
                plain = _plain_from_xml(text)
                msg_text = (plain[:200] if plain else "") or "[未知消息]"

        if kind == KIND_TEXT and not msg_text:
            return None
        return Message(friend=friend, kind=kind, sender=sender, text=msg_text,
                       media_path=media_path, extra=extra, ts=ts,
                       id=stable_id(username, row["local_id"]))

    # ---------- 媒体解析 ----------
    def _resolve_image(self, username: str, attrs: dict, ts: float) -> str:
        if not self.aes_key:
            return ""
        target_sizes = []
        for key in ("hdlength", "length", "cdnthumblength"):
            try:
                v = int(attrs.get(key) or 0)
            except ValueError:
                v = 0
            if v > 0:
                target_sizes.append(v)
        if not target_sizes:
            return ""
        attach = self.account_dir / "msg" / "attach" / hashlib.md5(username.encode()).hexdigest()
        best: tuple[float, Path] | None = None
        try:
            files = list(attach.rglob("*.dat"))
        except OSError:
            files = []
        for p in files:
            try:
                size = p.stat().st_size
                mtime = p.stat().st_mtime
            except OSError:
                continue
            # V2 .dat 头部 15 字节 + 校验；_h/_t 也如此，允许 15/31 字节误差
            if not any(abs(size - (n + 31)) <= 1 or abs(size - (n + 15)) <= 1 for n in target_sizes):
                continue
            score = abs(mtime - ts)
            if p.name.endswith("_t.dat"):
                score += 3600
            is_h = p.name.endswith("_h.dat")
            if is_h:
                score -= 600
            if best is None or score < best[0]:
                best = (score, p)
        if best is None:
            return ""
        path = best[1]
        stem = path.name.replace("_h.dat", "").replace("_t.dat", "").replace(".dat", "")
        # 优先用同 stem 的 _h.dat（原图）
        for cand_name in (f"{stem}_h.dat", f"{stem}.dat", path.name):
            cand = path.with_name(cand_name)
            if not cand.exists():
                continue
            try:
                raw = cand.read_bytes()
                plain, ext = dat_decrypt.decrypt_auto(raw, self.xor_key, self.aes_key)
            except Exception:
                continue
            if not plain:
                continue
            out_dir = self.media_dir / "originals"
            out_dir.mkdir(parents=True, exist_ok=True)
            # 微信 4.x 会把原图转成 HEVC 容器(wxgf)，PIL 打不开：
            # 用 ffmpeg 取出第一帧存成 PNG，保证 GUI 能预览。
            if ext in ("hevc", "wxgf", "h265") or plain[:4] == b"wxgf":
                out = out_dir / f"{stem}.png"
                if out.exists() and out.stat().st_size > 100:
                    return store.rel(out)
                if emoticon.wxgf_to_png(plain, out) and _image_ok(out):
                    return store.rel(out)
                self.log(f"图片(HEVC)转 PNG 失败: {stem}")
                return ""
            out = out_dir / f"{stem}.{ext}"
            try:
                out.write_bytes(plain)
            except OSError:
                return ""
            # 坏图（解密不完整/格式不支持）不留给 GUI 显示"无法加载预览"
            if not _image_ok(out):
                self.log(f"图片解密后无法预览，已跳过: {out.name}")
                try:
                    out.unlink()
                except OSError:
                    pass
                return ""
            return store.rel(out)
        return ""

    def _resolve_sticker(self, md5: str) -> str:
        if not md5 or self.emo_seed is None:
            return ""
        if not re.fullmatch(r"[0-9a-fA-F]{16,64}", md5):
            return ""
        p = self._find_sticker_file(md5)
        if p is None:
            return ""
        try:
            raw = p.read_bytes()
        except OSError:
            return ""
        key = emoticon.emoticon_key(self.emo_seed, self.wxid)
        data = emoticon.decrypt_bytes(raw, key)
        if data is None:
            return ""
        fmt = emoticon.detect_format(data)
        if fmt is None:
            return ""
        out_dir = self.media_dir / "stickers"
        out_dir.mkdir(parents=True, exist_ok=True)
        if fmt == "wxgf":
            out = out_dir / f"{md5}.gif"
            if not out.exists() or out.stat().st_size < 100:
                if not emoticon.wxgf_to_gif(data, out):
                    return ""
            return store.rel(out)
        if fmt in ("png", "gif", "jpg", "webp"):
            out = out_dir / f"{md5}.{fmt}"
            if not out.exists():
                out.write_bytes(data)
            return store.rel(out)
        return ""

    def _find_sticker_file(self, md5: str) -> Path | None:
        for pattern in (f"cache/*/Emoticon/*/{md5}", f"business/emoticon/**/{md5}",
                        f"business/emoticon/**/{md5}.thumb"):
            try:
                hits = sorted(self.account_dir.glob(pattern))
            except OSError:
                continue
            if hits:
                return hits[0]
        return None

    def _resolve_appmsg(self, attrs_text: str, ts: float):
        title = _xml_tag("title", attrs_text)
        des = _xml_tag("des", attrs_text)
        feedesc = _xml_tag("feedesc", attrs_text)
        m_type = re.search(r"<type>\s*(?:<!\[CDATA\[)?\s*(\d+)", attrs_text)
        try:
            app_type = int(m_type.group(1)) if m_type else 0
        except ValueError:
            app_type = 0
        if app_type == 2001:  # 微信红包
            label = title or des or "微信红包"
            if feedesc:
                label = f"{label} {feedesc}"
            return KIND_REDPACKET, label, ""
        if app_type == 2000:  # 微信转账
            label = title or "微信转账"
            if des:
                label = f"{label}（{des}）"
            elif feedesc:
                label = f"{label} {feedesc}"
            return KIND_TRANSFER, label, ""
        if app_type == 6 and title:
            path = self._find_file(title)
            media = ""
            if path is not None:
                out_dir = self.media_dir / "files"
                out_dir.mkdir(parents=True, exist_ok=True)
                dest = out_dir / path.name
                try:
                    if not dest.exists():
                        import shutil
                        shutil.copy2(path, dest)
                    media = store.rel(dest)
                except OSError:
                    media = ""
            return KIND_FILE, title, media
        if app_type in (57,):
            m = re.search(r"<refermsg>(.*?)</refermsg>", attrs_text, re.S)
            blob = m.group(1) if m else attrs_text
            refer_type = _xml_tag("type", blob)
            display = _plain_from_xml(_xml_tag("displayname", blob))[:40]
            content = _plain_from_xml(_xml_tag("content", blob))[:200]
            # 微信对图片/表情/内部引用常把 content 写成 wxid/长数字串；不能当人话回显
            internal = bool(re.search(r"(wxid_|:\d{10,}:\d+:)", content))
            if refer_type in ("3", "48"):
                quote = "[图片]"
            elif refer_type in ("47",):
                quote = "[表情包]"
            elif not content or internal:
                quote = "[消息]"
            else:
                quote = content
            label = (display + "：" if display else "") + quote
            # 引用消息的实际正文在 <title>（旧实现只取了 refermsg，导致用户回复的那句话丢失）
            reply = _plain_from_xml(_xml_tag("title", attrs_text)).strip()[:500]
            prefix = "[引用] 对方引用了这条消息：" + label.strip()
            msg_text = prefix + (("\n" + reply) if reply else "")
            return KIND_TEXT, msg_text, ""
        if app_type == 5:
            return KIND_SYSTEM, f"[链接] {title}" if title else "[链接]", ""
        if app_type in (33, 36):
            return KIND_SYSTEM, f"[小程序] {title}" if title else "[小程序]", ""
        if app_type == 3:
            return KIND_SYSTEM, f"[音乐] {title}" if title else "[音乐]", ""
        return KIND_SYSTEM, title or "[应用消息]", ""

    def _find_file(self, title: str) -> Path | None:
        root = self.account_dir / "msg" / "file"
        if not root.is_dir():
            return None
        try:
            for p in root.rglob(title):
                if p.is_file():
                    return p
        except OSError:
            return None
        # 宽松匹配（文件名可能带后缀差异）
        stem = Path(title).stem[:20]
        if stem:
            try:
                for p in root.rglob("*"):
                    if p.is_file() and stem in p.name:
                        return p
            except OSError:
                pass
        return None


def _is_self(username: str, self_wxid: str) -> bool:
    return username == self_wxid


def enumerate_friends(config: dict, mode: str = "recent", log=None) -> list[str]:
    """从数据库列好友/群（默认只保留有历史聊天记录的，符合"没聊过就不显示"）。

    mode:
      * "recent" —— 只列有消息记录的会话，按最近消息时间倒序（默认）
      * "all"    —— 好友（local_type=1）+ 群（local_type=2）里所有有历史记录的
      * "ui"     —— 保留旧的界面滚动枚举（若数据库可用，也会按"有历史记录"过滤）
    """
    import hashlib as _hashlib
    import sqlite3

    log = log or (lambda *a, **k: None)
    acc = media_cache.active_account_dir()
    if acc is None:
        return []
    wxid = str(config.get("image_wxid") or "")
    if not wxid and acc.name.startswith("wxid_"):
        wxid = acc.name.rsplit("_", 1)[0] if acc.name.count("_") >= 2 else acc.name
    keys = wcdb.ensure_keys(acc, config, log=log)
    if not keys:
        return []
    reader = wcdb.SessionReader(acc, keys, wxid)
    try:
        contact_path = reader._materialize("db_storage/contact/contact.db", "_contact_path")
        disp: dict[str, str] = {}
        kinds: dict[str, int] = {}
        if contact_path:
            try:
                con = sqlite3.connect(f"file:{contact_path}?mode=ro", uri=True)
                for u, lt, n in con.execute(
                        "select username, local_type, coalesce(nullif(remark,''), nick_name) "
                        "from contact"):
                    if u:
                        disp[u] = n or u
                        try:
                            kinds[u] = int(lt or 0)
                        except (TypeError, ValueError):
                            kinds[u] = 0
                con.close()
            except sqlite3.Error:
                pass
        msg_path = reader._msgdb()
        if not msg_path:
            return []
        # 会话表 = Msg_<md5(username)>；只保留"有消息记录"的会话
        sessions: list[tuple[int, str]] = []
        try:
            con = sqlite3.connect(f"file:{msg_path}?mode=ro", uri=True)
            tabs = [r[0] for r in con.execute(
                "select name from sqlite_master where type='table' and name like 'Msg_%'")]
            by_md5 = {_hashlib.md5(u.encode()).hexdigest(): u for u in disp}
            try:
                for rid, uname in con.execute("select rowid, user_name from Name2Id"):
                    if uname:
                        by_md5.setdefault(_hashlib.md5(str(uname).encode()).hexdigest(), str(uname))
            except sqlite3.Error:
                pass
            for t in tabs:
                try:
                    row = con.execute(f"select max(create_time) from {t}").fetchone()
                except Exception:
                    continue
                if row and row[0]:
                    u = by_md5.get(t[4:])
                    if u:
                        sessions.append((int(row[0]), u))
            con.close()
        except sqlite3.Error:
            return []
        sessions.sort(reverse=True)
        history_users = {u for _ts, u in sessions}

        def label(u: str) -> str:
            return disp.get(u) or u

        names: list[str] = []
        seen: set[str] = set()
        if mode == "all":
            # 好友(local_type=1) + 群(local_type=2)，且必须有历史记录
            for u, lt in kinds.items():
                if lt not in (1, 2):
                    continue
                if u == wxid or u not in history_users:
                    continue
                if lt == 2 and not u.endswith("@chatroom"):
                    continue
                if lt == 1 and u.endswith("@chatroom"):
                    continue
                if u.startswith("gh_"):
                    continue
                if u in ("notifymessage", "fmessage", "medianote", "weixin", "newsapp",
                         "floatbottle", "qqmail", "tmessage", "qmessage"):
                    continue
                name = label(u)
                if not name or name == "null" or name in seen:
                    continue
                seen.add(name)
                names.append(name)
            # 有历史但联系人表没有名字的会话（陌生/临时会话）也给个名字
            for _ts, u in sessions:
                if u == wxid or u in kinds:
                    continue
                name = label(u)
                if not name or name in seen:
                    continue
                seen.add(name)
                names.append(name)
            return names

        # recent：有历史记录的会话，按最近消息时间倒序
        for _ts, u in sessions:
            if u == wxid:
                continue
            name = label(u)
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
            if len(names) >= 500:
                break
        return names
    finally:
        try:
            reader.close()
        except Exception:
            pass
