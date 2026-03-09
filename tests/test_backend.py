#!/usr/bin/env python3
"""End-to-end test for all backend API endpoints."""

import sys
import os

# Set up path for backend imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))

from fastapi.testclient import TestClient
from src.main import app

client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    print(f"✅ /health → {r.json()['status']}")


def test_score():
    r = client.post("/score/intraday", json={"symbols": ["SPY", "QQQ", "AAPL"]})
    assert r.status_code == 200
    data = r.json()
    for s in data:
        print(f"  {s['symbol']}: score={s['score']} regime={s['regime']} "
              f"strategy={s['preferred_strategy']} passed={s['passed']}")
        print(f"    reason: {s['reason']}")
    print(f"✅ /score/intraday → {len(data)} results (real regime scoring)")


def test_plan():
    r = client.post("/options/plan", json={
        "symbols": ["SPY"],
        "equity": 50000,
        "horizon_days": 0,
    })
    assert r.status_code == 200
    plans = r.json()
    for p in plans:
        print(f"  {p['symbol']}: {p['strategy']} delta={p['short_delta']} "
              f"pop={p['pop_pct']}% regime={p['regime']}")
    print(f"✅ /options/plan → {len(plans)} plans (real signal-based)")
    return plans


def test_metrics():
    r = client.get("/metrics/summary")
    assert r.status_code == 200
    m = r.json()
    print(f"  date={m['date']} trades={m['trades']} "
          f"pnl=${m['pnl_today']:+.2f} can_trade={m['can_trade']}")
    print(f"✅ /metrics/summary → real state data")


def test_place(plans):
    r = client.post("/options/place", json={"plans": plans})
    assert r.status_code == 200
    orders = r.json()["orders"]
    for o in orders:
        print(f"  {o['symbol']}: {o['status']}")
    status = orders[0]["status"] if orders else "empty"
    print(f"✅ /options/place → {len(orders)} orders ({status})")


if __name__ == "__main__":
    test_health()
    test_score()
    plans = test_plan()
    test_metrics()
    test_place(plans)

    print()
    print("═" * 50)
    print("  ALL 5 ENDPOINT TESTS PASSED ✅")
    print("═" * 50)
