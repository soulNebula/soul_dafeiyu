# -*- coding: utf-8 -*-
"""P0-send：用系统 .NET UIA（PowerShell）精确定位的微信发送后端（默认关闭）。

背景：wxauto4 内置的 comtypes UIA 在本机枚举窗口树返回 0，而系统 .NET UIA
（PowerShell `System.Windows.Automation`）正常，能看到 ``mmui::MainWindow``、
会话列表 ``mmui::ChatSessionCell``、输入框 ``mmui::ChatInputField``。

流程（每一步都 fail-closed，任何一步不确定都**中止发送**）：
1. ps1 **只读**查询：主窗口 / 联系人矩形 / 输入框矩形（不点击、不输入）；
2. Python 激活微信 → 点击联系人矩形中心（找不到时用 UIA 搜索框：Python 粘贴
   联系人名 → 回车 → OCR 校验标题）；
3. `win_send._verify_chat_open`（wx_core OCR）校验聊天标题包含联系人；
4. 点输入框（优先 UIA 矩形，否则配置比例坐标 0.68w/0.93h）→ 剪贴板粘贴 → 回车。

开启方式（``agi/data/wechat/config.json``）：
``{"fallback_enabled": true, "fallback_mode": "auto", "fallback_send_mode": "uia"}``
默认仍是关闭；本模块不主动打开任何配置。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from . import win_send

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "wechat_uia" / "uia_query.ps1"
POWERSHELL = os.environ.get("WECHAT_UIA_POWERSHELL", "powershell.exe")
QUERY_TIMEOUT_S = 15.0
_OK_STATE = "ok"


# ------------------------------------------------------------------ ps1 查询
def available() -> bool:
    """powershell.exe 可执行 + ps1 脚本存在。"""
    if not SCRIPT.is_file():
        return False
    exe = POWERSHELL
    return bool(shutil.which(exe) or Path(exe).is_file())


def _parse_json(stdout: str) -> dict:
    """PowerShell 可能夹带 warning，取最后一行可解析的 JSON 对象。"""
    for line in reversed(str(stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                return value
        except (TypeError, ValueError):
            continue
    return {}


def _query(*args: str, timeout: float = QUERY_TIMEOUT_S,
           runner: Callable | None = None) -> dict:
    """通用 ps1 调用；返回解析后的原始 dict（失败时 ok=False+error）。"""
    if not SCRIPT.is_file():
        return {"ok": False, "error": "script_missing"}
    cmd = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-File", str(SCRIPT), *[str(arg) for arg in args]]
    run = runner or subprocess.run
    try:
        proc = run(cmd, capture_output=True, text=True, encoding="utf-8",
                   errors="replace", timeout=timeout,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    stdout = str(getattr(proc, "stdout", "") or "")
    payload = _parse_json(stdout)
    if not payload:
        return {"ok": False, "error": "invalid_json",
                "stdout_tail": stdout[-1000:]}
    return payload


def _normalize(payload: dict) -> dict:
    return {
        "ok": bool(payload.get("ok")),
        "window": payload.get("window") or {},
        "contact": payload.get("contact") or {"found": False},
        "input": payload.get("input") or {"found": False},
        "search_box": payload.get("search_box") or {"found": False},
        "error": str(payload.get("error") or ""),
    }


def _run_query(*, contact: str = "", find_input: bool = False,
               timeout: float = QUERY_TIMEOUT_S,
               runner: Callable | None = None) -> dict:
    args: list[str] = []
    if contact:
        args += ["-Contact", str(contact)]
    if find_input:
        args.append("-FindInput")
    payload = _query(*args, timeout=timeout, runner=runner)
    return _normalize(payload)


def find_contact(contact: str) -> dict:
    """只读查联系人矩形；优先常驻 worker，失败降级一次性 ps1。"""
    worker = _try_worker("find_contact", contact)
    if isinstance(worker, dict) and worker.get("ok"):
        return worker
    return _run_query(contact=str(contact or ""))


def find_input() -> dict:
    """只读查聊天输入框矩形：优先常驻 worker，失败降级一次性 ps1。"""
    worker = _try_worker("find_input")
    if isinstance(worker, dict) and worker.get("ok"):
        return worker
    return _run_query(find_input=True)


# ------------------------------------------------------------------ 兼容旧接口
def find_session(contact: str) -> dict:
    """兼容旧调用：优先常驻 worker，失败降级一次性 ps1。"""
    worker = _try_worker("find_session", contact)
    if isinstance(worker, dict) and worker.get("ok"):
        return worker
    payload = find_contact(contact)
    rect = payload.get("contact") or {}
    if rect.get("found"):
        flat = {key: rect.get(key) for key in ("x", "y", "w", "h")}
        if all(value is not None for value in flat.values()):
            return {"ok": True, "window": payload.get("window") or {},
                    "contact": {"found": True, "rect": flat}}
        if isinstance(rect.get("rect"), dict):
            return {"ok": True, "window": payload.get("window") or {},
                    "contact": rect}
    return {"ok": bool(payload.get("ok")),
            "contact": {"found": False},
            "error": payload.get("error") or ""}


def chat_title() -> str:
    """兼容旧接口：优先常驻 worker 的 title，失败再用旧 query.ps1 -Title。"""
    worker_title = _try_worker("chat_title")
    if worker_title:
        return str(worker_title)
    legacy = ROOT / "tools" / "wechat_uia" / "query.ps1"
    if not legacy.is_file():
        return ""
    saved = globals().get("SCRIPT")
    try:
        globals()["SCRIPT"] = legacy
        payload = _query("-Title")
        return str(payload.get("title") or "")
    finally:
        if saved is not None:
            globals()["SCRIPT"] = saved


# ------------------------------------------------------------------ 低层点击
def _rect_center(rect: dict | None) -> tuple[int, int]:
    rect = rect or {}
    return (int(rect.get("x", 0)) + int(rect.get("w", 0)) // 2,
            int(rect.get("y", 0)) + int(rect.get("h", 0)) // 2)


def _click(win, info, rect: dict | None) -> bool:
    if not isinstance(rect, dict) or not rect:
        return False
    x, y = _rect_center(rect)
    try:
        return bool(win.real_click(info, int(x), int(y)))
    except Exception:  # noqa: BLE001
        try:
            return bool(win.post_click(info, int(x), int(y)))
        except Exception:  # noqa: BLE001
            return False


def _activate(win) -> tuple[bool, str]:
    try:
        info = win.find_wechat_window()
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    if info is None:
        return False, "没找到微信窗口"
    for _ in range(3):
        try:
            if win.bring_to_front(info) or win.is_foreground(info):
                time.sleep(0.5)
                if win.is_foreground(info):
                    return True, ""
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    return False, "前台不是微信窗口（切换到前台失败）"


def _verify(win, info, contact: str) -> str:
    try:
        return str(win_send._verify_chat_open(win, info, contact) or "unknown")
    except Exception:  # noqa: BLE001
        return "unknown"


def _open_by_rect(win, info, contact: str, rect: dict) -> tuple[bool, str]:
    if not _click(win, info, rect):
        return False, "click_contact_failed"
    time.sleep(1.2)
    state = _verify(win, info, contact)
    if state != _OK_STATE:
        time.sleep(0.8)
        state = _verify(win, info, contact)
    if state == _OK_STATE:
        return True, ""
    return False, f"verify_{state}"


def _open_by_search(win, info, contact: str, search_box: dict,
                    clipboard) -> tuple[bool, str]:
    if not _click(win, info, search_box):
        return False, "click_search_failed"
    time.sleep(0.4)
    if not clipboard.set_text(str(contact)):
        return False, "clipboard_failed"
    time.sleep(0.15)
    win_send._ctrl_v()
    time.sleep(0.6)
    win.press_key(win_send.VK_RETURN)
    time.sleep(1.2)
    state = _verify(win, info, contact)
    if state == _OK_STATE:
        return True, ""
    return False, f"verify_{state}"


def _input_rect(win, info, cfg: dict) -> tuple[dict, str]:
    payload = find_input()
    if payload.get("ok") and (payload.get("input") or {}).get("found"):
        return payload["input"], "uia"
    box = cfg.get("fallback_input_box") or []
    try:
        image = win.capture(info)
        width, height = (image.size if image is not None else (960, 900))
    except Exception:  # noqa: BLE001
        width, height = 960, 900
    if isinstance(box, (list, tuple)) and len(box) == 2:
        try:
            x, y = float(box[0]), float(box[1])
            if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
                return {"found": True, "x": int(width * x), "y": int(height * y),
                        "w": 1, "h": 1}, "fallback_ratio"
            return {"found": True, "x": int(x), "y": int(y), "w": 1, "h": 1}, "fallback_px"
        except (TypeError, ValueError):
            pass
    return {"found": True, "x": int(width * 0.68), "y": int(height * 0.93),
            "w": 1, "h": 1}, "fallback_default"


# ------------------------------------------------------------------ 发送
def send_to_contact(contact: str, text: str, cfg: dict[str, Any] | None = None, *,
                    settle: float = 0.5) -> dict[str, Any]:
    """UIA 定位 + OCR 标题校验 + 窗口发送；失败一律不发送（fail-closed）。"""
    cfg = dict(cfg or {})
    who = str(contact or "").strip()
    value = str(text or "").strip()

    def failed(error: str, status: str = "failed", **extra: Any) -> dict:
        out = {"ok": False, "status": status, "method": "uia",
               "verified": False, "error": error, "sent": False}
        out.update(extra)
        return out

    if not cfg.get("fallback_enabled"):
        return failed("fallback 未开启", status="disabled")
    if not who or not value:
        return failed("contact/text 为空")
    if not available():
        return failed("uia_unavailable", status="unavailable")
    loaded, err = win_send._load()
    if loaded is None:
        return failed(f"wx_core 不可用：{err}")
    win, clipboard = loaded
    prev_fg = None
    try:
        import ctypes as _ct
        prev_fg = _ct.windll.user32.GetForegroundWindow()
    except Exception:  # noqa: BLE001
        prev_fg = None
    try:
        activated, activate_error = _activate(win)
        if not activated:
            return failed(activate_error)
        try:
            info = win.find_wechat_window()
        except Exception as exc:  # noqa: BLE001
            return failed(f"{type(exc).__name__}: {exc}")
        payload = find_contact(who)
        if not payload.get("ok"):
            return failed(str(payload.get("error") or "uia_query_failed"),
                          window=payload.get("window") or {})
        window = payload.get("window") or {}
        contact_rect = payload.get("contact") or {"found": False}
        search_box = payload.get("search_box") or {"found": False}
        if contact_rect.get("found"):
            ok_open, open_error = _open_by_rect(win, info, who, contact_rect)
            opened_by = "contact_rect"
        elif search_box.get("found"):
            ok_open, open_error = _open_by_search(win, info, who, search_box,
                                                  clipboard)
            opened_by = "search"
        else:
            return failed("contact_not_found", window=window)
        if not ok_open:
            return failed(open_error or "verify_failed", window=window,
                          opened_by=opened_by)
        rect, source = _input_rect(win, info, cfg)
        if not rect:
            return failed("input_not_found", opened_by=opened_by)
        if not _click(win, info, rect):
            return failed("input_click_failed", opened_by=opened_by)
        time.sleep(0.4)
        if settle:
            time.sleep(settle)
        if not clipboard.set_text(value):
            return failed("clipboard_failed", opened_by=opened_by)
        time.sleep(0.15)
        win_send._ctrl_v()
        time.sleep(0.5)
        win.press_key(win_send.VK_RETURN)
        time.sleep(0.8)
    except Exception as exc:  # noqa: BLE001
        return failed(f"{type(exc).__name__}: {exc}")
    finally:
        try:
            if prev_fg:
                import ctypes as _ct
                _ct.windll.user32.SetForegroundWindow(prev_fg)
        except Exception:  # noqa: BLE001
            pass
    return {"ok": True, "status": "sent", "method": "uia", "verified": True,
            "sent": True, "contact": who, "window": window,
            "contact_rect": contact_rect, "input_rect": rect,
            "input_source": source, "opened_by": opened_by}


def smoke(contact: str = "文件传输助手") -> dict:
    """只查不发：返回 find_contact + find_input 的结果（供 tools/wechat_uia/smoke.py）。"""
    return {"ok": True, "available": available(),
            "contact": find_contact(contact), "input": find_input(),
            "note": "只读查询，不发送任何消息"}


# ------------------------------------------------------------------ P0-2：媒体发送
WORKER_SCRIPT = ROOT / "tools" / "wechat_uia" / "uia_worker.ps1"
WORKER_TIMEOUT_S = 10.0
_MEDIA_TYPES = ("image", "gif", "sticker")


def _worker_enabled() -> bool:
    """常驻 worker 默认开（生产）；pytest 下默认关，测试显式开/直接实例化。"""
    value = os.environ.get("WECHAT_UIA_WORKER")
    if value is not None:
        return str(value).strip().lower() not in ("0", "false", "no", "off", "")
    return not bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _resolve_media_path(item: dict) -> Path | None:
    raw = str((item or {}).get("path") or "").strip()
    if not raw:
        return None
    target = Path(raw)
    if not target.is_absolute():
        target = ROOT / target
    try:
        return target if target.is_file() else None
    except OSError:
        return None


def _copy_image_to_clipboard(path: str | Path) -> bool:
    """PIL 读图 → RGB BMF（去 14 字节文件头）→ win32clipboard CF_DIB。"""
    try:
        from PIL import Image
        import io
        import win32clipboard
        import win32con
        img = Image.open(str(path))
        if getattr(img, "mode", "RGB") not in ("RGB",):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "BMP")
        data = buf.getvalue()[14:]
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_DIB, data)
        finally:
            win32clipboard.CloseClipboard()
        return True
    except Exception:  # noqa: BLE001 - 剪贴板失败一律返回 False（fail-closed）
        return False


def send_media(contact: str, item: dict, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """UIA 发送图片/贴纸/GIF：PIL→CF_DIB 剪贴板 → Ctrl+V → 等 0.8s → 回车。

    GIF 粘贴到微信会退化为静态图（结果 ``animated: false``）；任何一步不确定都中止，
    返回 ``ok=false``，不盲发。``item`` 需含 ``type`` + ``path``。
    """
    cfg = dict(cfg or {})
    who = str(contact or "").strip()
    item = dict(item or {})
    kind = str(item.get("type") or "").strip().lower()

    def failed(error: str, status: str = "failed", **extra: Any) -> dict:
        out = {"ok": False, "status": status, "method": "uia",
               "verified": False, "error": error, "sent": False,
               "type": kind}
        out.update(extra)
        return out

    if not cfg.get("fallback_enabled"):
        return failed("fallback 未开启", status="disabled")
    if not who:
        return failed("contact 为空")
    if kind not in _MEDIA_TYPES:
        return failed(f"unsupported_media_type:{kind or 'unknown'}", status="unsupported")
    path = _resolve_media_path(item)
    if path is None:
        return failed("media_path_missing")
    if not available():
        return failed("uia_unavailable", status="unavailable")
    loaded, err = win_send._load()
    if loaded is None:
        return failed(f"wx_core 不可用：{err}")
    win, _clipboard = loaded
    prev_fg = None
    try:
        import ctypes as _ct
        prev_fg = _ct.windll.user32.GetForegroundWindow()
    except Exception:  # noqa: BLE001
        prev_fg = None
    try:
        activated, activate_error = _activate(win)
        if not activated:
            return failed(activate_error)
        try:
            info = win.find_wechat_window()
        except Exception as exc:  # noqa: BLE001
            return failed(f"{type(exc).__name__}: {exc}")
        payload = find_contact(who)
        if not payload.get("ok"):
            return failed(str(payload.get("error") or "uia_query_failed"),
                          window=payload.get("window") or {})
        window = payload.get("window") or {}
        contact_rect = payload.get("contact") or {"found": False}
        search_box = payload.get("search_box") or {"found": False}
        if contact_rect.get("found"):
            rect = {key: contact_rect.get(key) for key in ("x", "y", "w", "h")}
            ok_open, open_error = _open_by_rect(win, info, who, rect)
            opened_by = "contact_rect"
        elif search_box.get("found"):
            ok_open, open_error = _open_by_search(win, info, who, search_box, _clipboard)
            opened_by = "search"
        else:
            return failed("contact_not_found", window=window)
        if not ok_open:
            return failed(open_error or "verify_failed", window=window,
                          opened_by=opened_by)
        # 媒体发送要求 UIA 明确找到输入框（不像文字可退化到比例坐标，避免盲发图片）
        input_payload = find_input()
        input_rect = (input_payload.get("input") or {}) if input_payload.get("ok") else {}
        if not input_rect.get("found"):
            return failed("input_not_found", opened_by=opened_by)
        if not _click(win, info, input_rect):
            return failed("input_click_failed", opened_by=opened_by)
        time.sleep(0.4)
        if not _copy_image_to_clipboard(str(path)):
            return failed("clipboard_image_failed", opened_by=opened_by)
        time.sleep(0.15)
        win_send._ctrl_v()
        time.sleep(0.8)          # 微信粘贴图片需要时间
        win.press_key(win_send.VK_RETURN)
        time.sleep(0.8)
    except Exception as exc:  # noqa: BLE001
        return failed(f"{type(exc).__name__}: {exc}")
    finally:
        try:
            if prev_fg:
                import ctypes as _ct
                _ct.windll.user32.SetForegroundWindow(prev_fg)
        except Exception:  # noqa: BLE001
            pass
    # 微信粘贴 GIF 只会发静态图 → animated 永远 false（附加 warning 说明）
    return {"ok": True, "status": "sent", "method": "uia", "verified": True,
            "sent": True, "contact": who, "type": kind, "path": str(path),
            "animated": False, "window": window,
            "contact_rect": contact_rect, "input_rect": input_rect,
            "opened_by": opened_by,
            "warning": "GIF 已退化为静态图" if kind == "gif" else ""}


# ------------------------------------------------------------------ P0-2：常驻 worker
class UIAClient:
    """常驻 PowerShell UIA worker 客户端：懒启动、加锁、逐行请求/响应。

    - 超时 ``timeout`` 秒（默认 10s）；进程挂掉自动重启一次后重试；
    - ``spawn`` 可注入（测试用），默认 ``subprocess.Popen``；
    - 单进程串行（锁），请求/响应各一行 JSON。
    """

    def __init__(self, *, script: Path | str | None = None,
                 powershell: str | None = None, timeout: float = WORKER_TIMEOUT_S,
                 spawn: Callable[[], Any] | None = None) -> None:
        self.script = Path(script or WORKER_SCRIPT)
        self.powershell = str(powershell or POWERSHELL)
        self.timeout = float(timeout)
        self._spawn = spawn or self._default_spawn
        self._proc: Any = None
        self._lock = __import__("threading").Lock()
        self.starts = 0          # 实际启动次数（便于测试/诊断）
        self.restarts = 0

    # -- 进程生命周期 ------------------------------------------------------
    def _default_spawn(self) -> Any:
        cmd = [self.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", str(self.script)]
        return subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
            bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def _alive(self, proc: Any) -> bool:
        try:
            return proc is not None and proc.poll() is None
        except Exception:  # noqa: BLE001
            return False

    def _ensure(self) -> Any:
        if not self._alive(self._proc):
            self._proc = self._spawn()
            self.starts += 1
        return self._proc

    def _kill(self, proc: Any) -> None:
        try:
            if proc is not None and self._alive(proc):
                proc.kill()
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        with self._lock:
            self._kill(self._proc)
            self._proc = None

    # -- 单行请求 ----------------------------------------------------------
    @staticmethod
    def _readline_with_timeout(proc: Any, timeout: float):
        import queue
        import threading
        box: "queue.Queue[Any]" = queue.Queue(maxsize=1)

        def reader() -> None:
            try:
                box.put(("ok", proc.stdout.readline()))
            except Exception as exc:  # noqa: BLE001
                box.put(("error", exc))

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            kind, value = box.get(timeout=timeout)
        except queue.Empty:
            return "timeout"
        if kind == "error":
            return f"error:{type(value).__name__}: {value}"
        return str(value)

    def request(self, payload: dict) -> dict:
        """发送一条命令并读一行 JSON；失败返回 ``{ok:false,error}``。"""
        with self._lock:
            for attempt in range(2):
                proc = None
                try:
                    proc = self._ensure()
                    proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                    proc.stdin.flush()
                except Exception as exc:  # noqa: BLE001
                    self._kill(proc)
                    self._proc = None
                    if attempt == 0:
                        self.restarts += 1
                        continue
                    return {"ok": False, "error": f"write_failed:{type(exc).__name__}"}
                line = self._readline_with_timeout(proc, self.timeout)
                if line == "timeout":
                    self._kill(proc)
                    self._proc = None
                    self.restarts += 1
                    return {"ok": False, "error": "timeout"}
                if not line or line.startswith("error:"):
                    # 进程崩溃/EOF → 重启一次后重试
                    self._kill(proc)
                    self._proc = None
                    if attempt == 0:
                        self.restarts += 1
                        continue
                    return {"ok": False, "error": "worker_crashed"}
                parsed = _parse_json(line)
                if parsed:
                    return parsed
                self._kill(proc)
                self._proc = None
                return {"ok": False, "error": "invalid_json",
                        "stdout_tail": line[-1000:]}
            return {"ok": False, "error": "worker_unavailable"}

    def _request_normalized(self, payload: dict) -> dict:
        payload = self.request(payload)
        return _normalize(payload)

    # -- 兼容 module 函数 --------------------------------------------------
    def find_contact(self, contact: str) -> dict:
        return self._request_normalized({"cmd": "contact", "name": str(contact or "")})

    def find_input(self) -> dict:
        return self._request_normalized({"cmd": "input"})

    def chat_title(self) -> str:
        result = self.request({"cmd": "title"})
        return str(result.get("title") or "")

    def find_session(self, contact: str) -> dict:
        payload = self.find_contact(contact)
        rect = payload.get("contact") or {}
        if rect.get("found"):
            flat = {key: rect.get(key) for key in ("x", "y", "w", "h")}
            if all(value is not None for value in flat.values()):
                return {"ok": True, "window": payload.get("window") or {},
                        "contact": {"found": True, "rect": flat}}
            if isinstance(rect.get("rect"), dict):
                return {"ok": True, "window": payload.get("window") or {},
                        "contact": rect}
        return {"ok": bool(payload.get("ok")),
                "contact": {"found": False},
                "error": payload.get("error") or ""}


_WORKER_CLIENT: UIAClient | None = None


def worker_client() -> UIAClient:
    global _WORKER_CLIENT
    if _WORKER_CLIENT is None:
        _WORKER_CLIENT = UIAClient()
    return _WORKER_CLIENT


def _try_worker(method: str, *args) -> Any:
    """worker 可用时优先走常驻 client；任何异常/未启用 → None（调用方降级）。"""
    if not _worker_enabled():
        return None
    script = Path(WORKER_SCRIPT)
    if not script.is_file():
        return None
    try:
        return getattr(worker_client(), method)(*args)
    except Exception:  # noqa: BLE001 - worker 失败一律降级到一次性 ps1
        return None
