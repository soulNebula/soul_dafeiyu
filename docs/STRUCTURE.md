# 项目目录结构（SoulAgent 完整目录树）

> **单一权威**：全项目目录树只保留这一份。根 `README.md`、技术文章、各子项目 README 不再各画一棵“局部树”，
> 需要看结构统一指向本文件。
>
> 目录树由 `python tools/tree_report.py --write` 扫描仓库根目录自动生成；运行期/生成物目录（`.data/`、`log/`、
> `media/` 等）折叠为一行，避免几百个临时文件把树撑爆。

最后更新：2026-10-29

## 0. 读图说明

- `#` 后面是目录用途；标“运行期（gitignore）”的目录**不入 git**，内容随使用变化；
- 仓库内只有代码/配置/文档/测试；模型与大数据在仓库外 `E:\agent`（见第 3 节）；
- `idea/` 是 803 工作台（独立 git 历史，以 subtree 合入主仓），有自己的 `app/web/tests/docs`。

## 1. 完整目录树

<!-- TREE:BEGIN（由 tools/tree_report.py 生成，勿手改）-->

```
SoulAgent/
├── .data/        # 运行期数据（gitignore；Edge profile/截图等）
├── .dsh-ops/        # DSH 工具产物（gitignore）
├── .github/        # CI 工作流
│   └── workflows/
│       └── ci.yml
├── _refs/        # 第三方参考源码/压缩包（不参与运行）
│   ├── wechat-4x-decrypt-main/
│   │   ├── scripts/
│   │   │   ├── find_wechat.py
│   │   │   ├── query_messages.py
│   │   ├── .gitignore
│   │   ├── LICENSE
│   │   ├── README.md
│   │   └── SKILL.md
│   ├── Wechat-Emoticon-Parser-4.0-plus/
│   │   ├── .gitignore
│   │   ├── LICENSE
│   │   ├── README.md
│   │   ├── README_CN.md
│   │   ├── requirements.txt
│   │   └── wechat_emoticon_export.py
│   ├── README.md
│   ├── wechat-4x-decrypt.zip
│   └── Wechat-Emoticon-Parser.zip
├── agi/        # 核心包：selflearn/ inner/ llm/ integrations/ wechat_gateway/ soul/ persona/
│   ├── data/        # 运行数据/密钥（gitignore，含 secrets.json）
│   ├── eval_data/
│   │   ├── human_tone/
│   │   │   └── … 12 个文件
│   │   └── … 26 个文件
│   ├── inner/
│   │   ├── __init__.py
│   │   ├── inject.py
│   │   ├── README.md
│   │   ├── render.py
│   │   └── schema.py
│   ├── integrations/
│   │   ├── __init__.py
│   │   ├── bili_digest.py
│   │   ├── bili_ingest.py
│   │   └── web_lookup.py
│   ├── llm/
│   │   └── … 9 个文件
│   ├── media_acquisition/
│   │   └── README.md
│   ├── persona/
│   │   └── xiaoyu/
│   │       ├── fewshot.json
│   │       ├── profile.json
│   │       ├── values.md
│   ├── selflearn/
│   │   └── … 104 个文件
│   ├── soul/
│   │   ├── data/
│   │   │   └── … 15 个文件
│   │   ├── __init__.py
│   │   ├── dataset.py
│   │   ├── evaluate.py
│   │   ├── inner_state_schema.json
│   │   ├── llama_client.py
│   │   ├── prompt.py
│   │   ├── scenarios.py
│   │   └── scenarios_extra.py
│   ├── tests/
│   │   └── … 184 个文件
│   ├── wechat_gateway/
│   │   ├── __init__.py
│   │   ├── config.py
│   │   ├── control.py
│   │   ├── drafts.py
│   │   ├── media.py
│   │   ├── policy.py
│   │   ├── proactive_worker.py
│   │   ├── README.md
│   │   ├── receiver.py
│   │   ├── send_cli.py
│   │   ├── sendclass.py
│   │   ├── sender.py
│   │   ├── server.py
│   │   ├── sticker_native.py
│   │   ├── STICKER_NATIVE_RESEARCH.md
│   │   ├── uia_sender.py
│   │   └── win_send.py
│   ├── __init__.py
│   ├── media_acquisition.py
│   ├── paths.py
│   ├── proactive_registry.py
│   ├── secrets_store.py
│   └── textguard.py
├── Demo/        # 独立工具实现（被 tools 动态 import）
│   ├── dev/
│   │   └── selftest.log
│   ├── tools_impl/
│   │   ├── emoticon_export.py
│   │   └── message_search.py
│   └── README.md
├── docs/        # 文档（架构/指南/质量/计划/归档）
│   ├── agent-b/
│   │   ├── CHANGES.md
│   │   └── SCORECARD.md
│   ├── architecture/
│   │   ├── figures/
│   │   │   └── … 9 个文件
│   │   ├── source/
│   │   │   ├── 小鱼-AI伴侣-技术与实践.md
│   │   ├── 00-总览与架构.md
│   │   ├── 01-数据与媒体管线.md
│   │   ├── 02-记忆与人格规格.md
│   │   ├── 03-在线对话与防幻觉.md
│   │   ├── 04-学习闭环与打分.md
│   │   ├── 05-路线图与验收.md
│   │   ├── 06-技能习得与认知闸门.md
│   │   ├── 07-从错误中学习.md
│   │   ├── 08-总结与最小验证.md
│   │   ├── 09-情绪状态与边界系统.md
│   │   ├── 10-训练工厂-TrainLab.md
│   │   ├── 10-记仇与信任账本.md
│   │   ├── 11-分享欲与主动表达.md
│   │   ├── 11-工作台-Workbench.md
│   │   ├── 12-主动行为的评分与决策.md
│   │   ├── 13-弱验证技能怎么学.md
│   │   ├── 14-按需运行与唤醒机制.md
│   │   ├── ARCHITECTURE.md
│   │   ├── architecture.png
│   │   ├── PLAN.md
│   │   ├── README.md
│   │   ├── SPEC.md
│   │   └── 小鱼-AI伴侣-技术与实践.pdf
│   ├── archive/
│   │   ├── 20261028-docs-snapshot/        # 不可变历史快照
│   │   ├── agi-notes/
│   │   │   └── … 22 个文件
│   │   ├── AGI-repo/
│   │   │   ├── .gitignore
│   │   │   ├── _db_suffix.py
│   │   │   ├── _w4d1_live_smoke.py
│   │   │   ├── AGI通用人工智能的概念解析.jpeg
│   │   │   ├── GIT.md
│   │   │   ├── README.md
│   │   │   ├── RESUME.md
│   │   ├── analysis/
│   │   │   ├── AstrBot-架构分析报告.md
│   │   ├── sources/
│   │   │   ├── README.md
│   │   ├── agentb_verify_result_20261029_premerge.json
│   │   ├── broken-links.md
│   │   ├── README-legacy.md
│   │   ├── WeChat-Hook-项目分析.md
│   │   ├── 主动试跑复盘_20261024.md
│   │   └── 示例_聊天记录.txt
│   ├── guides/
│   │   ├── MULTIGPU.md
│   │   ├── OPENSOURCE.md
│   │   ├── PATHS.md
│   │   ├── README-usage.md
│   │   ├── 常见问题.md
│   │   ├── 微信接入与自主能力_路线图.md
│   │   ├── 桌面版.md
│   │   ├── 网页端使用说明.md
│   │   └── 风控与合规说明.md
│   ├── plans/
│   │   ├── 15-小模型灵魂路线_可行性评估.md
│   │   ├── 16-嗓音层_v1_可执行方案.md
│   │   ├── 18-wx_box合并评估.md
│   │   ├── 19-整体开发路线.md
│   │   ├── 20-W1执行记录.md
│   │   ├── 21-W2执行记录.md
│   │   ├── 22-人格涌现_可行边界与成长闭环.md
│   │   ├── 23-W3执行记录.md
│   │   ├── 24-W4灵魂线_蒸馏2B.md
│   │   ├── 25-W4继续训练手册.md
│   │   ├── 26-W4收尾_六臂实验与G3结论.md
│   │   ├── 28-记忆架构与三个月压缩策略.md
│   │   ├── 29-语气与人味-实验结论.md
│   │   ├── 30-验收清单-2026-10-26.md
│   │   ├── 31-媒体能力-实现记录.md
│   │   ├── 32-桌面版与AstrBot功能移植方案.md
│   │   ├── 33-存储分层-记忆库与人格库.md
│   │   ├── 34-心情判定-移植方案.md
│   │   ├── 35-B站日报接话题种子.md
│   │   ├── 36-本地小模型-提取用.md
│   │   ├── 37-B站素材摄取与印象库.md
│   │   ├── 38-兴趣画像.md
│   │   ├── 39-推理过程可视化.md
│   │   ├── 40-主动节奏对等.md
│   │   ├── 41-引擎启停.md
│   │   ├── 42-模型启停修复.md
│   │   ├── 43-人格设定书-小满.md
│   │   ├── 44-微信特殊消息识别.md
│   │   ├── 45-聊天图片与素材库分离.md
│   │   ├── 46-刷B站日报头部修复.md
│   │   ├── 47-联网实时信息.md
│   │   ├── 48-网上冲浪引擎.md
│   │   ├── 49-去网感腔调.md
│   │   ├── 50-去金句腔与不主动收尾.md
│   │   ├── 51-拍一拍与上下文修复.md
│   │   ├── 52-每日语料与增量训练.md
│   │   ├── 53-小鱼的日记.md
│   │   ├── 54-自我叙事.md
│   │   ├── 56-内驱力队列.md
│   │   ├── 57-禁止编造网络故障.md
│   │   ├── 58-类海马体记忆衰减.md
│   │   ├── 59-项目总整理方案.md
│   │   ├── 60-遗留问题与后续修复清单.md
│   │   ├── 61-M5桌面化.md
│   │   ├── 62-项目全历程与实现总结.md
│   │   └── README.md
│   ├── quality/
│   │   ├── agentb_verify_result.json
│   │   ├── STATUS.md
│   │   └── status_measured.json
│   ├── INDEX.md
│   └── STRUCTURE.md
├── idea/        # 803 工作台（独立 git 历史，subtree 合入）
│   ├── .data/        # 工作台运行期数据（gitignore）
│   ├── .github/
│   │   └── workflows/
│   │       ├── ci.yml
│   ├── app/
│   │   ├── __init__.py
│   │   ├── annotstudio.py
│   │   ├── audit.py
│   │   ├── config_schema.py
│   │   ├── config_store.py
│   │   ├── corpusstudio.py
│   │   ├── datastudio.py
│   │   ├── distillstudio.py
│   │   ├── main.py
│   │   ├── modelstudio.py
│   │   ├── paths.py
│   │   ├── services.py
│   │   └── trainstudio.py
│   ├── docs/
│   │   ├── RUN.md
│   │   └── STRUCTURE.md
│   ├── pytest-of-Administrator/        # pytest 临时目录（gitignore）
│   ├── reports/
│   │   ├── archive/
│   │   │   └── … 9 个文件
│   │   ├── screens/        # 验收截图（按时间生成）
│   │   ├── acceptance_20261029_0602.md
│   │   └── latest.md
│   ├── scripts/
│   │   ├── acceptance.py
│   │   ├── port_pid.py
│   │   ├── run.bat
│   │   ├── run.sh
│   │   ├── smoke_all.py
│   │   ├── start_all.bat
│   │   ├── start_all.sh
│   │   ├── stop_all.bat
│   │   └── wait_port.py
│   ├── tests/
│   │   ├── test_annotstudio.py
│   │   ├── test_core.py
│   │   ├── test_corpusstudio.py
│   │   ├── test_datastudio.py
│   │   ├── test_distillstudio.py
│   │   ├── test_modelstudio.py
│   │   ├── test_modelstudio_import.py
│   │   └── test_trainstudio.py
│   ├── web/
│   │   ├── index.html
│   │   └── style.css
│   ├── .gitignore
│   ├── LICENSE
│   ├── README.md
│   └── requirements.txt
├── legacy/        # 死代码归档（只 mv 不删）
│   ├── agi__audit_parts/
│   │   ├── 01-02.md
│   │   ├── 03-04.md
│   │   ├── 05-08-14.md
│   │   ├── 06-07.md
│   │   ├── 09-10.md
│   │   └── 11-13.md
│   ├── Demo/
│   │   ├── dev/
│   │   │   └── … 10 个文件
│   │   └── snapshot/
│   │       ├── SPEC.md
│   ├── scratch/
│   │   ├── env_check.py
│   │   ├── hello_idea.py
│   │   └── pytorch_train_lora.py
│   └── README.md
├── log/        # 运行日志（gitignore）
├── media/        # 素材/贴图/文件缓存（gitignore）
├── reports/
│   └── archive/        # 根目录历史报告（star/analysis 等）
├── scratch/        # 临时实验产物（gitignore）
├── scripts/        # 发布/终验脚本
│   ├── agentb_verify.py
│   └── release_check.py
├── tests/        # 根测试套件
│   ├── fixtures/
│   │   ├── core/
│   │   │   ├── wechat_contacts.png
│   │   │   ├── wechat_frame.png
│   │   ├── gui/
│   │   │   ├── gui_fixtures.py
│   │   └── ocr/
│   │       ├── live_restored.png
│   │       ├── message_region.png
│   ├── conftest.py
│   ├── test_architecture.py
│   ├── test_core.py
│   ├── test_friends.py
│   ├── test_gui_smoke.py
│   ├── test_harden_acl.py
│   ├── test_isolation_guard.py
│   ├── test_ocr.py
│   ├── test_safe_fetch.py
│   ├── test_web_search.py
│   └── test_webui.py
├── tools/        # 控制台(802)/ desktop(桌面版)/ train/ soul_serve/ OCR/校验工具
│   ├── bili_patch/
│   │   └── apply_report_header_fix.py
│   ├── console/
│   │   ├── static/
│   │   │   ├── app.js
│   │   │   ├── index.html
│   │   │   ├── style.css
│   │   ├── growth_api.py
│   │   ├── media_store.py
│   │   ├── model_admin.py
│   │   ├── moments_api.py
│   │   ├── README.md
│   │   ├── server.py
│   │   └── trace_api.py
│   ├── desktop/
│   │   ├── autostart.ps1
│   │   ├── build_exe.ps1
│   │   ├── desktop_entry.py
│   │   ├── icon.ico
│   │   ├── launcher.py
│   │   ├── README.md
│   │   └── tray.ps1
│   ├── persona/
│   │   ├── __init__.py
│   │   ├── install_xiaoyu.py
│   │   ├── validate_canned.py
│   │   ├── validate_false_failure.py
│   │   ├── validate_slang.py
│   │   └── validate_xiaoyu.py
│   ├── reference_weflow/
│   │   ├── imageDecryptService.ts
│   │   ├── isaac64.ts
│   │   ├── keyProviderService.ts
│   │   ├── LICENSE
│   │   ├── nativeImageDecrypt.ts
│   │   └── third-party-components.md
│   ├── soul_serve/
│   │   ├── convert.py
│   │   ├── fetch_convert_src.py
│   │   ├── merge_adapter.py
│   │   ├── prepare_model.py
│   │   ├── README.md
│   │   ├── serve_local3b.py
│   │   ├── server.py
│   │   ├── smoke_eval.py
│   │   └── start_server.ps1
│   ├── train/
│   │   ├── _analyze_data.py
│   │   ├── _analyze_eval.py
│   │   ├── _compare_baselines.py
│   │   ├── _rule_baseline.py
│   │   ├── balance_target.py
│   │   ├── daily_corpus.py
│   │   ├── daily_summary.py
│   │   ├── export_chat.py
│   │   ├── README.md
│   │   ├── reduce_target.py
│   │   ├── run_daily.py
│   │   ├── sft_train.py
│   │   ├── smoke_lora.py
│   │   └── train_gate.py
│   ├── wechat_uia/
│   │   ├── bench.py
│   │   ├── dump_tree.ps1
│   │   ├── query.ps1
│   │   ├── smoke.py
│   │   ├── uia_query.ps1
│   │   └── uia_worker.ps1
│   ├── audit_compliance.py
│   ├── bili_memory_sync.py
│   ├── build_golden.py
│   ├── build_human_tone.py
│   ├── build_qq_tone.py
│   ├── chat_context_probe.py
│   ├── check_architecture.py
│   ├── check_docs_links.py
│   ├── check_portability.py
│   ├── check_secrets.py
│   ├── export_emoticons.py
│   ├── harden_acl.py
│   ├── local3b_quality_eval.py
│   ├── migrate_chat_media.py
│   ├── moments_fetch.py
│   ├── moments_media_export.py
│   ├── ocr_image.py
│   ├── prune_chat_logs.py
│   ├── repair_media_paths.py
│   ├── rotate_secrets.py
│   ├── search_messages.py
│   ├── status_report.py
│   ├── tree_report.py
│   └── verify_db_split.py
├── ui/        # legacy Tk 桌面 GUI（app.py）
│   ├── __init__.py
│   ├── dev_backend.py
│   ├── main_window.py
│   └── widgets.py
├── webui/        # legacy 8081 网页端（非主链）
│   ├── static/
│   │   ├── agi.html
│   │   ├── app.js
│   │   ├── favicon.svg
│   │   ├── index.html
│   │   └── style.css
│   ├── __init__.py
│   ├── account.py
│   ├── agi.py
│   ├── bridge.py
│   ├── contacts.py
│   ├── CONTRACT.md
│   ├── groups.py
│   ├── history.py
│   ├── llm.py
│   ├── moments.py
│   ├── moments_crypto.py
│   ├── moments_ui.py
│   ├── moments_worker.py
│   ├── pinyin.py
│   ├── replay.py
│   ├── server.py
│   ├── video_auto.py
│   ├── videos.py
│   └── wechat_text.py
├── wechatauto_logs/        # 发送器日志（gitignore）
├── wx_core/        # 微信底座：窗口/UIA/DB 直读/OCR/发送/媒体/密钥
│   ├── emoji_assets/
│   │   └── … 110 个文件
│   ├── __init__.py
│   ├── api.py
│   ├── builtin_emoji.py
│   ├── clipboard.py
│   ├── config.py
│   ├── dat_decrypt.py
│   ├── db_source.py
│   ├── emoticon.py
│   ├── extract.py
│   ├── friends.py
│   ├── layout.py
│   ├── media_cache.py
│   ├── media_export.py
│   ├── models.py
│   ├── monitor.py
│   ├── ocr.py
│   ├── safe_fetch.py
│   ├── store.py
│   ├── textlog.py
│   ├── wcdb.py
│   ├── web_search.py
│   ├── wechat_keys.py
│   └── win.py
├── wxauto_logs/        # wxauto 运行日志（gitignore）
├── .gitignore
├── app.py
├── config.json
├── CONTRIBUTING.md
├── LICENSE
├── README.md
├── requirements.txt
├── run.bat
├── run_web.bat
├── SECURITY.md
├── web_app.py
├── web_config.example.json
├── web_config.json
└── 启动桌面版.bat
```

<!-- TREE:END -->

## 2. 各目录职责（整个项目）

| 目录 | 放什么 | 不放什么 |
| --- | --- | --- |
| `agi/` | 核心包：`selflearn/`（记忆/情绪/路由/主动）、`inner/`、`llm/`、`integrations/`、`wechat_gateway/`、`soul/`、`persona/`、`tests/` | 训练产物、模型权重、运行日志 |
| `tools/` | 802 控制台（`console/`）、桌面版（`desktop/`）、训练（`train/`）、本地模型服务（`soul_serve/`）、OCR/朋友圈/记忆/校验工具 | 业务运行数据、模型文件 |
| `wx_core/` | 微信底座：窗口/UIA、WCDB 直读、OCR、发送、媒体、密钥、表情 | 业务人格/记忆逻辑 |
| `idea/` | 803 工作台：模型中心/训练工厂/数据工作室/语料清洗/用户标注/蒸馏；独立测试与报告 | 主链收发、运行时数据（`idea/.data` 除外） |
| `docs/` | 唯一文档区：`INDEX.md`、`STRUCTURE.md`、`architecture/`、`guides/`、`quality/`、`plans/`、`archive/`、`agent-b/` | 零散主题文档、旧报告 |
| `webui/` | legacy 8081 网页端（非主链） | 新功能入口 |
| `ui/` | legacy Tk 桌面 GUI（`app.py` 入口） | 新功能入口 |
| `Demo/` | 独立工具实现（被 `tools/` 动态 import） | 主链逻辑 |
| `legacy/` | 死代码归档（只 mv 不删，可搬回） | 现役代码 |
| `tests/` | 根测试套件（架构/隔离/WebUI/OCR 等） | 业务代码 |
| `scripts/` | 发布/终验脚本（`agentb_verify.py`、`release_check.py`） | 一次性实验脚本 |
| `_refs/` | 第三方参考源码/压缩包（不参与运行） | 项目自身代码 |
| `.github/` | CI 工作流 | 业务文档 |
| 根文件 | `README.md`、`LICENSE`、`SECURITY.md`、`CONTRIBUTING.md`、`requirements.txt`、`run.bat`、`启动桌面版.bat`、`config.json`（运行配置） | 零散报告、临时文件 |

## 3. 仓库外部目录（不在仓库，路径由 `agi/paths.py` 解析）

| 路径 | 内容 | 说明 |
| --- | --- | --- |
| `E:\agent\models\` | GGUF / VL 模型 / 旧模型回收站 `.trash` | 模型资产不入库 |
| `E:\agent\output\` | 训练产物（LoRA adapter、合并模型、GGUF） | 大文件 |
| `E:\agent\datasets\` | 训练/验证数据集（含语料清洗产物） | 含聊天数据，敏感 |
| `E:\agent\logs\` | 服务/桌面版/终验日志 | 运行期 |
| `E:\agent\venv_wx312\` | 发送器独立 Python 3.12 venv（wechatauto） | 主进程 3.14 不 import wx |
| `~/.dsh/bili-duty/` | B站值勤插件（DSH 侧）的产物/凭证 | 仓库外插件 |

## 4. 新文档放哪（docs 内部规则）

| 新文档类型 | 放置位置 |
| --- | --- |
| 架构变更、模块设计、设计规格 | `docs/architecture/` |
| 部署/启动/运维/操作/FAQ/合规/开源 | `docs/guides/` |
| 测试报告、验收数字、质量评分 | `docs/quality/` |
| 迭代计划、执行记录、实现方案、项目复盘 | `docs/plans/<编号>-<标题>.md` |
| 过时文档、一次性复盘、旧报告 | `docs/archive/` |
| 根目录产生的临时报告 | `reports/archive/` |

## 5. 维护约定

1. 目录变化后跑 `python tools/tree_report.py --write` 更新本文目录树，**不要手改 TREE 区块**；
2. 新文档先查 `docs/INDEX.md`，再按第 4 节放置；
3. 移动文档后更新 `INDEX.md` 链接，并跑 `python tools/check_docs_links.py`；
4. 不新增根目录平行 `plans/`/`specs/`/`analysis/`；
5. `docs/agent-b/` 不动；`docs/archive/20261028-docs-snapshot/` 视为不可变快照；
6. 测试数字只写 `docs/quality/STATUS.md`，不要手抄到 README/其他文档；
7. 运行期目录（`.data/`、`log/`、`media/`、`wxauto_logs/` 等）已在 `.gitignore`，不要提交。
