# 记忆指标（P1-g）

> 目的：让「记忆是否在退化」能被看见。P1-a~P1-f 叠了去重合并、重要性/衰减、时间分层、
> 月 gist、冷归档、时间模糊六层机制；本文件定义观测口径、健康阈值和处理建议。

## 一句话口径

- `messages`：原文层（冷归档只加标记，不删不改写）。
- `memories`：蒸馏层（active 才是"在用"的；dormant 检索不到但可恢复）。
- `retrieval`：检索层调用/命中（内存缓冲，批量落盘）。
- `context`：最近一次 prompt 预算裁剪（P1-f）。
- `archive`：冷归档与最近一次导出。

## 指标清单

| 字段 | 含义 | 采集方式 |
|---|---|---|
| `messages.total / archived / active` | 原文总数 / 已冷归档 / 活跃层 | `messages.archived_at`（P1-d） |
| `messages.oldest_active_days` | 最老活跃原文距今天数 | 无 active 时为 `null` |
| `messages.newest_days` | 最新原文距今天数 | 0 = 刚刚 |
| `memories.total` | 全部蒸馏记忆（含 dormant/invalid/conflict/pending） | `user_memories` |
| `memories.by_status` | 按 status 计数（active/dormant/invalid/conflict/pending） | 状态机（P1-a） |
| `memories.by_kind` | 按 kind（fact/preference/event/plan/value/insight） | 抽取侧 |
| `memories.by_tier` | **active** 记忆按创建时间的 hot(≤30d)/warm(30–90d)/cold(>90d) | P1-b 分层 |
| `memories.avg/median_importance` | active 记忆重要性均值/中位数 | P1-a importance |
| `memories.dormant_rate` | dormant / 全部记忆 | 衰减/休眠（P1-a） |
| `memories.merged_within_30d` | 30 天内发生过合并的 active 记忆数（evidence_history 长度>1） | 去重合并（P1-a） |
| `claims.total` | claims 条数 | 主张抽取 |
| `summaries.total / monthly / latest_month` | 摘要总数 / 月 gist 数 / 最新月 gist 月份 | P1-c |
| `retrieval.calls / hits / items` | 检索调用数 / 有真实重叠命中的调用数 / 返回条数累计 | P1-b 检索 |
| `retrieval.hit_rate` | hits / calls；0 调用时为 `null` | 同上 |
| `retrieval.avg_items` | items / calls | 同上 |
| `retrieval.dropped_stale` | 分层预算丢弃（老/次相关候选）累计 | `retrieve_tiered` 的 `dropped` |
| `retrieval.pending` | 尚未落盘的内存缓冲调用数（0–19） | 见"开销取舍" |
| `context.tokens / dropped / at` | 最近一次 `context_budget` 事件的 token 数/被丢段/时间 | P1-f，`raw_events` |
| `archive.total / exported_at / last_export_rows` | 冷归档总数 / 最近导出时间 / 行数 | P1-d |

## 健康阈值（nightly 每日检查，连续失败会出现在 `nightly.run` 的 `memory_warnings`）

| 条件 | 含义 | 处理建议 |
|---|---|---|
| `dormant_rate > 40%` | 休眠记忆过多，知识被"忘"得太快 | 检查 `memory.decay` 的衰减系数/importance 初始化；确认 `last_accessed` 是否在正常更新（`retrieve_tiered` 命中会更新） |
| `active messages` 一周净减 > 30% | 原文层一周内大量被归档 | 检查 `archive_cold(days=90)` 是否被误调用/阈值被改小；归档不可逆地退出默认检索，但数据仍在（`export_archive` 可冷备） |
| `retrieval.hit_rate < 5%`（calls>0） | 检索几乎从不命中 | 检查 `retrieve_tiered` 分层预算是否被调小、query 是否被过度截断、记忆是否都进了 dormant；冷启动 calls 少时该告警可能误报，连续多日才值得处理 |

## 用法

### 接口（只读）

selflearn web 服务只绑定 `127.0.0.1`，`/api/memory/stats` 额外校验 Host 必须是回环地址
（防 DNS rebinding），**只返回计数，不回传真实昵称/消息内容**。

```bash
curl -s "http://127.0.0.1:8082/api/memory/stats?contact=朋友A" | python -m json.tool
```

- `contact` 只是回显的 alias（用于多联系人看板），不会因此打开任意文件。
- 返回：上面的指标 dict + `contact_alias`。

### CLI

```bash
python -m agi.selflearn.metrics --path agi/data/selflearn/wechat_contacts/<hash>.sqlite3 --json
# 写当天快照并跑三条阈值：
python -m agi.selflearn.metrics --path <db> --snapshot --json
```

### 每日快照

`nightly.run` 的 `steps["memory_metrics"]`：把当天指标写到
`agi/data/selflearn/metrics/<YYYY-MM-DD>.json`（原子覆盖、同日幂等），
并用最近 7 天快照检测上面三条阈值，结果在 `nightly.run` 返回的
`memory_warnings` / `steps.memory_metrics.warnings` 里。

## 开销取舍（重要）

检索计数**不**在每次检索时写库：`retrieve_tiered` 只在进程内累加缓冲，
每 **20 次** 调用或 nightly 时通过 `memory.flush_retrieval_counters()` 批量写
`state` 表（`mem_retrieval_calls/hits/items/dropped_stale`）。

- 每轮对话因此不会为指标多一次写盘；`metrics()` 读取时会自动把未落盘的 `pending` 计入。
- 代价：进程崩溃最多丢 19 次检索计数（可观测性指标，可接受）。
- `retrieve_tiered` 命中本身仍会更新 `access_count/last_accessed`（这是 P1-a 的语义，不属于指标开销）。

## 已知限制

- `by_tier` 只统计 active（dormant 单独在 `by_status`），所以 `by_tier` 之和 ≤ active 数。
- `context` 只保留**最近一次**预算事件；想看历史请读 `raw_events` 的 `context_budget` 行。
- `merged_within_30d` 依赖 `evidence_history` 长度>1，迁移前的老数据（历史被回填成 1 条）不会计入。
- 指标是"计数/分布"，不含任何记忆正文；排查具体内容请走管理界面/CLI `memory.list_user_memories`。
