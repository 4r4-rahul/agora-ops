"""edge_size_multiplier — only ever sizes DOWN a proven-negative cell; never up; never raises."""
import types

from agora.ops.edge_sizing import edge_size_multiplier


def _s(**kw):
    base = dict(edge_sizing_enabled=True, edge_size_up_max=1.0, edge_size_down_min=0.5, edge_min_sample=30)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _patch(monkeypatch, cells):
    monkeypatch.setattr("agora.ops.strategy_health.compute_health", lambda db: cells)


class TestEdgeSizeMultiplier:
    def test_disabled_is_neutral(self, monkeypatch):
        assert edge_size_multiplier("x", "vol_premium", "neutral", _s(edge_sizing_enabled=False)) == 1.0

    def test_unproven_cell_is_neutral(self, monkeypatch):
        _patch(monkeypatch, {"vol_premium:neutral": {"count": 5, "sharpe": -2.0}})  # n<min
        assert edge_size_multiplier("x", "vol_premium", "neutral", _s()) == 1.0

    def test_none_sharpe_is_neutral(self, monkeypatch):
        _patch(monkeypatch, {"vol_premium:neutral": {"count": 50, "sharpe": None}})
        assert edge_size_multiplier("x", "vol_premium", "neutral", _s()) == 1.0

    def test_negative_sharpe_sizes_down(self, monkeypatch):
        _patch(monkeypatch, {"vol_premium:neutral": {"count": 50, "sharpe": -1.0}})
        # 1.0 + (-1.0)*0.25 = 0.75, clamped to [0.5, 1.0]
        assert edge_size_multiplier("x", "vol_premium", "neutral", _s()) == 0.75

    def test_strong_cell_stays_capped_at_one(self, monkeypatch):
        _patch(monkeypatch, {"vol_premium:neutral": {"count": 50, "sharpe": 1.5}})
        assert edge_size_multiplier("x", "vol_premium", "neutral", _s()) == 1.0   # never sizes up

    def test_error_is_neutral(self, monkeypatch):
        monkeypatch.setattr("agora.ops.strategy_health.compute_health",
                            lambda db: (_ for _ in ()).throw(RuntimeError("boom")))
        assert edge_size_multiplier("x", "vol_premium", "neutral", _s()) == 1.0
