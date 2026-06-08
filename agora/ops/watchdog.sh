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

# ── Startup grace period ──────────────────────────────────────────────────────
# The engine's startup reconciliation (position sync of all open IBKR positions)
# blocks the event loop, so /health returns non-200 for a minute or two AT LAUNCH.
# If a recently-started engine is still alive, do NOT restart — spawning a second
# instance collides on IBKR clientIds (Error 326) and kills both (observed 2026-06-05).
# Only restart once it has had GRACE_SECS to come up; a genuinely hung engine persists
# past the window and is then restarted. If NO engine process exists, restart immediately.
GRACE_SECS=300
PID_FILE="$LOG_DIR/agora.pid"
ENGINE_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
if [[ -n "${ENGINE_PID:-}" ]] && kill -0 "$ENGINE_PID" 2>/dev/null; then
  PID_MTIME="$(stat -f %m "$PID_FILE" 2>/dev/null || echo 0)"
  AGE=$(( $(date +%s) - PID_MTIME ))
  if [[ "$AGE" -ge 0 && "$AGE" -lt "$GRACE_SECS" ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] watchdog: PID $ENGINE_PID unhealthy (HTTP $CODE) but only ${AGE}s old (<${GRACE_SECS}s grace) — skipping restart (likely startup)" >> "$WLOG"
    exit 0
  fi
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] watchdog: engine unhealthy (HTTP $CODE) during trading hours — restarting" >> "$WLOG"
/bin/bash "$REPO/agora/ops/start.sh" >> "$WLOG" 2>&1
echo "[$(date '+%Y-%m-%d %H:%M:%S')] watchdog: start.sh invoked" >> "$WLOG"
