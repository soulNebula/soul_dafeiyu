@echo off
chcp 936 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title wx_box 桌面版 - 网关 801 + 控制台 802 + Edge app 窗口 / 托盘

rem ============================================================
rem  零依赖桌面启动器（P7-a + P7-d）：双击本文件即可
rem    1) 确保 801 微信网关 / 802 控制台在跑（没跑就后台起，日志写
rem       E:\agent\logs\desktop_gateway.log / desktop_console.log）
rem    2) 用 Edge（或 Chrome）的 --app 模式把 802 打开成独立窗口
rem  常用参数（可双击后在命令行里传，或写到快捷方式目标里）：
rem    --tray               同时拉起系统托盘（打开控制台/主动消息/STOP/状态/日志/退出）
rem    --tray --no-notify   托盘但不弹桌面通知
rem    --no-open            只起服务（+托盘），不开窗口
rem    --exit-with-window   关窗口后停掉本启动器拉起的服务
rem    --stop               停掉本启动器拉起的服务/窗口/托盘
rem    --status             打印 JSON 状态
rem    --autostart install  安装开机自启（用户级，无需管理员）
rem ============================================================

rem 优先仓库约定的 C:\Python314\python.exe，其次 PATH 里的 python
set "PY=C:\Python314\python.exe"
if not exist "%PY%" (
    where python >nul 2>nul
    if errorlevel 1 (
        echo.
        echo [错误] 没有找到 Python，请安装 Python 3.10+ 并勾选 Add to PATH，
        echo        或创建 C:\Python314\python.exe。
        echo.
        pause
        exit /b 1
    )
    set "PY=python"
)

set "EXE=%~dp0tools\desktop\dist\SoulAgentDesktop\SoulAgentDesktop.exe"
if exist "%EXE%" (
    "%EXE%" %*
    set "RC=%ERRORLEVEL%"
) else (
    "%PY%" tools\desktop\launcher.py %*
    set "RC=%ERRORLEVEL%"
)
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo [错误] 桌面启动器退出码 %RC%，详情见上方输出与 E:\agent\logs\desktop_*.log
    echo        常见原因：801/802 端口被别的程序占用，或首次启动 60s 内没就绪。
    echo.
    pause
    endlocal
    exit /b %RC%
)

rem 双击启动（没有额外参数）时停住，方便看输出；带参数调用则直接退出
if "%~1"=="" (
    echo.
    echo 桌面窗口已打开；关掉窗口后服务默认保留，想一起收尾请用 --exit-with-window。
    echo 想要托盘/通知：再次双击时用「--tray」（或把快捷方式目标改成 --tray）。
    echo 按任意键关闭本黑框，不会停服务。
    pause >nul
)
endlocal
exit /b 0
