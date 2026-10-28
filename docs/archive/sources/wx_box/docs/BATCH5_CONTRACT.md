# Batch 5 接口契约（09–14 细化）

> 只用于本批；每个 Agent 只改自己的文件。测试 Python：`C:/Python314/python.exe`。

## 已有

```text
state(key,value,updated_at)              # companion_state / trust
trust_events(id,kind,delta,reason,turn_id,resolved,created_at)
skills/variants/weak_checks              # 现有弱验证
intents(id,kind,text,wake_at,status,reason,created_at)
weak_rechecks(id,skill_id,variant_id,passed,note,created_at)
wake_runs(id,kind,status,detail_json,started_at,finished_at)
```

## Agent A：`affect.py`（09/10）

```python
def snapshot(path) -> dict
def update_from_message(path, text, *, feedback="") -> dict
def decay(path, elapsed_seconds) -> dict
def grudge_context(path) -> str
```

要求：
- 维护 mood/energy/sharing/battery/trust；反馈影响；时间衰减；
- 未解决负面事件形成 grudge 摘要，但不得鼓励报复；trust 修复走现有 trust；
- 测试纯规则+临时 DB，不联网。

## Agent B：`initiative.py`（11/12）

```python
def desire(path, now=None) -> dict
def candidates(path, now=None) -> list[dict]
def decide(path, now=None) -> dict
```

要求：
- 由 mood/sharing/trust/未解决话题/空闲共同算分；不是只按用户空闲；
- 输出候选与分数、是否主动、最多几条（情绪好/分享欲高 1-3，低则 0）；
- **只生成，不发送**；测试临时 DB。

## Agent C：`weak_validation.py` + `wake.py`（13/14）

```python
# weak_validation.py
def schedule_recheck(path, skill_id, reason="") -> dict
def run_recheck(path, skill_id, passed, note="") -> dict
def status(path) -> dict

# wake.py
def add(path, kind, text, wake_at, priority=0.5, reason="") -> str
def due(path, now=None, budget=5) -> dict
def run_due(path, now=None, budget=5) -> dict
```

要求：
- 弱验证复测写 weak_rechecks，失败可标记回退但不直接改 level；
- wake 有预算/优先级/去重/运行审计，不自动发微信；
- 测试临时 DB，不联网。

## 验收

Agent 只跑自己的测试；Lead 集成后全量测试。
