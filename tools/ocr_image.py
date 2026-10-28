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
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
MAX_WIDTH = 1600
#: 超长截图（聊天长图常见 700×14000+）分块 OCR；块高/重叠
TILE_MAX_H = 1800
TILE_OVERLAP = 80
TILE_MIN_H = 400


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


_WORKER = None


def _new_worker():
    from wx_core.ocr import OCRWorker
    return OCRWorker()


def _init_tile_worker():
    """ProcessPoolExecutor initializer：每个子进程只加载一次 OCR 引擎。"""
    global _WORKER
    try:
        _WORKER = _new_worker()
        _WORKER.__enter__()
    except Exception:  # noqa: BLE001
        _WORKER = None


def _ocr_with(img, worker=None):
    """用给定/临时 worker 做一次 OCR，返回 (lines, engine, ms)。"""
    started = time.time()
    own = worker is None
    if own:
        worker = _new_worker()
        worker.__enter__()
    try:
        lines = worker.ocr(img) or []
    finally:
        if own:
            try:
                worker.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
    engine = str(getattr(worker, "last_engine", "") or "")
    if not engine:
        engine = "rapidocr" if "rapidocr" in repr(worker).lower() else "windows"
    return lines, engine, round((time.time() - started) * 1000.0, 1)


_RAPID = None


def _rapid_engine():
    """进程内 RapidOCR 单例（比 OCRWorker 少一层多尺度/降级开销，实测快 2 倍+）。"""
    global _RAPID
    if _RAPID is None:
        from rapidocr_onnxruntime import RapidOCR
        import os as _os
        try:
            threads = int(_os.environ.get("IDEA_OCR_ORT_THREADS") or 2)
        except (TypeError, ValueError):
            threads = 2
        try:
            _RAPID = RapidOCR(intra_op_num_threads=max(1, min(8, threads)))
        except Exception:  # noqa: BLE001 - 老版本不支持线程参数
            _RAPID = RapidOCR()
    return _RAPID


def _ocr_direct(img):
    """直接用 RapidOCR（失败返回 None），返回 (lines, engine, ms)。"""
    try:
        import numpy as np
        engine = _rapid_engine()
        started = time.time()
        result, _elapse = engine(np.array(img))
        elapsed = round((time.time() - started) * 1000.0, 1)
    except Exception:  # noqa: BLE001 - 没装/推理失败 → 交给上层降级
        return None
    out = []
    for item in result or []:
        try:
            box, text = item[0], item[1]
            xs = [float(pt[0]) for pt in box]
            ys = [float(pt[1]) for pt in box]
            out.append((str(text), (int(min(xs)), int(min(ys)),
                                    int(max(xs)), int(max(ys)))))
        except Exception:  # noqa: BLE001
            continue
    return out, "rapidocr", elapsed


def _ocr(img):
    """返回 (lines, engine, ms)；优先 RapidOCR 直连，失败回退 OCRWorker。"""
    direct = _ocr_direct(img)
    if direct is not None:
        raw, engine, ms = direct
        from wx_core.models import OcrLine
        return [OcrLine(text=text, box=box) for text, box in raw], engine, ms
    return _ocr_with(img, None)


def _tile_ranges(height: int) -> list[tuple[int, int]]:
    """把超高图片切成有重叠的竖条；返回 [(y0, y1)]。"""
    if height <= TILE_MAX_H * 1.3:
        return [(0, height)]
    ranges = []
    y = 0
    while y < height:
        y1 = min(height, y + TILE_MAX_H)
        if y1 - y < TILE_MIN_H and ranges:
            ranges[-1] = (ranges[-1][0], height)
            break
        ranges.append((y, y1))
        if y1 >= height:
            break
        y = y1 - TILE_OVERLAP
    return ranges


def _tile_worker(args):
    """子进程/线程：对一块图做 OCR（复用进程内 worker），返回可序列化结果。"""
    data, (w, h) = args
    from PIL import Image
    img = Image.frombytes("RGB", (w, h), data)
    worker = globals().get("_WORKER")
    lines, engine, ms = _ocr_with(img, worker)
    return [{"text": line.text, "box": [int(v) for v in line.box]} for line in lines], engine, ms


def _ocr_tiles(img, workers: int = 3):
    """整图或分块 OCR；返回 (rows, engine, ms)。分块时按 y 偏移还原坐标并去掉重叠重复。"""
    width, height = img.size
    ranges = _tile_ranges(height)
    if len(ranges) <= 1:
        lines, engine, ms = _ocr(img)
        return lines, engine, ms
    crops = []
    for y0, y1 in ranges:
        crop = img.crop((0, y0, width, y1))
        crops.append((crop.tobytes(), crop.size, y0))
    if workers <= 1:
        raw_results = []
        try:
            worker = _new_worker()
            worker.__enter__()
        except Exception:  # noqa: BLE001
            worker = None
        try:
            for data, size, y0 in crops:
                lines, engine, ms = _ocr_with(
                    __import__("PIL.Image", fromlist=["Image"]).frombytes("RGB", size, data),
                    worker)
                raw_results.append((([{"text": ln.text, "box": [int(v) for v in ln.box]}
                                      for ln in lines], engine, ms), y0))
        finally:
            if worker is not None:
                try:
                    worker.__exit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass
    else:
        raw_results = []
        with ProcessPoolExecutor(max_workers=max(1, min(workers, len(crops))),
                                 initializer=_init_tile_worker) as pool:
            futures = [(pool.submit(_tile_worker, (data, size)), y0)
                       for data, size, y0 in crops]
            for future, y0 in futures:
                try:
                    raw_results.append((future.result(), y0))
                except Exception:  # noqa: BLE001 - 单块失败跳过
                    raw_results.append((([], "", 0.0), y0))
    from wx_core.models import OcrLine
    rows = []
    engine = ""
    ms_total = 0.0
    for (lines, eng, ms), y0 in raw_results:
        engine = engine or eng
        ms_total += float(ms or 0)
        for line in lines:
            box = list(line.get("box") or [0, 0, 0, 0])
            box[1] += y0
            box[3] += y0
            rows.append(OcrLine(text=str(line.get("text") or ""), box=tuple(box)))
    rows.sort(key=lambda r: (r.box[1], r.box[0]))
    dedup = []
    for row in rows:
        if dedup:
            prev = dedup[-1]
            if (row.text == prev.text
                    and abs((row.box[1] + row.box[3]) / 2 - (prev.box[1] + prev.box[3]) / 2)
                    <= max(8, (prev.box[3] - prev.box[1]) * 0.8)):
                dedup[-1] = row if (row.box[3] - row.box[1]) > (prev.box[3] - prev.box[1]) else prev
                continue
        dedup.append(row)
    return dedup, engine, ms_total


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
        # 默认整图直连 RapidOCR；分块只在显式 IDEA_OCR_TILE_WORKERS>1 时启用（实测无收益）
        try:
            tile_workers = int(os.environ.get("IDEA_OCR_TILE_WORKERS") or 0)
        except (TypeError, ValueError):
            tile_workers = 0
        if tile_workers > 1:
            lines, engine, ms = _ocr_tiles(img, min(4, tile_workers))
        else:
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
    parser.add_argument("--tile-workers", type=int, default=0,
                        help="超长截图分块 OCR 的并发进程数（默认 env IDEA_OCR_TILE_WORKERS 或 3）")
    args = parser.parse_args(argv)

    images: list[str] = []
    for item in args.image:
        images.extend(item.split(",") if "," in item else [item])
    images = [p for p in (str(x).strip() for x in images) if p]
    if getattr(args, "tile_workers", 0):
        os.environ["IDEA_OCR_TILE_WORKERS"] = str(int(args.tile_workers))
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
