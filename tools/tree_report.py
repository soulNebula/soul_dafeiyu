# -*- coding: utf-8 -*-
"""生成项目完整目录树，并可回填 docs/STRUCTURE.md 的 TREE 区块。

用法：
    python tools/tree_report.py                 # 打印到 stdout
    python tools/tree_report.py --write         # 回填 docs/STRUCTURE.md
    python tools/tree_report.py --depth 2       # 限制展示深度

规则：
- 跳过 .git/.venv/__pycache__/node_modules 等无关目录；
- 运行期目录（.data/log/media/scratch/reports/各类日志）折叠为一行，避免上百个临时 profile；
- 目录在前、文件在后，同类按名称排序；控制台输出 UTF-8。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STRUCTURE = ROOT / "docs" / "STRUCTURE.md"
BEGIN = "<!-- TREE:BEGIN（由 tools/tree_report.py 生成，勿手改）-->"
END = "<!-- TREE:END -->"

SKIP_DIRS = {
    ".git", ".venv", "venv", "venv312", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "node_modules", ".idea", "build", "dist",
}
SKIP_FILES = {".dsh_write_probe"}
#: 叶子目录文件过多时折叠显示
COLLAPSE_FILE_THRESHOLD = 8
#: 这些目录即使文件多也全部列出（文档/核心工具的“名字”本身有用）
LIST_ALL_DIRS = {
    "docs/architecture", "docs/guides", "docs/quality", "docs/plans",
    "tools", "wx_core", "tests",
    "agi/wechat_gateway", "idea/app", "idea/scripts", "tools/train", "tools/soul_serve",
}
#: 运行期/生成物目录：只显示一行，不展开（内容随使用变化，且大多在 gitignore）
RUNTIME_DIRS = {
    ".data": "运行期数据（gitignore；Edge profile/截图等）",
    ".dsh-ops": "DSH 工具产物（gitignore）",
    "log": "运行日志（gitignore）",
    "media": "素材/贴图/文件缓存（gitignore）",
    "wxauto_logs": "wxauto 运行日志（gitignore）",
    "wechatauto_logs": "发送器日志（gitignore）",
    "pytest-of-Administrator": "pytest 临时目录（gitignore）",
}
#: 这些子目录即使深度够也不展开（运行数据/敏感/截图）
COLLAPSE_REL = {
    "agi/data": "运行数据/密钥（gitignore，含 secrets.json）",
    "scratch": "临时实验产物（gitignore）",
    "reports/archive": "根目录历史报告（star/analysis 等）",
    "idea/.data": "工作台运行期数据（gitignore）",
    "idea/reports/screens": "验收截图（按时间生成）",
    "idea/pytest-of-Administrator": "pytest 临时目录（gitignore）",
    "docs/archive/20261028-docs-snapshot": "不可变历史快照",
}
#: 顶层目录用途注释
TOP_NOTES = {
    "agi": "核心包：selflearn/ inner/ llm/ integrations/ wechat_gateway/ soul/ persona/",
    "tools": "控制台(802)/ desktop(桌面版)/ train/ soul_serve/ OCR/校验工具",
    "wx_core": "微信底座：窗口/UIA/DB 直读/OCR/发送/媒体/密钥",
    "idea": "803 工作台（独立 git 历史，subtree 合入）",
    "docs": "文档（架构/指南/质量/计划/归档）",
    "webui": "legacy 8081 网页端（非主链）",
    "ui": "legacy Tk 桌面 GUI（app.py）",
    "Demo": "独立工具实现（被 tools 动态 import）",
    "legacy": "死代码归档（只 mv 不删）",
    "tests": "根测试套件",
    "scripts": "发布/终验脚本",
    "_refs": "第三方参考源码/压缩包（不参与运行）",
    ".github": "CI 工作流",
}


def _entries(path: Path) -> list[Path]:
    items = [e for e in path.iterdir() if e.name not in SKIP_DIRS]
    return sorted(items, key=lambda e: (not e.is_dir(), e.name.lower()))


def _runtime(path: Path, rel: Path) -> str | None:
    """返回该目录的运行期说明（命中则折叠）。"""
    key = rel.as_posix()
    if key in COLLAPSE_REL:
        return COLLAPSE_REL[key]
    if "/" not in key and path.name in RUNTIME_DIRS:
        return RUNTIME_DIRS[path.name]
    return None


def build_tree(max_depth: int = 3, expand: set[str] | None = None) -> str:
    """生成 markdown 树。max_depth 限制层数；expand 中的顶层目录额外展开一层。"""
    expand = expand or {"agi", "tools", "idea", "docs", "wx_core", "webui", "ui"}
    lines: list[str] = ["SoulAgent/"]

    def walk(path: Path, prefix: str, depth: int) -> None:
        entries = [e for e in _entries(path) if e.name not in SKIP_FILES]
        dirs = [e for e in entries if e.is_dir()]
        files = [e for e in entries if e.is_file()]
        rel_here = path.relative_to(ROOT).as_posix() if path != ROOT else ""
        # 目录先展示（便于看结构），文件在叶子处折叠展示
        for index, entry in enumerate(dirs):
            is_last_dir = index == len(dirs) - 1 and not files
            connector = "└── " if is_last_dir else "├── "
            child_prefix = prefix + ("    " if is_last_dir else "│   ")
            rel = entry.relative_to(ROOT)
            note = _runtime(entry, rel)
            suffix = ""
            if note:
                suffix = f"        # {note}"
            elif depth == 0 and entry.name in TOP_NOTES:
                suffix = f"        # {TOP_NOTES[entry.name]}"
            lines.append(f"{prefix}{connector}{entry.name}/{suffix}")
            if note:
                continue  # 运行期/敏感目录不展开
            this_depth = depth + 1
            limit = max_depth + (1 if depth == 0 and entry.name in expand else 0)
            if this_depth < limit:
                walk(entry, child_prefix, this_depth)
            else:
                sub_dirs = [e for e in _entries(entry) if e.is_dir() and e.name not in SKIP_DIRS]
                sub_files = [e for e in _entries(entry) if e.is_file()]
                total = len(sub_files)
                list_all = depth <= 1 or rel.as_posix() in LIST_ALL_DIRS
                if total > COLLAPSE_FILE_THRESHOLD and not list_all:
                    lines.append(f"{child_prefix}└── … {total} 个文件")
                else:
                    for f in sub_files:
                        lines.append(f"{child_prefix}├── {f.name}")
        # 当前目录的文件
        if files:
            list_all = depth <= 1 or rel_here in LIST_ALL_DIRS
            if len(files) > COLLAPSE_FILE_THRESHOLD and not list_all:
                lines.append(f"{prefix}└── … {len(files)} 个文件")
            else:
                for index, entry in enumerate(files):
                    last = index == len(files) - 1
                    connector = "└── " if last else "├── "
                    lines.append(f"{prefix}{connector}{entry.name}")

    walk(ROOT, "", 0)
    return "\n".join(lines)


def write_block(tree: str) -> bool:
    block = f"{BEGIN}\n\n```\n{tree}\n```\n\n{END}"
    text = STRUCTURE.read_text(encoding="utf-8") if STRUCTURE.is_file() else ""
    if BEGIN in text and END in text:
        head = text.split(BEGIN, 1)[0]
        tail = text.split(END, 1)[1]
        new = head + block + tail
    else:
        new = text.rstrip() + "\n\n## 完整目录树\n\n" + block + "\n"
    if new == text:
        return False
    STRUCTURE.write_text(new, encoding="utf-8")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成项目完整目录树")
    parser.add_argument("--depth", type=int, default=3, help="最大展示深度（默认 3）")
    parser.add_argument("--write", action="store_true", help="回填 docs/STRUCTURE.md")
    args = parser.parse_args(argv)
    tree = build_tree(max_depth=max(1, args.depth))
    if args.write:
        changed = write_block(tree)
        print(("[tree] 已更新 " if changed else "[tree] 无变化 ") + str(STRUCTURE))
        return 0
    sys.stdout.reconfigure(encoding="utf-8")
    print(tree)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
