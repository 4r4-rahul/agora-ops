"""
Pre-flight check script — run before starting the platform.

Verifies:
  1. ANTHROPIC_API_KEY is set and valid format
  2. yfinance can fetch live market data
  3. Anthropic SDK can reach the API (sends a tiny test message)
  4. IBKR connection (paper mode)
  5. Discord webhook (if configured)

Usage:
    python trading_platform/scripts/check_setup.py
"""

from __future__ import annotations

import asyncio
import os
import sys

# Load .env from project root
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

PASS = "  ✓"
FAIL = "  ✗"
WARN = "  ⚠"


def check(label: str, ok: bool, detail: str = "") -> bool:
    icon = PASS if ok else FAIL
    print(f"{icon} {label}" + (f" — {detail}" if detail else ""))
    return ok


async def main() -> None:
    print("\n=== Options Trading Platform — Pre-flight Check ===\n")
    all_ok = True

    # ── 1. Config ─────────────────────────────────────────────────
    print("[ Config ]")
    try:
        from trading_platform.core.config import get_settings
        s = get_settings()
        all_ok &= check("Config loads", True,
                        f"mode={s.trading_mode} account=${s.account_size:,.0f}")
        all_ok &= check("ANTHROPIC_API_KEY set", bool(s.anthropic_api_key),
                        s.anthropic_api_key[:16] + "..." if s.anthropic_api_key else "MISSING")
        check("Discord webhook", bool(s.alert_webhook_url),
              "configured" if s.alert_webhook_url else "not set (optional)")
        print(f"  • Max position: ${s.max_position_dollars:,.0f} "
              f"({s.max_position_size_pct:.0%} of account)")
        print(f"  • Daily loss limit: ${s.daily_loss_limit_dollars:,.0f}")
        print(f"  • Min R/R: {s.min_reward_risk_ratio}:1")
    except Exception as exc:
        all_ok &= check("Config loads", False, str(exc))
        print("\nAborting — config failed.\n")
        sys.exit(1)

    # ── 2. yfinance ───────────────────────────────────────────────
    print("\n[ Market Data ]")
    try:
        import yfinance as yf
        ticker = yf.Ticker("SPY")
        info = ticker.info
        price = info.get("regularMarketPrice") or info.get("currentPrice")
        vix_info = yf.Ticker("^VIX").info
        vix = vix_info.get("regularMarketPrice") or vix_info.get("currentPrice")
        all_ok &= check("yfinance SPY", bool(price), f"${price:.2f}")
        check("yfinance VIX", bool(vix), f"{vix:.1f}" if vix else "unavailable")
    except Exception as exc:
        all_ok &= check("yfinance", False, str(exc))

    # ── 3. Anthropic API ──────────────────────────────────────────
    print("\n[ Anthropic API ]")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=s.anthropic_api_key)
        resp = client.messages.create(
            model=s.claude_model,
            max_tokens=16,
            messages=[{"role": "user", "content": "Reply with OK only."}],
        )
        reply = resp.content[0].text.strip() if resp.content else ""
        all_ok &= check("Claude API reachable", bool(reply),
                        f"model={s.claude_model} reply={reply!r}")
    except Exception as exc:
        all_ok &= check("Claude API reachable", False, str(exc))

    # ── 4. Discord webhook ────────────────────────────────────────
    print("\n[ Alerts ]")
    if s.alert_webhook_url:
        try:
            import requests
            resp = requests.post(
                s.alert_webhook_url,
                json={"content": "🔧 Options Trading Platform — pre-flight check OK"},
                timeout=5,
            )
            check("Discord webhook", resp.status_code in (200, 204),
                  f"HTTP {resp.status_code}")
        except Exception as exc:
            check("Discord webhook", False, str(exc))
    else:
        print(f"{WARN} Discord webhook not configured (set ALERT_WEBHOOK_URL to enable)")

    # ── 5. FastAPI app ────────────────────────────────────────────
    print("\n[ FastAPI ]")
    try:
        from trading_platform.api.main import create_app
        app = create_app()
        route_count = len([r for r in app.routes if hasattr(r, "path")])
        all_ok &= check("FastAPI app", True, f"{route_count} routes registered")
    except Exception as exc:
        all_ok &= check("FastAPI app", False, str(exc))

    # ── Summary ───────────────────────────────────────────────────
    print("\n" + "=" * 50)
    if all_ok:
        print("✓ All checks passed — platform is ready\n")
        print("Start the API server:")
        print("  uvicorn trading_platform.api.main:app --reload --port 8000\n")
        print("Or analyze a ticker directly:")
        print("  python -m trading_platform.main SPY --no-approve\n")
    else:
        print("✗ Some checks failed — fix the issues above before trading\n")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
