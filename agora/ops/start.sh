#!/usr/bin/env bash
# AGORA trading session startup script.
# Invoked by launchd Mon–Fri at 6:45 AM CT (= 7:45 AM ET, 15 min before pre-market scan).

set -euo pipefail

REPO="/Users/rahul/Workspace/options_trading_agent_agentic"
VENV="$REPO/.venv/bin"
LOG_DIR="$REPO/agora/logs"
LOG="$LOG_DIR/agora.log"
PID_FILE="$LOG_DIR/agora.pid"

# ── Ensure required directories exist ────────────────────────────────────────
mkdir -p "$LOG_DIR"
mkdir -p "$REPO/.agora"

# ── Kill any stale instance ────────────────────────────────────────────────────
if [[ -f "$PID_FILE" ]]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[start.sh] Stopping stale PID $OLD_PID" >> "$LOG"
    kill "$OLD_PID" && sleep 2
  fi
  rm -f "$PID_FILE"
fi

# Belt-and-suspenders: kill any other uvicorn for this app
pkill -f "uvicorn agora.api.app" 2>/dev/null || true
sleep 1

# ── Log rotation: keep last 7 days ────────────────────────────────────────────
find "$LOG_DIR" -name "agora-*.log" -mtime +7 -delete 2>/dev/null || true
DATED_LOG="$LOG_DIR/agora-$(date +%Y%m%d).log"
ln -sf "$DATED_LOG" "$LOG"   # symlink agora.log → today's dated log

# ── Start uvicorn ──────────────────────────────────────────────────────────────
cd "$REPO"
# Load .env so subprocesses inherit API keys etc.
set -a; source "$REPO/.env"; set +a

"$VENV/uvicorn" agora.api.app:app \
  --host 0.0.0.0 --port 8001 \
  --workers 1 \
  >> "$DATED_LOG" 2>&1 &

echo $! > "$PID_FILE"
echo "[start.sh] AGORA started — PID $(cat $PID_FILE) — $(date)" >> "$DATED_LOG"
