#!/usr/bin/env python3
"""Compare VM-disabled baseline vs best VM configs."""

import pandas as pd
from trading_engine.config import EngineConfig
from trading_engine.data.scalp_backtester import ScalpBacktester

bars = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"])
bars.set_index("timestamp", inplace=True)

# ── 1. Baseline (no VM) ────────────────────────────────────────
cfg = EngineConfig()
cfg.vwap_mr.enabled = False
cfg.mean_reversion.enabled = False
bt = ScalpBacktester(config=cfg, account_size=10_000, spx_mode=True)
r = bt.run(bars, ticker="SPY", interval="1m", verbose=False)
print(f"BASELINE (no VM):  {r.total_trades} trades, WR={r.win_rate:.1f}%, "
      f"PnL=${r.total_pnl:+,.0f}, Days={r.days_traded}, PF={r.profit_factor:.2f}")
base_pnl = r.total_pnl
base_days = r.days_traded

# ── 2. Best H: hold=25 ─────────────────────────────────────────
cfg = EngineConfig()
cfg.mean_reversion.enabled = False
cfg.vwap_mr.enabled = True
cfg.vwap_mr.min_vwap_deviation_pct = 0.25
cfg.vwap_mr.max_trades_per_day = 1
cfg.vwap_mr.max_hold_bars = 25
cfg.vwap_mr.stop_deviation_pct = 0.40
cfg.vwap_mr.target_vwap_return_pct = 0.04
cfg.vwap_mr.rsi_overbought = 70.0
cfg.vwap_mr.rsi_oversold = 30.0
bt = ScalpBacktester(config=cfg, account_size=10_000, spx_mode=True)
r = bt.run(bars, ticker="SPY", interval="1m", verbose=False)
vm_t = [t for t in r.trades if t.tier == "vwap_mr"]
vm_pnl = sum(t.total_pnl for t in vm_t)
vm_days = len(set(str(t.expiry_date) for t in vm_t))
print(f"H: hold=25:        {r.total_trades} trades ({len(vm_t)} VM), WR={r.win_rate:.1f}%, "
      f"PnL=${r.total_pnl:+,.0f} (Δ${r.total_pnl-base_pnl:+,.0f}), "
      f"Days={r.days_traded} (+{r.days_traded-base_days}), PF={r.profit_factor:.2f}, "
      f"VM P&L=${vm_pnl:+,.0f} on {vm_days} days")

# ── 3. C: hold=20 (more days) ──────────────────────────────────
cfg = EngineConfig()
cfg.mean_reversion.enabled = False
cfg.vwap_mr.enabled = True
cfg.vwap_mr.min_vwap_deviation_pct = 0.15
cfg.vwap_mr.max_trades_per_day = 1
cfg.vwap_mr.max_hold_bars = 20
cfg.vwap_mr.stop_deviation_pct = 0.30
cfg.vwap_mr.target_vwap_return_pct = 0.06
cfg.vwap_mr.rsi_overbought = 65.0
cfg.vwap_mr.rsi_oversold = 35.0
bt = ScalpBacktester(config=cfg, account_size=10_000, spx_mode=True)
r = bt.run(bars, ticker="SPY", interval="1m", verbose=False)
vm_t = [t for t in r.trades if t.tier == "vwap_mr"]
vm_pnl = sum(t.total_pnl for t in vm_t)
vm_days = len(set(str(t.expiry_date) for t in vm_t))
print(f"C: hold=20:        {r.total_trades} trades ({len(vm_t)} VM), WR={r.win_rate:.1f}%, "
      f"PnL=${r.total_pnl:+,.0f} (Δ${r.total_pnl-base_pnl:+,.0f}), "
      f"Days={r.days_traded} (+{r.days_traded-base_days}), PF={r.profit_factor:.2f}, "
      f"VM P&L=${vm_pnl:+,.0f} on {vm_days} days")

# ── 4. H: rsi65/35 (best total PnL variant) ────────────────────
cfg = EngineConfig()
cfg.mean_reversion.enabled = False
cfg.vwap_mr.enabled = True
cfg.vwap_mr.min_vwap_deviation_pct = 0.25
cfg.vwap_mr.max_trades_per_day = 1
cfg.vwap_mr.max_hold_bars = 30
cfg.vwap_mr.stop_deviation_pct = 0.40
cfg.vwap_mr.target_vwap_return_pct = 0.04
cfg.vwap_mr.rsi_overbought = 65.0
cfg.vwap_mr.rsi_oversold = 35.0
bt = ScalpBacktester(config=cfg, account_size=10_000, spx_mode=True)
r = bt.run(bars, ticker="SPY", interval="1m", verbose=False)
vm_t = [t for t in r.trades if t.tier == "vwap_mr"]
vm_pnl = sum(t.total_pnl for t in vm_t)
vm_days = len(set(str(t.expiry_date) for t in vm_t))
print(f"H: rsi65/35:       {r.total_trades} trades ({len(vm_t)} VM), WR={r.win_rate:.1f}%, "
      f"PnL=${r.total_pnl:+,.0f} (Δ${r.total_pnl-base_pnl:+,.0f}), "
      f"Days={r.days_traded} (+{r.days_traded-base_days}), PF={r.profit_factor:.2f}, "
      f"VM P&L=${vm_pnl:+,.0f} on {vm_days} days")

# ── 5. C: dev=0.12 (most days) ─────────────────────────────────
cfg = EngineConfig()
cfg.mean_reversion.enabled = False
cfg.vwap_mr.enabled = True
cfg.vwap_mr.min_vwap_deviation_pct = 0.12
cfg.vwap_mr.max_trades_per_day = 1
cfg.vwap_mr.max_hold_bars = 25
cfg.vwap_mr.stop_deviation_pct = 0.30
cfg.vwap_mr.target_vwap_return_pct = 0.06
cfg.vwap_mr.rsi_overbought = 65.0
cfg.vwap_mr.rsi_oversold = 35.0
bt = ScalpBacktester(config=cfg, account_size=10_000, spx_mode=True)
r = bt.run(bars, ticker="SPY", interval="1m", verbose=False)
vm_t = [t for t in r.trades if t.tier == "vwap_mr"]
vm_pnl = sum(t.total_pnl for t in vm_t)
vm_days = len(set(str(t.expiry_date) for t in vm_t))
print(f"C: dev=0.12:       {r.total_trades} trades ({len(vm_t)} VM), WR={r.win_rate:.1f}%, "
      f"PnL=${r.total_pnl:+,.0f} (Δ${r.total_pnl-base_pnl:+,.0f}), "
      f"Days={r.days_traded} (+{r.days_traded-base_days}), PF={r.profit_factor:.2f}, "
      f"VM P&L=${vm_pnl:+,.0f} on {vm_days} days")

print(f"\n  Baseline: ${base_pnl:+,.0f}, {base_days} days")
print(f"  Goal: Add P&L on DEAD_FLAT days without hurting existing strategies")
