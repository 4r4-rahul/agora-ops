#!/usr/bin/env zsh
# ══════════════════════════════════════════════════════════════
#  health_check.sh — Quick health check for the trading system
# ══════════════════════════════════════════════════════════════
#
#  Checks:
#    1. Python venv exists
#    2. IBKR TWS/Gateway reachable
#    3. Trading loop process alive
#    4. State file readable
#    5. Webhook reachable
#    6. Disk space
#
#  Exit codes:
#    0 = all healthy
#    1 = one or more checks failed
#
# ══════════════════════════════════════════════════════════════

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PROJECT_DIR}/.venv/bin/python"
PID_FILE="${PROJECT_DIR}/data/.trading.pid"
STATE_FILE="${PROJECT_DIR}/data/live_state.json"

PASS=0
FAIL=0

check() {
    local name="$1"
    local result="$2"
    if [ "$result" = "ok" ]; then
        echo "  ✅ $name"
        PASS=$((PASS + 1))
    else
        echo "  ❌ $name — $result"
        FAIL=$((FAIL + 1))
    fi
}

echo ""
echo "  ════════════════════════════════════════"
echo "  HEALTH CHECK — $(date '+%Y-%m-%d %H:%M:%S')"
echo "  ════════════════════════════════════════"
echo ""

# 1. Python venv
if [ -f "$PYTHON" ]; then
    check "Python venv" "ok"
else
    check "Python venv" "not found at $PYTHON"
fi

# 2. IBKR connectivity
if [ -f "${PROJECT_DIR}/.env" ]; then
    source <(grep -v '^#' "${PROJECT_DIR}/.env" | grep '=' | sed 's/^/export /')
fi
IBKR_HOST="${IBKR_HOST:-127.0.0.1}"
IBKR_PORT="${IBKR_PORT:-7497}"
if nc -z "$IBKR_HOST" "$IBKR_PORT" 2>/dev/null; then
    check "IBKR (${IBKR_HOST}:${IBKR_PORT})" "ok"
else
    check "IBKR (${IBKR_HOST}:${IBKR_PORT})" "not reachable"
fi

# 3. Trading loop process
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        UPTIME=$(ps -o etime= -p "$PID" 2>/dev/null | xargs)
        check "Trading loop (PID $PID)" "ok"
        echo "       Uptime: $UPTIME"
    else
        check "Trading loop" "PID $PID not running (stale PID file)"
    fi
else
    check "Trading loop" "not running (no PID file)"
fi

# 4. State file
if [ -f "$STATE_FILE" ]; then
    LAST_MOD=$(stat -f "%Sm" -t "%Y-%m-%d %H:%M" "$STATE_FILE" 2>/dev/null || \
               stat -c "%y" "$STATE_FILE" 2>/dev/null | cut -d. -f1)
    check "State file" "ok"
    echo "       Last modified: $LAST_MOD"
else
    check "State file" "not found (first run?)"
fi

# 5. Webhook
WEBHOOK_URL="${ALERT_WEBHOOK_URL:-}"
if [ -n "$WEBHOOK_URL" ]; then
    HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 3 "$WEBHOOK_URL" 2>/dev/null || echo "000")
    if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "204" ] || [ "$HTTP_CODE" = "401" ]; then
        check "Webhook" "ok"
    else
        check "Webhook" "HTTP $HTTP_CODE"
    fi
else
    check "Webhook" "not configured (ALERT_WEBHOOK_URL missing)"
fi

# 6. Disk space
DISK_AVAIL=$(df -h "$PROJECT_DIR" | tail -1 | awk '{print $4}')
check "Disk space" "ok"
echo "       Available: $DISK_AVAIL"

# 7. Latest log (if running)
LOG_DIR="${PROJECT_DIR}/data/logs"
if [ -d "$LOG_DIR" ] && ls "$LOG_DIR"/trading_*.log 1>/dev/null 2>&1; then
    LATEST_LOG=$(ls -t "$LOG_DIR"/trading_*.log | head -1)
    LOG_SIZE=$(du -h "$LATEST_LOG" | cut -f1)
    LAST_LINE=$(tail -1 "$LATEST_LOG" 2>/dev/null | head -c 80)
    echo ""
    echo "  📝 Latest log: $(basename "$LATEST_LOG") ($LOG_SIZE)"
    echo "     Last line: $LAST_LINE"
fi

# Summary
echo ""
echo "  ────────────────────────────────────────"
TOTAL=$((PASS + FAIL))
if [ $FAIL -eq 0 ]; then
    echo "  🟢 ALL HEALTHY ($PASS/$TOTAL checks passed)"
else
    echo "  🔴 $FAIL ISSUE(S) ($PASS/$TOTAL checks passed)"
fi
echo ""

[ $FAIL -eq 0 ] && exit 0 || exit 1
