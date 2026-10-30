#!/usr/bin/env bash
# IDEA Workbench · macOS / Linux 启动脚本
set -e
cd "$(dirname "$0")/.."
PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "[错误] 未找到 venv：$PY"
  echo "先执行：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi
echo "IDEA Workbench: http://127.0.0.1:803"
echo "首次启动会生成随机 admin token：见启动日志里的带 token URL，或 idea/config.json 的 admin_token；也可用 IDEA_TOKEN 覆盖"
exec "$PY" -m uvicorn app.main:app --host 127.0.0.1 --port 803
