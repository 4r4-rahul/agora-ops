#!/usr/bin/env python3
"""Quick IBKR connection test - checks account info and market data availability."""

import math
import sys
import ib_insync


def main():
    ib = ib_insync.IB()

    errors = []

    def on_error(reqId, errorCode, errorString, contract):
        errors.append((reqId, errorCode, errorString))
        print(f"  [Error {errorCode}] {errorString}")

    ib.errorEvent += on_error

    try:
        ib.connect('127.0.0.1', 7497, clientId=99, timeout=10)
    except Exception as e:
        print(f"FAILED to connect: {e}")
        sys.exit(1)

    print("=== ACCOUNT INFO ===")
    accts = ib.managedAccounts()
    print(f"Account(s): {accts}")
    print(f"Server time: {ib.reqCurrentTime()}")

    summary = ib.accountSummary()
    for item in summary:
        if item.tag in ('NetLiquidation', 'TotalCashValue', 'BuyingPower', 'AvailableFunds'):
            print(f"  {item.tag}: ${float(item.value):,.2f}")

    spy = ib_insync.Stock('SPY', 'SMART', 'USD')
    ib.qualifyContracts(spy)

    # ---------- Market Data: Type 1 (live) ----------
    print("\n=== TYPE 1 (LIVE) TEST ===")
    errors.clear()
    ib.reqMarketDataType(1)
    ticker = ib.reqMktData(spy, '', False, False)
    ib.sleep(3)

    price = ticker.marketPrice()
    has_live = bool(price and not math.isnan(price) and price > 0)
    print(f"  marketPrice={price}  last={ticker.last}  bid={ticker.bid}  ask={ticker.ask}")
    print(f"  Live data available: {has_live}")
    got_10089 = any(e[1] == 10089 for e in errors)
    ib.cancelMktData(spy)
    ib.sleep(0.5)

    # ---------- Market Data: Type 3 (delayed) ----------
    print("\n=== TYPE 3 (DELAYED) TEST ===")
    errors.clear()
    ib.reqMarketDataType(3)
    ticker2 = ib.reqMktData(spy, '', False, False)
    ib.sleep(3)

    price2 = ticker2.marketPrice()
    has_delayed = bool(price2 and not math.isnan(price2) and price2 > 0)
    print(f"  marketPrice={price2}  last={ticker2.last}  bid={ticker2.bid}  ask={ticker2.ask}")
    print(f"  Delayed data available: {has_delayed}")
    ib.cancelMktData(spy)
    ib.sleep(0.5)

    # ---------- Market Data: Type 4 (delayed frozen) ----------
    print("\n=== TYPE 4 (DELAYED FROZEN) TEST ===")
    errors.clear()
    ib.reqMarketDataType(4)
    ticker3 = ib.reqMktData(spy, '', False, False)
    ib.sleep(3)

    price3 = ticker3.marketPrice()
    has_frozen = bool(price3 and not math.isnan(price3) and price3 > 0)
    print(f"  marketPrice={price3}  last={ticker3.last}  bid={ticker3.bid}  ask={ticker3.ask}")
    print(f"  Delayed-frozen data available: {has_frozen}")
    ib.cancelMktData(spy)
    ib.sleep(0.5)

    # ---------- Options chain ----------
    print("\n=== OPTIONS CHAIN TEST ===")
    chains = ib.reqSecDefOptParams(spy.symbol, '', spy.secType, spy.conId)
    if chains:
        chain = chains[0]
        print(f"  Exchange: {chain.exchange}")
        print(f"  Expirations available: {len(chain.expirations)}")
        exps = sorted(chain.expirations)[:5]
        print(f"  Nearest 5: {exps}")
        strikes = sorted(chain.strikes)
        mid = len(strikes) // 2
        print(f"  Total strikes: {len(strikes)}")
        print(f"  Sample strikes: {strikes[mid-3:mid+3]}")
    else:
        print("  No option chains found")

    # ---------- Summary ----------
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    if has_live:
        print("PASS: Live market data (Type 1) WORKS")
    elif has_delayed:
        print("OK: No live data, but DELAYED (Type 3) works - fine for paper testing")
    elif has_frozen:
        print("OK: Only delayed-frozen (Type 4) works - limited but usable")
    else:
        print("FAIL: NO market data available at all")
        if got_10089:
            print()
            print("IBKR Error 10089: Market data subscription needed.")
            print()
            print("TO FIX - subscribe to free delayed market data:")
            print("  1. Log in to IBKR Account Management (Client Portal)")
            print("     https://www.interactivebrokers.com/sso/Login")
            print("  2. Navigate to: Settings > User Settings > Market Data Subscriptions")
            print("     (or: https://www.interactivebrokers.com/en/trading/market-data.php)")
            print("  3. Subscribe to these FREE bundles:")
            print("     - 'US Equity and Options Add-On Streaming Bundle'")
            print("     - 'US Securities Snapshot and Futures Value Bundle' (sometimes called NBBO)")
            print("  4. Wait 5-10 minutes for activation, then restart TWS")
            print()
            print("  ALTERNATIVE - enable delayed data in TWS API settings:")
            print("  1. In TWS: Edit > Global Configuration > API > Settings")
            print("  2. Ensure 'Enable ActiveX and Socket Clients' is checked")
            print()
            print("  NOTE: Paper accounts linked to a funded live account")
            print("  inherit the live account's subscriptions automatically.")

    ib.disconnect()
    print("\nDONE")


if __name__ == '__main__':
    main()
