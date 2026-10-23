# -*- coding: utf-8 -*-
"""wx4 数据库层自测（契约见 ``wx4/API.md`` 2.1）。

运行（统一用项目 venv）::

    WechatMoments\\.venv\\Scripts\\python.exe -X utf8 _dev\\test_db_layer.py

覆盖：
1. 自动定位账号目录 / wxid 推导；
2. 20 个库密钥全部可取（页 1 HMAC 校验通过）；
3. ``open("sns/sns.db")`` / 反斜杠 / 大小写 / ``db_storage/`` 前缀；
4. 解密缓存复用、产物为 rollback 模式且无 -wal/-shm 旁生文件；
5. ``self_info()`` / ``contacts()`` / ``contact()`` 字段与真实值；
6. 跨线程读（GUI 线程场景）；
7. 冷启动（无任何密钥缓存 + 不导入历史缓存）时现场内存扫描仍能取全 20 把；
8. 失败路径抛中文 ``Wx4Error``；
9. ``get_db()`` / ``reset()`` 单例行为。

只读微信数据；所有产物写到 ``.wx4_cache/``（或临时目录），退出码 0=全绿。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from wx4 import Wx4Error                      # noqa: E402
from wx4 import db as wxdb                    # noqa: E402
from wx4 import keycache                      # noqa: E402

FAILS: list[str] = []
CHECKS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    mark = "PASS" if cond else "FAIL"
    print("  [%s] %s%s" % (mark, name, ("：%s" % detail) if detail else ""))
    if not cond:
        FAILS.append(name if not detail else "%s（%s）" % (name, detail))


def section(title: str) -> None:
    print("\n== %s ==" % title)


def expect_wx4_error(fn, hint: str) -> None:
    try:
        fn()
    except Wx4Error as exc:
        check(hint, True, str(exc)[:80])
        return
    except Exception as exc:  # 不是 Wx4Error 也算失败
        check(hint, False, "抛了 %s 而不是 Wx4Error：%s" % (type(exc).__name__, exc))
        return
    check(hint, False, "没有抛异常")


# ---------------------------------------------------------------------------
def main() -> int:
    t_start = time.time()
    print("wx4 数据库层自测")
    print("项目根目录：%s" % ROOT)
    print("缓存目录：%s" % keycache.cache_dir())

    # ---- 1. 定位与账号 -------------------------------------------------
    section("1. 定位账号 / 密钥")
    t0 = time.time()
    db = wxdb.Wx4DB()
    report = db.key_report()
    acct_name, wxid_name = db.account, db.wxid
    print("  初始化 %.2fs，%s" % (time.time() - t0,
                                  json.dumps(report, ensure_ascii=False)))
    check("account 形如 wxid_xxx_哈希", bool(db.account), db.account)
    check("wxid 去掉哈希后缀", db.wxid == "wxid_hsqe9w1lhl1j22"
          or db.wxid in db.account, db.wxid)
    check("account_dir 是目录", db.account_dir.is_dir(), str(db.account_dir))
    check("密钥覆盖全部库文件",
          report["keys_ok"] == report["db_files"] and report["db_files"] > 0,
          "%s/%s" % (report["keys_ok"], report["db_files"]))
    check("20 个库全部可用", report["keys_ok"] == 20, str(report))
    check("加密库都在数据目录内（无路径硬编码）",
          str(db.account_dir).startswith(str(db.db_dir)), str(db.db_dir))
    by_wxid = wxdb.Wx4DB(account=db.wxid)          # 允许只传 wxid
    check("account 可传 wxid（非目录名）",
          by_wxid.account == db.account and by_wxid.wxid == db.wxid)
    by_wxid.close()
    by_path = wxdb.Wx4DB(db_dir=str(db.account_dir))  # 允许直接传账号目录
    check("db_dir 可传账号目录",
          by_path.account_dir == db.account_dir and by_path.account == db.account)
    by_path.close()
    # 主密钥派生兜底（错误主密钥必须派生不出任何库密钥，否则解密会产出坏文件）
    wrong = wxdb._refdb.derive_keys_from_master("00" * 32, db.db_files[:1])
    check("错误主密钥派生不出密钥", wrong == {})

    # ---- 2. 打开 sns.db ------------------------------------------------
    section("2. open()：sns.db")
    t0 = time.time()
    conn = db.open("sns/sns.db")
    print("  首次打开 %.3fs" % (time.time() - t0))
    check("返回 sqlite3.Connection",
          isinstance(conn, __import__("sqlite3").Connection))
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    check("SnsTimeLine 表存在", "SnsTimeLine" in tables, ",".join(sorted(tables)))
    n_sns = conn.execute("SELECT COUNT(*) FROM SnsTimeLine").fetchone()[0]
    check("SnsTimeLine 可查询且有数据", n_sns > 0, "%d 行" % n_sns)
    row = conn.execute("SELECT * FROM SnsTimeLine LIMIT 1").fetchone()
    check("row_factory 为 sqlite3.Row", row is not None and "tid" in row.keys(),
          ",".join(row.keys()) if row is not None else "空表")

    # ---- 2b. 全部库都能解密并查询 -------------------------------------
    section("2b. 全部 %d 个库可解密" % len(db.db_files))
    t0 = time.time()
    bad: list[str] = []
    for rel, _p, _s in db.db_files:
        try:
            db.open(rel).execute(
                "SELECT count(*) FROM sqlite_master").fetchone()
        except Wx4Error as exc:
            bad.append("%s：%s" % (rel, exc))
    print("  解密/查询耗时 %.2fs" % (time.time() - t0))
    check("全部库可打开（含 3 个 *_fts.db 自定义分词器库）", not bad,
          "；".join(bad[:3]))

    # ---- 3. 打开 contact.db（多种写法） --------------------------------
    section("3. open()：contact.db 各种写法")
    c1 = db.open("contact/contact.db")
    c2 = db.open("contact\\contact.db")
    c3 = db.open("CONTACT/CONTACT.DB")
    c4 = db.open("db_storage/contact/contact.db")
    check("同线程多次 open 复用同一连接", c1 is c2 is c3 is c4)
    n_contact = c1.execute("SELECT COUNT(*) FROM contact").fetchone()[0]
    check("contact 表可查询", n_contact > 0, "%d 行" % n_contact)
    t0 = time.time()
    db.open("sns/sns.db")
    check("缓存命中很快（<0.05s）", time.time() - t0 < 0.05,
          "%.4fs" % (time.time() - t0))

    # ---- 4. 缓存产物 ---------------------------------------------------
    section("4. 解密缓存产物")
    cache_dir = keycache.db_cache_dir(db.account)
    dbs = sorted(cache_dir.glob("*.sqlite"))
    check("缓存目录有解密库文件", len(dbs) >= 2, str(cache_dir))
    sidecars = [p.name for p in cache_dir.iterdir()
                if p.name.endswith(("-wal", "-shm"))]
    check("缓存目录无 -wal/-shm 旁生文件", not sidecars, ",".join(sidecars))
    sns_cache = next((p for p in dbs if p.name.startswith("sns__")), None)
    check("sns 解密文件已落缓存", sns_cache is not None)
    if sns_cache is not None:
        head = sns_cache.read_bytes()[:20]
        check("解密文件是标准 SQLite 头", head[:16] == b"SQLite format 3\x00",
              head[:16].hex())
        check("头标记为 rollback 模式（无 WAL 旗标）", head[18:20] == b"\x01\x01",
              head[18:20].hex())
    key_file = keycache.keys_path(db.account)
    check("项目内密钥缓存已写入", key_file.is_file(), str(key_file))
    if key_file.is_file():
        saved = json.loads(key_file.read_text(encoding="utf-8"))
        check("密钥缓存条目数 = 库数量", len(saved) == report["db_files"],
              "%d 条" % len(saved))

    # ---- 5. self_info / contacts / contact -----------------------------
    section("5. 联系人接口")
    me_info = db.self_info()
    print("  self_info：%s" % json.dumps(me_info, ensure_ascii=False))
    check("self_info 字段完整",
          set(me_info) == {"username", "nick_name", "remark"}, ",".join(me_info))
    check("self_info.username == wxid", me_info["username"] == db.wxid)
    check("self_info.nick_name 非空", bool(me_info["nick_name"]),
          me_info["nick_name"])

    contacts = db.contacts()
    print("  contacts：%d 个" % len(contacts))
    check("contacts 非空", len(contacts) > 0, "%d 个" % len(contacts))
    fields_ok = all(set(item) == set(wxdb.CONTACT_FIELDS) for item in contacts)
    check("每个联系人字段与契约一致", fields_ok, str(wxdb.CONTACT_FIELDS))
    types_ok = all(
        isinstance(i["username"], str) and isinstance(i["nick_name"], str)
        and isinstance(i["remark"], str) and isinstance(i["alias"], str)
        and isinstance(i["head_img_md5"], str) and isinstance(i["local_type"], int)
        for i in contacts)
    check("联系人字段类型正确", types_ok)
    me = db.contact(db.wxid)
    print("  contact(self)：%s" % json.dumps(me, ensure_ascii=False))
    check("contact(self) 能查到本人", me is not None and me["username"] == db.wxid)
    check("本人昵称=瀑布", me is not None and me["nick_name"] == "瀑布",
          me["nick_name"] if me else "")
    check("本人头像 md5 为 32 位 hex",
          me is not None and len(me["head_img_md5"]) == 32
          and all(c in "0123456789abcdef" for c in me["head_img_md5"]),
          me["head_img_md5"] if me else "")
    check("本人微信号 alias 有值", me is not None and bool(me["alias"]),
          me["alias"] if me else "")
    check("contact(不存在) 返回 None", db.contact("wxid_this_does_not_exist") is None)
    check("contacts 里含本人",
          any(i["username"] == db.wxid for i in contacts))

    # ---- 6. 跨线程读 ---------------------------------------------------
    section("6. 跨线程读（GUI 线程场景）")
    result: dict = {}

    def worker() -> None:
        try:
            wconn = db.open("sns/sns.db")
            result["rows"] = wconn.execute(
                "SELECT COUNT(*) FROM SnsTimeLine").fetchone()[0]
            result["conn"] = wconn
        except Exception as exc:  # noqa: BLE001
            result["err"] = "%s: %s" % (type(exc).__name__, exc)

    th = threading.Thread(target=worker, name="test-db-worker")
    th.start()
    th.join(timeout=30)
    check("子线程 open 成功", "err" not in result, result.get("err", ""))
    check("子线程查询结果一致", result.get("rows") == n_sns,
          "主线程 %d / 子线程 %s" % (n_sns, result.get("rows")))
    check("不同线程连接互相独立", result.get("conn") is not conn)
    time.sleep(0.05)  # 让子线程结束，验证连接池回收逻辑不炸

    # ---- 7. 失败路径 ---------------------------------------------------
    section("7. 失败路径")
    expect_wx4_error(lambda: wxdb.Wx4DB(db_dir=str(ROOT / "_no_such_dir_")),
                     "数据目录不存在 → Wx4Error")
    expect_wx4_error(lambda: db.open("nope/nope.db"), "未知库 → Wx4Error")
    expect_wx4_error(lambda: db.open("sns"), "不完整库名 → Wx4Error")
    expect_wx4_error(lambda: wxdb.Wx4DB(account="wxid_not_exist_0000"),
                     "未知账号 → Wx4Error")

    # ---- 8. 单例 -------------------------------------------------------
    section("8. get_db() / reset()")
    db.close()
    wxdb.reset()
    d1 = wxdb.get_db()
    d2 = wxdb.get_db()
    check("get_db 返回单例", d1 is d2)
    wxdb.reset()
    d3 = wxdb.get_db()
    check("reset 后是新实例", d3 is not d1)
    d3.close()
    wxdb.reset()

    # ---- 9. 冷启动：完全无缓存时现场内存扫描 ---------------------------
    section("9. 冷启动（临时缓存 + 禁用历史缓存导入）")
    tmp_cache = Path(tempfile.mkdtemp(prefix="wx4_db_cache_"))
    old_env = {k: os.environ.get(k) for k in (keycache.CACHE_DIR_ENV,
                                              keycache.LEGACY_ENV)}
    try:
        os.environ[keycache.CACHE_DIR_ENV] = str(tmp_cache)
        os.environ[keycache.LEGACY_ENV] = "0"
        t0 = time.time()
        cold = wxdb.Wx4DB()
        elapsed = time.time() - t0
        cold_report = cold.key_report()
        print("  冷启动 %.2fs，%s" % (elapsed,
                                      json.dumps(cold_report, ensure_ascii=False)))
        check("冷启动现场扫描取全 20 把密钥",
              cold_report["keys_ok"] == cold_report["db_files"] == 20,
              "%s/%s" % (cold_report["keys_ok"], cold_report["db_files"]))
        cold_conn = cold.open("contact/contact.db")
        cold_self = cold.self_info()
        check("冷启动后 contact.db 可读",
              cold_conn.execute("SELECT COUNT(*) FROM contact").fetchone()[0] > 0)
        check("冷启动后 self_info 正确", cold_self["nick_name"] == "瀑布",
              cold_self["nick_name"])
        cold.close()
        tmp_keys = tmp_cache / "keys" / (cold.account + ".json")
        check("冷启动密钥写入临时缓存", tmp_keys.is_file(), str(tmp_keys))
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        wxdb.reset()
        shutil.rmtree(tmp_cache, ignore_errors=True)

    # ---- 汇总 ----------------------------------------------------------
    print("\n" + "=" * 72)
    total = time.time() - t_start
    if FAILS:
        print("结果：%d 项检查，%d 项失败（%.2fs）" % (CHECKS, len(FAILS), total))
        for f in FAILS:
            print("  - FAIL %s" % f)
        return 1
    print("结果：全部 %d 项检查通过（%.2fs）" % (CHECKS, total))
    print("实测：账号 %s（wxid=%s），密钥 %d/%d，self_info=%s，contacts=%d 个"
          % (acct_name, wxid_name, report["keys_ok"], report["db_files"],
             json.dumps(me_info, ensure_ascii=False), len(contacts)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n中断")
        sys.exit(130)
    except Exception:  # noqa: BLE001 - 让失败原因可见
        traceback.print_exc()
        sys.exit(1)
