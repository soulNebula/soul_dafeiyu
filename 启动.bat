@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo [错误] 未找到 Python。请安装 Python 3.12+ 并勾选 "Add python.exe to PATH"。
  echo 下载：https://www.python.org/downloads/windows/
  pause
  exit /b 1
)

python scripts\bootstrap.py %*
set RC=%ERRORLEVEL%
if not "%RC%"=="0" (
  echo.
  echo 启动未完成（退出码 %RC%），请查看 logs\bootstrap.log
)
pause
exit /b %RC%
