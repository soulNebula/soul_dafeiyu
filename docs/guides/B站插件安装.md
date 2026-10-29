# B站插件安装（bili-duty）

> 小鱼仓库**不包含** B站插件本体；802/803 的 B站页、主动视频分享、日报都依赖它。
> 没有插件时主链（微信聊天/记忆）不受影响，只是 B站相关页面为空或报不可用。

最后更新：2026-10-30

## 1. 它是什么

`bili-duty` 是一个独立的 DSH 插件，负责：

- B站扫码登录 / 凭证保存
- 按窗口/额度刷视频、读字幕、读评论
- 写日报（`digest/`、`report`）
- 把视频印象写进 `bili_impressions`，供小鱼聊天/主动分享使用

小鱼只读它的产物，**不内置它的代码**，避免把第三方插件和登录凭证打进仓库。

## 2. 放在哪里

默认路径：

```
%USERPROFILE%\.dsh\bili-duty\
├── config.json
├── duty.md
├── bili_credential.json     # 扫码登录后生成
├── digest/
├── duty.log
└── ...
```

也可以放到别处，设置环境变量：

```bat
set BILI_DUTY_HOME=D:\plugins\bili-duty
```

802 的 B站页会自动检测并显示：
- 插件路径
- 是否缺 `config.json` / `duty.md`
- 是否已有登录凭证

## 3. 安装步骤

1. 拿到 `bili-duty` 插件目录（你自己的来源/内部包）；
2. 整个目录放到 `C:\Users\<你>\.dsh\bili-duty`；
3. 确认 `config.json` 和 `duty.md` 存在；
4. 打开 802 → B站页 → 「生成二维码」→ 手机 B站扫码 → 「我已扫码，检查状态」；
5. 登录凭证会写 `bili_credential.json`（不要提交/共享）；
6. 在 802 B站页调整配置并保存。

> 如果插件缺失，802 B站页顶部会显示「B站插件未安装」，不会静默假装可用。

## 4. 一键脚本会做什么

`setup.ps1` 会检查：

- `~/.dsh/bili-duty` 是否存在
- `config.json` / `duty.md` 是否齐全
- 没有时只提示，**不会自动下载/复制**（插件来源与许可不在本仓库）

## 5. 常见问题

| 现象 | 处理 |
| --- | --- |
| B站页提示插件未安装 | 检查 `~/.dsh/bili-duty` 或 `BILI_DUTY_HOME` |
| 有目录但提示缺文件 | 补 `config.json` / `duty.md` |
| 扫码后状态不更新 | 点「我已扫码，检查状态」；看 `duty.log` |
| 日报为空 | 插件还没到运行窗口，或 `reportLevel=off` |
| 主动视频分享没有 B站内容 | 检查 Bot 设置 `bili_ingest_enabled` / `surf_enabled` |

## 6. 开源发布注意

- 不把 `bili-duty` 本体、`bili_credential.json`、`config.json` 提交到仓库；
- 不把 B站登录凭证写进文档/截图；
- 插件缺失属于**可选能力降级**，不是主链故障。
