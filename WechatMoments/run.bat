@echo off
chcp 65001 >nul
cd /d %~dp0

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py
) else (
    echo [!] 未找到 .venv，使用系统 Python（建议先按 README 创建虚拟环境并安装依赖）
    python main.py
)

if errorlevel 1 pause
