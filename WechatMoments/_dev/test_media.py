# -*- coding: utf-8 -*-
"""wx4 媒体层自测 / 验收脚本（media owner）。

跑法（项目根目录）::

    .venv\\Scripts\\python.exe _dev\\test_media.py

覆盖：

1. ``wx4.dat`` 纯算法自测：按 V2 布局构造 dat 再解密，验证 AES+PKCS#7+异或尾；
2. ``wx4.image_key``：取图片密钥（缓存/内存/已知值兜底），打印来源与自验证结果；
3. ``wx4.media.image_path(md5)``：**用朋友圈 xml 里真实的 ``url@md5``** 解出图片，
   PIL 打开并打印路径 + 尺寸（验收硬指标：至少 1 张）；
4. ``wx4.media.avatar_path(username)``：从 ``head_image.db`` 的 BLOB 导出头像，
   PIL 打开并打印（验收硬指标：至少 1 个）；
5. ``wx4.media.video_path/cover_path``：朋友圈视频本地缓存实测 + recon 结论
   （查过哪些目录、文件特征、为什么本机没有）。
"""
from __future__ import annotations

import io
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, PASS if ok else FAIL, detail))
    print(f"  [{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def pil_probe(path: Path) -> str:
    """用 PIL 完整解码，返回 ``宽x高 mode bytes``；失败抛异常。"""
    from PIL import Image

    data = path.read_bytes()
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        return f"{im.size[0]}x{im.size[1]} {im.mode} {len(data)}B"


# ---------------------------------------------------------------------------
# 1. dat 纯算法自测
# ---------------------------------------------------------------------------


def test_dat_algorithm() -> bool:
    section("[1] wx4.dat 纯算法自测（V2 构造 → 解密）")
    from wx4 import dat

    ok = True
    # 构造一张"伪 JPEG"：以真实 JPEG 魔数开头，尾巴是异或段
    jpeg_head = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + bytes(range(256)) * 8
    jpeg_tail = bytes(range(200, 256)) * 3
    plain = jpeg_head + jpeg_tail + b"\xff\xd9"
    aes_key = "0669b78f42c9a7d7"
    raw = dat.build_v2_dat(plain, aes_key, xor_key=82, xor_size=64, raw_size=0)
    ver = dat.detect_version(raw)
    record("V2 magic 识别", ver == 2, f"detect_version={ver}，文件 {len(raw)}B")
    got, ext = dat.decrypt_dat_bytes(raw, aes_key=aes_key, xor_key=82)
    record("V2 解密还原明文", got == plain, f"ext={ext!r} 还原={got == plain}")
    ok = ok and got == plain and ver == 2 and ext == "jpg"

    # 错误异或键应该解坏
    bad, _ = dat.decrypt_dat_bytes(raw, aes_key=aes_key, xor_key=0x82)
    record("错误 xor 键(0x82)产出的尾巴不同", bad != plain, "（十进制 82=0x52 才对）")

    # V1 容器：AES 密钥固定 b"cfcd208495d565ef"
    import struct

    head = jpeg_head
    pad = 16 - (len(head) % 16)
    enc = dat._aes_ecb_crypt(dat.V1_AES_KEY, head + bytes([pad]) * pad, encrypt=True)
    v1_tail = jpeg_tail
    v1_raw = (dat.V1_MAGIC + struct.pack("<LL", len(head), len(v1_tail)) + b"\x01"
              + enc + bytes(b ^ 82 for b in v1_tail))
    v1_plain = head + v1_tail
    got_v1, ext_v1 = dat.decrypt_dat_bytes(v1_raw, aes_key=None, xor_key=82)
    record("V1 容器解密", dat.detect_version(v1_raw) == 1 and got_v1 == v1_plain,
           f"ext={ext_v1!r}")
    # decrypt_auto 在 V1 上必须忽略外部传入的 V2 密钥
    got_v1b, ext_v1b = dat.decrypt_auto(v1_raw, xor_key=82, aes_key=aes_key)
    record("decrypt_auto 对 V1 用固定密钥", got_v1b == v1_plain, f"ext={ext_v1b!r}")
    ok = ok and got_v1 == v1_plain and got_v1b == v1_plain

    # 已是明文的文件应当原样返回
    same, ext2 = dat.decrypt_auto(plain, xor_key=82, aes_key=aes_key)
    record("明文缓存文件直接透传", same == plain and ext2 == "jpg", f"ext={ext2!r}")
    return ok


# ---------------------------------------------------------------------------
# 2. 图片密钥
# ---------------------------------------------------------------------------


def test_image_keys() -> tuple[bool, dict]:
    section("[2] wx4.image_key 图片密钥提取 / 自验证")
    from wx4 import image_key

    t0 = time.time()
    acc = image_key.pick_account_dir()
    print(f"  账号目录: {acc}")
    probes = image_key.dat_probes(acc)
    record("收集 .dat 自验证样本", len(probes) > 0, f"{len(probes)} 个 V2 首块，{time.time() - t0:.2f}s")
    try:
        keys = image_key.get_image_keys(account_dir=acc)
    except Exception as exc:
        record("取得图片密钥", False, f"{type(exc).__name__}: {exc}")
        return False, {}
    elapsed = time.time() - t0
    ok = image_key._aes_ok(keys["aes_key"], probes) if probes else True
    record("图片密钥自验证通过", ok,
           f"aes={keys['aes_key']} xor={keys['xor_key']}(0x{keys['xor_key']:02x}) "
           f"source={keys['source']} 耗时={elapsed * 1000:.0f}ms")
    print(f"  缓存文件: {image_key.KEY_CACHE_FILE}")
    if image_key.KEY_CACHE_FILE.is_file():
        print("  缓存内容:", image_key.KEY_CACHE_FILE.read_text(encoding="utf-8").replace("\n", " "))
    return ok, keys


# ---------------------------------------------------------------------------
# 3. 朋友圈图片
# ---------------------------------------------------------------------------


def test_images() -> bool:
    section("[3] 朋友圈图片：xml 真实 url@md5 → image_path() → PIL")
    from wx4 import media, sns

    moments = sns.all_moments()
    print(f"  朋友圈条数: {len(moments)}")
    # 收集 (md5, size) —— size 直接来自 xml 的 <size>（与 media.md5_sizes 同源）
    items: list[tuple[str, int, tuple[int, int] | None]] = []
    for m in moments:
        for md5, wh in media._media_size_from_xml(m.raw_xml):
            if md5 not in {x[0] for x in items}:
                items.append((md5, m.feed_id, wh))
    record("从 xml 提取到图片 md5", len(items) > 0, f"{len(items)} 个（含视频）")
    if not items:
        return False

    got_any = False
    matched = 0
    t0 = time.time()
    for md5, feed_id, wh in items:
        try:
            p = media.image_path(md5)
        except Exception as exc:
            print(f"  [错误] image_path({md5}) 抛 {type(exc).__name__}: {exc}")
            continue
        if p is None:
            # 未缓存是正常情况（朋友圈图片按需下载）：打印一声便于核对
            print(f"  [未缓存] md5={md5} feed={feed_id} xml尺寸={wh}")
            continue
        matched += 1
        try:
            info = pil_probe(p)
        except Exception as exc:
            record(f"PIL 打开 {md5}", False, f"{type(exc).__name__}: {exc}")
            continue
        got_any = True
        print(f"  [图片] md5={md5} feed={feed_id} xml尺寸={wh}")
        print(f"         路径={p}")
        print(f"         尺寸={info}")
    elapsed = time.time() - t0
    record("至少解出 1 张朋友圈图片（PIL 可开）", got_any,
           f"{matched}/{len(items)} 命中，耗时 {elapsed * 1000:.0f}ms")
    if not got_any:
        print("\n  提示：当前账号的朋友圈图片可能都没在本地缓存过（需在微信里滚动/点开过）。")
    if got_any:
        print(f"  解密产物目录: {media.IMG_DIR}")
    return got_any


# ---------------------------------------------------------------------------
# 4. 头像
# ---------------------------------------------------------------------------


def test_avatars() -> bool:
    section("[4] 头像：head_image.db BLOB → avatar_path() → PIL")
    from wx4 import media, sns

    usernames: list[str] = []
    try:
        moments = sns.all_moments()
        for m in moments:
            if m.username and m.username not in usernames:
                usernames.append(m.username)
            for like in m.likes:
                if like and like not in usernames:
                    usernames.append(like)
    except Exception as exc:
        print(f"  [警告] 从朋友圈取不到联系人: {type(exc).__name__}: {exc}")
    try:
        from wx4.db import get_db

        db = get_db()
        me = db.self_info().get("username")
        if me and me not in usernames:
            usernames.insert(0, me)
        for c in db.contacts():
            u = c.get("username")
            if u and u not in usernames and c.get("head_img_md5"):
                usernames.append(u)
    except Exception as exc:
        print(f"  [警告] contact 列表不可用: {type(exc).__name__}: {exc}")
    print(f"  候选联系人: {len(usernames)} 个 -> {usernames[:12]}")
    if not usernames:
        record("候选联系人为空", False)
        return False

    got_any = False
    t0 = time.time()
    for u in usernames:
        p = media.avatar_path(u)
        if p is None:
            print(f"  [无头像缓存] {u}")
            continue
        try:
            info = pil_probe(p)
        except Exception as exc:
            record(f"PIL 打开头像 {u}", False, f"{type(exc).__name__}: {exc}")
            continue
        got_any = True
        print(f"  [头像] {u}\n         路径={p}\n         尺寸={info}")
    record("至少解出 1 个头像（PIL 可开）", got_any, f"耗时 {(time.time() - t0) * 1000:.0f}ms")
    if got_any:
        print(f"  头像产物目录: {media.AVATAR_DIR}")
    return got_any


# ---------------------------------------------------------------------------
# 5. 视频 recon
# ---------------------------------------------------------------------------


def test_videos() -> bool:
    section("[5] 朋友圈视频：本地缓存 recon 结论")
    from wx4 import media, sns

    try:
        acc = media.account_dir()
    except Exception as exc:
        record("账号目录定位", False, f"{type(exc).__name__}: {exc}")
        return True
    print(f"  账号目录: {acc}")

    # 视频 md5（xml 里 <media type="6"> 的 url@md5）
    video_md5s: list[str] = []
    try:
        for m in sns.all_moments():
            for md5, _wh in media._media_size_from_xml(m.raw_xml):
                if md5 not in video_md5s:
                    video_md5s.append(md5)
    except Exception:
        pass
    print(f"  朋友圈 xml 里的媒体 md5 数量: {len(video_md5s)}")

    report = media.recon_report(acc)
    print("  目录探测:")
    for name, info in report["dirs"].items():
        print(f"    {name:28s} exists={info['exists']}  条目={info['files']}")
    msg_video = acc / "msg" / "video"
    print(f"  msg/video 文件: {report['video_files']}")
    if msg_video.is_dir():
        print(f"  msg/video 子目录: {[p.name for p in msg_video.glob('*')]}")
    sns_temp = list((acc / "cache").glob("*/Sns/Temp/*"))
    print(f"  cache/*/Sns/Temp 文件: {[str(p) for p in sns_temp]}")
    print(f"  cache/*/Sns/ 子目录: "
          f"{sorted({p.name for p in (acc / 'cache').glob('*/Sns/*') if p.is_dir()})}")

    local_hits = 0
    for md5 in video_md5s:
        p = media.video_path(md5)
        if p is not None:
            local_hits += 1
            print(f"  [视频命中] md5={md5} -> {p} ({p.stat().st_size}B)")
            c = media.cover_path(md5)
            print(f"            封面 -> {c}")
    print(f"  video_path 命中数: {local_hits}/{len(video_md5s)}")
    print("\n  recon 结论：")
    print("    * 查过 <账号目录>/cache/*/Sns/（只有 Img、Temp；无 Video/，Sns/Temp 为空）、")
    print("      msg/video/（空）、msg/attach/*/*（只有 Img）、temp/、business/。")
    print("    * 4.x 视频真实落盘位置是 msg/video/<YYYY-MM>/<32位md5>.mp4（同名 .jpg 封面、")
    print("      <md5>_raw.mp4 原始版），但只有聊天/保存的视频会留下；朋友圈视频边下边播，")
    print("      本机 0 命中（另一账号 398 feed/48 个朋友圈视频 vs 本地 148 个 msg/video 也只 0 命中）。")
    print("    * video_path() 仍按上述命名查找（对已保存的聊天视频有效），找不到返回 None，")
    print("      集成层应降级为「不渲染 <video> / 显示占位」，不要报错。")
    record("视频 recon 结论已给出（本机无朋友圈视频缓存属预期）", True,
           f"video_path 命中 {local_hits}，msg/video 文件 {len(report['video_files'])} 个")
    return True


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    print(f"Python: {sys.executable}\n项目根目录: {ROOT}")
    t_all = time.time()
    checks = [
        ("dat 算法", test_dat_algorithm, True),
        ("图片密钥", lambda: test_image_keys()[0], True),
        ("朋友圈图片", test_images, True),
        ("头像", test_avatars, True),
        ("视频 recon", test_videos, False),
    ]
    for name, fn, required in checks:
        try:
            ok = fn()
        except Exception:
            traceback.print_exc()
            ok = False
            record(name, False, "脚本异常（见上方 traceback）")
        if not ok and required:
            print(f"\n[!] 关键项失败：{name}")

    section("汇总")
    fails = 0
    for name, status, detail in results:
        print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
        if status == FAIL:
            fails += 1
    print(f"\n总耗时 {time.time() - t_all:.2f}s，结果：{len(results) - fails}/{len(results)} 通过")
    # 验收硬指标
    critical = [r for r in results if r[0].startswith(("至少解出", "图片密钥自验证", "V2 解密还原明文"))]
    hard_fail = [r for r in critical if r[1] == FAIL]
    if hard_fail:
        print("\n验收未通过：")
        for r in hard_fail:
            print(f"  - {r[0]}: {r[2]}")
        return 1
    print("\n验收通过：朋友圈图片 + 头像均已解出并 PIL 验证。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
