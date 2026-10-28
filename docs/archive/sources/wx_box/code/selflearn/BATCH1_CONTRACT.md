# Batch 1 接口契约（S6/S7/S8）

> 只在这批使用；不要让 Agent 改其他人的文件。
> Python 版本：项目用 3.14；测试用 `C:/Python314/python.exe`。

## 共享 DB

`agi/selflearn/db.py` 已有：

```text
claims(id, subject, predicate, object, polarity, time_ref, claim_type,
       evidence_span, evidence_message_id, confidence, source_level,
       status, conflict_group, created_at)
messages(id, role, text, ts, proactive)
user_memories(...)
learn_tasks(...)
```

## S6 `claims.py`（Agent A 负责）

```python
@dataclass
class Claim:
    id: str
    subject: str
    predicate: str
    object: str
    polarity: int          # 1 / -1
    time_ref: str
    claim_type: str        # fact|preference|event|plan|relation|inference
    evidence_span: str
    evidence_message_id: str
    confidence: float
    source_level: str      # A 用户原话 / B 规则 / C 模型推断

def normalize_predicate(predicate: str) -> str
def extract_claims(path, client, text, message_id) -> list[Claim]
```

硬规则：
- 每条 `evidence_span` 必须是 `text` 子串；
- 抽不出原文 → 丢弃；
- 模型推断标 `source_level="C", claim_type="inference"`；
- 一次最多 3 条 inference；
- 只测 `claims.py` 和 `tests/test_claims.py`。

## S7 `gates.py`（Agent B 负责）

```python
def gate_claim(claim: Claim, existing: list[dict]) -> dict:
    # {"result": "accept"|"reject"|"pending"|"merge"|"conflict",
    #  "reason": str, "conflict_with": str|None}
```

必须实现：
- 证据存在 + evidence_span 原文匹配；
- source_level C 不进事实层；confidence < .6 进 pending；
- 敏感信息（身份证/银行卡/手机号/密码/住址）拒绝；
- 同 subject+predicate+object 合并；
- 同 subject+predicate 不同 object（或偏好正负相反）→ conflict；
- 只测 `gates.py`/`tests/test_gates.py`，不要改 `claims.py`。

## S8 `search.py`（Agent C 负责）

```python
def index_texts(path, items: list[dict]) -> int
def search(path, query: str, limit: int = 8) -> list[dict]
```

实现 SQLite FTS5（中文 bigram 切分，ASCII 词保留），返回 `source_type/source_id/text/score`；
没有 FTS5 时降级 bigram 扫描，不崩；测试 `tests/test_search.py`。
不要改其他文件。

## 验收

各 Agent 只跑自己的测试；Lead 集成后跑全量 `pytest agi/tests`。
