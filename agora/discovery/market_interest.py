"""
MarketInterestAgent — 6 fingerprints for 15-30 day forward market interest.

The CSCO lesson: "the market knows what it's interested in over the next 15-30 days."
This agent reads those fingerprints and tells us where the smart money is positioned.

6 Fingerprints:
  1. OI Buildup     — unusual open interest growth in 30-45 DTE options
  2. IV Term Struct — elevated short-term vs long-term IV (event pricing premium)
  3. ETF Flows      — sector ETF volume spikes (money moving in/out)
  4. Sweep Orders   — large single-ticket options trades (dark pool signaling)
  5. Calendar Dense — many earnings in same sector same 2-week window
  6. Short Interest  — heavily shorted with upcoming catalyst (squeeze setup)

Runs every 30 minutes during market hours.
Outputs a MarketInterestMap: ticker → [list of active fingerprints].
Used by: _universe_scan() to prioritize which tickers to evaluate.
Used by: CEOAgent for "market interest" section in reports.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from ..core.config import AgoraSettings, get_settings

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

# Sector ETF → component tickers mapping (for ETF flow analysis)
_SECTOR_ETFS: dict[str, list[str]] = {
    "SMH": ["NVDA", "AMD", "INTC", "TSM", "AVGO", "MU", "ASML", "TXN", "LRCX"],
    "XLK": ["AAPL", "MSFT", "NVDA", "AVGO", "AMD", "ORCL", "CSCO", "ACN"],
    "XLE": ["VST", "CEG", "NEE", "CCJ"],
    "XLF": ["SCHW", "CBOE"],
    "XLV": ["LLY", "JNJ", "ABT", "BSX", "MDT"],
    "ITA": ["PLTR", "KTOS"],
    "ARKK": ["PLTR", "TSLA", "HOOD"],
}

# Thresholds
_OI_SPIKE_THRESHOLD   = 2.0    # OI > 2× 30-day average = unusual buildup
_VOL_SPIKE_THRESHOLD  = 3.0    # options volume > 3× 30-day avg = unusual activity
_IV_TERM_PREMIUM      = 0.15   # short-term IV > long-term by 15% = event priced
_ETF_VOLUME_SPIKE     = 2.0    # ETF volume > 2× average = sector flow event
_SHORT_INTEREST_HIGH  = 0.15   # short interest > 15% float = potential squeeze


@dataclass
class Fingerprint:
    kind: str          # "oi_buildup" | "iv_term_structure" | "etf_flow" | "sweep_order" | "calendar_dense" | "short_interest"
    ticker: str
    signal_strength: float   # 0-1
    description: str
    detected_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class MarketInterestScore:
    ticker: str
    score: float                         # 0-10 aggregate interest score
    fingerprints: list[Fingerprint] = field(default_factory=list)
    updated_at: datetime = field(default_factory=datetime.utcnow)


class MarketInterestAgent:
    """
    Continuously scans for market interest fingerprints.
    Exposes synchronous `get_interest_score(ticker)` for the scan loop.
    """

    def __init__(self, settings: AgoraSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._running  = False
        self._scores:  dict[str, MarketInterestScore] = {}
        self._last_run: float = 0.0

    def get_interest_score(self, ticker: str) -> MarketInterestScore | None:
        """Synchronous accessor for scan loop. Returns None if not yet computed."""
        return self._scores.get(ticker)

    def get_top_interest_tickers(self, n: int = 10) -> list[tuple[str, float]]:
        """Return top-n tickers by interest score, sorted descending."""
        sorted_scores = sorted(
            ((t, s.score) for t, s in self._scores.items()),
            key=lambda x: x[1],
            reverse=True,
        )
        return sorted_scores[:n]

    async def start(self) -> None:
        self._running = True
        logger.info("MarketInterestAgent started — 6-fingerprint scanner")
        while self._running:
            now_et = datetime.now(tz=ET)
            # Run every 30 minutes during market hours
            if 9 <= now_et.hour < 16:
                elapsed = time.monotonic() - self._last_run
                if elapsed >= 1800:   # 30 min
                    try:
                        await self._scan_all_fingerprints()
                        self._last_run = time.monotonic()
                    except Exception as exc:
                        logger.error("Market interest scan failed: %s", exc)
            await asyncio.sleep(60)

    async def stop(self) -> None:
        self._running = False

    # ── Full scan ──────────────────────────────────────────────────

    async def _scan_all_fingerprints(self) -> None:
        universe = self._settings.etf_universe
        logger.info("Market interest scan: %d tickers", len(universe))

        # Run all 6 fingerprints in parallel
        fp_tasks = [
            self._scan_oi_buildup(universe),
            self._scan_iv_term_structure(universe),
            self._scan_etf_flows(),
            self._scan_short_interest(universe),
            self._scan_sweep_orders(universe),
            self._scan_calendar_density(universe),
        ]
        results = await asyncio.gather(*fp_tasks, return_exceptions=True)

        # Aggregate all fingerprints into scores
        all_fingerprints: list[Fingerprint] = []
        for r in results:
            if isinstance(r, list):
                all_fingerprints.extend(r)

        # Group by ticker
        by_ticker: dict[str, list[Fingerprint]] = {}
        for fp in all_fingerprints:
            by_ticker.setdefault(fp.ticker, []).append(fp)

        # Score: each fingerprint contributes its signal_strength (max 10 total)
        now = datetime.now(tz=ET)
        for ticker, fingerprints in by_ticker.items():
            score = min(sum(fp.signal_strength * 2.5 for fp in fingerprints), 10.0)
            self._scores[ticker] = MarketInterestScore(
                ticker=ticker,
                score=score,
                fingerprints=fingerprints,
                updated_at=now,
            )

        # Log top 5
        top5 = self.get_top_interest_tickers(5)
        if top5:
            logger.info(
                "Market interest top-5: %s",
                " | ".join(f"{t}({s:.1f})" for t, s in top5),
            )

    # ── Fingerprint 1: OI Buildup ──────────────────────────────────

    async def _scan_oi_buildup(self, universe: list[str]) -> list[Fingerprint]:
        """Unusual open interest growth in 30-45 DTE options."""
        fingerprints = []
        try:
            from datetime import date as _date

            import yfinance as yf

            today = _date.today()
            for ticker in universe[:30]:   # cap to avoid rate limiting
                try:
                    tk = yf.Ticker(ticker)
                    exps = tk.options or []
                    # Find 30-45 DTE expiry
                    target_exp = None
                    for exp in exps:
                        dte = (_date.fromisoformat(exp) - today).days
                        if 25 <= dte <= 50:
                            target_exp = exp
                            break
                    if not target_exp:
                        continue

                    chain = tk.option_chain(target_exp)
                    calls = chain.calls
                    puts  = chain.puts
                    if calls.empty:
                        continue

                    total_oi  = int(calls["openInterest"].fillna(0).sum()) + int(puts["openInterest"].fillna(0).sum())
                    total_vol = int(calls["volume"].fillna(0).sum()) + int(puts["volume"].fillna(0).sum())

                    # Volume/OI ratio > threshold = unusual same-day interest
                    if total_oi > 1000 and total_vol > 0:
                        vol_oi_ratio = total_vol / total_oi
                        if vol_oi_ratio >= 0.5:   # 50% of OI traded today = unusual
                            strength = min(vol_oi_ratio / 2.0, 1.0)
                            fingerprints.append(Fingerprint(
                                kind="oi_buildup",
                                ticker=ticker,
                                signal_strength=strength,
                                description=(
                                    f"OI={total_oi:,} | vol={total_vol:,} | "
                                    f"vol/OI={vol_oi_ratio:.1%} in {target_exp} options"
                                ),
                            ))
                    await asyncio.sleep(0.2)   # rate limit
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("OI buildup scan failed: %s", exc)
        return fingerprints

    # ── Fingerprint 2: IV Term Structure ──────────────────────────

    async def _scan_iv_term_structure(self, universe: list[str]) -> list[Fingerprint]:
        """Short-term IV > long-term IV = event risk being priced."""
        fingerprints = []
        try:
            from datetime import date as _date

            import yfinance as yf

            today = _date.today()
            for ticker in universe[:30]:
                try:
                    tk = yf.Ticker(ticker)
                    exps = tk.options or []
                    if len(exps) < 2:
                        continue

                    try:
                        fi = tk.fast_info
                        spot = float(getattr(fi, "last_price", None) or fi.get("lastPrice", 0) or 0)
                    except Exception:
                        spot = 0.0
                    if spot <= 0:
                        continue

                    # Short-term: ~14 DTE; long-term: ~45 DTE
                    short_exp = long_exp = None
                    for exp in exps:
                        dte = (_date.fromisoformat(exp) - today).days
                        if 7 <= dte <= 21 and not short_exp:
                            short_exp = exp
                        if 35 <= dte <= 55 and not long_exp:
                            long_exp = exp

                    if not (short_exp and long_exp):
                        continue

                    def atm_iv(exp):
                        chain = tk.option_chain(exp)  # noqa: B023 (reviewed: immediate-consume / shared object)
                        calls = chain.calls
                        if calls.empty:
                            return 0.0
                        idx = (calls["strike"] - spot).abs().argsort().iloc[0]  # noqa: B023 (reviewed: immediate-consume / shared object)
                        return float(calls["impliedVolatility"].iloc[idx] or 0)

                    iv_short = atm_iv(short_exp)
                    iv_long  = atm_iv(long_exp)

                    if iv_long > 0 and iv_short > 0:
                        premium = (iv_short - iv_long) / iv_long
                        if premium >= _IV_TERM_PREMIUM:
                            strength = min(premium / 0.30, 1.0)
                            fingerprints.append(Fingerprint(
                                kind="iv_term_structure",
                                ticker=ticker,
                                signal_strength=strength,
                                description=(
                                    f"Short-term IV={iv_short:.1%} vs long-term={iv_long:.1%} "
                                    f"(premium={premium:.1%}) — event risk priced"
                                ),
                            ))
                    await asyncio.sleep(0.2)
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("IV term structure scan failed: %s", exc)
        return fingerprints

    # ── Fingerprint 3: ETF Flows ───────────────────────────────────

    async def _scan_etf_flows(self) -> list[Fingerprint]:
        """Sector ETF volume spike → sector receiving inflows."""
        fingerprints = []
        try:
            import yfinance as yf

            for etf, components in _SECTOR_ETFS.items():
                try:
                    tk = yf.Ticker(etf)
                    info = tk.info or {}
                    vol_today = float(info.get("regularMarketVolume") or 0)
                    avg_vol   = float(info.get("averageVolume") or info.get("averageDailyVolume10Day") or 0)

                    if avg_vol > 0 and vol_today > avg_vol * _ETF_VOLUME_SPIKE:
                        ratio = vol_today / avg_vol
                        strength = min((ratio - _ETF_VOLUME_SPIKE) / 3.0 + 0.5, 1.0)
                        for comp in components:
                            fingerprints.append(Fingerprint(
                                kind="etf_flow",
                                ticker=comp,
                                signal_strength=strength,
                                description=(
                                    f"{etf} volume {ratio:.1f}× average "
                                    f"({vol_today/1e6:.1f}M vs {avg_vol/1e6:.1f}M avg)"
                                ),
                            ))
                    await asyncio.sleep(0.1)
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("ETF flow scan failed: %s", exc)
        return fingerprints

    # ── Fingerprint 4: Short Interest ──────────────────────────────

    async def _scan_short_interest(self, universe: list[str]) -> list[Fingerprint]:
        """High short interest + upcoming catalyst = squeeze setup."""
        fingerprints = []
        try:
            import yfinance as yf

            for ticker in universe[:20]:
                try:
                    tk = yf.Ticker(ticker)
                    info = tk.info or {}
                    short_pct = float(info.get("shortPercentOfFloat") or 0)
                    if short_pct >= _SHORT_INTEREST_HIGH:
                        strength = min((short_pct - _SHORT_INTEREST_HIGH) / 0.15 + 0.5, 1.0)
                        fingerprints.append(Fingerprint(
                            kind="short_interest",
                            ticker=ticker,
                            signal_strength=strength,
                            description=(
                                f"Short interest {short_pct:.1%} of float — squeeze risk elevated"
                            ),
                        ))
                    await asyncio.sleep(0.1)
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("Short interest scan failed: %s", exc)
        return fingerprints

    # ── Fingerprint 5: Sweep Orders ────────────────────────────────

    async def _scan_sweep_orders(self, universe: list[str]) -> list[Fingerprint]:
        """
        Detect large directional options bets on specific strikes.

        A "sweep" = institution aggressively lifting all available offers at one strike
        in a single session. yfinance approximation: find strikes where today's volume
        is >2× OI AND volume > 500 contracts (can't see actual tape, but this catches
        the footprint: OI was stable, then suddenly 2× it traded today in one strike).

        Call sweeps → bullish institutional bet.
        Put sweeps → bearish institutional bet or hedge.
        Signal strength scales with (volume / OI) ratio.
        """
        fingerprints = []
        try:
            from datetime import date as _date

            import yfinance as yf

            today = _date.today()
            for ticker in universe[:25]:
                try:
                    tk = yf.Ticker(ticker)
                    try:
                        fi = tk.fast_info
                        spot = float(getattr(fi, "last_price", None) or fi.get("lastPrice", 0) or 0)
                    except Exception:
                        spot = 0.0
                    if spot <= 0:
                        continue

                    exps = tk.options or []
                    # Check 2-4 nearest expiries for sweep activity
                    for exp in exps[:4]:
                        dte = (_date.fromisoformat(exp) - today).days
                        if dte > 45:
                            break  # sweeps concentrate in near-term

                        chain = tk.option_chain(exp)
                        for opt_type, df in [("call", chain.calls), ("put", chain.puts)]:
                            if df.empty:
                                continue
                            # Find any strike with volume >> OI (single-session burst)
                            df = df.copy()
                            df["vol"] = df["volume"].fillna(0).astype(float)
                            df["oi"]  = df["openInterest"].fillna(0).astype(float)
                            # Exclude strikes near zero OI (thinly listed)
                            active = df[(df["oi"] > 100) & (df["vol"] > 500)]
                            if active.empty:
                                continue
                            active = active.copy()
                            active["ratio"] = active["vol"] / active["oi"].clip(lower=1)
                            sweep_rows = active[active["ratio"] >= 2.0]
                            if sweep_rows.empty:
                                continue

                            # Take the strongest sweep strike
                            best = sweep_rows.loc[sweep_rows["ratio"].idxmax()]
                            ratio    = float(best["ratio"])
                            vol      = int(best["vol"])
                            strike   = float(best["strike"])
                            otm_pct  = (strike - spot) / spot  # +ve = OTM call, -ve = OTM put

                            # Determine bias: OTM call sweep = bullish, OTM put sweep = bearish
                            if opt_type == "call" and otm_pct > 0.02:
                                bias = "bullish"
                            elif opt_type == "put" and otm_pct < -0.02:
                                bias = "bearish"
                            else:
                                bias = "directional"  # ATM = unknown but significant

                            strength = min((ratio - 2.0) / 8.0 + 0.5, 1.0)
                            fingerprints.append(Fingerprint(
                                kind="sweep_order",
                                ticker=ticker,
                                signal_strength=strength,
                                description=(
                                    f"{bias.upper()} sweep: {vol} {opt_type}s at ${strike:.0f} "
                                    f"({exp}, DTE={dte}) | vol/OI={ratio:.1f}× | "
                                    f"OTM={otm_pct*100:+.1f}%"
                                ),
                            ))

                    await asyncio.sleep(0.2)
                except Exception:
                    continue
        except Exception as exc:
            logger.debug("Sweep order scan failed: %s", exc)
        return fingerprints

    # ── Fingerprint 6: Calendar Density ───────────────────────────

    async def _scan_calendar_density(self, universe: list[str]) -> list[Fingerprint]:
        """
        High earnings density in same sector in next 14 days = read-through momentum.

        When 4+ stocks in semiconductors report in the same 2 weeks, each one's
        reaction creates a read-through that amplifies the others. The sector develops
        momentum (positive or negative) that institutional investors position into.

        Signal is applied to ALL tickers in the dense sector, not just the ones
        reporting — because non-reporting peers also get re-rated.

        Uses yfinance calendar — same data source as EarningsCalendarAgent.
        Runs once per scan cycle (30 min), cached for efficiency.
        """
        fingerprints = []
        try:
            from datetime import date as _date
            from datetime import timedelta

            import yfinance as yf

            today   = _date.today()
            horizon = today + timedelta(days=14)

            # Map ticker → sector (reuse SectorIntelligenceAgent's mapping logic)
            from agora.agents.sector_intelligence import _TICKER_SECTOR
            sector_earnings: dict[str, list[str]] = {}  # sector → [tickers reporting soon]

            for ticker in universe[:40]:   # sample for speed
                if ticker not in _TICKER_SECTOR:
                    continue
                sector = _TICKER_SECTOR[ticker]
                try:
                    tk  = yf.Ticker(ticker)
                    cal = tk.calendar
                    if not cal:
                        continue
                    earnings_raw = cal.get("Earnings Date") if hasattr(cal, "get") else None
                    if not earnings_raw:
                        continue
                    if isinstance(earnings_raw, (list, tuple)) and earnings_raw:
                        earnings_raw = earnings_raw[0]
                    if hasattr(earnings_raw, "date"):
                        ed = earnings_raw.date()
                    elif isinstance(earnings_raw, _date):
                        ed = earnings_raw
                    else:
                        continue
                    if today <= ed <= horizon:
                        sector_earnings.setdefault(sector, []).append(ticker)
                    await asyncio.sleep(0.1)
                except Exception:
                    continue

            # Sectors with ≥3 reporters in 14 days = dense calendar
            for sector, reporters in sector_earnings.items():
                if len(reporters) < 3:
                    continue

                # Signal all tickers in this sector (reporters + peers)
                sector_tickers = [t for t in universe if _TICKER_SECTOR.get(t) == sector]
                strength = min(0.4 + len(reporters) * 0.15, 1.0)  # 3 reporters = 0.85
                desc = (
                    f"Calendar density: {len(reporters)} {sector} earnings in 14 days "
                    f"({', '.join(reporters[:4])}{'...' if len(reporters) > 4 else ''})"
                )
                for ticker in sector_tickers:
                    fingerprints.append(Fingerprint(
                        kind="calendar_dense",
                        ticker=ticker,
                        signal_strength=strength,
                        description=desc,
                    ))

                logger.info("Calendar density: %s sector — %d reporters | %s",
                            sector, len(reporters), reporters)

        except Exception as exc:
            logger.debug("Calendar density scan failed: %s", exc)
        return fingerprints
