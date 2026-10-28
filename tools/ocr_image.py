# -*- coding: utf-8 -*-
"""聊天截图 OCR：识别图片里的文字行，并按气泡左右位置推断「你 / 我」。

用法::

    python tools/ocr_image.py --image shot1.png shot2.jpg
    python tools/ocr_image.py --image shot.png --no-infer-role --json

输出（stdout 最后一行是 JSON）::

    {"ok": true, "results": [{
        "file": "...", "width": 1280, "height": 2276, "scale": 1.0,
        "engine": "rapidocr", "ms": 123,
        "lines": [{"text": "...", "box": [l,t,r,b], "cx": 1.2, "cy": 3.4, "role": "you"}],
        "bubble_lines": [{"role": "me", "text": "..."}]
    }]}

说明
----
- OCR 引擎复用 ``wx_core.ocr.OCRWorker``（RapidOCR 主、Windows OCR 降级）。
- 角色推断：文字行中心在图片右半边 → ``me``（自己/我），左半边 → ``you``（对方/你）。
- ``bubble_lines`` 把相邻同角色、垂直间距 < 1.6 倍行高的行合并成一个气泡文本。
- 只读图片文件，不接触微信进程；识别失败返回 ``ok: false`` + error，不抛异常。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
MAX_WIDTH = 1600


def _load_image(path: Path):
    from PIL import Image
    img = Image.open(path)
    img = img.convert("RGB")
    width, height = img.size
    scale = 1.0
    if width > MAX_WIDTH:
        scale = MAX_WIDTH / float(width)
        img = img.resize((MAX_WIDTH, max(1, int(height * scale))))
    return img, width, height, scale


def _ocr(img):
    """返回 (lines, engine, ms)；异常时抛出，由调用方兜底。"""
    from wx_core.ocr import OCRWorker
    started = time.time()
    with OCRWorker() as worker:
        lines = worker.ocr(img) or []
        engine = str(getattr(worker, "last_engine", "") or "")
        if not engine:
            engine = "rapidocr" if "rapidocr" in repr(worker).lower() else "windows"
    return lines, engine, round((time.time() - started) * 1000.0, 1)


def _sort_lines(lines):
    """按 y 排序；同一视觉行（y 差 ≤8px）内按 x 排。"""
    rows = []
    for line in lines:
        text = str(getattr(line, "text", "") or "").strip()
        if not text:
            continue
        box = tuple(int(v) for v in (getattr(line, "box", (0, 0, 0, 0)) or (0, 0, 0, 0)))
        left, top, right, bottom = box
        rows.append({"text": text, "box": [left, top, right, bottom],
                     "cx": round((left + right) / 2.0, 1),
                     "cy": round((top + bottom) / 2.0, 1),
                     "h": max(1, bottom - top)})
    rows.sort(key=lambda r: (r["cy"], r["cx"]))
    # 同 y（±8px）的分段合并成同一行：按 cx 排序拼在一起
    merged = []
    for row in rows:
        if merged and abs(row["cy"] - merged[-1]["cy"]) <= 8:
            prev = merged[-1]
            prev["text"] = prev["text"] + row["text"]
            prev["box"] = [min(prev["box"][0], row["box"][0]),
                           min(prev["box"][1], row["box"][1]),
                           max(prev["box"][2], row["box"][2]),
                           max(prev["box"][3], row["box"][3])]
            prev["cx"] = round((prev["box"][0] + prev["box"][2]) / 2.0, 1)
            prev["cy"] = round((prev["box"][1] + prev["box"][3]) / 2.0, 1)
            prev["h"] = max(prev["h"], row["h"])
            continue
        merged.append(dict(row))
    return merged


def _infer_roles(rows, width, height):
    center = width / 2.0
    for row in rows:
        row["role"] = "me" if row["cx"] >= center else "you"


def _merge_bubbles(rows):
    """相邻同角色、y 间距 < 1.6 倍行高的行 → 一个气泡。"""
    bubbles = []
    for row in rows:
        if bubbles and bubbles[-1]["role"] == row["role"]:
            last = bubbles[-1]
            gap = row["cy"] - last["_last_cy"]
            ref_h = max(float(row["h"]), float(last["_last_h"]), 1.0)
            if gap <= ref_h * 1.6:
                last["text"] = last["text"] + "\n" + row["text"]
                last["_last_cy"] = row["cy"]
                last["_last_h"] = row["h"]
                continue
        bubbles.append({"role": row["role"], "text": row["text"],
                        "cy": row["cy"], "_last_cy": row["cy"], "_last_h": row["h"]})
    for bubble in bubbles:
        bubble.pop("_last_cy", None)
        bubble.pop("_last_h", None)
    return bubbles




def _estimate_bg(img) -> tuple[int, int, int]:
    """取四角+边缘像素的中位数当背景色。"""
    width, height = img.size
    samples = []
    step_x = max(1, width // 24)
    step_y = max(1, height // 24)
    for x in range(0, width, step_x):
        samples.append(img.getpixel((x, 2)))
        samples.append(img.getpixel((x, height - 3)))
    for y in range(0, height, step_y):
        samples.append(img.getpixel((2, y)))
        samples.append(img.getpixel((width - 3, y)))
    if not samples:
        return (0, 0, 0)
    r = sorted(p[0] for p in samples)[len(samples) // 2]
    g = sorted(p[1] for p in samples)[len(samples) // 2]
    b = sorted(p[2] for p in samples)[len(samples) // 2]
    return (r, g, b)


def _detect_media_regions(img, rows, width: int, height: int, *,
                          top_cut: int = 80, bottom_cut: int = 70) -> list[dict]:
    """在文字行之间的空白带里检测"图片/贴纸气泡"，返回占位符条目。

    启发式：把空白带缩放到宽 64，按与背景色的差异找显著列；
    只取连续显著列组成的 bbox（宽度 >75% 图宽视为背景噪声，丢弃）；
    角色按 bbox 中心 x 判左右，贴纸/小图 → [表情包]，大图 → [图片]。
    """
    if width <= 0 or height <= 0:
        return []
    ordered = sorted(rows, key=lambda r: float(r["cy"]))
    bands: list[tuple[float, float]] = []
    prev_bottom = float(top_cut)
    for row in ordered:
        top = float(row["cy"]) - float(row["h"]) / 2.0
        bottom = float(row["cy"]) + float(row["h"]) / 2.0
        if top - prev_bottom >= 72:
            bands.append((prev_bottom, top))
        prev_bottom = max(prev_bottom, bottom)
    if height - bottom_cut - prev_bottom >= 72:
        bands.append((prev_bottom, float(height - bottom_cut)))
    if not bands:
        return []
    bg = _estimate_bg(img)
    found: list[dict] = []
    for y0, y1 in bands:
        try:
            region = img.crop((0, int(y0), width, int(y1)))
        except Exception:  # noqa: BLE001
            continue
        band_h = max(1, region.height)
        scale = 64.0 / max(1, region.width)
        small = region.resize((64, max(2, int(band_h * scale))))
        width_s, height_s = small.size
        columns = [0] * width_s
        mask_rows = 0
        for yy in range(height_s):
            row_hit = False
            for xx in range(width_s):
                pixel = small.getpixel((xx, yy))
                dist = abs(pixel[0] - bg[0]) + abs(pixel[1] - bg[1]) + abs(pixel[2] - bg[2])
                if dist > 50:
                    columns[xx] += 1
                    row_hit = True
            if row_hit:
                mask_rows += 1
        threshold = max(2, int(height_s * 0.10))
        significant = [xx for xx, count in enumerate(columns) if count >= threshold]
        if not significant:
            continue
        # 取最长连续段（避免把左右两侧零散噪点拼成整行）
        runs, run_start, prev = [], significant[0], significant[0]
        for xx in significant[1:]:
            if xx == prev + 1:
                prev = xx
                continue
            runs.append((run_start, prev))
            run_start = prev = xx
        runs.append((run_start, prev))
        x0_s, x1_s = max(runs, key=lambda r: r[1] - r[0])
        blob_w = (x1_s - x0_s + 1) / scale
        if blob_w < 60 or blob_w > width * 0.75:
            continue
        ys = [yy for yy in range(height_s)
              if any(columns[xx] and small.getpixel((xx, yy)) is not None
                     for xx in range(max(0, x0_s), min(width_s, x1_s + 1)))]
        blob_h = mask_rows * band_h / float(height_s)
        if blob_h < 50:
            continue
        center_x = ((x0_s + x1_s) / 2.0) / scale
        center_y = y0 + mask_rows * band_h / float(height_s) / 2.0
        role = "me" if center_x >= width / 2.0 else "you"
        text = "[表情包]" if (blob_w < width * 0.45 and blob_h < 420) else "[图片]"
        found.append({"role": role, "text": text, "cy": round(center_y, 1),
                      "kind": "media", "w": round(blob_w, 1), "h": round(blob_h, 1)})
    return found


def _process_one(path: Path, *, infer_role: bool, detect_media: bool = True) -> dict:
    result = {"file": str(path), "ok": False}
    try:
        img, width, height, scale = _load_image(path)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return result
    try:
        lines, engine, ms = _ocr(img)
    except Exception as exc:  # noqa: BLE001
        result.update({"width": width, "height": height, "scale": round(scale, 3),
                       "error": f"{type(exc).__name__}: {exc}"[:200]})
        return result
    rows = _sort_lines(lines)
    if not rows:
        result.update({"ok": False, "width": width, "height": height,
                       "scale": round(scale, 3), "engine": engine, "ms": ms,
                       "error": "ocr_empty"})
        return result
    if infer_role:
        _infer_roles(rows, width, height)
    # 把缩放坐标还原成原图坐标，方便上层对齐
    if scale != 1.0:
        for row in rows:
            row["box"] = [int(round(v / scale)) for v in row["box"]]
            row["cx"] = round(row["cx"] / scale, 1)
            row["cy"] = round(row["cy"] / scale, 1)
            row["h"] = max(1, int(round(row["h"] / scale)))
    bubbles = _merge_bubbles(rows)
    if detect_media:
        media = _detect_media_regions(img, rows, width, height)
        if media:
            for bubble in bubbles:
                bubble.setdefault("kind", "text")
            bubbles.extend(media)
            bubbles.sort(key=lambda b: float(b.get("cy") or 0))
    result.update({"ok": True, "width": width, "height": height,
                   "scale": round(scale, 3), "engine": engine, "ms": ms,
                   "lines": rows, "bubble_lines": bubbles})
    return result


def main(argv=None) -> int:
    # Windows 管道下 stdout 默认可能是 cp936，显式改成 UTF-8，避免父进程按 UTF-8 解码变乱码
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(description="聊天截图 OCR（支持多图 + 左右角色推断）")
    parser.add_argument("--image", action="append", default=[],
                        help="图片路径，可重复/多值")
    parser.add_argument("--infer-role", dest="infer_role", action="store_true",
                        default=True, help="按左右位置推断你/我（默认开）")
    parser.add_argument("--no-infer-role", dest="infer_role", action="store_false")
    parser.add_argument("--json", action="store_true", help="只输出 JSON（默认也输出 JSON）")
    parser.add_argument("--detect-media", dest="detect_media", action="store_true",
                        default=True, help="识别无文字的图片/贴纸气泡并插占位符（默认开）")
    parser.add_argument("--no-detect-media", dest="detect_media", action="store_false")
    args = parser.parse_args(argv)

    images: list[str] = []
    for item in args.image:
        images.extend(item.split(",") if "," in item else [item])
    images = [p for p in (str(x).strip() for x in images) if p]
    if not images:
        print(json.dumps({"ok": False, "error": "no_image"}, ensure_ascii=False))
        return 2

    results = [_process_one(Path(path), infer_role=bool(args.infer_role),
                            detect_media=bool(args.detect_media))
               for path in images]
    ok = any(r.get("ok") for r in results)
    payload = {"ok": ok, "results": results}
    if not ok and results:
        payload["error"] = results[0].get("error") or "ocr_failed"
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
