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

    # ── Risk limits ────────────────────────────────────────────────
    account_size: float = Field(default=25_000.0, ge=1_000.0)
    max_position_size_pct: float = Field(default=0.05, ge=0.01, le=0.20)
    max_open_positions: int = Field(default=5, ge=1, le=20)
    daily_loss_limit_pct: float = Field(default=0.03, ge=0.005, le=0.10)
    weekly_loss_limit_pct: float = Field(default=0.07, ge=0.01, le=0.20)
    min_reward_risk_ratio: float = Field(default=1.5, ge=1.0)
    max_iv_rank: float = Field(default=85.0, ge=0.0, le=100.0)
    min_open_interest: int = Field(default=100, ge=0)
    min_volume: int = Field(default=50, ge=0)
    max_bid_ask_spread_pct: float = Field(default=0.10, ge=0.0)

    # ── Market data ────────────────────────────────────────────────
    default_tickers: list[str] = Field(
        default=["SPY", "QQQ", "AAPL", "TSLA", "NVDA", "MSFT", "AMZN"],
    )
    vix_ticker: str = Field(default="^VIX")
    data_lookback_days: int = Field(default=30, ge=5)

    # ── IBKR (live trading only) ───────────────────────────────────
    ibkr_host: str = Field(default="127.0.0.1")
    ibkr_port: int = Field(default=7497)
    ibkr_client_id: int = Field(default=1)

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
