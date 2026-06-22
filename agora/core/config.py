"""
AGORA configuration — extends the shared trading_platform Settings.
All AGORA-specific settings live here; shared settings are imported directly.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AgoraSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Anthropic ──────────────────────────────────────────────────
    anthropic_api_key: str = Field(..., description="Anthropic API key")
    claude_model: str = Field(
        default="claude-sonnet-4-6",
        description="Primary model — Sonnet 4.6 for paper/dev; set CLAUDE_MODEL=claude-opus-4-8 in .env for production",
    )
    claude_fast_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Fast model for high-frequency classification tasks",
    )
    claude_brief_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Haiku for oversight/summary work (C-suite briefs, sector/earnings/"
                    "premarket/system-health summaries) — these don't make trade decisions, "
                    "so the ~3x cheaper model is right-sized. Decision agents use claude_model.",
    )

    # ── Trading ────────────────────────────────────────────────────
    trading_mode: Literal["paper", "live"] = Field(default="paper")
    account_size: float = Field(default=25_000.0, ge=1_000.0)
    # Upper bounds widened to support "free paper" data-collection mode (caps set via
    # .env); the conservative production defaults are unchanged. Re-tighten le once
    # enough paper data exists to choose real caps.
    max_position_size_pct: float = Field(default=0.02, ge=0.005, le=1.0)
    max_open_positions: int = Field(default=4, ge=1, le=1000)
    max_per_correlation_group: int = Field(default=1, ge=1, le=1000)
    # Scanner review #3 — GLOBAL exposure ceiling across BOTH pipelines. The spread and
    # long-options loops each had their own count cap (max_open_positions /
    # long_options_max_positions) with no single bound on the SUM, so on a strong-signal day
    # both could load up at once. These two knobs are the one ceiling both pipelines consult
    # before submit. Defaults are intentionally non-binding (matches the current data-collection
    # "caps lifted" posture); tighten max_total_capital_deployed_pct to bound real exposure.
    max_total_open_positions: int = Field(default=1000, ge=1, le=10000)
    max_total_capital_deployed_pct: float = Field(
        default=1.0, ge=0.05, le=1.0,
        description="Max fraction of account_size deployed (Σ max_loss across all open "
                    "positions, both pipelines) before any new entry is blocked. 1.0 = off.",
    )
    daily_loss_limit_pct: float = Field(default=0.02, ge=0.005, le=0.25)
    weekly_loss_limit_pct: float = Field(default=0.06, ge=0.01, le=0.20)
    paper_disable_loss_breakers: bool = Field(
        default=False,
        description="PAPER MODE ONLY: skip the daily/weekly loss-limit halts so the engine keeps "
                    "running for data collection. DOUBLE-GUARDED — it has effect ONLY when "
                    "trading_mode=='paper'; in live the loss breakers are ALWAYS enforced.",
    )
    min_rr_ratio: float = Field(default=1.3, ge=0.5)
    min_credit_spread_rr_ratio: float = Field(
        default=0.10,
        description="Minimum R/R for credit spreads (collect ≥10% of spread width as premium). "
                    "Matches the rules_engine floor; credit spreads structurally have R/R < 1. "
                    "TLT at 11.5% IV yields R/R≈0.12; debit-spread threshold of 1.3 blocks all vol-premium trades.",
    )
    min_long_option_rr_ratio: float = Field(
        default=0.8,
        ge=0.4,
        description="Minimum R/R for LONG options (single-leg debit). For a long option, "
                    "reward_risk_ratio = exit profit-target%% ÷ stop%% — NOT a spread's reward-vs-"
                    "defined-risk. With a 50%% stop, conviction-2/3 setups yield 0.80/1.00, so the "
                    "spread floor of 1.30 (only reachable at conviction ≥4) categorically blocked "
                    "every long option after gate-parity was added 2026-06-03. The real long-option "
                    "edge is directional win-rate × unbounded asymmetric payoff, already gated by "
                    "conviction/delta/IVR/devils-advocate — so this floor only screens out the "
                    "weakest exit-policy skew, not the trade thesis.",
    )
    max_debit_to_width_ratio: float = Field(
        default=0.40,
        description="Max fraction of spread width acceptable as net debit (e.g. 0.40 = 40%). "
                    "Rejects expensive debit spreads where cost erodes expected value.",
    )

    # ── IBKR ───────────────────────────────────────────────────────
    ibkr_host: str = Field(default="127.0.0.1")
    ibkr_port: int = Field(default=7497)
    ibkr_client_id: int = Field(default=10)  # separate from APEX (client 1)
    ibkr_news_client_id: int = Field(
        default=4,
        description="clientId for IBKRNewsAgent (must differ from ibkr_client_id and startup_tws_sync_client_id).",
    )
    news_max_tickers: int = Field(
        default=40,
        description="Cap on real-time IBKR news subscriptions. Each consumes one of the account's "
        "~100 simultaneous market-data lines (Error 101 'Max number of tickers'); subscribing the "
        "whole universe (112) blew the cap AND starved the option-data feed (enrich/IV). 40 keeps "
        "news on the most-liquid names while leaving ~60 lines for transient option-data requests.",
    )
    ibkr_knowledge_client_id: int = Field(
        default=17,
        description="Dedicated clientId for IBKRKnowledgeAgent scans incl. the portfolio-P&L fetch. "
        "MUST differ from fundamental_data's clientId 13 — sharing 13 caused Error 326 collisions "
        "that left ib.portfolio() unread (TWS P&L never populated). 17 is clear of the 10-16 cluster.",
    )
    ibkr_portfolio_client_id: int = Field(
        default=18,
        description="Dedicated clientId for the lightweight TWS portfolio-P&L poller (separate from "
        "the heavy 30-min knowledge scan on 17, so a frequent poll never collides with it). "
        "Sources IBKR's exact per-leg unrealizedPNL for the live P&L display.",
    )
    ibkr_portfolio_refresh_secs: int = Field(
        default=60,
        description="How often the dedicated portfolio poller refreshes TWS unrealized P&L. Aligned "
        "with the 60s position-lifecycle loop so exit decisions act on TWS P&L <=60s old. Fast moves "
        "are caught separately by the event-driven shock detector.",
    )

    # ── IBKR GTC / order lifecycle ─────────────────────────────────
    gtc_max_concurrent: int = Field(
        default=20,
        description="Alert if more than this many GTC orders are live (Error 201 risk). "
                    "Set this in TWS Precautionary Settings → Options → max combo orders.",
    )
    gtc_max_open_combo_orders: int = Field(
        default=3,
        description="Hard gate: do not submit a new combo bracket if this many positions already "
                    "have active GTC profit-target orders. IBKR paper enforces a ~3-order limit. "
                    "Increase once TWS Precautionary Settings → max combo orders is raised.",
    )
    gtc_fill_sync_interval_sec: int = Field(
        default=1800,
        description="How often (seconds) IBKRKnowledgeAgent scans TWS fills to sync closed positions.",
    )
    startup_tws_sync_client_id: int = Field(
        default=12,
        description="clientId used by the startup TWS fill-sync connection (must not conflict with others).",
    )

    # ── Strategy parameters ────────────────────────────────────────
    target_dte_entry: int = Field(default=45, description="Target DTE at entry")
    target_dte_entry_min: int = Field(default=30, description="Minimum acceptable DTE at entry")
    target_dte_close: int = Field(default=21, description="Close position at this DTE")
    earnings_blackout_days: int = Field(default=3, description="Block new vol-premium entries within N days of earnings")
    evaluate_ticker_cooldown_secs: int = Field(default=900, ge=0, description="Skip re-running the full spread eval stack (analyst+strategy+advocate LLM) on the same ticker within N secs for BACKGROUND scans. 0 disables. Cuts the ~11x/session redundant LLM spend.")
    urgent_eval_cooldown_secs: int = Field(default=180, ge=0, description="LLM-spend audit (2026-06-22): LIGHTER eval cooldown for URGENT/NORMAL scans (price-move + aging promotions) — was a FULL bypass, so on a broad volatile day dozens of names crossing the move threshold each re-fired the full analyst+selector+advocate LLM stack every sweep with no throttle (the primary LLM-storm vector that drained the budget). IMMEDIATE (discrete catalyst / position-under-stress) still bypasses. 0 = revert URGENT/NORMAL to full bypass.")
    # ── Expectancy meter + legacy-data cutoff (S0.4) ──────────────────────────
    # Trades closed before the day-0/1 churn fix landed are mechanically-broken and would condemn
    # now-repaired cells. The post-fix expectancy view + the cell-gate exclude everything before
    # this date. Set to the date the min-hold/require-kill guards went live.
    expectancy_legacy_cutoff_date: str = Field(default="2026-06-12", description="Exclude trades closed before this date from the post-fix expectancy view + cell gate (pre-churn-fix legacy).")
    expectancy_upgrade_milestone_date: str = Field(default="2026-06-22", description="The date the IBKR decision-data layer (real IV/prices/greeks) + W1 33-delta credit spreads went live. Trades closed on/after this date are the clean 'upgraded system' window — the expectancy that decides go-live, isolated from the older yfinance-era trades.")
    expectancy_target_per_trade: float = Field(default=25.0, description="Target expectancy ($/trade) the whole system thrives toward — shown on the dashboard meter.")
    expectancy_target_date: str = Field(default="2026-09-30", description="Date the expectancy target is 'locked' for — drives the meter countdown/ETA.")
    # ── Tier 1 — stop the bleed (reversible, paper-gated) ─────────────────────
    # S1.1 long-options DTE floor: median entry was 15 DTE (theta knife). Lift the window out of the
    # worst decay zone so the directional thesis has room. Was the hardcoded _DTE_MIN/MAX 14/30.
    long_options_min_dte: int = Field(default=21, ge=7, description="S1.1: minimum DTE for a long-option entry (raised off the 14-DTE theta knife).")
    long_options_max_dte: int = Field(default=35, ge=14, description="S1.1: maximum DTE for a long-option entry.")
    # S1.2 credit spreads only sell premium when IV is rich — no edge selling cheap vol.
    credit_spread_min_ivr: float = Field(default=50.0, ge=0, le=100, description="S1.2: block credit-spread entries when IV-rank < this (only sell rich premium). 0 disables.")
    # S1.3 expectancy cell-gate: bench any strategy×pillar cell with a proven-negative post-fix
    # expectancy; auto-reopens when it recovers. Uses post-fix data only (excludes legacy churn).
    cell_gate_enabled: bool = Field(default=True, description="S1.3: suppress new entries in strategy×pillar cells with proven-negative post-fix expectancy.")
    cell_gate_min_samples: int = Field(default=8, ge=1, description="S1.3: a cell needs at least this many post-fix closes before it can be benched.")
    cell_gate_min_expectancy: float = Field(default=-15.0, description="S1.3: bench a cell when its post-fix expectancy ($/trade) is below this.")
    # ── Tier 2 — raise payoff / concentrate edge ──────────────────────────────
    # S2.1: a bearish DEBIT (long_put / bear_put_spread) in a CONFIRMED risk-on tape fights positive
    # drift + theta — the directional pillar's −$2,476 bleed. Block it; bearish premium-selling
    # (bear_call, IVR-gated) and the long_call side are unaffected.
    block_bearish_debit_in_risk_on: bool = Field(default=True, description="S2.1: block bearish debit entries when macro is confirmed risk-on.")
    block_bearish_debit_min_confidence: float = Field(default=0.60, ge=0, le=1, description="S2.1: only block when risk-on confidence is at least this (avoid blocking on an ambiguous regime).")
    # S2.2 (conviction-scaled trailing), S2.3 (combo-liquidity prescreen) already exist + on.
    # S2.4 expectancy-weighted sizing reuses the existing edge_size_multiplier — flipped ON below
    # (it ONLY sizes DOWN proven-negative cells, so it is purely protective; the "enable after
    # win≥55%" caveat was about UP-sizing, which it never does, and exits are now fixed).
    profit_target_pct: float = Field(default=0.50, description="Close at 50% of max profit for 45-DTE vol-premium trades (tastytrade-validated for 30-60 DTE)")
    profit_target_pct_short_dte: float = Field(default=0.75, description="Close at 75% of max profit for short-DTE trades (sector_momentum, event plays ≤14 DTE) — backtested: 75% saves $1,230 vs 50% over 2.4yr")
    short_delta_target: float = Field(default=0.20, description="20-delta short — used by the debit verticals' short leg (bull_call/bear_put) and the iron-condor wings. Credit verticals use credit_spread_short_delta instead (see W1).")
    credit_spread_short_delta: float = Field(default=0.35, description="W1d (2026-06-22, BOARD RULING): ~35-delta short for the CREDIT verticals only (bull_put/bear_call). History: 20Δ→cr/w 0.08-0.13 (far below the 0.30 EV gate); W1 30Δ + W1b adaptive-narrowing lifted it to 0.20-0.29; W1c 33Δ to 0.28-0.29 — STILL clustering just under 0.30, so credit spreads were submitted ZERO times (verified 06-22 execution_quality). The board (options/quant/CRO/COO) ruled 35Δ: it pushes cr/w over 0.30 into positive-EV, collects rich premium in the current high-IV regime (IVR 74-100), and nearer-ATM legs fill tighter. Costs ~2pts POP (67%→65%) — immaterial vs getting the edge driver to actually TRADE so it can be measured. Guards held: the 0.30 cr/w gate still cannot admit a negative-EV spread; defined-risk sizing; cell-gate auto-benches if it proves negative over a real sample; tracked in the post-upgrade window. Scoped OFF debit verticals + iron condor (cr/w-exempt).")
    long_delta_target: float = Field(default=0.35, description="35-delta long strike for debit spreads")

    # ── Signal thresholds ──────────────────────────────────────────
    ivr_bypass_threshold: float = Field(
        default=60.0,
        description="Minimum IV rank (0-100) required to activate vol-premium bypass",
    )
    iv_premium_threshold: float = Field(
        default=0.25,
        description="IV_implied_vs_realized ratio threshold to sell premium",
    )
    iv_premium_min_days: int = Field(
        default=15,
        description="Minimum consecutive days IV premium must hold before signal fires",
    )
    gex_negative_threshold: float = Field(
        default=-1_000_000.0,
        description="GEX below this is considered 'negative' (trending/amplifying regime)",
    )
    min_conviction_score: float = Field(default=60.0, description="Minimum score to enter any trade")
    vol_premium_conviction_floor: float = Field(default=50.0, description="Lower conviction floor for non-directional vol-premium plays (IVR bypass path)")
    high_ivr_threshold: float = Field(default=90.0, description="IVR at or above this is 'extreme' — IV compression risk is highest, requires full conviction floor")
    high_ivr_conviction_floor: float = Field(default=55.0, description="Conviction floor when IVR >= high_ivr_threshold; overrides the lower vol_premium_conviction_floor")
    high_conviction_score: float = Field(default=80.0, description="Score for 1.5x size multiplier")
    disagreement_resolver_floor: float = Field(default=40.0, description="Hard no-trade floor in DisagreementResolver. Lower for paper-mode validation.")
    min_credit_per_share: float = Field(default=0.50, description="Minimum credit collected per share for credit spreads. $0.50 avoids IBKR leg rejections in live; lower in paper mode.")
    min_credit_to_width_ratio: float = Field(default=0.30, ge=0.0, le=0.6, description="Binding edge gate for credit verticals (bull_put/bear_call): reject any spread whose credit/width is below this. Sub-0.30 credit spreads are negative-EV by construction (17/17 historical losers were 0.13-0.23; winner 0.61). Iron condors exempt.")
    block_bull_put_in_risk_off: bool = Field(default=True, description="Regime gate: in a risk_off macro, build a BEARISH credit spread (bear_call) instead of defaulting VOL_PREMIUM to a bullish bull_put — sell premium WITH the trend. Source-level complement to the StrategySelector's bull_put->bear_call flip.")
    # ── Credit-spread stop grace (THE expectancy lever) ─────────────────────────
    spread_stop_min_hold_days: int = Field(
        default=3, ge=0, le=10,
        description="Grace window (days) before the 2x-credit HARD STOP may fire on a CREDIT spread. "
                    "Root finding: credit spreads (the biggest cohort, 18 closes) won only 6% vs a "
                    "~70% theta-trade norm — because the 2x-credit hard stop tripped on DAY-1 mark "
                    "noise (bid-ask/natural mark, not real loss; 16/18 closed at exactly 1.0d at a "
                    "loss, all entered at 38-42 DTE). Credit spreads are theta trades + DEFINED-RISK, "
                    "so they must ride day-1 noise. Within the grace, the hard stop is suppressed "
                    "UNLESS a genuine blowout (see spread_stop_blowout_max_loss_frac) — which is the "
                    "only real defined risk. Mirrors spread_exit_llm_min_hold_days=3. THE highest-"
                    "leverage expectancy fix; tune up if win rate is still suppressed.")
    spread_stop_blowout_max_loss_frac: float = Field(
        default=0.85, ge=0.5, le=1.0,
        description="During the credit-spread stop grace, a loss at/under this fraction of max_loss "
                    "still hard-stops immediately (a genuine adverse move, not mark noise). 0.85 = "
                    "stop if within 15% of max defined loss; otherwise hold for theta.")
    # ── Edge-aware sizing (C-suite rank 12 — SHIPPED DARK) ───────────────────────
    edge_sizing_enabled: bool = Field(default=True, description="S2.4: scale position size by a (pillar,regime) cell's real-fill Sharpe (StrategyHealth). ONLY ever sizes DOWN (capped at 1.0) → purely protective. Enabled 2026-06-18 with Tier 2: exits are now fixed and down-only sizing carries no up-sizing risk; ramps with data (needs >=edge_min_sample closes/cell).")
    edge_size_up_max: float = Field(default=1.0, ge=1.0, le=2.0, description="Hard cap on the edge multiplier — PINNED at 1.0 so no subset is ever sized UP on unproven edge. Raise only with proven positive edge.")
    edge_size_down_min: float = Field(default=0.5, ge=0.1, le=1.0, description="Floor for sizing DOWN a negative-edge cell.")
    edge_min_sample: int = Field(default=30, ge=10, le=200, description="Min real closes in a (pillar,regime) cell before edge sizing acts; below this the multiplier is neutral (1.0).")
    force_vol_selling_ok: bool = Field(default=False, description="Paper-mode override: bypass MacroContext.vol_selling_ok=False gate. Lets credit spreads through when IVR/VIX are just below threshold.")
    long_loop_parallel_enabled: bool = Field(
        default=False,
        description="Process the long-options universe scan with a bounded worker pool "
                    "(concurrent coroutines) instead of sequentially. Default OFF — turn ON "
                    "when a long-scan cycle measurably approaches the scan interval. The worker "
                    "pool size below doubles as the LLM-burst cap (each worker makes <=1 "
                    "vetter+advocate call at a time). Safeguards: atomic position-cap, per-worker "
                    "isolation, yf_gate on data, vetter retry.",
    )
    long_loop_max_concurrency: int = Field(
        default=4,
        description="Bounded worker-pool size for the parallel long-options scan; also caps "
                    "concurrent Opus-vetter/advocate calls (cost stays flat, bursts controlled).",
        ge=1, le=12,
    )
    event_surgical_gate_enabled: bool = Field(
        default=True,
        description="Surgical macro-event (FOMC/CPI/NFP) handling: instead of blanket-blocking "
                    "near events, hard-block only the event DAY, size-down event-adjacent entries, "
                    "require short-strike cushion >= 1.25x expected move for credit spreads, and feed "
                    "the advocate ACCURATE quantified event context so it stops over-blocking on "
                    "mis-identified/assumed events. Set false to revert to blanket behavior.",
    )
    event_gate_intraday_reopen: bool = Field(
        default=True,
        description="C-lite event-day refinement: PRE-MARKET events (NFP/CPI/PPI, ~8:30 ET) "
                    "resolve before the open, so instead of blocking the whole day, block only "
                    "until the settle window below, then ALLOW entries (size-reduced + tagged "
                    "event_day) to ride the post-event momentum — the strategy's core edge. "
                    "INTRADAY events (FOMC, ~14:00 ET + presser) stay blocked ALL day regardless "
                    "(violent reversal risk). Set false to revert to all-day block on every event.",
    )
    event_gate_settle_et: str = Field(
        default="10:00",
        description="ET time (HH:MM) after which new entries reopen on a PRE-MARKET event day "
                    "(NFP/CPI prints at 8:30; ~90 min lets the open settle). Entries before this "
                    "time, and FOMC days entirely, remain blocked.",
    )

    # ── Universe ───────────────────────────────────────────────────
    etf_universe: list[str] = Field(
        default=[
            # Broad market ETFs
            "SPY", "QQQ", "IWM", "DIA", "EEM", "GLD", "TLT", "SLV", "COPX", "PPLT",
            # Sector ETFs (diversification — added 2026-06-04; liquid baskets replace the
            # small-cap single-name bleed, e.g. SMH instead of HIMX/AMKR)
            "SMH", "XLK", "XLF", "XLE", "XLV", "XBI",
            # Mega-cap tech
            "AAPL", "MSFT", "NVDA", "META", "AMZN", "GOOGL", "TSLA", "AVGO", "AMD",
            "NFLX", "CRM",
            # Semiconductors + equipment (liquid only — pruned small-cap losers HIMX/AMKR)
            "TSM", "MU", "INTC", "TXN", "LRCX", "ASML", "SMTC", "TSEM", "VECO",
            "MRVL", "QCOM", "AMAT", "KLAC", "ARM",
            # Memory / storage cycle (DRAM/NAND/flash + HDD — trade the memory pricing cycle)
            "SNDK", "STX", "WDC",
            # Networking (AI data-center connectivity)
            "CSCO", "ANET",
            # Cyber-security (mega-cap, liquid)
            "PANW", "CRWD", "FTNT",
            # Hardware / OEM / servers (AI infra buildout)
            "DELL", "HPE", "HPQ",
            # Defense / space
            "PLTR", "KTOS", "AVAV", "RKLB",
            # Energy / power (pruned proven losers FCEL/FLNC; added liquid XOM)
            "XOM", "VST", "CEG", "NEE", "BE", "AMSC", "CCJ",
            # Finance / brokers
            "JPM", "SCHW", "HOOD", "CBOE",
            # Large-cap diversified
            "COST", "WMT", "KO", "CAT", "ORCL", "MSI", "JBL", "SANM",
            # Biotech / healthcare
            "LLY", "JNJ", "ABT", "BSX", "BIIB", "MDT",
            # Cloud / software
            "SNOW", "ZS", "UPST",
            # Liquid high-beta / momentum (added — liquid options despite volatility)
            "COIN", "MARA", "SMCI",
            # Optical / photonics (AI optics; pruned proven loser AOSL; kept AAOI/AXTI per owner)
            "LITE", "COHR", "FN", "CIEN", "GLW", "IPGP", "MKSI", "AAOI", "AXTI",
            # Other watchlist (kept OKLO per owner; WDC moved to memory/storage group)
            "FLEX", "MSTR", "IREN", "NOK", "OKLO", "GEV",
            "MP", "POWL", "KEYS", "ONTO", "SERV", "VICR",
            "BJ", "F", "CIFR", "ASTS",
        ],
        description="Options universe for vol premium credit spreads and event plays. "
                    "Liquidity-tilted + sector-diversified (2026-06-04) after edge-by-liquidity "
                    "analysis showed positive edge in liquid names, negative in the small-cap tail.",
    )
    single_name_min_market_cap: float = Field(
        default=300_000_000.0,
        description="Minimum market cap for single-name catalyst plays",
    )
    single_name_max_market_cap: float = Field(
        default=10_000_000_000.0,
        description="Max market cap — above this whales compete, edge shrinks",
    )

    # ── Catalyst discovery ─────────────────────────────────────────
    edgar_poll_seconds: int = Field(default=60, description="How often to poll EDGAR RSS (60s for real-time 8-K/13D detection)")
    max_new_tickers_per_day: int = Field(default=5, description="Cap on catalyst-discovered tickers/day")
    min_contract_value_usd: float = Field(default=50_000_000.0)
    min_funding_round_usd: float = Field(default=25_000_000.0)
    catalyst_max_age_hours: float = Field(default=4.0)

    # ── Risk limits ────────────────────────────────────────────────
    max_portfolio_delta_per_10k: float = Field(default=30.0, description="Max net delta-shares per $10k NAV. 1 contract × 0.30 delta = 30 delta-shares.")
    max_portfolio_vega_per_10k: float = Field(default=200.0)
    max_daily_theta_pct: float = Field(default=0.005, description="Max theta decay as % of account/day")
    bid_ask_max_pct: float = Field(default=0.10)
    min_open_interest: int = Field(default=500)
    pricing_step_size: float = Field(
        default=0.05,
        description="Dollars to step limit price toward market every 30s during adaptive pricing. "
                    "6 steps × $0.05 = $0.30 sweep — covers typical $0.15–$0.50 combo bid-ask. "
                    "Do NOT set to $0.01 (min tick): 6 × $0.01 = $0.06 sweep, never crosses.",
    )
    use_adaptive_algo: bool = Field(
        default=False,
        description="Attach IBKR's Adaptive (Price Management) algo to entry orders. "
                    "VERIFIED 2026-06-05: IBKR does NOT support Adaptive on multi-leg combo/BAG "
                    "orders (credit & debit spreads) — it is silently ignored and the order rests "
                    "as a plain static limit. The 2026-06-04 'fix' that turned this ON (and turned "
                    "OFF the repricing walk) drove fill rate to 0.7%% on 06-05. Adaptive is valid "
                    "ONLY on single-leg orders, so this defaults OFF; the midpoint->natural "
                    "repricing walk is what fills combos. Leave OFF unless routing single legs.",
    )
    adaptive_algo_priority: Literal["Urgent", "Normal", "Patient"] = Field(
        default="Normal",
        description="IBKR Adaptive algo aggressiveness (only used when use_adaptive_algo is ON, "
                    "i.e. single-leg routing): Urgent (fastest fill, worst price), Normal "
                    "(balanced), Patient (best price, slowest).",
    )
    use_adaptive_single_leg: bool = Field(
        default=True,
        description="Attach the Adaptive algo to SINGLE-LEG native orders (long_call/long_put), "
                    "where it IS valid (unlike BAG combos — see use_adaptive_algo). The walk-LMT "
                    "alone fills poorly on a paper account with no market-data sub (~13%); Adaptive "
                    "fills server-side within the limit. Scoped to single legs ONLY — the combo "
                    "repricing walk is untouched. Safe to leave ON; set OFF to revert to walk-only.",
    )
    entry_marketable_start: bool = Field(
        default=True,
        description="Start leg-by-leg entry limits AT the marketable cross (natural ± a small "
                    "buffer) instead of resting at the net mid and walking. Orders that sit at mid "
                    "on a paper account simply don't cross the book — driving the ~2% fill rate "
                    "(92% timeout, 122 'protective long leg unfilled' aborts/3d). The cross price is "
                    "the SAME bounded worst-case the walk already targets; this just submits there "
                    "immediately so it fills now. Single legs already did this; this unifies it for "
                    "ALL legs (credit + paper-debit spreads). Live multi-leg still uses the atomic "
                    "BAG path (unchanged). Set False to revert to mid-start + walk.",
    )
    entry_walk_steps_paper: int = Field(
        default=20,
        description="BOARD RULING (2026-06-22): entry fill-window walk steps in PAPER mode. IBKR's "
        "paper simulator fills marketable orders on a 2-4 min LAG, so the old ~8 steps (~96s) "
        "cancelled orders that would have filled a minute later (~29% fill rate). 20 steps × 12s = "
        "~4 min of patience lets the slow paper fills land — the goal in paper is to FILL every "
        "validated setup so the edge can be measured (worse paper fill prices = a conservative test).",
    )
    entry_walk_steps_live: int = Field(
        default=4,
        description="BOARD RULING (2026-06-22): entry fill-window walk steps in LIVE mode. Real "
        "exchanges fill marketable orders in milliseconds — if it hasn't filled in ~45s (4×12s) the "
        "market moved, so ABORT rather than chase (chasing = silent slippage that erodes the edge). "
        "Speed + discipline live; patience only in paper.",
    )
    max_slippage_pct_of_width: float = Field(
        default=0.10,
        description="Repricing-walk slippage budget as a fraction of the spread's strike WIDTH. "
                    "The entry limit starts at the net mid and walks toward the marketable "
                    "(natural) price — up for debits, down for credits — stopping once it has given "
                    "up this fraction of the width (e.g. a $5-wide vertical at 0.10 => $0.50 of "
                    "room). Width-based (not %% of credit) so credit spreads still get enough room "
                    "to cross. Owner-chosen 2026-06-05. The execution advisor tunes this from "
                    "observed fill rates.",
    )
    use_ibkr_chain_pricing: bool = Field(
        default=True,
        description="Phase B: before strike selection, OVERRIDE the yfinance chain's bid/ask/IV "
                    "with real IBKR quotes for the OTM strikes of the target-DTE expiries, so the "
                    "rules engine selects strikes (credit-per-delta) on real prices, not stale "
                    "yfinance. yfinance still supplies the strike grid + OI (cheap reference). "
                    "Hard fallback: any failure keeps the yfinance chain — never breaks the scan. "
                    "Runs only for conviction+analyst-passing candidates (a handful/scan). Set False "
                    "to revert to pure yfinance selection (Phase A still reprices the chosen legs).",
    )
    ibkr_chain_range_pct: float = Field(
        default=0.15,
        description="Phase B: enrich OTM strikes within ±this fraction of spot (e.g. 0.15 = strikes "
                    "from 0.85×spot to 1.15×spot). Bounds the per-candidate IBKR fetch (~20-30 strikes).",
    )
    ibkr_chain_dte_lo: int = Field(default=18, description="Phase B: enrich expiries with DTE ≥ this.")
    ibkr_chain_dte_hi: int = Field(default=66, description="Phase B: enrich expiries with DTE ≤ this (credit-spread target band).")
    ibkr_market_data_type: int = Field(
        default=1,
        description="IBKR market-data type for execution pricing: 1=live (real-time, OPRA), "
                    "3=delayed (15-min, FREE). The execution walk reads IBKR's real combo "
                    "bid/ask + greeks as the limit's mid and natural (strictly better than the "
                    "yfinance width heuristic). Falls back to the yfinance mid + width heuristic "
                    "if IBKR returns no quote (e.g. contract not found / market closed) — so the "
                    "underlying-stock NMS denial (no equities sub) is harmless; spot stays on "
                    "yfinance. VERIFIED 2026-06-12 (scripts/probe_live_options.py): OPRA live "
                    "option quotes ARE served to the API on DUP344869 (TSLA/MSFT puts returned "
                    "live bid/ask, marketDataType=1) — flipped 3→1 for real-time limit pricing. "
                    "Revert to 3 if the OPRA sub lapses.",
    )
    scheduled_catalysts: list[dict] = Field(
        default_factory=list,
        description="Phase-3 proactive catalyst calendar — upcoming NON-earnings events to "
                    "pre-stage (earnings are handled by EarningsCalendarAgent). Each entry: "
                    "{'name': 'SpaceX IPO', 'date': '2026-06-12', 'peers': ['RKLB','LMT','NOC'], "
                    "'lead_days': 3}. When today is within lead_days of the date, the listed peers "
                    "are promoted to URGENT scan priority so the system analyzes the sector AHEAD "
                    "of the event. Promoted once per day per event.",
    )
    prescreen_combo_spread_pct: float = Field(
        default=0.80,
        description="Deterministic liquidity PRE-SCREEN run BEFORE the StrategySelector LLM: if "
                    "the rules-engine structure's net bid-ask exceeds this fraction of its mid "
                    "(read from the chain), skip the LLM call entirely (and the downstream "
                    "advocate) — the trade can't fill anyway. LOOSE on purpose (0.80) so only "
                    "egregiously illiquid structures (SMH ~117%, VECO ~190%) are dropped early; "
                    "borderline names still get the full LLM + the precise 50% reprice gate "
                    "(max_combo_spread_pct). FAIL-OPEN: if quotes can't be read, the LLM still "
                    "runs. Saves LLM tokens/latency on untradeable names without losing trades.",
    )
    exec_max_attempts_per_symbol: int = Field(
        default=4,
        description="Re-submission storm guard: after this many UNFILLED order attempts for the "
                    "same ticker in one session (and zero fills), skip further attempts on that "
                    "name until the next session. Stops the engine re-proposing an unfillable "
                    "name every cycle (observed 2026-06-12: SMH x25, VECO x21, all unfilled) — "
                    "which floods the pending queue, wastes broker traffic, and corrupts the "
                    "fill-rate denominator. A name that fills even once is never skipped.",
    )
    pricing_sanity_max_ratio: float = Field(
        default=2.0,
        description="Execution circuit breaker: abort the order if the IBKR mid is more than this "
                    "factor away from the yfinance mid the trade DECISION was built on (ratio "
                    "outside [1/x, x]). yfinance option mids are sometimes badly stale — e.g. COST "
                    "2026-06-08: yfinance net mid 1.85 but real IBKR/fill 8.55 (4.6x) — so the R/R "
                    "the strategy 'saw' wasn't real and we entered bad economics. This kills such "
                    "bad-data entries at the last step (recommended by the IBKR expert + COO as the "
                    "circuit breaker, pending the larger fix of pricing strategy selection off IBKR). "
                    "Arms the 2h exec cooldown on trip. Only applied when IBKR quotes are available.",
    )
    max_combo_spread_pct: float = Field(
        default=0.50,
        description="Liquidity gate at execution: skip the order if the spread's NET bid-ask "
                    "(from IBKR quotes) exceeds this fraction of the net mid. A thin name like "
                    "MKSI quotes ~138%% of mid (legs 7.10/9.40) — walking to fill there gives up "
                    "most of the credit, so don't trade it. Liquid SPY/QQQ verticals quote ~5-15%%. "
                    "Only applied when IBKR quotes are available (best-effort last line of defense "
                    "below the upstream conviction/OI gates). 0.50 = skip if the combo is >50%% wide.",
    )
    # NOTE: entry routing is spread-type-aware (ibkr_bridge.submit_trade), not a flag —
    # paper CREDIT spreads go leg-by-leg (riskless-combo Error 201 on a BAG), everything
    # else goes atomic BAG. The old paper_use_bag_combo flag was removed 2026-06-09.

    # ── Paths ──────────────────────────────────────────────────────
    iv_cache_dir: Path = Field(default=Path(".agora/iv_cache"))
    db_path: Path = Field(default=Path(".agora/agora.db"))
    audit_log_path: Path = Field(default=Path(".agora/audit.log"))
    shadow_book_path: Path = Field(default=Path(".agora/shadow_book.json"))

    # ── Trade sizing ──────────────────────────────────────────────
    risk_per_trade_dollars: float = Field(
        default=150.0,
        description="Max dollar risk per spread (1 contract) before size multiplier. $150 = 1.5% of $10k account, safely within 2% daily loss cap.",
    )
    max_contracts_per_trade: int = Field(
        default=10,
        description="Hard cap on contracts per trade regardless of size_multiplier",
    )
    # ── Learning-loop auto-approval (bounded §17 relaxation) ────────────────────
    lesson_auto_approve_enabled: bool = Field(
        default=True,
        description="Auto-approve PENDING lessons that clear strict evidence criteria, so the loop "
                    "ACTS on what it already learned instead of stranding it behind manual approval "
                    "(451 lessons pending, 10 ever approved — incl. the credit-spread bleed lesson "
                    "the loop found weeks before we fixed it). Bounded relaxation of Sacred Rule §17: "
                    "lessons are ADVISORY prompt context (the agent still decides), only PENDING "
                    "lessons are touched (never rejected), and each is tagged approved_by='auto' for "
                    "audit. Set False to require manual approval for everything.")
    lesson_auto_approve_min_confidence: float = Field(
        default=0.85, ge=0.5, le=1.0,
        description="Min confidence_in_lesson to auto-approve.")
    lesson_auto_approve_min_reinforced: int = Field(
        default=2, ge=1, le=10,
        description="Auto-approve if reinforced >= this (seen/confirmed more than once) ...")
    lesson_auto_approve_min_sample: int = Field(
        default=25, ge=5, le=200,
        description="... OR if sample_size >= this (a real evidence base). One of the two suffices; "
                    "a high-confidence one-shot with no sample still needs manual review.")
    csuite_close_min_hold_days: int = Field(
        default=1, ge=0, le=10,
        description="Min hold (days) before the CTO's DISCRETIONARY forced-closes (21-DTE "
                    "management + CEO session-plan close_targets) may fire. Prevents day-0 "
                    "insta-closing of a freshly-entered position whose thesis got no room — these "
                    "closed -$245 (CEO-plan x7) / -$59 (21-DTE x3) at hold=0d (avg hold across ALL "
                    "real closes is 0.5-0.8d vs 5-45 DTE horizons). The deterministic stop-loss "
                    "still owns the downside during the hold; this gates only the discretionary "
                    "overrides, mirroring the LLM-exit min-hold guards.",
    )
    stop_loss_multiplier: float = Field(
        default=2.0,
        description="Exit when position P&L = -stop_loss_multiplier × initial credit/debit",
    )

    # ── Stock Analyst (Phase 3 intelligence layer) ────────────────────────────
    stock_analyst_enabled: bool = Field(
        default=False,
        description="Enable StockAnalystAgent thesis layer. Start with shadow mode; "
                    "flip to live after ≥40 closed positions show direction hit ≥55%.",
    )
    stock_analyst_shadow_mode: bool = Field(
        default=True,
        description="When True the analyst logs but never blocks trade execution.",
    )
    stock_analyst_min_conviction: float = Field(
        default=60.0,
        description="Minimum conviction score to invoke the analyst (skip cheap tickers).",
        ge=0.0, le=100.0,
    )
    analyst_cache_ttl_secs: int = Field(
        default=3600, ge=300, le=14400,
        description="Reuse a ticker's thesis for this long instead of re-calling the analyst LLM "
                    "every scan cycle. A 1–30 day thesis is stable intraday; the analyst was "
                    "re-called ~27x/ticker/day (TLT 44x) — a ~$12/day leak. Keyed on ticker + "
                    "conviction band + macro stance; re-journals on hit to preserve attribution.",
    )
    advocate_cache_ttl_secs: int = Field(
        default=10800, ge=300, le=21600,
        description="Agent-level advocate verdict cache TTL. The long-options advocate gate had NO "
                    "cache — 499 reviews/39 tickers/day (~13x/ticker, ~$13/day). Cached on the "
                    "structure fingerprint (ticker+strategy+strikes+macro) so re-reviewing the same "
                    "structure reuses the verdict; a changed structure or macro flip re-runs.",
    )
    defender_cache_ttl_secs: int = Field(
        default=14400, ge=300, le=28800,
        description="Agent-level ThesisDefender verdict cache TTL (cost control). The defender fires "
                    "on EVERY advocate BLOCK, but re-scans block the same setup 12-26x/day (494 "
                    "blocks/40 tickers observed) — re-defending an identical thesis for the same "
                    "verdict at ~$0.027/call (~$13/day worst case). Cached on the structure "
                    "fingerprint (ticker+strategy+strikes); an identical block within TTL reuses the "
                    "verdict with NO LLM call, cutting volume ~92% (~$1/day). Changed strikes re-run.",
    )
    defender_max_tokens: int = Field(
        default=2500, ge=300, le=4096,
        description="Max output tokens for the defender. Headroom for adaptive-thinking reasoning + "
                    "the answer JSON so neither is truncated (a truncated answer fails to parse -> a "
                    "lost defense). Cost is driven by actual tokens, not this ceiling; the dedup cache "
                    "(defender_cache_ttl_secs) is what makes per-call quality affordable, not throttling "
                    "tokens — volume control beats quality-starving the high-stakes override decision.",
    )
    long_options_vetter_model: str = Field(
        default="claude-opus-4-8",
        description="Model for the long-options Opus vetter (the deliberate quality gate). Default"
                    "Opus; set claude-sonnet-4-6 to trade quality for ~80% lower per-call cost.",
    )
    csuite_patrol_interval_min: int = Field(
        default=60, ge=30, le=240,
        description="Minutes between C-suite officer patrols. 60 = hourly (~90 calls/day, ~$0.5). "
                    "Raise to 120 to halve C-suite LLM cost; routine officer reads are not time-critical.",
    )
    agent_mcp_tools_enabled: bool = Field(
        default=False,
        description="Attach the MCP tool schemas (sqlite/search/flow) to the analyst & advocate "
                    "requests. OFF by default: the tools were 0% used over a full week yet cost "
                    "~870–1,890 input tokens per call (~$1/day of pure redundant tokens). The "
                    "advocate's hallucination is already handled by the deterministic fact-grounding "
                    "gate, not these unused tools. Flip ON only if you want the model to fact-check.",
    )

    # ── StrategySelectorAgent (Phase 6 intelligence layer) ────────────────────
    strategy_selector_enabled: bool = Field(
        default=False,
        description="Enable StrategySelectorAgent. Start in shadow mode until ≥40 closed "
                    "positions show selector-chosen structures outperform rules engine.",
    )
    strategy_selector_shadow_mode: bool = Field(
        default=True,
        description="When True: selector journals but rules engine drives live trades.",
    )

    # ── AdvocateAgent (Phase 5 LLM adversarial review) ────────────────────────
    advocate_enabled: bool = Field(
        default=False,
        description="Enable LLM AdvocateAgent. Fires after all deterministic gates, "
                    "before IBKR. Start in shadow mode; promote after precision ≥60% / recall ≥50%.",
    )
    advocate_shadow_mode: bool = Field(
        default=True,
        description="When True: advocate journals but BLOCK verdicts never stop execution.",
    )
    advocate_fail_closed: bool = Field(
        default=True,
        description="When True (and advocate is live): if the advocate produces no verdict "
                    "(API error, timeout, credit exhaustion), the trade is BLOCKED rather than "
                    "submitted un-reviewed. Set ADVOCATE_FAIL_CLOSED=false to fall open (legacy "
                    "behaviour — trades proceed when the risk gate is unavailable).",
    )

    # ── ThesisDefenderAgent — counterweight to AdvocateAgent ──────────────────
    thesis_defender_enabled: bool = Field(
        default=False,
        description="Run ThesisDefenderAgent in parallel with AdvocateAgent. "
                    "A strong defense (confidence ≥ 0.65) moderates an advocate BLOCK to CAUTION, "
                    "letting the trade through. Enable after advocate is out of shadow mode.",
    )
    thesis_defender_shadow_mode: bool = Field(
        default=True,
        description="When True: defender journals but never overrides BLOCK verdicts.",
    )

    # ── Semantic trade memory (ChromaDB) ──────────────────────────────────────
    chroma_db_path: str = Field(
        default=".agora/chroma",
        description="Path to ChromaDB persistence directory for semantic trade memory. "
                    "Automatically indexed on every swing decision; queried by SwingJudge "
                    "to surface the 5 most similar historical trades before deciding.",
    )

    # ── ExitIntelligenceAgent (Phase 6 position monitoring) ───────────────────
    exit_intelligence_enabled: bool = Field(
        default=False,
        description="Enable ExitIntelligenceAgent hourly thesis validity checks. "
                    "Shadow mode recommended until exit alpha > 5% per position.",
    )
    exit_intelligence_shadow_mode: bool = Field(
        default=True,
        description="When True: recommendations are journaled; CLOSE_NOW is never executed.",
    )
    exit_intelligence_interval_hours: float = Field(
        default=1.0,
        description="Minimum hours between evaluations of the same position.",
        ge=0.25, le=24.0,
    )
    long_exit_llm_min_hold_days: int = Field(
        default=2, ge=1, le=4,
        description="Min calendar days held before the LLM may thesis-exit a LONG. Longs are "
                    "5-day swings; the deterministic stops own days 0..N-1. Was effectively 1 "
                    "(day-0-only guard), which let the LLM close 100% of longs on day 1.",
    )
    long_exit_llm_winner_lock: bool = Field(
        default=True,
        description="When True, the LLM may NEVER close a GREEN long — the conviction-scaled "
                    "trailing stop owns winners (it was built to let them run). The LLM only "
                    "adjudicates RED longs (thesis-break vs noise). Stops the day-1 churn that "
                    "cut e.g. META +$1,435 / ORCL +$1,325 on entry day.",
    )
    spread_exit_llm_min_hold_days: int = Field(
        default=3, ge=1, le=10,
        description="Min calendar days a CREDIT/DEBIT spread is held before the LLM may thesis-exit "
                    "it. Spreads are theta trades on 30-45 DTE; the day-0 guard let the brain close "
                    "39-DTE spreads on day 1. Deterministic stops/DTE still own the downside.",
    )
    spread_exit_require_kill: bool = Field(
        default=True,
        description="For spreads, honor an LLM CLOSE_NOW only on a HARD kill-condition trigger, not "
                    "a bare 'INVALIDATED' read (often off an empty/undocumented thesis). Code-gated, "
                    "not prompt-dependent.",
    )
    spread_stale_stop_cycles: int = Field(
        default=5, ge=2, le=60,
        description="Consecutive no-quote refresh cycles (≈60s each) before the stale-quote "
                    "intrinsic hard-stop backstop activates. Re-derives a conservative intrinsic "
                    "mark from the still-quoted underlying to catch a blowout that happens while "
                    "option quotes are missing (the no-data HOLD guard would otherwise freeze it).",
    )

    # ── Scan engine ───────────────────────────────────────────────────────────
    use_async_scan_engine: bool = Field(
        default=False,
        description="Enable async priority-queue scan engine (Phase 1). "
                    "Start with shadow_scan_engine=True for 5 trading days before going live.",
    )
    shadow_scan_engine: bool = Field(
        default=True,
        description="When use_async_scan_engine=True and this is True, the engine records "
                    "metrics but does NOT call _evaluate_ticker. Set False after shadow review.",
    )
    n_scan_workers: int = Field(
        default=4,
        description="Number of concurrent ticker-evaluation workers. "
                    "4 is conservative for IBKR pacing; increase to 6-8 once stable.",
        ge=1, le=16,
    )

    # ── MCP / External intelligence tools ──────────────────────────
    tavily_api_key: str | None = Field(
        default=None,
        description="Tavily API key for web search MCP tools (search_news, verify_earnings_date, etc). "
                    "Get from app.tavily.com. Free tier: 1k searches/month.",
    )
    unusual_whales_api_key: str | None = Field(
        default=None,
        description="Unusual Whales API key for real options flow (sweeps, dark pool, premium). "
                    "Get from unusualwhales.com/api. When set, replaces yfinance-based flow detection. "
                    "Falls back to yfinance if not set or on error.",
    )
    discord_uw_channel_id: str | None = Field(
        default=None,
        description="Discord channel ID where Unusual Whales posts flow alerts via webhook. "
                    "Bot polls this channel every 60s and converts alerts to FlowSignals. "
                    "Setup: create #uw-alerts channel → create webhook → paste URL in UW dashboard → "
                    "invite bot to server → copy channel ID here. "
                    "Right-click channel in Discord (Developer Mode on) → Copy Channel ID.",
    )
    edgar_user_agent: str = Field(
        default="AGORA Trading System rahulvari2021@gmail.com",
        description="User-Agent header required by SEC EDGAR API (SEC policy). "
                    "Format: 'AppName/version Contact@email'. Do not leave generic.",
    )
    mcp_tools_enabled: bool = Field(
        default=True,
        description="Enable MCP tools (sqlite_tools, search_tools, edgar_tools, market_tools) "
                    "for AdvocateAgent, StockAnalystAgent, and CatalystAgent. "
                    "Disable to revert to single-call no-tool mode.",
    )

    # ── Long Options Swing Specialist ──────────────────────────────
    long_options_enabled: bool = Field(
        default=False,
        description="Enable LongOptionsAgent. Buys calls/puts on directional conviction. "
                    "5-day time stop. Independent 15-min scan cycle.",
    )
    long_options_target_delta: float = Field(
        default=0.35,
        description="Target delta for strike selection. 0.35 (35Δ) is the professional "
                    "sweet-spot: moves with the stock, affordable premium.",
        ge=0.10, le=0.60,
    )
    long_options_ivr_cap: float = Field(
        default=45.0,
        description="Skip when IVR exceeds this — options too expensive to buy. "
                    "Buyers want low IVR (cheap premium) before IV expansion.",
        ge=20.0, le=80.0,
    )
    long_options_min_premium: float = Field(
        default=50.0,
        description="Minimum premium per contract (dollars). Below this the option "
                    "is too illiquid or too OTM to generate a meaningful swing return.",
        ge=10.0,
    )
    long_options_max_hold_days: int = Field(
        default=5,
        description="Hard time stop: close the position this many calendar days after "
                    "entry regardless of P&L. Prevents theta decay from compounding.",
        ge=1, le=30,
    )
    long_options_profit_target_pct: float = Field(
        default=0.50,
        description="Take profit when position gains this fraction of premium paid. "
                    "50% is the standard swing rule — lock in the move, don't overstay.",
        ge=0.20, le=2.0,
    )
    long_options_stop_loss_pct: float = Field(
        default=0.50,
        description="Cut loss when position loses this fraction of premium paid. "
                    "50% stop keeps max loss at 50% of capital deployed per trade.",
        ge=0.10, le=1.0,
    )
    long_options_scan_interval_minutes: int = Field(
        default=15,
        description="Minutes between long options scan cycles — fast enough "
                    "to capture momentum signals before they decay.",
        ge=5, le=60,
    )
    long_options_max_positions: int = Field(
        default=5,
        description="Max concurrent long option positions (separate from the spread limit).",
        ge=1, le=1000,
    )
    long_options_min_conviction: int = Field(
        default=2,
        description="Minimum signal score (out of 5 possible) to enter a trade. "
                    "Score 2 = 2 confirming signals; 3+ = high conviction.",
        ge=1, le=5,
    )
    long_options_vetter_overrides_advocate: bool = Field(
        default=True,
        description="For LONG options, a PROCEED from the purpose-built Opus vetter overrides a "
                    "BLOCK from the general (spread-calibrated) advocate. The advocate blocked "
                    "100% of longs (55/55), ~62% on debit-spread IV-crush logic that is backwards "
                    "for low-IVR long-vega single legs; letting it veto the Opus vetter was a SPOF. "
                    "When the vetter has NOT ruled (score-2, or vetter unavailable) the advocate "
                    "block still stands — this only resolves the vetter-PROCEED-vs-advocate-BLOCK "
                    "conflict in favor of the long-specific authority.",
    )
    long_options_counter_trend_flow_damp: bool = Field(
        default=True,
        description="Damp options-flow by 1 weight when it OPPOSES a confirmed price trend (bullish "
                    "dip-buying flow in a downtrend, or bearish flow in an uptrend). Flow is the "
                    "highest-weight signal; counter-trend call-sweeps on collapsing names (819 "
                    "bullish vs 107 bearish flow in a bear week) were canceling genuine bearish "
                    "momentum+rel_strength, capping bears at score-2 and blocking puts. Symmetric "
                    "(both directions) so it is a regime-consistency rule, not a bear-week patch.",
    )
    long_options_rsi_capitulation_floor: int = Field(
        default=18, ge=5, le=28,
        description="In a CONFIRMED downtrend (below both SMAs + 10d underperformance), a put is "
                    "blocked only below this RSI (true capitulation), not at the normal oversold "
                    "line. Oversold can stay oversold in a real trend; the standard filter vetoed "
                    "357 trend-confirmed puts in one bear week. Counter-trend puts keep the normal "
                    "oversold veto. Symmetric ceiling for calls = 100 - this.",
    )
    long_options_score2_min_signal_winrate: float = Field(
        default=0.35, ge=0.0, le=0.6,
        description="Deterministic auto-skip floor for minimum-conviction (score-2) longs: if the "
                    "dominant firing signal on the winning side has a historical win-rate below "
                    "this at n>=8 closes, skip the entry. Pure math (no LLM) — score-2 is the "
                    "cheapest/highest-volume bucket and is left advisory-only otherwise. Floor "
                    "self-tightens as signal_stats fills; at current data no signal trips it.",
    )
    # ── ITM-directional path (long-options expert + 2 verifiers — SHIPPED DARK) ──
    # Deep-ITM (intrinsic-dominated) directional buying for sustained-trend high-IV names, where
    # OTM long premium is locked out by the IVR cap but ITM is largely vega-immune. N=1 empirical
    # basis -> enabled=False (shadow-first). All verifier de-risking baked in.
    long_options_itm_enabled: bool = Field(default=False, description="LIVE master switch for the deep-ITM directional path. Off in LIVE until shadow-validated (>=30 ITM closes incl. adverse reversals). In PAPER it is force-enabled by long_options_itm_paper_data_collection (the data that earns the live promotion can only accrue if the path actually trades).")
    long_options_itm_paper_data_collection: bool = Field(default=True, description="In PAPER mode, run the ITM path regardless of long_options_itm_enabled — paper has no capital at risk, the all-verifier guards stay fully active, and this is the ONLY way the >=30 ITM closes that gate live enablement can ever accrue. Hard-ignored in LIVE (live obeys long_options_itm_enabled only).")
    long_options_itm_ivr_cap: float = Field(default=85.0, ge=60.0, le=100.0, description="ITM-only IVR ceiling. ITM activates when IVR>long_options_ivr_cap (OTM locked out) but <= this. Above this, even ITM's small extrinsic + fat bid-ask are punishing.")
    long_options_itm_target_delta: float = Field(default=0.75, ge=0.65, le=0.85, description="Target delta for the ITM leg (intrinsic-dominated). Strike is delta-driven; %ITM floats.")
    long_options_itm_min_conviction: int = Field(default=3, ge=3, le=5, description="ITM requires high conviction (no score-2 ITM).")
    long_options_itm_trend_ret: float = Field(default=0.05, ge=0.03, le=0.15, description="10-day return magnitude required for a CONFIRMED sustained trend before paying deep-ITM premium.")
    long_options_itm_rsi_put_max: int = Field(default=35, ge=20, le=45, description="Exhaustion guard: do NOT buy an ITM PUT when RSI < this (capitulation bottom — a high-delta put there loses fast on the bounce).")
    long_options_itm_rsi_call_min: int = Field(default=65, ge=55, le=80, description="Exhaustion guard: do NOT buy an ITM CALL when RSI > this (blow-off top).")
    long_options_itm_max_premium_pct: float = Field(default=0.05, ge=0.02, le=0.15, description="Tighter per-trade premium cap for ITM (full premium is at risk on a high-delta single). 0.05 keeps a single bad ITM day inside the daily breaker.")
    long_options_itm_max_positions: int = Field(default=1, ge=1, le=5, description="Max concurrent ITM positions — 1 by design (a single ITM contract IS the high-conviction bet; protects the daily breaker).")
    long_options_itm_min_oi: int = Field(default=500, ge=100, le=2000, description="Higher OI floor for ITM — deep strikes are thin; slippage on a wide spread erases the vega edge.")
    long_options_itm_max_bid_ask_pct: float = Field(default=0.10, ge=0.03, le=0.25, description="Tighter bid-ask gate for ITM — crossing a wide deep-ITM spread can erase ~40% of the vega advantage.")
    long_options_itm_dte_min: int = Field(default=21, ge=14, le=45, description="ITM DTE window min — wider/longer than OTM (slow theta, multi-day trend).")
    long_options_itm_dte_max: int = Field(default=45, ge=21, le=90, description="ITM DTE window max.")
    long_options_itm_max_hold_days: int = Field(default=10, ge=5, le=20, description="ITM hold horizon (vs 5-day OTM) — high-delta rides the trend; slow theta.")
    long_options_min_oi: int = Field(
        default=200,
        description="Minimum open interest at the selected strike. Ensures marketable quotes "
                    "and avoids wide bid-ask on thinly-traded strikes.",
        ge=0,
    )
    long_options_max_contracts: int = Field(
        default=3,
        description="Maximum contracts per long option trade. Scaled by conviction: "
                    "score 2→1, 3→2, 4+→max. Hard cap regardless of conviction.",
        ge=1, le=1000,
    )
    long_options_max_premium_pct: float = Field(
        default=0.15,
        description="Per-trade dollar risk cap: max premium paid on one long-options "
                    "trade as a fraction of account_size, regardless of contract count. "
                    "Bounds per-trade concentration; if one contract exceeds it, skip.",
        ge=0.01, le=1.0,
    )
    long_options_trailing_stop_trigger: float = Field(
        default=0.30,
        description="Activate trailing stop once position gains this fraction of premium paid. "
                    "E.g. 0.30 = trail kicks in after +30% gain.",
        ge=0.10, le=1.0,
    )
    long_options_trailing_stop_floor: float = Field(
        default=0.15,
        description="Trail floor below peak P&L. Position closes if P&L falls this fraction "
                    "below its all-time peak after trailing stop is triggered.",
        ge=0.05, le=0.50,
    )
    long_options_rsi_overbought: int = Field(
        default=72,
        description="Block LONG CALL entries when RSI14 exceeds this level. "
                    "Prevents chasing extended rallies near mean-reversion exhaustion.",
        ge=60, le=90,
    )
    long_options_rsi_oversold: int = Field(
        default=28,
        description="Block LONG PUT entries when RSI14 is below this level. "
                    "Prevents chasing extended selloffs near bounce exhaustion.",
        ge=10, le=40,
    )
    long_options_vetter_enabled: bool = Field(
        default=False,
        description="Enable Opus 4.8 pre-trade quality gate. Reviews signal confluence "
                    "with adaptive thinking before committing capital. Run in shadow mode "
                    "for ≥20 trades before setting shadow_mode=false.",
    )
    long_options_vetter_shadow_mode: bool = Field(
        default=True,
        description="Shadow mode: vetter logs verdicts but does NOT block or modify trades. "
                    "Flip to false only after calibrating on real trade outcomes.",
    )
    long_options_vetter_min_conviction: int = Field(
        default=3,
        description="Only vet setups at this conviction score or above. "
                    "Low-conviction trades are too borderline for the LLM to add signal.",
        ge=2, le=5,
    )

    # ── Alerts ─────────────────────────────────────────────────────
    alert_webhook_url: str | None = Field(default=None)
    discord_bot_token: str | None = Field(default=None)
    discord_approval_user_id: str | None = Field(default=None)

    # ── Logging ───────────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")

    @property
    def max_risk_per_trade(self) -> float:
        return self.account_size * self.max_position_size_pct

    @property
    def daily_loss_limit_dollars(self) -> float:
        return self.account_size * self.daily_loss_limit_pct

    @property
    def weekly_loss_limit_dollars(self) -> float:
        return self.account_size * self.weekly_loss_limit_pct

    @property
    def max_portfolio_delta(self) -> float:
        return self.max_portfolio_delta_per_10k * (self.account_size / 10_000)

    @property
    def max_portfolio_vega(self) -> float:
        return self.max_portfolio_vega_per_10k * (self.account_size / 10_000)

    @property
    def max_daily_theta_dollars(self) -> float:
        return self.account_size * self.max_daily_theta_pct


@lru_cache(maxsize=1)
def get_settings() -> AgoraSettings:
    return AgoraSettings()
