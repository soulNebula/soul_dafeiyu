# Batch 2 接口契约（06 Learn Loop）

> 只用于 Batch 2；每个 Agent 只改自己文件。
> 项目 Python：`C:/Python314/python.exe`。

## 已有表

```text
skill_nodes(id,name,level,status,provenance,practice_log,fail_count,updated_at)
learn_tasks(id,skill_id,goal,status,attempts,last_error,created_at)
skill_cards(id,skill_id,content,example,provenance_json,status,created_at)
practice_runs(id,task_id,attempt,passed,error,created_at)
```

## Learn Loop 八步

```text
缺口 → 拆解 → 检索来源 → 制卡/例子 → 练习 → verifier → 反思 → 解锁 L3
```

硬规则：
- 没有 provenance 的卡不能写入；
- 没有 verifier 的练习不能升级；
- 至少 3 次练习、最后一次/累计通过，才允许 L3；
- 只升不降；失败记录 last_error 与 fail_count。

## Agent A：`learn_loop.py`（只改这个 + test_learn_loop.py）

```python
def open_gap(path, skill_id, goal) -> str
def record_source(path, skill_id, url, title, quote, fetched_at=None) -> dict
def write_card(path, skill_id, content, example, provenance_ids=None) -> dict
def record_practice(path, task_id, attempt, passed, error="") -> dict
def verify_and_unlock(path, task_id, passed, note="") -> dict
def status(path, skill_id=None) -> dict
```

要求：
- 调 `skilltree.unlock` 只在通过且练习>=3；不能直接写 level；
- provenance 存 `skill_nodes.provenance` JSON，卡必须有来源；
- 测试 fake 数据，不联网、不跑真实代码。

## Agent B：`research.py`（只改这个 + test_research.py）

```python
def fetch_text(url, timeout=10) -> dict  # {ok,url,title,text,error}
def extract_quote(text, keyword, max_len=200) -> str
```

要求：
- urllib 抓正文，HTML 去标签、去脚本；超时/错误 fail-closed；
- 测试本地 HTTP server（127.0.0.1 随机端口），不访问外网；
- 不做搜索 API，不猜 URL。

## Agent C：`verifier.py`（只改这个 + test_verifier.py）

```python
def verify_python(code: str, test_code: str = "", timeout=5) -> dict
def verify_answer(answer: str, expected_terms: list[str]) -> dict
```

要求：
- Python verifier 用子进程执行临时文件，超时 kill，返回 passed/error/stdout/stderr；不联网；
- 禁止执行不可信的生产代码？测试只跑简单数学；失败不抛异常；
- 答案 verifier 只做关键词/要点检查，明确是弱验证。

## 验收

Agent 跑自己的测试；Lead 集成后跑全量。不要改 `db.py`/`claims.py`/`gates.py`/`search.py`/`chat.py`。
