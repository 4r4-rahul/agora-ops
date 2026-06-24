# AGORA — Agent Inventory & Wiring Reference

> **44 agents** across 8 categories. 24 LLM-backed (Claude), 20 pure Python.  
> Last updated: 2026-06-24 (added §8b — Adaptive & Learning Ops: per-ticker engine, ML provenance, model fleet)

---

## Table of Contents
1. [Signal Generators](#1-signal-generators)
2. [Core Agents — Scoring & Resolution](#2-core-agents--scoring--resolution)
3. [Core Agents — Macro & Synthesis](#3-core-agents--macro--synthesis)
4. [Swing Trading Stack](#4-swing-trading-stack)
5. [Discovery Agents](#5-discovery-agents)
6. [Pre-Market & Position Setup](#6-pre-market--position-setup)
7. [Risk Agents](#7-risk-agents)
8. [Ops Agents](#8-ops-agents)
9. [Position & Attribution](#9-position--attribution)
10. [C-Suite Executives](#10-c-suite-executives)
11. [Shared Data Models](#11-shared-data-models)
12. [Session Wiring Map](#12-session-wiring-map)
13. [Full Data Flow Diagram](#13-full-data-flow-diagram)
14. [Agent Stats Summary](#14-agent-stats-summary)
15. [Architecture Principles](#15-architecture-principles)

---

## 1. Signal Generators

Pure-Python deterministic signals — no LLM, run in the hot scan path.

### VolRegimeClassifier
- **File:** `agora/signals/vol_regime.py`
- **Model:** XGBoost with Platt scaling (rule-based fallback)
- **Purpose:** Classify market vol regime → `low_vol | normal | high_vol | crisis`
- **Context inputs:** IV rank, VIX, VIX3M ratio, HV10/HV30 ratio, SPY RSI, ATM IV
- **Persistent state:** In-memory model weights + scaler; JSON data cache
- **Output → consumed by:** `VolRegimeSignal` → ConvictionScorer, DisagreementResolver, MacroSynthesizer
- **Wired in session.py as:** `self._vol_classifier`

### IvPremiumScreen
- **File:** `agora/signals/iv_premium.py`
- **Model:** Pure Python (rolling threshold check)
- **Purpose:** Signal when implied vol > realized vol persistently — the vol risk premium edge for credit spreads
- **Context inputs:** ATM IV per ticker, 21-day historical vol per ticker
- **Persistent state:** JSON cache per ticker in `.agora/iv_premium/` (dates + ratios, rolling 252 days)
- **Output → consumed by:** `IvPremiumSignal` (premium_ratio, days_above_threshold, signal_active) → ConvictionScorer
- **Wired in session.py as:** `self._iv_screen`

### EventPatternEngine
- **File:** `agora/signals/event_patterns.py`
- **Model:** Pure Python (calendar-based pattern matching)
- **Purpose:** Detect exploitable macro event patterns: FOMC drift, CPI IV premium, post-earnings skew
- **Context inputs:** Ticker, event calendar, beat quality, guidance tone, days since earnings
- **Persistent state:** None (stateless)
- **Output → consumed by:** `EventSignal` (event_type, direction, confidence) → ConvictionScorer
- **Wired in session.py as:** `self._event_engine`

---

## 2. Core Agents — Scoring & Resolution

### ConvictionScorer
- **File:** `agora/agents/conviction_scorer.py`
- **Model:** Pure Python (deterministic 100-point weighted aggregation)
- **Purpose:** Aggregate 8 signals into a single conviction score + trading gate
- **Context inputs:**
  - IV premium signal (IvPremiumScreen)
  - GEX signal (gamma exposure)
  - Vol regime (VolRegimeClassifier)
  - Macro stance (MacroSynthesizer)
  - Event signal (EventPatternEngine)
  - Catalyst (CatalystDiscoveryAgent)
  - Market interest score (MarketInterestAgent)
- **Persistent state:** In-memory session score history (rolling 500 evaluations)
- **Output → consumed by:** `ConvictionScore` (total_score 0-100, gate: high/standard/low/no_trade, pillar) → DisagreementResolver, StrategyRulesEngine, C-suite briefings
- **Wired in session.py as:** `self._scorer`

### DisagreementResolver
- **File:** `agora/agents/disagreement_resolver.py`
- **Model:** Pure Python (3-signal weighted consensus, regime-weighted)
- **Purpose:** Resolve macro + microstructure + catalyst disagreement into a `size_multiplier` {0.0, 0.5, 1.0, 1.5}
- **Context inputs:** MacroContext, GEX signal, catalyst presence (bull/bear/neutral + confidence), conviction score, regime
- **Persistent state:** Session resolution statistics (gate distribution, no_trade counts)
- **Output → consumed by:** `size_multiplier` + gate override applied to all position sizing; StrategyRulesEngine
- **Wired in session.py as:** `self._resolver`

---

## 3. Core Agents — Macro & Synthesis

### MacroSynthesizer
- **File:** `agora/agents/macro_synthesizer.py`
- **Model:** **Claude Opus 4.7** (adaptive thinking, prompt cached)
- **Purpose:** Synthesize vol regime + VIX + calendar + technicals into a qualitative macro stance once per session
- **Context inputs:** Regime classification, VIX, VIX/VIX3M ratio, SPY RSI, FOMC/CPI proximity, Fed funds trajectory
- **Persistent state:** Last `MacroContext` + timestamp (in-memory)
- **What it has learned:** Updated every pre-market (7 AM ET) and on intraday regime shift (SPY moves >1.5% or VIX moves >3 pts). Each run produces a fresh `MacroContext` — not cumulative learning, but each call sees current market snapshot.
- **Output → consumed by:** `MacroContext` (macro_stance, vol_selling_ok, size_bias, confidence, key_risk, reasoning) → ConvictionScorer, DisagreementResolver, CEO, CIO
- **Wired in session.py as:** `self._macro`
- **Notes:** Uses ephemeral prompt caching for the large stable system prompt; called via `_premarket_macro_scan()`

### SectorMomentumAgent
- **File:** `agora/agents/sector_momentum.py`
- **Model:** **Claude Haiku** (Batch API, weekly cadence)
- **Purpose:** Score sector ETF momentum → recommend strategy bias (strong_bull → bull_put_spread, strong_bear → bear_call_spread, neutral → iron_condor)
- **Context inputs:** 4-week return, RSI, IV rank for 15 sector ETFs
- **Persistent state:** JSON cache at `.agora/sector_momentum.json` (valid 7 days)
- **What it has learned:** Sector bias is refreshed weekly; the JSON cache persists the latest bias between session restarts. Represents the most recent 7 days of sector momentum consensus.
- **Output → consumed by:** Sector bias → ConvictionScorer (pillar selection), StrategyRulesEngine (strategy type)
- **Wired in session.py as:** `self._sector`

### SectorIntelligenceAgent
- **File:** `agora/agents/sector_intelligence.py`
- **Model:** **Claude Opus 4.7** (regression synthesis)
- **Purpose:** Peer earnings read-through: given sector peers' beat rates, predict probability of positive surprise for the current ticker
- **Context inputs:** Sector peers' earnings history (beat %, stock move %), current ticker sector
- **Persistent state:** In-memory intelligence cache (ticker → `SectorIntelligence` dataclass with read_through_direction and confidence)
- **What it has learned:** Refreshed once per morning. Read-through direction and confidence survive until next morning refresh. Represents the current session's peer-earnings signal.
- **Output → consumed by:** `SectorIntelligence` → PriceTargetAgent, EarningsCalendarAgent, pre-earnings setup, `_evaluate_ticker()` (conviction boost of up to 10% when peers confirm direction)
- **Wired in session.py as:** `self._sector_intel`

### PriceTargetAgent
- **File:** `agora/agents/price_target.py`
- **Model:** **Claude Opus 4.7** (synthesis)
- **Purpose:** Generate bull/base/bear price scenarios + recommended strikes for directional plays
- **Context inputs:** Analyst consensus (mean/high/low PT), sector intel, peer regression, spot price
- **Persistent state:** Daily cache per ticker (in-memory)
- **Output → consumed by:** `PriceTargetAnalysis` (bull_target, base_target, bear_target, recommended_strike) → StrategyRulesEngine (strike selection), earnings setups
- **Wired in session.py as:** `self._price_target`

---

## 4. Swing Trading Stack

### SwingCandidateScorer
- **File:** `agora/agents/swing_scorer.py`
- **Model:** Pure Python (deterministic 100-point multi-factor)
- **Purpose:** Score each ticker for swing suitability — runs on every 30-min universe cycle for Tier-1 + top market-interest tickers
- **Context inputs:**
  - Technical (40 pts): SMA alignment, RSI setup, ATR dip/rip quality, volume spike
  - Catalyst (30 pts): event type + freshness decay (full credit <4h, 0 at 48h)
  - Fundamental (15 pts): market interest score (0-10) + sector leader flag
  - Options setup (15 pts): IV rank cheapness (≤25 = 10 pts) + IV/HV ratio (<0.9 = exceptional)
- **Persistent state:** None (stateless; threshold `MIN_SCORE_FOR_JUDGE = 40.0`)
- **Output → consumed by:** `SwingFactors` (breakdown + total + direction + notes) → SwingJudgeAgent if score ≥ 40
- **Wired in session.py as:** `self._swing_scorer`

### SwingJudgeAgent
- **File:** `agora/agents/swing_judge.py`
- **Model:** **Claude Opus 4.7** (adaptive thinking, system prompt cached)
- **Purpose:** Go/no-go decision for a directional long option (call or put) — resolves conflicting signals, picks strike/DTE/entry condition
- **Context inputs:**
  - `SwingFactors` breakdown with ASCII bars
  - Market snapshot (RSI, SMAs, ATR, IV rank, HV, VIX, volume)
  - Sector context (one-liner from SectorIntelligenceAgent)
  - Options chain summary (ATM ±2 strikes, 2 nearest expiries with bid/ask/IV)
  - **Last 5 journal entries for this ticker** (learning context — thesis, audit, outcome)
- **Persistent state:** None (stateless; learning comes via journal context injected per call)
- **What it has learned:** Every call to Claude includes the past 5 decisions for the ticker pulled from SwingJournal SQLite. Claude sees: timestamp, go/no-go, direction, outcome (win/loss/open), P&L %, thesis, post-trade audit. This is the learning loop — the model adapts its judgment based on what worked and what failed.
- **Output → consumed by:** `SwingDecision` (go, option_type, direction, entry_condition, strike, target_expiry_dte, price_target, stop_price, max_hold_days, confidence, key_thesis, what_kills_trade, journal_text) → SwingJournal + position creation
- **Wired in session.py as:** `self._swing_judge`
- **Rules:** Prefer 20-45 DTE, near-ATM (0.35-0.50 delta), 1 contract only, ≥60% confidence to go

### SwingJournal
- **File:** `agora/agents/swing_journal.py`
- **Model:** **Claude Opus 4.7** (post-trade self-audit)
- **Purpose:** Persist every swing decision; trigger self-audit when a position closes; provide learning context for future judge calls
- **Context inputs (on audit):** Full trade record — pre-trade thesis, entry/exit prices, P&L, outcome
- **Persistent state:** SQLite `swing_journal` table with columns:
  ```
  ticker, decision_ts, session_id, raw_score, factor_breakdown,
  go, option_type, direction, entry_condition, strike, target_expiry_dte,
  price_at_decision, price_target, stop_price, max_hold_days, confidence,
  key_thesis, what_kills_trade, journal_text, method,
  entry_order_id, fill_price, fill_ts,
  close_price, close_ts, pnl_dollars, pnl_pct, outcome,
  post_trade_audit, prediction_accuracy, lesson_learned
  ```
- **What it has learned:** Each `lesson_learned` (1 sentence, Claude-generated) + `prediction_accuracy` (0.0-1.0) accumulates per ticker. This is the explicit memory of the swing system — future judge calls read the last 5 rows including audit text.
- **Output → consumed by:** `get_past_entries(ticker, limit=5)` → SwingJudgeAgent context; performance summary → dashboard; self-audit → `lesson_learned` in DB
- **Wired in session.py as:** `self._swing_journal`
- **API endpoints:** `GET /agora/swing/decisions`, `GET /agora/swing/open`, `GET /agora/swing/performance`, `POST /agora/swing/audit`

---

## 5. Discovery Agents

### CatalystDiscoveryAgent
- **File:** `agora/discovery/catalyst_agent.py`
- **Model:** **Claude Haiku** (streaming, <3s first token)
- **Purpose:** Poll EDGAR every 60s for 8-K + press releases → classify as catalyst (type, direction, strength, liquidity gate)
- **Context inputs:** SEC filing text, company name, filing date, 8-K item type
- **Persistent state:** SQLite seen-hash table (prevents reprocessing across restarts)
- **What it has learned:** Seen-hash table ensures idempotency across session restarts. The Claude classification itself is stateless (each 8-K is evaluated fresh).
- **Output → consumed by:** `Catalyst` object → `session._on_catalyst()` callback → position creation pipeline
- **Wired in session.py as:** `self._catalyst_agent` | Polls EDGAR every 60s
- **Notes:** Ephemeral prompt caching for extraction schema; streaming for latency

### EarningsTranscriptAgent
- **File:** `agora/discovery/earnings_transcript.py`
- **Model:** **Claude Opus 4.7** (extended thinking, multi-hop derivative inference)
- **Purpose:** T+0 extraction from earnings 8-K Item 2.02: beat quality, guidance, derivative tickers (suppliers/customers/peers affected)
- **Context inputs:** 8-K filing text, company ticker
- **Persistent state:** None (stateless, fires catalysts immediately)
- **Output → consumed by:** Catalyst fired immediately + `EventPatternEngine` post-earnings skew signal at T+1–T+3
- **Wired in session.py as:** `self._earnings_agent` | Triggered by EDGAR 8-K Item 2.02

### EarningsCalendarAgent
- **File:** `agora/discovery/earnings_calendar.py`
- **Model:** **Claude Haiku** (peer classification) + **Claude Opus 4.7** (edge analysis)
- **Purpose:** Proactive pre-earnings setup T-7: edge ratio (implied vs historical move), sector read-through, probability of significant move
- **Context inputs:** Next 1-7 day earnings calendar, peer earnings history, implied move (straddle), historical move average
- **Persistent state:** None (one sweep per 6:30 AM ET)
- **Output → consumed by:** `EarningsSetup` (edge_ratio, peer_context, probability) → `session._on_earnings_setup()` → pre-earnings position
- **Wired in session.py as:** `self._earnings_calendar`

### SmartMoneyAgent
- **File:** `agora/discovery/smart_money.py`
- **Model:** **Claude Opus 4.7** (tool use loop for 13D intent) + **Claude Haiku** (batch for Form 4)
- **Purpose:** Detect activist stakes (13D ≥5%), passive stakes (13G), insider buy clusters (Form 4)
- **Context inputs:** SEC filing metadata, Item 4 text (13D purpose statement), transaction tables (Form 4)
- **Persistent state:** SQLite seen-hash table
- **What it has learned:** Claude explicitly reads specific SEC filing sections via tool calls before classifying — transparent and auditable. Seen-hash prevents reprocessing.
- **Output → consumed by:** Catalyst (activist_13d, insider_cluster) → `session._on_catalyst()` → position creation
- **Wired in session.py as:** `self._smart_money` | Monitors EDGAR every 5 min

### AnalystRevisionTracker
- **File:** `agora/discovery/analyst_revision.py`
- **Model:** Pure Python (yfinance aggregation)
- **Purpose:** Post-earnings analyst upgrade/downgrade momentum → multi-week drift catalyst
- **Context inputs:** `yfinance.Ticker.upgrades_downgrades` (90-day lookback per ticker)
- **Persistent state:** In-memory `_analyzed` dict (ticker → last analyzed date)
- **What it has learned:** In-memory state tracks which tickers were analyzed today (prevents duplicate firing in one session). Resets on restart.
- **Output → consumed by:** Catalyst fired on ≥2 upgrades or downgrades → post-earnings drift play
- **Wired in session.py as:** `self._analyst_rev`
- **Known issue (May 15):** R&D flagged "analyst revision feed silent" — tracker not emitting signals; check yfinance upgrades_downgrades connectivity

### IBKRNewsAgent
- **File:** `agora/discovery/ibkr_news.py`
- **Model:** **Claude Haiku** (real-time classification on background thread)
- **Purpose:** IBKR tick 292 (Dow Jones live news) → catalyst classification for universe tickers
- **Context inputs:** News headlines, article snippets from IBKR news subscriptions
- **Persistent state:** None (stateless, fires immediately)
- **Output → consumed by:** Catalyst → `session._on_catalyst()` (real-time)
- **Wired in session.py as:** `self._ibkr_news` | Dedicated background thread (ib_insync requires own event loop)
- **Known issue (May 15):** Error 10168 for ~30 tickers — market data not subscribed; delayed data not enabled in TWS

### MarketInterestAgent
- **File:** `agora/discovery/market_interest.py`
- **Model:** Pure Python (6-fingerprint rules)
- **Purpose:** Detect market interest via: OI buildup, IV term structure, ETF flows, sweep orders, earnings calendar density, short interest
- **Context inputs:** Options chains (OI, volume, IV), ETF volume, short interest data — updated every 30 min
- **Persistent state:** In-memory `MarketInterestScore` cache (ticker → score + fingerprint breakdown)
- **What it has learned:** In-memory scores from the current session. Top-interest tickers are promoted to the priority scan queue, getting re-evaluated every cycle even if not in Tier-1.
- **Output → consumed by:** `MarketInterestScore` → ConvictionScorer (market_interest component), universe priority queue, SwingCandidateScorer (fundamental component)
- **Wired in session.py as:** `self._market_interest`

### UniverseDiscoveryAgent
- **File:** `agora/discovery/universe_discovery.py`
- **Model:** **Claude Opus 4.7** (momentum + earnings classification)
- **Purpose:** Dynamically expand the trading universe (cap 15/week) based on earnings calendar, market interest, momentum, catalysts
- **Context inputs:** Earnings dates, market interest scores, momentum signals
- **Persistent state:** In-memory `_dynamic` dict (ticker → `DiscoveredTicker` with reason and timestamp)
- **What it has learned:** Dynamic tickers persist for the current session in memory; resets on restart. Discovered tickers are merged with the static `etf_universe` list on every scan.
- **Output → consumed by:** Dynamic tickers merged into `_universe_scan()` batch each cycle
- **Wired in session.py as:** `self._universe_disc` | Weekly Monday sweep + catalyst-triggered additions

---

## 6. Pre-Market & Position Setup

### PreMarketSetupAgent
- **File:** `agora/agents/premarket_setup.py`
- **Model:** **Claude Opus 4.7** (analysis + alert generation)
- **Purpose:** 6:30 AM ET review of open positions vs overnight moves — gap alerts, DTE warnings, sector context
- **Context inputs:** Open positions list, overnight price moves, sector futures, earnings dates
- **Persistent state:** Last `PreMarketSetupReport` (in-memory)
- **Output → consumed by:** `PositionAlert` callbacks fired to session + CEO morning brief
- **Wired in session.py as:** `self._premarket_setup`

---

## 7. Risk Agents

### RiskCouncil
- **File:** `agora/risk/risk_council.py`
- **Model:** Pure Python (10 deterministic gate checks in order)
- **Purpose:** Portfolio-level pre-trade risk gate — first failure blocks the trade
- **Context inputs:** Current Greeks (delta, vega, theta), open positions, recommendation (strategy, legs, contracts)
- **Persistent state:**
  - SQLite `kill_switch` table (persists across restarts; must be explicitly reset)
  - SQLite `daily_pnl` table (rolling P&L for daily/weekly loss checks)
- **What it has learned (persistent):**
  - Kill switch state: trips on daily loss breach, single position blowup, or manual API call. **Survives restarts.**
  - Daily P&L history: 5-day rolling window for weekly loss limit check.
- **Gate checks (in order):**
  1. Kill switch active?
  2. Daily loss > 2% of account?
  3. Weekly loss > 6% of account?
  4. Open positions ≥ max (4)?
  5. |Portfolio delta| > 30 per $10k?
  6. |Portfolio vega| > 200 per $10k?
  7. |Daily theta| > 0.5% of account/day?
  8. Correlation group limit exceeded?
  9. R/R ratio below minimum (1.3 for debit, 0.10 for credit spreads)?
- **Output → consumed by:** Approve/reject `TradeRecommendation` synchronously before any IBKR submission
- **Wired in session.py as:** `self._risk`

### CircuitBreakerAgent
- **File:** `agora/risk/circuit_breaker.py`
- **Model:** Pure Python (real-time P&L monitor) + **Claude Opus 4.7** (alert generation)
- **Purpose:** Real-time P&L monitor every 60s — auto-trip kill switch on daily loss breach, single position blowup, or VIX >40
- **Context inputs:** Current realized + unrealized P&L, portfolio delta, VIX, SPY opening price
- **Persistent state:** Kill switch flag (via RiskCouncil SQLite), VIX stress mode flag (in-memory), trip log
- **Output → consumed by:** Trips kill switch → CEO critical alert → immediate halt
- **Wired in session.py as:** `self._circuit_breaker`

### ComplianceAgent
- **File:** `agora/risk/compliance.py`
- **Model:** Pure Python (regulatory rules)
- **Purpose:** Wash sale tracking (30-day window), Reg T margin check, options level validation, concentration limits (max 20% in one ticker)
- **Context inputs:** Closed losing trades (ticker, date, P&L), proposed position, realized positions
- **Persistent state:** SQLite `wash_sale_log`
- **What it has learned (persistent):** Wash sale log accumulates across sessions — knows which tickers have recent losses that would trigger wash sale rules.
- **Output → consumed by:** Advisory warnings or hard blocks pre-execution
- **Wired in session.py as:** `self._compliance`

### EntryTimingGate
- **File:** `agora/risk/entry_timing.py`
- **Model:** Pure Python (time-of-day rules)
- **Purpose:** Hard block on after-hours and pre-market entries — only allows new entries 10:00 AM–3:30 PM ET (avoids illiquid fills)
- **Context inputs:** Current time (ET)
- **Persistent state:** None
- **Output → consumed by:** Hard block on any trade submission outside the window
- **Wired in session.py as:** `self._entry_timing` | Checked inline in `_submit_recommendation()` and `_swing_scan()`

---

## 8. Ops Agents

### ExecutionQualityAgent
- **File:** `agora/ops/execution_quality.py`
- **Model:** Pure Python (metrics tracking)
- **Purpose:** Track fill rate, reject taxonomy by error code, slippage (ticks) per strategy type per session
- **Context inputs:** Every order attempt (ticker, strategy, mid_price, outcome, fill_price)
- **Persistent state:**
  - SQLite `execution_quality` table (rolling history)
  - In-memory session counters (attempts, fills, rejects, by error code)
- **What it has learned:** SQLite history spans multiple sessions. Fill rate, P50/P90 slippage per strategy, most common reject codes are queryable historically. CEO patrol compares current session vs historical baseline.
- **Output → consumed by:** Fill rate <30% → CEO critical alert; COO department briefing
- **Wired in session.py as:** `self._exec_quality`
- **Known issue (May 15):** 0/6 fills = 0% fill rate — orders submitted after 3:30 PM due to session restarts; need earlier start

### DataIntegrityAgent
- **File:** `agora/ops/data_integrity.py`
- **Model:** Pure Python (sanity checks)
- **Purpose:** Cross-validate IVR feed; if ≥3 ETFs simultaneously at IVR≥99, flag as degraded and block vol-premium bypass
- **Context inputs:** IVR per ticker, price per ticker
- **Persistent state:** In-memory deque of last-3 IVR values per ticker; `ivr_feed_healthy` flag
- **What it has learned:** Per-ticker IVR history (last 3 readings) used to detect sudden spikes. If feed looks degraded, `vol_bypass_allowed()` returns `False` — prevents bad IVR data from triggering false vol-premium trades.
- **Output → consumed by:** `vol_bypass_allowed()` → session vol-premium bypass gate; COO alert on degradation
- **Wired in session.py as:** `self._data_integrity`

### PillarHealthAgent
- **File:** `agora/ops/pillar_health.py`
- **Model:** Pure Python (signal cadence monitoring)
- **Purpose:** Detect silent pillars — if a pillar (macro/sector/market_interest) hasn't emitted a directional signal for >60 min → warning; >180 min → critical
- **Context inputs:** `record_pillar_signal()` called by `_evaluate_ticker()` on every scan
- **Persistent state:** Per-pillar last-signal timestamp + contribution counts (in-memory)
- **Output → consumed by:** CEO critical alert on >180-min silence per pillar
- **Wired in session.py as:** `self._pillar_health`

### OrphanOrderReconciler
- **File:** `agora/ops/orphan_reconciler.py`
- **Model:** Pure Python + **Claude Opus 4.7** (IBKR diagnosis via IBKRKnowledgeAgent)
- **Purpose:** Sync IBKR open orders vs shadow book; cancel stale GTC bracket children that cause Error 201
- **Context inputs:** IBKR open orders, filled executions, shadow book positions
- **Persistent state:** Last reconciliation counts (in-memory)
- **Output → consumed by:** Orphans cancelled → reduces Error 201 frequency; COO alert
- **Wired in session.py as:** `self._orphan_reconciler` | Runs at startup + every 30 min + on Error 201 detection

### SystemHealthAgent
- **File:** `agora/ops/system_health.py`
- **Model:** Pure Python (heartbeat checks)
- **Purpose:** 5-min health probe for: IBKR connectivity, yfinance data freshness, Anthropic API latency, DB integrity, disk space
- **Context inputs:** Connectivity probes (TCP), data timestamp checks, API latency measurements
- **Persistent state:** `HealthCheck` state per service (name, status, message, failure_count) — in-memory
- **Output → consumed by:** First-failure alert only per check type → CEO; auto-recovers on success
- **Wired in session.py as:** `self._system_health`

### AgentPerformanceMonitor
- **File:** `agora/ops/agent_performance.py`
- **Model:** Pure Python (SQL aggregation)
- **Purpose:** Alpha attribution by pillar, regime, and conviction band (win rate, avg PnL, total PnL, profit factor)
- **Context inputs:** `trade_records` from SQLite (pillar, regime, conviction, realized_pnl)
- **Persistent state:** None (queries DB on-demand; history lives in `trade_records` SQLite table)
- **What it has learned:** Every closed trade is in SQLite. Attribution queries compute: which pillar (vol_premium, directional, catalyst, etc.) generated alpha, which regime (low_vol, normal, high_vol) performed best, which conviction band (60-70, 70-80, 80+) had the best risk-adjusted return.
- **Output → consumed by:** CFO weekly performance briefing
- **Wired in session.py as:** `self._agent_perf`

### IBKRKnowledgeAgent
- **File:** `agora/ops/ibkr_knowledge_agent.py`
- **Model:** **Claude Opus 4.7** (adaptive thinking, very large cached system prompt)
- **Purpose:** On-demand IBKR expert: diagnose Error 201 storms, audit adaptive pricing, analyze fill-rate failures, 30-min background scan
- **Context inputs:** IBKR order state, fills, executions, error logs, shadow book
- **Persistent state:** Scan history, diagnostic cache (in-memory)
- **What it has learned:** System prompt embeds the full IBKR TWS API reference (cached via `cache_control: ephemeral`). Each call sees current state. Background scans accumulate in `scan_history`.
- **Output → consumed by:** COO department diagnostic reports; `POST /agora/ibkr-diagnose` endpoint
- **Wired in session.py as:** `self._ibkr_agent` | Runs in own thread with separate event loop

---

## 8b. Adaptive & Learning Ops (added 2026-06)

Deterministic, read-only/shadow learning layer. All run in the `ScheduledAttributor` loop
(`agora/ops/outcome_attributor.py`); none affect live trades until board-promoted.

### Per-Ticker Engine (Phase 1+2, SHADOW)
- **`agora/ops/ticker_profile.py`** — characterizes each ticker from ~3y price history (yfinance):
  realized vol (HV), ATR%, trend persistence, β-SPY, 1m momentum → `ticker_profiles` table. Derives a
  DOWN-ONLY vol-scaled risk-cap factor. `build_profiles()` runs **daily** (network-gated).
- **`agora/ops/ticker_adapter.py`** — unifies the vol signature (profile) + shrunk realized edge
  (Bayesian partial-pooling, n≥6) → per-ticker risk-cap override = `min(vol, edge)` factor, **down-only,
  SHADOW** (`ticker_settings.active=0`). Runs every attributor cycle.
- **`agora/ops/ticker_settings.py`** — store + `TickerSettingsResolver` (O(1) cache; applies only
  `active=1`). **Wired into** `StrategyRulesEngine._size_contracts` (per-ticker `max_risk_per_trade_dollars`,
  inert until promoted — zero-regression).
- **Output → consumed by:** `/agora/ticker-settings` route + 🎯 Per-Ticker Engine dashboard panel.

### ML Config-Provenance
- **`agora/ops/config_provenance.py`** — fingerprints trade-affecting tunables; records a monotonic
  `config_version` (auto-diff) on change. `PositionManager` stamps `config_version_at_entry` on every
  position; `feature_store` carries it → ML segments outcomes by settings regime. **Extend:** add new
  tunables to `_TRACKED`.

### Path Instrumentation (MFE/MAE)
- `PositionManager._update_position_price` tracks running `peak_unrealized_pnl`/`trough_unrealized_pnl`
  (the sole mark path); `feature_store` converts to `max_favorable_pct`/`max_adverse_pct`. Fixed the
  sparse-daily-snapshot bug that corrupted exit-tuning data.

### ML Model Fleet
- **`agora/ops/model_runner.py`** + `agora/ops/ml_models/` (M1 fill, M2 regime, M3 liquidity, M4 win/EV,
  M5 conviction-calib, M8 lifecycle, dataset_health) + `model_analyst.py` + `shadow_advisor.py`. All
  n-gated/shadow. Routes: `/agora/models/*`.

---

## 9. Position & Attribution

### PositionManager
- **File:** `agora/lifecycle/position_manager.py`
- **Model:** Pure Python (state machine)
- **Purpose:** Full position lifecycle: OPEN → TESTED → ROLLED/CLOSED/EXPIRED; closes at 50% profit or 21 DTE or 2× stop-loss
- **Context inputs:** Current market prices, Greeks, open position DB
- **Persistent state:**
  - SQLite `open_positions` table (full position state; survives restarts)
  - SQLite `trade_records` table (closed trades with full attribution data)
- **What it has learned:** All closed trades with P&L, regime at entry, conviction at entry are in `trade_records`. This is the system's primary institutional memory — every trade ever taken is queryable.
- **Output → consumed by:** PositionAlert callbacks on state transitions; close/roll order callbacks to `_execute_close()` / `_execute_roll()`; TradeRecord written to SQLite on close
- **Wired in session.py as:** `self._position_mgr` | Runs every 60s during market hours

### PnlAttributor (+ PsiMonitor)
- **File:** `agora/ops/attribution.py`
- **Model:** Pure Python + **Claude Sonnet 4.6** (cost-efficient brief generation)
- **Purpose:** Daily P&L attribution (realized, unrealized, by pillar/regime/conviction) + PSI (Portfolio Stress Index) monitoring
- **Context inputs:** Closed trades, unrealized positions, current Greeks
- **Persistent state:** In-memory rolling metrics (7-day, 30-day, YTD)
- **Output → consumed by:** CFO briefing, CEO EOD report; PSI alert if stress index spikes
- **Wired in session.py as:** `self._attributor` + `self._psi`

---

## 10. C-Suite Executives

All C-suite agents share the same `ExecutiveAgent` base class (`agora/c_suite/base.py`):
- Run on a background patrol loop (default every 1800s)
- Write structured patrol reports with findings
- Escalate issues to CEO via alert dispatch
- Subscribe to `AgentEventBus` for lateral C-to-C communication
- Read the CEO's `SessionPlan` (published each morning) to self-calibrate

### CEOAgent
- **File:** `agora/agents/ceo_agent.py`
- **Model:** **Claude Opus 4.7** (adaptive thinking)
- **Purpose:** Autonomous daily operator — morning brief, mid-morning check, midday status, EOD report, ad-hoc critical alerts
- **Context inputs:** All agent states (position manager, risk council, market conditions, catalyst counts, pillar health, fill rate)
- **Persistent state:**
  - `SessionPlan` (morning board meeting output — tactical stance, focus tickers, size bias)
  - Closed-trade feedback ledger (in-memory ring buffer of last 50 closed trades)
- **What it has learned:** CEO adapts intraday based on what's happening — each patrol call synthesizes current state. SessionPlan persists for the day and is read by all C-suite. Feedback ledger informs position sizing guidance.
- **Output → consumed by:** Discord reports to Rahul (owner); SessionPlan → all C-suite agents; critical alerts → immediate action
- **Wired in session.py as:** `self._ceo`

### CROAgent (Chief Risk Officer)
- **File:** `agora/c_suite/cro.py`
- **Model:** **Claude Opus 4.7**
- **Purpose:** Oversee risk (RiskCouncil, CircuitBreaker, Compliance) — patrol for Greeks breaches, VaR exceedances, kill switch state
- **Context inputs:** Portfolio delta/vega/theta, kill switch state, daily loss, drawdown, compliance flags
- **Persistent state:** Audit history ring buffer (last 48 patrols)
- **Subscribes to:** `profit_factor_low`, `fill_rate_critical` (AgentEventBus)
- **Wired in session.py as:** `self._cro`
- **Sub-agents supervised:** RiskCouncil, CircuitBreaker, ComplianceAgent

### CIOAgent (Chief Intelligence Officer)
- **File:** `agora/c_suite/cio.py`
- **Model:** **Claude Opus 4.7**
- **Purpose:** Oversee intelligence — macro stance quality, sector bias freshness, catalyst hit rate, market interest signal
- **Context inputs:** MacroContext, sector bias, catalyst count and breakdown, market interest top tickers
- **Persistent state:** Audit history ring buffer
- **Subscribes to:** `position_closed` (tracks which intelligence led to fills)
- **Wired in session.py as:** `self._cio`
- **Sub-agents supervised:** MacroSynthesizer, SectorMomentumAgent, MarketInterestAgent, CatalystDiscoveryAgent, SmartMoneyAgent, IBKRNewsAgent

### CTOAgent (Chief Trading Officer)
- **File:** `agora/c_suite/cto.py`
- **Model:** **Claude Opus 4.7**
- **Purpose:** Oversee trading — conviction score distribution, gate distribution, execution quality, position manager state
- **Context inputs:** ConvictionScorer statistics, gate distribution, execution quality metrics, avg slippage
- **Persistent state:** Audit history ring buffer
- **Subscribes to:** `kill_switch_tripped`, `kill_switch_reset`, `size_bias_changed`, `regime_changed`, `macro_context_updated`, `fill_rate_critical`, `ibkr_disconnected`, `daily_loss_warning`, `conviction_drift`
- **Wired in session.py as:** `self._cto`
- **Sub-agents supervised:** ConvictionScorer, DisagreementResolver, ExecutionQualityAgent, PositionManager

### COOAgent (Chief Operations Officer)
- **File:** `agora/c_suite/coo.py`
- **Model:** **Claude Opus 4.7**
- **Purpose:** Oversee operations — fill rate, reject codes, IBKR health, data integrity, orphan orders, system health
- **Context inputs:** Fill rate, reject code taxonomy, IBKR health, IVR data integrity status, orphan counts, system health checks
- **Persistent state:** Audit history ring buffer
- **Subscribes to:** `fill_rate_critical`
- **Wired in session.py as:** `self._coo`
- **Sub-agents supervised:** ExecutionQualityAgent, OrphanOrderReconciler, DataIntegrityAgent, SystemHealthAgent, IBKRKnowledgeAgent

### CFOAgent (Chief Financial Officer)
- **File:** `agora/c_suite/cfo.py`
- **Model:** **Claude Opus 4.7**
- **Purpose:** Oversee finance — daily P&L, realized + unrealized, by-pillar attribution, capital allocation
- **Context inputs:** Daily P&L, by-pillar stats, Sharpe/Sortino, drawdown metrics
- **Persistent state:** Audit history ring buffer
- **Subscribes to:** `position_closed`
- **Wired in session.py as:** `self._cfo`
- **Sub-agents supervised:** PnlAttributor, AgentPerformanceMonitor

### RNDAgent (Chief Research Officer)
- **File:** `agora/c_suite/rnd.py`
- **Model:** **Claude Opus 4.7**
- **Purpose:** Oversee research — signal validation, pillar contribution, alpha decay, new universe discovery, analyst revision feed
- **Context inputs:** Signal validation stats, pillar contribution counts, alpha decay trend, newly discovered tickers, analyst revision activity
- **Persistent state:** Audit history ring buffer
- **Subscribes to:** `position_closed`, `conviction_drift`
- **Wired in session.py as:** `self._rnd`
- **Sub-agents supervised:** EarningsTranscriptAgent, EarningsCalendarAgent, EventPatternEngine, UniverseDiscoveryAgent, PillarHealthAgent, AnalystRevisionTracker

### CTechAgent (Chief Technology Officer)
- **File:** `agora/c_suite/ctech.py`
- **Model:** **Claude Opus 4.7**
- **Purpose:** Oversee technology — signal pipeline latency, Claude API usage/cost, data infrastructure health, model accuracy
- **Context inputs:** Signal latency per pillar, Claude API call counts, VolRegimeClassifier accuracy, data lag
- **Persistent state:** Audit history ring buffer
- **Subscribes to:** `fill_rate_critical`, `ibkr_disconnected`
- **Wired in session.py as:** `self._ctech`
- **Sub-agents supervised:** VolRegimeClassifier, IvPremiumScreen, EventPatternEngine, MacroSynthesizer, SectorIntelligenceAgent, UniverseDiscoveryAgent

---

## 11. Shared Data Models

Defined in `agora/core/models.py` — the lingua franca passed between all agents.

### Enums
| Enum | Values |
|---|---|
| `StrategyType` | bull_put_spread, bear_call_spread, iron_condor, iron_butterfly, cash_secured_put, bull_call_spread, bear_put_spread, long_call, long_put, calendar_spread |
| `StrategyPillar` | vol_premium, directional, event_fomc, event_cpi, post_earnings, catalyst, congressional, smart_money |
| `CatalystType` | earnings_beat, earnings_miss, contract_win, fda_approval, activist_13d, insider_cluster, merger, guidance_raise, guidance_cut |
| `Regime` | low_vol, normal, high_vol, crisis |
| `GexRegime` | positive (mean-reversion), negative (trending), neutral |
| `PositionStatus` | open, tested, rolled, closed, expired, assigned |

### Signal Models
| Model | Key Fields |
|---|---|
| `VolRegimeSignal` | regime, confidence, iv_rank, vix, vix_vix3m_ratio |
| `IvPremiumSignal` | premium_ratio, days_above_threshold, signal_active |
| `GexSignal` | gex_total, regime, dominant_strike, flip_level |
| `EventSignal` | event_type, ticker, days_to_event, direction, confidence |
| `ConvictionScore` | total_score (0-100), gate, pillar, 8 component scores, reasoning |
| `Catalyst` | ticker, type, direction, strength, filing_time, headline, derivative_tickers |
| `MacroContext` | macro_stance, vol_selling_ok, size_bias, confidence, key_risk, reasoning |
| `MarketInterestScore` | ticker, score (0-10), fingerprints (6 signals) |

### Trade Models
| Model | Key Fields |
|---|---|
| `SpreadLeg` | option_type, strike, expiration, action (buy/sell), delta, theta, vega, gamma, mid_price |
| `TradeRecommendation` | ticker, strategy, pillar, direction, legs[], entry_debit_credit, max_loss, max_gain, conviction_score, reward_risk_ratio |
| `OpenPosition` | position_id, ticker, strategy, pillar, status, legs, entry_price, max_loss, ibkr_order_ids, conviction_at_entry, regime_at_entry |
| `SwingDecision` | go, option_type, direction, entry_condition, strike, target_expiry_dte, price_target, stop_price, confidence, key_thesis, what_kills_trade, journal_text |

---

## 12. Session Wiring Map

How `AgoraSession.__init__()` wires every agent (ordered by instantiation):

```
session.py __init__() wiring blocks
────────────────────────────────────────────────────────────────────────────
Lines   80–92    Signal generators:
                   self._vol_classifier, self._iv_screen,
                   self._event_engine, self._psi

Lines   93–104   Core scoring + swing stack:
                   self._macro, self._sector, self._scorer,
                   self._resolver, self._strategy,
                   self._swing_scorer, self._swing_judge, self._swing_journal

Lines  107–118   Infrastructure:
                   self._position_mgr (on_close=_execute_close,
                                        on_roll=_execute_roll)
                   self._risk, self._attributor

Lines  119–148   Discovery agents:
                   self._catalyst_agent (on_catalyst=_on_catalyst)
                   self._earnings_agent (on_earnings=_on_earnings,
                                          on_catalyst=_on_catalyst)
                   self._smart_money (on_catalyst=_on_catalyst)
                   self._ibkr_news (on_catalyst=_on_catalyst)
                   self._earnings_calendar (on_earnings_setup=_on_earnings_setup)

Lines  149–185   Risk + ops layer:
                   self._entry_timing, self._ceo
                   self._circuit_breaker (position_mgr, risk, ceo)
                   self._premarket_setup (position_mgr, on_alert=_on_position_alert)
                   self._sector_intel
                   self._price_target (sector_intel_agent=_sector_intel)
                   self._market_interest, self._universe_disc
                   self._compliance, self._system_health

Lines  194–228   Ops agents:
                   self._agent_perf
                   self._analyst_rev (on_catalyst=_on_catalyst)
                   self._exec_quality (ceo_agent)
                   self._data_integrity (ceo_agent)
                   self._pillar_health (ceo_agent)
                   self._orphan_reconciler (position_mgr, ceo)
                   self._ibkr_agent (exec_quality, orphan_reconciler, position_mgr)

Lines  237–301   C-suite (injected with their sub-agents):
                   self._cro  (risk, circuit_breaker, compliance, position_mgr)
                   self._cio  (macro, sector_intel, market_interest, catalyst, smart_money, ibkr_news)
                   self._cto  (conviction_scorer, resolver, position_mgr, exec_quality)
                   self._coo  (exec_quality, orphan_reconciler, data_integrity, system_health, ibkr_agent)
                   self._cfo  (position_mgr, pnl_attributor, agent_performance)
                   self._rnd  (pillar_health, earnings_cal, earnings_transcript, event_engine,
                               universe_disc, analyst_rev)
                   self._ctech (conviction_scorer, resolver, event_engine, iv_screen,
                                vol_regime, macro, sector_intel, universe_disc)
                 → self._ceo.wire_c_suite(all 7)

Lines  307–337   AgentEventBus lateral subscriptions:
                   CRO  ← profit_factor_low, fill_rate_critical
                   CIO  ← position_closed
                   CTO  ← kill_switch_tripped/reset, size_bias_changed,
                            regime_changed, macro_context_updated,
                            fill_rate_critical, ibkr_disconnected,
                            daily_loss_warning, conviction_drift
                   COO  ← fill_rate_critical
                   CFO  ← position_closed
                   RND  ← position_closed, conviction_drift
                   CTech← fill_rate_critical, ibkr_disconnected

Lines  338–397   Sub-agent reporting chains (register_csuite_manager):
                   RiskCouncil     → CRO
                   ComplianceAgent → CRO
                   CircuitBreaker  → CRO
                   CatalystAgent   → CIO
                   SmartMoney      → CIO
                   IBKRNews        → CIO
                   ExecQuality     → COO
                   DataIntegrity   → COO
                   SystemHealth    → COO
                   OrphanRecon     → COO
                   IBKRAgent       → COO
                   PillarHealth    → RND
                   EarningsCal     → RND
                   EarningsAgent   → RND
                   AnalystRev      → RND

Lines  367–397   LiveReadinessMeter (go-live gate):
                   Registers all 18 production agents for readiness check
```

---

## 13. Full Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                         SIGNAL LAYER  (Pure Python)                             │
│                                                                                 │
│  yfinance / IBKR ──→ VolRegimeClassifier ──→ VolRegimeSignal                  │
│                 └──→ IvPremiumScreen     ──→ IvPremiumSignal                  │
│                 └──→ EventPatternEngine  ──→ EventSignal                       │
└────────────────────────────────┬────────────────────────────────────────────────┘
                                 │  (signals dict)
┌────────────────────────────────▼────────────────────────────────────────────────┐
│                     CATALYST & CONTEXT LAYER  (Claude LLM)                      │
│                                                                                 │
│  EDGAR 60s poll ──→ CatalystDiscoveryAgent (Haiku)  ──→ Catalyst              │
│  EDGAR 5m poll  ──→ SmartMoneyAgent (Opus+Haiku)    ──→ Catalyst              │
│  IBKR tick 292  ──→ IBKRNewsAgent (Haiku)           ──→ Catalyst              │
│  EDGAR 8-K 2.02 ──→ EarningsTranscriptAgent (Opus)  ──→ Catalyst + EventSignal│
│  7:00 AM daily  ──→ MacroSynthesizer (Opus)         ──→ MacroContext          │
│  6:30 AM daily  ──→ EarningsCalendarAgent (Opus+Haiku) → EarningsSetup        │
│  Weekly Monday  ──→ SectorMomentumAgent (Haiku)     ──→ SectorBias            │
│  Daily morning  ──→ SectorIntelligenceAgent (Opus)  ──→ SectorIntelligence    │
│  Per-ticker     ──→ PriceTargetAgent (Opus)         ──→ PriceScenarios        │
│  Every 30 min   ──→ MarketInterestAgent (Python)    ──→ MarketInterestScore   │
└────────────────────────────────┬────────────────────────────────────────────────┘
                                 │  (MacroContext, Catalyst, signals)
┌────────────────────────────────▼────────────────────────────────────────────────┐
│                      SCORING LAYER  (Pure Python)                               │
│                                                                                 │
│  ConvictionScorer  ←  8 signals (vol regime, IV premium, GEX, macro,          │
│  (100 pts)             event, catalyst, smart money, market interest)          │
│        │                                                                        │
│        ▼                                                                        │
│  ConvictionScore (total, gate: high/standard/low/no_trade, pillar)             │
│        │                                                                        │
│  DisagreementResolver ← macro + microstructure + catalyst direction           │
│        │                                                                        │
│        ▼                                                                        │
│  size_multiplier {0.0, 0.5, 1.0, 1.5}                                         │
└────────────────────────────────┬────────────────────────────────────────────────┘
                                 │
         ┌───────────────────────┴──────────────────────────┐
         │                                                  │
┌────────▼──────────────────────┐         ┌────────────────▼──────────────────┐
│     VOL PREMIUM PATH          │         │       SWING TRADING PATH           │
│                               │         │                                    │
│  IVR ≥ 60 + macro ok +        │         │  SwingCandidateScorer (Python)    │
│  conviction ≥ 50              │         │  (Technical/Catalyst/Fund/Options) │
│        │                      │         │         │                          │
│        ▼                      │         │    score ≥ 40?                    │
│  StrategyRulesEngine          │         │         │ yes                      │
│  (credit spread selection)    │         │         ▼                          │
│        │                      │         │  SwingJudgeAgent (Opus)            │
│        ▼                      │         │  + past journal context            │
│  TradeRecommendation          │         │         │                          │
│  (BULL_PUT / BEAR_CALL /      │         │    SwingDecision (go/no-go)       │
│   IRON_CONDOR)                │         │         │                          │
│                               │         │    SwingJournal (SQLite)          │
│                               │         │  + post-trade audit (Opus)        │
│                               │         │         │ (if go=True)             │
│                               │         │         ▼                          │
│                               │         │  TradeRecommendation              │
│                               │         │  (LONG_CALL / LONG_PUT)           │
└────────┬──────────────────────┘         └────────────────┬──────────────────┘
         └────────────────────┬───────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────────────────────────┐
│                     RISK GATING LAYER  (Pure Python, in order)                  │
│                                                                                 │
│  EntryTimingGate    →  10:00–15:30 ET only                                     │
│  MacroCalendarGate  →  no FOMC/NFP/CPI days                                   │
│  ComplianceAgent    →  wash sale, Reg T, concentration                         │
│  RiskCouncil        →  10 checks: kill switch, P&L limits, Greeks,             │
│                         correlation, R/R ratio                                  │
│  OpenComboGate      →  ≤3 open IBKR combo brackets (Error 201 prevention)     │
│  CircuitBreaker     →  VIX stress mode size reduction                         │
│  DiscordApproval    →  optional: high-conviction DM approval                   │
└─────────────────────────────┬───────────────────────────────────────────────────┘
                              │  APPROVED
┌─────────────────────────────▼───────────────────────────────────────────────────┐
│                    EXECUTION LAYER  (ib_insync + IBKR)                          │
│                                                                                 │
│  submit_trade() → adaptive limit pricing (6 steps × $0.05)                    │
│       │                                                                         │
│       ├─ Filled   → ExecutionQualityAgent.record_fill()                        │
│       │             PositionManager.add_position() → SQLite                    │
│       │             journal entry written (why + macro context)                 │
│       │                                                                         │
│       ├─ Rejected → ExecutionQualityAgent.record_reject(error_code)            │
│       │             exec_cooldown set (2h) if spread too illiquid              │
│       │             Error 201 → OrphanOrderReconciler triggered immediately    │
│       │                                                                         │
│       └─ Pending  → logged as attempt only (not recorded as position)          │
└─────────────────────────────┬───────────────────────────────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────────────────────────┐
│              POSITION LIFECYCLE  (PositionManager state machine, every 60s)     │
│                                                                                 │
│  Mark-to-market → check 50% profit target → check 2× stop-loss                │
│  → check 21 DTE close → check tested (ITM short strike)                       │
│  → 15:30 ET: auto-close all expiring positions                                 │
│  → on close: TradeRecord → SQLite, SwingJournal audit, CEO feedback            │
└─────────────────────────────┬───────────────────────────────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────────────────────────┐
│               PATROL & MONITORING LAYER  (background loops)                     │
│                                                                                 │
│  Every  60s:  CircuitBreaker (P&L + kill switch)                               │
│  Every   5m:  SystemHealthAgent (IBKR / yfinance / Anthropic / DB / disk)     │
│  Every  10m:  PillarHealthAgent (signal cadence per pillar)                    │
│  Every  30m:  DataIntegrityAgent + OrphanOrderReconciler + IBKRKnowledgeAgent  │
│  Every  30m:  Each C-suite agent patrol (domain-specific self-audit)           │
│  Post-market: PnlAttributor → CFO report                                       │
└─────────────────────────────┬───────────────────────────────────────────────────┘
                              │
┌─────────────────────────────▼───────────────────────────────────────────────────┐
│                    REPORTING LAYER  (CEOAgent, Discord)                          │
│                                                                                 │
│  6:00 AM  Morning Brief (positions, overnight, macro setup, watchlist)          │
│  6:30 AM  PreMarketSetupAgent (gap alerts, DTE warnings, sector context)        │
│  10:00 AM Mid-Morning Check (new positions, market conditions, alerts)          │
│  1:00 PM  Midday Status (P&L, risk, regime shifts)                             │
│  4:00 PM  C-suite Patrol Reports (CRO/CIO/CTO/COO/CFO/RND/CTech)              │
│  4:30 PM  EOD Report (full day P&L, tomorrow's plan)                           │
│  Ad-hoc   Critical alerts (kill switch, fill rate, data issues)                │
│                ↓                                                                │
│           Discord → Rahul                                                      │
└─────────────────────────────────────────────────────────────────────────────────┘
```

---

## 14. Agent Stats Summary

| Category | Count | LLM-Backed | Pure Python | Model Used |
|---|---|---|---|---|
| Signal Generators | 3 | 1 | 2 | XGBoost + rules |
| Core Scoring | 2 | 0 | 2 | — |
| Macro & Synthesis | 4 | 4 | 0 | Opus 4.7, Haiku |
| Swing Trading | 3 | 1 | 2 | Opus 4.7 (judge + audit) |
| Discovery | 8 | 7 | 1 | Opus 4.7, Haiku |
| Pre-Market | 1 | 1 | 0 | Opus 4.7 |
| Risk | 4 | 0 | 4 | — |
| Ops | 7 | 2 | 5 | Opus 4.7 (IBKR, audit) |
| Position & Attribution | 2 | 1 | 1 | Sonnet 4.6 (briefs) |
| C-Suite Executives | 7 | 7 | 0 | Opus 4.7 |
| **TOTAL** | **44** | **24** | **20** | — |

### SQLite Tables (Persistent Memory)
| Table | Owner | What Persists |
|---|---|---|
| `kill_switch` | RiskCouncil | Active flag + reason + timestamp; survives restarts |
| `daily_pnl` | RiskCouncil | Realized + unrealized P&L per day (5-day rolling for weekly limit) |
| `open_positions` | PositionManager | All open positions with full leg detail |
| `trade_records` | PositionManager | Every closed trade with P&L, regime, conviction |
| `catalyst_seen` | CatalystDiscoveryAgent | SHA-256 hashes of processed EDGAR filings |
| `smart_money_seen` | SmartMoneyAgent | SHA-256 hashes of processed SEC filings (13D/G, Form 4) |
| `wash_sale_log` | ComplianceAgent | Losing trades within 30-day wash sale window |
| `execution_quality` | ExecutionQualityAgent | Per-order fill rate, slippage, reject codes |
| `swing_journal` | SwingJournal | Every swing decision + fill + close + self-audit |

### File Caches (Persistent State)
| Cache | Owner | TTL |
|---|---|---|
| `.agora/iv_cache/{TICKER}.json` | YFinanceProvider | Rolling 252 days of ATM IV readings |
| `.agora/iv_premium/{TICKER}.json` | IvPremiumScreen | Rolling IV/HV ratio history |
| `.agora/sector_momentum.json` | SectorMomentumAgent | 7 days |
| `.agora/shadow_book.json` | PositionManager | Current open positions (for fast startup) |

---

## 15. Architecture Principles

### 1. Determinism First
Scoring (ConvictionScorer, DisagreementResolver, SwingCandidateScorer) is fully deterministic pure Python — no LLM randomness in the hot path. Claude is called only where synthesis or judgment is genuinely needed.

### 2. Risk-First, Not Risk-Last
Every trade goes through 10+ sequential gates before reaching IBKR. Kill switch is SQLite-persistent (not in-memory), so it survives crashes. Circuit breaker runs every 60s independently of the scan loop.

### 3. LLM Usage Tiering
- **Haiku** → high-frequency classification (every 8-K, every IBKR news tick)
- **Sonnet** → cost-efficient departmental reporting
- **Opus** → one-off synthesis (macro, sector intelligence, earnings analysis)
- **Opus + adaptive thinking** → conflict resolution (swing judge, IBKR diagnostics, CEO decisions)

### 4. Crash Recovery Without State Loss
WAL-mode SQLite ensures all position state, trade records, kill switch, and compliance data survive any restart. `OrphanOrderReconciler` re-syncs IBKR on startup. Cooldown dicts reset (acceptable — prevents ghost trades after restart).

### 5. Self-Learning via Journal + Audit Loop
The swing trading stack is the only component with explicit self-learning:
- SwingJudgeAgent → SwingJournal → Claude post-trade audit → `lesson_learned` in SQLite → fed back to next SwingJudgeAgent call as context
- All other agents use historical SQLite data (AgentPerformanceMonitor) for attribution but do not modify their own behavior based on it

### 6. Hierarchical Alert Routing
```
Sub-agent finds issue
      → C-suite manager (CRO/CIO/CTO/COO/CFO/RND/CTech) receives alert
            → C-suite evaluates severity
                  → escalates to CEO only if critical
                        → CEO sends Discord DM to Rahul
```
This prevents alert spam while ensuring nothing critical is missed.

### 7. Lateral C-to-C Communication
C-suite agents communicate directly via `AgentEventBus` without routing through CEO:
- CTO receives `kill_switch_tripped` and immediately re-calibrates sizing
- COO receives `fill_rate_critical` and immediately triggers IBKR diagnostics
- CRO receives `profit_factor_low` and tightens risk gates

### 8. SessionPlan as Shared Context
CEO publishes a `SessionPlan` each morning (tactical stance, focus tickers, size bias). All 7 C-suite agents read it to self-configure. This allows the CEO to set top-down direction that propagates without point-to-point coupling.
