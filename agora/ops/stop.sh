#!/usr/bin/env bash
# AGORA trading session shutdown script.
# Invoked by launchd Mon–Fri at 4:30 PM CT (= 5:30 PM ET, after after-hours report).

REPO="/Users/rahul/Workspace/options_trading_agent_agentic"
LOG_DIR="$REPO/agora/logs"
LOG="$LOG_DIR/agora.log"
PID_FILE="$LOG_DIR/agora.pid"

if [[ -f "$PID_FILE" ]]; then
  PID=$(cat "$PID_FILE")
  if kill -0 "$PID" 2>/dev/null; then
    echo "[stop.sh] Graceful shutdown — PID $PID — $(date)" >> "$LOG"
    kill -TERM "$PID"
    sleep 5
    kill -0 "$PID" 2>/dev/null && kill -KILL "$PID" || true
  fi
  rm -f "$PID_FILE"
else
  pkill -f "uvicorn agora.api.app" 2>/dev/null || true
fi

echo "[stop.sh] AGORA stopped — $(date)" >> "$LOG"
