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
        default="claude-opus-4-7",
        description="Primary model — Opus 4.7 for Macro Synthesizer, Resolver, 8-K parser",
    )
    claude_fast_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Fast model for high-frequency classification tasks",
    )
    claude_brief_model: str = Field(
        default="claude-sonnet-4-6",
        description="Sonnet model for C-suite departmental briefs — cost-efficient vs Opus for high-frequency brief generation",
    )

    # ── Trading ────────────────────────────────────────────────────
    trading_mode: Literal["paper", "live"] = Field(default="paper")
    account_size: float = Field(default=25_000.0, ge=1_000.0)
    max_position_size_pct: float = Field(default=0.02, ge=0.005, le=0.05)
    max_open_positions: int = Field(default=4, ge=1, le=20)
    max_per_correlation_group: int = Field(default=1, ge=1, le=5)
    daily_loss_limit_pct: float = Field(default=0.02, ge=0.005, le=0.10)
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
    high_ivr_conviction_floor: float = Field(default=60.0, description="Conviction floor when IVR >= high_ivr_threshold; overrides the lower vol_premium_conviction_floor")
    high_conviction_score: float = Field(default=80.0, description="Score for 1.5x size multiplier")

    # ── Universe ───────────────────────────────────────────────────
    etf_universe: list[str] = Field(
        default=[
            # Broad market ETFs
            "SPY", "QQQ", "IWM", "GLD", "TLT", "SLV", "COPX", "PPLT",
            # Mega-cap tech
            "AAPL", "MSFT", "NVDA", "META", "AMZN", "GOOGL", "TSLA", "AVGO", "AMD",
            # Semiconductors
            "TSM", "MU", "INTC", "TXN", "LRCX", "ASML", "SMTC", "AMKR",
            "HIMX", "TSEM", "VECO",
            # Defense / space
            "PLTR", "KTOS", "AVAV", "RKLB",
            # Energy / power
            "VST", "CEG", "NEE", "FCEL", "BE", "AMSC", "FLNC", "CCJ",
            # Finance / brokers
            "SCHW", "HOOD", "CBOE",
            # Large-cap diversified
            "COST", "WMT", "KO", "CAT", "ORCL", "MSI", "JBL", "SANM",
            # Biotech / healthcare
            "LLY", "JNJ", "ABT", "BSX", "BIIB", "MDT",
            # Cloud / software
            "SNOW", "ZS", "UPST",
            # Optical / photonics
            "LITE", "COHR", "AAOI", "AXTI", "AOSL",
            # Other watchlist
            "FLEX", "MSTR", "WDC", "IREN", "NOK", "OKLO", "GEV",
            "MP", "POWL", "KEYS", "ONTO", "SERV", "VICR",
            "BJ", "F", "CIFR", "ASTS",
        ],
        description="Options universe for vol premium credit spreads and event plays",
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
