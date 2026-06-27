"""
HARDEN-1 (2026-06-27) — the safety state must never be hidden.

Disaster-audit GAP-1: the daily-loss breaker is OFF in free-paper data collection. The owner chose
"keep OFF but surface it loudly." These tests lock that get_breaker_status() tells the truth AND that
LIVE mode can never disable the daily-loss breaker (the double-guard).
"""
from agora.core.config import get_settings
from agora.risk.risk_council import RiskCouncil


def _rc(tmp_path, *, mode, flag):
    s = get_settings().model_copy(update={
        "db_path": tmp_path / "t.db",
        "account_size": 10_000.0,
        "daily_loss_limit_pct": 0.40,
        "trading_mode": mode,
        "paper_disable_loss_breakers": flag,
    })
    return RiskCouncil(s)


class TestBreakerStatusSurfacing:
    def test_paper_with_flag_reports_OFF(self, tmp_path):
        st = _rc(tmp_path, mode="paper", flag=True).get_breaker_status()
        assert st["daily_loss_breaker_enabled"] is False
        assert "OFF" in st["state"]
        # the unconditional guards must still be advertised as always-on
        assert "over_fill_auto_halt" in st["always_on"]
        assert "per_position_2x_max_loss_trip" in st["always_on"]

    def test_paper_without_flag_reports_enforced(self, tmp_path):
        st = _rc(tmp_path, mode="paper", flag=False).get_breaker_status()
        assert st["daily_loss_breaker_enabled"] is True
        assert st["state"] == "enforced"

    def test_live_mode_forces_breaker_on_even_with_flag(self, tmp_path):
        # THE invariant: live can NEVER disable its daily-loss breaker, flag notwithstanding.
        st = _rc(tmp_path, mode="live", flag=True).get_breaker_status()
        assert st["daily_loss_breaker_enabled"] is True
        assert st["daily_loss_limit_dollars"] == 4_000.0
