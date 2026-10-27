# 历史断链清单（broken-links）

> 2026-10-28 文档收口：**active docs 已修复为 0 断链**；本文件保留无法/不建议修复的历史链接说明。

## active 检查结果

`python tools/check_docs_links.py` → `total_links=126, broken=0`。

## 不修复的历史区域（checker 默认跳过）

| 区域 | 原因 |
| --- | --- |
| `docs/archive/`（含 2026-10-28 快照、AGI-repo、agi-notes） | 历史快照，保持原样，不修改引用 |
| `docs/sources/wx_box/` | wx_box 来源材料/旧代码，仅作参考 |
| `docs/analysis/` | 外部项目分析，历史调研 |
| `docs/archive/README-legacy.md` | 旧 wx_box README，已归档；内部大量旧路径（recon/webui/agi/core 等）不适用于 SoulAgent |

这些区域里的相对链接若在旧目录打开会断，属预期；现役入口以 `docs/INDEX.md` 为准。
