#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫描 Markdown 相对链接并可选修复常见断链（文档收口用）。

用法::

    python tools/check_docs_links.py            # 只报告
    python tools/check_docs_links.py --fix      # 修常见断链（plans/ -> docs/plans/）
    python tools/check_docs_links.py --json

跳过：.git / node_modules / .venv / __pycache__ / agi/data / scratch / docs/archive/20261028-docs-snapshot。
外部链接、纯锚点、mailto 不检查。
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", "node_modules", ".venv", "__pycache__", "agi/data", "scratch",
             "docs/archive", "docs/sources", "docs/analysis"}
LINK_RE = re.compile(r"(!?\[[^\]]*\]\()([^)\s]+)([^)]*\))")
EXTERNAL_RE = re.compile(r"^(https?:|mailto:|tel:|#|data:)", re.I)


def _markdown_files(root: Path) -> list[Path]:
    files = []
    docs = root / "docs"
    if docs.is_dir():
        for path in docs.rglob("*.md"):
            rel = path.relative_to(root).as_posix()
            if any(rel == skip or rel.startswith(skip + "/") for skip in SKIP_DIRS):
                continue
            files.append(path)
    for name in ("README.md",):
        path = root / name
        if path.is_file():
            files.append(path)
    return sorted(files)


def _resolve(source: Path, target: str) -> Path | None:
    raw = unquote(target.split("#", 1)[0].split("?", 1)[0]).strip()
    if not raw:
        return None
    if EXTERNAL_RE.match(raw) or raw.startswith("//"):
        return None
    candidate = (source.parent / raw).resolve()
    return candidate


def check(root: Path) -> dict:
    broken = []
    total_links = 0
    for path in _markdown_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="replace")
        for match in LINK_RE.finditer(text):
            target = match.group(2).strip()
            if not target or EXTERNAL_RE.match(target):
                continue
            total_links += 1
            resolved = _resolve(path, target)
            if resolved is None:
                continue
            if not resolved.exists():
                broken.append({"file": path.relative_to(root).as_posix(),
                               "target": target,
                               "resolved": str(resolved)})
    return {"ok": not broken, "total_links": total_links, "broken": broken,
            "broken_count": len(broken)}


def fix(root: Path) -> dict:
    """保守修复：根 plans/ 已并入 docs/plans/，把 `](plans/` 改 `](docs/plans/`。"""
    changed = []
    for path in _markdown_files(root):
        text = path.read_text(encoding="utf-8")
        new = text.replace("](plans/", "](docs/plans/")
        new = new.replace("](../plans/", "](../docs/plans/")
        if new != text:
            path.write_text(new, encoding="utf-8")
            changed.append(path.relative_to(root).as_posix())
    return {"changed": changed}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="检查/修复 Markdown 相对链接")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--fix", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    fixed = fix(root) if args.fix else {"changed": []}
    result = check(root)
    result["fixed"] = fixed["changed"]
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(f"[links] total={result['total_links']} broken={result['broken_count']}")
        for item in result["broken"]:
            print(f"  {item['file']}: {item['target']} -> {item['resolved']}")
        if fixed["changed"]:
            print("[links] fixed:", ", ".join(fixed["changed"]))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
