"""IvPremiumScreen — the vol-risk-premium signal. Pure logic over a JSON day-cache (→ tmp)."""
import json

from agora.signals.iv_premium import IvPremiumScreen


def _screen(tmp_path, threshold=0.25, min_days=15):
    return IvPremiumScreen(threshold=threshold, min_days=min_days, cache_dir=tmp_path / "iv")


def _seed(tmp_path, ratios, dates):
    d = tmp_path / "iv"; d.mkdir(parents=True, exist_ok=True)
    (d / "AAPL.json").write_text(json.dumps({"dates": dates, "ratios": ratios}))


class TestIvPremiumScreen:
    def test_init_creates_cache_dir(self, tmp_path):
        _screen(tmp_path)
        assert (tmp_path / "iv").exists()

    def test_none_or_zero_inputs_inactive(self, tmp_path):
        s = _screen(tmp_path)
        assert s.check("AAPL", None, 0.2)["premium_ratio"] is None
        assert s.check("AAPL", 0.3, None)["signal_active"] is False
        assert s.check("AAPL", 0.3, 0.0)["signal_active"] is False   # hv<=0 guard

    def test_premium_ratio_computed_and_cached(self, tmp_path):
        s = _screen(tmp_path, min_days=1)
        r = s.check("aapl", 0.30, 0.20)                              # (0.30-0.20)/0.20 = 0.5
        assert r["premium_ratio"] == 0.5
        assert r["atm_iv_pct"] == 30.0 and r["hv_21d_pct"] == 20.0
        assert (tmp_path / "iv" / "AAPL.json").exists()             # ticker upper-cased

    def test_signal_active_after_min_days(self, tmp_path):
        s = _screen(tmp_path, threshold=0.25, min_days=3)
        _seed(tmp_path, [0.5, 0.5, 0.5], ["2026-06-24", "2026-06-25", "2026-06-26"])
        r = s.check("AAPL", 0.30, 0.20)                              # +today → 4 consecutive >= 3
        assert r["days_above_threshold"] >= 3 and r["signal_active"] is True

    def test_below_threshold_breaks_streak(self, tmp_path):
        s = _screen(tmp_path, threshold=0.25, min_days=2)
        _seed(tmp_path, [0.5, 0.01], ["2026-06-25", "2026-06-26"])  # interior break
        r = s.check("AAPL", 0.30, 0.20)
        assert r["signal_active"] is False                          # only 1 consecutive

    def test_corrupt_cache_is_handled(self, tmp_path):
        s = _screen(tmp_path, min_days=1)
        d = tmp_path / "iv"; d.mkdir(parents=True, exist_ok=True)
        (d / "AAPL.json").write_text("not-json{{")                  # must not raise
        assert s.check("AAPL", 0.30, 0.20)["premium_ratio"] == 0.5


class TestIvPremiumDedup:
    def test_same_day_not_appended_twice(self, tmp_path):
        from datetime import date
        s = _screen(tmp_path, min_days=1)
        today = date.today().isoformat()
        _seed(tmp_path, [0.5], [today])                 # cache already has TODAY
        s.check("AAPL", 0.30, 0.20)                     # dedup branch: today present → no re-append
        cache = json.loads((tmp_path / "iv" / "AAPL.json").read_text())
        assert cache["dates"].count(today) == 1
