@echo off
setlocal
cd /d "%~dp0.."
set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [错误] 未找到 venv：%PY%
  echo 先执行：C:\Python314\python.exe -m venv .venv ^&^& .venv\Scripts\python.exe -m pip install -r requirements.txt
  pause
  exit /b 1
)
echo IDEA Workbench: http://127.0.0.1:803
echo 首次启动会生成随机 admin token：见启动日志里的带 token URL，或 idea\config.json 的 admin_token；也可用 IDEA_TOKEN 覆盖
"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 803
