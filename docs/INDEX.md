# 文档总入口（docs/INDEX）

> **唯一入口**：找文档先来这里。`docs/quality/STATUS.md` 给可复现的当前状态，
> `docs/architecture/ARCHITECTURE.md` 给按代码现状写的系统架构，`docs/plans/` 给迭代计划与实现记录。
> 目录职责见 [`STRUCTURE.md`](STRUCTURE.md)。

最后更新：2026-10-29（文档目录整理）

## 1. 现役文档（active）

### 架构与设计

| 文档 | 用途 | 什么时候看 |
| --- | --- | --- |
| [`architecture/ARCHITECTURE.md`](architecture/ARCHITECTURE.md) | 系统架构（双后端、801/802 主链、发送闸门、主动链登记、数据层） | 改接入/对话/主动/数据层前 |
| [`architecture/A9-发送异步化.md`](architecture/A9-发送异步化.md) | A9 发送异步化 P0–P3：链路、开关与回滚、观测、压测/灰度证据 | 改发送/并发模型前 |
| [`architecture/README.md`](architecture/README.md) | 设计规格索引（00–14，含 TrainLab 10 / Workbench 11） | 查产品语义与边界 |
| [`architecture/PLAN.md`](architecture/PLAN.md) | 设计规格的施工图入口 | 实现前对照规格 |
| [`architecture/SPEC.md`](architecture/SPEC.md) | 项目规格总表 | 查完整需求/接口 |
| [`architecture/小鱼-AI伴侣-技术与实践.pdf`](architecture/小鱼-AI伴侣-技术与实践.pdf) | 项目技术文章（PDF，含 7 张配图；源文件在 `architecture/source/`） | 对外介绍/答辩 |
| [`architecture/architecture.png`](architecture/architecture.png) | 架构图 | 快速理解分层 |

### 使用与运维（guides）

| 文档 | 用途 |
| --- | --- |
| [`guides/新用户上手.md`](guides/新用户上手.md) | **从 clone 到第一条消息**：依赖/发送器 venv/API key/白名单/可选能力/排障 |
| [`guides/README-usage.md`](guides/README-usage.md) | 早期 legacy wx_box 查看器使用说明（归档；现行入口见 README/桌面版） |
| [`guides/桌面版.md`](guides/桌面版.md) | Windows 桌面版：快捷方式/托盘/自启/排障（M5） |
| [`guides/PATHS.md`](guides/PATHS.md) | 数据根/模型/日志路径解析与迁移 |
| [`guides/常见问题.md`](guides/常见问题.md) | FAQ |
| [`guides/网页端使用说明.md`](guides/网页端使用说明.md) | 旧版 8081 网页端使用说明（现行 802 控制台见 `桌面版.md`/README） |
| [`../idea/README.md`](../idea/README.md) | 803 工作台：语料清洗/用户标注/蒸馏/训练/模型中心 |
| [`guides/风控与合规说明.md`](guides/风控与合规说明.md) | 风险边界与合规 |
| [`guides/微信接入与自主能力_路线图.md`](guides/微信接入与自主能力_路线图.md) | 微信接入与自主能力路线（历史路线，现役参考） |
| [`guides/OPENSOURCE.md`](guides/OPENSOURCE.md) | 开源发布流程与数据剥离 |
| [`guides/MULTIGPU.md`](guides/MULTIGPU.md) | 多卡设备接入方案（方案稿） |

### 质量与状态（quality）

| 文档 | 用途 |
| --- | --- |
| [`quality/STATUS.md`](quality/STATUS.md) | **可复现测试数字、端口清单、里程碑、已知问题** |
| [`quality/status_measured.json`](quality/status_measured.json) | 状态工具落盘的实测数字（唯一来源） |
| [`quality/agentb_verify_result.json`](quality/agentb_verify_result.json) | Agent B 全量终验结果 |
| [`agent-b/SCORECARD.md`](agent-b/SCORECARD.md) | 质量维度评分卡（按约定保留在 agent-b/ 原样） |
| [`agent-b/CHANGES.md`](agent-b/CHANGES.md) | 本轮改动说明与部署前动作 |

### 计划（plans）

| 文档 | 用途 |
| --- | --- |
| [`plans/README.md`](plans/README.md) | **46 份**计划/记录的合并索引（15–63，缺 17/27/55） |
| `plans/15-…` ~ `plans/63-…` | 决策、执行记录、实现/验收细节 |
| [`plans/63-产品化路线图-V1-V3.md`](plans/63-产品化路线图-V1-V3.md) | 产品化规划：一键安装、803 bot 向导、802 登录、本地+云端、训练闭环（**规划稿，未开工**） |

### 项目根文档

| 文档 | 用途 |
| --- | --- |
| [`../README.md`](../README.md) | 项目首页/快速开始 |
| [`../SECURITY.md`](../SECURITY.md) | 安全披露 |
| [`../CONTRIBUTING.md`](../CONTRIBUTING.md) | 贡献指南 |
| [`../legacy/README.md`](../legacy/README.md) | 死代码归档清单与恢复方式 |

## 2. 工具（文档事实的复算入口）

| 命令 | 作用 |
| --- | --- |
| `python scripts/agentb_verify.py` | 全量终验：两套测试 + 静态检查，输出证据表 |
| `python tools/status_report.py` | 现场算 collect 数 / 代码规模 / 归档量；`--run` 真跑测试；`--check` 检查过期 |
| `python tools/check_architecture.py` | 死代码回归 / import 环 / 行数预算 / 分层检查 |
| `python tools/check_docs_links.py` | `docs/` 内相对链接断链检查 |
| `python tools/check_secrets.py` | 提交前密钥扫描 |
| `python tools/check_portability.py` | 硬编码本机路径扫描 |
| `python scripts/release_check.py` | 发布闸门（必备文件 + 密钥 + 大文件 + 离线测试） |

## 3. 历史/归档（archived，仅参考）

| 文档 | 说明 |
| --- | --- |
| [`archive/20261028-docs-snapshot/`](archive/20261028-docs-snapshot/) | 2026-10-28 收口前全量快照 |
| [`archive/agi-notes/`](archive/agi-notes/) | 原根目录 agi 笔记（历史，不再维护） |
| [`archive/AGI-repo/`](archive/AGI-repo/) | 旧 AGI 仓库文档快照 |
| [`archive/README-legacy.md`](archive/README-legacy.md) | 旧 wx_box / 8081 网页版 README |
| [`archive/broken-links.md`](archive/broken-links.md) | 历史区域断链说明 |
| [`archive/analysis/`](archive/analysis/) | 外部项目/仓库健康分析（历史调研） |
| [`archive/sources/`](archive/sources/) | wx_box 来源材料/旧代码/旧文档（参考） |
| [`archive/主动试跑复盘_20261024.md`](archive/主动试跑复盘_20261024.md) | 一次试跑复盘（历史） |
| [`archive/WeChat-Hook-项目分析.md`](archive/WeChat-Hook-项目分析.md) | 旧微信 Hook 调研（历史） |
| [`archive/示例_聊天记录.txt`](archive/示例_聊天记录.txt) | 示例聊天记录 |
| `reports/archive/star/`（仓库根） | 根目录 star 6 份报告，移到仓库级归档 |
| `reports/archive/analysis/`（仓库根） | 根目录 analysis 4 份审计报告，移到仓库级归档 |

## 4. 权威顺序（冲突时按此裁决）

1. **当前状态/数字**：`quality/STATUS.md`（自动区块优先于手写数字）；
2. **架构/端口/链路**：`architecture/ARCHITECTURE.md`；
3. **最新实现记录**：`plans/` 中编号较大的实现/验收文档；
4. **路线**：`plans/19-整体开发路线.md` 与后续执行记录；
5. **规格**：`architecture/` 下 00–14 与 SPEC；
6. **历史/来源**：`archive/`、`reports/archive/` 只作背景，不覆写现役结论。

## 5. 维护规则

- 新文档按 [`STRUCTURE.md`](STRUCTURE.md) 放：架构/设计进 `architecture/`，使用/运维进 `guides/`，测试/验收进 `quality/`，计划进 `plans/`，过时稿进 `archive/`。
- `docs/agent-b/` 按约定保留原样，不移动、不改写。
- 根目录只保留 `README.md`、`SECURITY.md`、`CONTRIBUTING.md` 与代码/配置；报告类杂物放 `reports/archive/`。
- 移动/新增文档后跑 `python tools/check_docs_links.py --fix`，active 文档断链必须为 0。
- 测试数字只从 `quality/STATUS.md` 自动区块取，README 不写具体 passed 数。
