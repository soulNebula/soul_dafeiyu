# Batch 4 接口契约（S5 表情包 + 04 评分/情境桶/提案）

> 只用于本批；每个 Agent 只改自己的文件。测试 Python：`C:/Python314/python.exe`。

## 已有表

```text
emoji_archive(id,name,path,tags_json,usage_count,created_at)
emoji_usage(id,emoji_id,message_id,context,created_at)
score_records(id,turn_id,bucket,raw,score,breakdown_json,created_at)
learning_proposals(id,kind,target,payload_json,evidence_json,status,created_at,updated_at)
turns(id,...,feedback,score,created_at)
```

## Agent A：`emoji.py`（S5）

```python
def index_assets(path, asset_dir, limit=200) -> dict
def record_use(path, emoji_name, message_id="", context="") -> dict
def stats(path) -> dict
def list_emojis(path, limit=50) -> list[dict]
```

要求：
- 扫描本地目录 png/jpg/gif/webp，按文件名做 name，幂等写入 emoji_archive；
- 只索引已有本地资产，不下载/不联网；usage_count 从 emoji_usage 统计或增量；
- 测试临时目录（生成小图片），不读旧客户数据。

## Agent B：`scoring.py`（04 情境桶/评分）

```python
def bucket_of(*, stage="陌生", mood="neutral", topic="生活") -> str
def score_turn(signals: dict) -> dict
def record_score(path, turn_id, signals) -> dict
```

要求：
- 三维桶：关系阶段 / 情绪极性 / 话题类型，共 60 桶；
- 加权信号：显式0.30、行为0.25、长度0.10、情绪0.15、留存0.15、复发-0.10、裁判0.20；sigmoid；
- 分数只与同桶 baseline 比（无 baseline 时返回 score_delta=None）；
- 记录 breakdown；测试纯函数 + 临时 DB。

## Agent C：`proposals.py`（04 proposal 状态机）

```python
def create(path, kind, target, payload, evidence=None) -> dict
def transition(path, proposal_id, action) -> dict
def list_proposals(path, status=None) -> list[dict]
```

要求：
- 状态：draft→gray→merged / rejected；只允许合法转换；
- 合并/灰度必须人审，不自动改 core；记录时间；
- 测试临时 DB，不联网。

## 验收

Agent 只跑自己的测试；Lead 集成到 acceptance/CLI 后跑全量。不要改 `db.py`/`chat.py`/其他模块。
