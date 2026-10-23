@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title wx_box 网页端 - 微信消息分析台（只读，不发送消息）

rem ============================================================
rem  1. 找到 Python（优先本机 C:\Python314\python.exe）
rem ============================================================
set "PY=C:\Python314\python.exe"
if not exist "%PY%" (
    where python >nul 2>nul
    if errorlevel 1 (
        echo.
        echo [错误] 没有找到 Python。
        echo        请安装 Python 3.10 或更高版本（本机预期路径：C:\Python314\python.exe），
        echo        或把 python 加入系统 PATH 后重新双击本文件。
        echo.
        pause
        exit /b 1
    )
    set "PY=python"
)

"%PY%" --version >nul 2>nul
if errorlevel 1 (
    echo.
    echo [错误] Python 无法运行：%PY%
    echo        请检查安装是否完整后重试。
    echo.
    pause
    exit /b 1
)

rem ============================================================
rem  2. 检查聊天采集依赖（网页端本身只用 Python 标准库）
rem ============================================================
"%PY%" -c "import importlib.util as u,sys; sys.exit(0 if u.find_spec('PIL') and u.find_spec('psutil') and u.find_spec('numpy') and u.find_spec('rapidocr_onnxruntime') and u.find_spec('Crypto') and u.find_spec('imageio_ffmpeg') else 1)" >nul 2>nul
if errorlevel 1 (
    echo [提示] 首次运行，正在安装依赖（Pillow / psutil / numpy / RapidOCR / pycryptodome / imageio-ffmpeg），请稍候 ...
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败。请手动执行下面命令后重试：
        echo        "%PY%" -m pip install -r requirements.txt
        echo        国内网络慢可加镜像：
        echo        "%PY%" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
        echo.
        pause
        exit /b 1
    )
)

rem ============================================================
rem  3. 朋友圈模块环境提示（缺失不影响聊天端）
rem ============================================================
if exist "WechatMoments\.venv\Scripts\python.exe" (
    echo [提示] 已找到朋友圈运行环境：WechatMoments\.venv
) else (
    echo [提示] 未找到 WechatMoments\.venv，网页端「朋友圈」页签会显示不可用。
    echo        如需朋友圈功能：先按 WechatMoments\README.md 建好 venv 并安装依赖。
)

rem ============================================================
rem  4. 启动网页端（默认 127.0.0.1:8081，浏览器自动打开）
rem     Ctrl+C 或关闭本窗口即退出
rem     参数会原样转给 web_app.py，例如：run_web.bat --fake --port 8082
rem     如果桌面版 app.py 正在运行，请改用只读跟随模式（不会抢占微信）：
rem         "%PY%" web_app.py --follow-log
rem ============================================================
echo.
echo [提示] 正在启动 wx_box 网页端：http://127.0.0.1:8081
echo        只读工具，不会发送任何消息；首次使用请在页面右上角「模型设置」填 DeepSeek API Key。
echo        如桌面版正在运行，请关掉桌面版后启动本页，或改用：web_app.py --follow-log
echo.
"%PY%" web_app.py %*
if errorlevel 1 (
    echo.
    echo [错误] 网页端异常退出。请查看上面的报错信息，或参考 docs\网页端使用说明.md
    echo        常见原因：8081 端口被占用（可执行 "%PY%" web_app.py --port 8082）
    echo.
    pause
)
endlocal
