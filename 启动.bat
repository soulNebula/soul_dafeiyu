@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
set "ROOT=%CD%"
if "%AGENT_HOME%"=="" set "AGENT_HOME=E:\agent"
set "MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple"
set "CHECK=0"
if /i "%~1"=="-CheckOnly" set "CHECK=1"
set "LOG=%AGENT_HOME%\logs\workbench_boot.log"

echo ============================================================
echo   小鱼 SoulAgent 一键启动
echo   Repo: %ROOT%
echo   AGENT_HOME: %AGENT_HOME%
echo ============================================================

call :find_python
if not defined PY (
    echo [环境] 没有找到 Python 3.12+，尝试自动安装...
    call :install_python
)
if not defined PY (
    echo [错误] Python 安装失败/未找到，请手动安装 Python 3.12+ 后重试。
    goto :fail
)
echo [环境] Python：%PY%

set "MAIN_PY=%ROOT%\.venv\Scripts\python.exe"
set "IDEA_PY=%ROOT%\idea\.venv\Scripts\python.exe"
set "SENDER_PY=%AGENT_HOME%\venv_wx312\Scripts\python.exe"

set "NEED=0"
if not exist "%MAIN_PY%" set "NEED=1"
if not exist "%IDEA_PY%" set "NEED=1"
if not exist "%AGENT_HOME%" set "NEED=1"
if exist "%MAIN_PY%" (
    "%MAIN_PY%" -c "import PIL, psutil, numpy" >nul 2>&1
    if errorlevel 1 set "NEED=1"
)
if exist "%IDEA_PY%" (
    "%IDEA_PY%" -c "import fastapi, uvicorn" >nul 2>&1
    if errorlevel 1 set "NEED=1"
)

if "%CHECK%"=="1" (
    echo [检查] 主 venv：%MAIN_PY%
    echo [检查] 803 venv：%IDEA_PY%
    echo [检查] 发送器 venv：%SENDER_PY%
    call :port_803
    if errorlevel 1 (echo [检查] 803：未运行) else (echo [检查] 803：运行中)
    if "%NEED%"=="1" (echo [检查] 需要自动配置：是) else (echo [检查] 需要自动配置：否)
    exit /b 0
)

if "%NEED%"=="1" (
    echo [配置] 缺少运行环境，开始安装依赖...
    call :install_deps
    if errorlevel 1 goto :fail
) else (
    echo [配置] 环境已存在，跳过安装
)

if not exist "%AGENT_HOME%\logs" mkdir "%AGENT_HOME%\logs" >nul 2>&1

echo [启动] 启动 Web 服务（803 + 802）...
"%MAIN_PY%" "%ROOT%\tools\start_web.py" --root "%ROOT%" --agent-home "%AGENT_HOME%"
if errorlevel 1 (
    echo [错误] Web 服务启动失败，看上方日志路径。
    goto :fail
)
start "" "http://127.0.0.1:803/"
exit /b 0

rem ============================================================
:find_python
set "PY="
if exist "C:\Python314\python.exe" set "PY=C:\Python314\python.exe"
if not defined PY if exist "C:\Python312\python.exe" set "PY=C:\Python312\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PY (
    "%PY%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
    if errorlevel 1 set "PY="
)
if not defined PY (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        "%%P" -c "import sys; raise SystemExit(0 if sys.version_info >= (3,12) else 1)" >nul 2>&1
        if not errorlevel 1 set "PY=%%P"
    )
)
if not defined PY (
    for /f "delims=" %%P in ('py -3.12 -c "import sys; print(sys.executable)" 2^>nul') do set "PY=%%P"
)
exit /b 0

:install_python
where winget >nul 2>&1
if not errorlevel 1 (
    echo [安装] 使用 winget 安装 Python 3.12...
    winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    call :find_python
    if defined PY exit /b 0
)
where curl.exe >nul 2>&1
if errorlevel 1 exit /b 0
set "INST=%TEMP%\python-3.12.10-amd64.exe"
echo [安装] 从国内镜像下载 Python...
curl.exe -L -o "%INST%" "https://mirrors.huaweicloud.com/python/3.12.10/python-3.12.10-amd64.exe"
if not exist "%INST%" exit /b 0
echo [安装] 静默安装（当前用户）...
start /wait "" "%INST%" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_launcher=1
call :find_python
exit /b 0

:install_deps
if not exist "%AGENT_HOME%" mkdir "%AGENT_HOME%" >nul 2>&1
for %%D in (models datasets output logs) do (
    if not exist "%AGENT_HOME%\%%D" mkdir "%AGENT_HOME%\%%D" >nul 2>&1
)
if not exist "%MAIN_PY%" (
    echo [安装] 创建主 venv...
    "%PY%" -m venv "%ROOT%\.venv"
    if errorlevel 1 exit /b 1
)
echo [安装] 主依赖（国内源：%MIRROR%）...
"%MAIN_PY%" -m pip install --upgrade pip -i "%MIRROR%" >nul
"%MAIN_PY%" -m pip install -r "%ROOT%\requirements.txt" -i "%MIRROR%"
if errorlevel 1 exit /b 1

if not exist "%IDEA_PY%" (
    echo [安装] 创建 803 venv...
    "%PY%" -m venv "%ROOT%\idea\.venv"
    if errorlevel 1 exit /b 1
)
echo [安装] 803 依赖...
"%IDEA_PY%" -m pip install --upgrade pip -i "%MIRROR%" >nul
"%IDEA_PY%" -m pip install -r "%ROOT%\idea\requirements.txt" -i "%MIRROR%"
if errorlevel 1 exit /b 1

if not exist "%SENDER_PY%" (
    echo [安装] 创建发送器 venv...
    "%PY%" -m venv "%AGENT_HOME%\venv_wx312"
)
echo [安装] 微信发送器 wechatauto（失败不阻塞主链）...
"%SENDER_PY%" -m pip install wechatauto -i "%MIRROR%"
if errorlevel 1 (
    echo [提示] wechatauto 安装失败：802 可以收消息，但发消息会不可用。
)
exit /b 0

:port_802
netstat -ano | findstr /C:"127.0.0.1:802" | findstr "LISTENING" >nul 2>&1
exit /b %errorlevel%

:wait_802
where curl.exe >nul 2>&1
if errorlevel 1 (
    timeout /t 5 /nobreak >nul
    exit /b 0
)
for /l %%I in (1,1,30) do (
    curl.exe -s -o nul --max-time 2 http://127.0.0.1:802/api/health
    if not errorlevel 1 exit /b 0
    timeout /t 1 /nobreak >nul
)
exit /b 1

:port_803
netstat -ano | findstr /C:"127.0.0.1:803" | findstr "LISTENING" >nul 2>&1
exit /b %errorlevel%

:wait_803
where curl.exe >nul 2>&1
if errorlevel 1 (
    timeout /t 8 /nobreak >nul
    exit /b 0
)
for /l %%I in (1,1,40) do (
    curl.exe -s -o nul --max-time 2 http://127.0.0.1:803/api/wb/health
    if not errorlevel 1 exit /b 0
    timeout /t 1 /nobreak >nul
)
exit /b 1

:fail
echo.
echo [失败] 请把上面的报错发给我，或手动执行 setup.ps1。
pause
exit /b 1
