# -*- coding: utf-8 -*-
"""四臂消融生成器：golden_v1.jsonl → ablation_raw.jsonl（只生成，不评审）。

对黄金集里的每条 context，用同一个 SelfLearnAgent 在 4 种 ``INNER_MODE`` 下各
跑一次 ``send()``，每次使用独立的临时 SQLite（不写 ``agi/data/``），输出逐行
可对比的 JSONL：

    {"golden_id": "G-001", "bucket": "work", "mode": "off",
     "reply": "...", "ok": true, "latency_ms": 12.3, "error": null,
     "client": "mock", "ts": 1730000000.0}

四臂（``INNER_MODE``，由 agi/inner 任务实现；本文件只负责设置环境变量）：

- ``off``    ：不注入内状态（默认，也是改动前行为）
- ``static`` ：注入固定人格段落
- ``rule``   ：按当前状态用规则渲染行为指令
- ``model``  ：让模型生成 InnerState 再渲染，失败自动回退 rule

没有 DeepSeek API key 时用 ``--mock``（确定性 FakeClient，不联网）。
``--real`` 缺 key 会明确报错退出（返回码 2），不会静默回退到 mock。

断点续跑：输出文件里已有的 ``(golden_id, mode)`` 会直接跳过，重跑不会重复写。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# 允许 `python agi/eval_data/ablation.py` 直接运行时找到仓库根
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agi.selflearn import memory  # noqa: E402
from agi.selflearn.chat import SelfLearnAgent  # noqa: E402

ROOT = _ROOT
GOLDEN_PATH = ROOT / "agi" / "eval_data" / "golden_v1.jsonl"
OUT_PATH = ROOT / "agi" / "eval_data" / "ablation_raw.jsonl"
MODES = ("off", "static", "rule", "model")
MODE_ENV = "INNER_MODE"
MOCK_MARK = "（mock）"

# 只用于 mock 自测的提示文案匹配：A 任务按契约在 prompt 里插入的固定标题
_INNER_HEADER = "你的内在状态"


class MockClient:
    """确定性 FakeClient：不联网、不需要 API key。

    - 对记忆/主张抽取返回空结果，避免测试数据库被 mock 数据污染；
    - 内状态生成请求（agi/inner 的 model 臂）返回合法 JSON，便于走通渲染；
    - 普通回复按 prompt 里是否出现「你的内在状态」加标记，方便确认注入是否生效。
    """

    def __init__(self) -> None:
        self.calls = 0

    def available(self) -> bool:
        return True

    def chat(self, messages, **kwargs) -> dict[str, Any]:
        self.calls += 1
        purpose = str(kwargs.get("purpose") or "")
        text = "\n".join(
            str(m.get("content") or "") if isinstance(m, dict) else str(m)
            for m in (messages or [])
        )
        if purpose == "selflearn_memory":
            return _ok('{"items":[]}')
        if purpose == "selflearn_claims":
            return _ok('{"claims":[]}')
        if purpose == "selflearn_evidence_stage":
            return _ok('{"claims":[]}')
        if purpose in ("selflearn_summary", "selflearn_profile"):
            return _ok('{"summary":"（mock 摘要）","open_threads":[]}')
        if "内在状态" in text and "json" in text.lower():
            return _ok(json.dumps({
                "mood": 0.5, "energy": 0.6, "sharing": 0.4, "battery": 0.8,
                "trust": 0.5, "stage": "认识",
                "stance": "温和", "intent": "接住对方",
                "monologue": "先听，再问一句",
                "behavior": {"length": "短", "ask": True, "advice": False,
                             "emoji": False},
                "source": "model",
            }, ensure_ascii=False))
        if purpose == "selflearn_chat":
            if _INNER_HEADER in text:
                return _ok(MOCK_MARK + "嗯，我听到了，你接着说。")
            return _ok(MOCK_MARK + "嗯，我在听，你慢慢说。")
        return _ok(MOCK_MARK + "嗯，我在听，你慢慢说。")


def _ok(text: str) -> dict[str, Any]:
    return {"ok": True, "text": text}


def inner_integration_ready() -> bool:
    """agi/inner 是否已合入；没合入时 INNER_MODE 不生效，四臂回复会相同。"""
    try:
        from agi.inner import inject  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def build_real_client():
    """构造真实 DeepSeek client；缺 key 时明确报错退出（不静默回退 mock）。"""
    from agi.selflearn.llm import Client, load_config

    config = load_config()
    env_key = os.environ.get("DEEPSEEK_API_KEY")
    if env_key is not None:  # 显式设置（含空串）优先于 web_config.json
        config["api_key"] = env_key.strip()
    client = Client(config)
    if not client.available():
        print(
            "错误：--real 需要 DeepSeek API key，但当前没有可用 key。\n"
            "  已检查 web_config.json 的 llm.api_key 与 DEEPSEEK_API_KEY，均未配置。\n"
            "  不会静默回退到 mock；请配置 key 后重跑，或改用 --mock 离线自测。",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return client


def load_golden(path: Path | str = GOLDEN_PATH) -> list[dict[str, Any]]:
    """读取黄金集 JSONL；每行必须可 json.loads 且含 id/context/user_msg。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"黄金集不存在：{path}")
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"黄金集第 {lineno} 行不是合法 JSON：{exc}") from exc
            if not isinstance(item, dict) or not item.get("id"):
                raise ValueError(f"黄金集第 {lineno} 行缺少 id：{line[:120]}")
            items.append(item)
    return items


def existing_keys(out_path: Path | str) -> set[tuple[str, str]]:
    """已完成的 (golden_id, mode) 集合，用于断点续跑；坏行跳过并提示。"""
    path = Path(out_path)
    done: set[tuple[str, str]] = set()
    if not path.exists():
        return done
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                print(f"警告：输出文件第 {lineno} 行不是合法 JSON，忽略：{line[:80]}",
                      file=sys.stderr)
                continue
            if isinstance(row, dict) and row.get("golden_id") and row.get("mode"):
                done.add((str(row["golden_id"]), str(row["mode"])))
    return done


def seed_context(path: Path | str, context: list[dict[str, Any]]) -> int:
    """把黄金集 context 的 user/assistant 轮次按顺序写进临时库。"""
    total = 0
    base = 1_700_000_000.0
    for i, turn in enumerate(context or []):
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "").strip()
        text = str(turn.get("text") or "").strip()
        if role not in ("user", "assistant") or not text:
            continue
        memory.store_message(path, role, text, now=base + i * 0.001)
        total += 1
    return total


def run_one(item: dict[str, Any], mode: str, client, *, client_label: str,
            tmp_root: Path | str | None = None) -> dict[str, Any]:
    """在独立 tmp SQLite 上跑一条 (golden_id, mode)，返回输出行。"""
    row: dict[str, Any] = {
        "golden_id": str(item.get("id") or ""),
        "bucket": str(item.get("bucket") or ""),
        "mode": mode,
        "reply": "",
        "ok": False,
        "latency_ms": 0.0,
        "error": None,
        "client": client_label,
        "ts": round(time.time(), 3),
    }
    old = os.environ.get(MODE_ENV)
    os.environ[MODE_ENV] = mode
    started = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix="ablation_", dir=tmp_root) as td:
            path = Path(td) / "selflearn.sqlite3"
            seed_context(path, item.get("context") or [])
            agent = SelfLearnAgent(path, client=client, rng=random.Random(0))
            out = agent.send(str(item.get("user_msg") or ""))
        reply = str(out.get("reply") or "")
        row["reply"] = reply
        row["ok"] = bool(out.get("ok"))
        if not row["ok"]:
            row["error"] = str(out.get("error") or "send() 返回 ok=False")
        elif not reply:
            # 情绪差主动不回时 reply 合法为空；记录原因，不当作成功生成
            row["error"] = str(out.get("skip_reason") or "empty_reply")
    except Exception as exc:  # noqa: BLE001 - 单条失败不能让整批中断
        row["ok"] = False
        row["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if old is None:
            os.environ.pop(MODE_ENV, None)
        else:
            os.environ[MODE_ENV] = old
        row["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return row


def run_ablation(*, golden: Path | str = GOLDEN_PATH, out: Path | str = OUT_PATH,
                 modes: list[str] | tuple[str, ...] | None = None,
                 limit: int = 0, client, client_label: str = "mock",
                 tmp_root: Path | str | None = None) -> dict[str, Any]:
    """批量生成；返回统计。已存在的 (golden_id, mode) 跳过。"""
    modes = list(modes or MODES)
    items = load_golden(golden)
    if limit and limit > 0:
        items = items[:limit]
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = existing_keys(out)
    planned = len(items) * len(modes)
    written = skipped = failed = 0
    started = time.perf_counter()
    with out.open("a", encoding="utf-8") as fh:
        for item in items:
            for mode in modes:
                key = (str(item.get("id")), mode)
                if key in done:
                    skipped += 1
                    continue
                row = run_one(item, mode, client, client_label=client_label,
                              tmp_root=tmp_root)
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()  # 每行落盘，进程中断也能续跑
                done.add(key)
                written += 1
                if not row["ok"]:
                    failed += 1
                print(f"[{written + skipped}/{planned}] {key[0]} {mode} "
                      f"ok={row['ok']} {row['latency_ms']}ms", flush=True)
    elapsed = round(time.perf_counter() - started, 2)
    stats = {"planned": planned, "written": written, "skipped": skipped,
             "failed": failed, "elapsed_s": elapsed, "out": str(out)}
    print(f"完成：计划 {planned} 行，新写 {written} 行，跳过 {skipped} 行，"
          f"失败 {failed} 行，耗时 {elapsed}s → {out}", flush=True)
    return stats


def _parse_modes(raw: str, parser: argparse.ArgumentParser) -> list[str]:
    modes: list[str] = []
    for part in str(raw or "").replace(" ", "").split(","):
        if not part:
            continue
        if part not in MODES:
            parser.error(f"未知模式 {part!r}，可选：{','.join(MODES)}")
        if part not in modes:
            modes.append(part)
    if not modes:
        parser.error(f"至少要指定一个模式，可选：{','.join(MODES)}")
    return modes


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agi.eval_data.ablation",
        description="四臂消融生成器：golden_v1.jsonl → ablation_raw.jsonl",
    )
    parser.add_argument("--golden", default=str(GOLDEN_PATH),
                        help=f"黄金集 JSONL（默认 {GOLDEN_PATH}）")
    parser.add_argument("--out", default=str(OUT_PATH),
                        help=f"输出 JSONL（默认 {OUT_PATH}）")
    parser.add_argument("--modes", default=",".join(MODES),
                        help=f"逗号分隔的模式，默认 {','.join(MODES)}")
    parser.add_argument("--limit", type=int, default=0,
                        help="只跑前 N 条 golden（0=全部；按文件顺序取前 N 条）")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--mock", action="store_true",
                       help="用确定性 FakeClient（不联网，默认）")
    group.add_argument("--real", action="store_true",
                       help="真实调用 DeepSeek；缺 key 时明确报错退出")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    modes = _parse_modes(args.modes, parser)
    if args.limit < 0:
        parser.error("--limit 不能为负")
    if not inner_integration_ready():
        print("警告：未检测到 agi/inner，INNER_MODE 不会生效；四臂回复将完全相同，"
              "结果只能验证管线（mode 字段仍会如实记录）。", file=sys.stderr)
    if args.real:
        client, label = build_real_client(), "real"
    else:
        client, label = MockClient(), "mock"
        if not args.mock:
            print("提示：未指定 --real，默认使用 --mock（确定性 FakeClient，"
                  "不联网、不需要 key）。", file=sys.stderr)
    try:
        run_ablation(golden=args.golden, out=args.out, modes=modes,
                     limit=args.limit, client=client, client_label=label)
    except FileNotFoundError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
