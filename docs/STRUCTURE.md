# 文档结构（docs/STRUCTURE）

> 目标：单一入口、按主题归类；`docs/` 下只保留 5 个职责目录 + 入口文件。
> 根目录只保留 `README.md`、`SECURITY.md`、`CONTRIBUTING.md` 与代码/配置。

最后更新：2026-10-29

## 最终目录树

```
SoulAgent/
├── README.md                 项目首页
├── SECURITY.md               安全披露
├── CONTRIBUTING.md           贡献指南
├── docs/
│   ├── INDEX.md              唯一文档入口（按主题索引）
│   ├── STRUCTURE.md          本文件：目录职责与放文档规则
│   ├── architecture/         架构、设计规格、技术文章
│   │   ├── ARCHITECTURE.md
│   │   ├── README.md         00–14 设计规格索引
│   │   ├── PLAN.md           设计规格的施工图入口
│   │   ├── SPEC.md           项目规格总表
│   │   ├── architecture.png  架构图
│   │   ├── 小鱼-AI伴侣-技术与实践.md
│   │   └── 00-… ~ 14-…       设计规格正文
│   ├── guides/               部署、启动、操作、FAQ、合规、开源
│   │   ├── README-usage.md
│   │   ├── PATHS.md
│   │   ├── 常见问题.md
│   │   ├── 网页端使用说明.md
│   │   ├── 风控与合规说明.md
│   │   ├── 微信接入与自主能力_路线图.md
│   │   ├── OPENSOURCE.md
│   │   └── MULTIGPU.md
│   ├── quality/              状态、测试数字、验收证据
│   │   ├── STATUS.md
│   │   ├── status_measured.json
│   │   └── agentb_verify_result.json
│   ├── plans/                迭代计划与执行记录（15–60）
│   ├── archive/              历史文档/快照/旧报告
│   │   ├── 20261028-docs-snapshot/
│   │   ├── agi-notes/
│   │   ├── AGI-repo/
│   │   ├── analysis/
│   │   ├── sources/
│   │   ├── README-legacy.md
│   │   ├── broken-links.md
│   │   ├── 主动试跑复盘_20261024.md
│   │   ├── WeChat-Hook-项目分析.md
│   │   └── 示例_聊天记录.txt
│   └── agent-b/              按约定原样保留（SCORECARD/CHANGES）
├── reports/
│   └── archive/              根目录历史报告（star/、analysis/）
└── …代码/配置（不动）
```

## 每个目录的职责

| 目录 | 放什么 | 不放什么 |
| --- | --- | --- |
| `docs/` | `INDEX.md`、`STRUCTURE.md` | 不放零散主题文档、旧报告 |
| `docs/architecture/` | 系统架构、设计规格/需求、架构图、技术文章 | 使用步骤、测试数字、历史复盘 |
| `docs/guides/` | 安装/启动/操作/FAQ/路径/合规/开源发布 | 架构决策、历史方案 |
| `docs/quality/` | 当前状态、可复现测试数字、终验/验收证据 | 计划正文、部署手册 |
| `docs/plans/` | 按编号的决策/执行记录/实现方案（15–60） | 产品规格原文、旧项目快照 |
| `docs/archive/` | 过时文档、旧报告、历史快照、旧项目材料 | 现役口径、会继续更新的文档 |
| `docs/agent-b/` | Agent B 的 SCORECARD/CHANGES，**原样保留** | 不要移动/改写 |
| `reports/archive/` | 从根目录移出的历史报告（star、analysis） | 现役测试/验收数字 |

## 新文档放哪

| 新文档类型 | 放置位置 |
| --- | --- |
| 架构变更、模块设计、设计规格 | `docs/architecture/` |
| 部署/启动/运维/操作/FAQ/合规/开源 | `docs/guides/` |
| 测试报告、验收数字、质量评分 | `docs/quality/` |
| 迭代计划、执行记录、实现方案 | `docs/plans/<编号>-<标题>.md` |
| 过时文档、一次性复盘、旧报告 | `docs/archive/` |
| 根目录产生的临时报告 | `reports/archive/` |

## 维护约定

1. 新文档先查 `docs/INDEX.md`，再按本表放置；
2. 移动文档后更新 `INDEX.md` 链接，并跑 `python tools/check_docs_links.py`；
3. 不新增根目录平行 `plans/`/`specs/`/`analysis/`；
4. `docs/agent-b/` 不动；`docs/archive/20261028-docs-snapshot/` 视为不可变快照；
5. 测试数字只写 `docs/quality/STATUS.md`，不要手抄到 README/其他文档。
