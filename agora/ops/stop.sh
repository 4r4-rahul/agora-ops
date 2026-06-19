#!/usr/bin/env bash
# AGORA trading session shutdown — ORPHAN-PROOF.
# The old version trusted a single agora.pid file. A prior bad restart (start over a still-running
# engine → port-8001 collision) leaves the old process alive running its background tasks (IBKR
# poller, position lifecycle) the pidfile knows nothing about → TWO engines on ONE IBKR account
# (could double-close/fight positions). So we kill by PORT OWNER + every uvicorn-agora pid, not just
# the pidfile: TERM, wait for exit, then KILL survivors (uvicorn often ignores SIGTERM).

REPO="/Users/rahul/Workspace/options_trading_agent_agentic"
LOG_DIR="$REPO/agora/logs"
LOG="$LOG_DIR/agora.log"
PID_FILE="$LOG_DIR/agora.pid"

# Every engine pid: the port-8001 listener + all uvicorn-agora matches + the pidfile, de-duped.
collect_pids() {
  { lsof -nP -iTCP:8001 -sTCP:LISTEN -t 2>/dev/null
    pgrep -f "uvicorn agora.api.app" 2>/dev/null
    [[ -f "$PID_FILE" ]] && cat "$PID_FILE" 2>/dev/null
  } | grep -E '^[0-9]+$' | sort -u
}

echo "[stop.sh] shutdown begin — $(date)" >> "$LOG"

PIDS=$(collect_pids)
if [[ -n "$PIDS" ]]; then
  echo "[stop.sh] SIGTERM: $(echo "$PIDS" | tr '\n' ' ') — $(date)" >> "$LOG"
  for p in $PIDS; do kill -TERM "$p" 2>/dev/null || true; done
  # wait up to 10s for graceful exit
  for _ in $(seq 1 10); do
    [[ -z "$(collect_pids)" ]] && break
    sleep 1
  done
  SURV=$(collect_pids)
  if [[ -n "$SURV" ]]; then
    echo "[stop.sh] SIGKILL survivors: $(echo "$SURV" | tr '\n' ' ') — $(date)" >> "$LOG"
    for p in $SURV; do kill -KILL "$p" 2>/dev/null || true; done
    sleep 2
  fi
fi

rm -f "$PID_FILE"
LEFT=$(collect_pids)
if [[ -n "$LEFT" ]]; then
  echo "[stop.sh] WARNING — still running after kill: $(echo "$LEFT" | tr '\n' ' ') — $(date)" >> "$LOG"
else
  echo "[stop.sh] AGORA stopped clean — $(date)" >> "$LOG"
fi
