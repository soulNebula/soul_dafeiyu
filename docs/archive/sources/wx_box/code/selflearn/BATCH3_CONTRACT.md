# Batch 3 接口契约（S0/S1/S2 + S3 + S9）

> 只用于本批；每个 Agent 只改自己的文件。测试 Python：`C:/Python314/python.exe`。
> 不使用旧客户数据；测试只用临时/合成数据。

## 已有表

```text
raw_events(id,source,source_id,friend,sender,kind,text,ts,meta_json)
sessions(id,friend,start_ts,end_ts,message_count,status)
session_segments(id,session_id,seq,start_ts,end_ts,text,meta_json)
media_understanding(id,attachment_id,path,caption,tags_json,emotion,confidence,status,created_at)
user_profile(id,dimension,value,evidence_json,confidence,status,updated_at)
```

## Agent A：`ingest.py`（S0/S1/S2）

```python
def ingest_jsonl(path, jsonl_path, *, source="jsonl") -> dict
def clean_event(raw: dict) -> dict | None
def build_sessions(path, *, friend=None, gap_seconds=1800) -> list[dict]
def segment_session(path, session_id, *, max_chars=1200) -> list[dict]
def stats(path) -> dict
```

要求：
- JSONL 字段：friend/sender/kind/text/ts/id；缺 ts 用 0 但 `clean_event` 可拒绝；空文本/系统/非文本按 kind 保留但 session 只用 text；
- 清洗归一空白、去重按 source_id/id；写入 raw_events 幂等；
- session 按 friend + 时间间隔 gap_seconds 切；segment 按字符上限切、保留 start/end；
- 测试临时 JSONL，不读旧数据库、不联网。

## Agent B：`image_understanding.py`（S3）

```python
def describe_image(path, client, attachment_id="", existing_caption="") -> dict
def structured_caption(path, client) -> dict
def list_media(path, limit=20) -> list[dict]
```

要求：
- 可注入 fake client；输出 caption/tags/emotion/confidence，写 media_understanding；
- 只接受 image 文件/本地路径；无 client 时有 fallback `{ok:false}`；
- 不联网、不调用 wxauto；测试用 fake client。
- 不要改 chat.py。

## Agent C：`profile.py`（S9）

```python
DIMENSIONS = ("work","city","family","age","school","health","interests","values","style","goals")
def distill(path, client) -> dict
def get_profile(path) -> dict
```

要求：
- 从 user_memories/claims 取证据，LLM 输出每维 value + evidence_memory_ids；每维至少 1 条 evidence；不确定跳过；
- 写 user_profile，保留 confidence/status；没证据不编；
- 测试 fake client；不联网。

## 验收

Agent 只跑自己的测试；Lead 集成到 `acceptance`/CLI 后跑全量。不要改 `db.py`/`chat.py`/`claims.py`/`gates.py`/`search.py`。
