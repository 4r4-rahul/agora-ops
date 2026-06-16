#!/usr/bin/env bash
# Fill-canary reliability read — auto-run at the market open by launchd
# (com.agora.fill_canary). Fires the broker fill-engine canary a few times over ~6 min so we
# get a daily fill-RELIABILITY sample (not a single noisy data point), independent of strategy.
# Each run places + flattens a 1-lot marketable SPY ATM call (paper) and writes to canary_log.
set -uo pipefail

REPO="/Users/rahul/Workspace/options_trading_agent_agentic"
VENV="$REPO/.venv/bin"
LOG="$REPO/agora/logs/fill_canary.log"
RUNS=4
GAP=90   # seconds between runs

cd "$REPO" || exit 1
# Load .env so IBKR host/port/market-data-type match the engine.
set -a; [ -f "$REPO/.env" ] && source "$REPO/.env"; set +a

echo "===== canary reliability read $(date) =====" >> "$LOG"
fills=0
for i in $(seq 1 "$RUNS"); do
  PYTHONPATH="$REPO" "$VENV/python" "$REPO/scripts/fill_canary.py" >/dev/null 2>&1
  # Read the outcome of the run we just did from canary_log (the source of truth).
  oc=$("$VENV/python" -c "import sqlite3; c=sqlite3.connect('$REPO/.agora/agora.db'); print(c.execute('SELECT outcome FROM canary_log ORDER BY id DESC LIMIT 1').fetchone()[0])" 2>/dev/null)
  echo "  run $i/$RUNS -> ${oc:-error}" >> "$LOG"
  [ "$oc" = "filled" ] && fills=$((fills+1))
  [ "$i" -lt "$RUNS" ] && sleep "$GAP"
done
echo "  RESULT: $fills/$RUNS filled ($(( fills * 100 / RUNS ))%) — broker fill reliability at the open" >> "$LOG"
