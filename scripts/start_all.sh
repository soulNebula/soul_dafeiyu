#!/usr/bin/env bash
# IDEA 启动链路（mac/Linux 预留版；Windows 请用 start_all.bat）
set -u
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
SOUL="${SOULAGENT_HOME:-$HOME/Desktop/SoulAgent}"
PY="${IDEA_PYTHON:-python3}"
WB_PY="$ROOT/.venv/bin/python"
[ -x "$WB_PY" ] || WB_PY="$PY"
LOG_DIR="${AGENT_HOME:-$HOME/agent}/logs"
mkdir -p "$LOG_DIR"
ONLY="${1:-}"
if [ "$ONLY" = "--only" ]; then ONLY="${2:-}"; fi

port_up() { (echo >/dev/tcp/127.0.0.1/"$1") >/dev/null 2>&1; }
wait_port() { "$PY" "$ROOT/scripts/wait_port.py" "$1" 30; }
want() { [ -z "$ONLY" ] || printf '%s' "$ONLY" | grep -qi "$1"; }

start_one() {
  name="$1"; port="$2"; cwd="$3"; shift 3
  if port_up "$port"; then echo "[$port] 已在运行，跳过 $name"; return 0; fi
  echo "[$port] 启动 $name ..."
  ( cd "$cwd" && nohup "$@" >"$LOG_DIR/${name}_${port}.log" 2>&1 & )
  if wait_port "$port"; then echo "[$port] 就绪"; else
    echo "[$port] 启动失败，日志：$LOG_DIR/${name}_${port}.log"; return 1
  fi
}

rc=0
want gateway && { start_one gateway 801 "$SOUL" "$PY" -m agi.wechat_gateway.server || rc=1; }
want console && { start_one console 802 "$SOUL" "$PY" tools/console/server.py --port 802 || rc=1; }
want workbench && { start_one workbench 803 "$ROOT" "$WB_PY" -m uvicorn app.main:app --host 127.0.0.1 --port 803 || rc=1; }
if want models; then
  LLAMA="${LLAMA_SERVER:-$HOME/agent/llama/cuda/llama-server}"
  GGUF="${SOUL_GGUF:-$HOME/agent/output/gguf/soul_qwen25_05b_v1b-q4_k_m.gguf}"
  if [ -x "$LLAMA" ]; then
    start_one soul 8123 "$LOG_DIR" "$LLAMA" -m "$GGUF" --port 8123 --host 127.0.0.1 -ngl 99 -c 4096 || rc=1
  else
    echo "[8123] 缺少 llama-server：$LLAMA"
  fi
  start_one local3b 8125 "$SOUL" "$PY" tools/soul_serve/serve_local3b.py || rc=1
fi
echo "启动流程完成；smoke：$PY $ROOT/scripts/smoke_all.py"
exit "$rc"
