#!/usr/bin/env python
"""
Standalone fill-canary runner — answers "will the IBKR paper account fill a marketable order?"
independent of the engine. Uses a distinct clientId (99) so it never collides with the running
engine. Run during market hours with TWS up:

    PYTHONPATH=. .venv/bin/python scripts/fill_canary.py
"""
import asyncio
import json
import sys

from agora.core.config import AgoraSettings
from agora.ops.fill_canary import run_fill_canary


async def _main() -> int:
    s = AgoraSettings()
    res = await run_fill_canary(
        host=s.ibkr_host,
        port=s.ibkr_port,
        client_id=99,
        market_data_type=getattr(s, "ibkr_market_data_type", 1),
        db_path=str(s.db_path),
    )
    print(json.dumps(res, indent=2, default=str))
    verdict = res.get("outcome")
    if verdict == "filled":
        print("\n✅ BROKER FILLS MARKETABLE ORDERS — the ~2% strategy fill rate is UPSTREAM "
              "(our pricing/walk), not the paper account.")
        return 0
    if verdict == "no_fill":
        print("\n🚨 BROKER DID NOT FILL A MAXIMALLY-MARKETABLE ORDER — the fill bottleneck is the "
              "PAPER ACCOUNT fill engine / market data, NOT our execution code. Fix is TWS/data-side.")
        return 0
    print(f"\n⚠️ Inconclusive ({verdict}): {res.get('note')}")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
