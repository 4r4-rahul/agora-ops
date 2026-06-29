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

# ── Log retention (2026-06-29): dated logs had NO retention and grew to 4.3 GB (one day = 2.1 GB).
# Self-clean on every start so it never recurs: gzip any past-day .log (≈10× smaller, audit trail
# preserved), then delete gzipped logs older than 14 days. Today's live file is left untouched.
# nice'd + best-effort (|| true) so log hygiene can never block the engine from starting.
find "$LOG_DIR" -name 'agora-*.log' -type f -mtime +0 -print0 2>/dev/null \
  | xargs -0 -I{} nice -n 19 gzip -f {} 2>/dev/null || true
find "$LOG_DIR" -name 'agora-*.log.gz' -type f -mtime +14 -delete 2>/dev/null || true

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

# ── Kill any stale instance — ORPHAN-PROOF, wait for port 8001 to actually free ────────────────
# uvicorn ignores SIGTERM during shutdown, so a TERM + 2s sleep used to start a new process OVER a
# still-running one → port-8001 collision → the old engine kept running its IBKR/lifecycle tasks
# (two engines on one account). Now: kill the port owner + every uvicorn-agora pid (TERM, then KILL
# survivors) and BLOCK until 8001 is free before starting. Never start over a live engine.
# NOTE: `set -euo pipefail` is on — every conditional below uses `if; then` (NOT `cond && action`,
# which exits the script under set -e when cond is false) and `|| true` on pipes that may find nothing.
engine_pids() {
  { lsof -nP -iTCP:8001 -sTCP:LISTEN -t 2>/dev/null || true
    pgrep -f "uvicorn agora.api.app" 2>/dev/null || true
    { [[ -f "$PID_FILE" ]] && cat "$PID_FILE" 2>/dev/null; } || true
  } | grep -E '^[0-9]+$' | sort -u || true
}
PIDS=$(engine_pids || true)
if [[ -n "$PIDS" ]]; then
  echo "[start.sh] stopping existing engine: $(echo "$PIDS" | tr '\n' ' ')" >> "$LOG"
  for p in $PIDS; do kill -TERM "$p" 2>/dev/null || true; done
  for _ in $(seq 1 8); do
    if [[ -z "$(engine_pids || true)" ]]; then break; fi
    sleep 1
  done
  for p in $(engine_pids || true); do kill -KILL "$p" 2>/dev/null || true; done
fi
rm -f "$PID_FILE"
# Block until port 8001 is free (max ~10s) so the new uvicorn binds cleanly instead of orphaning.
for _ in $(seq 1 10); do
  if [[ -z "$(lsof -nP -iTCP:8001 -sTCP:LISTEN -t 2>/dev/null || true)" ]]; then break; fi
  sleep 1
done

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
# DISASTER-PROOF: sourcing is NON-FATAL. Under `set -e`, a single malformed .env line aborted the
# whole startup and took the engine down (2026-06-26: a token pasted as `GITHUB_TOKEN= <token>` ran
# the token as a command → exit 127 → engine never launched). We lift errexit around the source so a
# bad line is ignored (every well-formed assignment before AND after it still loads) and the engine
# always starts. The secret-scan + a clean .env are the right place to catch malformed lines — not a
# dead engine.
set +e
set -a; source "$REPO/.env" 2>/dev/null; set +a
set -e

# ── Rotate the IBKR clientId block each restart (deploy-churn fix) ──────────────
# A hard-killed engine leaves its IBKR connection (clientId) held by TWS for ~30-60s, so a fresh
# process reusing the same id gets "Error 326: client id already in use" and burns ~30-60s + ~20
# error lines reconnecting on every restart. Rotating the block (offset cycles 0→20→40→60) means
# the new process never collides with the dying one. Spacing (≥20) keeps main/news/sync distinct
# within and across consecutive blocks. Set AFTER sourcing .env so it overrides the static value.
OFFSET_FILE="$LOG_DIR/.ibkr_clientid_offset"
_OFF=$(cat "$OFFSET_FILE" 2>/dev/null || echo 0)
_OFF=$(( (_OFF + 20) % 80 ))
echo "$_OFF" > "$OFFSET_FILE"
export IBKR_CLIENT_ID=$(( 2 + _OFF ))
export IBKR_NEWS_CLIENT_ID=$(( 4 + _OFF ))
export STARTUP_TWS_SYNC_CLIENT_ID=$(( 12 + _OFF ))
echo "[start.sh] IBKR clientId block offset=$_OFF (main=$IBKR_CLIENT_ID news=$IBKR_NEWS_CLIENT_ID sync=$STARTUP_TWS_SYNC_CLIENT_ID) $(date)" >> "$LOG_DIR/launchd-start.log"

# nohup → uvicorn ignores SIGHUP so it survives the parent (launchd OR a manual/ssh shell) exiting.
# (macOS has no `setsid`; nohup is the portable equivalent here.)
nohup "$VENV/uvicorn" agora.api.app:app \
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
