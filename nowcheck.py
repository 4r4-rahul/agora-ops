import asyncio, yfinance as yf
from ib_insync import IB, Stock, Option
from datetime import date
async def chk(ib, t, mdt):
    ib.reqMarketDataType(mdt)
    tk=yf.Ticker(t); spot=float(tk.fast_info.get("lastPrice"))
    exps=[e for e in tk.options if (date.fromisoformat(e)-date.today()).days>=7]; exp=exps[0].replace("-","")
    inc=1 if spot<60 else 5; k=round(spot/inc)*inc
    st=Stock(t,"SMART","USD"); await ib.qualifyContractsAsync(st)
    sq=ib.reqMktData(st,"",False,False)
    opt=Option(t,exp,k,"C","SMART","100","USD"); q=await ib.qualifyContractsAsync(opt)
    oq=ib.reqMktData(opt,"",False,False) if q else None
    await asyncio.sleep(6)
    iv=getattr(oq.modelGreeks,'impliedVol',None) if (oq and oq.modelGreeks) else None
    sb = sq.bid if sq.bid==sq.bid else None
    print(f"  {t} type{mdt}: STOCK bid={sb}  OPT bid={oq.bid if oq else '-'} ask={oq.ask if oq else '-'} iv={('%.1f%%'%(iv*100)) if iv else None}")
async def main():
    ib=IB(); await ib.connectAsync("127.0.0.1",7497,clientId=29,timeout=10)
    for t in ("SPY","IWM"):
        for mdt in (1,3):
            try: await chk(ib,t,mdt)
            except Exception as e: print(f"  {t} type{mdt}: ERR {repr(e)[:60]}")
    ib.disconnect()
asyncio.run(main())
