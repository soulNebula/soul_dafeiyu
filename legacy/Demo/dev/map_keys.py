"""Recon：为每个数据库匹配自己的 WCDB 密钥（Config.Cipher 候选池 × 逐库 HMAC 校验），并解密全部库。"""
from __future__ import annotations

import ctypes
import io
import json
import sqlite3
import struct
import sys
from pathlib import Path

try:
    sys.stdout = io.TextIOWrapper(sys.__stdout__.buffer, encoding="utf-8", errors="replace")
except Exception:
    pass
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
import try_db_key as K  # noqa: E402
import try_db_key5 as M  # noqa: E402
import decrypt_dbs as D  # noqa: E402

ACC = Path.home() / "Documents" / "xwechat_files" / "wxid_hsqe9w1lhl1j22_5663"
OUT = Path("recon/decrypted")


def collect_candidates(pid: int) -> set[bytes]:
    h = M.k32.OpenProcess(0x0010 | 0x0400, False, pid)
    if not h:
        return set()

    def read(addr, n):
        buf = ctypes.create_string_buffer(n)
        br = ctypes.c_size_t(0)
        if M.k32.ReadProcessMemory(ctypes.c_void_p(h), ctypes.c_void_p(addr), buf, n, ctypes.byref(br)) and br.value:
            return buf.raw[: br.value]
        return None

    try:
        M.regions_read = M.regions(pid)
        needles = M.find_bytes(read, M.CONFIG_CIPHER_NAME)
        print("needles:", [hex(a) for a in needles])
        cands: set[bytes] = set()
        pairs = [struct.pack("<Q", a) + struct.pack("<Q", len(M.CONFIG_CIPHER_NAME)) for a in needles]
        for pair in pairs:
            for qaddr in M.find_bytes(read, pair):
                node = M.read_mem(read, qaddr - 0x10, 0x50)
                if node:
                    cfg = struct.unpack_from("<Q", node, 0x28)[0]
                    if M.plausible_config_ptr(cfg):
                        cands |= M.parse_obj_candidates(read, cfg)
                # 自适应
                for delta in range(-0x40, 0x60, 8):
                    raw = M.read_mem(read, qaddr + delta, 0x40)
                    if not raw:
                        continue
                    for off in range(0, 0x38, 8):
                        v = struct.unpack_from("<Q", raw, off)[0]
                        if M.plausible_config_ptr(v):
                            cands |= M.parse_obj_candidates(read, v)
        return cands
    finally:
        M.k32.CloseHandle(h)


def match_db(page1: bytes, cands: set[bytes]):
    for cand in cands:
        if len(cand) == 32:
            if D.derive_key_salt(page1, cand, b"")[0] is not None if False else False:
                pass
            ek, salt, pth = D.derive_key_salt(page1, cand, b"\x00" * 16)
            if ek is not None:
                return cand, ek, salt, pth
        elif len(cand) == 48:
            k32, s16 = cand[:32], cand[32:]
            ek, salt, pth = D.derive_key_salt(page1, k32, s16)
            if ek is not None:
                return cand, ek, salt, pth
    return None


def main() -> int:
    db_files = [p for p in sorted(ACC.glob("db_storage/*/*.db")) if p.is_file()]
    print("db 数:", len(db_files))
    all_cands: set[bytes] = set()
    for pid in K.weixin_pids()[:1]:
        print("扫 PID", pid)
        all_cands |= collect_candidates(pid)
    print("候选池:", len(all_cands), "（32B/48B 混）")
    Path("recon/key_cands.json").write_text(
        json.dumps(sorted(c.hex() for c in all_cands)), encoding="utf-8")

    OUT.mkdir(parents=True, exist_ok=True)
    mapping = {}
    for db in db_files:
        page1 = db.read_bytes()[:M.PAGE_SZ]
        hit = match_db(page1, all_cands)
        if not hit:
            print(f"  {db.relative_to(ACC)}: 未匹配")
            continue
        cand, ek, salt, pth = hit
        dst = OUT / db.name
        ok = D.decrypt_db(db, dst, ek, salt, pth)
        mapping[str(db.relative_to(ACC))] = {"cand": cand.hex(), "layout": "plaintext" if pth else "standard", "ok": ok}
        print(f"  {db.relative_to(ACC)}: layout={'plaintext' if pth else 'standard'} ok={ok}")
        if ok:
            try:
                con = sqlite3.connect(str(dst))
                tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
                con.close()
                print(f"    表: {tables[:10]}{' ...' if len(tables) > 10 else ''}")
            except Exception as e:
                print("    sqlite 打开失败:", e)
    Path("recon/db_key_map.json").write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")
    print("映射已存 recon/db_key_map.json，解出", sum(1 for v in mapping.values() if v["ok"]), "个库")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
