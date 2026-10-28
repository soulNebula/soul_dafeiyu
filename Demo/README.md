# Demo —— 独立工具实现（其余历史归档已移入 legacy/）

这个文件夹现在**只保留仍在被主项目调用的独立工具实现**：

## tools_impl/

由主项目 `tools/` 下的启动文件动态 import（`sys.path.insert(0, ROOT/"Demo"/"tools_impl")`），
所以必须留在原位：

- `emoticon_export.py` —— 表情包全通路导出（商店/收藏/未知，wxgf→GIF，manifest.json），
  启动入口 `tools/export_emoticons.py`
- `message_search.py` —— 全库历史消息检索（关键词/时间/联系人，导出 txt/json），
  启动入口 `tools/search_messages.py`

```bat
python tools/export_emoticons.py --limit 20
python tools/search_messages.py --keyword 关键词 --out log/检索.txt
```

## 已归档（2026-10-29，内容未改，可原样搬回）

- `dev/`（开发期 WCDB 密钥/解密试探、表情包解密验证、GUI 自测脚本）
  → `legacy/Demo/dev/`
- `snapshot/`（`wx_core/*` 与 `ui/main_window.py` 的旧副本；活模块在原位，主程序不读副本）
  → `legacy/Demo/snapshot/`

归档理由、引用证明与恢复命令见 [`../legacy/README.md`](../legacy/README.md)。