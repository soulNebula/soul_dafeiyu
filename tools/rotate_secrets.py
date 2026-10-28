#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把明文密钥从 ``config.json`` / ``web_config.json`` 迁到 ``agi/data/secrets.json``。

为什么需要
----------
这两个 JSON 承担的是「业务配置」角色：网页端会保存它、用户会打包/截图/备份它。
把 ``llm.api_key``（DeepSeek，可被刷额度）和 ``db_keys``/``image_aes_key``
（可解密整台机器的微信聊天与图片）混在里面，泄漏面远大于必要。
迁移后：

* 密钥只在 ``agi/data/secrets.json``（已 gitignore + ACL 收紧）和环境变量里；
* ``config.json`` / ``web_config.json`` 里只剩业务配置（不再有可用的凭证）；
* ``agi.secrets_store`` 是唯一读取入口，``wx_core/config.py``、``webui/llm.py``、
  ``wx_core/web_search.py``、``agi/selflearn/llm.py`` 都已支持。

**默认行为：只复制 + 收紧 ACL，不改旧文件。** 清空旧字段必须显式
``--purge-plaintext``，因为生产进程正在跑：把密钥清掉而进程没重启，等于让
正在运行的服务在下次读配置时突然拿不到 key（DB 直读/网页端会直接失败）。

用法::

    python tools/rotate_secrets.py                     # = --dry-run，打印计划，不改文件
    python tools/rotate_secrets.py --apply             # 复制到 secrets.json（旧文件保持原样）
    python tools/rotate_secrets.py --apply --purge-plaintext
                                                       # 复制 + 校验 + 清空旧字段
    python tools/rotate_secrets.py --json              # 机器可读（同样默认 dry-run）

退出码::

    0  成功 / 无需迁移（幂等）
    1  写入失败（磁盘/权限问题；此时**不会**清空旧字段）
    2  参数用法错误（如只给 --purge-plaintext 不给 --apply）
    3  迁移已写盘，但有字段因「密钥库里没有确认过的副本」而拒绝清空（人工处理）

安全约束：本工具只读/写白名单文件；不联网；输出里不打印任何密钥值（只打长度和
SHA-256 前 12 位指纹，用于人工核对「搬的就是这一把」）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# why：脚本方式运行时 sys.path[0] 是 tools/，这里补仓库根，保证能 import agi.*;
# 不影响被 pytest 以 tools.rotate_secrets 方式导入。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 根 config.json 里会被迁移的敏感字段（与 wx_core/config.py::SECRET_KEYS 一致）
WX_SECRET_FIELDS = (
    "db_keys",             # WCDB 数据库密钥（当前账号）
    "db_keys_by_account",  # 按账号缓存的数据库密钥
    "image_aes_key",       # 图片 .dat 的 AES 密钥
    "image_xor_key",       # 图片 .dat 的单字节异或密钥
    "image_keys_by_account",
    "image_cfg_dword",     # 表情包密钥派生 seed
)
#: web_config.json 里会被迁移的敏感字段（嵌套在 llm 段）
LLM_SECRET_FIELDS = ("api_key",)

SECRETS_VERSION = 1


def _utf8_stdout() -> None:
    """Windows 控制台/CI 重定向默认 cp936，统一 UTF-8，避免中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (dict, list, tuple)):
        return not value
    return False


def fingerprint(value: Any) -> str:
    """值指纹（只是为了核对搬家是否一致；不可逆，也不泄露值本身）。"""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _read_json(path: Path) -> dict[str, Any] | None:
    """读 JSON；不存在/损坏返回 None（调用方按「没有这个文件」处理）。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _atomic_write_json(path: Path, data: dict[str, Any]) -> bool:
    """原子写 JSON + 尽力 0600；失败返回 False（绝不半写）。"""
    import tempfile

    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    descriptor = -1
    tmp_name = ""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass
        os.replace(tmp_name, path)
        return True
    except (OSError, ValueError, TypeError):
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if tmp_name:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass
        return False


def _tighten(path: Path) -> str:
    """收紧单文件 ACL（Windows 用 icacls；其它平台靠 chmod 600）。"""
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
            return "ok(chmod 600)"
        except OSError as exc:
            return f"failed({type(exc).__name__})"
    try:
        from tools import harden_acl  # 复用同一套 SID/继承策略
    except Exception:  # noqa: BLE001
        return "skipped(harden_acl 不可用)"
    try:
        result = harden_acl.apply_target(path)
    except Exception as exc:  # noqa: BLE001
        return f"failed({type(exc).__name__})"
    return "ok(icacls)" if result.get("ok") else "failed(icacls)"


# ---------------------------------------------------------------- 计划收集
def _secret_entries(root: Path) -> list[dict[str, Any]]:
    """待迁移条目：只含**非空**值，并保留值本身（不打印）。"""
    entries: list[dict[str, Any]] = []
    web_path = root / "web_config.json"
    cfg_path = root / "config.json"

    web = _read_json(web_path)
    llm = (web or {}).get("llm")
    if isinstance(llm, dict):
        for field in LLM_SECRET_FIELDS:
            value = llm.get(field)
            if not _is_empty(value):
                entries.append({"source": "web_config.json", "section": "llm",
                                "field": field, "value": value})

    cfg = _read_json(cfg_path)
    if isinstance(cfg, dict):
        for field in WX_SECRET_FIELDS:
            value = cfg.get(field)
            if not _is_empty(value):
                entries.append({"source": "config.json", "section": "config",
                                "field": field, "value": value})
    return entries


def _existing_secrets(path: Path) -> dict[str, Any]:
    data = _read_json(path)
    return data if isinstance(data, dict) else {}


def _build_payload(existing: dict[str, Any], entries: list[dict[str, Any]]) -> tuple[dict, list]:
    """把条目并入已有密钥库；返回 (新 payload, 变更列表)。"""
    payload = dict(existing)
    payload.setdefault("version", SECRETS_VERSION)
    changes: list[str] = []
    for entry in entries:
        section = entry["section"]
        bucket = payload.get(section)
        bucket = dict(bucket) if isinstance(bucket, dict) else {}
        if bucket.get(entry["field"]) != entry["value"]:
            changes.append(f"{entry['source']}:{section}.{entry['field']}")
        bucket[entry["field"]] = entry["value"]
        payload[section] = bucket
    if changes:
        payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    return payload, changes


def _confirmed(payload: dict[str, Any], entry: dict[str, Any]) -> bool:
    """密钥库里是否存有该条目的**同一个值**（清空旧字段的前提）。"""
    section = payload.get(entry["section"])
    if not isinstance(section, dict):
        return False
    return section.get(entry["field"]) == entry["value"]


# ---------------------------------------------------------------- 清空旧字段
def _purge_web_config(root: Path, entries: list[dict[str, Any]],
                      payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    """把 web_config.json 的 llm.api_key 置空；返回 (done, refused)。"""
    done: list[str] = []
    refused: list[str] = []
    path = root / "web_config.json"
    data = _read_json(path)
    if data is None:
        return done, refused
    llm = data.get("llm")
    if not isinstance(llm, dict):
        return done, refused
    changed = False
    for entry in entries:
        if entry["source"] != "web_config.json":
            continue
        field = entry["field"]
        if not _confirmed(payload, entry):
            refused.append(f"web_config.json:llm.{field}")
            continue
        if not _is_empty(llm.get(field)):
            llm[field] = ""
            changed = True
            done.append(f"web_config.json:llm.{field}")
    if changed and not _atomic_write_json(path, data):
        raise OSError("写回 web_config.json 失败")
    return done, refused


def _purge_root_config(root: Path, entries: list[dict[str, Any]],
                       payload: dict[str, Any]) -> tuple[list[str], list[str]]:
    """把根 config.json 的敏感字段置空（dict→{}，其余→""）。"""
    done: list[str] = []
    refused: list[str] = []
    path = root / "config.json"
    data = _read_json(path)
    if data is None:
        return done, refused
    changed = False
    for entry in entries:
        if entry["source"] != "config.json":
            continue
        field = entry["field"]
        if not _confirmed(payload, entry):
            refused.append(f"config.json:{field}")
            continue
        current = data.get(field)
        if isinstance(current, dict):
            replacement: Any = {}
        elif isinstance(current, bool):
            replacement = current  # 布尔字段不在这里；防御性保底
        else:
            replacement = ""
        if current != replacement:
            data[field] = replacement
            changed = True
            done.append(f"config.json:{field}")
    if changed and not _atomic_write_json(path, data):
        raise OSError("写回 config.json 失败")
    return done, refused


# ---------------------------------------------------------------- 输出
def _relative(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _print_plan(root: Path, secrets_path: Path, entries: list[dict[str, Any]],
                payload: dict[str, Any], changes: list[str], exists: bool) -> None:
    print("[DRY-RUN] 密钥迁移计划（本次不会修改任何文件）")
    print(f"  仓库根：{root}")
    print(f"  密钥库：{_relative(root, secrets_path)}"
          f"（{'已存在，将合并' if exists else '不存在，--apply 时创建'}）")
    source_files = sorted({entry["source"] for entry in entries})
    if not entries:
        print("  未发现需要迁移的明文密钥（可能已迁移过，或本来就没有配置）。")
    else:
        print(f"  将迁移 {len(entries)} 项（只打印长度与指纹，不打印值）：")
        for entry in entries:
            value = entry["value"]
            length = len(value) if isinstance(value, (str, dict, list)) else len(str(value))
            print(f"    - {entry['source']}:{entry['section']}.{entry['field']}"
                  f"  长度 {length}  指纹 {fingerprint(value)}")
        print(f"  涉及文件：{', '.join(source_files)}")
        if changes:
            print(f"  密钥库需要更新 {len(changes)} 项：")
            for item in changes:
                print(f"    * {item}")
        else:
            print("  密钥库已是最新（幂等：--apply 不会重复写入）。")
    print("  注意：--dry-run 不会改动 config.json / web_config.json（默认也不改）。")
    print("  下一步：")
    if entries or not exists:
        print("    python tools/rotate_secrets.py --apply    # 复制到密钥库并收紧 ACL")
    else:
        print("    密钥已迁移；如需清空旧文件里的明文：")
        print("    python tools/rotate_secrets.py --apply --purge-plaintext")
    print("  清空旧字段（--purge-plaintext）会让**正在运行的进程**在下次读配置时拿不到"
          "旧 key；生产迁移请按文档顺序：停服务 → 迁移 → 设 DEEPSEEK_API_KEY → 重启。")


def _print_rollback(root: Path, secrets_path: Path) -> None:
    rel = _relative(root, secrets_path)
    print("  回滚方式（把密钥搬回明文文件）：")
    print(f"    1) 从 {rel} 的 llm.api_key 手工填回 web_config.json 的 llm.api_key；")
    print(f"    2) 从 {rel} 的 config.* 填回 config.json 同名字段；")
    print("    3) 或者直接重启程序：db_keys / 图片密钥会在首次运行时从微信内存重新提取。")
    print("    注意：回滚会把明文 key 重新写回可被复制/提交的文件，只在排障时短时使用。")


def _print_next_steps() -> None:
    print("  生产迁移最小操作序列：")
    print("    1) 停掉网关/网页端（避免旧进程继续读被清空的配置）；")
    print("    2) python tools/rotate_secrets.py --apply            # 先复制，不动旧文件")
    print("    3) setx DEEPSEEK_API_KEY \"<你的 key>\"                 # 新环境变量（重启终端生效）")
    print("    4) python tools/rotate_secrets.py --apply --purge-plaintext   # 校验后清空旧字段")
    print("    5) python tools/harden_acl.py --apply                 # 收紧目录/文件 ACL")
    print("    6) 启动服务，确认聊天/搜索/DB 直读正常；")
    print("    7) python tools/check_secrets.py                     # 确认 git 跟踪文件里没有密钥")


def _print_summary(payload: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    fields = []
    for entry in entries:
        fields.append(f"{entry['section']}.{entry['field']}")
    print(f"  密钥库字段：{', '.join(sorted(set(fields))) if fields else '（无）'}")


# ---------------------------------------------------------------- 主流程
def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    parser = argparse.ArgumentParser(
        prog="python tools/rotate_secrets.py",
        description="把 config.json / web_config.json 的明文密钥迁到 agi/data/secrets.json"
                    "（默认 dry-run；清空旧字段需 --purge-plaintext）")
    parser.add_argument("--root", default=str(ROOT), help="仓库根（默认本脚本上一级）")
    parser.add_argument("--secrets", default=None,
                        help="密钥库路径（默认读 SOULAGENT_SECRETS 或 agi/data/secrets.json）")
    parser.add_argument("--apply", action="store_true", help="真正写入密钥库（默认 dry-run）")
    parser.add_argument("--dry-run", action="store_true",
                        help="显式声明只预演（默认行为；与 --apply 互斥）")
    parser.add_argument("--purge-plaintext", action="store_true",
                        help="确认密钥库已有副本后，清空旧文件里的敏感字段（破坏性）")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args(argv)

    if args.purge_plaintext and not args.apply:
        print("[FAIL] --purge-plaintext 必须和 --apply 一起用（先复制，再清空）。",
              file=sys.stderr)
        return 2
    if args.dry_run and args.apply:
        print("[FAIL] --dry-run 与 --apply 互斥（预演和真实写入不能同时要）。",
              file=sys.stderr)
        return 2

    root = Path(args.root).resolve()
    secrets_path = (Path(args.secrets).resolve() if args.secrets
                    else _default_secrets_path(root))
    entries = _secret_entries(root)
    exists = secrets_path.is_file()
    existing = _existing_secrets(secrets_path)
    payload, changes = _build_payload(existing, entries)

    result: dict[str, Any] = {
        "ok": True, "mode": "apply" if args.apply else "dry-run",
        "root": str(root), "secrets_path": str(secrets_path),
        "entries": [
            {"source": e["source"], "section": e["section"], "field": e["field"],
             "length": len(e["value"]) if isinstance(e["value"], (str, dict, list)) else None,
             "fingerprint": fingerprint(e["value"])}
            for e in entries
        ],
        "secrets_changes": changes, "purged": [], "refused": [],
    }

    if not args.apply:
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            _print_plan(root, secrets_path, entries, payload, changes, exists)
        return 0

    # ---- apply：先写密钥库（失败则什么都不清空）----
    if entries:
        if not _atomic_write_json(secrets_path, payload):
            print("[FAIL] 写入密钥库失败（磁盘/权限）；旧文件未改动。", file=sys.stderr)
            if args.json:
                result["ok"] = False
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1
        acl_state = _tighten(secrets_path)
        print(f"[APPLY] 已写入 {_relative(root, secrets_path)}"
              f"（{len(entries)} 项，权限：{acl_state}）")
        if acl_state.startswith("failed"):
            print("  [WARN] ACL 收紧失败：请在管理员终端执行 python tools/harden_acl.py --apply")
        if args.json:
            result["acl"] = acl_state
    else:
        print(f"[APPLY] 没有需要迁移的明文密钥；{_relative(root, secrets_path)}"
              f"{'已存在' if exists else '不存在'}，未改动。")

    # ---- 可选：清空旧字段（必须逐项确认密钥库里的副本一致）----
    exit_code = 0
    if args.purge_plaintext:
        try:
            purged_web, refused_web = _purge_web_config(root, entries, payload)
            purged_cfg, refused_cfg = _purge_root_config(root, entries, payload)
        except OSError as exc:
            print(f"[FAIL] 清空旧字段时写回失败：{exc}", file=sys.stderr)
            return 1
        purged = purged_web + purged_cfg
        refused = refused_web + refused_cfg
        result["purged"] = purged
        result["refused"] = refused
        if purged:
            print(f"[APPLY] 已清空 {len(purged)} 个明文字段：")
            for item in purged:
                print(f"    - {item} = <已清空>")
        if refused:
            exit_code = 3
            print(f"[WARN] 有 {len(refused)} 个字段拒绝清空（密钥库里没有确认过的副本）：")
            for item in refused:
                print(f"    - {item}（保留明文，避免丢密钥）")
        if not purged and not refused:
            print("[APPLY] 旧文件里已没有明文敏感字段（幂等）。")

    if args.json:
        result["ok"] = exit_code == 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return exit_code

    _print_summary(payload, entries)
    if args.purge_plaintext and exit_code == 0:
        print("[DONE] 迁移完成：密钥只在密钥库/环境变量里。")
        print("  现在设置 DEEPSEEK_API_KEY 环境变量（迁移后的推荐来源），然后重启服务。")
        print("  验证：python tools/check_secrets.py 与 python tools/harden_acl.py --check")
        _print_rollback(root, secrets_path)
    else:
        print("[DONE] 密钥已复制到密钥库（旧文件未清空，生产进程不受影响）。")
        _print_next_steps()
        _print_rollback(root, secrets_path)
    return exit_code


def _default_secrets_path(root: Path) -> Path:
    """密钥库默认位置：SOULAGENT_SECRETS → <root>/agi/data/secrets.json。"""
    raw = str(os.environ.get("SOULAGENT_SECRETS") or "").strip()
    if raw:
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        return path if path.is_absolute() else root / path
    return root / "agi" / "data" / "secrets.json"


if __name__ == "__main__":
    raise SystemExit(main())