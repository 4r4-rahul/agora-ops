"""
Platform configuration via pydantic-settings.
All secrets come from environment variables or .env file.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
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
        description="Claude model ID for all intelligent agents",
    )

    # ── Trading mode ───────────────────────────────────────────────
    trading_mode: Literal["paper", "live"] = Field(
        default="paper",
        description="paper = no real orders; live = real execution (requires IBKR)",
    )
    require_human_approval: bool = Field(
        default=True,
        description="Gate every trade on human confirmation before execution",
    )
    pdt_exempt: bool = Field(
        default=False,
        description=(
            "Set True for non-US accounts or cash accounts where the SEC Pattern Day Trader "
            "rule does not apply. Removes the 3 day-trades/week limit and unlocks full trade "
            "frequency regardless of account size."
        ),
    )

    # ── Risk limits ────────────────────────────────────────────────
    account_size: float = Field(default=25_000.0, ge=1_000.0)
    max_position_size_pct: float = Field(default=0.05, ge=0.01, le=0.20)
    max_open_positions: int = Field(default=10, ge=1, le=20)
    daily_loss_limit_pct: float = Field(default=0.03, ge=0.005, le=0.10)
    weekly_loss_limit_pct: float = Field(default=0.07, ge=0.01, le=0.20)
    min_reward_risk_ratio: float = Field(default=1.5, ge=1.0)
    max_iv_rank: float = Field(default=85.0, ge=0.0, le=100.0)
    min_open_interest: int = Field(default=100, ge=0)
    min_volume: int = Field(default=50, ge=0)
    max_bid_ask_spread_pct: float = Field(default=0.10, ge=0.0)

    # ── Market data ────────────────────────────────────────────────
    # Tier-appropriate default ticker lists (overridden by ENV DEFAULT_TICKERS)
    # starter:      SPY + QQQ only — maximum liquidity, manageable for $10k accounts
    # intermediate: add mega-cap tech (AAPL, MSFT, NVDA) — tight spreads, high OI
    # advanced:     sector ETFs + high-ADV single names (6-10 symbols)
    # professional: full universe screener via UniverseScreenerAgent
    default_tickers: list[str] = Field(
        default=["SPY", "QQQ", "AAPL", "TSLA", "NVDA", "MSFT", "AMZN"],
    )
    vix_ticker: str = Field(default="^VIX")
    data_lookback_days: int = Field(default=30, ge=5)

    # ── IBKR connection ────────────────────────────────────────────
    # Ports: TWS paper=7497, TWS live=7496, Gateway paper=4002, Gateway live=4001
    ibkr_host: str = Field(default="127.0.0.1")
    ibkr_port: int = Field(default=7497, description="7497=TWS paper, 7496=TWS live")
    ibkr_client_id: int = Field(default=1, description="Entry orders; close orders use client_id+1")

    # ── Database ───────────────────────────────────────────────────
    database_url: str = Field(
        default="sqlite+aiosqlite:///./trade_journal.db",
        description="SQLAlchemy async DB URL",
    )

    # ── API ────────────────────────────────────────────────────────
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)
    api_reload: bool = Field(default=False)
    cors_origins: list[str] = Field(default=["http://localhost:3000"])

    # ── Alerts ─────────────────────────────────────────────────────
    alert_webhook_url: str | None = Field(default=None)

    # ── Discord interactive approval ────────────────────────────────
    # Bot token from Discord Developer Portal (Bot → Token).
    # Approval user ID: right-click your username → Copy User ID (Developer Mode required).
    discord_bot_token: str | None = Field(default=None)
    discord_approval_user_id: str | None = Field(default=None)
    discord_approval_timeout_seconds: int = Field(default=300)

    # ── Commissions ────────────────────────────────────────────────
    # IBKR fixed: $0.65/contract/leg (each side).  Round-trip = rate × legs × contracts × 2.
    commission_per_contract_leg: float = Field(default=0.65)

    # ── Logging ────────────────────────────────────────────────────
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(default="INFO")
    log_json: bool = Field(default=False)

    @field_validator("default_tickers", mode="before")
    @classmethod
    def parse_tickers(cls, v: object) -> list[str]:
        if isinstance(v, str):
            return [t.strip().upper() for t in v.split(",") if t.strip()]
        return [t.upper() for t in v]

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_origins(cls, v: object) -> list[str]:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return list(v)

    @property
    def account_tier(self) -> str:
        """
        Tier determines which strategies and DTE ranges are appropriate.
        starter      < $25k  — verticals only, 1 contract (PDT-gated if not exempt)
        intermediate $25-50k — condors viable, 1-2 contracts
        advanced     $50-100k — full defined-risk toolkit, 6-10 names
        professional $100k+  — portfolio margin eligible, Greeks dashboard

        PDT-exempt accounts (<$25k but no day-trade restriction) are treated as
        'intermediate' for trade-frequency purposes while keeping the conservative
        strategy restrictions of 'starter' for position sizing.
        """
        if self.account_size < 25_000:
            return "starter"
        if self.account_size < 50_000:
            return "intermediate"
        if self.account_size < 100_000:
            return "advanced"
        return "professional"

    @property
    def effective_trade_tier(self) -> str:
        """
        Trade-frequency tier — decoupled from strategy complexity tier.
        PDT-exempt accounts below $25K can trade at intermediate frequency.
        """
        if self.account_size < 25_000 and not self.pdt_exempt:
            return "starter"   # capped at 3 day-trades/week
        if self.account_size < 50_000:
            return "intermediate"
        if self.account_size < 100_000:
            return "advanced"
        return "professional"

    @property
    def tier_tickers(self) -> list[str]:
        """
        Tier-appropriate default tickers.
        Returns the configured default_tickers if explicitly set,
        otherwise returns the tier-appropriate default list.
        """
        tier_defaults = {
            "starter":      ["SPY", "QQQ"],
            "intermediate": ["SPY", "QQQ", "AAPL", "MSFT", "NVDA"],
            "advanced": [
                "SPY", "QQQ", "IWM", "XLK", "XLF",
                "AAPL", "MSFT", "NVDA", "GOOGL", "META",
            ],
            "professional": [
                "SPY", "QQQ", "IWM", "GLD", "TLT",
                "XLE", "XLF", "XLK", "XLV", "SMH",
                "AAPL", "MSFT", "NVDA", "GOOGL", "META",
                "AMZN", "TSLA", "AMD", "AVGO", "JPM",
            ],
        }
        # If user has configured explicit tickers, honor them
        env_default = ["SPY", "QQQ", "AAPL", "TSLA", "NVDA", "MSFT", "AMZN"]
        if self.default_tickers != env_default:
            return self.default_tickers
        return tier_defaults.get(self.account_tier, self.default_tickers)

    @property
    def max_position_dollars(self) -> float:
        return self.account_size * self.max_position_size_pct

    @property
    def daily_loss_limit_dollars(self) -> float:
        return self.account_size * self.daily_loss_limit_pct

    @property
    def weekly_loss_limit_dollars(self) -> float:
        return self.account_size * self.weekly_loss_limit_pct


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
