"""
Phase-2 feasibility probe: can IBKR serve core market data (spot + option-chain
definition) at acceptable quality vs yfinance — BEFORE we build a routing layer?

Answers four questions that decide whether phase-2 is a win or a regression:
  1. Live or DELAYED data?  (marketDataType: 1=live, 2=frozen, 3=delayed, 4=delayed-frozen)
  2. Coverage — do large-caps AND small-caps (AAOI) return a price?
  3. Agreement — does IBKR spot match yfinance within tolerance?
  4. Option-chain feasibility — can we pull the chain DEFINITION (strikes/expiries)
     without burning market-data lines? (the expensive per-strike quotes come later)

Read-only. Snapshot market data (auto-cancels, minimal line usage). Dedicated
clientId=15 so it never collides with the running app's connections (4/9/10/11/12).
"""
import asyncio
import time

TICKERS = ["SPY", "NVDA", "AAPL", "AAOI"]   # 2 mega-cap, 1 large, 1 small-cap tail


async def main():
    from ib_insync import IB, Stock
    ib = IB()
    try:
        await ib.connectAsync("127.0.0.1", 7497, clientId=15, timeout=15)
    except Exception as exc:
        print(f"CONNECT FAILED: {exc!r}")
        return
    print(f"connected: {ib.isConnected()}  serverVersion={ib.client.serverVersion()}")

    # Ask for LIVE; IBKR downgrades to delayed(3) automatically if unsubscribed.
    ib.reqMarketDataType(1)

    # ── yfinance comparison baseline ──────────────────────────────────────
    import yfinance as yf
    yf_px = {}
    for t in TICKERS:
        try:
            fi = yf.Ticker(t).fast_info
            yf_px[t] = float(fi.get("lastPrice") or fi.get("previousClose") or 0)
        except Exception as e:
            yf_px[t] = 0.0

    MDT = {1: "LIVE", 2: "frozen", 3: "DELAYED", 4: "delayed-frozen"}
    print(f"\n{'ticker':<7}{'ibkr_last':>11}{'ibkr_bid':>10}{'ibkr_ask':>10}"
          f"{'type':>16}{'yf_last':>10}{'diff%':>8}{'latency':>9}")
    for t in TICKERS:
        c = Stock(t, "SMART", "USD")
        try:
            await ib.qualifyContractsAsync(c)
        except Exception as e:
            print(f"{t:<7}  qualify failed: {e!r}")
            continue
        t0 = time.monotonic()
        tk = ib.reqMktData(c, "", snapshot=True, regulatorySnapshot=False)
        # snapshot fills over a few ticks; poll up to 6s
        last = bid = ask = None
        mdt = None
        for _ in range(60):
            await asyncio.sleep(0.1)
            mdt = getattr(tk, "marketDataType", None)
            last = tk.last if tk.last == tk.last else None          # NaN-safe
            bid  = tk.bid  if tk.bid  == tk.bid  else None
            ask  = tk.ask  if tk.ask  == tk.ask  else None
            close = tk.close if tk.close == tk.close else None
            if (last and last > 0) or (bid and bid > 0) or (close and close > 0):
                break
        lat = time.monotonic() - t0
        px = (last if last and last > 0 else
              (bid + ask) / 2 if bid and ask and bid > 0 and ask > 0 else
              (close if close and close > 0 else 0))
        yfv = yf_px.get(t, 0)
        diff = (abs(px - yfv) / yfv * 100) if (px and yfv) else float("nan")
        print(f"{t:<7}{px:>11.2f}{(bid or 0):>10.2f}{(ask or 0):>10.2f}"
              f"{MDT.get(mdt, str(mdt)):>16}{yfv:>10.2f}{diff:>8.2f}{lat:>8.2f}s")
        ib.cancelMktData(c)

    # ── Option-chain DEFINITION feasibility (no per-strike quotes) ─────────
    print("\n── option-chain definition (strikes/expiries available, line-free) ──")
    try:
        spy = Stock("SPY", "SMART", "USD")
        await ib.qualifyContractsAsync(spy)
        t0 = time.monotonic()
        params = await ib.reqSecDefOptParamsAsync(spy.symbol, "", spy.secType, spy.conId)
        lat = time.monotonic() - t0
        if params:
            p = params[0]
            print(f"SPY chain def: {len(p.expirations)} expiries, {len(p.strikes)} strikes "
                  f"(exchange={p.exchange}) in {lat:.2f}s")
            exps = sorted(p.expirations)[:4]
            print(f"  sample expiries: {exps}")
        else:
            print("SPY chain def: EMPTY (no params returned)")
    except Exception as e:
        print(f"chain def FAILED: {e!r}")

    ib.disconnect()
    print("\ndisconnected. probe complete.")


if __name__ == "__main__":
    asyncio.run(main())
