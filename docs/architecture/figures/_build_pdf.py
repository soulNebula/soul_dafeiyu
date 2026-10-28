# -*- coding: utf-8 -*-
"""把文章 Markdown + 配图渲染成 PDF（Chromium/Edge headless print-to-pdf）。

产物：docs/architecture/小鱼-AI伴侣-技术与实践.pdf
源文件：docs/architecture/source/小鱼-AI伴侣-技术与实践.md
配图：docs/architecture/figures/*.png（md 里以 `figures/xx.png` 引用，这里自动转 data URI）
"""
from __future__ import annotations

import base64
import html
import re
import subprocess
import sys
from pathlib import Path

import markdown

HERE = Path(__file__).resolve().parent
ARCH = HERE.parent
ROOT = ARCH.parents[1]
SOURCE = ARCH / "source" / "小鱼-AI伴侣-技术与实践.md"
FIG_DIR = ARCH / "figures"
OUT_PDF = ARCH / "小鱼-AI伴侣-技术与实践.pdf"
TMP_HTML = ARCH / ".article_print.html"
EDGE = Path(r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe")

CSS = """
@page { size: A4; margin: 17mm 15mm 16mm 15mm; }
* { box-sizing: border-box; }
body { font-family: "Microsoft YaHei", "Segoe UI", sans-serif; color: #1f2937;
       font-size: 10.4pt; line-height: 1.72; margin: 0; }
.cover { height: 258mm; display: flex; flex-direction: column; justify-content: center;
         page-break-after: always; text-align: center; }
.cover .kicker { color: #2f6fed; font-size: 12pt; letter-spacing: 3px; margin-bottom: 10mm; }
.cover h1 { font-size: 30pt; line-height: 1.35; margin: 0 0 6mm; color: #111827;
            border: none; padding: 0; }
.cover .sub { font-size: 14pt; color: #4b5563; margin-bottom: 12mm; }
.cover .hero { width: 100%; border-radius: 3mm; border: 1px solid #e5e7eb; }
.cover .meta { margin-top: 10mm; font-size: 10pt; color: #6b7280; line-height: 2; }
h2 { page-break-before: auto; font-size: 17pt; color: #1d4ed8; border-top: 1.5px solid #dbeafe;
     border-left: 5px solid #2f6fed; padding: 7mm 0 0 8px; margin: 0 0 5mm;
     page-break-after: avoid; }
h3 { font-size: 13pt; color: #111827; margin: 6mm 0 2.5mm; page-break-after: avoid; }
h4 { font-size: 11.5pt; margin: 4mm 0 2mm; page-break-after: avoid; }
p { margin: 2.2mm 0; text-align: justify; }
ul, ol { margin: 2mm 0 2mm 6mm; padding-left: 4mm; }
li { margin: 1.2mm 0; }
strong { color: #111827; }
a { color: #1d4ed8; text-decoration: none; word-break: break-all; }
table { border-collapse: collapse; width: 100%; margin: 3mm 0 4mm; font-size: 9pt;
        page-break-inside: avoid; }
th { background: #eff6ff; color: #1e3a8a; font-weight: 600; }
th, td { border: 0.6pt solid #d1d5db; padding: 1.6mm 2mm; vertical-align: top; }
tr:nth-child(even) td { background: #f9fafb; }
pre { background: #f6f8fa; border: 1px solid #e5e7eb; border-radius: 2mm;
      padding: 2.5mm 3mm; font-size: 8.4pt; line-height: 1.5; overflow-x: hidden;
      white-space: pre-wrap; word-break: break-all; page-break-inside: avoid; }
code { font-family: "Cascadia Mono", Consolas, monospace; font-size: 8.8pt;
       background: #f3f4f6; padding: 0 1px; border-radius: 1mm; }
pre code { background: none; padding: 0; }
blockquote { border-left: 3px solid #93c5fd; background: #f8fafc; margin: 3mm 0;
             padding: 2mm 4mm; color: #374151; font-size: 9.6pt; }
img { max-width: 100%; border: 1px solid #e5e7eb; border-radius: 2mm;
      page-break-inside: avoid; margin: 3mm auto 1mm; display: block; }
p > em:only-child { display: block; text-align: center; color: #6b7280;
                    font-size: 9pt; margin: 0 0 5mm; }
hr { border: none; border-top: 1px solid #e5e7eb; margin: 5mm 0; }
"""

COVER = """<div class="cover">
  <div class="kicker">SOULAGENT · 2026</div>
  <h1>从微信聊天到本地智能体</h1>
  <div class="sub">小鱼（SoulAgent）的设计、实现与实测</div>
  <img class="hero" src="FIG_ARCH" />
  <div class="meta">
    版本 2026-10-29 ｜ 运行环境：Windows 10 / Python 3.14.7 / RTX 4050 Laptop 6GB<br/>
    代码仓库：SoulAgent（智能体核心与网关）+ IDEA（工作台）<br/>
    说明：文中不公开真实聊天内容与密钥，涉及人物的数据均已匿名/聚合处理。
  </div>
</div>"""


def _data_uri(path: Path) -> str:
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{data}"


def build_html() -> str:
    md_text = SOURCE.read_text(encoding="utf-8")
    body = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "sane_lists", "nl2br", "toc"],
        output_format="html5",
    )

    def repl(match: re.Match) -> str:
        rel = match.group(1)
        path = (FIG_DIR / rel).resolve()
        if not path.is_file():
            return match.group(0)
        return f'src="{_data_uri(path)}"'

    body = re.sub(r'src="(?:\.\./)?figures/([^"]+)"', repl, body)
    # 封面已包含标题/说明：去掉正文开头重复的 h1 + 引用块 + 分隔线
    body = re.sub(r'^\s*<h1[^>]*>.*?</h1>\s*', '', body, count=1, flags=re.S)
    body = re.sub(r'^\s*<blockquote>.*?</blockquote>\s*', '', body, count=1, flags=re.S)
    body = re.sub(r'^\s*<hr\s*/?>\s*', '', body, count=1)
    hero = _data_uri(FIG_DIR / "fig_architecture.png")
    cover = COVER.replace("FIG_ARCH", hero)
    return (
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>"
        "<title>小鱼（SoulAgent）技术与实践</title>"
        f"<style>{CSS}</style></head><body>{cover}{body}</body></html>"
    )


def render_pdf(html_text: str) -> None:
    if not EDGE.is_file():
        raise SystemExit(f"找不到 Edge：{EDGE}")
    TMP_HTML.write_text(html_text, encoding="utf-8")
    cmd = [
        str(EDGE), "--headless=new", "--disable-gpu", "--no-sandbox",
        "--no-pdf-header-footer", "--run-all-compositor-stages-before-draw",
        f"--print-to-pdf={OUT_PDF}",
        TMP_HTML.resolve().as_uri(),
    ]
    proc = subprocess.run(cmd, capture_output=True, timeout=180)
    if not OUT_PDF.is_file():
        raise SystemExit(f"PDF 生成失败：{proc.stderr.decode('utf-8', 'replace')[-800:]}")
    print("PDF:", OUT_PDF, f"{OUT_PDF.stat().st_size / 1024:.0f} KB")
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(OUT_PDF))
        text = (reader.pages[0].extract_text() or "")[:200].replace("\n", " ")
        print("pages:", len(reader.pages), "| first page:", text[:120])
    except Exception as exc:  # noqa: BLE001
        print("（跳过校验）", exc)


if __name__ == "__main__":
    render_pdf(build_html())
    TMP_HTML.unlink(missing_ok=True)
    print("done")
