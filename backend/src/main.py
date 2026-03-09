import logging
import os
import time
import uuid
from typing import Any, Dict, List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import openai_client
from .data_providers.ibkr_client import IBKRMarketDataProvider
from .data_providers.market_data_base import GreeksSnapshot, OptionContract, OptionRight, ProviderError
from .services.audit_logger import AuditLogger
from .services.market_data_service import MarketDataService
from .services.scoring_service import ScoringService

app = FastAPI(title="Options Trading Agent", version="0.3.0")
logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

market_data_service: Optional[MarketDataService] = None
audit_logger: Optional[AuditLogger] = AuditLogger()
scoring_service: ScoringService = ScoringService(
    account_size=float(os.getenv("ACCOUNT_SIZE", "50000")),
)


def require_market_data_service() -> MarketDataService:
    if market_data_service is None:
        raise HTTPException(status_code=503, detail="Market data provider not configured")
    return market_data_service


def _contract_payload(contract: OptionContract) -> "OptionContractPayload":
    return OptionContractPayload(
        symbol=contract.symbol,
        expiry=contract.expiry,
        strike=contract.strike,
        right=contract.right,
    )


def _greeks_payload(snapshot: Optional[GreeksSnapshot]) -> Optional["GreeksPayload"]:
    if snapshot is None:
        return None
    return GreeksPayload(
        iv=snapshot.iv,
        delta=snapshot.delta,
        gamma=snapshot.gamma,
        theta=snapshot.theta,
        vega=snapshot.vega,
        rho=snapshot.rho,
        timestamp=snapshot.timestamp,
    )


def _log_plan(plan: "OptionPlan", score: float) -> None:
    if not audit_logger:
        return
    payload = plan.model_dump()
    payload["score"] = score
    payload["logged_at"] = time.time()
    audit_logger.log_plan_generated(payload)


def _log_execution(plan: "OptionPlan", result: Dict[str, Any]) -> None:
    if not audit_logger:
        return
    audit_logger.log_execution_result(plan.plan_id or plan.symbol, result)

class SymbolsRequest(BaseModel):
    symbols: List[str] = Field(...)

class StockScore(BaseModel):
    symbol: str
    score: float
    direction: str
    passed: bool
    regime: str = ""
    vix: float = 0.0
    atr_pct: float = 0.0
    size_multiplier: float = 1.0
    preferred_strategy: str = ""
    reason: str = ""

class OptionsPlanRequest(BaseModel):
    symbols: List[str]
    equity: float = 10000.0
    horizon_days: int = 5

class OptionLeg(BaseModel):
    side: Literal["buy","sell"]
    type: Literal["call","put"]
    expiry: str
    strike: float
    qty: int

class OptionPlan(BaseModel):
    plan_id: Optional[str] = None
    symbol: str
    structure: str = "credit_spread"
    legs: List[OptionLeg] = []
    entry_debit: float = 0.0
    max_loss: float = 0.0
    max_profit: float = 0.0
    pop_pct: float = 0.0
    horizon_days: int = 0
    dte: int | None = None
    # Real fields from scoring service
    strategy: str = ""
    short_strike: float = 0.0
    long_strike: float = 0.0
    credit_per_contract: float = 0.0
    num_contracts: int = 0
    total_credit: float = 0.0
    total_max_loss: float = 0.0
    width: float = 0.0
    short_delta: float = 0.0
    short_iv: float = 0.0
    expiry: str = ""
    regime: str = ""
    vix: float = 0.0

class OptionsPlaceRequest(BaseModel):
    plans: List[OptionPlan]

class LLMExplanation(BaseModel):
    symbol: str
    summary: str
    risk: str
    time_comment: str

class ExplainPlanRequest(BaseModel):
    plans: List[OptionPlan]

class MetricsSummary(BaseModel):
    date: str
    trades: int
    pnl_today: float
    pnl_week: float = 0.0
    pnl_month: float = 0.0
    open_positions: int = 0
    consecutive_losses: int = 0
    in_recovery: bool = False
    can_trade: bool = True
    can_trade_reason: str = "OK"
    max_drawdown_30d: float = 0.0
    peak_equity: float = 0.0
    current_equity: float = 0.0
    positions: List[Dict[str, Any]] = []
    closed_trades_today: List[Dict[str, Any]] = []


class OptionsChainRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=12)
    expiry: Optional[str] = Field(default=None, description="YYYY-MM-DD expiry override")
    force_refresh: bool = False


class OptionContractPayload(BaseModel):
    symbol: str
    expiry: str
    strike: float
    right: OptionRight


class GreeksPayload(BaseModel):
    iv: float
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float
    timestamp: float


class OptionQuotePayload(BaseModel):
    contract: OptionContractPayload
    bid: float
    ask: float
    mark: float
    last: float
    open_interest: int
    volume: int
    timestamp: float
    greeks: Optional[GreeksPayload] = None
    midpoint_iv: Optional[float] = None


class OptionsChainResponse(BaseModel):
    symbol: str
    expiry: Optional[str]
    fetched_at: float
    provider: str
    quotes: List[OptionQuotePayload]


class GreeksRequest(BaseModel):
    symbol: str
    expiry: str
    strike: float
    right: OptionRight


class GreeksResponse(GreeksPayload):
    contract: OptionContractPayload


@app.on_event("startup")
async def startup_event() -> None:
    global market_data_service
    provider_name = os.getenv("OPTIONS_DATA_PROVIDER", "disabled").lower()
    if provider_name != "ibkr":
        logger.info("Market data provider disabled or unsupported: %s", provider_name)
        return
    provider = IBKRMarketDataProvider()
    cache_ttl = float(os.getenv("MARKET_CACHE_TTL", "3.0"))
    stale_tolerance = float(os.getenv("MARKET_STALE_TOLERANCE", "15.0"))
    service = MarketDataService(provider, cache_ttl=cache_ttl, stale_tolerance=stale_tolerance)
    try:
        await service.startup()
    except ProviderError as exc:
        logger.error("Failed to initialize market data provider: %s", exc)
        raise
    market_data_service = service


@app.on_event("shutdown")
async def shutdown_event() -> None:
    global market_data_service
    if market_data_service is None:
        return
    await market_data_service.shutdown()
    market_data_service = None


@app.get("/health")
def health():
    return {
        "status":"ok",
        "ts": time.time(),
        "equity_data_provider": os.getenv("EQUITY_DATA_PROVIDER","none"),
        "options_data_provider": os.getenv("OPTIONS_DATA_PROVIDER","none"),
        "broker_provider": os.getenv("BROKER_PROVIDER","paper_mock"),
        "market_data": market_data_service.connection_state() if market_data_service else {"provider":"disabled","connected":"false"},
    }


@app.post("/market/options-chain", response_model=OptionsChainResponse)
async def market_options_chain(
    body: OptionsChainRequest,
    service: MarketDataService = Depends(require_market_data_service),
):
    try:
        symbol = body.symbol.upper()
        chain = await service.get_options_chain(symbol, body.expiry, force_refresh=body.force_refresh)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    quotes = [
        OptionQuotePayload(
            contract=_contract_payload(q.contract),
            bid=q.bid,
            ask=q.ask,
            mark=q.mark,
            last=q.last,
            open_interest=q.open_interest,
            volume=q.volume,
            timestamp=q.timestamp,
            greeks=_greeks_payload(q.greeks),
            midpoint_iv=q.midpoint_iv,
        )
        for q in chain.quotes
    ]
    return OptionsChainResponse(
        symbol=chain.symbol,
        expiry=chain.expiry,
        fetched_at=chain.fetched_at,
        provider=service.provider.name,
        quotes=quotes,
    )


@app.post("/market/greeks", response_model=GreeksResponse)
async def market_greeks(
    body: GreeksRequest,
    service: MarketDataService = Depends(require_market_data_service),
):
    contract = OptionContract(
        symbol=body.symbol.upper(),
        expiry=body.expiry,
        strike=body.strike,
        right=body.right,
    )
    try:
        snapshot = await service.get_greeks(contract)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    payload = _greeks_payload(snapshot)
    if payload is None:
        raise HTTPException(status_code=502, detail="Provider returned empty greeks payload")
    return GreeksResponse(contract=_contract_payload(snapshot.contract), **payload.dict())

@app.post("/score/intraday", response_model=List[StockScore])
def score_intraday(body: SymbolsRequest):
    ibkr_client = None
    if market_data_service and hasattr(market_data_service, 'provider'):
        ibkr_client = market_data_service.provider
    results = scoring_service.score_symbols(body.symbols, ibkr_client=ibkr_client)
    out: List[StockScore] = []
    for r in results:
        out.append(StockScore(
            symbol=r.symbol,
            score=round(r.score, 4),
            direction=r.direction,
            passed=r.trade_enabled and r.score > 0,
            regime=r.regime,
            vix=round(r.vix, 2),
            atr_pct=round(r.atr_pct, 3),
            size_multiplier=round(r.size_multiplier, 4),
            preferred_strategy=r.preferred_strategy,
            reason=r.reason,
        ))
    return out

@app.post("/options/plan", response_model=List[OptionPlan])
async def options_plan(body: OptionsPlanRequest):
    # Fetch real chain data if IBKR is connected
    chain_quotes: Dict[str, List[Dict[str, Any]]] = {}
    if market_data_service:
        for sym in body.symbols:
            try:
                chain = await market_data_service.get_options_chain(sym.upper())
                chain_quotes[sym.upper()] = [
                    {
                        "strike": q.contract.strike,
                        "right": "C" if q.contract.right == "call" else "P",
                        "bid": q.bid,
                        "ask": q.ask,
                        "mark": q.mark,
                        "delta": q.greeks.delta if q.greeks else 0,
                        "gamma": q.greeks.gamma if q.greeks else 0,
                        "iv": q.greeks.iv if q.greeks else 0,
                    }
                    for q in chain.quotes
                ]
            except Exception as e:
                logger.warning("Failed to fetch chain for %s: %s", sym, e)

    spread_plans = scoring_service.generate_plans(
        symbols=body.symbols,
        equity=body.equity,
        chain_quotes=chain_quotes if chain_quotes else None,
    )

    plans: List[OptionPlan] = []
    for sp in spread_plans:
        legs = []
        if sp.short_strike > 0:
            legs.append(OptionLeg(
                side="sell",
                type="call" if sp.right == "C" else "put",
                expiry=sp.expiry,
                strike=sp.short_strike,
                qty=sp.num_contracts,
            ))
            legs.append(OptionLeg(
                side="buy",
                type="call" if sp.right == "C" else "put",
                expiry=sp.expiry,
                strike=sp.long_strike,
                qty=sp.num_contracts,
            ))

        plan = OptionPlan(
            plan_id=uuid.uuid4().hex,
            symbol=sp.symbol,
            structure="credit_spread",
            legs=legs,
            entry_debit=0.0,
            max_loss=sp.total_max_loss,
            max_profit=sp.total_credit,
            pop_pct=sp.pop_estimate,
            horizon_days=body.horizon_days,
            dte=0,
            strategy=sp.strategy,
            short_strike=sp.short_strike,
            long_strike=sp.long_strike,
            credit_per_contract=sp.credit_per_contract,
            num_contracts=sp.num_contracts,
            total_credit=sp.total_credit,
            total_max_loss=sp.total_max_loss,
            width=sp.width,
            short_delta=sp.short_delta,
            short_iv=sp.short_iv,
            expiry=sp.expiry,
            regime=sp.regime,
            vix=sp.vix,
        )
        plans.append(plan)
        _log_plan(plan, sp.pop_estimate)

    return plans

@app.post("/options/place")
def options_place(body: OptionsPlaceRequest):
    broker = os.getenv("BROKER_PROVIDER", "paper_mock")
    orders = []
    for p in body.plans:
        order: Dict[str, Any] = {
            "plan_id": p.plan_id,
            "symbol": p.symbol,
            "structure": p.structure,
            "legs": [l.model_dump() for l in p.legs],
            "broker": broker,
        }

        if broker == "ibkr" and market_data_service:
            # Route to real executor
            try:
                import sys as _sys
                _project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
                if _project_root not in _sys.path:
                    _sys.path.insert(0, _project_root)
                from trading_engine.execution import OrderExecutor, OrderStatus

                executor = OrderExecutor(
                    ibkr_provider=None,  # Will use market_data_service's provider
                    require_confirmation=True,  # Always require confirmation for API orders
                )
                # Wire to the backend's IBKR client
                executor._ib = market_data_service.provider._client
                executor._ib_insync = __import__("ib_insync")

                if p.short_strike > 0 and p.long_strike > 0:
                    expiry_ib = p.expiry.replace("-", "") if p.expiry else ""
                    right = "C" if p.strategy and "call" in p.strategy else "P"
                    fill = executor.place_credit_spread(
                        ticker=p.symbol,
                        expiry=expiry_ib,
                        short_strike=p.short_strike,
                        long_strike=p.long_strike,
                        right=right,
                        num_contracts=p.num_contracts or 1,
                        limit_credit=p.credit_per_contract if p.credit_per_contract > 0 else None,
                    )
                    order["status"] = fill.status.value
                    order["order_id"] = fill.order_id
                    order["filled"] = fill.num_filled
                    order["avg_price"] = fill.avg_fill_price
                    order["commission"] = fill.commission
                else:
                    order["status"] = "rejected"
                    order["error"] = "Missing strike data"
            except Exception as e:
                logger.error("Execution failed for %s: %s", p.symbol, e)
                order["status"] = "error"
                order["error"] = str(e)
        else:
            order["status"] = "accepted_mock"

        orders.append(order)
        _log_execution(p, order)
    return {"orders": orders}

@app.post("/llm/explain-plans", response_model=List[LLMExplanation])
def llm_explain_plans(body: ExplainPlanRequest):
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(status_code=400, detail="OPENAI_API_KEY not configured")
    plan_dicts: List[Dict[str, Any]] = [p.dict() for p in body.plans]
    expl_raw = openai_client.explain_option_plans(plan_dicts)
    return [LLMExplanation(**ex) for ex in expl_raw]

@app.get("/metrics/summary", response_model=MetricsSummary)
def metrics_summary():
    m = scoring_service.get_live_metrics()
    return MetricsSummary(
        date=m["date"],
        trades=m["trades"],
        pnl_today=m["daily_pnl"],
        pnl_week=m["weekly_pnl"],
        pnl_month=m["monthly_pnl"],
        open_positions=m["open_positions"],
        consecutive_losses=m["consecutive_losses"],
        in_recovery=m["in_recovery"],
        can_trade=m["can_trade"],
        can_trade_reason=m["can_trade_reason"],
        max_drawdown_30d=m["max_drawdown_30d"],
        peak_equity=m["peak_equity"],
        current_equity=m["current_equity"],
        positions=m["positions"],
        closed_trades_today=m["closed_trades_today"],
    )
