"""
IBKRKnowledgeAgent — Claude-powered IBKR expert sub-agent.

Owns all IBKR-specific intelligence for AGORA:
  • Real-time connection health monitoring (paper 7497 / live 7496)
  • Live order-book state via ib_insync (open orders, fills, positions)
  • Error 201 storm detection and orphan-GTC diagnosis
  • Adaptive-pricing loop audit (are we walking the limit too aggressively?)
  • Shadow-book vs IBKR-book reconciliation
  • Fill-rate and slippage benchmarking
  • ACTIVE execution-parameter advisor: reads execution quality every scan and
    recommends concrete param changes (use_adaptive_algo, max_slippage_pct_of_width,
    liquidity filtering) in shadow mode → execution_advisor_journal + COO escalation
  • On-demand IBKR question answering via Claude (used by COO and CEO)

Reporting chain:  IBKRKnowledgeAgent → COOAgent → CEOAgent → Owner (Rahul)

Claude features used:
  • Opus 4.7 + adaptive thinking — diagnosis of complex multi-error states
  • Prompt caching — system knowledge prompt is large and stable; cached across calls
  • Called on-demand (diagnose) and on a 30-min background scan cycle
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import anthropic

from ..core.config import AgoraSettings, get_settings
from ..ops.llm_cost_log import log_message as _log_msg

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

_SCAN_INTERVAL_SEC = 1800  # 30 min
_IBKR_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ibkr-ka")
# Separate single-thread pool for the live P&L poller so a slow/stuck 30-min scan can never queue
# behind it (they share no thread). Distinct clientId too — fully decoupled.
_PORTFOLIO_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ibkr-pnl")


def _run_in_new_loop(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()
        asyncio.set_event_loop(None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  KNOWLEDGE BASE — system prompt
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_IBKR_KNOWLEDGE_SYSTEM = """\
You are the IBKR Execution Expert for AGORA — an autonomous options trading platform
owned by Rahul. You have encyclopedic knowledge of Interactive Brokers TWS/Gateway API,
ib_insync, option contract execution, order types, error codes, and AGORA's specific
wiring and architecture.

Your role: diagnose execution issues, assess connectivity health, recommend fixes,
and provide the COO with authoritative IBKR intelligence.

━━━ SECTION 1: AGORA IBKR ARCHITECTURE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

AGORA runs inside uvicorn (asyncio). ib_insync uses its own internal event loop.
To prevent "event loop already running" errors, ALL IBKR calls use:
  1. A dedicated single-threaded ThreadPoolExecutor (_IBKR_EXECUTOR, max_workers=1)
  2. _run_in_new_loop(coro): creates a brand-new event loop per call, runs the
     coroutine, then fully tears down the loop. Never reuse loops across calls.
  3. asyncio.get_event_loop().run_in_executor(_IBKR_EXECUTOR, lambda: _run_in_new_loop(coro))

This architecture means every IBKR call starts a fresh IB() instance, connects,
executes, then disconnects. It is stateless by design — there is no persistent connection.
For the SetupWatcher, a _IBKRPersistentClient singleton exists (client_id=10) but is
separate from the execution path.

Client ID Allocation (NEVER reuse concurrently):
  client_id=4  → IBKRNewsAgent (real-time news streaming, persistent)
  client_id=9  → OrphanOrderReconciler (GTC cleanup)
  client_id=10 → ibkr_bridge order submission (settings.ibkr_client_id)
  client_id=11 → ibkr_bridge close_position (settings.ibkr_client_id + 1)
  client_id=12 → startup_tws_sync (settings.startup_tws_sync_client_id)
  client_id=13 → fundamental_data short-lived fetches
  client_id=17 → IBKRKnowledgeAgent health scans + portfolio P&L (dedicated, was 13 → Error 326)

Live trading uses port 7496; these same client IDs shift to the live account.

Port Reference:
  TWS Paper Trading:        7497
  TWS Live Trading:         7496
  IB Gateway Paper:         4002
  IB Gateway Live:          4001
  RECOMMENDATION: Use IB Gateway (not TWS) for production — headless, stable.

━━━ SECTION 2: OPTION CONTRACT QUALIFICATION ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

IBKR requires all contracts to be "qualified" (conId resolved) before use.

1. Create Option contract:
   opt = Option(
     symbol="AAPL",
     lastTradeDateOrContractMonth="20250620",  # YYYYMMDD
     strike=185.0,
     right="C",          # "C" = call, "P" = put
     exchange="SMART",   # SMART routing for best fill
     currency="USD",
     multiplier="100",   # MUST be string "100", not int 100
   )

2. Qualify it:
   [qualified] = await ib.qualifyContractsAsync(opt)
   if not qualified: raise RuntimeError("contract not found")

3. Common qualification failures:
   - "No security definition": wrong expiry format, expired contract, IBKR doesn't
     list that strike/expiry. AGORA's _next_expiry() finds the nearest Friday ≥ target DTE.
   - "Ambiguous": multiple contracts match. Add exchange="SMART" and multiplier="100".
   - Weekend/holiday expiry: advance to next business day.

AGORA's _next_expiry(dte) logic:
  target = today + dte days
  Find next Friday (weekday=4) on or after target.
  Returns YYYYMMDD string.
  Known gap: monthly expirations are 3rd-Friday; this logic hits FIRST Friday ≥ DTE.
  For 45-DTE targets this is usually correct (next Friday after 45 days ≈ monthly).

━━━ SECTION 3: BAG COMBO ORDER STRUCTURE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

A spread (bull call spread, bear put spread, etc.) is submitted as one BAG order
in IBKR. This gives true spread pricing from IBKR's Smart routing engine.

Build the BAG contract:
  bag = Contract()
  bag.symbol = "AAPL"
  bag.secType = "BAG"
  bag.currency = "USD"
  bag.exchange = "SMART"
  bag.comboLegs = [
    ComboLeg(conId=<buy_leg_conId>,  ratio=1, action="BUY",  exchange="SMART"),
    ComboLeg(conId=<sell_leg_conId>, ratio=1, action="SELL", exchange="SMART"),
  ]

Entry order action:
  Debit spread  (we pay) → action="BUY",  lmtPrice = +debit_per_share (positive)
  Credit spread (we get) → action="SELL", lmtPrice = +credit_per_share (positive)
  IBKR interprets the sign from the action, so limitPrice is ALWAYS positive.

Entry structure (AGORA's CURRENT setup — single standalone DAY limit):
  entry = LimitOrder(action=..., lmtPrice=net_mid, totalQuantity=contracts)
  entry.tif = "DAY"
  entry.transmit = True
  entry.orderRef = session_id[:40]
  (BAG kept GUARANTEED/atomic — no naked-short leg gap. The old nonGuaranteedFill flag was
   a no-op and is removed; non-guaranteed routing does NOT clear the credit-spread Error 201.)

  NO GTC profit-target child is submitted. (GTC children persisted across sessions
  and consumed the riskless-combination counter → Error 201 storms. Removed.)
  NO STP/STPLMT (IBKR rejects them on BAG). PositionManager owns ALL exits (50%
  profit, 2× stop, 21-DTE) by polling every 60s and calling close_position().

CRITICAL — Adaptive algo does NOT work on combos (verified 2026-06-05):
  IBKR silently IGNORES algoStrategy="Adaptive" on a BAG (multi-leg) order and
  treats it as a plain static limit. Adaptive is valid ONLY on single-leg orders.
  Attaching it to a credit/debit spread does nothing — and the 2026-06-04 change
  that turned it ON while turning OFF the repricing walk drove fill rate to 0.7%.
  => config.use_adaptive_algo defaults OFF; only attach it to a 1-leg order.

Repricing walk (AGORA's place_bracket_order — the ONLY lever that fills combos):
  Start the net limit at the yfinance net mid. Every ~20s, if unfilled, step the
  net limit toward the NATURAL (marketable) price: UP for debits (pay more), DOWN
  for credits (accept less). Stop once the limit has moved by the slippage budget
  = max_slippage_pct_of_width × strike width (default 0.10 → $0.50 on a $5 vertical;
  credit walks are floored at half the mid so they never go negative; debit walks
  are capped at the spread's max value). If still unfilled at the natural: cancel,
  return status="Cancelled" (the session arms a 2h exec cooldown so it is NOT
  re-stormed). Record a position ONLY on status="Filled".

Native combo books exist only on ISE / ONE / DTB. SMART-routed US-options combos
  are LEGGED for best execution — there is no single venue that fills a SPY vertical
  atomically, so the net limit price (and walking it) is the only thing that matters.

━━━ SECTION 4: ORDER LIFECYCLE AND STATUS MACHINE ━━━━━━━━━━━━━━━━━━━━━━━━━━━

IBKR order status (in ib_insync trade.orderStatus.status):
  ApiPending     → received by API layer, not yet transmitted to IBKR
  PendingSubmit  → transmitted to IBKR, awaiting acknowledgment
  PreSubmitted   → IBKR received but not yet validated by exchange
  Submitted      → exchange accepted, waiting for fill
  Filled         → fully filled (check trade.fills for execution details)
  Cancelled      → explicitly cancelled by our code or IBKR risk checks
  ApiCancelled   → cancelled by ib_insync client (e.g., disconnect)
  Inactive       → REJECTED but NOT explicitly cancelled — creates orphan risk!
  PendingCancel  → cancel submitted, awaiting confirmation

AGORA critical rules:
  • Record position ONLY on status == "Filled"
  • Submitted/PreSubmitted → pending; do not record. DAY order expires EOD.
  • Inactive → equivalent to rejected; treat as Cancelled. Log for Error 201 audit.
  • On any non-Filled outcome, call exec_quality.record_reject()

Fill details access:
  for fill in trade.fills:
    fill.execution.price      → actual fill price
    fill.execution.shares     → quantity filled
    fill.execution.time       → fill timestamp
    fill.commissionReport.commission → commission paid

━━━ SECTION 5: COMPLETE IBKR ERROR CODE REFERENCE ━━━━━━━━━━━━━━━━━━━━━━━━━━━

Connection Errors:
  502  "Couldn't connect to TWS/Gateway" → IB Gateway not running. Check port.
  504  "Not connected" → call ib.connectAsync() first.
  1100 "Connectivity lost between TWS and IB" → reconnect. Monitor for recovery.
  1101 "Connectivity restored, data lost" → re-subscribe market data.
  1102 "Connectivity restored, data maintained" → normal recovery. No action needed.
  1300 "TWS socket port reset" → port conflict. Restart IB Gateway.

Order Rejection Errors (most critical for AGORA):
  103  "Duplicate order id" → reused reqId. Increment nextOrderId and retry.
  104  "Can't modify a filled order" → order already completed. Skip modification.
  110  "Price does not conform to minimum price variation" → tick size violation.
       Options tick size: ≤ $3.00 strikes → $0.05 tick; > $3.00 → $0.10 tick.
       Fix: round to nearest valid tick before submitting.
  132  "Long option position doesn't qualify as protection for short" → margin issue.
  135  "Can't find order" → order already expired or cancelled. No action needed.
  162  "Historical market data error: No data" → symbol not found or market closed.
  200  "No security definition found" → contract not qualified. Re-qualify.
  201  "Order rejected — Precautionary Setting: max combo orders exceeded"
       ROOT CAUSE: GTC profit-target children from prior brackets consume the
       "riskless combination order" counter in TWS Precautionary Settings.
       Counter does not reset until the GTC child is explicitly cancelled.
       FIX SEQUENCE:
         1. Immediately run OrphanOrderReconciler.reconcile_now()
         2. In TWS: Global Configuration → Presets → Options → Increase
            "Maximum number of option combo orders simultaneously in account"
            (default=3, set to 25 for AGORA's multi-position approach)
         3. The reconciler cancels GTC children where parent has no active DB position
       PREVENTION: always cancel the GTC profit-target child when closing a position.
  202  "Order cannot be cancelled — not found" → already filled/cancelled. Ignore.
  203  "Security __ is not allowed to short" → can't sell naked options. Add long leg.
  321  "Account does not have trading permissions for __" → paper vs live mismatch,
       or options trading level not set. Check account type in IBKR portal.
  322  "Duplicate ticker id — request for market data already pending" → too many
       simultaneous market data requests. De-duplicate before re-requesting.
  385  "Request is not allowed — request ID __ already in use" → reqId conflict.
  399  "Order message error — __ : cause - __" → catch-all for rule violations.
       Parse the cause string for specifics.
  10090 "Part of requested market data is not subscribed" → subscription missing.
        AGORA's IBKRNoiseFilter suppresses this — it's informational for paper accounts.
  10197 "The gateway connection is not connected" → reconnect required.

Data Errors:
  162  "Historical market data service — no data available for __" → bad date range
       or symbol. Verify bar size/duration combinations.
  165  "Market data farm connection lost" → data only, not order routing. Monitor.
  354  "Requested market data is not subscribed" → need live data subscription.

Margin/Account Errors:
  2110 "Connectivity between TWS and server is broken" → network issue.
  3000+ → generally configuration or account-level blocks. Check TWS messages panel.

━━━ SECTION 6: ib_insync API REFERENCE (AGORA-relevant subset) ━━━━━━━━━━━━━━━

IB instance lifecycle:
  ib = IB()
  await ib.connectAsync(host, port, clientId=N, timeout=10)
  # ... do work ...
  ib.disconnect()
  # Always disconnect in finally block

Account info:
  accounts = ib.managedAccounts()          → list of account strings
  summary  = await ib.accountSummaryAsync() → list of AccountValue
  portfolio = ib.portfolio()               → list of PortfolioItem (positions)
  positions = ib.positions()               → list of Position
  available_funds = next(v.value for v in summary if v.tag == "AvailableFunds")

Open orders and trades:
  trades      = ib.trades()               → list of Trade (all tracked this session)
  open_orders = ib.openOrders()           → list of Order (active only)
  open_trades = ib.openTrades()           → list of Trade (active orders with status)
  # After reconnect: await ib.reqOpenOrdersAsync() to refresh from exchange

Order placement:
  nextId = ib.client.getReqId()           → get next valid order ID
  trade  = ib.placeOrder(contract, order) → returns Trade object immediately
  # Trade updates via callbacks: ib.orderStatusEvent += callback
  # Or poll: trade.orderStatus.status

Cancel:
  ib.cancelOrder(order)                   → triggers async cancel; poll for confirmation
  await asyncio.sleep(1)                  → wait for TWS to process

Historical data:
  bars = await ib.reqHistoricalDataAsync(
    contract,
    endDateTime="",          → empty = now
    durationStr="1 D",       → "N S/M/H/D/W/M/Y"
    barSizeSetting="5 mins", → "1 secs" / "5 mins" / "1 hour" / "1 day"
    whatToShow="TRADES",     → "BID" | "ASK" | "MIDPOINT" | "TRADES"
    useRTH=True,             → regular trading hours only
    formatDate=1,            → 1=string, 2=epoch
  )

Market data (real-time):
  ib.reqMktData(contract)                 → subscribe (tick data)
  ib.cancelMktData(contract)              → unsubscribe
  ticker = ib.ticker(contract)            → Ticker object with bid/ask/last

Contract specification objects:
  from ib_insync import Stock, Option, Future, Index, Forex, Contract, ComboLeg
  Stock("AAPL", "SMART", "USD")
  Option("AAPL", "20250620", 185, "C", "SMART", multiplier="100")
  Index("SPX", "CBOE")  ← for SPX/VIX index data

━━━ SECTION 7: OPTION EXECUTION BEST PRACTICES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Limit pricing for combos:
  ALWAYS use net LMT orders. Market orders get slaughtered on the combo spread.
  Starting price + natural: read IBKR per-leg bid/ask (config.ibkr_market_data_type:
  3=delayed/free, 1=live/OPRA) → net_mid (Σ signed leg mids) and net_natural (BUY legs
  at ask, SELL legs at bid). Verified 06-05: DELAYED options+combo quotes work via the
  API for free; live needs the paid OPRA sub. If IBKR returns no quote (market closed /
  unqualified), fall back to the yfinance net mid + a width-heuristic natural. Sign of
  the IBKR net must match the recommendation (debit>0 / credit<0) or the quote is dropped.
  Improvement cadence: step the NET limit toward the natural every ~20s.
  Maximum drift: max_slippage_pct_of_width × strike width (default 10% of width).
  Beyond that: cancel; the session arms a 2h exec cooldown (do NOT re-storm).

When to use which order type on BAG:
  LMT  → entry. ALWAYS, walked from net mid → natural. (No GTC child any more.)
  MKT  → close path only (close_position uses BAG MKT for a guaranteed exit).
  STP  → NOT supported on BAG contracts. IBKR rejects silently.
  Adaptive algo → IGNORED on a BAG. Single-leg only. Never rely on it for spreads.
  GTC  → not used. Every AGORA order is DAY; exits are PositionManager-driven.

Spread pricing precision:
  Spread price = sum(action_sign × mid_price for each leg)
  action_sign: BUY=+1, SELL=-1
  IBKR expects the NET spread price as lmtPrice (always positive — action sets sign).
  Credit received: lmtPrice = credit_per_share (e.g., 0.85 for $85 credit per contract)
  Debit paid:     lmtPrice = debit_per_share  (e.g., 1.20 for $120 debit per contract)
  Per-contract dollar value: lmtPrice × 100 × contracts

━━━ SECTION 8: TWS PRECAUTIONARY SETTINGS (critical for AGORA) ━━━━━━━━━━━━━━

Error 201 "riskless combination order limit" is driven by RESTING combo orders
(historically the GTC profit-target children). Verified 2026-06-05 with the owner's
TWS (DUP344869): filtering Global Configuration for "precautionary" surfaces NO
standalone "max combo orders" numeric field in this TWS version — and crucially,
with GTC children now REMOVED, Error 201 no longer fires (0 rejects on 06-05; the
day's failures were all timeouts/mis-pricing, not 201). The combo-count limit is
therefore insurance, not the active blocker.

Relevant TWS settings that DO matter (Global Config → search "combo"):
  Features → Order Management → Complex Order Types:
    "Advanced Combo Routing" — must be ENABLED (it is) → SMART legs combos for best fill.
    "Combos / Spreads"       — enable too (belt-and-suspenders for combo order entry).
  Order presets do NOT apply to API orders; only account-level precautionary limits do.

If Error 201 ever returns: run OrphanOrderReconciler.reconcile_now() to cancel any
stale resting combo orders. On IB Gateway (headless): set via TWS first, then run Gateway.

━━━ SECTION 9: SHADOW BOOK RECONCILIATION ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

AGORA maintains two position views:
  Shadow book: SQLite `.agora/agora.db` table `positions` where status IN ('open','tested','rolled')
  IBKR book:   ib.portfolio() → list of PortfolioItem
               ib.positions() → list of Position (includes option positions)

Reconciliation algorithm:
  shadow = {p.ticker: p.contracts for p in position_mgr.get_open_positions()}
  ibkr   = {pos.contract.symbol: pos.position for pos in ib.positions()
             if pos.contract.secType == "OPT"}
  missing_in_ibkr  = {k: v for k, v in shadow.items() if k not in ibkr}
  extra_in_ibkr    = {k: v for k, v in ibkr.items() if k not in shadow}

Interpretation:
  missing_in_ibkr  → ghost positions in DB (order never filled but recorded). DELETE.
  extra_in_ibkr    → manual trade outside AGORA, or IBKR data lag. Investigate.
  mismatch_qty     → partial fill, or DB update missed. Re-query IBKR fills.

Run at: session startup, every 30 min during market hours, after every Error 201.

━━━ SECTION 10: CONNECTIVITY DIAGNOSTICS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Connectivity check sequence:
  1. ib.isConnected() → True/False (socket level only)
  2. await ib.reqCurrentTimeAsync() → server timestamp; proves full TWS connectivity
  3. accounts = ib.managedAccounts() → non-empty means account data flowing
  4. Check error 2104/2106 — "Market data farm is OK" — normal startup noise.
     Suppress with IBKRNoiseFilter on ib_insync.wrapper / ib_insync.client loggers.

Connection failure modes:
  "Timeout" → IB Gateway/TWS not running, or wrong port. Check process is alive.
  "Connection refused" → firewall or wrong host. For local: host="127.0.0.1".
  "Already connected" → clientId reuse. Each IB() instance needs unique clientId.
  "getaddrinfo failed" → DNS issue. Use IP address not hostname.
  "No data" → connected but market data subscription missing.

IB Gateway vs TWS:
  TWS: GUI required, memory-heavy, not suitable for 24/7 operation.
  IB Gateway: headless, lower memory, designed for API use. USE THIS FOR PRODUCTION.
  API Configuration (both): File → Global Configuration → API → Settings →
    "Enable ActiveX and Socket Clients" → checked
    "Socket port": 7497 (paper) or 7496 (live)
    "Allow connections from localhost only": checked (security)
    "Master API client ID": 0 (leave default)

Auto-reconnect behavior (ib_insync):
  ib_insync handles disconnects internally via ib.disconnectedEvent.
  On reconnect: order state is preserved; market data subscriptions may need refresh.
  AGORA's stateless approach (new IB() per trade) means reconnect is implicit.

━━━ SECTION 11: AGORA-SPECIFIC EXECUTION FLOW ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Complete execution path for a new trade:
  1. ConvictionScorer produces ConvictionScore (total_score ≥ min_conviction_score)
  2. StrategyRulesEngine.build_recommendation() → TradeRecommendation with legs/pricing
     - Cost/width gate: if debit/width > 40%, skip (expensive spread filter)
     - R/R gate: if R/R < 0.10, skip (floor quality check)
  3. AgoraSession._submit_recommendation():
     a. EntryTimingGate.is_entry_permitted() (10 AM – 3:30 PM ET only)
     b. VIX stress: circuit breaker size reduction
     c. ComplianceAgent.check_trade() (wash sale, concentration, strategy levels)
     d. RiskCouncil.approve_trade() (max positions=4, delta limits, loss limits)
     e. exec_quality.record_attempt()
     f. ibkr_bridge.submit_trade() → IBKR_EXECUTOR → _run_in_new_loop → place_bracket_order
  4. ENTRY routing is SPREAD-TYPE-AWARE (ibkr_bridge.submit_trade):
       • paper CREDIT spread (entry<0) → place_legs_individually (riskless-combo 201 on a BAG)
       • debit spreads + ALL live      → place_bracket_order (atomic BAG)
     place_bracket_order():
     a. ib.connectAsync(host, port, clientId=ibkr_client_id, timeout=10)
     b. Qualify all option legs; build BAG with ComboLegs; compute strike width
     c. Price off IBKR market data (config.ibkr_market_data_type 1=live/3=delayed): net_mid +
        net_natural (per-leg, signed). Pricing-sanity + liquidity gates abort here too.
     d. Single entry LimitOrder at the NET MID (no GTC child, no Adaptive on a BAG)
     e. Repricing walk: step the net limit from the mid THROUGH the natural to the
        slippage-budget cap (max_slippage_pct_of_width × width) every ~12s, filling at the
        FIRST crossing (the combo "natural" alone often isn't a real combo-book price).
     f. On Filled: return net_fill_price (signed sum of leg fills)
     g. On unfilled at the budget cap: cancelOrder() → "Cancelled" (session arms 2h cooldown)
     h. On Error 201: "Cancelled" + error_code="201" (NO leg fallback in the BAG path).
  5. Session records outcome:
     - Filled: record_fill(net_fill_price, mid) + _record_position() + journal
     - Cancelled/Rejected: record_reject(); arm exec cooldown; reconcile if code=201

Leg-by-leg: place_legs_individually()
  Used for paper CREDIT spreads (a credit-spread BAG is hard-rejected with riskless-combo
  Error 201, not bypassable). Prices EACH leg off IBKR per-leg quotes (delayed/live) and
  walks each toward its marketable side; skips if a leg has no IBKR quote (it no longer
  stamps the net spread value onto every leg). Brief leg-gap risk is acceptable in paper.

━━━ SECTION 12: FILL RATE AND SLIPPAGE BENCHMARKS ━━━━━━━━━━━━━━━━━━━━━━━━━━━

Fill rate (per session, per day):
  > 90%: Excellent — mid-price strategy working well, liquid names, tight spreads
  70-90%: Good — normal for multi-leg options in most conditions
  50-70%: Warning — spreads too wide, market moving fast, or Error 201 cropping up
  < 50%: Critical — investigate immediately. Check Error 201, connectivity, pricing aggressiveness.

Slippage targets by tier (mid - fill_price):
  SPY/QQQ/AAPL/MSFT (Tier 1):     ≤ $0.05/share acceptable
  NVDA/TSLA/META/AMZN (Tier 2):   ≤ $0.10/share
  Mid-cap singles (Tier 3):        ≤ $0.20/share
  High-IV single names (>100% IV): ≤ $0.30/share (wide spreads expected)

Positive slippage (mid - fill > 0 = filled BETTER than mid): excellent, keep strategy.
Negative slippage (mid - fill < 0 = paid more than mid): investigate cause.

IBKR commission (estimate):
  Options: $0.65/contract/leg + exchange fees (~$0.15-0.30)
  Combo/BAG: $0.65 × (number of legs) × contracts
  For 2-leg spread, 1 contract: ~$1.30 + $0.30 = ~$1.60 total

━━━ SECTION 13: TROUBLESHOOTING PLAYBOOK ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SYMPTOM: Error 201 on every order attempt
  CAUSE:   GTC profit-target children from prior brackets consuming TWS combo slot
  FIX:     1. Run OrphanOrderReconciler.reconcile_now() immediately
           2. TWS → Global Config → Presets → Options → raise combo limit to 25
           3. Restart AGORA session after clearing orphans

SYMPTOM: fill_rate = 0%, all orders stay Submitted/PreSubmitted
  CAUSE:   Limit price too tight (too far from market), or market closed
  FIX:     Check if market is open (9:30-16:00 ET). If open, log bid/ask and mid.
           If spread is >$0.50 wide, limit may never reach ask. Enable more aggressive walk.

SYMPTOM: "Could not qualify contract" for specific ticker/strike
  CAUSE:   Strike doesn't exist on IBKR (penny increments vs $2.5/$5 increments)
  FIX:     Round strike to nearest valid increment for that underlying. Most stocks:
           < $25: $0.50 increments; $25-$200: $1.00 or $2.50; > $200: $5.00
           Some liquid names have $1.00 or $0.50 strikes. Query chain to confirm.

SYMPTOM: Position visible in IBKR TWS but not in AGORA dashboard
  CAUSE:   Manual trade outside AGORA, or AGORA session didn't receive Filled status
  FIX:     Run shadow-book reconciliation. If extra in IBKR: optionally add to DB manually.

SYMPTOM: Position in AGORA DB but no corresponding IBKR position
  CAUSE:   Ghost position from pre-fix ghost-position bug, or position closed manually
  FIX:     Query trade_journal for position_id to confirm entry was logged. If ghost:
           DELETE FROM positions WHERE position_id='...' AND status='open'

SYMPTOM: "No data" for reqHistoricalDataAsync
  CAUSE:   Market closed, wrong duration/barSize combo, or symbol delisted
  FIX:     Use "1 D" duration with "5 mins" barSize. Verify symbol is still trading.

SYMPTOM: AGORA connects briefly then disconnects
  CAUSE:   clientId conflict (two IB() instances using same ID)
  FIX:     Check _IBKR_EXECUTOR is single-threaded. Ensure no concurrent connections.
           Use separate clientIds for each connection type (see Section 1).

SYMPTOM: "Account DU..." not in managedAccounts()
  CAUSE:   Wrong port (paper vs live), or IB Gateway/TWS not fully started
  FIX:     Wait 30 seconds after starting IB Gateway before connecting. Check port.
"""

_CACHED_KNOWLEDGE_PROMPT = [
    {"type": "text", "text": _IBKR_KNOWLEDGE_SYSTEM, "cache_control": {"type": "ephemeral"}}
]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  IBKRKnowledgeAgent
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class IBKRKnowledgeAgent:
    """
    IBKR Expert Agent — sub-agent of COOAgent.

    Two modes:
      1. Background scan (every 30 min): polls live IBKR state and surfaces issues
      2. On-demand diagnose(question): Claude + knowledge base answers any IBKR question
    """

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        exec_quality: Any = None,
        orphan_reconciler: Any = None,
        position_mgr: Any = None,
        coo_agent: Any = None,
    ) -> None:
        self._settings    = settings or get_settings()
        self._eq          = exec_quality
        self._reconciler  = orphan_reconciler
        self._pm          = position_mgr
        self._coo         = coo_agent
        self._csuite_manager: Any = None  # COOAgent set via register_csuite_manager()

        self._client  = anthropic.AsyncAnthropic(api_key=self._settings.anthropic_api_key)
        self._running = False

        # Latest scan results — readable synchronously by COO.collect_intelligence()
        self._last_scan: dict[str, Any] = {}
        self._last_scan_time: datetime | None = None
        # Liveness from the 90s portfolio poller (clientId 18). A successful poll is positive proof
        # the TWS API accepts connections — i.e. orders CAN be submitted — even when the 30-min
        # _background_scan's _connection_healthy flag is stale (e.g. its last run landed inside a
        # transient outage). The COO uses this to avoid a false "orders cannot be submitted" alarm.
        self._last_portfolio_ok_time: datetime | None = None
        self._last_diagnosis: str = ""
        self._connection_healthy: bool = False
        self._open_order_count: int = 0
        self._orphan_count: int = 0
        self._error_201_count: int = 0
        self._last_recommendations: list[dict] = []  # active execution-param advice

    def register_csuite_manager(self, manager: Any) -> None:
        self._csuite_manager = manager

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        logger.info("IBKRKnowledgeAgent started (scan every 30 min)")
        # Dedicated lightweight portfolio-P&L poller — decoupled from the heavy 30-min scan so the
        # live TWS unrealizedPNL refreshes frequently and reliably (the scan can hang/slow; this
        # can't take it down). Runs concurrently on its own clientId.
        asyncio.create_task(self._portfolio_refresh_loop())
        await asyncio.sleep(30)  # let session stabilize before first scan
        while self._running:
            try:
                await self._background_scan()
            except Exception as exc:
                logger.error("IBKRKnowledgeAgent scan error: %s", exc)
            await asyncio.sleep(_SCAN_INTERVAL_SEC)

    async def _portfolio_refresh_loop(self) -> None:
        """Refresh IBKR's exact per-leg unrealized P&L on a short cadence, into the cache the API
        reads. Hard-timeout-guarded so a stuck IBKR call can never wedge the loop."""
        await asyncio.sleep(12)  # let the main connections settle first
        interval = int(getattr(self._settings, "ibkr_portfolio_refresh_secs", 90))
        while self._running:
            try:
                items = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(
                        _PORTFOLIO_EXECUTOR, lambda: _run_in_new_loop(self._fetch_portfolio_only())),
                    timeout=20,
                )
                if items is not None:
                    if self._last_scan is None:
                        self._last_scan = {}
                    self._last_scan["ibkr_portfolio_items"] = items
                    self._last_scan_time = datetime.now(tz=ET)
                    # A successful connect+fetch on the dedicated clientId is hard proof the TWS API
                    # is reachable and orders can flow — stamp it for the COO health check.
                    self._last_portfolio_ok_time = datetime.now(tz=ET)
            except Exception as exc:
                logger.debug("portfolio refresh failed: %s", exc)
            await asyncio.sleep(interval)

    async def _fetch_portfolio_only(self) -> list[dict] | None:
        """Minimal TWS portfolio fetch: connect on the dedicated clientId, pull IBKR's own per-leg
        unrealizedPNL/market price, disconnect. No LLM, no order/fill scans — fast + robust."""
        from ib_insync import IB
        ib = IB()
        cid = getattr(self._settings, "ibkr_portfolio_client_id", 18)
        try:
            await ib.connectAsync(self._settings.ibkr_host, self._settings.ibkr_port,
                                  clientId=cid, timeout=8)
            accounts = ib.managedAccounts()
            if not accounts:
                return None
            # CRITICAL: reqAccountUpdatesAsync HANGS on this TWS — it awaits an account-download-end
            # signal that never arrives (verified). But the SUBSCRIBE request still fires, and TWS
            # streams updatePortfolio events that populate ib.portfolio() regardless. So fire it with
            # a short timeout, ignore the (never-coming) completion, then poll portfolio().
            try:
                await asyncio.wait_for(ib.reqAccountUpdatesAsync(accounts[0]), timeout=2)
            except TimeoutError:
                pass
            items: list = []
            for _ in range(25):  # up to ~5s for IBKR's account push to land
                items = [it for it in ib.portfolio() if it.position]
                if items:
                    break
                await asyncio.sleep(0.2)
            return [
                {
                    "symbol":         it.contract.symbol,
                    "secType":        it.contract.secType,
                    "strike":         getattr(it.contract, "strike", None),
                    "right":          getattr(it.contract, "right", None),
                    "expiry":         getattr(it.contract, "lastTradeDateOrContractMonth", None),
                    "position":       it.position,
                    "market_price":   round(float(it.marketPrice or 0), 4),
                    "market_value":   round(float(it.marketValue or 0), 2),
                    "avg_cost":       round(float(it.averageCost or 0), 4),
                    "unrealized_pnl": round(float(it.unrealizedPNL or 0), 2),
                    "realized_pnl":   round(float(it.realizedPNL or 0), 2),
                    "account":        it.account,
                }
                for it in items[:50]
            ]
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

    async def stop(self) -> None:
        self._running = False

    # ── Public API ───────────────────────────────────────────────────

    async def diagnose(self, question: str, include_live_state: bool = True) -> str:
        """
        Ask the IBKR expert a question. Claude answers using its full knowledge base
        plus the current live IBKR state (connection, orders, error counts).

        Examples:
          await agent.diagnose("Why am I getting Error 201 on every bracket order?")
          await agent.diagnose("What's the best order type for a BE bear put spread?")
          await agent.diagnose("How do I fix the orphan GTC order problem?")
        """
        context = ""
        if include_live_state:
            state = self._last_scan or await self._collect_ibkr_state()
            import json
            context = f"\nCurrent IBKR state:\n{json.dumps(state, indent=2, default=str)}\n"

        prompt = (
            f"Question: {question}"
            f"{context}"
            "\nAnswer as the IBKR expert. Be specific and actionable. "
            "Reference exact IBKR settings, error codes, or code patterns as needed. "
            "Under 1000 characters unless a code example is required."
        )
        try:
            resp = await self._client.messages.create(
                model=self._settings.claude_model,
                max_tokens=1500,
                thinking={"type": "adaptive"},
                system=_CACHED_KNOWLEDGE_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            if hasattr(resp, "usage"):
                _log_msg(str(self._settings.db_path), "IBKRKnowledge", self._settings.claude_model,
                         resp.usage, purpose="ibkr_diagnosis")
            for block in reversed(resp.content):
                if hasattr(block, "text"):
                    result = block.text.strip()
                    self._last_diagnosis = result
                    return result
            return "[IBKRKnowledgeAgent: no text in response]"
        except Exception as exc:
            logger.error("IBKRKnowledgeAgent.diagnose failed: %s", exc)
            return f"[Diagnosis failed: {exc}]"

    async def assess_connectivity(self) -> dict[str, Any]:
        """Quick connectivity check — used by LiveReadinessMeter operations pillar."""
        try:
            state = await asyncio.get_event_loop().run_in_executor(
                _IBKR_EXECUTOR,
                lambda: _run_in_new_loop(self._connectivity_check()),
            )
            self._connection_healthy = state.get("connected", False)
            return state
        except Exception as exc:
            self._connection_healthy = False
            return {"connected": False, "error": str(exc)}

    def get_status(self) -> dict[str, Any]:
        """Synchronous snapshot for COO.collect_intelligence()."""
        portfolio_poll_age = (
            (datetime.now(tz=ET) - self._last_portfolio_ok_time).total_seconds()
            if self._last_portfolio_ok_time else None
        )
        return {
            "connection_healthy":  self._connection_healthy,
            "portfolio_poll_age_secs": portfolio_poll_age,
            "open_order_count":    self._open_order_count,
            "orphan_count":        self._orphan_count,
            "error_201_count":     self._error_201_count,
            "last_scan_time":      self._last_scan_time.isoformat() if self._last_scan_time else None,
            "last_scan":           self._last_scan,
            "last_diagnosis":      self._last_diagnosis[:300] if self._last_diagnosis else "",
            "execution_recommendations": self._last_recommendations,
        }

    def get_cached_portfolio(self) -> tuple[list[dict], float | None]:
        """
        Return (ibkr_portfolio_items, age_seconds) from the most recent background scan.
        Portfolio items carry IBKR's own unrealized_pnl, market_price, market_value.
        Returns ([], None) when no scan has run yet.
        """
        if not self._last_scan or "ibkr_portfolio_items" not in self._last_scan:
            return [], None
        items = self._last_scan["ibkr_portfolio_items"]
        age: float | None = None
        if self._last_scan_time:
            age = (datetime.now(tz=ET) - self._last_scan_time).total_seconds()
        return items, age

    # ── Active execution-parameter advisor ───────────────────────────
    # The expert doesn't just answer questions — it reads execution quality every
    # scan and recommends concrete parameter changes (shadow mode). This is the
    # "system recommends its own execution solutions" loop.

    _FILL_RATE_TARGET   = 0.70   # below this → loosen to fill more
    _FILL_RATE_HEALTHY  = 0.85   # above this → may tighten to recapture price
    _SLIPPAGE_TOLERANCE = 0.15   # paying worse than this/sh over mid → tighten
    _WIDTH_CAP_FLOOR    = 0.05
    _WIDTH_CAP_CEILING  = 0.25

    def evaluate_execution_params(self) -> dict[str, Any]:
        """
        Read execution-quality stats and emit concrete shadow-mode recommendations
        for the execution parameters (use_adaptive_algo, max_slippage_pct_of_width,
        upstream liquidity filtering). Deterministic rules — cheap, runs every scan.
        Recommendations are persisted to execution_advisor_journal and surfaced to
        the COO/dashboard; nothing is auto-applied.
        """
        if not self._eq:
            return {"available": False, "reason": "no execution_quality agent wired"}

        today = self._eq.get_today_db_stats()
        week  = self._eq.get_7day_stats()
        total_today = today.get("total", 0) or 0
        use = today if total_today >= 10 else week  # need a meaningful sample
        fill_rate    = use.get("fill_rate")
        timeout_rate = today.get("timeout_rate")
        avg_slippage = float(week.get("avg_slippage", 0.0) or 0.0)
        sample       = use.get("total", 0) or 0
        reject_reasons = week.get("reject_reasons", {}) or {}

        cur_cap      = float(getattr(self._settings, "max_slippage_pct_of_width", 0.10))
        cur_adaptive = bool(getattr(self._settings, "use_adaptive_algo", False))
        mode         = str(getattr(self._settings, "trading_mode", "paper"))
        recs: list[dict] = []

        # Paper-untrusted guard: IBKR's paper-sim structurally won't fill multi-leg combos
        # (single legs fill 12-20%, spreads ~1%), and combos dominate the attempt mix — so a low
        # aggregate fill rate in paper is a SIM ARTIFACT, not a slippage problem. Widening the
        # budget chasing it would bleed edge for nothing. So a paper widen is advisory-only
        # (trusted=False, never auto-applied); the real proving ground for spread fills is the
        # fill-realistic backtester. In live, fills are real, so a widen is trusted.
        widen_trusted = (mode != "paper")

        # 1) Adaptive must be OFF for combos (no-op that masks the repricing walk).
        if cur_adaptive:
            recs.append({
                "param": "use_adaptive_algo", "current": True, "suggested": False,
                "severity": "high",
                "reason": "Adaptive is silently ignored on BAG combos and disables the "
                          "repricing walk — it tanks fill rate. Turn OFF for combos.",
            })

        # 2) Low fill rate (timeout-dominated) → widen the slippage budget.
        if fill_rate is not None and fill_rate < self._FILL_RATE_TARGET and sample >= 10:
            if cur_cap < self._WIDTH_CAP_CEILING:
                suggested = round(min(self._WIDTH_CAP_CEILING, cur_cap + 0.05), 2)
                recs.append({
                    "param": "max_slippage_pct_of_width",
                    "current": cur_cap, "suggested": suggested, "severity": "high",
                    "trusted": widen_trusted,
                    "reason": f"Fill rate {fill_rate:.0%} < target {self._FILL_RATE_TARGET:.0%} "
                              f"on {sample} attempts — widen the walk so the net limit reaches "
                              f"the natural before the DAY order expires."
                              + ("" if widen_trusted
                                 else " [paper-sim untrusted — combo fills are an artifact; advisory only]"),
                })
            else:
                recs.append({
                    "param": "min_open_interest / bid_ask_max_pct",
                    "current": "cap at ceiling", "suggested": "tighten liquidity filter",
                    "severity": "medium",
                    "reason": f"Fill rate {fill_rate:.0%} still low at the cap ceiling "
                              f"({cur_cap:.2f}) — the spreads are too wide/illiquid to fill; "
                              f"filter them out upstream rather than overpay.",
                })

        # 3) Healthy fills but paying up → tighten to recapture entry price.
        if (fill_rate is not None and fill_rate >= self._FILL_RATE_HEALTHY
                and avg_slippage < -self._SLIPPAGE_TOLERANCE
                and cur_cap > self._WIDTH_CAP_FLOOR):
            suggested = round(max(self._WIDTH_CAP_FLOOR, cur_cap - 0.02), 2)
            recs.append({
                "param": "max_slippage_pct_of_width",
                "current": cur_cap, "suggested": suggested, "severity": "low",
                "trusted": True,   # tightening to recover edge is always safe (can't fake fills)
                "reason": f"Fills healthy ({fill_rate:.0%}) but avg slippage {avg_slippage:+.2f}/sh "
                          f"is costly — tighten the cap to recapture entry price.",
            })

        # 4) Error 201 → account/orphan action, not a pricing param.
        if reject_reasons.get("201"):
            recs.append({
                "param": "orphans / tws_combo_limit", "current": "201 seen",
                "suggested": "reconcile + verify TWS combo limit", "severity": "high",
                "reason": f"{reject_reasons['201']} Error-201 rejects in 7d — run "
                          f"OrphanOrderReconciler and confirm GTC children are gone.",
            })

        # 5) On DELAYED data with weak fills/slippage → live OPRA would tighten the anchor.
        md_type = int(getattr(self._settings, "ibkr_market_data_type", 3))
        weak = (fill_rate is not None and fill_rate < self._FILL_RATE_HEALTHY) or \
               (avg_slippage < -self._SLIPPAGE_TOLERANCE)
        if md_type == 3 and weak and sample >= 10:
            recs.append({
                "param": "ibkr_market_data_type", "current": 3, "suggested": 1,
                "severity": "low",
                "reason": "Execution prices off the 15-min DELAYED combo quote while fills/"
                          "slippage are soft. Real-time (non-pro OPRA ~$1.50/mo) would tighten "
                          "the walk's start mid + natural. Flip to 1 once OPRA is subscribed.",
            })

        # Trusted-gated auto-apply (promotion). When exec_advisor_autoapply is on, apply the
        # max_slippage_pct_of_width suggestion to the live setting — but ONLY if the rec is
        # trusted (paper widens are not). The order path reads settings live; the value is
        # in-memory and resets to the config default each restart. Bounded already by the
        # FLOOR/CEILING used to compute `suggested`. Set exec_advisor_autoapply=False for shadow.
        applied = None
        if bool(getattr(self._settings, "exec_advisor_autoapply", False)):
            for rec in recs:
                if rec["param"] == "max_slippage_pct_of_width" and rec.get("trusted", False):
                    try:
                        self._settings.max_slippage_pct_of_width = rec["suggested"]
                        applied = {"from": rec["current"], "to": rec["suggested"]}
                        logger.info(
                            "IBKRKnowledgeAgent[ACTIVE]: max_slippage_pct_of_width %.2f→%.2f (trusted)",
                            rec["current"], rec["suggested"],
                        )
                    except Exception as exc:
                        logger.warning("IBKRKnowledgeAgent: slippage apply failed: %s", exc)
                    break

        headline = (
            (f"fill={fill_rate:.0%} " if fill_rate is not None else "fill=n/a ")
            + (f"timeout={timeout_rate:.0%} " if timeout_rate is not None else "")
            + f"slip={avg_slippage:+.2f}/sh n={sample} | {len(recs)} rec(s)"
            + (f" | APPLIED {applied['from']}→{applied['to']}" if applied else "")
        )
        result = {
            "available": True, "fill_rate": fill_rate, "timeout_rate": timeout_rate,
            "avg_slippage": avg_slippage, "sample_size": sample,
            "recommendations": recs, "headline": headline,
            "shadow_mode": applied is None, "applied": applied,
        }
        self._last_recommendations = recs
        self._persist_recommendations(result)
        return result

    def _persist_recommendations(self, result: dict[str, Any]) -> None:
        try:
            conn = sqlite3.connect(str(self._settings.db_path), check_same_thread=False)
            conn.execute(
                """CREATE TABLE IF NOT EXISTS execution_advisor_journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    fill_rate REAL, timeout_rate REAL, avg_slippage REAL,
                    sample_size INTEGER, recommendations TEXT, headline TEXT,
                    shadow_mode INTEGER DEFAULT 1
                )"""
            )
            conn.execute(
                "INSERT INTO execution_advisor_journal "
                "(ts, fill_rate, timeout_rate, avg_slippage, sample_size, recommendations, headline, shadow_mode) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    datetime.now(tz=ET).isoformat(),
                    result.get("fill_rate"), result.get("timeout_rate"),
                    result.get("avg_slippage"), result.get("sample_size"),
                    json.dumps(result.get("recommendations", [])),
                    result.get("headline", ""),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.debug("persist execution recommendations failed: %s", exc)

    # ── Background scan ──────────────────────────────────────────────

    async def _background_scan(self) -> None:
        now_et = datetime.now(tz=ET)
        if not (8 <= now_et.hour < 20):
            return

        state = await self._collect_ibkr_state()
        self._last_scan = state
        self._last_scan_time = now_et

        # Update summary counters
        self._connection_healthy = state.get("connected", False)
        self._open_order_count   = state.get("open_order_count", 0)
        self._orphan_count       = state.get("orphan_gtc_count", 0)
        self._error_201_count    = state.get("error_201_session_count", 0)

        # Act on TWS fills: close shadow-book positions whose GTC target fired
        if self._pm and state.get("tws_fills"):
            synced = self._sync_closed_positions_from_fills(state["tws_fills"])
            if synced:
                state["positions_synced_from_fills"] = synced

        # Escalate critical findings to COO
        await self._evaluate_and_escalate(state)

        # Active execution-parameter advisor — recommend (shadow mode) and escalate.
        try:
            advice = self.evaluate_execution_params()
            if advice.get("available"):
                state["execution_advisor"] = advice
                highs = [r for r in advice.get("recommendations", [])
                         if r.get("severity") == "high"]
                if highs:
                    lines = "; ".join(
                        f"{r['param']}: {r['current']}→{r['suggested']}" for r in highs
                    )
                    await self._notify(
                        "warning",
                        f"🛠️ Execution advisor ({advice.get('headline', '')}) — "
                        f"recommend (shadow): {lines}",
                    )
        except Exception as exc:
            logger.debug("execution advisor evaluation failed: %s", exc)

    def _sync_closed_positions_from_fills(self, tws_fills: list[dict]) -> int:
        """
        Compare TWS BAG fills against shadow book. Mark positions closed where
        a SLD fill exists but the shadow book still shows status open.
        Called every 30 min by the background scan.
        """
        bag_sells = [f for f in tws_fills if f.get("secType") == "BAG" and f.get("action") == "SLD"]
        if not bag_sells:
            return 0

        open_by_ticker = {p.ticker: p for p in self._pm.get_open_positions()}
        synced = 0
        for fill in bag_sells:
            ticker = fill.get("symbol", "")
            pos = open_by_ticker.get(ticker)
            if pos is None:
                continue
            close_price  = float(fill.get("price", 0))
            realized_pnl = round((close_price - pos.entry_price) * 100 * pos.contracts, 2)
            closed = self._pm.mark_position_closed(
                position_id=pos.position_id,
                realized_pnl=realized_pnl,
                close_price=close_price,
                source="ibkr_knowledge_scan",
            )
            if closed:
                logger.info(
                    "IBKRKnowledgeAgent: synced close for %s fill=$%.4f pnl=$%.2f",
                    ticker, close_price, realized_pnl,
                )
                synced += 1
        return synced

    async def _collect_ibkr_state(self) -> dict[str, Any]:
        """Gather live IBKR state + session execution stats."""
        state: dict[str, Any] = {
            "collected_at":   datetime.now(tz=ET).isoformat(),
            "ibkr_port":      self._settings.ibkr_port,
            "trading_mode":   self._settings.trading_mode,
        }

        # Connectivity + open orders via live IBKR poll
        try:
            ibkr_live = await asyncio.get_event_loop().run_in_executor(
                _IBKR_EXECUTOR,
                lambda: _run_in_new_loop(self._ibkr_poll()),
            )
            state.update(ibkr_live)
        except Exception as exc:
            state["connected"] = False
            state["ibkr_poll_error"] = str(exc)

        # Execution quality stats (from DB)
        if self._eq:
            try:
                sess  = self._eq.get_session_stats()
                week  = self._eq.get_7day_stats()
                state["execution_session"] = {
                    "attempts":  sess.get("attempts", 0),
                    "fills":     sess.get("fills", 0),
                    "fill_rate": f"{sess.get('fill_rate', 0):.1%}",
                    "rejects":   sess.get("rejects", 0),
                    "reject_reasons": sess.get("reject_reasons", {}),
                    "error_201_storm": sess.get("error_201_storm", False),
                }
                state["execution_7day"] = {
                    "fill_rate":  f"{week.get('fill_rate', 0):.1%}",
                    "total":      week.get("total", 0),
                    "avg_slippage": week.get("avg_slippage", 0),
                }
                state["error_201_session_count"] = (
                    sess.get("reject_reasons", {}).get("201", 0)
                )
            except Exception as exc:
                state["execution_error"] = str(exc)

        # Orphan count from last reconciliation
        if self._reconciler:
            state["last_orphans_cancelled"] = self._reconciler.last_orphans_cancelled

        # Position count from shadow book
        if self._pm:
            try:
                state["shadow_book_positions"] = len(self._pm.get_open_positions())
            except Exception:
                pass

        return state

    async def _ibkr_poll(self) -> dict[str, Any]:
        """Live IBKR poll: connectivity + open orders (ALL clients) + positions."""
        try:
            from ib_insync import IB
        except ImportError:
            return {"connected": False, "error": "ib_insync not installed"}

        ib = IB()
        result: dict[str, Any] = {}
        try:
            await ib.connectAsync(
                self._settings.ibkr_host,
                self._settings.ibkr_port,
                clientId=getattr(self._settings, "ibkr_knowledge_client_id", 17),
                timeout=8,
            )
            result["connected"] = True
            result["managed_accounts"] = ib.managedAccounts()

            # reqAllOpenOrdersAsync fetches orders placed by ALL clientIds (not just ours).
            # Critical: AGORA submits orders with clientId=2; ib.openTrades() only returns
            # orders from this session's clientId=11 — always empty without this call.
            all_trades = await ib.reqAllOpenOrdersAsync()
            result["open_order_count"] = len(all_trades)

            # GTC profit-target children — these consume TWS combo slots (Error 201 risk)
            gtc_orders = [t for t in all_trades if t.order.tif == "GTC"]
            result["orphan_gtc_count"] = len(gtc_orders)
            result["gtc_order_details"] = [
                {
                    "orderId":  t.order.orderId,
                    "parentId": t.order.parentId,
                    "symbol":   t.contract.symbol,
                    "secType":  t.contract.secType,
                    "action":   t.order.action,
                    "orderType": t.order.orderType,
                    "tif":      t.order.tif,
                    "status":   t.orderStatus.status,
                    "lmtPrice": t.order.lmtPrice,
                    "qty":      t.order.totalQuantity,
                    "filled":   t.orderStatus.filled,
                    "remaining": t.orderStatus.remaining,
                }
                for t in all_trades[:20]  # cap at 20
            ]

            # reqPositionsAsync subscribes to position updates and waits for the initial
            # data push. Give ib_insync a moment to process the incoming position events
            # before calling ib.positions() — paper accounts can be slow to respond.
            await ib.reqPositionsAsync()
            await asyncio.sleep(1.5)
            positions = ib.positions()
            opt_positions = [p for p in positions if p.contract.secType == "OPT"]
            result["ibkr_option_positions"] = len(opt_positions)
            result["ibkr_position_symbols"] = list({p.contract.symbol for p in opt_positions})
            result["ibkr_positions_detail"] = [
                {
                    "symbol":    p.contract.symbol,
                    "secType":   p.contract.secType,
                    "strike":    getattr(p.contract, "strike", None),
                    "right":     getattr(p.contract, "right", None),
                    "expiry":    getattr(p.contract, "lastTradeDateOrContractMonth", None),
                    "position":  p.position,
                    "avg_cost":  round(p.avgCost, 4),
                    "account":   p.account,
                }
                for p in positions[:30]
            ]

            # Portfolio items — IBKR's own unrealized P&L, market price, and market value
            # per position. reqAccountUpdatesAsync triggers a one-shot account push so
            # ib.portfolio() is populated before we read it.
            accounts = result.get("managed_accounts") or ib.managedAccounts()
            if accounts:
                try:
                    # reqAccountUpdatesAsync HANGS on this TWS (awaits an account-download-end that
                    # never arrives) — fire it with a short timeout and ignore completion; the
                    # subscribe still streams updatePortfolio events that populate ib.portfolio().
                    # (The old subscribe=/account= kwargs ALSO raised TypeError, swallowed here, which
                    # is why ibkr_portfolio_items was always empty → yfinance fallback.)
                    try:
                        await asyncio.wait_for(ib.reqAccountUpdatesAsync(accounts[0]), timeout=2)
                    except TimeoutError:
                        pass
                    # Poll until the push populates ib.portfolio() — break on first live position.
                    portfolio_items: list = []
                    for _ in range(20):
                        portfolio_items = [it for it in ib.portfolio() if it.position]
                        if portfolio_items:
                            break
                        await asyncio.sleep(0.2)
                    result["ibkr_portfolio_fetched_at"] = datetime.now(tz=ET).isoformat()
                    result["ibkr_portfolio_items"] = [
                        {
                            "symbol":         item.contract.symbol,
                            "secType":        item.contract.secType,
                            "strike":         getattr(item.contract, "strike", None),
                            "right":          getattr(item.contract, "right", None),
                            "expiry":         getattr(item.contract, "lastTradeDateOrContractMonth", None),
                            "position":       item.position,
                            "market_price":   round(float(item.marketPrice or 0), 4),
                            "market_value":   round(float(item.marketValue or 0), 2),
                            "avg_cost":       round(float(item.averageCost or 0), 4),
                            "unrealized_pnl": round(float(item.unrealizedPNL or 0), 2),
                            "realized_pnl":   round(float(item.realizedPNL or 0), 2),
                            "account":        item.account,
                        }
                        for item in portfolio_items[:50]
                    ]
                except Exception as exc:
                    result["ibkr_portfolio_items"] = []
                    result["ibkr_portfolio_error"] = str(exc)

            # Today's executions (fills) from TWS — reqExecutionsAsync fetches all fills
            # for the current session day, regardless of which clientId placed the order.
            fills = await ib.reqExecutionsAsync()
            result["tws_fills"] = [
                {
                    "symbol":    f.contract.symbol,
                    "secType":   f.contract.secType,
                    "action":    f.execution.side,          # "BOT" or "SLD"
                    "shares":    f.execution.shares,
                    "price":     round(f.execution.price, 4),
                    "time":      f.execution.time.isoformat() if f.execution.time else None,
                    "exec_id":   f.execution.execId,
                    "order_id":  f.execution.orderId,
                    "exchange":  f.execution.exchange,
                    "commission": round(getattr(f.commissionReport, "commission", 0) or 0, 4),
                }
                for f in fills[:50]
            ]
            result["tws_fill_count"] = len(fills)

        except Exception as exc:
            result["connected"] = False
            result["connection_error"] = str(exc)
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

        return result

    async def _connectivity_check(self) -> dict[str, Any]:
        """Minimal check: just verify TCP connect + managed accounts."""
        try:
            from ib_insync import IB
        except ImportError:
            return {"connected": False, "error": "ib_insync not installed"}

        ib = IB()
        try:
            await ib.connectAsync(
                self._settings.ibkr_host,
                self._settings.ibkr_port,
                clientId=getattr(self._settings, "ibkr_knowledge_client_id", 17),
                timeout=6,
            )
            accounts = ib.managedAccounts()
            return {"connected": True, "accounts": accounts}
        except Exception as exc:
            return {"connected": False, "error": str(exc)}
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

    async def _evaluate_and_escalate(self, state: dict[str, Any]) -> None:
        """Check state for critical conditions and alert COO."""
        issues: list[tuple[str, str]] = []

        if not state.get("connected", False):
            err = state.get("connection_error", "unknown")
            issues.append(("critical", f"🚨 IBKR not reachable: {err}"))

        exec_s = state.get("execution_session", {})
        if exec_s.get("error_201_storm"):
            count = exec_s.get("reject_reasons", {}).get("201", 0)
            issues.append(("critical",
                f"🚨 Error 201 storm: {count} rejections — orphan GTC orders blocking "
                f"new brackets. Run OrphanOrderReconciler now. "
                f"Check TWS Precautionary Settings: raise combo order limit to 25."))

        orphan_count = state.get("orphan_gtc_count", 0)
        if orphan_count >= 3:
            issues.append(("warning",
                f"⚠️ {orphan_count} open GTC orders in IBKR. Risk of Error 201. "
                f"Verify all have matching DB positions."))

        fill_rate_str = exec_s.get("fill_rate", "100%")
        attempts = exec_s.get("attempts", 0)
        if attempts >= 5:
            fill_rate = float(fill_rate_str.rstrip("%")) / 100
            if fill_rate < 0.40:
                issues.append(("warning",
                    f"⚠️ Fill rate critically low: {fill_rate_str} on {attempts} attempts. "
                    f"Check limit pricing aggressiveness and connectivity."))

        shadow = state.get("shadow_book_positions", 0)
        ibkr_opts = state.get("ibkr_option_positions", 0)
        if shadow > 0 and ibkr_opts == 0 and state.get("connected"):
            issues.append(("warning",
                f"⚠️ Shadow book has {shadow} positions, IBKR shows 0 option positions. "
                f"Possible ghost positions in DB. Run reconciliation."))

        for level, message in issues:
            await self._notify(level, message)

    async def _notify(self, level: str, message: str) -> None:
        if self._csuite_manager:
            await self._csuite_manager.receive_alert("IBKRKnowledgeAgent", level, message)
        elif self._coo:
            await self._coo.receive_alert("IBKRKnowledgeAgent", level, message)
        else:
            logger.warning("IBKRKnowledgeAgent [%s]: %s", level, message[:200])
