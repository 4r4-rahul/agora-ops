#!/usr/bin/env python3
"""Analyze morning vs afternoon trade performance."""
import sys; sys.path.insert(0, ".")
import pandas as pd
from trading_engine.config import EngineConfig as EC
from trading_engine.data.scalp_backtester import ScalpBacktester as SB

df = pd.read_csv("data/intraday/SPY_ibkr_1m_180d.csv", parse_dates=["timestamp"], index_col="timestamp")
df.index = pd.to_datetime(df.index, utc=True)

c = EC()
c.scalp.iv_discount_enabled = True
c.scalp.rv_iv_min_ratio = 0.8
r = SB(config=c, account_size=10000, spx_mode=True).run(df, ticker="SPY", interval="1m", verbose=False)

am_pnl = 0; am_trades = 0; am_wins = 0
pm_pnl = 0; pm_trades = 0; pm_wins = 0
for t in r.trades:
    hour = t.entry_time.hour
    # UTC 18:00 = 2PM ET
    session = "AM" if hour < 18 else "PM"
    if session == "AM":
        am_pnl += t.total_pnl; am_trades += 1
        if t.total_pnl > 0: am_wins += 1
    else:
        pm_pnl += t.total_pnl; pm_trades += 1
        if t.total_pnl > 0: pm_wins += 1
    print(f"  {session} [{t.tier[0].upper()}] {t.entry_time:%m/%d %H:%M} pnl=${t.total_pnl:+,.0f} {t.exit_reason}")

wr_am = (am_wins/am_trades*100) if am_trades else 0
wr_pm = (pm_wins/pm_trades*100) if pm_trades else 0
print(f"\nMORNING:   {am_trades} trades, {am_wins} wins ({wr_am:.0f}% WR), PnL=${am_pnl:+,.0f}")
print(f"AFTERNOON: {pm_trades} trades, {pm_wins} wins ({wr_pm:.0f}% WR), PnL=${pm_pnl:+,.0f}")
print(f"TOTAL:     {r.total_trades} trades, PnL=${r.total_pnl:+,.0f}")
print(f"\nMorning drags down: ${am_pnl:+,.0f} vs afternoon ${pm_pnl:+,.0f}")
if am_pnl < 0:
    print(f"Cutting morning would add ${abs(am_pnl):,.0f} to P&L -> ${pm_pnl:+,.0f}")
