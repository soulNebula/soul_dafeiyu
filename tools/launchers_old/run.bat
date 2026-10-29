@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title wx_box - 微信好友消息监听器（只读，不发送消息）

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
rem  2. 检查依赖，缺了自动安装（Pillow / psutil / numpy / RapidOCR / pycryptodome / imageio-ffmpeg）
rem     用 importlib.util.find_spec 只查"包是否存在"，不真正加载，启动快。
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
rem  3. 启动主程序（关闭窗口即退出）
rem ============================================================
echo.
echo [提示] 正在启动 wx_box ... 关闭程序窗口即可退出。
echo        只读工具，不会发送任何消息；微信窗口请勿最小化。
echo.
"%PY%" app.py
if errorlevel 1 (
    echo.
    echo [错误] 程序异常退出。请查看上面的报错信息，或参考 docs\常见问题.md
    echo.
    pause
)
endlocal
