@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [重启] 停止 801/802/803...
for %%P in (801 802 803) do (
    for /f "tokens=5" %%I in ('netstat -ano ^| findstr ":%%P " ^| findstr "LISTENING"') do (
        taskkill /F /PID %%I >nul 2>&1
    )
)
timeout /t 2 /nobreak >nul
echo [重启] 以当前用户权限重新启动 Web 端...
call "%~dp0..\..\启动.bat"
