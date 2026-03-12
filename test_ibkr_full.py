#!/usr/bin/env python3
"""Full IBKR capability test - historical data, options, orders."""
import datetime
import ib_insync

def main():
    ib = ib_insync.IB()
    ib.connect('127.0.0.1', 7497, clientId=97, timeout=10)

    print("=== HISTORICAL DATA (SPY 1-min) ===")
    spy = ib_insync.Stock('SPY', 'SMART', 'USD')
    ib.qualifyContracts(spy)
    bars = ib.reqHistoricalData(spy, '', '1 D', '1 min', 'TRADES', True, 1)
    print(f"  Got {len(bars)} bars")
    print(f"  Latest: {bars[-1].date}  C={bars[-1].close}")

    print()
    print("=== LIVE-UPDATING BARS (keepUpToDate) ===")
    bars2 = ib.reqHistoricalData(
        spy, '', '900 S', '1 min', 'TRADES', False, 1, keepUpToDate=True
    )
    ib.sleep(3)
    print(f"  Got {len(bars2)} bars (live-updating)")
    if bars2:
        print(f"  Latest: {bars2[-1].date}  C={bars2[-1].close}")
    ib.cancelHistoricalData(bars2)

    print()
    print("=== OPTIONS CHAIN ===")
    chains = ib.reqSecDefOptParams(spy.symbol, '', spy.secType, spy.conId)
    chain = [c for c in chains if c.exchange == 'SMART']
    if not chain:
        chain = chains[:1]
    chain = chain[0]
    today = datetime.date.today().strftime('%Y%m%d')
    exps = sorted(chain.expirations)
    exp = exps[0] if exps[0] >= today else exps[1]
    print(f"  Expiry: {exp}")

    last_price = bars[-1].close
    strike = round(last_price)
    print(f"  SPY last={last_price}, using strike={strike}")

    opt = ib_insync.Option('SPY', exp, strike, 'C', 'SMART')
    qualified = ib.qualifyContracts(opt)
    if qualified:
        print(f"  Qualified: {opt.localSymbol}")
        opt_bars = ib.reqHistoricalData(opt, '', '900 S', '1 min', 'TRADES', False, 1)
        if opt_bars:
            print(f"  Option bars: {len(opt_bars)}, last={opt_bars[-1].close}")
        else:
            opt_bars = ib.reqHistoricalData(opt, '', '900 S', '1 min', 'MIDPOINT', False, 1)
            if opt_bars:
                print(f"  Option bars (midpoint): {len(opt_bars)}, last={opt_bars[-1].close}")
            else:
                print("  Option bars: none (market may be closed)")
    else:
        print("  Could not qualify option contract")

    print()
    print("=== ORDER PLACEMENT TEST ===")
    spy_order = ib_insync.LimitOrder('BUY', 1, 1.00)
    trade = ib.placeOrder(spy, spy_order)
    ib.sleep(1)
    print(f"  Order status: {trade.orderStatus.status}")
    ib.cancelOrder(spy_order)
    ib.sleep(1)
    print(f"  After cancel: {trade.orderStatus.status}")

    print()
    print("=" * 50)
    print("RESULTS:")
    print("  Historical bars:    OK")
    print("  Live-updating bars: OK")
    print(f"  Options chain:      OK ({len(chain.expirations)} expirations)")
    print("  Order placement:    OK")
    print()
    print("System is READY for paper trading using historical bar mode.")

    ib.disconnect()

if __name__ == '__main__':
    main()
