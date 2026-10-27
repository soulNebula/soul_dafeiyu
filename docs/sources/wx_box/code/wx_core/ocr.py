"""OCR 统一封装：RapidOCR 主引擎 + Windows 自带 OCR 降级。

主引擎
------
``rapidocr_onnxruntime.RapidOCR``（ONNX，模型随包，无需下载、无网络）。
进程内懒加载单例 + ``threading.Lock``，首次使用做一次 warmup；之后每次调用复用
同一个引擎（不重建）。识别结果的 4 点多边形取 min/max 转成 ``(left, top, right, bottom)``；
同一行的多段（RapidOCR 常把一行中文拆成几段）按 y 中心分组、按 x 排序直接拼接（中文
内部不留空格），box 取并集。

降级
----
* 导入 ``rapidocr_onnxruntime`` 失败（或推理异常）→ 回退到 Windows 自带的
  ``Windows.Media.Ocr``（PowerShell 5.1 + WinRT，常驻 worker，JSON-lines 协议）；
* Windows 侧再失败 → 返回 ``[]`` 并记录 :attr:`OCRWorker.last_error`。

对外接口::

    from wx_core.ocr import OCRWorker

    with OCRWorker() as worker:
        lines = worker.ocr(pil_image)          # 或图片路径
        # lines: list[wx_core.models.OcrLine]，box 是图像像素坐标
    print(worker.last_ms)                       # 本次调用耗时（毫秒）

参数类型/路径错误抛 ``TypeError`` / ``FileNotFoundError``；识别失败返回 ``[]``（不抛异常）。
线程安全（内部互斥锁），可在监控线程里反复调用。

红线：本模块只读取图片文件，不接触微信进程内存，不注入、不 Hook。
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image, ImageOps

from .models import OcrLine

__all__ = [
    "OCRWorker",
    "OcrUnavailableError",
    "rapidocr_available",
    "powershell_available",
    "LANGUAGE",
]

LANGUAGE = "zh-Hans-CN"

# ---------------------------------------------------------------------------
# 引擎选择
# ---------------------------------------------------------------------------
ENGINE_AUTO = "auto"
ENGINE_RAPID = "rapidocr"
ENGINE_WINDOWS = "windows"
_ENGINES = (ENGINE_AUTO, ENGINE_RAPID, ENGINE_WINDOWS)

#: 同段合并：两段水平间隙超过「行高 × 该系数」就不再拼成一行（防止把左栏名字和右栏时间粘一起）
DEFAULT_MERGE_GAP_FACTOR = 1.2
#: 同一行判定的 y 中心容差（行高的倍数）
LINE_Y_TOLERANCE = 0.6

#: 已在本工程验证可用的 PowerShell 5.1（WinRT 需要 .NET Framework 的 Windows Runtime 投影）
PS_EXE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
DEFAULT_TIMEOUT = 15.0
DEFAULT_WINDOWS_SCALE = 2.0
DEFAULT_MAX_SIDE = 2600
#: 连续失败这么多次后彻底放弃常驻 worker，改用「每次调用新进程」
MAX_RESIDENT_FAILURES = 2
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

# ---------------------------------------------------------------------------
# RapidOCR（主引擎）
# ---------------------------------------------------------------------------
_rapid_lock = threading.Lock()
_rapid_state: dict = {
    "engine": None,
    "error": None,
    "warmed": False,
    "init_ms": 0.0,
    "warmup_ms": 0.0,
}
_rapid_import_ok: bool | None = None
#: 当前持有 RapidOCR 单例的 OCRWorker 数量；归零时释放 onnxruntime 会话。
_rapid_refs: int = 0


class OcrUnavailableError(RuntimeError):
    """当前机器上两种 OCR 引擎都不可用（或强制指定的引擎不可用）。"""


def rapidocr_available() -> bool:
    """rapidocr_onnxruntime 是否已安装可导入（结果缓存）。"""
    global _rapid_import_ok
    if _rapid_import_ok is None:
        try:
            import rapidocr_onnxruntime  # noqa: F401
        except Exception:  # noqa: BLE001 - 导入失败即视为不可用
            _rapid_import_ok = False
        else:
            _rapid_import_ok = True
    return _rapid_import_ok


#: RapidOCR 默认参数：截图永远是正的，关掉方向分类省时间；rec 批大小调大提速。
#: 注意引擎是进程内单例，**第一次初始化时的参数生效**（之后复用）。
_RAPID_DEFAULTS: dict = {"use_angle_cls": False}
_RAPID_REC_BATCH_NUM = 20


def _rapid_engine_locked(rapid_kwargs: dict | None = None, rec_batch_num: int | None = None):
    """懒加载 RapidOCR 单例并做一次 warmup。调用方必须持有 ``_rapid_lock``。"""
    state = _rapid_state
    if state["engine"] is None:
        if state["error"]:
            raise OcrUnavailableError(state["error"])
        try:
            from rapidocr_onnxruntime import RapidOCR
        except Exception as exc:  # noqa: BLE001
            state["error"] = f"rapidocr_onnxruntime 导入失败: {exc}"
            raise OcrUnavailableError(state["error"]) from exc
        options = dict(_RAPID_DEFAULTS)
        options.update(rapid_kwargs or {})
        started = time.perf_counter()
        try:
            engine = RapidOCR(**options)
        except Exception as exc:  # noqa: BLE001
            state["error"] = f"RapidOCR 初始化失败: {exc}"
            raise OcrUnavailableError(state["error"]) from exc
        # rec_batch_num 走构造参数时 rapidocr 的 kwargs 更新器有 bug（KeyError: model_path），
        # 所以初始化后直接改识别器属性；1 行文本时也不影响正确性。
        batch = _RAPID_REC_BATCH_NUM if rec_batch_num is None else int(rec_batch_num)
        try:
            engine.text_recognizer.rec_batch_num = max(1, batch)
        except Exception:  # noqa: BLE001 - 老版本没有该属性也不影响
            pass
        state["engine"] = engine
        state["init_ms"] = (time.perf_counter() - started) * 1000.0
    if not state["warmed"]:
        try:
            import numpy as np

            started = time.perf_counter()
            state["engine"](np.zeros((32, 128, 3), dtype=np.uint8))
            state["warmup_ms"] = (time.perf_counter() - started) * 1000.0
        except Exception:  # noqa: BLE001 - warmup 失败不致命
            state["warmup_ms"] = 0.0
        finally:
            state["warmed"] = True
    return state["engine"]


def _rapid_acquire(rapid_kwargs: dict | None = None, rec_batch_num: int | None = None):
    """取引擎持有引用（供 :meth:`OCRWorker.close` 归零时释放会话）。"""
    global _rapid_refs
    with _rapid_lock:
        engine = _rapid_engine_locked(rapid_kwargs, rec_batch_num)
        _rapid_refs += 1
        return engine


def _rapid_release() -> None:
    """释放引擎持有引用；最后一个持有者关闭时丢弃单例（onnxruntime 会话随 GC 释放）。"""
    global _rapid_refs
    with _rapid_lock:
        if _rapid_refs <= 0:
            return
        _rapid_refs -= 1
        if _rapid_refs > 0 or _rapid_state["engine"] is None:
            return
        _rapid_state["engine"] = None
        _rapid_state["warmed"] = False
        import gc

        gc.collect()


def _rapid_run_batch(arr, rapid_kwargs: dict | None = None, rec_batch_num: int | None = None) -> tuple[list, float]:
    """线程安全地跑一次 RapidOCR。返回 (result, 推理毫秒)。"""
    with _rapid_lock:
        engine = _rapid_engine_locked(rapid_kwargs, rec_batch_num)
        started = time.perf_counter()
        result, _elapse = engine(arr)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
    return list(result or []), elapsed_ms


def _raw_to_segments(result) -> list[dict]:
    """把 RapidOCR 结果转成内部段结构（图像像素坐标，已四舍五入为 int）。"""
    segments: list[dict] = []
    for item in result or []:
        try:
            box, text, score = item[0], item[1], item[2]
        except (TypeError, IndexError, ValueError):
            continue
        text = str(text or "").strip()
        if not text or box is None:
            continue
        try:
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
        except (TypeError, IndexError, ValueError):
            continue
        if not xs or not ys:
            continue
        left, top = int(round(min(xs))), int(round(min(ys)))
        right, bottom = int(round(max(xs))), int(round(max(ys)))
        try:
            score_value = float(score)
        except (TypeError, ValueError):
            score_value = 0.0
        segments.append({"text": text, "box": (left, top, right, bottom), "score": score_value})
    return segments


def _segment_cy(seg: dict) -> float:
    left, top, right, bottom = seg["box"]
    return (top + bottom) / 2.0


def _segments_to_lines(result, gap_factor: float = DEFAULT_MERGE_GAP_FACTOR) -> list[OcrLine]:
    """同一行的多段合并成 OcrLine：按 y 中心分组 → 按 x 排序 → 直接拼接、box 取并集。"""
    segments = _raw_to_segments(result)
    if not segments:
        return []

    segments.sort(key=lambda s: (_segment_cy(s), s["box"][0]))
    groups: list[dict] = []
    for seg in segments:
        height = max(1, seg["box"][3] - seg["box"][1])
        cy = _segment_cy(seg)
        target = None
        for group in groups:
            tolerance = LINE_Y_TOLERANCE * max(height, group["height"])
            if abs(cy - group["cy"]) <= tolerance:
                if target is None or abs(cy - group["cy"]) < abs(cy - target["cy"]):
                    target = group
        if target is None:
            groups.append({"items": [seg], "cy": cy, "height": height})
        else:
            count = len(target["items"])
            target["items"].append(seg)
            target["cy"] = (target["cy"] * count + cy) / (count + 1)
            target["height"] = max(target["height"], height)

    lines: list[OcrLine] = []
    for group in groups:
        items = sorted(group["items"], key=lambda s: s["box"][0])
        clusters: list[list[dict]] = [[items[0]]]
        for item in items[1:]:
            previous = clusters[-1][-1]
            gap = item["box"][0] - previous["box"][2]
            line_height = max(
                item["box"][3] - item["box"][1],
                previous["box"][3] - previous["box"][1],
                1,
            )
            if gap > gap_factor * line_height:
                clusters.append([item])  # 水平方向隔得太远，视为两段独立文本（如左栏名字 + 右栏时间）
            else:
                clusters[-1].append(item)
        for cluster in clusters:
            text = _clean_text("".join(part["text"] for part in cluster))
            if not text:
                continue
            left = min(part["box"][0] for part in cluster)
            top = min(part["box"][1] for part in cluster)
            right = max(part["box"][2] for part in cluster)
            bottom = max(part["box"][3] for part in cluster)
            words = [
                {"text": part["text"], "box": part["box"], "score": part["score"]}
                for part in cluster
            ]
            lines.append(OcrLine(text=text, box=(left, top, right, bottom), words=words))

    lines.sort(key=lambda line: (line.box[1], line.box[0]))
    return lines


# ---------------------------------------------------------------------------
# Windows OCR（降级引擎：PowerShell 5.1 + WinRT，常驻 worker）
# ---------------------------------------------------------------------------
#: 保持纯 ASCII（PS 5.1 读无 BOM 脚本时按 ANSI 解码）
_PS_SCRIPT = r"""
param([string]$ImagePath = '')

$ErrorActionPreference = 'Stop'
$utf8 = New-Object System.Text.UTF8Encoding $false
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
[Console]::InputEncoding = $utf8

function Write-JsonLine($obj) {
    Write-Output (ConvertTo-Json -InputObject $obj -Compress -Depth 8)
    [Console]::Out.Flush()
}

function Stop-WithError([string]$msg, [int]$code) {
    Write-JsonLine ([pscustomobject]@{ ok = $false; error = $msg })
    exit $code
}

try {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $script:asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
            $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
            $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
        })[0]
    [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
    [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
    [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
    [Windows.Globalization.Language, Windows.Globalization, ContentType = WindowsRuntime] | Out-Null
}
catch {
    Stop-WithError ("init failed: " + $_.Exception.Message) 2
}

function Await($op, [Type]$type) {
    $m = $script:asTask.MakeGenericMethod($type)
    $task = $m.Invoke($null, @($op))
    $task.Wait(-1) | Out-Null
    return $task.Result
}

$engine = $null
try {
    $lang = New-Object Windows.Globalization.Language 'zh-Hans-CN'
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
}
catch { }
if (-not $engine) {
    try { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() } catch { }
}
if (-not $engine) {
    Stop-WithError 'no ocr engine available (zh-Hans-CN untrusted/unavailable)' 3
}

function Recognize([string]$path) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    try {
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        try {
            $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
            $lines = @()
            foreach ($line in $result.Lines) {
                $words = @()
                $l = [double]::MaxValue; $t = [double]::MaxValue
                $r = [double]::MinValue; $b = [double]::MinValue
                foreach ($w in $line.Words) {
                    $rect = $w.BoundingRect
                    $wl = [int][math]::Round($rect.X)
                    $wt = [int][math]::Round($rect.Y)
                    $wr = [int][math]::Round($rect.X + $rect.Width)
                    $wb = [int][math]::Round($rect.Y + $rect.Height)
                    $words += [pscustomobject]@{ text = $w.Text; box = @($wl, $wt, $wr, $wb) }
                    if ($rect.X -lt $l) { $l = $rect.X }
                    if ($rect.Y -lt $t) { $t = $rect.Y }
                    if (($rect.X + $rect.Width) -gt $r) { $r = $rect.X + $rect.Width }
                    if (($rect.Y + $rect.Height) -gt $b) { $b = $rect.Y + $rect.Height }
                }
                if ($l -eq [double]::MaxValue) { $l = 0; $t = 0; $r = 0; $b = 0 }
                $lines += [pscustomobject]@{
                    text  = $line.Text
                    box   = @([int][math]::Round($l), [int][math]::Round($t), [int][math]::Round($r), [int][math]::Round($b))
                    words = $words
                }
            }
            $sw.Stop()
            return [pscustomobject]@{
                ok     = $true
                ms     = [int]$sw.ElapsedMilliseconds
                width  = [int]$bitmap.PixelWidth
                height = [int]$bitmap.PixelHeight
                lines  = @($lines)
            }
        }
        finally { $bitmap.Dispose() }
    }
    finally { $stream.Dispose() }
}

# ---- one-shot mode: image path as argument, print one JSON line, exit ----
if ($ImagePath -ne '') {
    try { $resp = Recognize $ImagePath }
    catch { $resp = [pscustomobject]@{ ok = $false; error = $_.Exception.Message } }
    Write-JsonLine $resp
    if (-not $resp.ok) { exit 1 }
    exit 0
}

# ---- resident mode: one image path per stdin line ----
$maxDim = 0
try { $maxDim = [int]$engine.MaxImageDimension } catch { }
Write-JsonLine ([pscustomobject]@{ ok = $true; ready = $true; lang = 'zh-Hans-CN'; max_dim = $maxDim })

while ($true) {
    $line = [Console]::In.ReadLine()
    if ($null -eq $line) { break }
    $line = $line.Trim()
    if ($line -eq '') { continue }
    if ($line -eq '__QUIT__') { break }
    try { $resp = Recognize $line }
    catch { $resp = [pscustomobject]@{ ok = $false; error = $_.Exception.Message } }
    Write-JsonLine $resp
}
exit 0
"""

_SCRIPT_LOCK = threading.Lock()
_SCRIPT_PATH: str | None = None


def _cleanup_script(path: str | None = None) -> None:
    target = Path(path) if path else (Path(_SCRIPT_PATH) if _SCRIPT_PATH else None)
    if target is None:
        return
    try:
        target.unlink(missing_ok=True)
    except OSError:
        pass


def _ensure_script() -> str:
    """把 PowerShell 脚本落到临时文件（进程内共享，退出时删除）。"""
    global _SCRIPT_PATH
    with _SCRIPT_LOCK:
        if _SCRIPT_PATH and Path(_SCRIPT_PATH).is_file():
            return _SCRIPT_PATH
        digest = hashlib.sha1(_PS_SCRIPT.encode("utf-8")).hexdigest()[:12]
        folder = Path(tempfile.gettempdir()) / "wx_box_ocr"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"ocr_worker_{digest}.ps1"
        path.write_text(_PS_SCRIPT, encoding="ascii")
        _SCRIPT_PATH = str(path)
        atexit.register(_cleanup_script, str(path))
        return _SCRIPT_PATH


def _find_powershell() -> str | None:
    override = os.environ.get("WX_OCR_POWERSHELL")
    if override and Path(override).is_file():
        return override
    if Path(PS_EXE).is_file():
        return PS_EXE
    return shutil.which("powershell.exe") or shutil.which("powershell")


def powershell_available() -> bool:
    """测试用：PowerShell 可执行文件是否存在（不代表 OCR 引擎一定可用）。"""
    return _find_powershell() is not None


class _WorkerFailure(RuntimeError):
    """Windows 常驻 worker 的单次失败（崩溃 / 超时 / 乱码 / 引擎报错）。"""


def _looks_garbled(text: str) -> bool:
    if not text:
        return False
    if "\ufffd" in text:
        return True
    return any(seq in text for seq in ("锟斤拷", "烫烫烫", "Ã¤", "Ã¥", "â€", "ï¿½"))


class _ResidentWorker:
    """一个常驻的 powershell.exe 进程 + JSON-lines 收发。"""

    def __init__(self, timeout: float) -> None:
        self._timeout = float(timeout)
        self._stdout_q: "queue.Queue[bytes | None]" = queue.Queue()
        self._stderr_tail: list[str] = []
        self._stderr_lock = threading.Lock()
        self.proc: subprocess.Popen | None = None

    # -- 生命周期 --
    def start(self, ps_exe: str, script: str, cwd: str | None) -> dict:
        args = [
            ps_exe,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            script,
        ]
        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            bufsize=0,
            creationflags=_CREATE_NO_WINDOW,
        )
        threading.Thread(target=self._pump_stdout, name="wxocr-stdout", daemon=True).start()
        threading.Thread(target=self._pump_stderr, name="wxocr-stderr", daemon=True).start()
        return self._wait_ready()

    def stop(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is None or proc.poll() is not None:
            return
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=1.0)
            return
        except subprocess.TimeoutExpired:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=1.0)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            pass

    # -- 后台读线程 --
    def _pump_stdout(self) -> None:
        stream = self.proc.stdout if self.proc else None
        try:
            while True:
                raw = stream.readline() if stream is not None else b""
                if not raw:
                    break
                self._stdout_q.put(raw)
        except Exception:  # noqa: BLE001 - 管道关闭属正常
            pass
        finally:
            self._stdout_q.put(None)

    def _pump_stderr(self) -> None:
        stream = self.proc.stderr if self.proc else None
        try:
            while True:
                raw = stream.readline() if stream is not None else b""
                if not raw:
                    break
                text = raw.decode("utf-8", "replace").rstrip()
                if text:
                    with self._stderr_lock:
                        self._stderr_tail.append(text)
                        del self._stderr_tail[:-20]
        except Exception:  # noqa: BLE001
            pass

    # -- 协议 --
    def _parse(self, raw: bytes) -> dict:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _WorkerFailure(f"worker 输出不是合法 UTF-8（CJK 乱码）: {exc}") from exc
        text = text.lstrip("\ufeff").strip()
        if not text:
            raise _WorkerFailure("worker 输出空行")
        if _looks_garbled(text):
            raise _WorkerFailure("worker 输出疑似 CJK 乱码（编码不一致）")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _WorkerFailure(f"worker 输出不是 JSON: {text[:200]!r}") from exc
        if not isinstance(data, dict):
            raise _WorkerFailure(f"worker 输出 JSON 不是对象: {text[:200]!r}")
        if data.get("error") and not data.get("ok"):
            raise _WorkerFailure("worker 报错: " + str(data.get("error")))
        return data

    def _wait_ready(self, timeout: float | None = None) -> dict:
        deadline = time.monotonic() + (timeout or self._timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _WorkerFailure("worker 启动超时（未收到 ready）")
            if self.proc is not None and self.proc.poll() is not None and self._stdout_q.empty():
                raise _WorkerFailure(self._exit_reason("worker 启动即退出"))
            try:
                raw = self._stdout_q.get(timeout=remaining)
            except queue.Empty:
                raise _WorkerFailure("worker 启动超时（未收到 ready）") from None
            if raw is None:
                raise _WorkerFailure(self._exit_reason("worker 已退出"))
            try:
                data = self._parse(raw)
            except _WorkerFailure:
                continue  # 启动期可能混入噪声行
            if data.get("ready"):
                return data

    def request(self, path: str, timeout: float | None = None) -> dict:
        timeout = timeout or self._timeout
        if self.proc is None or self.proc.poll() is not None:
            raise _WorkerFailure(self._exit_reason("worker 已退出"))
        stdin = self.proc.stdin
        if stdin is None:
            raise _WorkerFailure("worker stdin 不可用")
        try:
            stdin.write((path + "\n").encode("utf-8"))
            stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise _WorkerFailure(self._exit_reason(f"写入 worker stdin 失败: {exc}")) from exc
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _WorkerFailure(f"OCR 超时（>{timeout:.0f}s）")
            try:
                raw = self._stdout_q.get(timeout=remaining)
            except queue.Empty:
                raise _WorkerFailure(f"OCR 超时（>{timeout:.0f}s）") from None
            if raw is None:
                raise _WorkerFailure(self._exit_reason("worker 已退出"))
            try:
                data = self._parse(raw)
            except _WorkerFailure:
                if self.proc.poll() is not None and self._stdout_q.empty():
                    raise _WorkerFailure(self._exit_reason("worker 已退出")) from None
                continue
            return data

    def _exit_reason(self, prefix: str) -> str:
        code = None
        if self.proc is not None:
            try:
                code = self.proc.poll()
            except Exception:  # noqa: BLE001
                code = None
        with self._stderr_lock:
            tail = " | ".join(self._stderr_tail[-3:])
        reason = f"{prefix} (exit={code})"
        return reason + (f": {tail}" if tail else "")


class _WindowsEngine:
    """Windows 自带 OCR（``Windows.Media.Ocr``）客户端：预处理 + 常驻 worker + 降级。"""

    def __init__(
        self,
        *,
        scale: float = DEFAULT_WINDOWS_SCALE,
        preprocess: str = "gray",
        bin_threshold: int = 60,
        timeout: float = DEFAULT_TIMEOUT,
        max_side: int = DEFAULT_MAX_SIDE,
        powershell: str | None = None,
    ) -> None:
        if scale <= 0:
            raise ValueError("scale 必须 > 0")
        if preprocess not in ("gray", "bin", "none"):
            raise ValueError("preprocess 只能是 'gray' / 'bin' / 'none'")
        self.scale = float(scale)
        self.preprocess = preprocess
        self.bin_threshold = int(bin_threshold)
        self.timeout = float(timeout)
        self.max_side = int(max_side)
        self._ps_exe = powershell or _find_powershell()

        self._lock = threading.RLock()
        self._resident: _ResidentWorker | None = None
        self._resident_disabled = False
        self._resident_failures = 0
        self._closed = False
        self._tmpdir: Path | None = None
        self._seq = 0

        self.last_error: str | None = None
        self.last_backend = ""
        self.last_engine_ms = 0
        self.max_dim: int | None = None

    # -- 公共 --
    def recognize(self, image_or_path) -> list[OcrLine]:
        """识别一张图（PIL 或路径），返回 OcrLine 列表；失败抛 ``_WorkerFailure``。"""
        with self._lock:
            if self._closed:
                raise _WorkerFailure("Windows OCR 引擎已关闭")
            path, effective_scale, temp = self._prepare(image_or_path)
            try:
                data = self._recognize(path)
            finally:
                if temp:
                    try:
                        Path(path).unlink(missing_ok=True)
                    except OSError:
                        pass
            self.last_engine_ms = int(data.get("ms") or 0)
            return self._to_lines(data, effective_scale)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stop_resident()
            if self._tmpdir is not None:
                shutil.rmtree(self._tmpdir, ignore_errors=True)
                self._tmpdir = None

    # -- 预处理 --
    def _prepare(self, image_or_path) -> tuple[str, float, bool]:
        if isinstance(image_or_path, Image.Image):
            src_path = None
        else:
            src_path = os.fspath(image_or_path)
        effective_scale = self.scale
        if self.preprocess == "none" and effective_scale == 1.0 and src_path is not None:
            return src_path, 1.0, False

        if isinstance(image_or_path, Image.Image):
            image = image_or_path
        else:
            with Image.open(src_path) as opened:
                image = opened.convert("RGB")
        try:
            with image:
                if self.preprocess != "none":
                    gray = image.convert("L")
                else:
                    gray = image
                longest = max(gray.width, gray.height)
                if longest * effective_scale > self.max_side:
                    effective_scale = self.max_side / float(longest)
                if effective_scale <= 0:
                    effective_scale = 1.0
                if effective_scale != 1.0:
                    scaled = gray.resize(
                        (
                            max(1, int(gray.width * effective_scale)),
                            max(1, int(gray.height * effective_scale)),
                        ),
                        Image.Resampling.LANCZOS,
                    )
                else:
                    scaled = gray
                if self.preprocess == "bin":
                    threshold = self.bin_threshold
                    prepared = ImageOps.invert(scaled).point(
                        lambda v, t=threshold: 255 if v > t else 0
                    )
                else:
                    prepared = scaled
                target = self._temp_path()
                prepared.save(target, format="PNG", compress_level=1)
            return str(target), effective_scale, True
        finally:
            if not isinstance(image_or_path, Image.Image):
                try:
                    image.close()
                except Exception:  # noqa: BLE001
                    pass

    def _temp_path(self) -> Path:
        if self._tmpdir is None:
            self._tmpdir = Path(tempfile.mkdtemp(prefix="wx_box_ocr_"))
        self._seq += 1
        return self._tmpdir / f"frame_{self._seq:06d}.png"

    # -- worker 管理 --
    def _ensure_resident(self) -> _ResidentWorker:
        if self._resident is not None and self._resident.proc is not None and self._resident.proc.poll() is None:
            return self._resident
        self._stop_resident()
        if self._ps_exe is None:
            raise _WorkerFailure("找不到 powershell.exe（可用环境变量 WX_OCR_POWERSHELL 指定）")
        script = _ensure_script()
        cwd = str(self._tmpdir) if self._tmpdir is not None else str(Path(tempfile.gettempdir()))
        worker = _ResidentWorker(self.timeout)
        ready = worker.start(self._ps_exe, script, cwd)
        self.max_dim = ready.get("max_dim")
        self._resident = worker
        self._resident_failures = 0
        return worker

    def _recognize(self, path: str) -> dict:
        if not self._resident_disabled:
            try:
                worker = self._ensure_resident()
                data = worker.request(path, self.timeout)
                if not data.get("ok"):
                    raise _WorkerFailure("引擎返回失败: " + str(data.get("error")))
                self._resident_failures = 0
                self.last_backend = "windows-resident"
                self.last_error = None
                return data
            except _WorkerFailure as exc:
                self.last_error = str(exc)
                self._resident_failed(exc)
                try:
                    data = self._run_oneshot(path)
                except _WorkerFailure as exc2:
                    self.last_error = f"{self.last_error}；一次性调用也失败: {exc2}"
                    raise
                self.last_backend = "windows-oneshot-fallback"
                return data
        data = self._run_oneshot(path)
        self.last_backend = "windows-oneshot"
        self.last_error = None
        return data

    def _resident_failed(self, exc: _WorkerFailure) -> None:
        self._stop_resident()
        self._resident_failures += 1
        if self._resident_failures >= MAX_RESIDENT_FAILURES:
            self._resident_disabled = True

    def _stop_resident(self) -> None:
        worker = self._resident
        self._resident = None
        if worker is not None:
            try:
                worker.stop()
            except Exception:  # noqa: BLE001
                pass

    def _run_oneshot(self, path: str) -> dict:
        if self._ps_exe is None:
            raise _WorkerFailure("找不到 powershell.exe")
        script = _ensure_script()
        args = [
            self._ps_exe,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            script,
            path,
        ]
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                timeout=self.timeout + 10.0,
                creationflags=_CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            raise _WorkerFailure(f"一次性 OCR 子进程超时: {exc}") from exc
        except OSError as exc:
            raise _WorkerFailure(f"启动 powershell 失败: {exc}") from exc
        stdout = proc.stdout or b""
        stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
        last = None
        for raw in stdout.splitlines():
            if not raw.strip():
                continue
            try:
                last = json.loads(raw.decode("utf-8").lstrip("\ufeff"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
        if last is None:
            raise _WorkerFailure(
                f"一次性 OCR 无有效输出（exit={proc.returncode}）: {stderr[:300]}"
            )
        if not last.get("ok"):
            raise _WorkerFailure("一次性 OCR 失败: " + str(last.get("error")))
        return last

    # -- 结果转换 --
    def _to_lines(self, data: dict, scale: float) -> list[OcrLine]:
        raw_lines = data.get("lines")
        if isinstance(raw_lines, dict):  # PS 5.1 ConvertTo-Json 单元素数组退化
            raw_lines = [raw_lines]
        if not isinstance(raw_lines, list):
            return []
        engine_w = int(data.get("width") or 0)
        engine_h = int(data.get("height") or 0)
        result: list[OcrLine] = []
        for raw in raw_lines:
            if not isinstance(raw, dict):
                continue
            text = _clean_text(str(raw.get("text") or ""))
            if not text:
                continue
            box = _scale_box(raw.get("box"), scale, engine_w, engine_h)
            words_raw = raw.get("words")
            if isinstance(words_raw, dict):
                words_raw = [words_raw]
            words = []
            for word in words_raw or []:
                if not isinstance(word, dict):
                    continue
                word_text = str(word.get("text") or "")
                if not word_text:
                    continue
                words.append(
                    {"text": word_text, "box": _scale_box(word.get("box"), scale, engine_w, engine_h)}
                )
            result.append(OcrLine(text=text, box=box, words=words))
        return result


# ---------------------------------------------------------------------------
# 对外封装
# ---------------------------------------------------------------------------
class OCRWorker:
    """统一 OCR 客户端：RapidOCR 主引擎 + Windows OCR 降级。

    参数
    ----
    engine:
        ``"auto"``（默认，优先 RapidOCR，失败自动降级）、``"rapidocr"``、``"windows"``。
    scale:
        RapidOCR 前把图放大多少倍（默认 1.0；实测放大会变差，一般不用改）。
    merge_max_gap_factor:
        同一行分段合并时允许的最大水平间隙 = 行高 × 该系数（默认 1.2）。
    timeout:
        Windows 降级引擎的单次识别超时秒数（默认 15）。
    rapid_kwargs:
        透传给 ``RapidOCR(**kwargs)`` 的参数（可选；进程内单例，第一次生效）。
    rapid_rec_batch_num:
        RapidOCR 识别批大小（默认 20，越大越快；同样第一次生效）。
    """

    def __init__(
        self,
        *,
        engine: str = ENGINE_AUTO,
        scale: float = 1.0,
        merge_max_gap_factor: float = DEFAULT_MERGE_GAP_FACTOR,
        timeout: float = DEFAULT_TIMEOUT,
        rapid_kwargs: dict | None = None,
        rapid_rec_batch_num: int = _RAPID_REC_BATCH_NUM,
        windows_scale: float = DEFAULT_WINDOWS_SCALE,
        windows_preprocess: str = "gray",
        windows_bin_threshold: int = 60,
        powershell: str | None = None,
    ) -> None:
        if engine not in _ENGINES:
            raise ValueError(f"engine 只能是 {_ENGINES}")
        if scale <= 0:
            raise ValueError("scale 必须 > 0")
        self.engine = engine
        self.scale = float(scale)
        self.merge_max_gap_factor = float(merge_max_gap_factor)
        self.timeout = float(timeout)
        self.rapid_kwargs = dict(rapid_kwargs or {})
        self.rapid_rec_batch_num = int(rapid_rec_batch_num)
        self.windows_scale = float(windows_scale)
        self.windows_preprocess = windows_preprocess
        self.windows_bin_threshold = int(windows_bin_threshold)
        self._powershell = powershell

        self._lock = threading.RLock()
        self._windows: _WindowsEngine | None = None
        self._rapid_disabled = False
        self._rapid_held = False
        self._closed = False

        # 诊断信息
        self.last_error: str | None = None
        self.last_ms: float = 0.0
        self.last_engine_ms: float = 0.0
        self.last_backend: str = ""
        self.last_engine_name: str = ""
        self.calls: int = 0
        self.failures: int = 0

    # ------------------------------------------------------------------ 公共接口
    def ocr(self, image_or_path) -> list[OcrLine]:
        """识别一张图，返回 ``list[OcrLine]``（box 为原图像素坐标）。

        ``image_or_path``：``PIL.Image.Image`` 或图片路径（str / PathLike）。
        识别失败返回 ``[]``，原因写入 :attr:`last_error`。
        """
        is_pil = isinstance(image_or_path, Image.Image)
        if not is_pil and not isinstance(image_or_path, (str, os.PathLike)):
            raise TypeError("ocr() 需要 PIL.Image.Image 或图片路径（str/PathLike）")
        src_path = None if is_pil else os.fspath(image_or_path)
        if not is_pil and not isinstance(src_path, str):
            raise TypeError("ocr() 路径参数必须是 str")
        if not is_pil and not Path(src_path).is_file():
            raise FileNotFoundError(src_path)

        started = time.perf_counter()
        with self._lock:
            self.calls += 1
            if self._closed:
                self.last_error = "OCRWorker 已关闭"
                self.last_ms = (time.perf_counter() - started) * 1000.0
                return []
            try:
                lines = self._dispatch(image_or_path)
            except (TypeError, FileNotFoundError):
                raise
            except Exception as exc:  # noqa: BLE001 - 兜底，绝不让监控线程崩
                self.failures += 1
                self.last_error = f"OCR 失败: {exc}"
                lines = []
        self.last_ms = (time.perf_counter() - started) * 1000.0
        return lines

    def warmup(self) -> bool:
        """提前加载引擎（可选，建议在后台线程调用）。成功返回 True。"""
        with self._lock:
            if self._closed:
                return False
            try:
                self._warmup_engine()
                return True
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                return False

    def close(self) -> None:
        """释放 Windows 降级 worker、临时文件，并释放 RapidOCR 会话持有引用。

        可重复调用。RapidOCR 引擎是进程内共享单例：所有 :class:`OCRWorker` 都关闭后
        （引用计数归零）才丢弃单例并 ``gc.collect()``，onnxruntime 会话随之释放。
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._windows is not None:
                self._windows.close()
                self._windows = None
            if self._rapid_held:
                self._rapid_held = False
                _rapid_release()

    def __enter__(self) -> "OCRWorker":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - 仅兜底
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass

    @property
    def backend(self) -> str:
        """最近一次识别实际使用的后端：``rapidocr`` / ``windows-resident`` 等。"""
        return self.last_backend

    @property
    def engine_name(self) -> str:
        """当前会使用的引擎名：``"rapidocr"`` 或 ``"windows"``（auto 模式会反映降级）。"""
        if self.engine == ENGINE_WINDOWS:
            return "windows"
        if self.engine == ENGINE_RAPID:
            return "rapidocr"
        if self._rapid_disabled or not rapidocr_available():
            return "windows"
        return "rapidocr"

    def info(self) -> dict:
        """日志用状态快照。"""
        return {
            "engine": self.engine_name,
            "backend": self.last_backend,
            "last_ms": round(self.last_ms, 1),
            "engine_ms": round(self.last_engine_ms, 1),
            "calls": self.calls,
            "failures": self.failures,
            "error": self.last_error,
        }

    # ------------------------------------------------------------------ 引擎调度
    def _dispatch(self, image_or_path) -> list[OcrLine]:
        choice = self.engine
        if choice in (ENGINE_AUTO, ENGINE_RAPID) and not self._rapid_disabled:
            try:
                return self._ocr_rapid(image_or_path)
            except OcrUnavailableError as exc:
                self.last_error = str(exc)
                if choice == ENGINE_RAPID:
                    self.failures += 1
                    return []
                self._rapid_disabled = True  # auto 模式：之后直接走 Windows
            except Exception as exc:  # noqa: BLE001 - 推理异常也降级
                self.last_error = f"RapidOCR 识别失败: {exc}"
                if choice == ENGINE_RAPID:
                    self.failures += 1
                    return []
                self._rapid_disabled = True
        return self._ocr_windows(image_or_path)

    def _warmup_engine(self) -> None:
        if self.engine in (ENGINE_AUTO, ENGINE_RAPID) and not self._rapid_disabled:
            try:
                self._acquire_rapid()
                self.last_backend = "rapidocr"
                self.last_engine_name = "rapidocr"
                return
            except OcrUnavailableError:
                if self.engine == ENGINE_RAPID:
                    raise
                self._rapid_disabled = True
        self._ensure_windows()._ensure_resident()

    def _acquire_rapid(self) -> None:
        """首次使用时持有 RapidOCR 单例引用（close() 时释放）。"""
        if not self._rapid_held:
            _rapid_acquire(self.rapid_kwargs or None, self.rapid_rec_batch_num)
            self._rapid_held = True

    # -- RapidOCR --
    def _ocr_rapid(self, image_or_path) -> list[OcrLine]:
        try:
            import numpy as np
        except Exception as exc:  # noqa: BLE001
            raise OcrUnavailableError(f"numpy 不可用: {exc}") from exc
        self._acquire_rapid()
        is_pil = isinstance(image_or_path, Image.Image)
        ctx = _null_ctx() if is_pil else Image.open(image_or_path)
        with ctx as opened:
            image = image_or_path if is_pil else opened
            if self.scale != 1.0:
                image = image.resize(
                    (
                        max(1, int(image.width * self.scale)),
                        max(1, int(image.height * self.scale)),
                    ),
                    Image.Resampling.LANCZOS,
                )
            arr = np.asarray(image.convert("RGB"))
            result, engine_ms = _rapid_run_batch(
                arr, self.rapid_kwargs or None, self.rapid_rec_batch_num
            )
        self.last_backend = "rapidocr"
        self.last_engine_name = "rapidocr"
        self.last_engine_ms = engine_ms
        self.last_error = None
        return _segments_to_lines(result, self.merge_max_gap_factor)

    # -- Windows 降级 --
    def _ensure_windows(self) -> _WindowsEngine:
        if self._windows is None:
            self._windows = _WindowsEngine(
                scale=self.windows_scale,
                preprocess=self.windows_preprocess,
                bin_threshold=self.windows_bin_threshold,
                timeout=self.timeout,
                powershell=self._powershell,
            )
        return self._windows

    def _ocr_windows(self, image_or_path) -> list[OcrLine]:
        engine = self._ensure_windows()
        try:
            lines = engine.recognize(image_or_path)
        except _WorkerFailure as exc:
            self.failures += 1
            self.last_error = str(exc)
            return []
        self.last_backend = engine.last_backend or "windows"
        self.last_engine_name = "windows"
        self.last_engine_ms = float(engine.last_engine_ms)
        if engine.last_error:
            self.last_error = engine.last_error
        return lines


class _null_ctx:
    """空上下文管理器（PIL 输入时不需要 with Image.open）。"""

    def __enter__(self):
        return None

    def __exit__(self, *args) -> None:
        return None


# ---------------------------------------------------------------------------
# 坐标 / 文本工具
# ---------------------------------------------------------------------------
def _scale_box(box, scale: float, width: int, height: int) -> tuple[int, int, int, int]:
    """把放大图上的 box 缩回原图坐标，并裁剪到图像范围内。"""
    try:
        vals = [float(v) for v in (box or (0, 0, 0, 0))][:4]
    except (TypeError, ValueError):
        vals = [0.0, 0.0, 0.0, 0.0]
    while len(vals) < 4:
        vals.append(0.0)
    inv = 1.0 / scale if scale else 1.0
    left = int(round(vals[0] * inv))
    top = int(round(vals[1] * inv))
    right = int(round(vals[2] * inv))
    bottom = int(round(vals[3] * inv))
    if width > 0:
        left = min(max(left, 0), width)
        right = min(max(right, 0), width)
    if height > 0:
        top = min(max(top, 0), height)
        bottom = min(max(bottom, 0), height)
    if right < left:
        right = left
    if bottom < top:
        bottom = top
    return (left, top, right, bottom)


_CJK_RANGES = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x3000, 0x303F),  # CJK 标点
    (0xFF00, 0xFFEF),  # 全角字符
)
_CJK_PUNCT = "，。！？；：、（）《》“”‘’·…—"


def _is_cjk(ch: str) -> bool:
    if not ch:
        return False
    code = ord(ch)
    for low, high in _CJK_RANGES:
        if low <= code <= high:
            return True
    return ch in _CJK_PUNCT


def _clean_text(text: str) -> str:
    """清理 OCR 排版噪声。

    * 去掉带换行/制表/全角空格的空白；
    * 去掉中文（含中文标点）之间的空格（OCR 常把 ``微 信 团 队`` 拆开）；
    * 把 OCR 拆开的单个拉丁字母/数字粘回去（``c p`` -> ``cp``）；
    * 其它英文单词之间的空格保留。
    """
    text = text.replace("\u3000", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    chars = list(text)
    out: list[str] = []
    i = 0
    n = len(chars)
    while i < n:
        ch = chars[i]
        if ch == " ":
            while i < n and chars[i] == " ":
                i += 1
            prev = out[-1] if out else ""
            nxt = chars[i] if i < n else ""
            if not prev or not nxt:
                continue
            if _is_cjk(prev) or _is_cjk(nxt):
                continue
            if (
                len(prev) == 1
                and len(nxt) == 1
                and prev.isascii()
                and nxt.isascii()
                and prev.isalnum()
                and nxt.isalnum()
            ):
                before = out[-2] if len(out) >= 2 else ""
                after = chars[i + 1] if i + 1 < n else ""
                if (not before.isascii() or not before.isalnum()) and (
                    not after.isascii() or not after.isalnum()
                ):
                    continue
            out.append(" ")
            continue
        out.append(ch)
        i += 1
    return "".join(out).strip()
