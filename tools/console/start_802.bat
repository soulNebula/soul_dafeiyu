@echo off
setlocal
if "%AGENT_HOME%"=="" set "AGENT_HOME=E:gent"
cd /d "%~dp0..\.."
if not exist "%AGENT_HOME%\logs" mkdir "%AGENT_HOME%\logs" >nul 2>&1
".venv\Scripts\python.exe" "tools\console\server.py" --port 802 >> "%AGENT_HOME%\logs\console_boot.log" 2>&1
