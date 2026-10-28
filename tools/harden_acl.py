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
    python tools/harden_acl.py --verify     # 只读复核：仍有 Authenticated Users 等
                                            # 继承/显式权限 → 非零退出（CI/发布闸门用）
    python tools/harden_acl.py --apply      # 真正收紧
    python tools/harden_acl.py --apply --verify   # 收紧后立即复核
    python tools/harden_acl.py --apply --strict   # 失败时非零退出

安全约束：

* 只处理**白名单里的路径**（不递归删任何东西；只改 ACL）；
* 任何失败都打印可复制的修复命令，默认**以 0 退出、绝不阻断**；
  （``--verify``/``--strict`` 是显式闸门，才会非零退出）
* ``--check``/``--verify`` 完全只读，不调用 icacls 的写模式；
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

#: 需要收紧的路径（相对仓库根）；不存在的会自动跳过。
#: why 这些：全部是「本机高敏感数据」——明文密钥、聊天记录、解密媒体、画像/日志。
TARGETS = (
    "config.json",       # db_keys / image_aes_key（迁移前）等
    "web_config.json",   # LLM API key（迁移前）
    "log",               # messages.jsonl / 聊天记录.txt / gateway.log
    "media",             # 解密出的图片/贴纸
    "agi/data",          # secrets.json、自学习库、草稿、画像
    "agi/data/desktop_profile",
    ".data",             # 控制台/桌面版本机状态
    "star",              # 冲浪/星标抓取产物（存在才处理）
)

#: 必须保留的主体（SID，与系统语言无关）
SYSTEM_SID = "S-1-5-18"
ADMINS_SID = "S-1-5-32-544"

#: 可信主体的名字片段（小写匹配）。icacls 默认显示账户名而不是 SID，
#: 所以 --verify 需要按名字识别「谁能访问」，SID 只用于 grant。
TRUSTED_NAME_PARTS = (
    "administrator",   # 覆盖 Administrators / 当前管理员账户
    "system",          # NT AUTHORITY\\SYSTEM
)
#: 不可信主体（本机任何登录用户/匿名/交互式会话）。出现即算未加固。
UNTRUSTED_NAME_PARTS = (
    "authenticated users",
    "\\users",
    "everyone",
    "interactive",
    "anonymous",
    "\\local",
    "terminal server",
)
#: 已知「本机其他用户/匿名」SID。为什么需要：中文系统/某些环境里 icacls
#: 会把 Authenticated Users 显示成裸 SID（实测出现 `S-1-4-...:(I)(W,D,DC)`），
#: 只按名字匹配会漏判 —— 而漏判正是「verify 绿了但其实没收紧」。
UNTRUSTED_SIDS = frozenset({
    "S-1-5-11",       # NT AUTHORITY\\Authenticated Users
    "S-1-5-32-545",   # BUILTIN\\Users
    "S-1-5-32-546",   # BUILTIN\\Guests
    "S-1-1-0",        # Everyone
    "S-1-5-4",        # NT AUTHORITY\\INTERACTIVE
    "S-1-5-7",        # NT AUTHORITY\\ANONYMOUS LOGON
    "S-1-2-0",        # NT AUTHORITY\\LOCAL
})
#: 写/删除类权限 token（icacls 的权限位是逗号分隔的字母：W/M/F/D/DC…）
WRITE_TOKENS = frozenset({"W", "M", "F", "D", "DC", "WDAC"})
#: 裸 SID 形式（不限制在 S-1-5，S-1-4 之类的也要能识别）
_ANY_SID_RE = re.compile(r"^S-1-\d+(?:-\d+)+$")

_SID_RE = re.compile(r"S-1-5-\d+(?:-\d+)+")
#: icacls 的一行：`主体名:(权限)…`，权限是若干括号组
_ACL_ENTRY_RE = re.compile(r"([^\s:][^:]*?):((?:\([A-Z(),I ]*\))+)")


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


#: whoami 结果缓存（一次进程只查一次；失败也缓存空串避免反复起子进程）
_USER_NAME_CACHE: dict[str, str] = {}


def current_user_name() -> str:
    """当前用户名（形如 ``DOMAIN\\user``）；拿不到返回空串。

    why 需要：icacls 默认显示账户名而不是 SID。若只认 SYSTEM/Administrators 片段，
    普通用户名（如 ``bob``）会被判成 unknown 从而让 --verify 误报。这里把
    「当前用户自己」也算可信主体（它本来就是授予对象）。
    """
    if "name" in _USER_NAME_CACHE:
        return _USER_NAME_CACHE["name"]
    name = ""
    try:
        out = subprocess.run(["whoami"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=15)
        name = (out.stdout or "").strip()
    except Exception:  # noqa: BLE001
        name = ""
    _USER_NAME_CACHE["name"] = name
    return name


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


def _principal_kind(principal: str, allowed: set[str] | None = None,
                    user_name: str | None = None) -> str:
    """把 icacls 里的主体分成 trusted / untrusted / unknown。

    * trusted：当前用户 / SYSTEM / Administrators（按名字片段、当前用户名或白名单 SID）；
    * untrusted：Authenticated Users、Users、Everyone、INTERACTIVE 等；
    * unknown：既不是白名单也不是已知不可信（可能是历史显式 ACE、域账号、
      沙箱 SID）。**带写权限的 unknown 也算风险**——「只保留 3 个主体」失败。
    """
    text = str(principal or "").strip()
    allowed_set = {item.upper() for item in (allowed if allowed is not None
                                             else allowed_sids())}
    if _ANY_SID_RE.match(text):
        normalized = text.upper()
        if normalized in allowed_set:
            return "trusted"
        if normalized in UNTRUSTED_SIDS:
            return "untrusted"
        return "unknown"
    lowered = text.lower()
    # 注意顺序：`Administrator` 里含 `users` 子串，先判可信再判不可信。
    if any(part in lowered for part in TRUSTED_NAME_PARTS):
        return "trusted"
    me = (current_user_name() if user_name is None else user_name).lower()
    if me and me.split("\\")[-1] == lowered.split("\\")[-1]:
        return "trusted"
    if any(part in lowered for part in UNTRUSTED_NAME_PARTS):
        return "untrusted"
    return "unknown"


def _flags_have_write(flags: str) -> bool:
    """权限位里是否含写/删（W/M/F/D/DC…）。``(I)(W,D,DC)`` → True。"""
    tokens = {token.strip().upper() for token in (flags or "").replace("(", "").replace(")", "").split(",")}
    return bool(tokens & WRITE_TOKENS)


def _is_untrusted(principal: str) -> bool:
    """该主体是否属于「本机其他登录用户/匿名」——出现即视为未加固。"""
    return _principal_kind(principal) == "untrusted"


def parse_entries(text: str, path: Path | None = None) -> list[dict]:
    """把 icacls 输出解析成 [{"principal","flags","inherited"}]。

    icacls 的权限是若干括号组：``(OI)(CI)(F)``、``(I)(M)``、``(R)`` 等；
    中文系统只有提示语被翻译，权限字母仍是英文，所以可以直接判 ``(I)``。

    ``path``：icacls 会把「被检查的路径」放在每行最前面（如
    ``C:\\\\data\\\\config.json BUILTIN\\\\Administrators:(F)``）。不先切掉它，
    路径里的 ``\\\\Users\\\\`` 会被误判成不可信主体。
    """
    entries: list[dict] = []
    prefix = f"{path} " if path is not None else ""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if prefix and line.lower().startswith(prefix.lower()):
            line = line[len(prefix):].strip()
        match = _ACL_ENTRY_RE.search(line)
        if not match:
            continue
        principal = match.group(1).strip()
        if not principal:
            continue
        flags = match.group(2) or ""
        entries.append({"principal": principal, "flags": flags,
                        "inherited": "(I)" in flags})
    return entries


def parse_icacls(text: str, path: Path | None = None) -> dict:
    """从 icacls 输出里粗略判断：是否有继承、出现了哪些主体。"""
    entries = parse_entries(text, path)
    inherited = any(entry["inherited"] for entry in entries)
    principals = [entry["principal"] for entry in entries]
    return {"inherited": inherited, "principals": principals, "entries": entries}


def untrusted_entries(text: str, path: Path | None = None) -> list[dict]:
    """不可信主体（Authenticated Users 等）的权限条目。

    为什么单独拎出来：--apply 之后理论上不该再有这些主体；只要还出现
    （无论继承还是显式授予），就说明「本机任何登录用户能读/写聊天记录」，
    发布闸门必须非零退出。
    """
    allowed = set(allowed_sids())
    return [entry for entry in parse_entries(text, path)
            if _principal_kind(entry["principal"], allowed) == "untrusted"]


def unknown_write_entries(text: str, path: Path | None = None) -> list[dict]:
    """既不在白名单、也不是已知不可信，但**有写权限**的主体条目。

    为什么也当风险：加固目标是「只保留 当前用户/SYSTEM/Administrators」。
    历史显式 ACE、沙箱/域账号等 unknown 主体如果还能写本机密钥目录，
    等于没达到目标；但它们不是「本机任何登录用户」，所以单独归类、单独提示。
    """
    allowed = set(allowed_sids())
    risky = []
    for entry in parse_entries(text, path):
        if _principal_kind(entry["principal"], allowed) != "unknown":
            continue
        if _flags_have_write(entry["flags"]):
            risky.append(entry)
    return risky


def check_target(path: Path) -> dict:
    code, text = run_icacls(icacls_check_args(path))
    info = parse_icacls(text if code == 0 else "", path)
    info.update({"path": str(path), "ok": code == 0, "raw": text[:400]})
    info["untrusted"] = untrusted_entries(text if code == 0 else "", path)
    info["unknown_write"] = unknown_write_entries(text if code == 0 else "", path)
    info["hardened"] = bool(code == 0 and not info["inherited"]
                            and not info["untrusted"] and not info["unknown_write"])
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


def verify_targets(*, quiet: bool = False) -> list[dict]:
    """只读复核：不可信主体仍能访问 → 记为问题（供 --verify 非零退出）。

    判定口径（写清楚，避免「verify 绿了但其实没查」）：
    * ``icacls`` 本身执行失败 → 问题（无法证明安全）；
    * 出现 ``Authenticated Users`` / ``Users`` / ``Everyone`` / ``INTERACTIVE``
      等不可信主体 → 问题（无论继承还是显式授予）；
    * 仍有继承权限但主体只有当前用户/SYSTEM/Administrators → 只告警不失败
      （没有「其他用户可访问」的事实，但说明 /inheritance:r 没生效）。
    """
    problems: list[dict] = []
    for path in _targets():
        if not path.exists():
            continue
        rel = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
        info = check_target(path)
        if not info["ok"]:
            problems.append({"path": str(path), "reason": "icacls 执行失败"})
            if not quiet:
                print(f"  [FAIL] {rel}：icacls 执行失败（无法证明 ACL 安全）")
            continue
        if info["untrusted"]:
            names = ", ".join(sorted({item["principal"] for item in info["untrusted"]}))
            problems.append({"path": str(path), "reason": f"不可信主体：{names}"})
            if not quiet:
                print(f"  [FAIL] {rel}：仍有本机其他用户可访问 → {names}")
                print("         修复：python tools/harden_acl.py --apply")
            continue
        if info["unknown_write"]:
            names = ", ".join(sorted({item["principal"] for item in info["unknown_write"]}))
            problems.append({"path": str(path), "reason": f"未知主体可写：{names}"})
            if not quiet:
                print(f"  [FAIL] {rel}：存在白名单之外但可写的主体 → {names}")
                print("         --apply 只切断继承，不删除历史显式 ACE；请人工复核后执行：")
                print(f"         icacls \"{path}\" /remove:g \"{names}\"")
            continue
        if info["inherited"]:
            if not quiet:
                print(f"  [WARN] {rel}：仍存在继承权限（主体可信，但继承未切断）")
            continue
        if not quiet:
            print(f"  [PASS] {rel}")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python tools/harden_acl.py")
    parser.add_argument("--apply", action="store_true", help="真正修改 ACL（默认只检查）")
    parser.add_argument("--check", action="store_true", help="只读盘点（默认行为）")
    parser.add_argument("--verify", action="store_true",
                        help="只读复核；仍有 Authenticated Users 等不可信主体 → 非零退出")
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
        return 0 if not (args.strict or args.verify) else 1

    if not args.quiet:
        print(f"当前用户 SID：{me}")
        _print_plan()

    # --verify 单独用时是纯只读闸门（不改 ACL），适用于 CI/发布前检查。
    if args.verify and not args.apply:
        if not args.quiet:
            print("\n只读复核（不修改 ACL）：")
        problems = verify_targets(quiet=args.quiet)
        if problems:
            print(f"\n[FAIL] {len(problems)} 个敏感路径仍可被本机其他用户访问；"
                  "在管理员终端执行 python tools/harden_acl.py --apply")
            return 1
        print("\n[PASS] 敏感路径 ACL 已收紧（无 Authenticated Users 等不可信主体）。")
        return 0

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
        if args.verify or args.strict:
            return 1
        return 0
    if args.verify:
        problems = verify_targets(quiet=args.quiet)
        if problems:
            print(f"\n[FAIL] {len(problems)} 个敏感路径仍可被本机其他用户访问。")
            return 1
        print("\n[PASS] 敏感路径 ACL 已收紧（无 Authenticated Users 等不可信主体）。")
        return 0
    if not args.apply:
        print("\n只读盘点完成（未改动任何 ACL）；确认后执行：python tools/harden_acl.py --apply")
        return 0
    print("\n全部完成：敏感路径已切断继承，仅当前用户/SYSTEM/Administrators 可访问。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
