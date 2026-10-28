# legacy/ · 死代码归档区（只归档、不删除）

本目录存放**已证明没有任何活引用**的历史代码/笔记。归档方式是 `mv`（移动），内容一字未改，
所以随时可以原样搬回；归档的唯一目的是让仓库根目录只保留仍在运行或仍被文档引用的东西。

- 归档日期：2026-10-29
- 归档总量：**36 个文件 / 9078 行**（其中 `.py` 29 个 / 7946 行，`.md` 7 个 / 1132 行）；
  本 README 不计入清单。`tools/status_report.py` 统计整个 `legacy/` 目录会显示
  **37 个文件 / 9149 行**（含本文件），两个口径都对，只是范围不同。
- 使用规则：`legacy/` 里的代码**不参与运行、不参与 pytest 收集、不进入合规/可移植性扫描**；
  `tools/check_architecture.py` 的「死代码回归」规则会跳过本目录（这里本来就是死的）。
- 恢复方式：把对应子目录搬回原路径即可，例如
  `mv legacy/Demo/dev Demo/dev`（Windows 资源管理器里剪切/粘贴等价）。

## 一、怎么证明「死的」（方法可复现）

1. **静态 import 图**：解析仓库内全部 `.py` 的 `import` / `from ... import ...`（含相对 import），
   建立「模块 → 被谁 import」的反向边；
2. **文本引用扫描**：对 `.py/.md/.bat/.json/.ps1` 全量搜模块全名、仓库相对路径、文件名，
   用来抓 `importlib.import_module("...")`、`sys.path.insert(...)+import xxx`、子进程脚本路径等
   动态/字符串引用；
3. **pytest 收集面**：归档前跑
   `python -m pytest tests agi/tests --collect-only -q` 记录计数，归档后再跑一次对比（不降）；
4. **人工复核**：对每个候选逐个看「谁引用过、引用的是不是历史文档」，确认没有配置/脚本/文档
   把它当现役入口后，才 `mv`。

归档前收集基线：根 `tests/` **270** 条、`agi/tests/` **2104** 条；本轮收口后复测（2026-10-29）
为 **298 / 2197**（其他 teammate 同时在加测试，只会涨）。**`legacy/` 本身贡献 0 条**，
可用下面的命令验证（输出 `no tests collected`；pytest 空收集时返回码是 5，属正常，不代表失败）：

```bat
python -m pytest legacy --collect-only -q
```

## 二、归档清单（逐项证据）

| 原路径（现位于） | 文件数 / 行数 | 为什么是死的（证据命令） | 曾经被谁引用 | 恢复 |
| --- | --- | --- | --- | --- |
| `Demo/snapshot/` → `legacy/Demo/snapshot/` | 17 个 / 6505 行（`.py` 6170 行） | `wx_core/*` 与 `ui/main_window.py` 的**旧副本**；活模块在原位，主程序只 import 原位版本。证据：`python tools/check_architecture.py --dead` 不再列出原位模块；`grep -rn "Demo/snapshot" --include=*.py .` 无 import 方（仅 README 叙述） | 仅 `Demo/README.md`、根 `SPEC.md` 第 354 节的归档说明（历史叙述，不是调用） | `mv legacy/Demo/snapshot Demo/snapshot` |
| `Demo/dev/` → `legacy/Demo/dev/` | 10 个 / 1688 行 | 开发期一次性诊断脚本（WCDB 密钥/解密试探 `try_db_key*.py`、`map_keys.py`、`decrypt_dbs.py`、表情包解密验证 `test_emoticon.py`、GUI 自测 `selftest_gui.py`）。证据：`python tools/check_architecture.py --dead` 无对应模块；`grep -rn "Demo/dev\|test_emoticon\|selftest_gui" --include=*.py .` 仅本目录自身 | 仅 `Demo/README.md`、`SPEC.md` 的归档说明；`selftest_gui.py` 自己 import 的是 `app`/`ui.main_window`（活模块） | `mv legacy/Demo/dev Demo/dev` |
| `scratch/` → `legacy/scratch/` | 3 个 / 88 行 | 根目录调试残留：`hello_idea.py`（IDE 冒烟）、`env_check.py`（torch 版本探测）、`pytorch_train_lora.py`（LoRA 骨架，含本机 `E:\agent` 路径）。证据：`python tools/check_architecture.py --dead` 无引用；`grep -rn "hello_idea\|env_check\|pytorch_train_lora" --include=*.py --include=*.bat --include=*.ps1 .` 仅本目录 | 无。`docs/OPENSOURCE.md` 明确把它列为「调试残留，清理」 | `mv legacy/scratch scratch` |
| `agi/_audit_parts/` → `legacy/agi__audit_parts/` | 6 个 / 797 行（纯 `.md`） | 历史审计的原始输出分片（01-02、03-04、…、11-13），自述「只读源码 + 只写本片段」。证据：不是 Python 模块、无 import 边；`grep -rn "_audit_parts" --include=*.py --include=*.md .` 仅本目录与历史归档 | 无引用；同类内容已在 `docs/archive/agi-notes/` 留档 | `mv legacy/agi__audit_parts/_audit_parts agi/_audit_parts`（或按文件名搬回） |

> 注：`Demo/` **没有整体归档**。`Demo/tools_impl/`（`emoticon_export.py`、`message_search.py`）
> 仍被 `tools/export_emoticons.py`、`tools/search_messages.py` 用
> `sys.path.insert(0, ROOT/"Demo"/"tools_impl")` 动态 import，是**活的**，必须留在原位。
> 同理，`webui/`、`ui/`、`wx_core/api.py`、`webui/moments_worker.py`（子进程调用）都是活的，
> 不要因为「看起来没人 import」就归档——它们靠字符串/子进程路径被引用。

## 三、确认「不误伤」的检查（每次改动后都跑）

```bat
python -m pytest tests -q
python -m pytest agi/tests --collect-only -q
python -m pytest legacy --collect-only -q
python tools/check_architecture.py
python tools/check_docs_links.py
```

- 根 `tests/` 必须全绿；
- `agi/tests` 收集数不低于归档前记录的 2104（本目录不贡献任何测试）；
- `python -m pytest legacy --collect-only -q` 必须输出 `no tests collected`（归档区不产生测试面；
  pytest 空收集返回码 5 属正常）；
- `tools/check_architecture.py` 退出 0（它会用同一套 import/引用扫描防止死代码回流）；
- `tools/check_docs_links.py` 退出 0（`docs/` 内 0 断链）。

## 四、以后怎么继续归档（给接手的人）

1. 先跑 `python tools/check_architecture.py --dead`：列出「无 import 边、也无字符串引用」的模块；
2. 逐个确认不是动态入口（子进程脚本、`sys.path.insert` 后 import、文档里的手动命令）；
3. 记录归档前 `pytest ... --collect-only` 计数，`mv` 到 `legacy/` 并在本表登记
   （原路径 / 判定证据命令 / 曾引用方 / 恢复命令，四样都要写）；
4. 跑上面五条检查，全绿后再交给提交者。