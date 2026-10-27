# -*- coding: utf-8 -*-
"""收紧敏感目录/文件的 Windows ACL（M0 遗留项）。

为什么需要：Windows 上 ``chmod 600`` 是空操作。默认情况下 ``config.json``
（含微信数据库密钥 ``db_keys`` / 图片密钥）、``web_config.json``（LLM API key）、
``log/``（全部聊天记录）、``media/``（解密出的原图）、``agi/data/``（自学习库、
网关 token、草稿、gateway.log）都会继承父目录的
``NT AUTHORITY\\Authenticated Users: Modify`` —— 本机任何登录用户都能读走。

做法：对每个目标切断继承（``/inheritance:r``），只保留：

* 当前用户（完全控制）
* ``SYSTEM``（S-1-5-18）
* ``Administrators``（S-1-5-32-544）

用法::

    python tools/harden_acl.py --check      # 只读盘点（默认）
    python tools/harden_acl.py --apply      # 真正收紧
    python tools/harden_acl.py --apply --strict   # 失败时非零退出

安全约束：

* 只处理**白名单里的路径**（不递归删任何东西；只改 ACL）；
* 任何失败都打印可复制的修复命令，默认**以 0 退出、绝不阻断**；
* ``--check`` 完全只读，不调用 icacls 的写模式；
* 非 Windows 平台直接跳过（exit 0）。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 需要收紧的路径（相对仓库根）；不存在的会自动跳过
TARGETS = (
    "config.json",
    "web_config.json",
    "log",
    "media",
    "agi/data",
)

#: 必须保留的主体（SID，与系统语言无关）
SYSTEM_SID = "S-1-5-18"
ADMINS_SID = "S-1-5-32-544"

_SID_RE = re.compile(r"S-1-5-\d+(?:-\d+)+")


def is_windows() -> bool:
    return os.name == "nt"


def current_user_sid() -> str | None:
    """当前用户 SID（``whoami /user``）；拿不到返回 None。"""
    try:
        out = subprocess.run(["whoami", "/user"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=15)
    except Exception:  # noqa: BLE001
        return None
    match = _SID_RE.search(out.stdout or "")
    return match.group(0) if match else None


def allowed_sids() -> list[str]:
    sids = [SYSTEM_SID, ADMINS_SID]
    me = current_user_sid()
    if me:
        sids.insert(0, me)
    return sids


def _is_dir(path: Path) -> bool:
    return path.is_dir()


def icacls_apply_args(path: Path) -> list[str]:
    """构造 icacls 收紧命令（数组参数，绝不拼 shell 字符串）。"""
    inherit = "(OI)(CI)F" if _is_dir(path) else "F"
    args = ["icacls", str(path), "/inheritance:r"]
    for sid in allowed_sids():
        args.extend(["/grant:r", f"*{sid}:{inherit}"])
    return args


def icacls_check_args(path: Path) -> list[str]:
    return ["icacls", str(path)]


def run_icacls(args: list[str], *, timeout: float = 60.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
        return int(proc.returncode), ((proc.stdout or "") + (proc.stderr or "")).strip()
    except Exception as exc:  # noqa: BLE001
        return 1, f"{type(exc).__name__}: {exc}"


def parse_icacls(text: str) -> dict:
    """从 icacls 输出里粗略判断：是否有继承、出现了哪些主体。"""
    inherited = False
    principals: list[str] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # 形如：「BUILTIN\\Administrators:(OI)(CI)(F)」或「... :(I)(F)」
        match = re.search(r"([^\s:][^:]*?):(\([A-Z(),I ]*\))+", line)
        if match:
            principal = match.group(1).strip()
            if principal:
                principals.append(principal)
        if "(I)" in line:
            inherited = True
    return {"inherited": inherited, "principals": principals}


def check_target(path: Path) -> dict:
    code, text = run_icacls(icacls_check_args(path))
    info = parse_icacls(text if code == 0 else "")
    info.update({"path": str(path), "ok": code == 0, "raw": text[:400]})
    info["hardened"] = bool(code == 0 and not info["inherited"])
    return info


def apply_target(path: Path) -> dict:
    code, text = run_icacls(icacls_apply_args(path))
    return {"path": str(path), "ok": code == 0, "output": text[:400],
            "command": " ".join(icacls_apply_args(path))}


def _targets() -> list[Path]:
    return [ROOT / rel for rel in TARGETS]


def _print_plan() -> None:
    print("将要收紧（切断继承，只保留当前用户 / SYSTEM / Administrators）：")
    for path in _targets():
        status = "存在" if path.exists() else "不存在（跳过）"
        kind = "目录" if path.is_dir() else "文件"
        print(f"  - {path.relative_to(ROOT)}  [{kind}, {status}]")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python tools/harden_acl.py")
    parser.add_argument("--apply", action="store_true", help="真正修改 ACL（默认只检查）")
    parser.add_argument("--check", action="store_true", help="只读盘点（默认行为）")
    parser.add_argument("--strict", action="store_true", help="失败时返回非零退出码")
    parser.add_argument("--quiet", action="store_true", help="只打结论")
    args = parser.parse_args(argv)

    if not is_windows():
        print("非 Windows 平台：跳过（POSIX 上用 chmod 即可）。")
        return 0

    me = current_user_sid()
    if not me:
        print("拿不到当前用户 SID（whoami 失败）；不改 ACL。")
        if not args.quiet:
            _print_plan()
        return 0 if not args.strict else 1

    if not args.quiet:
        print(f"当前用户 SID：{me}")
        _print_plan()

    failures: list[dict] = []
    if args.apply:
        for path in _targets():
            if not path.exists():
                continue
            result = apply_target(path)
            if not result["ok"]:
                failures.append(result)
            if not args.quiet:
                print(f"  {'OK ' if result['ok'] else 'FAIL'} {path.relative_to(ROOT)}")
                if not result["ok"]:
                    print(f"       可手动执行：{result['command']}")
        if not args.quiet:
            print("\n收紧后复核：")
    # 无论 check 还是 apply，最后都做一次复核
    for path in _targets():
        if not path.exists():
            continue
        info = check_target(path)
        if not args.quiet:
            state = "已收紧" if info["hardened"] else "仍有继承/未生效"
            print(f"  [{state}] {path.relative_to(ROOT)}")
        if not info["hardened"] and args.apply:
            failures.append({"path": str(path), "ok": False,
                             "command": " ".join(icacls_apply_args(path))})

    if failures:
        print(f"\n有 {len(failures)} 个路径没能收紧（ACL 可能需要提权）。"
              "在管理员终端重跑 --apply，或手动执行上面的命令。")
        return 1 if args.strict else 0
    if not args.apply:
        print("\n只读盘点完成（未改动任何 ACL）；确认后执行：python tools/harden_acl.py --apply")
        return 0
    print("\n全部完成：敏感路径已切断继承，仅当前用户/SYSTEM/Administrators 可访问。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
