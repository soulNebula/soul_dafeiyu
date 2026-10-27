@echo off
chcp 65001 >nul
setlocal EnableExtensions
cd /d "%~dp0.."
set "PY=%IDEA_PYTHON%"
if "%PY%"=="" set "PY=C:\Python314\python.exe"
set "KILLARGS="
if /I "%~1"=="--dry-run" set "KILLARGS=--dry-run"

echo ============================================================
echo IDEA 停止链路：只杀 801 / 802 / 803 / 8123 / 8125 的监听 PID
echo ============================================================
for %%P in (801 802 803 8123 8125) do (
  "%PY%" "%~dp0port_pid.py" %%P --kill %KILLARGS%
)
echo ============================================================
echo 停止完成。可用 start_all.bat 重新启动。
echo ============================================================
endlocal
exit /b 0
