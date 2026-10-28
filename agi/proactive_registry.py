# -*- coding: utf-8 -*-
"""主动链登记表（唯一真相源）——「到底有几套主动链、哪套在跑」。

背景：项目里曾经同时存在网关常驻循环、selflearn ticker、8082 网页 tick、CLI 手动 tick
等多条主动入口，文档一度写成「现役是 simple_tick」，但代码里的真实调用链是：

    Gateway._proactive_loop                (agi/wechat_gateway/server.py)
      → Gateway._proactive_cycle           (同上；先看 engine_enabled)
        → proactive_worker.run_cycle       (agi/wechat_gateway/proactive_worker.py)
          → ticker.run_once                (agi/selflearn/ticker.py；先过 autonomy.gate)
            → proactive.tick               (agi/selflearn/proactive.py)
              → proactive._tick_impl       (按 proactive_style 分流)
                → simple_tick.tick_simple  (仅当 proactive_style=simple；
                                           默认 legacy，走 proactive.py 内的生成逻辑)

本模块只做**登记**与唯一入口包装，不改变任何运行行为：
- `ENTRIES` 是主动链清单（status=active/legacy/manual），登记调用者与开关；
- `run_proactive_tick(gateway)` 是唯一对外入口，行为与 `Gateway._proactive_cycle`
  逐字节一致（引擎开关 → `proactive_worker.run_cycle`），供后续把 server 的调用点
  收敛到这里时直接替换；
- `PRIMARY` 标记主链（有且只有一个），`describe_chain()` 给出从主链到风格分流的顺序。

为什么 server 的调用点也收敛到这里：`Gateway._proactive_cycle` 现在直接委托
`run_proactive_tick`（一行），所以「代码里真正在跑的链」就是本登记表的 primary 项，
不会再出现文档写一套、代码跑另一套的漂移。`tests/test_architecture.py` 会静态核对
「server 调用 registry」「registry 全仓库只此一处定义」「登记证据能在代码里搜到」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ProactiveEntry:
    """一条主动链入口的登记项（全部字段都是可静态核对的代码事实）。"""

    name: str                 # 稳定标识（测试/文档引用它）
    status: str               # active=现役；legacy=保留但无现役调用；manual=手动触发
    role: str                 # primary=主链唯一入口；scheduler=主链调度；component=主链一环；
                              # variant=风格分流；legacy/manual
    module: str               # 模块文件（仓库相对路径，便于静态核对）
    entrypoint: str           # 模块内入口（函数名/HTTP 路径/子命令）
    caller: str               # 谁调用它
    switch: str               # 开关/配置（没有就写「无」）
    delegates_to: str = ""    # 下一环（"module.attr" 或说明）
    note: str = ""
    evidence: tuple[str, ...] = field(default_factory=tuple)  # 代码里能搜到的调用证据

    @property
    def path(self) -> str:
        return self.module


#: 主链唯一入口的名称（有且只有一个 primary；测试会断言）
PRIMARY = "gateway_proactive_cycle"

#: 唯一对外入口函数名（测试会断言全仓库只此一处定义）
PUBLIC_ENTRY = "run_proactive_tick"

ENTRIES: tuple[ProactiveEntry, ...] = (
    ProactiveEntry(
        name="gateway_proactive_loop",
        status="active",
        role="scheduler",
        module="agi/wechat_gateway/server.py",
        entrypoint="_proactive_loop",
        caller="Gateway.start() 启动的 gateway-proactive 常驻线程（config: proactive_enabled）",
        switch="proactive_enabled（线程开关）；engine_enabled（引擎停则空转）；"
               "STOP 文件/夜间静默/引擎停止在更外层或更内层再次拦截",
        delegates_to="agi.wechat_gateway.server._proactive_cycle",
        note="主链调度器：随机 60–7200s 间隔，engine 一停立刻醒来空转，不跑 cycle 不发送。",
        evidence=('threading.Thread(', 'target=self._proactive_loop', 'self._proactive_cycle()'),
    ),
    ProactiveEntry(
        name="gateway_proactive_cycle",
        status="active",
        role="primary",
        module="agi/wechat_gateway/server.py",
        entrypoint="_proactive_cycle",
        caller="Gateway._proactive_loop（主链）；测试/其它入口可直接调用",
        switch="engine_enabled（false 时返回 ran=False/engine_stopped）",
        delegates_to="agi.wechat_gateway.proactive_worker.run_cycle",
        note="主链唯一对外入口的现行实现：委托 registry.run_proactive_tick（与旧实现逐字节等价）。",
        evidence=("proactive_registry.run_proactive_tick(self)",),
    ),
    ProactiveEntry(
        name="worker_run_cycle",
        status="active",
        role="component",
        module="agi/wechat_gateway/proactive_worker.py",
        entrypoint="run_cycle",
        caller="Gateway._proactive_cycle",
        switch="config: proactive_enabled / proactive_until_ts / proactive_* 阈值；"
               "STOP 文件（最高优先）；熔断/去重/白名单/静默",
        delegates_to="agi.selflearn.ticker.run_once",
        note="每轮遍历联系人：配置热重载 → STOP → 开关 → 熔断 → 概率/间隔 → "
             "ticker.run_once 生成 → 发送闸门发送；只生成不直接绕过网关发送。",
        evidence=("ticker.run_once(", "from agi.selflearn import ticker"),
    ),
    ProactiveEntry(
        name="ticker_run_once",
        status="active",
        role="component",
        module="agi/selflearn/ticker.py",
        entrypoint="run_once",
        caller="proactive_worker.run_cycle",
        switch="autonomy 配置（state 表 autonomy：enabled/level/quiet_hours/daily_budget）",
        delegates_to="agi.selflearn.proactive.tick",
        note="先过 autonomy.gate 再 tick；gate 不过直接返回。",
        evidence=("gate = autonomy.gate(path, now)", "proactive.tick("),
    ),
    ProactiveEntry(
        name="selflearn_tick",
        status="active",
        role="component",
        module="agi/selflearn/proactive.py",
        entrypoint="tick",
        caller="ticker.run_once（主链）；cli/web 等手动入口也调用它",
        switch="proactive_style=legacy(默认)/simple（config 或 PROACTIVE_STYLE 环境变量）",
        delegates_to="agi.selflearn.proactive._tick_impl → simple_tick.tick_simple（仅 simple 风格）",
        note="生成主动消息正文并落库（messages/proactive），从不直接发送；"
             "内层由 pacing × initiative × urges 门控/选题。",
        evidence=("from .simple_tick import proactive_style, tick_simple",),
    ),
    ProactiveEntry(
        name="simple_tick_style",
        status="active",
        role="variant",
        module="agi/selflearn/simple_tick.py",
        entrypoint="tick_simple",
        caller="proactive._tick_impl（仅 proactive_style=simple 时）",
        switch="proactive_style=simple；默认 legacy",
        delegates_to="",
        note="P6-e 的可回滚分流实验；默认不走，但代码是活的（被 proactive 模块级 import）。",
        evidence=("def tick_simple(",),
    ),
    ProactiveEntry(
        name="selflearn_web_tick",
        status="legacy",
        role="legacy",
        module="agi/selflearn/web.py",
        entrypoint="HTTP /api/proactive/tick",
        caller="python -m agi.selflearn.cli serve（8082 调试页，手动启动）",
        switch="仅手动运行；非主链",
        delegates_to="agi.selflearn.proactive.tick",
        note="旧 selflearn 网页的按需 tick（还带 autonomy.gate）。日常入口是 802 控制台。",
        evidence=('"/api/proactive/tick"',),
    ),
    ProactiveEntry(
        name="ticker_loop",
        status="legacy",
        role="legacy",
        module="agi/selflearn/ticker.py",
        entrypoint="loop",
        caller="无现役调用者（仅旧脚本/测试；主链用 Gateway._proactive_loop）",
        switch="仅手动运行；非主链",
        delegates_to="ticker.run_once",
        note="独立线程循环的旧实现，不参与主链；保留用于对照/测试。",
        evidence=("while not stop.is_set():",),
    ),
    ProactiveEntry(
        name="cli_manual_tick",
        status="manual",
        role="manual",
        module="agi/selflearn/cli.py",
        entrypoint="proactive 子命令",
        caller="python -m agi.selflearn.cli proactive（人工调试）",
        switch="仅手动运行",
        delegates_to="agi.selflearn.ticker.run_once",
        note="手动触发一次主动生成（不发送），用于排查。",
        evidence=("ticker.run_once(path, Client())",),
    ),
)


def active_entries() -> tuple[ProactiveEntry, ...]:
    """现役（status=active）的入口，按主链顺序。"""
    return tuple(entry for entry in ENTRIES if entry.status == "active")


def primary_entry() -> ProactiveEntry:
    """主链入口（有且只有一个；多个就是登记错误）。"""
    primaries = [entry for entry in ENTRIES if entry.role == "primary"
                 and entry.name == PRIMARY]
    if len(primaries) != 1:
        raise RuntimeError(f"主动链主入口登记有误：{PRIMARY} 命中了 {len(primaries)} 条")
    return primaries[0]


def describe_chain() -> list[str]:
    """从主链到风格分流的顺序描述（给文档/控制台用，避免手抄走样）。"""
    ordered = ["gateway_proactive_loop", "gateway_proactive_cycle", "worker_run_cycle",
               "ticker_run_once", "selflearn_tick", "simple_tick_style"]
    by_name = {entry.name: entry for entry in ENTRIES}
    return [f"{by_name[name].module}::{by_name[name].entrypoint}" for name in ordered]


def run_proactive_tick(gateway: Any) -> dict[str, Any]:
    """主动链唯一对外入口（行为等价于 ``Gateway._proactive_cycle``）。

    为什么不直接 import Gateway：网关模块会拉起真实发送/接收依赖，registry 要能被
    文档工具和无副作用的测试 import；这里只依赖传入的 gateway 对象。

    行为与 ``Gateway._proactive_cycle`` 一致：
    1. ``gateway.engine_enabled`` 为假 → 返回 ran=False/engine_stopped，不跑 cycle；
    2. 否则委托 ``proactive_worker.run_cycle(gateway)``（内部仍会二次检查引擎开关、
       STOP、静默、熔断、白名单、概率/间隔，行为不变）。
    """
    if not getattr(gateway, "engine_enabled", True):
        return {"ok": True, "ran": False, "reason": "engine_stopped",
                "contacts": 0, "sent": 0, "drafts": 0, "deduped": 0,
                "clipboard": 0, "skipped": []}
    from agi.wechat_gateway import proactive_worker
    return proactive_worker.run_cycle(gateway)


__all__ = [
    "ENTRIES", "PRIMARY", "PUBLIC_ENTRY", "ProactiveEntry",
    "active_entries", "primary_entry", "describe_chain", "run_proactive_tick",
]