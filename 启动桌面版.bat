@echo off
chcp 936 >nul
setlocal EnableExtensions
cd /d "%~dp0"
title wx_box 桌面版 - 网关 801 + 控制台 802 + Edge app 窗口

rem ============================================================
rem  P7-a 零依赖桌面启动器：双击本文件即可
rem    1) 确保 801 微信网关 / 802 控制台在跑（没跑就后台起，日志写
rem       E:\agent\logs\desktop_gateway.log / desktop_console.log）
rem    2) 用 Edge（或 Chrome）的 --app 模式把 802 打开成独立窗口
rem  参数：--no-open / --exit-with-window / --stop / --status / --start-timeout N
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

"%PY%" tools\desktop\launcher.py %*
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
    echo 按任意键关闭本黑框，不会停服务。
    pause >nul
)
endlocal
exit /b 0
