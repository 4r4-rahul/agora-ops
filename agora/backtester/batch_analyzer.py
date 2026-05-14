"""
BacktestBatchAnalyzer — run MacroSynthesizer calls in bulk via the Anthropic Batch API.

Instead of calling Claude synchronously per backtest day (expensive, slow),
this module:
  1. Collects all macro scenarios from a backtest run
  2. Submits them as a single Batch API job (50% cost vs real-time calls)
  3. Polls until complete (Batch API is async — results may take minutes)
  4. Returns a dict of custom_id → MacroContext for the backtester to consume

Usage:
    analyzer = BacktestBatchAnalyzer(settings)
    scenarios = [
        {"custom_id": "2024-01-15", "regime": "normal", "vix": 17.2, ...},
        ...
    ]
    results = await analyzer.analyze_batch(scenarios)
    # results["2024-01-15"] → MacroContext(macro_stance="risk_on", ...)
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

import anthropic

from ..agents.macro_synthesizer import MacroContext, MacroSynthesizer, _CACHED_SYSTEM
from ..core.config import AgoraSettings, get_settings

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SEC = 30   # check batch status every 30s
_MAX_WAIT_SEC = 3600      # give up after 1 hour


class BacktestBatchAnalyzer:
    """
    Submits macro synthesis requests to the Anthropic Batch API.
    Use this in backtesting to run real Claude macro reads at 50% cost.

    The synchronous fallback (_fallback_synthesizer) ensures the backtester
    never blocks — if the batch times out, rules take over.
    """

    def __init__(self, settings: AgoraSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client = anthropic.AsyncAnthropic(api_key=self._settings.anthropic_api_key)
        self._fallback = MacroSynthesizer(settings)

    async def analyze_batch(
        self,
        scenarios: list[dict[str, Any]],
        timeout_sec: int = _MAX_WAIT_SEC,
    ) -> dict[str, MacroContext]:
        """
        Submit scenarios to Batch API and return MacroContext per custom_id.

        Each scenario dict must have:
            custom_id: str       — unique identifier (e.g. "2024-01-15_SPY")
            regime: str          — vol regime
            vix: float | None
            iv_rank: float | None
            vix_vix3m: float | None
            spy_rsi: float | None
            days_to_fomc: int | None
            days_to_cpi: int | None
            fed_rate: float | None

        Returns dict[custom_id → MacroContext]. Missing results get rule-based fallback.
        """
        if not scenarios:
            return {}

        requests = self._build_batch_requests(scenarios)

        logger.info(
            "BacktestBatchAnalyzer: submitting %d scenarios to Batch API",
            len(requests),
        )
        try:
            batch = await self._client.messages.batches.create(requests=requests)
            batch_id = batch.id
            logger.info("Batch submitted: id=%s", batch_id)
        except Exception as exc:
            logger.warning("Batch API submit failed: %s — using rule fallback for all", exc)
            return await self._fallback_all(scenarios)

        # Poll until processing_status == "ended"
        deadline = asyncio.get_event_loop().time() + timeout_sec
        while True:
            await asyncio.sleep(_POLL_INTERVAL_SEC)
            try:
                status = await self._client.messages.batches.retrieve(batch_id)
                logger.debug(
                    "Batch %s status: %s (succeeded=%d, errored=%d)",
                    batch_id,
                    status.processing_status,
                    status.request_counts.succeeded,
                    status.request_counts.errored,
                )
                if status.processing_status == "ended":
                    break
            except Exception as exc:
                logger.warning("Batch poll failed: %s", exc)

            if asyncio.get_event_loop().time() > deadline:
                logger.warning("Batch %s timed out after %ds — rule fallback", batch_id, timeout_sec)
                return await self._fallback_all(scenarios)

        # Collect results
        results: dict[str, MacroContext] = {}
        try:
            async for result in await self._client.messages.batches.results(batch_id):
                custom_id = result.custom_id
                if result.result.type == "succeeded":
                    ctx = self._parse_result(result.result.message)
                    results[custom_id] = ctx
                else:
                    logger.debug("Batch result %s failed: %s", custom_id, result.result.type)
        except Exception as exc:
            logger.warning("Batch result collection failed: %s", exc)

        # Fill missing custom_ids with rule-based fallback
        for scenario in scenarios:
            cid = scenario["custom_id"]
            if cid not in results:
                results[cid] = self._rule_fallback(scenario)

        logger.info(
            "BacktestBatchAnalyzer: %d/%d results from Claude, %d rule-based",
            sum(1 for c in results.values() if c.method == "claude"),
            len(scenarios),
            sum(1 for c in results.values() if c.method == "rules"),
        )
        return results

    # ── Internals ──────────────────────────────────────────────────────

    def _build_batch_requests(
        self, scenarios: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Convert scenario dicts into Batch API request format."""
        requests = []
        for sc in scenarios:
            state_str = self._fallback._format_state(
                regime=sc.get("regime", "normal"),
                iv_rank=sc.get("iv_rank"),
                vix=sc.get("vix"),
                vix_vix3m=sc.get("vix_vix3m"),
                spy_rsi=sc.get("spy_rsi"),
                days_to_fomc=sc.get("days_to_fomc"),
                days_to_cpi=sc.get("days_to_cpi"),
                fed_rate=sc.get("fed_rate"),
            )
            requests.append({
                "custom_id": sc["custom_id"],
                "params": {
                    "model": self._settings.claude_model,
                    "max_tokens": 512,
                    "system": _CACHED_SYSTEM,
                    "messages": [{"role": "user", "content": state_str}],
                },
            })
        return requests

    def _parse_result(self, message: Any) -> MacroContext:
        """Parse a Batch API succeeded message into MacroContext."""
        try:
            text_blocks = [b for b in message.content if b.type == "text"]
            if not text_blocks:
                raise ValueError("no text block")
            raw = text_blocks[-1].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1].lstrip("json").strip()
            data = json.loads(raw)
            return MacroContext(
                macro_stance=data.get("macro_stance", "neutral"),
                confidence=float(data.get("confidence", 0.5)),
                vol_selling_ok=bool(data.get("vol_selling_ok", True)),
                size_bias=data.get("size_bias", "maintain"),
                key_risk=data.get("key_risk", ""),
                reasoning=data.get("reasoning", ""),
                method="claude",
            )
        except Exception as exc:
            logger.debug("Batch result parse failed: %s", exc)
            return MacroContext(method="rules")

    def _rule_fallback(self, scenario: dict[str, Any]) -> MacroContext:
        return self._fallback._fallback_context(
            regime=scenario.get("regime", "normal"),
            iv_rank=scenario.get("iv_rank"),
            vix=scenario.get("vix"),
        )

    async def _fallback_all(
        self, scenarios: list[dict[str, Any]]
    ) -> dict[str, MacroContext]:
        return {sc["custom_id"]: self._rule_fallback(sc) for sc in scenarios}
