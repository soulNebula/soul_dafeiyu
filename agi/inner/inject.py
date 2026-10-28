# -*- coding: utf-8 -*-
"""W2-A 注入：按 INNER_MODE 组装注入 chat prompt 的 inner_ctx。

模式：
- ``off``    → ""（默认；prompt 与改动前逐字节一致，由 chat._prompt 的空串分支保证）
- ``static`` → 固定人格段落（B 臂），不读用户状态
- ``rule``   → 用当前会话状态按阈值渲染行为指令
- ``model``  → 让 client 生成 InnerState JSON 再渲染；client 不可用 / 超时 / 非 JSON /
               字段非法 / JSON 为空壳，一律回退 rule（不抛异常，不影响主链）

model 模式有两种 client：
- **本地灵魂 client**（带 ``local_soul = True``，或调用方显式 ``is_local=True``）：
  messages 由 ``agi/soul/prompt.py::build_messages`` 构造——与 SFT 训练分布同源
  （system = SOUL_PERSONA + 当前状态行 + 最近对话 + 用户这句话）；
- **远程 DeepSeek client**：保持 W2 原有单条 prompt 路径不变（兼容旧行为）。

额外安全：``monologue`` 先 sanitize——≤40 字、无换行、去掉第二人称命令（你是/你必须…）、
不含用户原文 8 字以上片段，避免把用户原话或命令式语气带进提示词。

P3-a 生产接入（本模块负责，chat.py 只做编排）：

- :func:`resolve_settings`：``INNER_MODE`` 环境变量 > 配置文件 ``inner_mode`` > ``off``；
  另读 ``soul_server_url`` / ``soul_timeout_s`` / ``inner_shadow_only``（env 优先）。
  显式注入 client 的调用方（测试/离线消融）视为「非生产上下文」，不受配置文件开关影响
  （由 chat.py 传 production 标志），保证测试不会被线上灰度配置污染。
- :func:`make_local_client`：按 settings 构造/复用 ``agi.soul.llama_client.LlamaClient``。
- :func:`probe_local`：``/health`` 探活 + TTL 缓存。
- :func:`build_inner_ctx_ex`：带元数据的构建入口（ctx/source/fallback/latency/injected），
  供 chat.py 落 ``inner_states`` 与 ``gen_meta``；失败自动回退 rule 并节流写
  ``inner_fallback`` 日志（:func:`log_fallback`）。
- :func:`stats`：``/status`` 用的进程级观测值（mode / last_latency_ms / fallbacks_24h）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any
from agi.paths import data_dir as _default_data_dir

from . import render, schema

MODES = ("off", "static", "rule", "model")
SHADOW_MODE = "shadow"
ALL_MODES = MODES + (SHADOW_MODE,)
DEFAULT_MODE = "off"
MAX_MONOLOGUE = 40

# ---------------------------------------------------------------- P3-a 生产接入
#: 本地灵魂模型（llama-server）默认地址与超时；环境变量/配置可覆盖（env 优先）
DEFAULT_SOUL_SERVER_URL = "http://127.0.0.1:8123"
DEFAULT_SOUL_TIMEOUT_S = 2.0
#: /health 探测结果缓存（秒）：避免每条消息都打一次 HTTP 探活
DEFAULT_HEALTH_CACHE_S = 15.0
#: inner_fallback 日志节流（任务要求每 10 分钟最多 1 条）
FALLBACK_LOG_INTERVAL_S = 600.0
#: 回退计数窗口（/status fallbacks_24h）
FALLBACK_WINDOW_S = 86400.0
#: P3-b：内状态调用范围（延迟兜底）。被动对话默认 rule（0 延迟），主动消息才用 model。
SCOPES = ("all", "proactive_only", "off")
DEFAULT_SCOPE = "proactive_only"
#: 测试/离线（显式注入 client）上下文默认 all，保持 W2/W4 的 model 语义不变。
DEFAULT_SCOPE_NON_PROD = "all"
#: P3-b：输出 schema。slim=只要求 4 字段（省 ~60 tokens）；full=完整 InnerState（对照用）。
SCHEMA_MODES = ("slim", "full")
DEFAULT_SCHEMA_MODE = "slim"

ROOT = Path(__file__).resolve().parents[2]
#: inner_fallback JSONL 日志；测试可 monkeypatch 本模块属性
INNER_LOG_PATH = _default_data_dir() / "selflearn" / "inner.log"

_LOCK = threading.Lock()
_FALLBACK_TIMES: deque[float] = deque(maxlen=10000)
_LAST_LATENCY_MS: float | None = None
_LAST_FALLBACK_LOG_TS: float = 0.0
_HEALTH_CACHE: dict[str, tuple[float, bool]] = {}
_LOCAL_CLIENTS: dict[tuple[str, float, str], Any] = {}

_WS = re.compile(r"\s+")
_SECOND_PERSON = re.compile(r"你(?:必须|应该|需要|不能|不准|可以|得|要|是|别)")

MODEL_INSTRUCTION = (
    "你是内状态生成器（不是对话者）。根据会话状态、最近对话和用户这句话，"
    "生成这一轮的内在状态。只输出 JSON，不要输出其他文字：\n"
    '{"stance":"语气立场，不超过12字",'
    '"intent":"这轮想做什么，不超过30字",'
    '"monologue":"留在心里的一句话，不超过40字，不要出现第二人称命令，不要复述用户原话",'
    '"behavior":{"length":"short|normal|long","ask":true,"advice":true,"emoji":"偶尔|不用"}}\n'
    "要求：贴合给定状态与对话，不编造事实；不要输出解释或 markdown。"
)


def normalize_mode(mode: Any) -> str:
    """未知/空值一律回退 off（默认关闭，失败不改变行为）。"""
    value = str(mode or DEFAULT_MODE).strip().lower()
    return value if value in MODES else DEFAULT_MODE


def sanitize_monologue(text: Any, user_text: Any = "") -> str:
    """清洗内心独白：≤40 字、单行、无第二人称命令、不复述用户原文长片段。"""
    value = _WS.sub(" ", str(text or "").replace("\r", " ").replace("\n", " ")).strip()
    value = _SECOND_PERSON.sub("", value)
    value = value.strip("，,。.；;：:、 ")
    user = _WS.sub("", str(user_text or ""))
    if len(user) >= 8:
        for index in range(len(user) - 7):
            if user[index:index + 8] in value:
                value = ""
                break
    return value[:MAX_MONOLOGUE]


def _base_state(path, state: dict | None) -> schema.InnerState:
    # 延迟 import：agi.selflearn.__init__ 会导入 chat，避开自学习包的初始化环。
    from ..selflearn import state as sl_state
    from ..selflearn import trust as sl_trust

    data: dict = state if isinstance(state, dict) and state else {}
    if not data:
        try:
            data = sl_state.load(path)
        except Exception:  # noqa: BLE001
            data = {}
    try:
        trust_value = sl_trust.get(path)
    except Exception:  # noqa: BLE001
        trust_value = 0.5
    return schema.from_state(data, trust_value)


def _rule_ctx(path, text: str, state: dict | None) -> str:
    st = render.render_rule(_base_state(path, state))
    st.monologue = sanitize_monologue(st.monologue, text)
    return render.render_block(st)


def _is_local_client(client, is_local: bool | None) -> bool:
    """本地灵魂 client：显式 is_local 优先，否则看 client.local_soul 属性。"""
    if is_local is not None:
        return bool(is_local)
    try:
        return bool(getattr(client, "local_soul", False))
    except Exception:  # noqa: BLE001
        return False


def _client_available(client, *, local: bool) -> bool:
    """client 是否可用。

    非本地 client 保持 W2 原语义（没有 available() 视为不可用）；
    本地灵魂 client 的契约只有 local_soul/chat，没有 available() 时按「有 chat 即可用」，
    避免 llama_client 漏实现 available() 时静默回退 rule。
    带 ``base_url`` 的本地 client（生产 llama-server）走 :func:`probe_local` 的 TTL 缓存，
    避免每条消息都打一次 /health。
    """
    fn = getattr(client, "available", None)
    if local and str(getattr(client, "base_url", "") or ""):
        return probe_local(client)
    if callable(fn):
        try:
            return bool(fn())
        except Exception:  # noqa: BLE001
            return False
    return bool(local and callable(getattr(client, "chat", None)))


# ------------------------------------------------------------- 生产接入辅助


def _env_bool(name: str, default: bool | None = None) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in ("1", "true", "yes", "on", "y"):
        return True
    if value in ("0", "false", "no", "off", "n", ""):
        return False
    return default


def _env_float(name: str, default: float | None = None) -> float | None:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _read_gateway_config() -> dict:
    """无副作用读取网关配置（不走 config.load()，避免触发迁移写盘）。

    读失败时返回空 dict；由调用方回退默认值。
    """
    try:
        from ..wechat_gateway import config as gateway_config

        raw = json.loads(Path(gateway_config.CONFIG_PATH).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            merged = dict(getattr(gateway_config, "DEFAULTS", {}) or {})
            merged.update(raw)
            return merged
    except Exception:  # noqa: BLE001
        pass
    return {}


def resolve_settings(*, production: bool = True) -> dict:
    """解析内状态运行参数：环境变量优先，其次配置文件，最后默认值。

    ``production=False``（调用方显式注入 client 的测试/离线场景）时**不读配置文件**，
    只认环境变量，保证线上灰度配置不会污染测试套件。
    返回 ``{mode, shadow_only, server_url, timeout_s, scope, schema}``；
    mode/scope/schema 非法值一律回退安全默认。
    """
    cfg: dict = _read_gateway_config() if production else {}
    raw_mode = os.environ.get("INNER_MODE")
    if raw_mode is None:
        raw_mode = cfg.get("inner_mode", DEFAULT_MODE)
    mode = str(raw_mode or DEFAULT_MODE).strip().lower()
    if mode not in ALL_MODES:
        mode = DEFAULT_MODE
    env_shadow = _env_bool("INNER_SHADOW_ONLY")
    shadow_only = (bool(env_shadow) if env_shadow is not None
                   else bool(cfg.get("inner_shadow_only", False)))
    url = os.environ.get("SOUL_SERVER_URL") or str(
        cfg.get("soul_server_url") or DEFAULT_SOUL_SERVER_URL)
    timeout = _env_float("SOUL_TIMEOUT_S")
    if timeout is None:
        try:
            timeout = float(cfg.get("soul_timeout_s", DEFAULT_SOUL_TIMEOUT_S))
        except (TypeError, ValueError):
            timeout = DEFAULT_SOUL_TIMEOUT_S
    timeout = max(0.05, min(float(timeout), 30.0))
    # P3-b：scope（all/proactive_only/off）。生产默认 proactive_only（被动 0 延迟），
    # 测试/消融默认 all（保持 W2/W4 的 model 全量语义）。
    env_scope = os.environ.get("INNER_MODE_SCOPE")
    raw_scope = env_scope
    if raw_scope is None:
        raw_scope = cfg.get("inner_mode_scope")
    if raw_scope is None:
        raw_scope = DEFAULT_SCOPE if production else DEFAULT_SCOPE_NON_PROD
    scope = str(raw_scope or "").strip().lower()
    if scope not in SCOPES:
        scope = DEFAULT_SCOPE if production else DEFAULT_SCOPE_NON_PROD
    # P3-b：输出 schema（slim=4 字段省 token；full=对照）。
    raw_schema = os.environ.get("INNER_SCHEMA") or cfg.get("inner_schema")
    schema_mode = str(raw_schema or DEFAULT_SCHEMA_MODE).strip().lower()
    if schema_mode not in SCHEMA_MODES:
        schema_mode = DEFAULT_SCHEMA_MODE
    return {"mode": mode, "shadow_only": bool(shadow_only),
            "server_url": str(url or DEFAULT_SOUL_SERVER_URL).rstrip("/"),
            "timeout_s": round(timeout, 3), "scope": scope,
            "schema": schema_mode}


def scope_allows_model(scope: Any, *, proactive: bool) -> bool:
    """scope → 这次调用是否允许走 model（P3-b 延迟兜底）。

    - ``all``：被动/主动都用 model；
    - ``proactive_only``（生产默认）：被动消息用 rule（0 延迟），主动消息才用 model；
    - ``off``：都不用 model（等价内状态只用 rule）。
    """
    value = str(scope or "").strip().lower()
    if value == "all":
        return True
    if value == "proactive_only":
        return bool(proactive)
    return False


def client_schema(settings: dict | None = None) -> dict | None:
    """按 settings 选 llama-client 的 response_format schema。

    - ``slim``（默认）→ ``agi/soul/prompt.INNER_SCHEMA_SLIM``（4 字段，省 token）；
    - ``full`` → None（LlamaClient 用默认完整 InnerState schema，对照用）。
    """
    current = settings or resolve_settings()
    if str(current.get("schema") or DEFAULT_SCHEMA_MODE).strip().lower() != "slim":
        return None
    from ..soul import prompt as soul_prompt

    return soul_prompt.INNER_SCHEMA_SLIM


def make_local_client(settings: dict | None = None, *, force_new: bool = False) -> Any:
    """构造/复用本地 llama-server client（按 url+timeout+schema 缓存）。

    P3-b：默认用 slim schema（4 字段）；``settings["schema"] == "full"`` 时用完整 schema。
    """
    current = settings or resolve_settings()
    key = (str(current.get("server_url") or DEFAULT_SOUL_SERVER_URL),
           float(current.get("timeout_s") or DEFAULT_SOUL_TIMEOUT_S),
           str(current.get("schema") or DEFAULT_SCHEMA_MODE))
    if not force_new:
        client = _LOCAL_CLIENTS.get(key)
        if client is not None:
            return client
    from ..soul.llama_client import LlamaClient

    client = LlamaClient(key[0], timeout_s=key[1], schema=client_schema(current))
    _LOCAL_CLIENTS[key] = client
    return client


def probe_local(client, *, now: float | None = None) -> bool:
    """本地 client /health 探活，带 TTL 缓存（INNER_HEALTH_CACHE_S 可覆盖，0=每次探）。"""
    url = str(getattr(client, "base_url", "") or "")
    ttl = _env_float("INNER_HEALTH_CACHE_S", DEFAULT_HEALTH_CACHE_S)
    if ttl is None:
        ttl = DEFAULT_HEALTH_CACHE_S
    now = time.time() if now is None else now
    if url and ttl > 0:
        cached = _HEALTH_CACHE.get(url)
        if cached is not None and now - cached[0] < ttl:
            return cached[1]
    fn = getattr(client, "available", None)
    ok = False
    if callable(fn):
        try:
            ok = bool(fn())
        except Exception:  # noqa: BLE001
            ok = False
    if url:
        _HEALTH_CACHE[url] = (now, ok)
    return ok


def reset_stats() -> None:
    """清空进程级观测状态（测试用）。"""
    global _LAST_LATENCY_MS, _LAST_FALLBACK_LOG_TS
    with _LOCK:
        _FALLBACK_TIMES.clear()
        _HEALTH_CACHE.clear()
        _LOCAL_CLIENTS.clear()
        _LAST_LATENCY_MS = None
        _LAST_FALLBACK_LOG_TS = 0.0


def record_latency(latency_ms: float | None) -> None:
    """记录最近一次内状态调用耗时（/status last_latency_ms）。"""
    global _LAST_LATENCY_MS
    if latency_ms is None:
        return
    with _LOCK:
        _LAST_LATENCY_MS = round(float(latency_ms), 1)


def log_fallback(reason: str, *, now: float | None = None,
                 path: Path | str | None = None) -> bool:
    """记内状态回退事件：计数必记，JSONL 日志每 10 分钟最多 1 条。

    返回本次是否真的写了日志行（节流时 False）。
    """
    global _LAST_FALLBACK_LOG_TS
    now = time.time() if now is None else float(now)
    with _LOCK:
        _FALLBACK_TIMES.append(now)
        throttled = (now - _LAST_FALLBACK_LOG_TS) < FALLBACK_LOG_INTERVAL_S
        if not throttled:
            _LAST_FALLBACK_LOG_TS = now
    if throttled:
        return False
    entry = {"ts": round(now, 3), "kind": "inner_fallback",
             "reason": str(reason or "unknown")[:200]}
    target = Path(path or INNER_LOG_PATH)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001 - 日志失败不影响主链
        return False
    return True


def stats(*, now: float | None = None) -> dict:
    """``/status`` 用的内状态观测：mode / scope / last_latency_ms / fallbacks_24h。"""
    now = time.time() if now is None else float(now)
    with _LOCK:
        recent = sum(1 for ts in _FALLBACK_TIMES if now - ts <= FALLBACK_WINDOW_S)
        last = _LAST_LATENCY_MS
    settings = resolve_settings()
    return {
        "mode": settings["mode"],
        "shadow_only": settings["shadow_only"],
        "scope": settings["scope"],
        "schema": settings["schema"],
        "backend": "local" if settings["mode"] == "model" else "rule",
        "server_url": settings["server_url"],
        "timeout_s": settings["timeout_s"],
        "last_latency_ms": last,
        "fallbacks_24h": int(recent),
    }


def _classify_failure(message: str) -> str:
    """把 client 错误串归类成回退原因（写入 inner_states.fallback 与日志）。"""
    low = str(message or "").lower()
    if "timeout" in low or "timed out" in low:
        return "timeout"
    if "json" in low and "decode" in low:
        return "bad_json"
    if "connection" in low or "refused" in low or "urlopen" in low or "network" in low:
        return "connection_error"
    if "http" in low or "status" in low or "500" in low or "502" in low or "503" in low:
        return "http_error"
    return "client_error"


def _local_messages(base: schema.InnerState, recent: list | None,
                    text: str) -> list[dict]:
    """本地灵魂模型输入：与训练分布同源（agi/soul/prompt.py 唯一真源）。

    system 只带训练时存在的 7 个字段：mood/energy/sharing/battery/trust/stage/crisis；
    patience/openness 是 rule 层派生量，训练时没有，不能进 system。
    """
    # 延迟 import：inner 包初始化期不把 soul 拉进来（soul 依赖 inner.schema）。
    from ..soul import prompt as soul_prompt

    state_dict = {
        "mood": float(base.mood), "energy": float(base.energy),
        "sharing": float(base.sharing), "battery": float(base.battery),
        "trust": float(base.trust), "stage": str(base.stage),
        "crisis": bool(base.crisis),
    }
    return soul_prompt.build_messages(state_dict, list(recent or []), str(text or ""))


def _generate_model(path, client, text: str, recent: list | None,
                    state: dict | None, *, is_local: bool | None = None
                    ) -> tuple[str, dict, str]:
    """跑一次模型内状态生成。

    返回 ``(block, state_dict, fallback_reason)``：成功能拿到 block/state，
    失败 block="" 且 reason 标明原因（client_missing/client_unavailable/timeout/...）。
    本函数不写日志、不记延迟（保持 ``build_inner_ctx`` 旧行为零副作用）。
    """
    local = _is_local_client(client, is_local)
    if client is None:
        return "", {}, "client_missing"
    if not _client_available(client, local=local):
        return "", {}, "client_unavailable"

    base = _base_state(path, state)
    if local:
        # W4-D1：本地灵魂模型走训练同源 messages（system 带状态，历史是真实 turns）
        messages = _local_messages(base, recent, text)
    else:
        lines = []
        for item in list(recent or [])[-6:]:
            if not isinstance(item, dict):
                continue
            role = "用户" if item.get("role") == "user" else "你"
            lines.append(f"{role}：{str(item.get('text') or '')[:120]}")
        prompt = (
            MODEL_INSTRUCTION
            + "\n\n当前状态：mood={:.2f} energy={:.2f} sharing={:.2f} battery={:.2f} "
              "trust={:.2f} patience={:.2f} openness={:.2f} stage={} crisis={}".format(
                  float(base.mood), float(base.energy), float(base.sharing),
                  float(base.battery), float(base.trust), float(base.patience),
                  float(base.openness), base.stage,
                  "true" if base.crisis else "false")
            + "\n\n最近对话：\n" + ("\n".join(lines) or "（无）")
            + "\n\n用户刚刚说：\n" + str(text or "")
        )
        messages = [{"role": "system", "content": "你是只输出 JSON 的内状态生成器。"},
                    {"role": "user", "content": prompt}]
    try:
        result = client.chat(
            messages,
            purpose="inner_state", max_tokens=300, temperature=0.2, json_mode=True,
        )
    except Exception as exc:  # noqa: BLE001
        return "", {}, _classify_failure(f"{type(exc).__name__}: {exc}")
    if not isinstance(result, dict) or not result.get("ok"):
        error = str((result or {}).get("error") or "") if isinstance(result, dict) else ""
        return "", {}, _classify_failure(error) or "client_error"
    from ..selflearn.llm import parse_json

    obj = parse_json(str(result.get("text") or ""))
    if not isinstance(obj, dict):
        return "", {}, "bad_json"
    # P3-b：slim schema 下 4 个字段都必须有；缺字段视为输出不可用 → 回退 rule
    required = ("stance", "intent", "monologue", "behavior")
    if not any(key in obj for key in required):
        return "", {}, "empty_json"
    missing = [key for key in required if key not in obj]
    if missing:
        return "", {}, "missing_fields"
    try:
        st = schema.validate({**obj, "source": "model"})
    except (TypeError, ValueError):
        return "", {}, "invalid_state"
    # 数值/关系来自真实会话状态，模型只负责行为与独白，不允许改事实
    st.mood, st.energy, st.sharing, st.battery = base.mood, base.energy, base.sharing, base.battery
    st.trust, st.stage, st.crisis = base.trust, base.stage, base.crisis
    st.patience, st.openness = base.patience, base.openness
    st.monologue = sanitize_monologue(st.monologue, text)
    return render.render_block(st), st.to_dict(), ""


def _model_ctx(path, client, text: str, recent: list | None,
               state: dict | None, *, is_local: bool | None = None) -> str:
    """模型模式；任何异常/非法返回都返回 ""（→ 上层回退 rule）。"""
    block, _, _ = _generate_model(path, client, text, recent, state, is_local=is_local)
    return block


def _rule_ctx_ex(path, text: str, state: dict | None) -> tuple[str, dict]:
    """rule 渲染（block + 落库快照）；离线/回退时用。"""
    st = render.render_rule(_base_state(path, state))
    st.monologue = sanitize_monologue(st.monologue, text)
    return render.render_block(st), st.to_dict()


def build_inner_ctx(path, mode: str = "off", client=None, text: str = "",
                    recent: list | None = None, state: dict | None = None,
                    *, is_local: bool | None = None) -> str:
    """构建注入 chat prompt 的内状态片段；off 模式返回空串。

    ``is_local`` 只影响 model 模式：None = 按 ``client.local_soul`` 自动判断。
    本函数保持 W2 的零副作用语义（不写日志/不记延迟）；生产编排用
    :func:`build_inner_ctx_ex`。
    """
    mode = normalize_mode(mode)
    if mode == "off":
        return ""
    if mode == "static":
        return render.render_static()
    if mode == "rule":
        try:
            return _rule_ctx(path, text, state)
        except Exception:  # noqa: BLE001
            return ""
    block = ""
    try:
        block = _model_ctx(path, client, text, recent, state, is_local=is_local)
    except Exception:  # noqa: BLE001
        block = ""
    if block:
        return block
    try:
        return _rule_ctx(path, text, state)
    except Exception:  # noqa: BLE001
        return ""


def build_inner_ctx_ex(path, mode: str = "off", client=None, text: str = "",
                       recent: list | None = None, state: dict | None = None,
                       *, is_local: bool | None = None,
                       shadow_only: bool = False, proactive: bool = False,
                       scope: str | None = None) -> dict:
    """生产接入入口：构建内状态并返回元数据。

    返回::

        {"mode": off|static|rule|model,   # 实际生效模式（scope 拦下 model 时为 rule）
         "model_attempted": 是否真的调了模型（scope 拦截/off/rule 时 False）,
         "scope": all|proactive_only|off（本次判定所用策略）,
         "ctx": 渲染块（可能为空串）,
         "injected": 是否允许注入 prompt（shadow_only 时恒 False）,
         "source": "off"|"static"|"rule"|"local"|"remote",
         "state": 落库用 InnerState dict（失败/static/off 时为空 dict）,
         "fallback": "" | 回退原因,
         "latency_ms": 模型调用耗时（rule/static/off/scope 拦截为 None；失败也记）}

    model 模式：``scope`` 不允许（如默认 proactive_only 下的被动消息）→ 直接 rule
    渲染、**0 模型延迟**；允许时本地 client 不可用/超时/返回非法 → 自动回退 rule
    （不阻塞主链），回退计数 + 节流日志由本函数负责。
    """
    mode = normalize_mode(mode)
    effective_scope = str(scope or ("all" if proactive else DEFAULT_SCOPE)).strip().lower()
    result: dict = {"mode": mode, "model_attempted": False,
                    "scope": effective_scope, "ctx": "", "injected": False,
                    "source": "off", "state": {}, "fallback": "",
                    "latency_ms": None}
    if mode == "off":
        return result
    if mode == "static":
        result.update(ctx=render.render_static(), source="static",
                      injected=not shadow_only)
        return result
    if mode == "rule":
        try:
            ctx, state_dict = _rule_ctx_ex(path, text, state)
        except Exception:  # noqa: BLE001
            ctx, state_dict = "", {}
        result.update(ctx=ctx, state=state_dict, source="rule",
                      injected=not shadow_only)
        return result

    # P3-b：scope 兜底——被动消息默认不调模型，直接 rule（0 延迟）
    if not scope_allows_model(effective_scope, proactive=bool(proactive)):
        try:
            rule_ctx, rule_state = _rule_ctx_ex(path, text, state)
        except Exception:  # noqa: BLE001
            rule_ctx, rule_state = "", {}
        result.update(mode="rule", ctx=rule_ctx, state=rule_state, source="rule",
                      injected=not shadow_only)
        return result

    local = _is_local_client(client, is_local)
    started = time.perf_counter()
    block, state_dict, fallback = "", {}, "internal_error"
    try:
        block, state_dict, fallback = _generate_model(
            path, client, text, recent, state, is_local=is_local)
    except Exception as exc:  # noqa: BLE001
        fallback = _classify_failure(f"{type(exc).__name__}: {exc}")
    latency_ms = round((time.perf_counter() - started) * 1000, 1)
    result["latency_ms"] = latency_ms
    result["model_attempted"] = True
    record_latency(latency_ms)
    if block:
        result.update(ctx=block, state=state_dict, fallback="",
                      source="local" if local else "remote",
                      injected=not shadow_only)
        return result

    # 回退：计数 + 节流日志（不抛异常、不阻塞主链）
    log_fallback(fallback or "unknown")
    if shadow_only:
        # 灰度期只记录、不注入；渲染失败就没有可注入的块
        result.update(ctx="", state=state_dict, fallback=fallback,
                      source="rule", injected=False)
        return result
    try:
        rule_ctx, rule_state = _rule_ctx_ex(path, text, state)
    except Exception:  # noqa: BLE001
        rule_ctx, rule_state = "", {}
    result.update(ctx=rule_ctx, state=rule_state, fallback=fallback,
                  source="rule", injected=True)
    return result
