# 贡献指南（Contributing）

先谢谢你愿意花时间。这个项目的目标是「陪伴型 AI 的人格/记忆/情绪层」，不是一个通用
框架；所以提交前请先确认方向对得上（可以先开 issue 聊）。

## 1. 环境

- **开发/真机**：Windows 10/11 x64，Python 3.14（实测），微信 PC 客户端；
- **CI**：ubuntu-latest + Python 3.12（只跑离线测试；`setup-python` 没有 3.14 稳定轮子时
  按 3.12 跑，代码要保持 3.12+ 兼容）；
- 依赖安装：

  ```bash
  python -m pip install -r requirements.txt
  python -m pip install pytest
  ```

  微信窗口自动化（wxauto / 本机 Hook）是 **Windows-only 外部依赖**，不在
  `requirements.txt` 里；没有真实微信也能跑绝大多数离线测试。

## 2. 跑测试

```bash
# 全量离线测试（CI 同款）
python -m pytest agi/tests tests -q --ignore=tests/test_webui.py

# 只跑你改的部分
python -m pytest agi/tests/test_selflearn.py -q
```

约定：

- 新增行为 → 新增测试；修 bug → **先写能复现的失败测试**，再修到绿；
- 测试必须**离线**：不联网、不写真实 `agi/data/`、不依赖真实微信窗口（用 `tmp_path`）；
- 涉及时间/随机/网络/LLM 的地方要可注入（monkeypatch、fake client、`now=` 参数）。

## 3. 代码风格

- Python 3.12+，`from __future__ import annotations`，类型标注尽量写；
- 4 空格缩进、单行 ≤100 字符；注释/文档字符串用中文（跟现有代码一致）；
- **优先标准库**，新增第三方依赖要说明理由并同步 `requirements.txt`；
- 不破坏「数据不出本机」与「发送闸门」的边界；新增外发渠道必须在 PR 里写清；
- 不改 `agi/data/` 下的运行时数据、不提交 `config.json` / `*.sqlite3` / 日志。

## 4. 提交规范

用 Conventional Commits，描述用中文短句：

```
feat(gateway): 主动消息支持分享 B 站视频
fix(selflearn): tick 变量遮蔽导致三级分享失效
docs(opensource): 补发布清单
test(diary): 补 23:00 定时用例
chore(ci): 加密钥扫描 job
```

- 一个 PR 只做一件事；大改动先开 issue 对齐；
- 提交信息里写清「为什么」，不只是「改了什么」；
- 涉及行为变化/数据格式变化，在 PR 描述里写迁移与回滚方式。

## 5. 提交前检查

```bash
python tools/check_secrets.py     # 密钥扫描（必须 PASS）
python scripts/release_check.py   # 发布前全量检查（测试 + 密钥 + 大文件）
```

**绝对不要提交**：真实 API key / `admin_token` / 微信 Cookie（`SESSDATA`、`bili_jct`
等）/ 聊天记录 / 好友昵称与 wxid / 含个人信息的截图。测试夹具请用明显的假值
（`sk-test-xxx`、`SESSDATA=sess-abc`），确保 `check_secrets.py` 不误报。

## 6. PR 流程

1. Fork + 新建分支（`feat/xxx`、`fix/xxx`）；
2. 写测试 → 实现 → 本地全绿；
3. 开 PR，描述里带上：动机、方案、测试命令与结果、风险/回滚；
4. CI 绿 + 至少一位维护者 review 后合入；
5. 安全相关问题**不要走公开 PR**，见 `SECURITY.md`。

## 7. 行为准则

- 对事不对人，不人身攻击；
- 不提交/索要他人的聊天记录、凭证、个人信息；
- 不讨论「绕过他人账号/群控/营销群发」类用法；
- 维护者有权关闭偏离项目方向或涉及滥用的 PR/issue。
