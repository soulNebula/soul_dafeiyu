# bots/ · bot 工作区

每个 bot 一个目录（V1 单 bot，`bots/active` 指向当前）：

```
bots/<id>/
  bot.json
  config/{wechat.json,app.json}
  secrets/keys.enc
  db/{wechat.sqlite3,selflearn/*.sqlite3}
  models/{soul,vision}/
  prompts/ media/ logs/
```

- 目录内容不入库（见 `.gitignore`）；`SOULAGENT_BOT_ROOT` 优先级最高。
- 设计见 [`docs/plans/64-V1详细技术设计.md`](../docs/plans/64-V1详细技术设计.md)。
- 迁移：`scripts/migrate_to_bot.py`（V1 施工 PR1，暂未提供）。
