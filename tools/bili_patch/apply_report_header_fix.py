# -*- coding: utf-8 -*-
"""给「刷B站」插件打补丁：历史失败占位报告不再长期挂在日报头部。

问题：占位报告只在"报告不存在"时补；之后成功的轮次是**追加**写的，
      于是 out/<日期>.md 的头部永远是那句「这一轮没跑起来」，用户以为当天又挂了。

做法：每轮结束写「投递」笔记前，若发现头部是失败占位（没跑起来/没写出报告），
      把整份移到 out/_failed/<日期>-<时间戳>.md 归档，再重开一份正常日报。

⚠️ 插件升级/重装会覆盖 node_modules 里的代码；升级后重新执行本脚本即可。

用法：
    python tools/bili_patch/apply_report_header_fix.py [--profile desktop]
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

HOME = Path.home() / ".dsh" / "profiles"


def target_file(profile: str) -> Path:
    return HOME / profile / "node_modules" / "dsh-bili-scroll" / "index.js"


HELPER = '''    /**
     * ⭐ 修：历史失败占位报告会把"没跑起来"的报错一直挂在日报开头（本仓库补丁）。
     */
    const adoptStaleFailureReport = () => {
      try {
        const f = join(outDir, `${dayKey()}.md`)
        if (!existsSync(f)) return
        const cur = readFileSync(f, 'utf8')
        const head = cur.slice(0, 200)
        if (!/没跑起来|没写出报告/.test(head)) return
        const failedDir = join(outDir, '_failed')
        try { mkdirSync(failedDir, { recursive: true }) } catch { /* ignore */ }
        const stamp = new Date().toISOString().replace(/[:.]/g, '-')
        try { renameSync(f, join(failedDir, `${dayKey()}-${stamp}.md`)) }
        catch {
          writeFileSync(f, `# 刷B站 · ${dayKey()}\n\n`, 'utf8')
          return
        }
        writeFileSync(f, `# 刷B站 · ${dayKey()}\n\n`, 'utf8')
        log(`日报头部是历史失败占位 ⇒ 已归档到 out/_failed/，本日报告重开`)
      } catch { /* 归档失败不影响这一轮 */ }
    }

'''

ANCHOR = "    const writeRoundNotesToReport = () => {"
CALL_OLD = "    const writeRoundNotesToReport = () => {\n      try {\n        if (!roundNotes.length) return"
CALL_NEW = "    const writeRoundNotesToReport = () => {\n      try {\n        adoptStaleFailureReport()\n        if (!roundNotes.length) return"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="desktop")
    args = ap.parse_args()
    target = target_file(args.profile)
    if not target.is_file():
        print(f"找不到插件：{target}", file=sys.stderr)
        return 2
    src = target.read_text(encoding="utf-8")
    if "adoptStaleFailureReport" in src:
        print("已经是打过补丁的版本，跳过")
        return 0
    if ANCHOR not in src or CALL_OLD not in src:
        print("插件结构变了，找不到锚点；请人工检查后再打补丁", file=sys.stderr)
        return 3
    backup = target.with_name(target.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(target, backup)
    out = src.replace(ANCHOR, HELPER + ANCHOR, 1).replace(CALL_OLD, CALL_NEW, 1)
    target.write_text(out, encoding="utf-8")
    print(f"已打补丁：{target}\n备份：{backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
