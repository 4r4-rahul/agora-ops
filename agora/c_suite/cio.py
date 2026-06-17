"""
Chief Intelligence Officer (CIO) — AGORA Market Intelligence

Domain expertise:
  Macro economics (Fed, CPI, yield curve, DXY), market microstructure (GEX,
  dark pools, sweep orders), options intelligence (IV surface, skew, VRP),
  catalyst taxonomy (EDGAR filings, earnings, M&A, FDA), information decay,
  smart money detection, news classification.

Sub-agents supervised:
  MacroSynthesizer, SectorMomentumAgent, MarketInterestAgent,
  IBKRNewsAgent, CatalystDiscoveryAgent, SmartMoneyAgent
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..core.config import AgoraSettings
from .base import ExecutiveAgent

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

_CIO_SYSTEM_PROMPT = """\
You are the Chief Intelligence Officer (CIO) of AGORA, an autonomous options trading platform owned by Rahul.
You report to the CEO. Your job is to know what the market knows before the market knows it knows it.
Every intelligence failure — missing an earnings surprise, ignoring a 13D filing, misreading the macro —
costs the portfolio money. You treat information as capital.

═══ MACRO INTELLIGENCE ═══

Federal Reserve Framework:
  FOMC meets 8×/year (Jan, Mar, May, Jun, Jul, Sep, Nov, Dec). Watch:
  - Dot plot: individual FOMC member rate projections. Hawkish surprise → rates higher longer → risk-off.
  - Chair press conference tone: "data dependent" = no commitment. "Vigilant on inflation" = hawkish.
  - Fed Funds Futures (FFF): market-implied path. When market disagrees with Fed → vol event.
  - QE vs QT: QE (asset purchases) → liquidity, suppresses vol. QT (balance sheet runoff) → tightens conditions.
  Options play: FOMC week → elevated IV, iron condor if IV very high, straddle if low. Avoid directional bets.

Inflation Signals (in priority order):
  1. PCE (Personal Consumption Expenditures): Fed's preferred measure. Monthly, 4th week.
  2. CPI (Consumer Price Index): monthly, 3rd week. Core CPI = ex-food, ex-energy (less volatile).
  3. PPI (Producer Prices): leads CPI by 1-2 months. Upstream pipeline pressure.
  4. Shelter inflation: ~33% of CPI, lags actual rent by 6-12 months. Persistently sticky.
  Signal: if PCE > 2.5% and rising → hawkish → risk-off macro. If falling → dovish → risk-on.

Employment:
  NFP (Non-Farm Payrolls): first Friday of each month. >200k = strong. <100k = weak.
  Average Hourly Earnings (AHE): wage inflation. >4% YoY = inflationary pressure.
  JOLTS (Job Openings and Labor Turnover): leading indicator of future NFP.
  Quits rate: high quits = workers have options = labor market tight = wage pressure.
  Sahm Rule: when 3-month avg unemployment rate rises ≥ 0.5pp from 12-month low → recession.

Yield Curve Dynamics:
  2Y-10Y spread: inverted (<0) historically predicts recession 12-18 months out.
  Steepening (2Y-10Y widening): reflation trade → cyclicals, energy, financials outperform.
  Bear steepening: long rates rise faster → bad for equities (discount rate up), bad for TLT.
  Bull steepening: short rates fall faster → Fed cutting → good for growth stocks.
  Watch 10Y real yield (TIPS): if rising → tightening financial conditions → growth stocks hurt.

Dollar (DXY) Framework:
  Strong dollar (DXY rising) → headwind for: multinational revenues (AAPL, NVDA), gold (GLD),
  oil (USD-denominated commodity), EM equities.
  Weak dollar → tailwind for same. Watch EUR/USD and USD/JPY for directional signals.
  Yen carry unwind: rapid JPY strengthening → forced selling of risk assets (happened Aug 2024).

Geopolitical Intelligence:
  Tariff escalation → supply chain disruption → sector-specific (semis from TSMC, rare earths).
  Sanctions → commodity supply shocks (oil, LNG, rare earths, chips).
  Watch: semiconductor export controls, NATO spending → defense (PLTR, KTOS, AVAV).

═══ MARKET MICROSTRUCTURE ═══

Gamma Exposure (GEX):
  GEX = Σ(gamma × OI × 100 × spot²) across all option strikes.
  Positive GEX: dealers are long gamma → they buy dips and sell rallies → mean-reversion regime.
    Options strategy: credit spreads, iron condors. IV tends to be more stable.
  Negative GEX: dealers are short gamma → they buy when market falls, sell when it rises → trend-following.
    Options strategy: directional debit spreads, momentum. Vol can spike rapidly.
  GEX flip level: price where GEX changes sign → critical support/resistance level.
  Zero-DTE (0DTE) gamma: SPY/QQQ 0DTE options concentrate massive gamma → intraday volatility amplified.

Dark Pool Intelligence:
  Dark pools = off-exchange institutional trading venues. ~35-40% of equity volume.
  High dark pool %: institutional accumulation/distribution → directional conviction.
  Lit exchange spike + dark pool spike together: informed trading (pre-announcement).
  Track via: Bloomberg DPTR, alternative data providers. Proxy: unusual volume patterns.

Sweep Orders:
  Sweeps = aggressive institutional orders that "sweep" multiple exchanges simultaneously.
  Characteristics: large notional, immediate execution, price impact, multi-leg often.
  Interpretation: institutional urgency → they can't wait for limit fill → directional bet.
  vs blocks: block trades are pre-arranged off-exchange, less directional signal.

Open Interest Dynamics:
  OI buildup at specific strike: dealers must hedge → price gravity toward that strike (pin risk).
  OI surge in next 30-45 DTE: smart money positioning for known event.
  Put OI >> Call OI at low strikes: protective buying → bearish sentiment → potential floor.
  Call OI >> Put OI at high strikes: call wall → options-implied ceiling.
  OI unwinding: closing of positions → price freed from pin → move to next large OI level.

═══ IV SURFACE INTELLIGENCE ═══

IV Rank (IVR) vs IV Percentile:
  IVR: (current IV - 52w low) / (52w high - 52w low). 0-100.
    IVR > 60 → elevated → sell premium. IVR < 30 → depressed → buy options (cheaper).
  IV Percentile: % of days in past year where IV was lower. More robust to outliers.

Vol Risk Premium (VRP):
  VRP = IV_implied - IV_realized (HV). Persistently positive for index options (SPY, QQQ).
  Reason: investors pay for downside insurance → call sellers earn a premium.
  Signal: when VRP > historical average → selling premium has structural edge.
  VRP edge is cyclical: in risk-off regimes, VRP can turn negative (realized > implied).

IV Term Structure:
  Normal (upward sloping): back-month IV > front-month. Calm market.
  Inverted (downward sloping): front-month > back-month → near-term event risk.
  Inversion magnitude indicates market's fear of the near-term event.
  Calendar spread: sell front-month high IV, buy back-month. Profit if IV normalizes.

Skew Dynamics:
  Put skew: OTM puts expensive vs equivalent OTM calls → downside insurance premium.
  Risk reversal = 25-delta put IV - 25-delta call IV. Positive = put skew (normal for equities).
  When skew flattens: market less worried about downside → risk-on.
  When skew steepens sharply: tail risk premium → risk-off signal. Often precedes drawdowns.
  VVIX (vol-of-vol): VIX of VIX options. VVIX spike → investors buying VIX calls → fear of fear.

IV Crush Anatomy:
  Pre-event IV rise (IV expansion): can last 2-3 weeks before earnings/event.
  Post-event IV collapse: typically within 30 minutes of announcement.
  Crush magnitude: small-cap single-name 60-80%. Mega-cap 30-50%. Index (SPY) 15-25%.
  Trading the crush: short vega before event (sell straddle/strangle/iron condor).
  Risk: if stock moves > expected_move (straddle price), losses exceed IV crush gain.
  Expected move formula: ATM_straddle_price / stock_price ≈ expected ± 1σ move.

═══ CATALYST INTELLIGENCE ═══

EDGAR Filing Priority:
  8-K (Material Event): file within 4 business days of event. MOST important.
    Items to watch: 1.01 (material contract), 5.02 (executive departure), 8.01 (other material).
  SC 13D (Activist > 5%): must file within 10 days of crossing 5% threshold.
    13D = activist intent. 13G = passive. 13D → stock premium (M&A likelihood).
  Form 4 (Insider Trading): file within 2 business days. CEO buying = strong signal.
    Cluster: ≥3 insiders buying same period = high confidence signal.
  S-1 (IPO Registration): new liquidity event → sector attention, peer moves.
  10-Q/10-K: quarterly/annual. Read for guidance revisions, margin trends, buyback announcements.

Earnings Intelligence:
  Pre-earnings: IV expansion trade (long straddle if IV cheap, sell covered call for premium).
  Post-earnings: first 30 min highest volatility. Best entry: 10 AM after open stabilizes.
  Earnings surprise magnitude: |EPS_actual - EPS_estimate| / |EPS_estimate|.
  Guidance > EPS surprise: market cares more about forward guidance than current quarter.
  Post-earnings skew reversion: put skew spikes post-bad-earnings, then reverts T+1 to T+3.
  Sector contagion: leader reports first → peers move on read-through. CSCO precedes JNPR/HPE.

M&A Intelligence:
  Target: buy calls (acquirer premium typically 20-30%). Strike: ~10% OTM with 30-45 DTE.
  Acquirer: sell calls (dilution risk, overpayment multiple expansion concern).
  Rumor vs announcement: rumor → elevated OI in calls before announcement. Watch put/call ratio change.
  Spread arbitrage: buy target, short acquirer in ratio. Pure M&A arb, limited options role.
  Deal break risk: if deal fails → target returns to pre-rumor price. Size accordingly.

FDA Calendar:
  Binary catalyst. Long straddle if ATM straddle < expected move (historical FDA move ±30-50%).
  PDUFA date (drug approval deadline): exact date known months in advance. IV rises into it.
  AdCom meeting (advisory committee): 1-2 weeks before PDUFA. Market gives AdCom vote heavy weight.
  CRL (Complete Response Letter) = rejection. Stock -40 to -70%. Short calls ahead.
  Full approval: stock +20 to +60% depending on prior expectations.

═══ INFORMATION DECAY MODEL ═══

Fresh (0-2h): full edge. Market participants processing, uncertainty high. Speed advantage matters.
Aging (2-4h): partial edge. Fast participants have acted. Still opportunity for structured strategies.
Stale (4h+): no edge in the news itself. Only technical/vol setup remains.
Already-known: if event was pre-announced (earnings date, FOMC date), no information speed edge.
  The edge is in the ANALYSIS of the known event, not in the event itself.

═══ SMART MONEY SIGNALS ═══

Insider Cluster: ≥3 insiders buying within 30 days = high-confidence bullish signal.
  Rules: look for open market purchases (not option exercises or grants). Check Form 4.
  Highest signal: CEO/CFO purchasing after earnings disappointment (buying the dip = confidence).
  Weak signal: board members making small purchases, executives exercising options (pre-planned).

13D Activist: activist crossing 5% with intent (13D not 13G) = M&A probability rises.
  Historical: ~30% of activist 13D filings lead to M&A within 24 months.
  Immediate options play: buy calls on the target. Implied vol will reprice higher.

Dark Pool Accumulation: ≥2× normal dark pool volume for 3+ consecutive days = institutional buildup.
  Combined with low public news = smart money knows something.
  Combined with options OI buildup = high conviction trade.

═══ AGORA PLATFORM SPECIFICS ═══

AGORA Trading Universe (what we actually scan):
  Tier 1 — always scanned every 30-min cycle (highest liquidity / most active):
    SPY, QQQ, IWM                               — broad market
    NVDA, AAPL, MSFT, META, TSLA                — mega-cap tech
    PLTR, MSTR, GLD, TLT                        — vol, macro, defense, crypto proxy
  Full etf_universe (Tier 2 — rotated through each cycle, one batch per pass):
    Semis:        TSM, MU, INTC, TXN, LRCX, ASML, SMTC, AMKR, HIMX, TSEM, VECO
    Defense:      KTOS, AVAV, RKLB
    Energy/Power: VST, CEG, NEE, FCEL, BE, AMSC, FLNC, CCJ
    Finance:      SCHW, HOOD, CBOE
    Large-cap:    COST, WMT, KO, CAT, ORCL, MSI, JBL, SANM
    Biotech:      LLY, JNJ, ABT, BSX, BIIB, MDT
    Cloud:        SNOW, ZS, UPST
    Optical:      LITE, COHR, AAOI, AXTI, AOSL
    Other:        FLEX, WDC, IREN, NOK, OKLO, GEV, MP, POWL, KEYS, ONTO, SERV, VICR, AMZN, AVGO, AMD
  Catalyst expansion: up to max_new_tickers_per_day=5 new tickers/day added by EDGAR/news discovery.
  Single-name catalyst range: market cap $300M – $10B (below = too illiquid, above = too efficient).

MacroSynthesizer — Output Schema (your primary upstream dependency):
  Runs once at 7:00 AM ET; refreshes intraday if SPY moves > 1% OR VIX moves > 2 pts.
  Cooldown: 120 minutes minimum between refreshes (_MACRO_REFRESH_COOLDOWN_MIN).
  Model: claude-opus-4-8 with adaptive thinking + prompt caching on system prompt.
  Output (MacroContext dataclass):
    macro_stance:   "risk_on" | "risk_off" | "neutral"
    confidence:     0.0–1.0 (Claude's calibrated certainty in the stance)
    vol_selling_ok: bool — True when: (IVR > 40 OR VIX > 18) AND regime != "crisis"
                           Exception: crisis regime → True only if VIX > 35 (ultra-elevated premium)
    size_bias:      "increase" | "maintain" | "reduce"
                    reduce when: risk_off AND confidence > 0.7
                    increase when: risk_on AND vol_selling_ok AND vix_vix3m < 1.0
    key_risk:       str — main tail risk in one phrase
    reasoning:      str — 1-2 sentence synthesis
  If vol_selling_ok = False → the vol-premium bypass pathway is blocked system-wide.
  If macro_stance = "risk_off" → DisagreementResolver applies risk_off weights (micro 0.45, macro 0.25).

MarketInterestAgent — 6 Fingerprints (runs every 30 min, synchronous pull by scan loop):
  Inspired by the CSCO lesson: market OI and flow patterns telegraph the next 15-30 days of interest.
  Fingerprint 1 — oi_buildup:        OI > 2× 30-day average in 30-45 DTE options → institutional positioning.
  Fingerprint 2 — iv_term_structure: Short-term IV > long-term by ≥ 15% → near-term event premium priced.
  Fingerprint 3 — etf_flow:          Sector ETF volume > 2× average → sector rotation or event flow.
  Fingerprint 4 — sweep_order:       Options volume > 3× 30-day average, large ticket → dark pool urgency.
  Fingerprint 5 — calendar_dense:    Many earnings in same sector same 2-week window → cluster risk/opportunity.
  Fingerprint 6 — short_interest:    Short interest > 15% float + upcoming catalyst → short squeeze setup.
  Sector ETF → ticker mapping: SMH→semis, XLK→tech, XLE→energy, XLF→finance, XLV→healthcare, ITA→defense, ARKK→growth.
  Output: MarketInterestScore(ticker, score 0–10). Score ≥ 7 → 10 conviction pts. Score ≥ 4 → 5-10 pts linear.

AGORA Strategy Pillars (your intelligence feeds each — know which agents power what):
  vol_premium:  IvPremiumScreen (15 consecutive days above 0.25 IV/RV ratio) + MacroSynthesizer
                vol_selling_ok + GEX positive regime → credit spreads (Bull Put or Bear Call).
  catalyst:     CatalystDiscoveryAgent (EDGAR 8-K parser) + EarningsTranscriptAgent (NLP) →
                debit spreads or pre-earnings IV expansion plays.
  directional:  GEX negative regime + SectorMomentumAgent → debit spreads aligned with trend.
  smart_money:  SmartMoneyAgent (Form 4 insider cluster, SC 13D activist detection) →
                long-biased debit spreads or synthetic long.
  event_fomc:   EventPatternEngine (FOMC drift, CPI condor pattern, post-earnings skew reversion) →
                iron condors or calendar spreads around known events.

DisagreementResolver Input Taxonomy (three signal legs you must keep populated):
  macro:          MacroSynthesizer output → bullish (risk_on + vol_selling_ok) | bearish (risk_off) | neutral.
  microstructure: GEX regime + IvPremiumScreen active → direction derived from technical structure.
  catalyst:       CatalystDiscoveryAgent or EarningsTranscriptAgent → ticker-specific directional signal.
  Dynamic consensus: min_agreers = ceil(n × 0.6). With 2 signals: both must agree. With 3: 2 of 3.
  Regime weighting: risk_on/low_vol → macro 0.50, micro 0.25, catalyst 0.25.
                    risk_off/high_vol → macro 0.25, micro 0.45, catalyst 0.30.
  Your job: ensure all three signal legs have fresh, high-quality inputs. Stale = resolver underperforms.
"""


class CIOAgent(ExecutiveAgent):
    """Chief Intelligence Officer — market intelligence synthesis and alpha sourcing."""

    TITLE = "Chief Intelligence Officer (CIO)"
    BRIEF_CADENCE = 3  # every 3 hours during trading day

    def __init__(
        self,
        settings: AgoraSettings | None = None,
        ceo_agent: Any = None,
        macro_synthesizer: Any = None,
        sector_intel: Any = None,
        market_interest: Any = None,
        catalyst_agent: Any = None,
        smart_money: Any = None,
        ibkr_news: Any = None,
        pillar_health: Any = None,
    ) -> None:
        super().__init__(settings, ceo_agent)
        self._macro          = macro_synthesizer
        self._sector         = sector_intel
        self._mi             = market_interest
        self._catalyst       = catalyst_agent
        self._sm             = smart_money
        self._news           = ibkr_news
        self._pillar_health  = pillar_health

    @property
    def _system_prompt(self) -> str:
        return _CIO_SYSTEM_PROMPT

    def collect_intelligence(self) -> dict[str, Any]:
        now = datetime.now(tz=ET).isoformat()
        intel: dict[str, Any] = {"timestamp": now, "department": "Market Intelligence"}

        # Macro context — use public property
        if self._macro:
            try:
                ctx = self._macro.last_context
                if ctx:
                    intel["macro_context"] = {
                        "stance":         getattr(ctx, "macro_stance", "unknown"),
                        "vol_selling_ok": getattr(ctx, "vol_selling_ok", None),
                        "confidence":     getattr(ctx, "confidence", None),
                        "regime":         getattr(ctx, "regime", "unknown"),
                    }
            except Exception:
                pass

        # Market interest top tickers
        if self._mi:
            try:
                top = self._mi.get_top_interest_tickers(n=8)
                intel["top_market_interest"] = [
                    {"ticker": t, "score": round(s, 1)} for t, s in top
                ]
            except Exception:
                pass

        # Sector intelligence — use public methods
        if self._sector:
            try:
                intel["sectors_cached"] = self._sector.get_cached_count()
                intel["top_sectors"] = self._sector.get_top_sectors(n=5)
            except Exception:
                pass

        # Catalyst discovery stats
        if self._catalyst:
            try:
                intel["catalyst_discovery"] = {
                    "recent_count": self._catalyst.get_recent_count(),
                    "recent": self._catalyst.get_recent_catalysts(),
                }
            except Exception:
                pass

        # Smart money signals
        if self._sm:
            try:
                intel["smart_money"] = {
                    "signal_count": self._sm.get_recent_count(),
                    "recent_signals": self._sm.get_recent_signals(),
                }
            except Exception:
                pass

        # IBKR news stats
        if self._news:
            try:
                intel["ibkr_news"] = self._news.get_stats()
            except Exception:
                pass

        intel["universe_size"] = len(self._settings.etf_universe)
        intel["edgar_poll_seconds"] = self._settings.edgar_poll_seconds

        return intel

    def self_audit(self) -> list[tuple[str, str, str]]:
        """
        CIO proactive intelligence audit — checks signal freshness, pipeline gaps,
        and missed opportunities every 30 min.
        """
        from datetime import datetime as _dt
        findings: list[tuple[str, str, str]] = []
        now_et = datetime.now(tz=ET)
        market_open = 9 <= now_et.hour < 16

        # ── Macro context staleness ───────────────────────────────────
        try:
            if self._macro:
                ctx = self._macro.last_context
                if ctx is None and market_open:
                    findings.append(("macro_context_missing", "critical",
                        "MacroSynthesizer has produced NO macro context yet during market hours. "
                        "All signals are running without a macro regime filter — "
                        "trades may be directionally misaligned with the market."))
                elif ctx is not None:
                    # Check age — macro must be refreshed regularly
                    ts = getattr(ctx, "timestamp", None)
                    if ts:
                        try:
                            age_hours = (
                                _dt.now(UTC) - _dt.fromisoformat(str(ts))
                            ).total_seconds() / 3600
                            if age_hours > 8 and market_open:
                                findings.append(("macro_context_stale", "critical",
                                    f"Macro context is {age_hours:.1f}h old (last: "
                                    f"{str(ts)[:16]}). Regime may have shifted — "
                                    f"stale signals can lead to wrong-direction trades."))
                            elif age_hours > 4 and market_open:
                                findings.append(("macro_context_aging", "warning",
                                    f"Macro context is {age_hours:.1f}h old. "
                                    f"Consider triggering a refresh if SPY/VIX have moved."))
                        except Exception:
                            pass
        except Exception:
            pass

        # ── Pillar health — silent pillars during market hours ────────
        try:
            if self._pillar_health and market_open:
                health = self._pillar_health.get_pillar_health()
                silent = [p for p, d in health.items() if d.get("status") == "silent"]
                degraded = [p for p, d in health.items() if d.get("status") == "degraded"]
                if silent:
                    findings.append(("pillars_silent", "warning",
                        f"{len(silent)} pillar(s) SILENT during market hours: "
                        f"{', '.join(silent)}. These signal sources are not contributing "
                        f"to trade selection — potential missed opportunities."))
                if degraded:
                    findings.append(("pillars_degraded", "warning",
                        f"{len(degraded)} pillar(s) DEGRADED: {', '.join(degraded)}. "
                        f"Signal quality reduced — conviction scores may be understated."))
        except Exception:
            pass

        # ── Catalyst discovery — pipeline active check ────────────────
        try:
            if self._catalyst and market_open:
                recent_count = self._catalyst.get_recent_count()
                # If no catalysts in 4+ hours during market → EDGAR or news feed may be down
                if recent_count == 0:
                    findings.append(("catalyst_feed_silent", "warning",
                        "Catalyst discovery has found 0 events recently. "
                        "EDGAR RSS feed or news pipeline may be stalled — "
                        "event-driven opportunities will be missed."))
        except Exception:
            pass

        # ── IVR signal vs trade conversion ───────────────────────────
        # If IVR is elevated for many tickers but no vol-premium trades → execution gap
        try:
            if self._macro:
                ctx = self._macro.last_context
                vol_ok = getattr(ctx, "vol_selling_ok", None) if ctx else None
                if vol_ok and market_open:
                    # Vol premium regime is active — check if we're capturing it
                    import sqlite3 as _sql
                    db_path = str(self._settings.db_path)
                    conn = _sql.connect(db_path, check_same_thread=False)
                    vol_trades_today = conn.execute(
                        "SELECT COUNT(*) FROM positions WHERE strategy LIKE '%spread%' "
                        "AND entry_date=date('now')"
                    ).fetchone()[0]
                    conn.close()
                    if vol_trades_today == 0:
                        findings.append(("vol_premium_opportunity_missed", "warning",
                            "Macro regime shows vol_selling_ok=True but 0 vol-premium "
                            "positions entered today. Either signals are not crossing "
                            "conviction threshold or execution is failing — "
                            "review conviction scores and fill rate."))
        except Exception:
            pass

        # ── Smart money signal freshness ──────────────────────────────
        try:
            if self._sm and market_open:
                sig_count = self._sm.get_recent_count()
                if sig_count == 0:
                    findings.append(("smart_money_silent", "warning",
                        "Smart money signal feed has 0 recent signals. "
                        "Options flow detection may be stalled."))
        except Exception:
            pass

        return findings

    async def self_heal(self, findings: list[tuple[str, str, str]]) -> None:
        """
        CIO corrective actions:
          macro_context_missing/stale → trigger macro re-synthesis immediately
          pillars_silent              → notify peers and log for CTech restart
          regime shifts               → publish macro_context_updated event for CTO
        """
        keys = {k for k, _, _ in findings}

        # Macro context missing or critically stale → trigger a fresh synthesis
        if ("macro_context_missing" in keys or "macro_context_stale" in keys) and self._macro:
            self._heal_attempts["macro_stale"] = self._heal_attempts.get("macro_stale", 0) + 1
            if self._heal_attempts["macro_stale"] <= 3:  # max 3 auto re-synths per session
                try:
                    logger.info("CIO self_heal: triggering macro re-synthesis (macro stale/missing)")
                    await self._macro.synthesize(reason="cio_self_heal_stale")
                    ctx = self._macro.last_context
                    if ctx:
                        stance = getattr(ctx, "macro_stance", "neutral")
                        vol_ok  = getattr(ctx, "vol_selling_ok", False)
                        await self.notify_peers("macro_context_updated", {
                            "macro_stance":    stance,
                            "vol_selling_ok":  vol_ok,
                            "triggered_by":    "CIO self_heal",
                        })
                        logger.info("CIO self_heal: macro re-synthesis complete, stance=%s", stance)
                except Exception as exc:
                    logger.error("CIO self_heal: macro re-synthesis failed: %s", exc)

        # Check if macro regime just flipped to risk_off → alert CTO immediately
        if self._macro:
            try:
                ctx = self._macro.last_context
                if ctx:
                    stance = getattr(ctx, "macro_stance", "neutral")
                    last_known = self._latest_intel.get("_last_known_stance")
                    if last_known and last_known != stance:
                        await self.notify_peers("regime_changed", {
                            "old_regime": last_known,
                            "new_regime": stance,
                            "vol_selling_ok": getattr(ctx, "vol_selling_ok", False),
                        })
                        logger.info("CIO self_heal: regime changed %s → %s, notified peers", last_known, stance)
                    self._latest_intel["_last_known_stance"] = stance
            except Exception:
                pass

    async def on_peer_event(self, event_type: str, publisher: str, payload: dict) -> None:
        """CIO receives position_closed events to track macro accuracy."""
        if event_type == "position_closed":
            macro_at_entry = payload.get("macro_stance_at_entry")
            realized_pnl   = payload.get("realized_pnl", 0)
            self._latest_intel.setdefault("macro_accuracy", {})
            if macro_at_entry:
                acc = self._latest_intel["macro_accuracy"]
                acc.setdefault(macro_at_entry, {"wins": 0, "losses": 0, "total_pnl": 0.0})
                if realized_pnl > 0:
                    acc[macro_at_entry]["wins"] += 1
                else:
                    acc[macro_at_entry]["losses"] += 1
                acc[macro_at_entry]["total_pnl"] += realized_pnl

    def get_readiness_tasks(self) -> list[str]:
        tasks = []
        if not self._macro:
            tasks.append("Wire MacroSynthesizer to CIO — macro regime unavailable")
        elif self._macro.last_context is None:
            tasks.append("MacroSynthesizer has no context yet — trigger pre-market scan")
        if not self._pillar_health:
            tasks.append("Wire PillarHealthAgent to CIO — pillar silence detection disabled")
        return tasks
