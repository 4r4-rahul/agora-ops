#!/usr/bin/env zsh
# ══════════════════════════════════════════════════════════════
#  supervised_start.sh — Launch trading loop with auto-restart
# ══════════════════════════════════════════════════════════════
#
#  Features:
#    • Pre-flight checks (venv, IBKR, .env)
#    • PID file management
#    • Auto-restart on crash (up to MAX_RESTARTS per day)
#    • Rotating log files
#    • Discord webhook alert on crash/restart
#
#  Usage:
#    ./scripts/supervised_start.sh          # normal
#    ./scripts/supervised_start.sh --dry    # dry-run mode
#
# ══════════════════════════════════════════════════════════════

set -euo pipefail

# ── Configuration ────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="${PROJECT_DIR}/.venv/bin"
PYTHON="${VENV}/python"
LOG_DIR="${PROJECT_DIR}/data/logs"
PID_FILE="${PROJECT_DIR}/data/.trading.pid"
MAX_RESTARTS=5          # Max restarts per calendar day
RESTART_DELAY=30        # Seconds between restarts
RESTART_COUNT_FILE="${PROJECT_DIR}/data/.restart_count"
DRY_RUN=""

# Parse args
[[ "${1:-}" == "--dry" ]] && DRY_RUN="--dry-run"

# ── Helpers ──────────────────────────────────────────────────
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1"; }

send_webhook() {
    local msg="$1"
    if [ -f "${PROJECT_DIR}/.env" ]; then
        local url=$(grep ALERT_WEBHOOK_URL "${PROJECT_DIR}/.env" | cut -d= -f2-)
        if [ -n "$url" ]; then
            curl -s -H "Content-Type: application/json" \
                -d "{\"content\": \"🤖 **Trading Supervisor**: ${msg}\"}" \
                "$url" > /dev/null 2>&1 || true
        fi
    fi
}

cleanup() {
    log "🛑 Supervisor shutting down ..."
    if [ -f "$PID_FILE" ]; then
        local pid=$(cat "$PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            log "Stopping trading loop (PID $pid) ..."
            kill "$pid" 2>/dev/null || true
            sleep 3
            kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f "$PID_FILE"
    fi
    send_webhook "Supervisor stopped."
    exit 0
}

trap cleanup SIGINT SIGTERM

# ── Pre-flight Checks ───────────────────────────────────────
log "═══════════════════════════════════════════"
log "  0DTE Trading Supervisor"
log "═══════════════════════════════════════════"

# Check venv
if [ ! -f "$PYTHON" ]; then
    log "❌ Python venv not found at $VENV"
    log "   Run: python -m venv .venv && pip install -r requirements.txt"
    exit 1
fi
log "✅ Python: $($PYTHON --version 2>&1)"

# Check .env
if [ ! -f "${PROJECT_DIR}/.env" ]; then
    log "⚠️  No .env file found — using defaults"
else
    log "✅ .env loaded"
    source <(grep -v '^#' "${PROJECT_DIR}/.env" | grep '=' | sed 's/^/export /')
fi

# Check IBKR connectivity
IBKR_HOST="${IBKR_HOST:-127.0.0.1}"
IBKR_PORT="${IBKR_PORT:-7497}"
if nc -z "$IBKR_HOST" "$IBKR_PORT" 2>/dev/null; then
    log "✅ IBKR reachable at ${IBKR_HOST}:${IBKR_PORT}"
else
    log "❌ Cannot reach IBKR at ${IBKR_HOST}:${IBKR_PORT}"
    log "   Make sure TWS/IB Gateway is running with API enabled"
    exit 1
fi

# Check market hours (loose check — just a warning)
HOUR=$(date +%H)
DOW=$(date +%u)  # 1=Mon, 7=Sun
if [[ $DOW -ge 6 ]] || [[ $HOUR -lt 9 ]] || [[ $HOUR -ge 17 ]]; then
    log "⚠️  Outside US market hours — trading loop will wait for open"
fi

# Create log directory
mkdir -p "$LOG_DIR"

# ── Restart Counter ──────────────────────────────────────────
TODAY=$(date +%Y%m%d)
RESTARTS=0

if [ -f "$RESTART_COUNT_FILE" ]; then
    SAVED_DATE=$(head -1 "$RESTART_COUNT_FILE" 2>/dev/null || echo "")
    SAVED_COUNT=$(tail -1 "$RESTART_COUNT_FILE" 2>/dev/null || echo "0")
    if [ "$SAVED_DATE" = "$TODAY" ]; then
        RESTARTS=$SAVED_COUNT
    fi
fi

save_restart_count() {
    echo "$TODAY" > "$RESTART_COUNT_FILE"
    echo "$RESTARTS" >> "$RESTART_COUNT_FILE"
}

# ── Main Supervisor Loop ────────────────────────────────────
log "🚀 Starting supervised trading loop ..."
[ -n "$DRY_RUN" ] && log "   Mode: DRY RUN (no real orders)"

send_webhook "Supervisor started. Max restarts: ${MAX_RESTARTS}/day."

while true; do
    LOG_FILE="${LOG_DIR}/trading_$(date +%Y%m%d_%H%M%S).log"
    log "📝 Log file: $LOG_FILE"

    # Launch trading loop in background
    $PYTHON "${PROJECT_DIR}/run_live.py" --tickers SPY --no-confirm $DRY_RUN \
        > "$LOG_FILE" 2>&1 &
    TRADING_PID=$!
    echo "$TRADING_PID" > "$PID_FILE"
    log "✅ Trading loop started (PID $TRADING_PID)"

    # Wait for it to exit
    set +e
    wait $TRADING_PID
    EXIT_CODE=$?
    set -e

    rm -f "$PID_FILE"

    # Check if this was a clean shutdown (exit 0) or crash
    if [ $EXIT_CODE -eq 0 ]; then
        log "✅ Trading loop exited cleanly (code 0). Stopping supervisor."
        send_webhook "Trading loop exited cleanly. Supervisor stopping."
        break
    fi

    # Crash detected
    RESTARTS=$((RESTARTS + 1))
    save_restart_count
    log "💥 Trading loop crashed! Exit code: $EXIT_CODE (restart $RESTARTS/$MAX_RESTARTS)"
    send_webhook "⚠️ Trading loop crashed (exit $EXIT_CODE). Restart $RESTARTS/$MAX_RESTARTS."

    # Check restart budget
    if [ $RESTARTS -ge $MAX_RESTARTS ]; then
        log "❌ Max restarts ($MAX_RESTARTS) exceeded for today. Stopping."
        send_webhook "🚨 Max restarts exceeded ($MAX_RESTARTS). Manual intervention needed."
        exit 1
    fi

    # Brief cooldown before restart
    log "⏳ Waiting ${RESTART_DELAY}s before restart ..."
    sleep $RESTART_DELAY

    # Re-check IBKR before restarting
    if ! nc -z "$IBKR_HOST" "$IBKR_PORT" 2>/dev/null; then
        log "❌ IBKR not reachable — waiting for reconnection ..."
        send_webhook "IBKR unreachable. Waiting for reconnection ..."
        WAIT=0
        while ! nc -z "$IBKR_HOST" "$IBKR_PORT" 2>/dev/null; do
            sleep 10
            WAIT=$((WAIT + 10))
            if [ $WAIT -ge 300 ]; then
                log "❌ IBKR still unreachable after 5 minutes. Stopping."
                send_webhook "🚨 IBKR unreachable for 5 min. Stopping supervisor."
                exit 1
            fi
        done
        log "✅ IBKR reconnected after ${WAIT}s"
    fi

    log "🔄 Restarting trading loop ..."
done
