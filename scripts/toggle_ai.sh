#!/usr/bin/env bash
# Kill the AI feasibility service (port 8001) on its own, leaving the web API (port 8000)
# and the robot untouched, so you can watch admission fall back to the deterministic model
# (NFR2) - then bring it back.
#
#   ./scripts/toggle_ai.sh            # kill it, wait for you to test, then restart it
#   ./scripts/toggle_ai.sh down       # just kill it
#   ./scripts/toggle_ai.sh up         # just (re)start it
#
# Matched the same way scripts/stop.sh matches it: by full command line, because the OS
# process name for both web services is just "python".
set -e
WS="$(cd "$(dirname "$0")/.." && pwd)"
PORT=8001
PY="$WS/robofetch_venv/bin/python"
LOG="$WS/logs/ai_service.log"
PID_FILE="/tmp/robofetch_ai_service.pid"

ai_pid() {
  ps -eo pid,args --no-headers | grep -E 'robofetch_ai\.service' | grep -v grep \
    | awk '{print $1}'
}

down() {
  local pid
  pid=$(ai_pid)
  if [ -z "$pid" ]; then
    echo "[toggle_ai] AI service is not running."
    return 0
  fi
  echo "[toggle_ai] stopping AI service (pid $pid, port $PORT) ..."
  kill -TERM "$pid" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    [ -z "$(ai_pid)" ] && break
    sleep 1
  done
  [ -n "$(ai_pid)" ] && kill -KILL "$(ai_pid)" 2>/dev/null || true
  if ss -lptn "sport = :$PORT" 2>/dev/null | tail -n +2 | grep -q .; then
    echo "[toggle_ai] WARNING: something is still bound to $PORT:"
    ss -lptn "sport = :$PORT" | tail -n +2
  else
    echo "[toggle_ai] AI service is down. Port $PORT is free."
  fi
}

up() {
  if [ -n "$(ai_pid)" ]; then
    echo "[toggle_ai] AI service is already running (pid $(ai_pid))."
    return 0
  fi
  mkdir -p "$WS/logs"
  echo "[toggle_ai] starting AI service on port $PORT ..."
  nohup "$PY" -m uvicorn robofetch_ai.service:app --host 0.0.0.0 --port "$PORT" \
    >>"$LOG" 2>&1 &
  echo $! >"$PID_FILE"
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    ss -lptn "sport = :$PORT" 2>/dev/null | tail -n +2 | grep -q . && break
    sleep 1
  done
  if ss -lptn "sport = :$PORT" 2>/dev/null | tail -n +2 | grep -q .; then
    echo "[toggle_ai] AI service is up (pid $(ai_pid), log: $LOG)."
  else
    echo "[toggle_ai] AI service did not come up - check $LOG."
    return 1
  fi
}

case "${1:-}" in
  down) down ;;
  up)   up ;;
  "")
    down
    echo
    echo "[toggle_ai] Now check the app still works, e.g.:"
    echo "    curl -s http://localhost:8000/health | python3 -m json.tool"
    echo "    ./scripts/order.sh SKU-1001 delivery_1"
    echo "  (look for \"ai_service\": {\"reachable\": false, ...} and decided_by: \"policy\")"
    echo
    read -r -p "[toggle_ai] press Enter to restart the AI service ... " _
    up
    ;;
  *)
    echo "usage: $0 [down|up]" >&2
    exit 1
    ;;
esac
