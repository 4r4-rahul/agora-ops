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

# ── Single-instance guard (deploy-race fix) ──────────────────────────────────
# A manual deploy and the launchd watchdog can invoke start.sh concurrently; both
# `pkill -f "uvicorn agora.api.app"` then start, and interleaved pkills can leave
# NOTHING running (observed 2026-06-09 — engine down post-deploy). mkdir is atomic, so a
# second concurrent invocation skips the kill+start critical section entirely. The trap
# releases the lock when this script exits (uvicorn is already backgrounded by then).
LOCK_DIR="$LOG_DIR/start.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  LOCK_AGE=$(( $(date +%s) - $(stat -f %m "$LOCK_DIR" 2>/dev/null || echo 0) ))
  if (( LOCK_AGE < 120 )); then
    echo "[start.sh] another start.sh in progress (lock ${LOCK_AGE}s) — skipping $(date)" \
      >> "$LOG_DIR/launchd-start.log"
    exit 0
  fi
  # Lock older than 120s ⇒ a prior run died holding it; take over.
  echo "[start.sh] stale lock (${LOCK_AGE}s) — taking over $(date)" >> "$LOG_DIR/launchd-start.log"
  rm -rf "$LOCK_DIR"
  mkdir "$LOCK_DIR" 2>/dev/null || { echo "[start.sh] lock race lost — skipping" \
    >> "$LOG_DIR/launchd-start.log"; exit 0; }
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

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

# Raise the open-file limit. The engine opens many concurrent FDs — IBKR + yfinance
# sockets, per-agent sqlite handles, the IV cache — and the launchd default (~256) is
# exhausted mid-session => "Too many open files" (Errno 24), which silently breaks price
# fetching and the health endpoint (observed 2026-06-08). 10240 gives ample headroom.
ulimit -n 10240 2>/dev/null || ulimit -n 4096 2>/dev/null || true
echo "[start.sh] open-file limit (ulimit -n) = $(ulimit -n)" >> "$LOG_DIR/launchd-start.log"

# Load .env so subprocesses inherit API keys etc.
set -a; source "$REPO/.env"; set +a

"$VENV/uvicorn" agora.api.app:app \
  --host 0.0.0.0 --port 8001 \
  --workers 1 \
  >> "$DATED_LOG" 2>&1 &

AGORA_PID=$!
echo "$AGORA_PID" > "$PID_FILE"

# ── Keep the Mac awake while the engine runs ──────────────────────────────────
# A laptop on idle/battery sleeps and suspends this process mid-session (observed:
# died at 09:55 ET = idle sleep). caffeinate -is holds a no-idle / no-system-sleep
# assertion; -w ties it to the engine PID so it auto-releases when the engine exits
# (incl. the scheduled 4:30pm stop) — no orphaned assertion. NOTE: system sleep can
# only be blocked on AC power; keep the Mac plugged in during trading hours.
caffeinate -is -w "$AGORA_PID" >> "$DATED_LOG" 2>&1 &

echo "[start.sh] AGORA started — PID $AGORA_PID (caffeinated) — $(date)" >> "$DATED_LOG"
