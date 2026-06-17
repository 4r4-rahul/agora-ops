"""
SemanticTradeStore — ChromaDB-backed semantic search over swing trade journal.

Answers: "find the 5 most similar past trades to this setup"
Similarity is over: ticker + direction + factor scores + key signals
Results include outcome (win/loss) so SwingJudge learns from similar historical trades.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _make_doc_text(
    ticker: str,
    direction: str,
    factor_breakdown: dict,
    key_thesis: str,
    notes: list[str] | None,
) -> str:
    fb = factor_breakdown or {}
    return (
        f"Ticker: {ticker} Direction: {direction} "
        f"Technical: {fb.get('technical', 0):.0f} "
        f"Catalyst: {fb.get('catalyst', 0):.0f} "
        f"Fundamental: {fb.get('fundamental', 0):.0f} "
        f"OptionsSetup: {fb.get('options_setup', 0):.0f} "
        f"Signals: {' '.join((notes or [])[:5])} "
        f"Thesis: {(key_thesis or '')[:200]}"
    )


class SemanticTradeStore:
    COLLECTION = "swing_trades"

    def __init__(self, persist_dir: str) -> None:
        """
        persist_dir: path to ChromaDB storage directory (e.g., ".agora/chroma")
        Creates collection if it doesn't exist.
        Uses ChromaDB's default embedding function (sentence-transformers all-MiniLM-L6-v2).
        """
        self._collection: Any = None
        try:
            import chromadb
            from chromadb.utils import embedding_functions

            client = chromadb.PersistentClient(path=persist_dir)
            ef = embedding_functions.DefaultEmbeddingFunction()
            self._collection = client.get_or_create_collection(
                name=self.COLLECTION,
                embedding_function=ef,
                metadata={"hnsw:space": "cosine"},
            )
            logger.info(
                "SemanticTradeStore ready: %s (%d docs)",
                persist_dir,
                self._collection.count(),
            )
        except Exception as exc:
            logger.warning("SemanticTradeStore init failed — running as no-op: %s", exc)
            self._collection = None

    # ── Write ──────────────────────────────────────────────────────────────────

    def index_decision(
        self,
        journal_id: int,
        ticker: str,
        decision: Any,  # SwingDecision dataclass
        outcome: str = "open",
        pnl_pct: float | None = None,
    ) -> None:
        """Add or update a trade in the vector store. Called after each swing decision."""
        if self._collection is None:
            return
        try:
            fb: dict = getattr(decision, "factor_breakdown", {}) or {}
            notes: list[str] = fb.get("notes", [])
            key_thesis: str = getattr(decision, "key_thesis", "") or ""

            doc_text = _make_doc_text(ticker, decision.direction, fb, key_thesis, notes)

            metadata = {
                "ticker": ticker,
                "direction": decision.direction,
                "go": int(decision.go),
                "confidence": float(decision.confidence),
                "outcome": outcome,
                "pnl_pct": float(pnl_pct) if pnl_pct is not None else 0.0,
                "raw_score": float(decision.raw_score),
                "technical": float(fb.get("technical", 0)),
                "catalyst": float(fb.get("catalyst", 0)),
                # store extra fields for result reconstruction
                "key_thesis": key_thesis[:500],
                "what_kills_trade": (getattr(decision, "what_kills_trade", "") or "")[:500],
            }

            self._collection.upsert(
                ids=[str(journal_id)],
                documents=[doc_text],
                metadatas=[metadata],
            )
        except Exception as exc:
            logger.warning("SemanticTradeStore.index_decision failed: %s", exc)

    def update_outcome(self, journal_id: int, outcome: str, pnl_pct: float | None) -> None:
        """Update outcome after a position closes. Upserts the document."""
        if self._collection is None:
            return
        try:
            existing = self._collection.get(ids=[str(journal_id)], include=["metadatas", "documents"])
            if not existing["ids"]:
                logger.debug("update_outcome: journal_id %d not found in store", journal_id)
                return

            meta = existing["metadatas"][0]
            doc = existing["documents"][0]

            meta["outcome"] = outcome
            meta["pnl_pct"] = float(pnl_pct) if pnl_pct is not None else meta.get("pnl_pct", 0.0)

            self._collection.upsert(
                ids=[str(journal_id)],
                documents=[doc],
                metadatas=[meta],
            )
        except Exception as exc:
            logger.warning("SemanticTradeStore.update_outcome failed: %s", exc)

    # ── Read ───────────────────────────────────────────────────────────────────

    def find_similar(
        self,
        ticker: str,
        direction: str,
        factor_breakdown: dict,
        k: int = 5,
    ) -> list[dict]:
        """
        Semantic search for most similar past trades.
        Returns list of dicts with keys:
          ticker, direction, go, confidence, outcome, pnl_pct,
          key_thesis, what_kills_trade, technical_score, catalyst_score,
          similarity_score (0-1, higher=more similar)
        Only returns completed trades (outcome != "open") when possible.
        Falls back to all trades if fewer than k completed.
        """
        if self._collection is None:
            return []
        try:
            total = self._collection.count()
            if total == 0:
                return []

            notes: list[str] = factor_breakdown.get("notes", [])
            query_text = _make_doc_text(ticker, direction, factor_breakdown, "", notes)

            n_fetch = min(k * 2, total)
            results = self._collection.query(
                query_texts=[query_text],
                n_results=n_fetch,
                include=["metadatas", "distances"],
            )

            metadatas: list[dict] = results["metadatas"][0]
            distances: list[float] = results["distances"][0]

            records: list[dict] = []
            for meta, dist in zip(metadatas, distances, strict=False):
                similarity = max(0.0, 1.0 - dist)
                records.append(
                    {
                        "ticker": meta.get("ticker", ""),
                        "direction": meta.get("direction", ""),
                        "go": bool(meta.get("go", 0)),
                        "confidence": float(meta.get("confidence", 0.0)),
                        "outcome": meta.get("outcome", "open"),
                        "pnl_pct": float(meta.get("pnl_pct", 0.0)),
                        "key_thesis": meta.get("key_thesis", ""),
                        "what_kills_trade": meta.get("what_kills_trade", ""),
                        "technical_score": float(meta.get("technical", 0.0)),
                        "catalyst_score": float(meta.get("catalyst", 0.0)),
                        "similarity_score": round(similarity, 4),
                    }
                )

            # Prefer completed trades (outcome != "open")
            completed = [r for r in records if r["outcome"] != "open"]
            open_trades = [r for r in records if r["outcome"] == "open"]

            if len(completed) >= k:
                return completed[:k]

            # Fill remaining slots with open trades
            merged = completed + open_trades
            return merged[:k]

        except Exception as exc:
            logger.warning("SemanticTradeStore.find_similar failed: %s", exc)
            return []

    def get_stats(self) -> dict:
        """Collection size and win rate stats."""
        if self._collection is None:
            return {"error": "store not initialized", "count": 0}
        try:
            total = self._collection.count()
            if total == 0:
                return {"count": 0, "win_rate": None, "completed": 0, "open": 0}

            all_records = self._collection.get(include=["metadatas"])
            metas: list[dict] = all_records["metadatas"]

            completed = [m for m in metas if m.get("outcome") != "open"]
            open_trades = [m for m in metas if m.get("outcome") == "open"]
            wins = [m for m in completed if m.get("outcome") == "win"]
            win_rate = len(wins) / len(completed) if completed else None

            avg_pnl: float | None = None
            pnl_vals = [m["pnl_pct"] for m in completed if m.get("pnl_pct", 0.0) != 0.0]
            if pnl_vals:
                avg_pnl = round(sum(pnl_vals) / len(pnl_vals), 4)

            return {
                "count": total,
                "completed": len(completed),
                "open": len(open_trades),
                "wins": len(wins),
                "losses": len([m for m in completed if m.get("outcome") == "loss"]),
                "win_rate": round(win_rate, 4) if win_rate is not None else None,
                "avg_pnl_pct": avg_pnl,
            }
        except Exception as exc:
            logger.warning("SemanticTradeStore.get_stats failed: %s", exc)
            return {"error": str(exc), "count": 0}
