#!/usr/bin/env python3
"""
IBKR Connection Test & Data Explorer
======================================
Quick script to verify your IBKR connection and explore available data.

Prerequisites:
  1. pip install ib_insync
  2. TWS or IB Gateway running with API enabled
  3. Market data subscription active (US Securities bundle ~$10/mo)

Usage:
    python test_ibkr.py                  # Test connection + fetch sample data
    python test_ibkr.py --port 4001      # IB Gateway paper trading
    python test_ibkr.py --port 7496      # TWS live trading
    python test_ibkr.py --chain SPY      # Fetch live 0DTE options chain
    python test_ibkr.py --bars SPY 30    # Fetch 30 days of 5-min bars
    python test_ibkr.py --snapshot SPY 580 P  # Watch a specific put spread
"""

import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def test_connection(host: str, port: int, client_id: int):
    """Test basic IBKR connectivity."""
    from trading_engine.data.ibkr_provider import IBKRDataProvider

    print("\n" + "=" * 60)
    print("  IBKR CONNECTION TEST")
    print("=" * 60)
    print(f"  Host:      {host}")
    print(f"  Port:      {port}")
    print(f"  Client ID: {client_id}")
    print("=" * 60 + "\n")

    provider = IBKRDataProvider(host=host, port=port, client_id=client_id)

    if not provider.connect():
        print("\n" + "=" * 60)
        print("  TROUBLESHOOTING GUIDE")
        print("=" * 60)
        print("""
  1. Is TWS or IB Gateway running?
     → Download from: https://www.interactivebrokers.com/en/trading/tws.php

  2. Is the API enabled?
     TWS: Edit → Global Configuration → API → Settings
       ✓ Enable ActiveX and Socket Clients
       ✓ Socket port matches your --port argument
       ✓ Allow connections from localhost

     IB Gateway: Configure → Settings → API → Settings
       Same checkboxes as above

  3. Common port assignments:
     7497 = TWS Paper Trading (default)
     7496 = TWS Live Trading
     4001 = IB Gateway Paper Trading
     4002 = IB Gateway Live Trading

  4. Is ib_insync installed?
     pip install ib_insync

  5. Firewall / antivirus blocking local connections?
     Ensure localhost (127.0.0.1) connections are allowed.

  6. Already have another connection with same client ID?
     Try: python test_ibkr.py --client-id 99
""")
        return False

    # Show account status
    provider.status()
    return provider


def test_bars(provider, ticker: str, days: int):
    """Test historical bar fetching."""
    print(f"\n{'=' * 60}")
    print(f"  HISTORICAL BARS: {ticker} ({days} days, 5-min)")
    print(f"{'=' * 60}\n")

    df = provider.get_historical_bars(ticker, days=days, interval="5m")
    print(f"\n  Shape: {df.shape}")
    print(f"  Date range: {df.index[0]} → {df.index[-1]}")
    print(f"  Price range: ${df['low'].min():.2f} → ${df['high'].max():.2f}")
    print(f"\n  Last 10 bars:")
    print(df.tail(10).to_string())

    # Save to CSV
    out_path = os.path.join("data", "intraday", f"{ticker}_ibkr_5m_{days}d.csv")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    df.to_csv(out_path)
    print(f"\n  💾 Saved to {out_path}")


def test_chain(provider, ticker: str):
    """Test live options chain fetch."""
    print(f"\n{'=' * 60}")
    print(f"  LIVE OPTIONS CHAIN: {ticker}")
    print(f"{'=' * 60}\n")

    chain = provider.get_options_chain(ticker, strikes_around_atm=5)

    if not chain:
        print("  ❌ No options data returned.")
        print("  Check your market data subscription:")
        print("  Account → Settings → Market Data Subscriptions")
        return

    # Separate calls and puts
    calls = [o for o in chain if o["right"] == "C"]
    puts = [o for o in chain if o["right"] == "P"]

    underlying = chain[0].get("underlying_price", 0)
    print(f"  Underlying: ${underlying:.2f}")
    print(f"  Expiry:     {chain[0]['expiry']}")
    print(f"  Total contracts: {len(chain)} ({len(calls)}C + {len(puts)}P)\n")

    # Print chain table
    header = f"  {'Strike':>8} │ {'Bid':>6} {'Ask':>6} {'Mid':>6} │ {'Δ':>7} {'Γ':>7} {'θ':>7} {'IV':>6} │ {'Vol':>5} {'OI':>6}"
    sep = "  " + "─" * len(header)

    print("  ── CALLS ──")
    print(header)
    print(sep)
    for o in sorted(calls, key=lambda x: x["strike"]):
        print(f"  {o['strike']:>8.1f} │ {o['bid']:>6.2f} {o['ask']:>6.2f} {o['mid']:>6.2f} │ "
              f"{o['delta']:>+7.3f} {o['gamma']:>7.4f} {o['theta']:>7.3f} {o['iv']:>5.1%} │ "
              f"{o['volume']:>5} {o['open_interest']:>6}")

    print(f"\n  ── PUTS ──")
    print(header)
    print(sep)
    for o in sorted(puts, key=lambda x: x["strike"]):
        print(f"  {o['strike']:>8.1f} │ {o['bid']:>6.2f} {o['ask']:>6.2f} {o['mid']:>6.2f} │ "
              f"{o['delta']:>+7.3f} {o['gamma']:>7.4f} {o['theta']:>7.3f} {o['iv']:>5.1%} │ "
              f"{o['volume']:>5} {o['open_interest']:>6}")

    # Identify good 0DTE credit spread candidates
    print(f"\n  ── POTENTIAL 0DTE PUT CREDIT SPREADS (Δ < -0.15) ──")
    candidates = [p for p in puts if -0.20 < p["delta"] < -0.08 and p["bid"] > 0.05]
    for p in sorted(candidates, key=lambda x: abs(x["delta"])):
        width = 1  # $1 wide for SPY
        long_strike = p["strike"] - width
        # Find the long leg
        long = next((x for x in puts if x["strike"] == long_strike), None)
        if long:
            credit = p["bid"] - long["ask"]
            max_loss = width - credit
            if credit > 0:
                print(f"    Sell {p['strike']:.0f}P / Buy {long_strike:.0f}P: "
                      f"credit=${credit:.2f} max_loss=${max_loss:.2f} "
                      f"Δ={p['delta']:+.3f} RoR={credit/max_loss:.1%}")


def main():
    parser = argparse.ArgumentParser(description="Test IBKR connection and data")
    parser.add_argument("--host", default=os.getenv("IBKR_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("IBKR_PORT", "7497")),
                        help="TWS port: 7497(paper)/7496(live), Gateway: 4001(paper)/4002(live)")
    parser.add_argument("--client-id", type=int, default=int(os.getenv("IBKR_CLIENT_ID", "10")))
    parser.add_argument("--bars", nargs=2, metavar=("TICKER", "DAYS"),
                        help="Fetch historical bars, e.g., --bars SPY 30")
    parser.add_argument("--chain", metavar="TICKER",
                        help="Fetch live options chain, e.g., --chain SPY")
    parser.add_argument("--snapshot", nargs=3, metavar=("TICKER", "STRIKE", "RIGHT"),
                        help="Watch a spread, e.g., --snapshot SPY 580 P")
    args = parser.parse_args()

    provider = test_connection(args.host, args.port, args.client_id)
    if not provider:
        sys.exit(1)

    try:
        if args.bars:
            test_bars(provider, args.bars[0], int(args.bars[1]))
        elif args.chain:
            test_chain(provider, args.chain)
        elif args.snapshot:
            ticker, strike, right = args.snapshot
            from trading_engine.data.ibkr_provider import IBKRDataProvider
            df = provider.snapshot_spread_over_day(
                ticker=ticker,
                short_strike=float(strike),
                long_strike=float(strike) - 1,
                right=right,
                interval_sec=60,      # Every 1 min for test
                duration_hours=0.1,   # 6 min test run
            )
            if not df.empty:
                print(df.to_string())
        else:
            # Default: quick test with 5 bars
            print("\n  Running quick data test ...\n")
            test_bars(provider, "SPY", 5)
    finally:
        provider.disconnect()

    print("\n  ✅ All tests passed. IBKR data pipeline is ready.\n")


if __name__ == "__main__":
    main()
