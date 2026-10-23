# Demo —— 本次新增/修改的代码归档

这个文件夹是为了和主项目/其他 agent 的代码分开，方便回滚与查阅。
结构仿照 `_refs/`（第三方参考项目归档）：**主项目文件夹只保留启动文件**
（`app.py`、`run.bat`、`web_app.py`、`run_web.bat`、`tools/export_emoticons.py`、
`tools/search_messages.py`），其余由我新增/修改的代码归档到这里。

> 注意：为保证程序仍可正常运行，`snapshot/` 里是**副本**（原始文件仍在原位）；
> `tools_impl/` 与 `dev/` 是真正的移动。

## tools_impl/
独立工具的实现（由主项目 `tools/` 下的启动文件调用）：
- `emoticon_export.py` —— 表情包全通路导出（商店/收藏/未知，wxgf→GIF，manifest.json）
- `message_search.py` —— 全库历史消息检索（关键词/时间/联系人，导出 txt/json）

启动方式（在主项目根目录）：
```bat
python tools/export_emoticons.py --limit 20
python tools/search_messages.py --keyword 关键词 --out log/检索.txt
```

## dev/
开发期原型/诊断脚本（只读探查，不参与运行，需要时从项目根目录运行）：
- `try_db_key*.py`、`map_keys.py`、`decrypt_dbs.py` —— WCDB 数据库密钥/解密研究
- `test_emoticon.py` —— 表情包解密验证
- `selftest_gui.py` —— 真实后端 + GUI 自测（截图到 recon/）

## snapshot/
我新增/修改过、但被主程序直接引用的模块的**副本**（仅归档，原始文件不动）：
- `wx_core/`：monitor、db_source、wcdb、emoticon、wechat_keys、dat_decrypt、textlog、
  clipboard、media_export、builtin_emoji、config、models、store、media_cache、extract
- `ui/main_window.py`、`SPEC.md`

## 为什么不能真的全部移动
`wx_core/` 是一个 Python 包，同一个包里既有我改的文件（monitor/db_source/...）也有
团队/其他 agent 的文件（ocr/win/layout/friends/api/models...）。把包的一部分移到
`Demo/` 会导致 `import wx_core.xxx` 失败，主程序和网页端都无法启动。所以这些文件
以副本形式归档，运行版本仍在原位。
