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
# The scan loop can saturate the single event loop for several seconds, so ONE probe
# can time out on a perfectly healthy engine (observed 2026-06-09: 000,000,000,000,200,200
# over ~25s — busy, not hung). Probe up to 3× before declaring unhealthy; a real hang
# fails all three. Combined with the consecutive-failure debounce below, this stopped a
# ~15-min restart churn that was interrupting the (15-min) long-options scan cycle.
FAILCOUNT_FILE="$LOG_DIR/watchdog.failcount"
CODE="000"
for _try in 1 2 3; do
  CODE=$(curl -s -o /dev/null -w "%{http_code}" -m 8 "$HEALTH_URL" 2>/dev/null || echo "000")
  if [[ "$CODE" == "200" ]]; then break; fi
  sleep 3
done
if [[ "$CODE" == "200" ]]; then
  rm -f "$FAILCOUNT_FILE"   # healthy — reset the debounce streak, nothing to do
  exit 0
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
ENGINE_ALIVE=0
if [[ -n "${ENGINE_PID:-}" ]] && kill -0 "$ENGINE_PID" 2>/dev/null; then
  ENGINE_ALIVE=1
  PID_MTIME="$(stat -f %m "$PID_FILE" 2>/dev/null || echo 0)"
  AGE=$(( $(date +%s) - PID_MTIME ))
  if [[ "$AGE" -ge 0 && "$AGE" -lt "$GRACE_SECS" ]]; then
    rm -f "$FAILCOUNT_FILE"   # fresh engine still starting — not a hang; clear the streak
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] watchdog: PID $ENGINE_PID unhealthy (HTTP $CODE) but only ${AGE}s old (<${GRACE_SECS}s grace) — skipping restart (likely startup)" >> "$WLOG"
    exit 0
  fi
fi

# A genuinely DEAD process (no PID alive) is restarted IMMEDIATELY — the debounce only
# guards the alive-but-unhealthy case (a busy scan that outlasts the in-run probes), where
# a needless restart would interrupt work. A dead engine has no work to interrupt.
if [[ "$ENGINE_ALIVE" -eq 0 ]]; then
  rm -f "$FAILCOUNT_FILE"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] watchdog: no live engine process — restarting immediately" >> "$WLOG"
  /bin/bash "$REPO/agora/ops/start.sh" >> "$WLOG" 2>&1
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] watchdog: start.sh invoked" >> "$WLOG"
  exit 0
fi

# ── Debounce: require 2 consecutive unhealthy runs before restarting ───────────
# One unhealthy run (a scan burst that outlasts all 3 in-run probes) is not proof of a
# hang. Restart only after TWO consecutive 5-min checks fail (~10 min unresponsive) — a
# genuine freeze, not a busy moment. Prevents the restart churn that never let a full
# long-options scan cycle complete. (ALIVE-but-hung only; dead engines handled above.)
FAILS="$(cat "$FAILCOUNT_FILE" 2>/dev/null || echo 0)"
case "$FAILS" in ''|*[!0-9]*) FAILS=0 ;; esac
FAILS=$(( FAILS + 1 ))
echo "$FAILS" > "$FAILCOUNT_FILE"
if [[ "$FAILS" -lt 2 ]]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] watchdog: unhealthy (HTTP $CODE) strike ${FAILS}/2 — deferring restart" >> "$WLOG"
  exit 0
fi

rm -f "$FAILCOUNT_FILE"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] watchdog: engine unhealthy (HTTP $CODE) ${FAILS} consecutive checks during trading hours — restarting" >> "$WLOG"
/bin/bash "$REPO/agora/ops/start.sh" >> "$WLOG" 2>&1
echo "[$(date '+%Y-%m-%d %H:%M:%S')] watchdog: start.sh invoked" >> "$WLOG"
