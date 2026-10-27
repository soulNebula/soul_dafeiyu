# 文档总入口（docs/INDEX）

> **唯一入口**：找文档先来这里。`docs/STATUS.md` 给当前真实状态，`docs/ARCHITECTURE.md` 给系统架构，
> `docs/plans/` 给迭代计划与实现记录。根目录不再有平行 plans/；旧文档在 `docs/archive/`，只作历史参考。

最后更新：2026-10-28（文档收口 P1-1）

## 1. 现役文档（active）

| 文档 | 用途 | 必须看的时候 |
| --- | --- | --- |
| [`STATUS.md`](STATUS.md) | **真实测试数字 / 服务清单 / 里程碑 / 已知失败** | 每次开工、交接、写报告前 |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 系统架构（双后端、801/802/8123/8125、主动链） | 改接入/对话/主动/数据层前 |
| [`plans/README.md`](plans/README.md) | 42 份计划/记录的合并索引（15–59，缺 17/27/55） | 找方案、复盘、实施细节 |
| [`specs/README.md`](specs/README.md) | 规格/需求（00–14，含 TrainLab 10/Workbench 11） | 查产品语义与边界 |
| [`README-usage.md`](README-usage.md) | 使用与运维说明（安装、启动、测试、常见问题） | 部署/运行/排查 |
| [`PATHS.md`](PATHS.md) | 数据根/模型/日志路径解析与迁移（AGENT_HOME / paths.json） | 换机器/换盘/排路径问题 |
| [`常见问题.md`](常见问题.md) | FAQ | 用户问“为什么/怎么办” |
| [`网页端使用说明.md`](网页端使用说明.md) | 控制台/网页端使用 | 操作 802 控制台 |
| [`风控与合规说明.md`](风控与合规说明.md) | 风险边界与合规 | 对外说明/安全评审 |
| [`微信接入与自主能力_路线图.md`](微信接入与自主能力_路线图.md) | 微信接入与自主能力路线（历史路线，现役参考） | 讨论接入/主动方向 |
| [`../README.md`](../README.md) | 项目首页/快速开始 | 第一次进项目 |

## 2. 历史/归档（archived，仅参考）

| 文档 | 说明 |
| --- | --- |
| [`archive/20261028-docs-snapshot/`](archive/20261028-docs-snapshot/) | **2026-10-28 收口前全量快照**（docs + 根 plans + agi 根笔记 + MANIFEST） |
| [`archive/agi-notes/`](archive/agi-notes/) | 原根目录 `agi/00-14*.md` 等笔记（已从 `agi/` 移出，避免和 specs 双份口径） |
| [`archive/README-legacy.md`](archive/README-legacy.md) | 旧 wx_box / 8081 网页版 README（历史，不再维护） |
| [`archive/broken-links.md`](archive/broken-links.md) | 历史区域断链说明（active 已 0 断链） |
| [`archive/AGI-repo/`](archive/AGI-repo/) | 旧 AGI 仓库文档快照 |
| [`sources/wx_box/`](sources/wx_box/) | wx_box 来源材料/旧代码/旧文档（参考，不进现役口径） |
| [`analysis/`](analysis/) | AstrBot 等外部项目分析（历史调研） |
| [`主动试跑复盘_20261024.md`](主动试跑复盘_20261024.md) | 一次试跑复盘（历史） |

## 3. 权威顺序（冲突时按此裁决）

1. **当前状态/数字**：`docs/STATUS.md`；
2. **架构/端口/链路**：`docs/ARCHITECTURE.md`；
3. **最新实现记录**：`docs/plans/` 中编号较大的实现/验收文档（30/59 等）；
4. **路线**：`docs/plans/19-整体开发路线.md` 与后续执行记录；
5. **规格**：`docs/specs/`；
6. **历史/来源**：`docs/archive/`、`docs/sources/` 只作背景，不覆写现役结论。

## 4. 维护规则

- 新文档放 `docs/`（计划放 `docs/plans/`，规格放 `docs/specs/`），不要另起根目录平行 plans。
- 新增/移动文档后跑 `python tools/check_docs_links.py --fix`，断链必须为 0。
- `docs/archive/20261028-docs-snapshot/` 视为不可变快照，不再修改。
- README 里的测试数字只作示例，**真实数字以 STATUS.md 为准**。
