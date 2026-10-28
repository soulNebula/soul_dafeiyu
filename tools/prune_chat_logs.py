#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""聊天/媒体留存 TTL 清理（默认 dry-run，绝不静默删数据）。

为什么需要
----------
本工具会在本机留下两类**高敏感**数据，而且是持续增长的：

* ``log/messages.jsonl``：微信全量消息（含对方昵称、聊天正文、媒体相对路径）；
* ``log/聊天记录.txt``：同上，纯文本版；
* ``media/``：解密出来的图片/贴纸/GIF/文件缩略图。

不设 TTL 等于「永久保存全部聊天记录 + 全部图片」。一旦设备丢失/多人共用/目录被
同步到网盘，泄漏面随时间线性变大。这里提供一个**显式、可预演**的清理入口：

* 默认 ``--dry-run``：只统计「会删多少」，不动任何文件；
* ``--apply`` 才真正删/改写；
* 只处理白名单目录（``log/``、``media/``、``<root>/agi/data/desktop_profile`` 只提示）。

口径（写清楚，别让人误以为「删了就等于没了」）
------------------------------------------------
* ``messages.jsonl``：按每行 JSON 的 ``ts``（epoch 秒）判龄；缺 ``ts`` 时退化为文件
  mtime。**解析不了的行一律保留**（宁可多留，不能误删无法判断时间的证据）。
* ``聊天记录.txt``：这个文件**每行没有时间戳**（``名字: 内容``），无法逐行判龄。
  因此只在整个文件 ``mtime`` 已超期时删除整文件；否则保留并提示「无时间戳，无法
  逐行清理」。要更细的留存策略，请改用 ``messages.jsonl``（有 ts）。
* ``media/``：按文件 mtime 删除超期文件（不递归进子目录以外的符号链接，不删目录）。
* ``agi/data/desktop_profile``：**不自动删**（用户画像/训练语料，删了要重新积累），
  只在报告里说明它包含什么、以及手动清理的风险。

退出码::

    0  成功（含「没有超期数据」）
    1  执行失败（读写错误；此时不会继续删下一个目标）
    2  参数错误（如 --days <= 0）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_DAYS = 30


def _utf8_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def _cutoff(days: int, now: float | None = None) -> float:
    return (time.time() if now is None else float(now)) - days * 86400.0


def _line_timestamp(raw: str) -> float | None:
    """从一行 JSONL 里取时间戳：``ts`` 优先，其次 ``time``/``created_at``。

    取不到返回 None —— 调用方必须「保留该行」，不能当成 0（1970）而被删掉。
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("ts", "time", "created_at", "timestamp"):
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def plan_jsonl(path: Path, cutoff: float) -> dict[str, Any]:
    """统计 messages.jsonl 的超期行；不写文件。"""
    info: dict[str, Any] = {"path": str(path), "total": 0, "expired": 0,
                            "kept_unparsed": 0, "bytes": 0, "expired_bytes": 0,
                            "missing": not path.is_file()}
    if info["missing"]:
        return info
    try:
        stat = path.stat()
        fallback_ts = stat.st_mtime
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info
    info["bytes"] = stat.st_size
    kept: list[str] = []
    expired: list[str] = []
    for line in text.splitlines(keepends=True):
        info["total"] += 1
        stamp = _line_timestamp(line)
        if stamp is None:
            if line.strip():
                info["kept_unparsed"] += 1
            kept.append(line)
            continue
        if stamp < cutoff:
            expired.append(line)
        else:
            kept.append(line)
    info["expired"] = len(expired)
    info["expired_bytes"] = sum(len(item.encode("utf-8")) for item in expired)
    info["_kept"] = kept
    info["_expired"] = expired
    info["file_older_than_cutoff"] = fallback_ts < cutoff
    return info


def apply_jsonl(path: Path, plan: dict[str, Any]) -> bool:
    """原子写回保留行；返回是否成功。超期行为 0 时不动盘。"""
    if not plan.get("expired"):
        return True
    kept = plan.get("_kept") or []
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text("".join(kept), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def scan_media(media_dir: Path, cutoff: float) -> dict[str, Any]:
    """统计 media/ 下超期文件（按 mtime）；不删。"""
    info: dict[str, Any] = {"path": str(media_dir), "total": 0, "expired": 0,
                            "bytes": 0, "expired_bytes": 0, "missing": not media_dir.is_dir(),
                            "items": []}
    if info["missing"]:
        return info
    for item in sorted(media_dir.rglob("*")):
        try:
            if item.is_symlink() or not item.is_file():
                continue
            stat = item.stat()
        except OSError:
            continue
        info["total"] += 1
        info["bytes"] += stat.st_size
        if stat.st_mtime < cutoff:
            info["expired"] += 1
            info["expired_bytes"] += stat.st_size
            info["items"].append(str(item))
    return info


def apply_media(info: dict[str, Any]) -> tuple[int, list[str]]:
    """删除计划里的文件；返回 (删除数, 失败项)。"""
    removed = 0
    failures: list[str] = []
    for name in info.get("items") or []:
        try:
            Path(name).unlink()
            removed += 1
        except OSError as exc:
            failures.append(f"{name}: {type(exc).__name__}")
    return removed, failures


def plan_chat_text(path: Path, cutoff: float) -> dict[str, Any]:
    """聊天记录.txt：无逐行时间戳，只能整文件按 mtime 判定。"""
    info: dict[str, Any] = {"path": str(path), "missing": not path.is_file(),
                            "expired": False, "bytes": 0, "lines": 0}
    if info["missing"]:
        return info
    try:
        stat = path.stat()
        info["bytes"] = stat.st_size
        info["lines"] = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    except OSError as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info
    info["expired"] = stat.st_mtime < cutoff
    return info


def apply_chat_text(path: Path, plan: dict[str, Any]) -> bool:
    if not plan.get("expired"):
        return True
    try:
        path.unlink()
        return True
    except OSError:
        return False


def desktop_profile_note(root: Path) -> str | None:
    """desktop_profile 只提示不删：它是用户画像/训练语料，删掉等于重建成本。"""
    path = root / "agi" / "data" / "desktop_profile"
    if not path.exists():
        return None
    return (f"{path} 含桌面版画像/训练派生数据；本工具**不自动删除**（删了要重新积累）。"
            "如确需清理，请先备份并确认没有正在进行的训练/评测任务。")


def _human_bytes(value: float) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{size:.1f}GB"


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    parser = argparse.ArgumentParser(
        prog="python tools/prune_chat_logs.py",
        description="聊天/媒体留存 TTL 清理（默认 dry-run；--apply 才删除）")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"保留天数（默认 {DEFAULT_DAYS}；必须 >=1）")
    parser.add_argument("--root", default=str(ROOT), help="仓库根（默认本脚本上一级）")
    parser.add_argument("--log-dir", default=None, help="覆盖 log 目录（默认 <root>/log）")
    parser.add_argument("--media-dir", default=None, help="覆盖 media 目录（默认 <root>/media）")
    parser.add_argument("--apply", action="store_true", help="真正删除（默认只统计）")
    parser.add_argument("--dry-run", action="store_true",
                        help="显式声明只预演（默认行为；与 --apply 互斥）")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args(argv)

    if args.dry_run and args.apply:
        print("[FAIL] --dry-run 与 --apply 互斥（预演和真实删除不能同时要）。",
              file=sys.stderr)
        return 2

    if args.days < 1:
        print("[FAIL] --days 必须 >= 1（0 或负数会清空全部历史，工具拒绝执行）。",
              file=sys.stderr)
        return 2

    root = Path(args.root).resolve()
    log_dir = Path(args.log_dir).resolve() if args.log_dir else root / "log"
    media_dir = Path(args.media_dir).resolve() if args.media_dir else root / "media"
    cutoff = _cutoff(args.days)
    cutoff_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cutoff))

    jsonl_plan = plan_jsonl(log_dir / "messages.jsonl", cutoff)
    chat_plan = plan_chat_text(log_dir / "聊天记录.txt", cutoff)
    media_plan = scan_media(media_dir, cutoff)
    profile_note = desktop_profile_note(root)

    result: dict[str, Any] = {
        "ok": True, "mode": "apply" if args.apply else "dry-run",
        "days": args.days, "cutoff": cutoff_text,
        "messages_jsonl": {k: v for k, v in jsonl_plan.items() if not k.startswith("_")},
        "chat_text": chat_plan,
        "media": {k: v for k, v in media_plan.items() if k != "items"},
        "desktop_profile_note": profile_note,
        "removed": {},
    }

    if not args.apply:
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"[DRY-RUN] 留存 TTL 清理计划（保留 {args.days} 天；截止 {cutoff_text}）")
            print("  不会删除/改写任何文件；确认后加 --apply。")
            if jsonl_plan.get("missing"):
                print("  - log/messages.jsonl：不存在（跳过）")
            else:
                print(f"  - log/messages.jsonl：共 {jsonl_plan['total']} 行，"
                      f"超期 {jsonl_plan['expired']} 行（{_human_bytes(jsonl_plan['expired_bytes'])}；"
                      f"无法判定时间而保留 {jsonl_plan['kept_unparsed']} 行）")
            if chat_plan.get("missing"):
                print("  - log/聊天记录.txt：不存在（跳过）")
            elif chat_plan.get("expired"):
                print(f"  - log/聊天记录.txt：整体超期（{_human_bytes(chat_plan['bytes'])}，"
                      f"{chat_plan['lines']} 行）→ --apply 时删除整文件")
            else:
                print(f"  - log/聊天记录.txt：未整体超期（{chat_plan['lines']} 行）；"
                      "该文件逐行没有时间戳，无法按行清理，保留")
            if media_plan.get("missing"):
                print("  - media/：不存在（跳过）")
            else:
                print(f"  - media/：共 {media_plan['total']} 个文件 "
                      f"{_human_bytes(media_plan['bytes'])}，超期 {media_plan['expired']} 个"
                      f"（{_human_bytes(media_plan['expired_bytes'])}）")
            if profile_note:
                print(f"  - {profile_note}")
            total_bytes = jsonl_plan.get("expired_bytes", 0) + media_plan.get("expired_bytes", 0)
            if chat_plan.get("expired"):
                total_bytes += chat_plan.get("bytes", 0)
            print(f"  预计释放：{_human_bytes(total_bytes)}")
            print("  提示：删除后不可恢复；需要留档请先复制 log/ 到加密备份。")
            print("  执行：python tools/prune_chat_logs.py --apply")
        return 0

    # ---- apply ----
    failures: list[str] = []
    if jsonl_plan.get("error"):
        failures.append(f"messages.jsonl 读取失败：{jsonl_plan['error']}")
    elif not apply_jsonl(log_dir / "messages.jsonl", jsonl_plan):
        failures.append("messages.jsonl 写回失败（原文件未删除，可重跑）")
    result["removed"]["messages_lines"] = jsonl_plan.get("expired", 0)

    if chat_plan.get("error"):
        failures.append(f"聊天记录.txt 读取失败：{chat_plan['error']}")
    elif not apply_chat_text(log_dir / "聊天记录.txt", chat_plan):
        failures.append("聊天记录.txt 删除失败")
    result["removed"]["chat_text_file"] = bool(chat_plan.get("expired"))

    removed_media, media_failures = apply_media(media_plan)
    result["removed"]["media_files"] = removed_media
    failures.extend(media_failures[:5])

    result["ok"] = not failures
    if failures:
        result["failures"] = failures
        print("[FAIL] 清理未全部成功：", file=sys.stderr)
        for item in failures:
            print(f"  - {item}", file=sys.stderr)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"[APPLY] 留存 TTL 清理完成（保留 {args.days} 天；截止 {cutoff_text}）")
        print(f"  - messages.jsonl：删除 {jsonl_plan.get('expired', 0)} 行，"
              f"保留 {jsonl_plan.get('total', 0) - jsonl_plan.get('expired', 0)} 行")
        print(f"  - 聊天记录.txt：{'已删除整文件' if chat_plan.get('expired') else '未超期，保留'}")
        print(f"  - media/：删除 {removed_media} 个文件")
        if profile_note:
            print(f"  - {profile_note}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())