"""``wx_core/ocr.py`` 的 pytest 测试（完全离线，用 tests/fixtures/ocr 的真实截图）。

运行::

    python -m pytest tests/test_ocr.py -q

* 主引擎测试需要 ``rapidocr_onnxruntime``（未安装时自动 skip）；
* 降级链路测试需要 PowerShell 5.1 + Windows OCR（不可用时自动 skip）；
* 两种引擎都不可用时，依赖 OCR 的用例整体 skip。
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wx_core import ocr as ocr_mod  # noqa: E402
from wx_core.ocr import OCRWorker, powershell_available, rapidocr_available  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ocr"
REGION_FILE = FIXTURES / "message_region.png"  # recon/live_restored.png 消息区 (384,101,1108,619)
FULL_FILE = FIXTURES / "live_restored.png"     # 整帧 1118x809

REGION_KEYS = [
    "你去看看",
    "m_参数和_pt_参数的构造还在做，稍等",
    "何工，参数这边客户没问题，剩下你cp过来就行",
]
FULL_KEYS = [
    "白云一片去悠悠",
    "文件传输助手",
    "微信团队",
    "腾讯新闻",
    "你去看看",
    "参数这边客户没问题",
    "发送",
]

HAS_RAPID = rapidocr_available()
HAS_WINDOWS = powershell_available()

requires_rapid = pytest.mark.skipif(
    not HAS_RAPID, reason="rapidocr_onnxruntime 未安装（离线环境），跳过主引擎用例"
)
requires_windows = pytest.mark.skipif(
    not HAS_WINDOWS, reason="powershell.exe 不可用，跳过 Windows OCR 降级用例"
)
requires_any = pytest.mark.skipif(
    not (HAS_RAPID or HAS_WINDOWS), reason="RapidOCR 与 Windows OCR 都不可用"
)


@pytest.fixture()
def worker():
    """每个用例独立的 OCRWorker（用完释放；RapidOCR 会话引用计数归零即释放）。"""
    instance = OCRWorker()
    try:
        yield instance
    finally:
        instance.close()


def _joined(lines) -> str:
    return "\n".join(line.text for line in lines)


# --------------------------------------------------------------------------- 主引擎
@requires_rapid
def test_region_keywords_and_same_line_merge(worker):
    """真实消息区能识别出关键文本，且同一行的多段被合并成一条 OcrLine。"""
    lines = worker.ocr(REGION_FILE)
    assert lines, f"识别失败: {worker.last_error}"
    assert worker.backend == "rapidocr"
    assert worker.last_error is None
    assert worker.last_ms > 0

    joined = _joined(lines)
    missing = [key for key in REGION_KEYS if key not in joined]
    assert not missing, f"缺少关键词 {missing}；实际文本:\n{joined}"

    merged = [line for line in lines if line.text == REGION_KEYS[2]]
    assert merged, "长消息行没有被合并成一条"
    assert len(merged[0].words) >= 2, "合并行的 words 应该保留原始分段"
    # 合并后的 box 应覆盖所有分段的并集
    box = merged[0].box
    for word in merged[0].words:
        wl, wt, wr, wb = word["box"]
        assert box[0] <= wl and box[1] <= wt and wr <= box[2] and wb <= box[3], (box, word)


@requires_rapid
def test_boxes_are_image_pixel_coordinates(worker):
    """box 是图像像素坐标且落在图像范围内（PIL 输入）。"""
    with Image.open(REGION_FILE) as image:
        width, height = image.size
        lines = worker.ocr(image)
    assert lines, f"识别失败: {worker.last_error}"
    for line in lines:
        left, top, right, bottom = line.box
        assert all(isinstance(value, int) for value in line.box), line.box
        assert 0 <= left <= right <= width, (line.text, line.box, (width, height))
        assert 0 <= top <= bottom <= height, (line.text, line.box, (width, height))
        for word in line.words:
            wl, wt, wr, wb = word["box"]
            assert 0 <= wl <= wr <= width and 0 <= wt <= wb <= height, (line.text, word)


@requires_rapid
def test_pil_and_path_inputs_agree(worker):
    """PIL.Image 输入与路径输入结果一致。"""
    path_lines = worker.ocr(REGION_FILE)
    with Image.open(REGION_FILE) as image:
        pil_lines = worker.ocr(image)
    assert path_lines and pil_lines
    assert [line.text for line in path_lines] == [line.text for line in pil_lines]


@requires_rapid
def test_full_frame_keywords(worker):
    """整帧（1118x809）截图能识别标题、会话列表与消息区关键词。"""
    lines = worker.ocr(FULL_FILE)
    assert lines, f"识别失败: {worker.last_error}"
    joined = _joined(lines)
    missing = [key for key in FULL_KEYS if key not in joined]
    assert not missing, f"整帧缺少关键词 {missing}；实际文本:\n{joined}"


@requires_rapid
def test_thread_safe(worker):
    """多线程并发调用不崩、结果正确（内部串行化）。"""
    results: list[list] = []
    errors: list[BaseException] = []

    def run():
        try:
            results.append(worker.ocr(REGION_FILE))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert not errors, errors
    assert len(results) == 3
    for lines in results:
        assert "你去看看" in _joined(lines)


@requires_rapid
def test_blank_image_returns_empty_list(worker):
    """纯白图没有文字 → 返回 []，不抛异常。"""
    blank = Image.new("RGB", (160, 80), (255, 255, 255))
    assert worker.ocr(blank) == []
    assert worker.last_error is None


@requires_rapid
def test_close_releases_rapidocr_session():
    """close() 可重复调用；最后一个 worker 关闭后丢弃单例、释放 onnxruntime 会话。"""
    instance = OCRWorker()
    lines = instance.ocr(REGION_FILE)
    assert lines
    assert ocr_mod._rapid_refs >= 1
    instance.close()
    instance.close()

    assert ocr_mod._rapid_refs == 0, "rapid 引用计数没有归零"
    assert ocr_mod._rapid_state["engine"] is None, "RapidOCR 单例未释放（会话泄漏）"

    # 关闭后继续调用：返回 [] 并给出明确原因，不崩
    assert instance.ocr(REGION_FILE) == []
    assert instance.last_error == "OCRWorker 已关闭"


# --------------------------------------------------------------------------- 参数与降级
@requires_any
def test_invalid_arguments_raise():
    """参数错误按约定抛异常；不存在的路径抛 FileNotFoundError。"""
    instance = OCRWorker()
    try:
        with pytest.raises(TypeError):
            instance.ocr(123)
        with pytest.raises(FileNotFoundError):
            instance.ocr("definitely_not_here.png")
    finally:
        instance.close()


@requires_windows
def test_windows_ocr_fallback():
    """强制 Windows OCR 引擎也能用（rapidocr 不可用时的降级链路）。"""
    instance = OCRWorker(engine="windows")
    try:
        assert instance.engine_name == "windows"
        lines = instance.ocr(REGION_FILE)
        assert lines, f"Windows OCR 失败: {instance.last_error}"
        joined = _joined(lines)
        # 降级链路只保证"能用"：这句短文本 Windows OCR 能认对
        assert "你去看看" in joined
        assert instance.backend.startswith("windows")
    finally:
        instance.close()


@requires_any
def test_engine_name_and_warmup():
    """engine_name 是明确的引擎标识；warmup() 提前加载成功。"""
    instance = OCRWorker()
    try:
        assert instance.engine_name in ("rapidocr", "windows")
        assert instance.warmup() is True
    finally:
        instance.close()
