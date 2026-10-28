# -*- coding: utf-8 -*-
"""为技术文章生成配图（架构/闭环/路由/性能/质量/数据/截图拼图）。

只在 docs/architecture/figures/ 下写 PNG，不参与运行时。
运行：python docs/architecture/figures/_build_figures.py
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
from PIL import Image, ImageDraw, ImageFont

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

OUT = Path(__file__).resolve().parent
SOUL = OUT.parents[2]                     # SoulAgent 根
IDEA = SOUL.parent / "idea"

C_BLUE = "#2f6fed"
C_DARK = "#1f2937"
C_GRAY = "#6b7280"
C_LIGHT = "#e8f0fe"
C_GREEN = "#16a34a"
C_ORANGE = "#f59e0b"
C_RED = "#dc2626"
C_PURPLE = "#7c3aed"


def _box(ax, x, y, w, h, text, fc=C_LIGHT, ec=C_BLUE, fs=10, bold=False):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.012,rounding_size=0.02",
                                linewidth=1.4, edgecolor=ec, facecolor=fc))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fs,
            color=C_DARK, fontweight="bold" if bold else "normal", wrap=True)


def _arrow(ax, x1, y1, x2, y2, color=C_GRAY, style="-|>", lw=1.4):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle=style,
                                 mutation_scale=13, linewidth=lw, color=color))


def fig_architecture():
    fig, ax = plt.subplots(figsize=(11, 7.2), dpi=170)
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    ax.text(0.5, 0.975, "小鱼（SoulAgent）分层架构", ha="center", fontsize=16,
            fontweight="bold", color=C_DARK)

    # 左侧：微信客户端 + 独立发送 venv
    _box(ax, 0.02, 0.70, 0.20, 0.18,
         "微信 PC 客户端\n（真实账号）", fc="#fff7ed", ec=C_ORANGE, fs=10, bold=True)
    _box(ax, 0.02, 0.47, 0.20, 0.16,
         "wechatauto-replica\nUIA/OCR 发送\n（独立 3.12 venv）", fc="#fff7ed", ec=C_ORANGE, fs=9)
    _box(ax, 0.02, 0.25, 0.20, 0.14,
         "SQLCipher 本地库\n（读取消息）", fc="#fff7ed", ec=C_ORANGE, fs=9)
    _arrow(ax, 0.12, 0.70, 0.12, 0.63, color=C_ORANGE)
    _arrow(ax, 0.22, 0.55, 0.28, 0.55, color=C_ORANGE)

    # 801 网关
    _box(ax, 0.28, 0.46, 0.28, 0.22,
         "801 网关\n\n· 入站队列 / 去重\n· 白名单 / 静默时段\n· 全局发送锁\n· 送达回执（unverified 单列）\n· 草稿 TTL",
         fc="#eef2ff", ec=C_BLUE, fs=9)

    # 认知层
    _box(ax, 0.28, 0.14, 0.28, 0.26,
         "认知与人格层（selflearn）\n\n记忆/衰减/画像/claims\n情绪与关系 · 风格守卫\n主动消息调度 · 日记\n朋友圈 · B站 · 联网 · 识图",
         fc="#f0fdf4", ec=C_GREEN, fs=9)

    # 灵魂层
    _box(ax, 0.60, 0.55, 0.18, 0.16,
         "8123 灵魂模型\nQwen2.5-0.5B v1b\nInnerState JSON", fc="#faf5ff", ec=C_PURPLE, fs=9)
    # 路由
    _box(ax, 0.60, 0.32, 0.18, 0.18,
         "agi/llm 路由\n\n本地 8125\nQwen2.5-VL-3B\n文本提取 + 识图\n\n云端 DeepSeek\n聊天/判断/兜底",
         fc="#fffbeb", ec=C_ORANGE, fs=8.5)
    # 802/803
    _box(ax, 0.80, 0.56, 0.18, 0.15,
         "802 控制台\n20+ 页面\n监控/记忆/内容", fc="#eff6ff", ec=C_BLUE, fs=9)
    _box(ax, 0.80, 0.34, 0.18, 0.17,
         "803 工作台\n模型/训练/数据\n语料/标注/蒸馏", fc="#eff6ff", ec=C_BLUE, fs=9)
    # 外部插件
    _box(ax, 0.60, 0.10, 0.38, 0.12,
         "DSH B站值勤插件（最小权限 profile：无文件系统/命令/联网，仅 MCP 工具）",
         fc="#f3f4f6", ec=C_GRAY, fs=8.5)
    _box(ax, 0.02, 0.05, 0.20, 0.10, "本地存储\nagi/data（gitignore）", fc="#f9fafb", ec=C_GRAY, fs=9)

    _arrow(ax, 0.42, 0.46, 0.42, 0.40, color=C_GREEN)
    _arrow(ax, 0.56, 0.58, 0.60, 0.62, color=C_PURPLE)
    _arrow(ax, 0.56, 0.30, 0.60, 0.40, color=C_ORANGE)
    _arrow(ax, 0.78, 0.63, 0.80, 0.63, color=C_BLUE)
    _arrow(ax, 0.78, 0.42, 0.80, 0.42, color=C_BLUE)
    _arrow(ax, 0.90, 0.34, 0.90, 0.22, color=C_GRAY)
    _arrow(ax, 0.60, 0.18, 0.56, 0.18, color=C_GRAY, style="<|-")
    ax.text(0.29, 0.06, "所有外发：白名单 + 闸门 + 审计；默认只服务本人账号", fontsize=8.5, color=C_GRAY)
    fig.tight_layout()
    fig.savefig(OUT / "fig_architecture.png", bbox_inches="tight", facecolor="white")
    plt.close(fig)


def fig_pipeline():
    fig, ax = plt.subplots(figsize=(11, 3.6), dpi=170)
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    ax.text(0.5, 0.92, "数据闭环：从聊天记录到可上线模型", ha="center", fontsize=15,
            fontweight="bold", color=C_DARK)
    stages = [
        ("① 采集\n聊天记录/截图\nB站/朋友圈", C_BLUE),
        ("② 清洗\nOCR/你·我分离\n噪声过滤", C_GREEN),
        ("③ 标注\nInnerState 打分\n用户标注", C_PURPLE),
        ("④ 蒸馏\n教师生成语义\nschema 校验", C_ORANGE),
        ("⑤ 训练\nLoRA / 全参\n合并导出 GGUF", C_RED),
        ("⑥ 评估\n离线测试集\n线上闸门", C_GRAY),
    ]
    w, gap = 0.135, 0.028
    x = 0.015
    for text, color in stages:
        _box(ax, x, 0.40, w, 0.36, text, fc="white", ec=color, fs=9)
        if x > 0.02:
            _arrow(ax, x - gap + 0.002, 0.58, x - 0.002, 0.58, color=C_GRAY)
        x += w + gap
    _arrow(ax, 0.93, 0.40, 0.93, 0.16, color=C_GRAY)
    _arrow(ax, 0.09, 0.16, 0.09, 0.40, color=C_GRAY)
    ax.text(0.5, 0.14, "数据回流：线上反馈 → 新标注 → 再训练（持续迭代）",
            ha="center", fontsize=10, color=C_GRAY)
    fig.tight_layout()
    fig.savefig(OUT / "fig_pipeline.png", bbox_inches="tight", facecolor="white")
    plt.close(fig)


def fig_routing():
    fig, ax = plt.subplots(figsize=(10, 4.2), dpi=170)
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    ax.text(0.5, 0.94, "按用途路由：本地省钱，云端保质量", ha="center", fontsize=15,
            fontweight="bold", color=C_DARK)
    _box(ax, 0.03, 0.62, 0.22, 0.24,
         "高吞吐 / 低判断\n关键词抽取 0.3s\n摘要 · 评论聚类\n相关性打分",
         fc="white", ec=C_GREEN, fs=9)
    _box(ax, 0.03, 0.30, 0.22, 0.24,
         "多模态识图\ncaption/标签/情绪\n1.1–2.1s/张\n失败回落 API",
         fc="white", ec=C_PURPLE, fs=9)
    _box(ax, 0.03, 0.02, 0.22, 0.20,
         "纯规则\n零成本 / 可解释", fc="white", ec=C_GRAY, fs=9)
    _box(ax, 0.36, 0.42, 0.26, 0.28,
         "agi/llm 路由\n\n预算（30万 token/天）\n超限自动降级\n探活 + 失败回退\n用量 JSONL 记账",
         fc="#fffbeb", ec=C_ORANGE, fs=9.5)
    _box(ax, 0.72, 0.55, 0.25, 0.28,
         "本地 llama-server\n8125 Qwen2.5-VL-3B\n（GPU RTX 4050）\n8123 0.5B 灵魂",
         fc="#f0fdf4", ec=C_GREEN, fs=9)
    _box(ax, 0.72, 0.10, 0.25, 0.28,
         "云端 DeepSeek\n聊天回复 / 情绪判断\n分享文案 / 视频理解\n（强制走 API）",
         fc="#eff6ff", ec=C_BLUE, fs=9)
    _arrow(ax, 0.25, 0.74, 0.36, 0.62, color=C_GREEN)
    _arrow(ax, 0.25, 0.42, 0.36, 0.52, color=C_PURPLE)
    _arrow(ax, 0.25, 0.12, 0.36, 0.44, color=C_GRAY)
    _arrow(ax, 0.62, 0.62, 0.72, 0.68, color=C_GREEN)
    _arrow(ax, 0.62, 0.42, 0.72, 0.26, color=C_BLUE)
    fig.tight_layout()
    fig.savefig(OUT / "fig_routing.png", bbox_inches="tight", facecolor="white")
    plt.close(fig)


def fig_perf():
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.0), dpi=170)
    labels = ["识图 480p", "识图 720p", "关键词抽取", "OCR 单张", "OCR 18 张整批"]
    cpu = [62.0, 62.0, 2.7, 22.6, 126.0]
    gpu = [1.1, 2.1, 0.3, 8.4, 126.0]
    x = range(len(labels))
    ax1.bar([i - 0.2 for i in x], cpu, width=0.38, label="优化前（CPU）", color="#fca5a5")
    ax1.bar([i + 0.2 for i in x], gpu, width=0.38, label="优化后（GPU/直连）", color="#4ade80")
    ax1.set_yscale("log")
    ax1.set_xticks(list(x)); ax1.set_xticklabels(labels, fontsize=9)
    ax1.set_ylabel("秒（对数坐标）", fontsize=9)
    ax1.set_title("本地推理/OCR 延迟（实测）", fontsize=11, fontweight="bold")
    ax1.legend(fontsize=8); ax1.grid(axis="y", alpha=0.25)
    for i, (a, b) in enumerate(zip(cpu, gpu)):
        ax1.text(i - 0.2, a * 1.12, f"{a:g}", ha="center", fontsize=8, color="#7f1d1d")
        ax1.text(i + 0.2, b * 1.12, f"{b:g}", ha="center", fontsize=8, color="#14532d")

    models = ["识图\n(480p)", "文本提取\n(关键词)", "灵魂 0.5B\n生成", "聊天回复\n(云端)"]
    lat = [1.1, 0.3, 1.7, 3.5]
    colors = [C_PURPLE, C_GREEN, C_PURPLE, C_BLUE]
    ax2.barh(models, lat, color=colors, height=0.55)
    ax2.invert_yaxis()
    ax2.set_xlabel("秒 / 次（实测均值）", fontsize=9)
    ax2.set_title("各用途端到端延迟", fontsize=11, fontweight="bold")
    for i, v in enumerate(lat):
        ax2.text(v + 0.05, i, f"{v:g}s", va="center", fontsize=9)
    ax2.grid(axis="x", alpha=0.25)
    fig.tight_layout()
    fig.savefig(OUT / "fig_perf.png", bbox_inches="tight", facecolor="white")
    plt.close(fig)


def fig_quality():
    dims = ["功能完成度", "主链健壮性", "安全与合规", "测试规模", "测试可证伪",
            "架构清晰度", "可移植性", "文档事实性", "叙事诚实度"]
    before = [8.5, 4.5, 5.5, 7.5, 4.5, 3.5, 7.0, 4.0, 5.0]
    after = [8.5, 7.5, 7.5, 8.0, 7.5, 7.5, 7.5, 8.0, 7.5]
    fig, ax = plt.subplots(figsize=(10.5, 4.4), dpi=170)
    y = range(len(dims))
    ax.barh([i + 0.2 for i in y], before, height=0.36, label="改前（综合≈5.4）",
            color="#fca5a5")
    ax.barh([i - 0.2 for i in y], after, height=0.36, label="改后（综合 7.7）",
            color="#4ade80")
    ax.set_yticks(list(y)); ax.set_yticklabels(dims, fontsize=9.5)
    ax.invert_yaxis()
    ax.axvline(7, color=C_RED, linestyle="--", linewidth=1.2, alpha=0.8)
    ax.text(7.05, -0.55, "可交付线 7.0", color=C_RED, fontsize=8.5)
    ax.set_xlim(0, 10)
    ax.set_xlabel("评分（10 分制）", fontsize=9)
    ax.legend(fontsize=8.5, loc="lower right")
    ax.set_title("Agent B 审计整改：9 个维度全部 ≥7", fontsize=12, fontweight="bold")
    ax.grid(axis="x", alpha=0.25)
    fig.tight_layout()
    fig.savefig(OUT / "fig_quality.png", bbox_inches="tight", facecolor="white")
    plt.close(fig)


def fig_data():
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 4.0), dpi=170)
    names = ["messages", "turns", "memories", "bili", "media", "diary"]
    vals = [531, 223, 113, 44, 21, 2]
    ax1.bar(names, vals, color=["#60a5fa", "#93c5fd", "#a5b4fc", "#c4b5fd", "#ddd6fe", "#e9d5ff"])
    ax1.set_yscale("log")
    ax1.set_title("单个联系人库的数据规模（行）", fontsize=11, fontweight="bold")
    ax1.set_ylabel("行数（对数）", fontsize=9)
    for i, v in enumerate(vals):
        ax1.text(i, v * 1.15, str(v), ha="center", fontsize=8.5)
    ax1.tick_params(axis="x", labelsize=8.5)
    ax1.grid(axis="y", alpha=0.25)

    labels = ["SoulAgent\ntests", "SoulAgent\nagi/tests", "IDEA\npytest", "全服务\n冒烟"]
    counts = [301, 2220, 101, 13]
    colors = [C_BLUE, C_BLUE, C_GREEN, C_ORANGE]
    bars = ax2.bar(labels, counts, color=colors)
    ax2.set_yscale("log")
    ax2.set_title("自动化测试规模（2026-10-29 实测）", fontsize=11, fontweight="bold")
    ax2.set_ylabel("通过数（对数）", fontsize=9)
    for b, v in zip(bars, counts):
        ax2.text(b.get_x() + b.get_width() / 2, v * 1.15, str(v), ha="center", fontsize=9)
    ax2.grid(axis="y", alpha=0.25)
    fig.tight_layout()
    fig.savefig(OUT / "fig_data.png", bbox_inches="tight", facecolor="white")
    plt.close(fig)


def _font(size=22):
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf"):
        try:
            return ImageFont.truetype(f"C:/Windows/Fonts/{name}", size)
        except OSError:
            continue
    return ImageFont.load_default()


def _label(img: Image.Image, text: str, bar=46, font_size=22) -> Image.Image:
    canvas = Image.new("RGB", (img.width, img.height + bar), "white")
    canvas.paste(img, (0, bar))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle([0, 0, canvas.width, bar], fill="#1f2937")
    draw.text((14, 9), text, fill="white", font=_font(font_size))
    return canvas


def fig_shots():
    """把真实截图拼成 2×2 展示图。"""
    picks = [
        (IDEA / ".data/screens/corpus_progress.png", "803 语料清洗：OCR 进度与“你/我”筛选"),
        (IDEA / ".data/screens/annotate_chat.png", "803 用户标注：微信式气泡 + InnerState 编辑"),
        (SOUL / ".data/screens/bili_feed.png", "802 B站：刷到的视频 / 每日摘要 / 报告"),
        (SOUL / ".data/screens/diary.png", "802 日记：每晚自动写、只读阅读器"),
    ]
    tiles = []
    for path, caption in picks:
        try:
            img = Image.open(path).convert("RGB")
        except OSError:
            continue
        img.thumbnail((1200, 750))
        tiles.append(_label(img, caption))
    if not tiles:
        return
    w = max(t.width for t in tiles)
    tiles = [t.resize((w, int(t.height * w / t.width))) for t in tiles]
    cols, rows = 2, (len(tiles) + 1) // 2
    gap = 18
    h = sum(t.height for t in tiles[:rows]) // rows if rows else 0
    heights = [max(t.height for t in tiles[i * cols:(i + 1) * cols]) for i in range(rows)]
    canvas = Image.new("RGB", (w * cols + gap * (cols + 1), sum(heights) + gap * (rows + 1)),
                       "#f3f4f6")
    y = gap
    for r in range(rows):
        for c in range(cols):
            idx = r * cols + c
            if idx >= len(tiles):
                continue
            canvas.paste(tiles[idx], (gap + c * (w + gap), y))
        y += heights[r] + gap
    canvas.save(OUT / "fig_shots.png")


if __name__ == "__main__":
    fig_architecture()
    fig_pipeline()
    fig_routing()
    fig_perf()
    fig_quality()
    fig_data()
    fig_shots()
    print("figures ->", OUT)
    for p in sorted(OUT.glob("*.png")):
        print(" ", p.name, p.stat().st_size // 1024, "KB")
