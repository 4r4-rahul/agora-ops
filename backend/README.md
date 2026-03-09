# Backend Service

FastAPI backend powering the options trading agent. Key features:

- Deterministic scoring and plan generation endpoints (`/score/intraday`, `/options/plan`, `/options/place`).
- Real-time market data abstraction wired for Interactive Brokers via `ib_insync`.
- JSONL audit logging for generated plans and execution results to enable compliance + retraining loops.

## Environment Configuration

Create a `.env` file (or export variables) with:

```
OPTIONS_DATA_PROVIDER=ibkr
EQUITY_DATA_PROVIDER=disabled
BROKER_PROVIDER=paper_mock
LOG_LEVEL=INFO

# IBKR connectivity (TWS or IB Gateway)
IBKR_HOST=127.0.0.1
IBKR_PORT=7497
IBKR_CLIENT_ID=32
IBKR_ACCOUNT=***optional***
IBKR_TIMEOUT=10.0
IBKR_MAX_STRIKES=5          # number of strikes per side
IBKR_MAX_CONTRACTS=20       # hard cap on simultaneous option tickers
IBKR_EXCHANGE=SMART
IBKR_CURRENCY=USD

# Market data cache behaviour
MARKET_CACHE_TTL=3.0
MARKET_STALE_TOLERANCE=15.0

# Audit logging
AUDIT_LOG_DIR=./audit_logs
```

Install deps and run:

```
pip install -r requirements.txt
uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
```

## Market Data Flow

1. `MarketDataService` loads an `IBKRMarketDataProvider` during FastAPI startup when `OPTIONS_DATA_PROVIDER=ibkr`.
2. `/market/options-chain` requests fetch cached chains (3s TTL) and normalize quotes (bid/ask/mark, IV, greeks).
3. `/market/greeks` exposes direct greeks snapshots for individual contracts.
4. Plan generation (`/options/plan`) will later consume these snapshots for sizing logic.

## Audit Logging

- JSONL files written under `AUDIT_LOG_DIR` (default `./audit_logs`).
- Each plan emitted gets a unique `plan_id` and is logged with generation metadata.
- `/options/place` appends execution events to the same log so downstream jobs can compute realized metrics.

## Next Steps

- Finish wiring plan sizing to real greeks, then connect actual broker APIs for live or paper execution.
- Add pytest coverage for market endpoints (mocking `MarketDataService`).
- Build daily audit summarizer that uploads metrics to the retraining loop.
