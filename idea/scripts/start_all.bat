@echo off
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0.."
set "ROOT=%CD%"
set "SOUL=%SOULAGENT_HOME%"
if "%SOUL%"=="" set "SOUL=%USERPROFILE%\Desktop\SoulAgent"
set "PY=%IDEA_PYTHON%"
if "%PY%"=="" set "PY=C:\Python314\python.exe"
set "WB_PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%WB_PY%" set "WB_PY=%PY%"
set "LOGS=%AGENT_HOME%"
if "%LOGS%"=="" set "LOGS=E:\agent"
set "LOGDIR=%LOGS%\logs"
set "LLAMA=%LLAMA_SERVER%"
if "%LLAMA%"=="" set "LLAMA=E:\agent\llama\cuda\llama-server.exe"
set "SOUL_GGUF=%SOUL_GGUF%"
if "%SOUL_GGUF%"=="" set "SOUL_GGUF=E:\agent\output\gguf\soul_qwen25_05b_v1b-q4_k_m.gguf"
set "ONLY=%*"
set "ONLY=%ONLY:--only =%"
set "ONLY=%ONLY:,= %"

echo ============================================================
echo IDEA 启动链路  工作台 %ROOT%
echo SoulAgent %SOUL%
echo 日志根 %LOGDIR%
echo 仅启动：%ONLY%（空=全部）
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
echo ============================================================

if "%ONLY%"=="" goto all
echo %ONLY% | findstr /I /C:"gateway" >nul && call :gateway
echo %ONLY% | findstr /I /C:"console" >nul && call :console
echo %ONLY% | findstr /I /C:"workbench" >nul && call :workbench
echo %ONLY% | findstr /I /C:"models" >nul && (call :model_8123 & call :model_8125)
goto done
:all
call :gateway
call :console
call :workbench
call :model_8123
call :model_8125
goto done

:port_up
rem %1=port -> errorlevel 0 if listening（Python 探测，避免 netstat 格式差异）
"%PY%" "%~dp0port_pid.py" %~1 --check >nul 2>&1
exit /b %errorlevel%

:wait_port
rem %1=port; errorlevel 0 when ready (uses scripts/wait_port.py)
"%PY%" "%~dp0wait_port.py" %~1 30
exit /b %errorlevel%

:gateway
call :port_up 801 && (echo [801] 已在运行，跳过 gateway & exit /b 0)
echo [801] 启动 gateway...
start "gateway" /B /D "%SOUL%" "%PY%" -m agi.wechat_gateway.server > "%LOGDIR%\gateway_run.log" 2>&1
call :wait_port 801
if errorlevel 1 (echo [801] 启动失败，日志：%LOGDIR%\gateway_run.log & exit /b 1)
echo [801] 就绪 http://127.0.0.1:801
exit /b 0

:console
call :port_up 802 && (echo [802] 已在运行，跳过 console & exit /b 0)
echo [802] 启动 console...
start "console" /B /D "%SOUL%" "%PY%" tools/console/server.py --port 802 > "%LOGDIR%\console_802.log" 2>&1
call :wait_port 802
if errorlevel 1 (echo [802] 启动失败，日志：%LOGDIR%\console_802.log & exit /b 1)
echo [802] 就绪 http://127.0.0.1:802
exit /b 0

:workbench
call :port_up 803 && (echo [803] 已在运行，跳过 workbench & exit /b 0)
echo [803] 启动 workbench...
start "workbench" /B /D "%ROOT%" "%WB_PY%" -m uvicorn app.main:app --host 127.0.0.1 --port 803 > "%LOGDIR%\workbench_803.log" 2>&1
call :wait_port 803
if errorlevel 1 (echo [803] 启动失败，日志：%LOGDIR%\workbench_803.log & exit /b 1)
echo [803] 就绪 http://127.0.0.1:803
exit /b 0

:model_8123
call :port_up 8123 && (echo [8123] 已在运行，跳过 soul & exit /b 0)
if not exist "%LLAMA%" (echo [8123] 缺少 llama-server：%LLAMA% & exit /b 1)
echo [8123] 启动 llama-server...
start "soul8123" /B /D "%LOGS%" "%LLAMA%" -m "%SOUL_GGUF%" --port 8123 --host 127.0.0.1 -ngl 99 -c 4096 > "%LOGDIR%\llama_8123.log" 2>&1
call :wait_port 8123
if errorlevel 1 (echo [8123] 启动失败，日志：%LOGDIR%\llama_8123.log & exit /b 1)
echo [8123] 就绪
exit /b 0

:model_8125
call :port_up 8125 && (echo [8125] 已在运行，跳过 local3b & exit /b 0)
echo [8125] 启动 local3b...
start "local3b" /B /D "%SOUL%" "%PY%" tools/soul_serve/serve_local3b.py > "%LOGDIR%\llama_8125.log" 2>&1
call :wait_port 8125
if errorlevel 1 (echo [8125] 启动失败，日志：%LOGDIR%\llama_8125.log & exit /b 1)
echo [8125] 就绪
exit /b 0

:done
echo ============================================================
echo 启动流程完成。运行 smoke：python scripts\smoke_all.py
echo ============================================================
endlocal
exit /b 0

