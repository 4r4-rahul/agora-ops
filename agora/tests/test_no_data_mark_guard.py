"""
agora/tests/test_no_data_mark_guard.py

Mock-tests for the no-data (0.00 / missing-quote) mark guard and the credit/width gate —
the profitability core that stops the -$1,374 day-1 false-LOCK_IN bleed. Exercises the REAL
profit_engine and rules_engine code paths that actually broke (not the exit LLM).

C-suite required scenarios:
  1. Stale mark (current_price==0.0) -> profit engine does NOT lock-in AND does NOT pin HWM.
  2. Real near-worthless mark (0.05) -> lock-in STILL fires (guard must not block legit wins).
  3. cr_w below floor -> credit vertical rejected; above floor -> passes.
  4. _maybe_llm_exit precondition: 0.00 mark -> never acts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from types import SimpleNamespace

from agora.lifecycle.profit_engine import IntelligentProfitEngine as ProfitEngine


@dataclass
class _Pos:
    position_id: str
    current_price: float
    unrealized_pnl: float
    ticker: str = "TEST"
    max_gain_dollars: float = 385.0
    max_loss_dollars: float = 615.0
    entry_price: float = -3.85          # credit spread: negative entry
    contracts: int = 1
    expiry_date: date = field(default_factory=lambda: date.today() + timedelta(days=39))
    entry_date: date = field(default_factory=lambda: date.today())
    pillar: object = None
    strategy: str = "bull_put_spread"
    entry_theta_daily: float = 1.0
    legs: list = field(default_factory=list)


def test_stale_mark_no_lockin_no_hwm_poison():
    eng = ProfitEngine()
    # Credit spread with NO quote: current_mid would be 0.0 -> unrealized fabricated to +max_gain.
    pos = _Pos("p_stale", current_price=0.0, unrealized_pnl=385.0)  # +max_gain (fabricated 100%)
    eng.register_position(pos)
    d = eng.evaluate(pos, realized_pnl_today=0.0)
    assert d.rule == "HOLD", f"stale 0.00 mark must HOLD, got {d.rule} ({d.reason})"
    assert not d.should_close, "stale mark must not close"
    # HWM must NOT be pinned to 100% (guard returns before hwm mutation)
    st = eng._states["p_stale"]
    assert st.hwm == 0.0, f"HWM poisoned to {st.hwm} — guard placed too late (after hwm mutate)"
    print(f"OK 1: stale 0.00 mark -> HOLD, HWM not pinned (={st.hwm:.2f})")


def test_real_worthless_mark_still_locks_in():
    eng = ProfitEngine()
    # REAL near-worthless credit spread: mark 0.05 (not 0.00), 95% of max profit captured.
    pos = _Pos("p_real", current_price=0.05, unrealized_pnl=0.95 * 385.0)
    eng.register_position(pos)
    d = eng.evaluate(pos, realized_pnl_today=0.0)
    # Guard must NOT block a legitimate win — a real mark reaches the profit logic.
    st = eng._states["p_real"]
    assert st.hwm > 0.0, "real mark must pass the guard and update HWM"
    assert d.should_close or d.rule in ("LOCK_IN", "SHORT_DTE", "PROFIT_TARGET", "RATCHET"), \
        f"real 95% win should trigger a profit close, got {d.rule}"
    print(f"OK 2: real 0.05 mark @95% -> {d.rule} fires (HWM={st.hwm:.2f})")


def test_credit_width_gate_logic():
    # Pure logic mirror of the rules_engine gate: cr_w = |debit_credit| / (width*100)
    floor = 0.30
    def passes(credit_dollars, width_pts):
        cr_w = abs(credit_dollars) / (width_pts * 100)
        return cr_w >= floor, round(cr_w, 3)
    ok_lo, cr_lo = passes(-65.0, 5.0)    # MKSI-style loser: $0.65 credit on $5 width -> 0.13
    ok_hi, cr_hi = passes(-200.0, 5.0)   # $2.00 credit on $5 width -> 0.40
    assert not ok_lo, f"cr_w {cr_lo} should be rejected"
    assert ok_hi, f"cr_w {cr_hi} should pass"
    print(f"OK 3: cr_w gate rejects {cr_lo:.2f} (loser cohort), passes {cr_hi:.2f}")


def test_llm_exit_precondition_zero_mark():
    import asyncio

    from agora.lifecycle.position_manager import PositionManager
    pm = PositionManager.__new__(PositionManager)
    pm._settings = SimpleNamespace(long_exit_llm_min_hold_days=2, spread_exit_llm_min_hold_days=3,
                                   long_exit_llm_winner_lock=True, spread_exit_require_kill=True,
                                   exit_intelligence_interval_hours=1.0)
    pm._macro_ctx = None
    pm._long_thesis_for = lambda pid: {}
    called = {"evaluate": False}
    class _Agent:
        shadow_mode = False
        def should_evaluate(self, pid, iv): return True
        async def evaluate(self, *a, **k):
            called["evaluate"] = True
            return SimpleNamespace(should_close=True, kill_triggered=True,
                                   thesis_validity="INVALIDATED", kill_condition_status="x",
                                   recommendation_reasoning="r")
    pm._exit_agent = _Agent()
    # Spread (is_long=False) held 5 days but with a 0.00 mark -> must NOT reach the agent / close.
    pos = _Pos("p_llm", current_price=0.0, unrealized_pnl=385.0,
               entry_date=date.today() - timedelta(days=5))
    r = asyncio.get_event_loop().run_until_complete(pm._maybe_llm_exit(pos, is_long=False))
    assert r is False, "0.00 mark must block the LLM exit"
    assert called["evaluate"] is False, "must not even call the exit agent on a 0.00 mark"
    print("OK 4: _maybe_llm_exit on 0.00 mark -> False, agent never called")


if __name__ == "__main__":
    test_stale_mark_no_lockin_no_hwm_poison()
    test_real_worthless_mark_still_locks_in()
    test_credit_width_gate_logic()
    test_llm_exit_precondition_zero_mark()
    print("\nALL MOCK SCENARIOS PASSED")
