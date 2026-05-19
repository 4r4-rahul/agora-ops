#!/usr/bin/env python3
"""
Tier 2 improvement tests.

Tests:
  1. VWAP MR enabled (re-test with current portfolio)
  2. VWAP MR tuned parameters
  3. Scalp cooldown reduction
  4. ORB wider entry window  
  5. Scalp window tuning
  6. Combinations
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from copy import deepcopy
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "intraday")
ACCOUNT = 10_000.0

def run_shared(spy_cfg, qqq_cfg, label="", verbose=False):
    """Run shared-balance portfolio backtest with given configs."""
    spy_df = pd.read_csv(os.path.join(DATA_DIR, "SPY_ibkr_1m_180d.csv"),
                         parse_dates=["timestamp"], index_col="timestamp")
    qqq_df = pd.read_csv(os.path.join(DATA_DIR, "QQQ_ibkr_1m_180d.csv"),
                         parse_dates=["timestamp"], index_col="timestamp")
    spy_df.index = pd.to_datetime(spy_df.index, utc=True)
    qqq_df.index = pd.to_datetime(qqq_df.index, utc=True)

    spy_bt = ScalpBacktester(config=spy_cfg, account_size=ACCOUNT, spx_mode=True)
    spy_r = spy_bt.run(spy_df, ticker="SPY", interval="1m", verbose=verbose)

    qqq_bt = ScalpBacktester(config=qqq_cfg, account_size=ACCOUNT)
    qqq_r = qqq_bt.run(qqq_df, ticker="QQQ", interval="1m", verbose=verbose)

    all_trades = []
    for dr in spy_r.daily_results:
        for t in getattr(dr, '_trades', []):
            if not t.is_open:
                all_trades.append(('SPY', t))
    for dr in qqq_r.daily_results:
        for t in getattr(dr, '_trades', []):
            if not t.is_open:
                all_trades.append(('QQQ', t))
    all_trades.sort(key=lambda x: x[1].exit_time)

    balance = ACCOUNT
    peak = ACCOUNT
    max_dd = 0
    total_pnl = 0
    wins = 0
    losses = 0
    total_trades = spy_r.total_trades + qqq_r.total_trades
    strat_pnl = {"scalp": 0, "runner": 0, "orb": 0, "range_fade": 0, "vwap_mr": 0}

    for ticker, t in all_trades:
        pnl = t.total_pnl
        total_pnl += pnl
        balance += pnl
        if balance > peak:
            peak = balance
        dd = (peak - balance) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
        if pnl > 5:
            wins += 1
        elif pnl < -5:
            losses += 1
        tier = t.tier if hasattr(t, 'tier') else 'scalp'
        if tier in strat_pnl:
            strat_pnl[tier] += pnl

    wr = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0
    gross_wins = sum(t.total_pnl for _, t in all_trades if t.total_pnl > 5)
    gross_losses = abs(sum(t.total_pnl for _, t in all_trades if t.total_pnl < -5))
    pf = gross_wins / gross_losses if gross_losses > 0 else float('inf')

    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}")
    print(f"  SPY: {spy_r.total_trades}t  QQQ: {qqq_r.total_trades}t  Total: {total_trades}t")
    print(f"  WR={wr:.1f}%  PF={pf:.2f}  PnL=${total_pnl:+,.0f}  MaxDD={max_dd:.1%}")
    print(f"  Scalp: ${strat_pnl['scalp']:+,.0f}  Runner: ${strat_pnl['runner']:+,.0f}  "
          f"ORB: ${strat_pnl['orb']:+,.0f}  RF: ${strat_pnl['range_fade']:+,.0f}  "
          f"VM: ${strat_pnl['vwap_mr']:+,.0f}")

    return {
        "total_trades": total_trades,
        "spy_trades": spy_r.total_trades,
        "qqq_trades": qqq_r.total_trades,
        "wr": wr,
        "pf": pf,
        "pnl": total_pnl,
        "max_dd": max_dd,
        "strats": strat_pnl,
    }


def baseline():
    return EngineConfig(), EngineConfig.for_qqq()


# ── VWAP MR tests ───────────────────────────────────────────────

def test_vwap_mr_default():
    """Enable VWAP MR with current default params."""
    spy_cfg, qqq_cfg = baseline()
    spy_cfg.vwap_mr.enabled = True
    qqq_cfg.vwap_mr.enabled = True
    return spy_cfg, qqq_cfg


def test_vwap_mr_stricter():
    """VWAP MR with higher deviation threshold (0.30 vs 0.25)."""
    spy_cfg, qqq_cfg = baseline()
    spy_cfg.vwap_mr.enabled = True
    qqq_cfg.vwap_mr.enabled = True
    spy_cfg.vwap_mr.min_vwap_deviation_pct = 0.30
    qqq_cfg.vwap_mr.min_vwap_deviation_pct = 0.30
    return spy_cfg, qqq_cfg


def test_vwap_mr_quicker():
    """VWAP MR with shorter hold (15 bars) and tighter target."""
    spy_cfg, qqq_cfg = baseline()
    spy_cfg.vwap_mr.enabled = True
    qqq_cfg.vwap_mr.enabled = True
    spy_cfg.vwap_mr.max_hold_bars = 15
    qqq_cfg.vwap_mr.max_hold_bars = 15
    spy_cfg.vwap_mr.target_vwap_return_pct = 0.06
    qqq_cfg.vwap_mr.target_vwap_return_pct = 0.06
    return spy_cfg, qqq_cfg


def test_vwap_mr_3_confirm():
    """VWAP MR with 3 confirmations (stricter entry)."""
    spy_cfg, qqq_cfg = baseline()
    spy_cfg.vwap_mr.enabled = True
    qqq_cfg.vwap_mr.enabled = True
    spy_cfg.vwap_mr.min_confirmations = 3
    qqq_cfg.vwap_mr.min_confirmations = 3
    return spy_cfg, qqq_cfg


# ── Scalp tuning tests ──────────────────────────────────────────

def test_scalp_cooldown_10():
    """Reduce scalp cooldown from 15 to 10 bars."""
    spy_cfg, qqq_cfg = baseline()
    spy_cfg.scalp.cooldown_bars = 10
    qqq_cfg.scalp.cooldown_bars = 10
    return spy_cfg, qqq_cfg


def test_scalp_cooldown_5():
    """Reduce scalp cooldown from 15 to 5 bars."""
    spy_cfg, qqq_cfg = baseline()
    spy_cfg.scalp.cooldown_bars = 5
    qqq_cfg.scalp.cooldown_bars = 5
    return spy_cfg, qqq_cfg


def test_scalp_w1_wider():
    """Widen W1 window: 15-70 (from 20-60)."""
    spy_cfg, qqq_cfg = baseline()
    spy_cfg.scalp.window_1_start = 15
    qqq_cfg.scalp.window_1_start = 15
    spy_cfg.scalp.window_1_end = 70
    qqq_cfg.scalp.window_1_end = 70
    return spy_cfg, qqq_cfg


def test_scalp_w2_earlier():
    """Start W2 earlier: 240 (from 270) = 1:30 PM instead of 2:00 PM."""
    spy_cfg, qqq_cfg = baseline()
    spy_cfg.scalp.window_2_start = 240
    qqq_cfg.scalp.window_2_start = 240
    return spy_cfg, qqq_cfg


# ── ORB tuning tests ────────────────────────────────────────────

def test_orb_wider_entry():
    """Extend ORB entry window to bar 150 (11:00 AM → 12:00 PM)."""
    spy_cfg, qqq_cfg = baseline()
    spy_cfg.orb.entry_end_bar = 150
    qqq_cfg.orb.entry_end_bar = 150
    return spy_cfg, qqq_cfg


def test_orb_wider_wait():
    """Extend ORB max_wait_bars to 120 (longer to detect breakout)."""
    spy_cfg, qqq_cfg = baseline()
    spy_cfg.orb.max_wait_bars = 120
    qqq_cfg.orb.max_wait_bars = 120
    return spy_cfg, qqq_cfg


def test_orb_entry_150():
    """ORB entry window end at bar 150."""
    spy_cfg, qqq_cfg = baseline()
    spy_cfg.orb.entry_end_bar = 150
    qqq_cfg.orb.entry_end_bar = 150
    return spy_cfg, qqq_cfg


def test_vwap_mr_ultra_selective():
    """VWAP MR: 0.30% dev, 15 bar hold, 0.06% target."""
    spy_cfg, qqq_cfg = baseline()
    spy_cfg.vwap_mr.enabled = True
    qqq_cfg.vwap_mr.enabled = True
    spy_cfg.vwap_mr.min_vwap_deviation_pct = 0.30
    qqq_cfg.vwap_mr.min_vwap_deviation_pct = 0.30
    spy_cfg.vwap_mr.max_hold_bars = 15
    qqq_cfg.vwap_mr.max_hold_bars = 15
    spy_cfg.vwap_mr.target_vwap_return_pct = 0.06
    qqq_cfg.vwap_mr.target_vwap_return_pct = 0.06
    return spy_cfg, qqq_cfg


def test_vwap_mr_spy_only():
    """VWAP MR only for SPY (SPX-scaled, higher premiums)."""
    spy_cfg, qqq_cfg = baseline()
    spy_cfg.vwap_mr.enabled = True
    # QQQ stays disabled
    return spy_cfg, qqq_cfg


def test_vwap_mr_qqq_only():
    """VWAP MR only for QQQ."""
    spy_cfg, qqq_cfg = baseline()
    qqq_cfg.vwap_mr.enabled = True
    return spy_cfg, qqq_cfg


def test_vwap_mr_quicker_spy_only():
    """VWAP MR quicker params, SPY only."""
    spy_cfg, qqq_cfg = baseline()
    spy_cfg.vwap_mr.enabled = True
    spy_cfg.vwap_mr.max_hold_bars = 15
    spy_cfg.vwap_mr.target_vwap_return_pct = 0.06
    return spy_cfg, qqq_cfg


def test_orb150_plus_vwap_quicker():
    """Combo: ORB entry=150 + VWAP MR quicker."""
    spy_cfg, qqq_cfg = baseline()
    spy_cfg.orb.entry_end_bar = 150
    qqq_cfg.orb.entry_end_bar = 150
    spy_cfg.vwap_mr.enabled = True
    qqq_cfg.vwap_mr.enabled = True
    spy_cfg.vwap_mr.max_hold_bars = 15
    qqq_cfg.vwap_mr.max_hold_bars = 15
    spy_cfg.vwap_mr.target_vwap_return_pct = 0.06
    qqq_cfg.vwap_mr.target_vwap_return_pct = 0.06
    return spy_cfg, qqq_cfg


def test_orb150_plus_vwap_spy():
    """Combo: ORB entry=150 + VWAP MR SPY-only."""
    spy_cfg, qqq_cfg = baseline()
    spy_cfg.orb.entry_end_bar = 150
    qqq_cfg.orb.entry_end_bar = 150
    spy_cfg.vwap_mr.enabled = True
    spy_cfg.vwap_mr.max_hold_bars = 15
    spy_cfg.vwap_mr.target_vwap_return_pct = 0.06
    return spy_cfg, qqq_cfg


def test_orb150_plus_vwap_qqq_default():
    """Combo: ORB entry=150 + VWAP MR QQQ-only (default params)."""
    spy_cfg, qqq_cfg = baseline()
    spy_cfg.orb.entry_end_bar = 150
    qqq_cfg.orb.entry_end_bar = 150
    qqq_cfg.vwap_mr.enabled = True
    return spy_cfg, qqq_cfg


def test_orb150_plus_vwap_qqq_quicker():
    """Combo: ORB entry=150 + VWAP MR QQQ-only quicker params."""
    spy_cfg, qqq_cfg = baseline()
    spy_cfg.orb.entry_end_bar = 150
    qqq_cfg.orb.entry_end_bar = 150
    qqq_cfg.vwap_mr.enabled = True
    qqq_cfg.vwap_mr.max_hold_bars = 15
    qqq_cfg.vwap_mr.target_vwap_return_pct = 0.06
    return spy_cfg, qqq_cfg


def test_vwap_mr_qqq_quicker():
    """VWAP MR QQQ-only with quicker params."""
    spy_cfg, qqq_cfg = baseline()
    qqq_cfg.vwap_mr.enabled = True
    qqq_cfg.vwap_mr.max_hold_bars = 15
    qqq_cfg.vwap_mr.target_vwap_return_pct = 0.06
    return spy_cfg, qqq_cfg


def test_orb150_vm_qqq_default_vm_spy_quicker():
    """Kitchen sink: ORB150 + VM QQQ default + VM SPY quicker."""
    spy_cfg, qqq_cfg = baseline()
    spy_cfg.orb.entry_end_bar = 150
    qqq_cfg.orb.entry_end_bar = 150
    spy_cfg.vwap_mr.enabled = True
    spy_cfg.vwap_mr.max_hold_bars = 15
    spy_cfg.vwap_mr.target_vwap_return_pct = 0.06
    qqq_cfg.vwap_mr.enabled = True
    return spy_cfg, qqq_cfg


if __name__ == "__main__":
    print("Running Tier 2 v3 focused tests...")
    print("Baseline: 175t, WR=58.6%, PF=3.69, PnL=+$79,450\n")

    tests = [
        ("BASELINE", baseline),
        ("ORB entry end=150", test_orb_entry_150),
        ("VM QQQ-only (default)", test_vwap_mr_qqq_only),
        ("VM QQQ-only (quicker)", test_vwap_mr_qqq_quicker),
        ("VM quicker SPY-only", test_vwap_mr_quicker_spy_only),
        ("ORB150 + VM QQQ default", test_orb150_plus_vwap_qqq_default),
        ("ORB150 + VM QQQ quicker", test_orb150_plus_vwap_qqq_quicker),
        ("ORB150 + VM SPY quicker", test_orb150_plus_vwap_spy),
        ("ORB150 + VM QQQ dflt + SPY qk", test_orb150_vm_qqq_default_vm_spy_quicker),
    ]

    results = {}
    for label, config_fn in tests:
        spy_cfg, qqq_cfg = config_fn()
        r = run_shared(spy_cfg, qqq_cfg, label)
        results[label] = r

    # Summary comparison
    print(f"\n{'='*90}")
    print(f"  SUMMARY COMPARISON")
    print(f"{'='*90}")
    print(f"  {'Test':<38} {'Trades':>6} {'WR':>6} {'PF':>6} {'PnL':>12} {'MaxDD':>7} {'VM':>8}")
    print(f"  {'-'*84}")
    for label, r in results.items():
        short = label[:36]
        vm = r['strats'].get('vwap_mr', 0)
        print(f"  {short:<38} {r['total_trades']:>5}  {r['wr']:>5.1f}% {r['pf']:>5.2f} "
              f"${r['pnl']:>+10,.0f} {r['max_dd']:>6.1%} ${vm:>+7,.0f}")
