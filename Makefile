# ══════════════════════════════════════════════════════════════
#  0DTE Options Trading Engine — Makefile
# ══════════════════════════════════════════════════════════════
#
#  Usage:
#    make start          Start live trading loop (foreground)
#    make start-bg       Start live trading loop (background, supervised)
#    make stop           Stop the trading loop
#    make status         Show trading state + open positions
#    make kill           Emergency: flatten all positions & stop
#    make validate       Full paper validation (places real paper orders)
#    make validate-dry   Dry-run validation (no orders)
#    make logs           Tail the live trading log
#    make health         Quick health check (IBKR + process)
#    make backend        Start FastAPI backend
#    make frontend       Start Vite frontend
#    make dashboard      Start backend + frontend together
#    make test           Run all tests
#    make clean          Remove logs, state, pycache
#
# ══════════════════════════════════════════════════════════════

SHELL   := /bin/zsh
PROJECT := $(shell pwd)
VENV    := $(PROJECT)/.venv/bin
PYTHON  := $(VENV)/python
PIP     := $(VENV)/pip
LOG_DIR := $(PROJECT)/data/logs
PID_FILE := $(PROJECT)/data/.trading.pid

.PHONY: help start start-bg stop status kill validate validate-dry \
        logs health morning backend frontend dashboard test clean install

# ── Default ──────────────────────────────────────────────────
help:
	@echo ""
	@echo "  0DTE Options Trading Engine"
	@echo "  ════════════════════════════════════════════"
	@echo ""
	@echo "  Trading:"
	@echo "    make start          Live trading (foreground)"
	@echo "    make start-bg       Live trading (background, supervised)"
	@echo "    make stop           Stop trading loop"
	@echo "    make status         Show state & positions"
	@echo "    make kill           Emergency flatten & stop"
	@echo ""
	@echo "  Validation:"
	@echo "    make validate       Full validation (paper orders)"
	@echo "    make validate-dry   Dry-run validation (no orders)"
	@echo "    make health         Health check (IBKR + process)"
	@echo "    make morning        Pre-market diagnostic + Discord"
	@echo ""
	@echo "  Services:"
	@echo "    make backend        Start FastAPI API server"
	@echo "    make frontend       Start Vite dev server"
	@echo "    make dashboard      Start backend + frontend"
	@echo ""
	@echo "  Maintenance:"
	@echo "    make logs           Tail trading log"
	@echo "    make test           Run tests"
	@echo "    make install        Install dependencies"
	@echo "    make clean          Remove logs & cache"
	@echo ""

# ── Setup ────────────────────────────────────────────────────
$(LOG_DIR):
	@mkdir -p $(LOG_DIR)

install:
	$(PIP) install -r requirements.txt
	cd frontend && npm install

# ── Live Trading ─────────────────────────────────────────────
start:
	@echo "🚀 Starting live trading loop (foreground) ..."
	@mkdir -p $(LOG_DIR)
	$(PYTHON) run_live.py --tickers SPY \
		2>&1 | tee $(LOG_DIR)/trading_$$(date +%Y%m%d_%H%M%S).log

start-bg: $(LOG_DIR)
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		echo "⚠️  Trading loop already running (PID $$(cat $(PID_FILE)))"; \
		exit 1; \
	fi
	@echo "🚀 Starting live trading loop (background) ..."
	@$(PROJECT)/scripts/supervised_start.sh

stop:
	@if [ -f $(PID_FILE) ]; then \
		PID=$$(cat $(PID_FILE)); \
		if kill -0 $$PID 2>/dev/null; then \
			echo "🛑 Stopping trading loop (PID $$PID) ..."; \
			kill $$PID; \
			sleep 2; \
			if kill -0 $$PID 2>/dev/null; then \
				echo "⚠️  Sending SIGKILL ..."; \
				kill -9 $$PID; \
			fi; \
			echo "✅ Stopped."; \
		else \
			echo "ℹ️  Process $$PID not running (stale PID file)."; \
		fi; \
		rm -f $(PID_FILE); \
	else \
		echo "ℹ️  No trading loop running (no PID file)."; \
	fi

status:
	@echo ""
	@$(PYTHON) run_live.py --status
	@echo ""
	@if [ -f $(PID_FILE) ] && kill -0 $$(cat $(PID_FILE)) 2>/dev/null; then \
		echo "  Process: ✅ Running (PID $$(cat $(PID_FILE)))"; \
	else \
		echo "  Process: ⏹  Not running"; \
	fi
	@echo ""

kill:
	@echo "🚨 EMERGENCY: Flattening all positions ..."
	@$(PYTHON) run_live.py --kill
	@$(MAKE) stop

# ── Validation ───────────────────────────────────────────────
validate:
	$(PYTHON) scripts/paper_validation.py

validate-dry:
	$(PYTHON) scripts/paper_validation.py --dry-run

health:
	@$(PROJECT)/scripts/health_check.sh

morning:
	@echo "🌅 Running pre-market diagnostic ..."
	$(PYTHON) scripts/morning_premarket.py

# ── Logging ──────────────────────────────────────────────────
logs:
	@if ls $(LOG_DIR)/trading_*.log 1>/dev/null 2>&1; then \
		tail -f $$(ls -t $(LOG_DIR)/trading_*.log | head -1); \
	else \
		echo "No trading logs found in $(LOG_DIR)"; \
	fi

# ── Backend & Frontend ───────────────────────────────────────
backend:
	@echo "🌐 Starting FastAPI backend on :8000 ..."
	cd $(PROJECT)/backend && $(VENV)/uvicorn src.main:app \
		--host 0.0.0.0 --port 8000 --reload

frontend:
	@echo "🎨 Starting Vite frontend on :5173 ..."
	cd $(PROJECT)/frontend && npm run dev

dashboard:
	@echo "📊 Starting backend + frontend ..."
	@$(MAKE) backend &
	@sleep 2
	@$(MAKE) frontend

# ── Tests ────────────────────────────────────────────────────
test:
	$(PYTHON) -m pytest tests/ -v --tb=short 2>/dev/null || \
	$(PYTHON) -m unittest discover -s tests -v

# ── Cleanup ──────────────────────────────────────────────────
clean:
	@echo "🧹 Cleaning up ..."
	rm -rf __pycache__ **/__pycache__ .pytest_cache
	rm -f data/validation_state.json data/validation_report.json
	rm -f data/validation_orders.json
	@echo "✅ Clean. (Trading state & logs preserved.)"

clean-all: clean
	@echo "🧹 Also removing logs ..."
	rm -rf $(LOG_DIR)
	@echo "✅ All clean."
