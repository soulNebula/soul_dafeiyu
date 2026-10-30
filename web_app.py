#!/usr/bin/env python3
"""wx_box 网页端入口（默认 http://127.0.0.1:8081）。

启动顺序：
1. 读取 ``config.json``（与桌面版共用）；
2. 加载后端：真实后端失败时自动回退 ``ui.dev_backend.DevBackend``（``--fake`` 强制演示）；
3. 创建 ``WebBridge``（独占消费后端 events 队列）与 DeepSeek 分析服务；
4. 启动标准库 HTTP 服务：静态页面 + REST API + SSE 实时事件；
5. 打开浏览器（``--no-browser`` 可关），Ctrl+C 退出并释放后台线程。

常用命令::

    C:\\Python314\\python.exe web_app.py                 # 真实微信 + 朋友圈
    C:\\Python314\\python.exe web_app.py --fake          # 演示模式，无需微信
    C:\\Python314\\python.exe web_app.py --port 8082 --no-browser
"""
from __future__ import annotations

import argparse
import secrets
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import load_backend, load_config  # noqa: E402  （复用桌面版的后端加载逻辑）
from webui import __version__  # noqa: E402
from webui.bridge import WebBridge  # noqa: E402
from webui.contacts import ContactsService  # noqa: E402
from webui.groups import GroupsService  # noqa: E402
from webui.history import HistoryService  # noqa: E402
from webui.llm import LLMConfigStore, LLMService  # noqa: E402
from webui.replay import ReplayBackend  # noqa: E402
from webui.server import WebContext, create_server  # noqa: E402
from webui.videos import VideoService  # noqa: E402
from webui.video_auto import ChatVideoAutoCache  # noqa: E402

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8081


def build_arg_parser() -> argparse.ArgumentParser:
    """构造网页端命令行参数。"""
    parser = argparse.ArgumentParser(
        prog="web_app.py",
        description=f"wx_box 网页端 v{__version__}：本地 Web 界面（只读监听微信消息，不发送消息）。",
    )
    parser.add_argument("--fake", action="store_true",
                        help="使用假后端演示（无需微信，每 2 秒一条示例消息）")
    parser.add_argument("--interval", type=float, default=None,
                        help="假后端生成消息的间隔秒数（默认 2 秒，仅 --fake 生效）")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"监听地址（默认 {DEFAULT_HOST}；仅本机使用，不要改成 0.0.0.0）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"监听端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--no-moments", action="store_true", help="关闭朋友圈模块（排障用）")
    parser.add_argument("--follow-log", action="store_true",
                        help="只读跟随模式：不启动采集，只读取并 tail 桌面版写入的 log/messages.jsonl"
                             "（当桌面版/另一个程序正在监听微信时使用，避免抢占）")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="运行 N 秒后自动退出（0 表示一直运行，自动化测试用）")
    return parser


def _make_logger(prefix: str = "[web]"):
    """返回带时间前缀的线程安全控制台日志函数。"""

    def log(text: str, level: str = "info") -> None:
        try:
            stamp = time.strftime("%H:%M:%S")
            print(f"{stamp} {prefix} {level.upper():5s} {text}", flush=True)
        except Exception:  # noqa: BLE001 - 控制台编码异常不影响服务
            pass

    return log


def _build_moments(bridge: WebBridge, log, disabled: bool):
    """创建朋友圈服务；任何异常都降级为 None（不影响聊天端）。"""
    if disabled:
        return None
    try:
        from webui.moments import MomentsService

        service = MomentsService(ROOT, bridge=bridge, logger=log)
        return service
    except Exception as exc:  # noqa: BLE001 - 朋友圈不可用不应阻止网页启动
        log(f"朋友圈模块不可用：{exc}", "warn")
        return None


def _open_browser_later(url: str, delay: float = 0.8) -> None:
    """延迟打开浏览器，避免服务端口尚未开始 accept。"""

    def worker() -> None:
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - 打不开浏览器不影响手动访问
            pass

    threading.Thread(target=worker, name="wxbox-open-browser", daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    """程序入口：解析参数、搭建服务并阻塞在 HTTP 循环。"""
    args = build_arg_parser().parse_args(argv)
    log = _make_logger()

    config = load_config()
    if args.follow_log:
        # 只读跟随：另一个监听实例（桌面版）在跑时使用，绝不启动第二份采集
        backend = ReplayBackend(config)
        fallback_reason = "只读跟随模式：不启动采集，只同步桌面版写入的消息日志。"
        is_fake = False
        mode = "follow"
    else:
        force_fake = bool(args.fake)
        backend, fallback_reason, is_fake = load_backend(
            config, force_fake=force_fake, fake_interval=args.interval
        )
        mode = "fake" if is_fake else "real"
        if is_fake and fallback_reason:
            log(fallback_reason, "warn")
        if not is_fake:
            # 网页端只读数据库、不自动点击/切换微信窗口：直接在实例上把 _switch_to
            # 变成 no-op（不要改 config 里的 auto_switch，避免把安全开关写进共享
            # config.json 影响桌面版/Agent A 的窗口行为）。
            try:
                backend._switch_to = lambda *_a, **_k: False  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            log("实时模式：直接读微信数据库（网页端不自动切换/点击微信窗口）")

    bridge = WebBridge(backend, mode=mode)
    moments = _build_moments(bridge, log, args.no_moments)
    llm = LLMService(LLMConfigStore(ROOT / "web_config.json"), bridge, moments, logger=log)
    history = HistoryService(config, log=lambda text: log(text, "info"))
    groups = GroupsService(config, log=lambda text: log(text, "info"))
    contacts = ContactsService(config, log=lambda text: log(text, "info"))
    videos = VideoService(config, log=lambda text: log(text, "info"))
    video_auto = ChatVideoAutoCache(
        bridge, videos, log=lambda text: log(text, "info")
    )
    token = secrets.token_hex(24)
    context = WebContext(
        bridge=bridge,
        llm=llm,
        token=token,
        host=args.host,
        port=args.port,
        mode=mode,
        version=__version__,
        backend_note=fallback_reason,
        moments=moments,
        history=history,
        groups=groups,
        contacts=contacts,
        videos=videos,
        video_auto=video_auto,
    )

    try:
        server = create_server(context, args.host, args.port)
    except OSError as exc:
        log(f"无法监听 {args.host}:{args.port}：{exc}", "error")
        log("端口可能已被占用：请关闭占用程序，或使用 --port 8081 换一个端口。", "error")
        return 1

    bridge.start()
    try:
        backend.start()
    except Exception as exc:  # noqa: BLE001 - 后端启动失败仍可打开页面看提示
        log(f"后端启动失败：{exc}", "error")
    if moments is not None:
        try:
            moments.start()
            # 自动监听朋友圈只在真实模式、且「开始监听」后运行；每 30 分钟刷新一次微信朋友圈
            if mode == "real":
                moments.set_auto(True, 30)
                moments.set_listening(bool(backend.is_running()))
        except Exception as exc:  # noqa: BLE001
            log(f"朋友圈服务启动失败：{exc}", "warn")

    url = f"http://{args.host}:{args.port}/"
    mode_label = {"fake": "演示（假后端）", "follow": "只读跟随（桌面版日志）"}.get(mode, "真实微信")
    print("", flush=True)
    print("!" * 66, flush=True)
    print("  [已弃用] 8081 网页端：功能已停止维护，仅保留历史排障用途。", flush=True)
    print("  日常入口请用 802 控制台：http://127.0.0.1:802/", flush=True)
    print("  朋友圈 Tab 依赖外部 WechatMoments/，缺失时会显示「未安装（该功能不再维护）」。", flush=True)
    print("!" * 66, flush=True)
    print("=" * 66, flush=True)
    print("  wx_box 网页端已启动（legacy / 已弃用）", flush=True)
    print(f"  地址：{url}", flush=True)
    print(f"  模式：{mode_label}   版本：v{__version__}", flush=True)
    print("  提示：页面仅监听本机；只读工具，不会发送任何消息。", flush=True)
    print("  按 Ctrl+C 退出。", flush=True)
    print("=" * 66, flush=True)
    print("", flush=True)

    if not args.no_browser:
        _open_browser_later(url)

    if args.duration and args.duration > 0:
        def shutdown_later() -> None:
            time.sleep(max(0.2, args.duration))
            log(f"已运行 {args.duration:g} 秒，自动退出。")
            server.shutdown()

        threading.Thread(target=shutdown_later, name="wxbox-auto-shutdown", daemon=True).start()

    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        log("收到 Ctrl+C，正在退出…")
    finally:
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            server.server_close()
        except Exception:  # noqa: BLE001
            pass
        if moments is not None:
            try:
                moments.stop()
            except Exception:  # noqa: BLE001
                pass
        for service in (history, groups, contacts):
            try:
                service.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            backend.stop()
        except Exception:  # noqa: BLE001
            pass
        bridge.stop()
        log("网页端已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
