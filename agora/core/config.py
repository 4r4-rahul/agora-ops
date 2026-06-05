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
    daily_loss_limit_pct: float = Field(default=0.02, ge=0.005, le=0.25)
    weekly_loss_limit_pct: float = Field(default=0.06, ge=0.01, le=0.20)
    min_rr_ratio: float = Field(default=1.3, ge=0.5)
    min_credit_spread_rr_ratio: float = Field(
        default=0.10,
        description="Minimum R/R for credit spreads (collect ≥10% of spread width as premium). "
                    "Matches the rules_engine floor; credit spreads structurally have R/R < 1. "
                    "TLT at 11.5% IV yields R/R≈0.12; debit-spread threshold of 1.3 blocks all vol-premium trades.",
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
    profit_target_pct: float = Field(default=0.50, description="Close at 50% of max profit for 45-DTE vol-premium trades (tastytrade-validated for 30-60 DTE)")
    profit_target_pct_short_dte: float = Field(default=0.75, description="Close at 75% of max profit for short-DTE trades (sector_momentum, event plays ≤14 DTE) — backtested: 75% saves $1,230 vs 50% over 2.4yr")
    short_delta_target: float = Field(default=0.20, description="20-delta short strike for credit spreads")
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
        default=True,
        description="Submit orders with IBKR's Adaptive (Price Management) algo so they fill at a "
                    "fair price WITHIN the regulatory price collar instead of being rejected/stuck "
                    "at PendingSubmit. Diagnosed root cause of 100%% order timeouts (2026-06-04): "
                    "AGORA priced limits >3%% from reference, tripping IBKR's price collar; the "
                    "Adaptive algo manages the price server-side to avoid that. When ON, the manual "
                    "price-step loop is skipped (the algo owns pricing).",
    )
    adaptive_algo_priority: Literal["Urgent", "Normal", "Patient"] = Field(
        default="Normal",
        description="IBKR Adaptive algo aggressiveness: Urgent (fastest fill, worst price), "
                    "Normal (balanced), Patient (best price, slowest). Normal is a good default "
                    "for swing entries; Urgent if fills still lag at the open.",
    )

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
