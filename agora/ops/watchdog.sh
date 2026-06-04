#!/usr/bin/env bash
# AGORA watchdog — restarts the engine if it died/hung DURING trading hours.
# Invoked by launchd every 5 min (com.agora.trading.watchdog). It self-gates to the
# trading window so it never fights the scheduled 4:30pm stop or restarts overnight.
#
# "Alive" = the health endpoint returns HTTP 200. A process that is PID-alive but
# hung (frozen loop, dropped event loop) fails this and is restarted — a hung engine
# is worse than a dead one.

set -uo pipefail

REPO="/Users/rahul/Workspace/options_trading_agent_agentic"
LOG_DIR="$REPO/agora/logs"
WLOG="$LOG_DIR/watchdog.log"
HEALTH_URL="http://127.0.0.1:8001/agora/health"

# ── Gate: weekdays only, 08:25–15:05 CDT (market 08:30–15:00 CT + buffer) ──────
DOW=$(date +%u)          # 1=Mon … 7=Sun
HHMM=$(date +%H%M)       # local time (machine is CDT)
if [[ "$DOW" -gt 5 ]]; then exit 0; fi
if [[ "$HHMM" < "0825" || "$HHMM" > "1505" ]]; then exit 0; fi

# ── Alive check: health endpoint must return HTTP 200 ─────────────────────────
CODE=$(curl -s -o /dev/null -w "%{http_code}" -m 8 "$HEALTH_URL" 2>/dev/null || echo "000")
if [[ "$CODE" == "200" ]]; then
  exit 0   # healthy — nothing to do
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] watchdog: engine unhealthy (HTTP $CODE) during trading hours — restarting" >> "$WLOG"
/bin/bash "$REPO/agora/ops/start.sh" >> "$WLOG" 2>&1
echo "[$(date '+%Y-%m-%d %H:%M:%S')] watchdog: start.sh invoked" >> "$WLOG"
