#!/usr/bin/env python3
"""wx_box 图形界面入口。

启动顺序：
1. 读取 config.json（缺失时由 wx_core.config 生成默认值）；
2. 决定后端：``--fake`` / ``WXBOX_FAKE=1`` -> ui/dev_backend.DevBackend；
   否则尝试加载真实后端（``wx_core.monitor.create_backend(config)`` 等）；
   真实后端 import / 初始化失败时自动回退假后端，并在界面日志区警告；
3. 创建 Tk 主窗口并启动 100ms 事件泵。

常用命令::

    python app.py                 # 真实后端（需微信桌面端已登录）
    python app.py --fake          # 假后端演示，无需微信
    python app.py --fake --screenshot recon/gui_preview.png --duration 12
"""
from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui.dev_backend import DevBackend  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402

# 真实后端候选：(模块名, 属性名候选)；工厂函数/类都支持，优先传 config 字典。
_BACKEND_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("wx_core.monitor", ("create_backend", "MonitorBackend", "Monitor", "Backend")),
    ("wx_core.backend", ("create_backend", "Backend")),
    ("wx_core", ("create_backend", "Backend")),
)

_REQUIRED_BACKEND_METHODS = ("start", "stop", "get_state", "list_friends", "set_monitored")

_FAKE_ENV_VALUES = {"1", "true", "yes", "on", "y"}


# ---------------------------------------------------------------------------
# 配置 / 后端加载
# ---------------------------------------------------------------------------
def load_config() -> dict:
    """读取 wx_core.config 的配置字典；任何异常都返回空字典。"""
    try:
        from wx_core import config as core_config

        config = core_config.load()
        return dict(config) if isinstance(config, dict) else {}
    except Exception:  # noqa: BLE001 - 配置读取失败不应阻止界面启动
        return {}


def _valid_backend(obj) -> bool:
    """检查对象是否满足 wx_core.api.Backend 协议（events 队列 + 必需方法）。"""
    if obj is None or not hasattr(obj, "events"):
        return False
    return all(callable(getattr(obj, name, None)) for name in _REQUIRED_BACKEND_METHODS)


def _instantiate(factory, config: dict):
    """尝试用 config 构造后端对象，失败后再尝试无参构造。

    返回 (对象, 错误文本)；成功时错误文本为 None。
    """
    errors: list[str] = []
    for args in ((config,), ()):
        try:
            return factory(*args), None
        except Exception as exc:  # noqa: BLE001 - 继续尝试下一种签名
            errors.append(f"{type(exc).__name__}: {exc}")
    return None, " / ".join(errors)


def make_fake_backend(config: dict, interval: float | None = None) -> DevBackend:
    """创建假后端（演示用），interval 可由调用方或环境变量覆盖。"""
    return DevBackend(config, interval=interval)


def load_backend(
    config: dict | None = None,
    *,
    force_fake: bool = False,
    fake_interval: float | None = None,
) -> tuple[object, str, bool]:
    """加载后端。返回 (backend, 回退原因, 是否假后端)。

    * ``force_fake=True`` 或真实后端不可用时返回 DevBackend；
    * force_fake 时回退原因为空字符串（调用方自行决定如何提示）。
    """
    config = dict(config or {})
    if force_fake:
        return make_fake_backend(config, fake_interval), "", True

    errors: list[str] = []
    for module_name, attribute_names in _BACKEND_CANDIDATES:
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - 模块还不存在属于正常情况
            errors.append(f"{module_name}: {type(exc).__name__}: {exc}")
            continue
        for attribute_name in attribute_names:
            factory = getattr(module, attribute_name, None)
            if factory is None or not callable(factory):
                continue
            backend, error = _instantiate(factory, config)
            if _valid_backend(backend):
                return backend, "", False
            errors.append(
                f"{module_name}.{attribute_name}: {error or '返回对象不满足 Backend 协议'}"
            )

    reason = "真实后端不可用，已自动回退到假后端演示模式。"
    if errors:
        reason += " 探测详情：" + "；".join(errors[-3:])
    return make_fake_backend(config, fake_interval), reason, True


# ---------------------------------------------------------------------------
# 窗口截图（Pillow ImageGrab，只截自己的窗口区域）
# ---------------------------------------------------------------------------
def capture_window(root, window: MainWindow, path: str | Path) -> str:
    """抓取主窗口区域并保存为 PNG，返回保存路径（失败返回空字符串）。"""
    target = Path(path)
    if not target.is_absolute():
        target = ROOT / target
    try:
        from PIL import ImageGrab

        root.update_idletasks()
        root.lift()
        root.attributes("-topmost", True)
        root.update()
        x, y = root.winfo_rootx(), root.winfo_rooty()
        width, height = root.winfo_width(), root.winfo_height()
        image = ImageGrab.grab(bbox=(x, y, x + width, y + height), all_screens=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        image.save(target)
        window.log("info", f"界面截图已保存：{target}")
        return str(target)
    except Exception as exc:  # noqa: BLE001 - 截图失败不影响正常使用
        try:
            window.log("error", f"界面截图失败：{exc}")
        except Exception:
            pass
        return ""
    finally:
        try:
            root.attributes("-topmost", False)
        except Exception:
            pass


def _enable_dpi_awareness() -> None:
    """在 Windows 上开启 DPI 感知，避免界面模糊（失败则忽略）。"""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def build_arg_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="app.py",
        description="微信好友消息监听器（只读：截图 + OCR，不读写微信进程内存）。",
    )
    parser.add_argument(
        "--fake",
        action="store_true",
        help="使用假后端演示：每 2 秒生成一条示例消息，不连接真实微信",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=None,
        help="假后端生成消息的间隔秒数（默认 2 秒，仅 --fake 生效）",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="运行 N 秒后自动关闭（0 表示一直运行，便于自动化截图）",
    )
    parser.add_argument(
        "--screenshot",
        default="",
        help="启动一段时间后把主窗口截图保存到指定路径（如 recon/gui_preview.png）",
    )
    parser.add_argument(
        "--screenshot-delay",
        type=float,
        default=6.0,
        help="截图前等待的秒数（默认 6 秒，让示例消息先出现）",
    )
    parser.add_argument(
        "--geometry",
        default="",
        help="覆盖窗口尺寸（如 1220x1000），主要用于自动化截图，默认 1220x860",
    )
    return parser


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """程序入口：解析参数、加载后端、创建窗口并进入事件循环。"""
    args = build_arg_parser().parse_args(argv)
    _enable_dpi_awareness()
    config = load_config()

    env_fake = os.environ.get("WXBOX_FAKE", "").strip().lower() in _FAKE_ENV_VALUES
    force_fake = bool(args.fake or env_fake)
    backend, fallback_reason, is_fake = load_backend(
        config, force_fake=force_fake, fake_interval=args.interval
    )

    try:
        import tkinter as tk

        root = tk.Tk()
    except Exception as exc:  # noqa: BLE001 - 无桌面环境时给出明确提示
        print(f"无法创建 Tk 窗口（可能没有桌面环境）：{exc}", file=sys.stderr)
        return 1

    root.withdraw()
    window = MainWindow(
        root,
        backend,
        config=config,
        demo_mode=is_fake,
        demo_note=fallback_reason,
    )
    window.start()
    if args.geometry:
        try:
            root.geometry(args.geometry)
        except Exception:  # noqa: BLE001 - 非法尺寸忽略
            window.log("warn", f"忽略非法窗口尺寸参数：{args.geometry}")
    root.deiconify()

    if is_fake and not force_fake:
        # 真实后端不可用 → 自动降级假后端：启动即弹模态说明 + 顶部持久横幅，
        # 避免用户误以为正在监听真实微信（B8）。
        note = fallback_reason or "真实后端不可用"
        window.set_demo_notice(note)
        try:
            from tkinter import messagebox

            root.after(400, lambda: messagebox.showwarning(
                "当前是演示模式（非真实微信）",
                "未检测到可用的真实微信后端，本窗口显示的是示例数据。\n\n"
                "真实微信收听请使用 802 控制台：http://127.0.0.1:802/\n\n"
                "若只想预览界面，可显式使用：python app.py --fake",
                parent=root,
            ))
        except Exception:  # noqa: BLE001 - 弹窗不可用时顶部横幅仍然生效
            pass
    elif is_fake:
        window.set_demo_notice("已用 --fake 显式演示")

    try:
        backend.start()
    except Exception as exc:  # noqa: BLE001 - 后端启动失败也不影响看界面
        message = f"后端启动失败：{exc}"
        window.log("error", message)
        window.show_error(message)

    if is_fake:
        # 演示模式：好友同步完成后自动勾选前 3 位并开始监听
        root.after(700, window.auto_demo_start)

    if args.screenshot:
        delay_ms = max(0, int(args.screenshot_delay * 1000))
        root.after(delay_ms, lambda: capture_window(root, window, args.screenshot))

    if args.duration and args.duration > 0:
        root.after(int(args.duration * 1000), window.close)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        window.close()
    finally:
        try:
            backend.stop()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
