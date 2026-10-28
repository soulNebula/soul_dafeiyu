# 文档总入口（docs/INDEX）

> **唯一入口**：找文档先来这里。`docs/STATUS.md` 给可复现的当前状态，
> `docs/ARCHITECTURE.md` 给按代码现状写的系统架构，`docs/plans/` 给迭代计划与实现记录。
> 根目录没有平行 plans/；旧文档在 `docs/archive/`，只作历史参考。

最后更新：2026-10-29（架构收敛 + 文档=事实）

## 1. 现役文档（active）

| 文档 | 用途 | 什么时候看 |
| --- | --- | --- |
| [`STATUS.md`](STATUS.md) | **可复现测试数字（collect/实测两口径）、端口清单、里程碑、已知问题** | 每次开工、交接、写报告前 |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 系统架构（双后端、801/802 主链、发送闸门、主动链登记、数据层） | 改接入/对话/主动/数据层前 |
| [`plans/README.md`](plans/README.md) | **43 份**计划/记录的合并索引（15–60，缺 17/27/55） | 找方案、复盘、实施细节 |
| [`specs/README.md`](specs/README.md) | 规格/需求（00–14，含 TrainLab 10/Workbench 11） | 查产品语义与边界 |
| [`README-usage.md`](README-usage.md) | 使用与运维说明（安装、启动、测试、常见问题） | 部署/运行/排查 |
| [`PATHS.md`](PATHS.md) | 数据根/模型/日志路径解析与迁移（AGENT_HOME / paths.json） | 换机器/换盘/排路径问题 |
| [`常见问题.md`](常见问题.md) | FAQ | 用户问「为什么/怎么办」 |
| [`网页端使用说明.md`](网页端使用说明.md) | 控制台/网页端使用 | 操作 802 控制台 |
| [`风控与合规说明.md`](风控与合规说明.md) | 风险边界与合规 | 对外说明/安全评审 |
| [`OPENSOURCE.md`](OPENSOURCE.md) | 开源发布流程与数据剥离（declared vs 实际能力一致） | 发版/开源前 |
| [`微信接入与自主能力_路线图.md`](微信接入与自主能力_路线图.md) | 微信接入与自主能力路线（历史路线，现役参考） | 讨论接入/主动方向 |
| [`../legacy/README.md`](../legacy/README.md) | **死代码归档清单与恢复方式**（只 mv 不删） | 想动「看起来没人用」的代码前 |
| [`../README.md`](../README.md) | 项目首页/快速开始/能力边界（已实现·未完成·外部依赖） | 第一次进项目 |

## 2. 工具（文档事实的复算入口）

| 命令 | 作用 |
| --- | --- |
| `python scripts/agentb_verify.py` | **全量终验**：两套测试 + 静态检查（链接/可移植性/密钥/架构）+ 生产文件未被测试污染，输出「套件\|通过\|失败\|跳过\|耗时\|结果」证据表；`--quick` 跳过 agi/tests |
| `python tools/status_report.py` | 现场算测试 collect 数 / 代码规模 / 归档量；`--from-agentb <json>` 导入终验数字 |
| `python tools/status_report.py --run` | 真跑两套测试并回填 `STATUS.md` 的 `STATUS:AUTO` 区块 |
| `python tools/status_report.py --check` | 文档数字过期则退出 1 |
| `python tools/check_architecture.py` | 死代码回归 / agi import 环 / 行数预算 / 分层违例（退出 0 = 通过） |
| `python tools/check_docs_links.py` | `docs/` 内相对链接断链检查（断链退出 1） |
| `python tools/check_secrets.py` | 提交前密钥扫描（CI 与 release_check 都跑） |
| `python tools/check_portability.py` | 硬编码本机路径/绝对路径扫描 |
| `python tools/rotate_secrets.py` | 把明文密钥从 `config.json`/`web_config.json` 迁到 `agi/data/secrets.json` 或环境变量（默认 dry-run） |
| `python tools/prune_chat_logs.py` | 聊天/媒体留存 TTL 清理（默认 dry-run，`--apply` 才删） |
| `python scripts/release_check.py` | 发布闸门（必备文件 + 密钥 + 大文件 + 全量离线测试） |

`docs/status_measured.json` 是 `status_report.py --run` / `--from-agentb` 的落盘结果（实测数字的唯一来源），
`STATUS.md` 自动区块由它生成，**不要手改**；命令与口径在 `README.md` 第 6 节和 `STATUS.md` 第 0/6 节。

## 3. 历史/归档（archived，仅参考）

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
| [`../legacy/`](../legacy/) | 死代码归档（Demo 旧副本、dev 脚本、scratch、审计分片） |

## 4. 权威顺序（冲突时按此裁决）

1. **当前状态/数字**：`docs/STATUS.md`（自动区块由工具生成，优先于任何手写数字）；
2. **架构/端口/链路**：`docs/ARCHITECTURE.md`；
3. **最新实现记录**：`docs/plans/` 中编号较大的实现/验收文档；
4. **路线**：`docs/plans/19-整体开发路线.md` 与后续执行记录；
5. **规格**：`docs/specs/`；
6. **历史/来源**：`docs/archive/`、`docs/sources/` 只作背景，不覆写现役结论。

## 5. 维护规则

- 新文档放 `docs/`（计划放 `docs/plans/`，规格放 `docs/specs/`），不要另起根目录平行 plans。
- 新增/移动文档后跑 `python tools/check_docs_links.py --fix`，断链必须为 0。
- 改代码链路后同步改 `ARCHITECTURE.md`，并在 `tests/test_architecture.py` 有对应断言时一起改。
- `docs/archive/20261028-docs-snapshot/` 视为不可变快照，不再修改。
- 测试数字只从 `STATUS.md` 自动区块取；README 里不写具体 passed 数，只写复算命令。