# -*- coding: utf-8 -*-
"""803 本机硬件探测：GPU/显存/驱动 → 本地模型方案推荐。

原则：没有 GPU 也能跑本地（llama.cpp CPU 版）；模型中心/新建 Bot 的本地模式
提供一键下载与进度，不再只给结论。
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from typing import Any


def _nvidia() -> list[dict[str, Any]]:
    exe = shutil.which("nvidia-smi")
    if not exe:
        return []
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except Exception:  # noqa: BLE001
        return []
    gpus = []
    for line in out.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            vram = int(float(parts[1]))
        except ValueError:
            vram = 0
        gpus.append({"name": parts[0], "vram_mb": vram,
                     "driver": parts[2] if len(parts) > 2 else ""})
    return gpus


def _recommend(gpus: list[dict[str, Any]]) -> dict[str, Any]:
    if not gpus:
        return {
            "mode": "cloud",
            "reason": "没有检测到 NVIDIA GPU（或驱动未安装）",
            "models": [],
            "local_training": False,
            "hint": "按你的选择走云端方案；不需要下载本地灵魂/识图模型。",
        }
    vram = max(int(gpu.get("vram_mb") or 0) for gpu in gpus)
    if vram < 6 * 1024:
        return {"mode": "cloud", "reason": f"显存 {vram} MB < 6 GB，本地跑不动训练",
                "models": [], "local_training": False,
                "hint": "建议云端方案；能跑 0.5B 推理但不建议训练。"}
    if vram < 8 * 1024:
        models = ["soul-05b-q4 (0.5B 内状态)", "qwen25-vl-3b-q4 (识图/文本提取)"]
        return {"mode": "local+cloud", "reason": f"显存 {vram} MB，可跑 0.5B + 3B 推理",
                "models": models, "local_training": True,
                "hint": "训练建议 LoRA + 4bit，batch 小；云端仍保留作兜底。"}
    if vram < 12 * 1024:
        models = ["soul-05b-q4", "qwen25-vl-3b-q4", "qwen25-7b-q4 (可选)"]
        return {"mode": "local+cloud", "reason": f"显存 {vram} MB，可跑 7B 量化推理",
                "models": models, "local_training": True,
                "hint": "可训 0.5B/3B LoRA；7B 训练需 QLoRA。"}
    models = ["soul-05b-q4", "qwen25-vl-3b-q4", "qwen25-7b-q4", "qwen25-14b-q4 (可选)"]
    return {"mode": "local+cloud", "reason": f"显存 {vram} MB，可跑 14B 量化/多模型",
            "models": models, "local_training": True,
            "hint": "可同时跑灵魂 + 识图 + 文本提取；训练/蒸馏空间充足。"}


def probe() -> dict[str, Any]:
    gpus = _nvidia()
    rec = _recommend(gpus)
    return {
        "ok": True,
        "os": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count() or 0,
        "gpus": gpus,
        "recommendation": rec,
        "note": "本地模型文件不随仓库分发；下载/安装见 Phase 2（当前先支持云端方案）。",
    }
