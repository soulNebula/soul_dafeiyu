# -*- coding: utf-8 -*-
"""聊天截图 OCR 工具：排序/角色推断/气泡合并/坏文件（OCR 全 mock，不跑真模型）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from PIL import Image

import tools.ocr_image as ocr_image
from wx_core.models import OcrLine


def _line(text, box):
    return OcrLine(text=text, box=box)


def test_sort_merges_same_row_and_orders():
    rows = ocr_image._sort_lines([
        _line("右下", (500, 200, 600, 230)),
        _line("左下", (40, 200, 140, 230)),
        _line("上方", (40, 40, 140, 70)),
    ])
    assert [r["text"] for r in rows] == ["上方", "左下右下"]


def test_infer_roles_left_right():
    rows = ocr_image._sort_lines([
        _line("左", (40, 40, 140, 70)),
        _line("右", (600, 120, 700, 150)),
    ])
    ocr_image._infer_roles(rows, width=800, height=600)
    assert rows[0]["role"] == "you" and rows[1]["role"] == "me"


def test_merge_bubbles_multi_line_and_switch():
    rows = ocr_image._sort_lines([
        _line("第一行", (40, 40, 140, 70)),
        _line("第二行", (40, 78, 140, 108)),
        _line("回复", (600, 150, 700, 180)),
    ])
    ocr_image._infer_roles(rows, width=800, height=600)
    bubbles = ocr_image._merge_bubbles(rows)
    assert len(bubbles) == 2
    assert bubbles[0]["role"] == "you" and "第一行" in bubbles[0]["text"] \
        and "第二行" in bubbles[0]["text"]
    assert bubbles[1]["role"] == "me"


def test_process_one_ok_with_mock(monkeypatch, tmp_path):
    img = tmp_path / "shot.png"
    Image.new("RGB", (800, 600), "black").save(img)
    monkeypatch.setattr(ocr_image, "_ocr", lambda image: ([
        _line("今天好累", (40, 40, 140, 70)),
        _line("先瘫着", (600, 120, 700, 150)),
    ], "rapidocr", 12.5))
    result = ocr_image._process_one(img, infer_role=True)
    assert result["ok"] is True
    assert result["engine"] == "rapidocr" and len(result["lines"]) == 2
    assert [b["role"] for b in result["bubble_lines"]] == ["you", "me"]


def test_process_one_bad_file(tmp_path):
    result = ocr_image._process_one(tmp_path / "missing.png", infer_role=True)
    assert result["ok"] is False and result["error"]


def test_process_one_ocr_empty(monkeypatch, tmp_path):
    img = tmp_path / "blank.png"
    Image.new("RGB", (400, 300), "white").save(img)
    monkeypatch.setattr(ocr_image, "_ocr", lambda image: ([], "windows", 5.0))
    result = ocr_image._process_one(img, infer_role=True)
    assert result["ok"] is False and result["error"] == "ocr_empty"


def test_main_outputs_json(monkeypatch, tmp_path, capsys):
    img = tmp_path / "shot.png"
    Image.new("RGB", (800, 600), "black").save(img)
    monkeypatch.setattr(ocr_image, "_ocr", lambda image: ([
        _line("hi", (40, 40, 140, 70)),
    ], "windows", 3.0))
    code = ocr_image.main(["--image", str(img)])
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 0 and payload["ok"] is True
    assert payload["results"][0]["bubble_lines"][0]["role"] == "you"


def test_main_no_image(capsys):
    code = ocr_image.main(["--image", ""])
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == 2 and payload["error"] == "no_image"
