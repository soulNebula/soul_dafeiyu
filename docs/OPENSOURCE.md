# 开源化与发布（M6）

本文写清楚「怎么把 SoulAgent 开源出去、怎么发一个版本」。目标：**别人 clone 下来能跑、
能提 issue/PR；我们发布时不会把用户数据/凭证带出去**。

## 1. 版本与分支

- 版本号：`0.1.x` 起步（`0.x` 阶段 API/数据格式可能不兼容地变）；
- 分支：`main`（可发布）／`feat/*`、`fix/*`（开发）；`w4-soul` 等长期分支合并后删除；
- 发布用 **git tag**：`v0.1.0`、`v0.1.1`，tag 与 PyPI 无关（本项目不发布到 PyPI）；
- 版本说明写进 `CHANGELOG.md`（见第 5 节模板）。

## 2. 开源基线文件清单

| 文件 | 作用 | 检查 |
| --- | --- | --- |
| `LICENSE`（MIT） | 许可 | `release_check` 必备文件检查 |
| `.github/workflows/ci.yml` | 密钥扫描 + 离线测试 | 同上 |
| `tools/check_secrets.py` | 提交前/CI 密钥扫描 | CI 与 release_check 都跑 |
| `SECURITY.md` | 漏洞报告方式与边界 | 必备文件检查 |
| `CONTRIBUTING.md` | 环境/测试/提交规范 | 必备文件检查 |
| `docs/OPENSOURCE.md` | 本文（发布流程） | 必备文件检查 |
| `scripts/release_check.py` | 一键发布前检查 | 必备文件检查 |
| `README.md` 徽章 | CI/License/Python（静态 shields.io） | 人工确认 |

## 3. 一键检查

```bash
python tools/check_secrets.py     # 只做密钥扫描
python scripts/release_check.py   # 发布闸门：必备文件 + 密钥 + 大文件 + 全量离线测试
```

`release_check.py` 输出逐项 `[PASS]/[FAIL]` 和最终 `RESULT: PASS|FAIL`，退出码 0/1；
CI 之外发版、或改完依赖想确认「能不能发」时跑它。常用参数：

```bash
python scripts/release_check.py --skip-tests     # 只查文件/密钥/大文件（本地快速）
python scripts/release_check.py --max-mb 20      # 收紧单文件上限
python scripts/release_check.py --json           # 机器可读
```

## 4. 发布前必做（数据剥离 / 敏感配置 / 二进制）

### 4.1 数据剥离 —— 这些**绝对不能**进仓库

`.gitignore` 是第一道防线，发布打包时再人工核一遍：

| 路径 | 内容 | 处理 |
| --- | --- | --- |
| `agi/data/**` | 记忆库/人格库/联系人库/日记/媒体/日志 | 不入库、不打包 |
| `log/`、`wxauto_logs/`、`*.log` | 运行日志（含聊天文本） | 不入库 |
| `media/`、`wx_core/emoji_assets/` 下用户自建素材 | 用户媒体 | 不入库（仓库只留最小示例） |
| `web_config.json`、`agi/data/wechat/config.json` | API key、admin_token、好友白名单 | 不入库（留 `*.example.json`） |
| `*.sqlite3` / `*.db` | 数据库（聊天/记忆/训练样本） | 不入库 |
| 截图/录屏/Demo 数据 | 可能含好友昵称、wxid、头像、聊天内容 | 发布前逐张检查 |
| `docs/analysis/` 里的真实对话摘录 | 隐私 | 脱敏或删除 |
| `scratch/`、`_tmp*`、`.data_shots/` | 调试残留 | 清理 |

### 4.2 敏感配置检查

- `python tools/check_secrets.py` 必须 PASS（扫描 git 已跟踪文件）：
  `sk-` API key、`ghp_` GitHub token、`SESSDATA`、`Bearer`、私钥块、赋值式凭证；
- 人工再确认：`git ls-files | grep -E "config\.json|\.env|credential|cookie"`；
- 确认 `web_config.json` 只在 `.gitignore` 里，仓库里只有 `web_config.example.json`；
- 确认 README/文档里的示例 key 都是 `sk-xxx` 占位符；
- 发布前把本机用过的 key 视为「可能已泄漏」：换 key 比后悔便宜。

### 4.3 模型 / 二进制不入库

- 单文件 > **50MB** 不入库（`release_check.py` 会拦）；`git ls-files` 里也不许出现
  `.onnx`、`.pt`、`.pth`、`.exe`、`.dll`、`.ffmpeg` 等大二进制；
- 模型请走 Releases 附件或外部下载，仓库只放下载脚本/校验和（`sha256`）；
- 不要用 Git LFS 存模型（clone 成本与仓库体积仍然存在，且国内网络差）。

### 4.4 许可与第三方

- 项目本体 MIT；`requirements.txt` 里依赖的许可证需兼容 MIT（Pillow/numpy/rapidocr 等
  均为 BSD/Apache 类，可商用）；
- 发布时附 `THIRD_PARTY_NOTICES.md`（可选，列出主要依赖与许可证）；
- 不要搬运来路不明的第三方代码/模型权重。

## 5. CHANGELOG 模板

遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的简化版：

```markdown
# Changelog

## [Unreleased]

## [0.1.0] - 2026-10-28
### Added
- 微信网关：真实收发、主动消息、暂停/恢复、媒体气泡
- 自学习层：记忆/人格/心情/信任/冲浪引擎/日记
### Changed
- 主动消息节奏（30s → 1–120 分钟随机 + 概率闸门 + 夜间静音）
### Fixed
- 危机分支 UnboundLocalError；tick 变量遮蔽导致三级分享失效
### Security
- 新增密钥扫描与发送闸门（白名单/STOP/内容拦截）
```

## 6. 发布步骤（Checklist）

1. 确认 `main` 干净、CI 绿；
2. 跑 `python scripts/release_check.py` → `RESULT: PASS`；
3. 更新 `CHANGELOG.md`（新版本号 + 日期 + 变更）；
4. 提升版本号（`agi/__init__.py` 或 `VERSION` 文件，若存在），提交；
5. 打 tag：`git tag -a v0.1.0 -m "v0.1.0"`，推送 tag；
6. GitHub → Releases → Draft a new release → 选 tag → 贴 CHANGELOG → 附可选的说明性
   压缩包（**只含代码**，按 4.1 排除数据/媒体/配置；附 `sha256`）；
7. 发布后自检：在干净目录 `git clone` + `pip install -r requirements.txt` +
   `pytest agi/tests tests -q --ignore=tests/test_webui.py` 能跑；
8. 回滚：删除 Release、`git tag -d`/远端删 tag；已合入的问题用 `fix:` 补丁版本；
   **已泄漏的凭证先轮换，再考虑回滚代码**。

## 7. 已知限制（写进 Release Notes，避免误解）

- 微信自动化依赖 Windows 客户端与 wx4 数据层，**Linux/macOS 只能跑离线逻辑层**；
- CI（ubuntu）只保证离线测试；真机行为以 Windows 实测为准；
- 需要自备大模型 API key（DeepSeek 等），费用自负；
- 仅用于**本人账号**的陪伴/自动化，不支持多开群控与商业群发。
