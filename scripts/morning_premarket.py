#!/usr/bin/env python3
"""
Morning Pre-Market Diagnostic
==============================
Run at ~9:15 AM ET before market opens. Produces a compact
report and sends it to Discord so you know TODAY's trading plan.

Usage:
    make morning          # quick summary
    python scripts/morning_premarket.py --no-discord   # stdout only

Checks:
    1. VIX level → Regime classification (GREEN/YELLOW/RED)
    2. ATR for each ticker → position sizing guidance
    3. Trend direction (5d, 20d EMAs)
    4. Yesterday's P&L from StateManager
    5. Safety monitor status (daily/weekly/monthly limits)
    6. IBKR connectivity

Outputs a Discord-ready report.
"""

import os
import sys
import json
import argparse
import logging
from datetime import datetime, date, timedelta
from pathlib import Path

# ── Ensure project root is on sys.path ──
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import numpy as np

logging.basicConfig(
    level=logging.WARNING,
    format="%(message)s",
)
log = logging.getLogger("morning")


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────
def _get_vix() -> float | None:
    """Get VIX via yfinance (no IBKR needed)."""
    try:
        import yfinance as yf
        vix = yf.Ticker("^VIX")
        hist = vix.history(period="2d")
        if not hist.empty:
            return float(hist["Close"].iloc[-1])
    except Exception as e:
        log.warning(f"VIX fetch failed: {e}")
    return None


def _classify_regime(vix: float) -> tuple[str, str]:
    """Return (regime, emoji)."""
    if vix < 18:
        return "GREEN", "🟢"
    elif vix < 25:
        return "YELLOW", "🟡"
    else:
        return "RED", "🔴"


def _get_bars(ticker: str, period: str = "30d") -> "pd.DataFrame | None":
    """Fetch daily bars via yfinance."""
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        df = t.history(period=period)
        if df is not None and not df.empty:
            return df
    except Exception as e:
        log.warning(f"Bars fetch for {ticker} failed: {e}")
    return None


def _compute_atr(df: "pd.DataFrame", period: int = 14) -> float | None:
    """14-period ATR from daily bars."""
    if df is None or len(df) < period + 1:
        return None
    high = df["High"].values
    low = df["Low"].values
    close = df["Close"].values
    tr = np.maximum(
        high[1:] - low[1:],
        np.maximum(
            np.abs(high[1:] - close[:-1]),
            np.abs(low[1:] - close[:-1]),
        ),
    )
    atr = float(np.mean(tr[-period:]))
    return atr


def _trend_direction(df: "pd.DataFrame") -> tuple[str, str]:
    """
    Return (direction, emoji).
    Compare 5-EMA vs 20-EMA on last close.
    """
    if df is None or len(df) < 20:
        return "UNKNOWN", "❓"
    close = df["Close"]
    ema5 = close.ewm(span=5).mean().iloc[-1]
    ema20 = close.ewm(span=20).mean().iloc[-1]
    if ema5 > ema20 * 1.002:
        return "BULLISH", "📈"
    elif ema5 < ema20 * 0.998:
        return "BEARISH", "📉"
    else:
        return "NEUTRAL", "➡️"


def _ibkr_connectivity() -> tuple[bool, str]:
    """Quick TCP check to IBKR gateway."""
    import socket
    host = os.getenv("IBKR_HOST", "127.0.0.1")
    port = int(os.getenv("IBKR_PORT", "7497"))
    try:
        s = socket.create_connection((host, port), timeout=3)
        s.close()
        return True, f"✅ Connected ({host}:{port})"
    except Exception:
        return False, f"❌ Unreachable ({host}:{port})"


def _load_state_summary() -> dict:
    """Read StateManager JSON for yesterday's P&L etc."""
    state_path = ROOT / "data" / "trading_state.json"
    result = {"found": False}
    if state_path.exists():
        try:
            with open(state_path) as f:
                state = json.load(f)
            result["found"] = True
            result["daily_pnl"] = state.get("daily_pnl", 0)
            result["weekly_pnl"] = state.get("weekly_pnl", 0)
            result["monthly_pnl"] = state.get("monthly_pnl", 0)
            result["trades_today"] = state.get("trades_today", 0)
            result["last_trade_date"] = state.get("last_trade_date", "N/A")
            result["open_positions"] = len(state.get("open_positions", []))
        except Exception:
            pass
    return result


def _send_discord(content: str) -> bool:
    """Post message to Discord webhook."""
    url = os.getenv("ALERT_WEBHOOK_URL", "")
    if not url:
        return False
    try:
        import requests
        resp = requests.post(url, json={"content": content}, timeout=10)
        return resp.status_code in (200, 204)
    except Exception:
        return False


# ─────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────
def run_morning_diagnostic(send_discord: bool = True) -> str:
    """Run all checks and return formatted report."""
    from trading_engine.config import AdaptiveConfig

    lines: list[str] = []
    lines.append("```")
    lines.append("╔══════════════════════════════════════════╗")
    lines.append("║   🌅  MORNING PRE-MARKET DIAGNOSTIC     ║")
    lines.append(f"║   {datetime.now().strftime('%Y-%m-%d %H:%M ET'):>36}   ║")
    lines.append("╚══════════════════════════════════════════╝")
    lines.append("")

    # ── 1. VIX / Regime ──
    vix = _get_vix()
    if vix is not None:
        regime, emoji = _classify_regime(vix)
        adaptive = AdaptiveConfig()
        params = adaptive.for_regime(regime)
        lines.append(f"VIX:      {vix:.2f}  →  {emoji} {regime}")
        lines.append(f"Strategy: {params.preferred_strategy}")
        lines.append(f"Trading:  {'ENABLED' if params.trade_enabled else 'DISABLED'}")
        lines.append(f"Position: {params.position_size_mult:.0%} size")
        lines.append(f"Delta:    {params.delta:.2f}  |  Stop: {params.stop_mult:.1f}x  |  TP: {params.profit_target:.0%}")
    else:
        regime = "UNKNOWN"
        lines.append("VIX:      ❌ Failed to fetch")
    lines.append("")

    # ── 2. Ticker Analysis ──
    tickers = os.getenv("TRADE_TICKERS", "SPY").split(",")
    # Always include SPY and QQQ for reference even if not trading
    for ref in ["SPY", "QQQ"]:
        if ref not in tickers:
            tickers.append(ref)
    lines.append("── TICKER ANALYSIS ─────────────────────────")
    for tk in tickers:
        df = _get_bars(tk)
        if df is not None and not df.empty:
            price = float(df["Close"].iloc[-1])
            atr = _compute_atr(df)
            atr_pct = (atr / price * 100) if atr else 0
            trend, t_emoji = _trend_direction(df)

            # Previous day range
            prev_high = float(df["High"].iloc[-1])
            prev_low = float(df["Low"].iloc[-1])
            prev_range = prev_high - prev_low

            lines.append(f"{tk:4s}  ${price:>8.2f}  ATR=${atr:.2f} ({atr_pct:.1f}%)  {t_emoji} {trend}")
            lines.append(f"      Prev range: ${prev_range:.2f}  H=${prev_high:.2f}  L=${prev_low:.2f}")

            # ATR safety check
            if atr_pct > 2.0:
                lines.append(f"      ⚠️  ATR > 2% — sizing reduced or skip day")
        else:
            lines.append(f"{tk:4s}  ❌ No data")
    lines.append("")

    # ── 3. IBKR Connectivity ──
    ibkr_ok, ibkr_msg = _ibkr_connectivity()
    lines.append(f"IBKR:     {ibkr_msg}")

    # ── 4. State / Yesterday's P&L ──
    state = _load_state_summary()
    if state["found"]:
        lines.append("")
        lines.append("── ACCOUNT STATE ───────────────────────────")
        lines.append(f"Daily P&L:   ${state['daily_pnl']:>+8.2f}")
        lines.append(f"Weekly P&L:  ${state['weekly_pnl']:>+8.2f}")
        lines.append(f"Monthly P&L: ${state['monthly_pnl']:>+8.2f}")
        lines.append(f"Trades:      {state['trades_today']}  (last: {state['last_trade_date']})")
        if state["open_positions"] > 0:
            lines.append(f"⚠️  Open positions: {state['open_positions']}")
    else:
        lines.append("State:    No state file (fresh start)")
    lines.append("")

    # ── 5. Trading Decision ──
    lines.append("── TODAY'S PLAN ────────────────────────────")
    if regime == "RED":
        lines.append("🛑 REGIME RED — NO TRADING TODAY")
        lines.append("   VIX too high. Monitor only.")
    elif regime == "YELLOW":
        lines.append("⚠️  REGIME YELLOW — HALF SIZE")
        lines.append("   call_credit only, cautious entries.")
    elif regime == "GREEN":
        lines.append("✅ REGIME GREEN — FULL TRADING")
        lines.append("   call_credit spreads, standard sizing.")
    else:
        lines.append("❓ REGIME UNKNOWN — MANUAL CHECK REQUIRED")

    if not ibkr_ok:
        lines.append("⚠️  IBKR not connected — start TWS first!")

    lines.append("```")

    report = "\n".join(lines)

    # Print to stdout
    print(report)

    # Send to Discord
    if send_discord:
        sent = _send_discord(report)
        if sent:
            print("\n✅ Sent to Discord")
        else:
            print("\n⚠️  Discord send failed (check ALERT_WEBHOOK_URL)")

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Morning Pre-Market Diagnostic")
    parser.add_argument("--no-discord", action="store_true", help="Skip Discord webhook")
    args = parser.parse_args()
    run_morning_diagnostic(send_discord=not args.no_discord)
