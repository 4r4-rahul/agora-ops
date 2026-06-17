"""
agora/scan/engine.py — Async priority-queue scan engine.

Replaces the synchronous sequential ticker loop in session._universe_scan().

Key improvements over the old approach:
  - N concurrent workers pull from an asyncio.PriorityQueue — full universe
    evaluation time drops from ~universe_size × 1s to ~universe_size/N × 1s
  - Four priority tiers ensure catalysts and position alerts are never
    delayed behind a routine background sweep
  - In-flight dedup prevents the same ticker being evaluated twice concurrently
  - Aging promoter prevents BACKGROUND starvation during busy periods
  - scan_metrics table gives evidence for performance comparison

Shadow mode (use when first enabling):
  When shadow_mode=True, workers record timing metrics but do NOT call the
  real evaluator. Run for 5 trading days and review scan_metrics to confirm
  queue latencies then flip shadow_mode=False to go live.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import IntEnum
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)

# ── Cadence table ─────────────────────────────────────────────────────────────
# (start_minutes_from_midnight, end_minutes, sweep_interval_s, stale_threshold_s)
_CADENCE: list[tuple[int, int, int, int]] = [
    ( 4*60,  9*60+30,  300, 1800),   # pre-market     04:00–09:30
    ( 9*60+30, 10*60,   30,   60),   # opening watch  09:30–10:00  (no entries)
    (10*60,  11*60+30,  60,  180),   # prime time     10:00–11:30
    (11*60+30, 13*60+30, 300, 900),  # midday lull    11:30–13:30
    (13*60+30, 15*60,   60,  180),   # afternoon      13:30–15:00
    (15*60,  15*60+30,  30,   60),   # final hour     15:00–15:30
    (15*60+30, 16*60,  120,  600),   # exits only     15:30–16:00
    (16*60,  24*60,    600, 3600),   # after-hours    16:00–24:00
]

_AGING_INTERVAL_S  = 60    # how often aging_promoter runs
_AGING_STALE_S     = 600   # promote BACKGROUND→NORMAL if not evaluated in 10 min
_MAX_QUEUE_SIZE    = 500
_HEARTBEAT_S       = 30


# ── Data types ────────────────────────────────────────────────────────────────

class ScanPriority(IntEnum):
    IMMEDIATE  = 0   # catalyst / position under stress
    URGENT     = 1   # price move > 1.5%, volume spike, options sweep
    NORMAL     = 2   # high-conviction watchlist, stale > 15 min
    BACKGROUND = 3   # routine universe sweep


@dataclass(order=True)
class ScanRequest:
    """Priority queue item — ordered by (priority, enqueue_time) automatically."""
    priority:     int
    enqueue_time: float = field(compare=False)
    ticker:       str   = field(compare=False)
    reason:       str   = field(compare=False)
    metadata:     dict  = field(default_factory=dict, compare=False)


# ── DB helpers ────────────────────────────────────────────────────────────────

_CREATE_METRICS = """
CREATE TABLE IF NOT EXISTS scan_metrics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp_utc TEXT    NOT NULL,
    ticker        TEXT    NOT NULL,
    priority      INTEGER NOT NULL,
    reason        TEXT    NOT NULL,
    enqueue_time  REAL    NOT NULL,
    dequeue_time  REAL    NOT NULL,
    queue_wait_ms INTEGER NOT NULL,
    worker_id     INTEGER,
    eval_ms       INTEGER,
    outcome       TEXT         -- 'evaluated' | 'shadow' | 'in_flight_skip' | 'error'
);
CREATE INDEX IF NOT EXISTS idx_scan_priority ON scan_metrics(priority, timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_scan_ticker   ON scan_metrics(ticker, timestamp_utc);
"""

def _ensure_table(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(_CREATE_METRICS)


# ── Engine ────────────────────────────────────────────────────────────────────

class UniverseScanEngine:
    """
    Async priority-queue scan engine.

    Usage (in session.py):
        engine = UniverseScanEngine(
            universe_fn=lambda: settings.etf_universe + dynamic_tickers,
            evaluator=self._evaluate_ticker,
            db_path=str(settings.db_path),
            n_workers=settings.n_scan_workers,
            shadow_mode=True,   # start in shadow; flip False after 5-day review
        )
        await engine.start()

        # promote a ticker from price monitor:
        await engine.enqueue(ScanPriority.URGENT, ticker, reason="move 2.1% in 30m")
    """

    def __init__(
        self,
        universe_fn:  Callable[[], list[str]],
        evaluator:    Callable[[str, int, str], Awaitable[None]],
        db_path:      str,
        n_workers:    int  = 4,
        shadow_mode:  bool = True,
        priority_fn:  Callable[[str], int] | None = None,
    ) -> None:
        self._universe_fn  = universe_fn
        self._evaluator    = evaluator
        self._db_path      = db_path
        self._n_workers    = n_workers
        self._shadow_mode  = shadow_mode
        # S1: per-ticker priority classifier for the scheduled sweep. Lets the LIVE engine
        # honor the Tier1 "always-hot" set and market-interest names (which previously only
        # the now-fallback _universe_scan tiering used) — Tier1 → NORMAL, top-interest →
        # URGENT, the rest → BACKGROUND. None → everything sweeps at BACKGROUND (old behavior).
        self._priority_fn  = priority_fn

        self._queue: asyncio.PriorityQueue[ScanRequest] = asyncio.PriorityQueue(
            maxsize=_MAX_QUEUE_SIZE
        )
        self._in_flight:      set[str]          = set()
        self._in_flight_lock: asyncio.Lock      = asyncio.Lock()
        self._last_evaluated: dict[str, float]  = {}   # ticker → monotonic time

        self._running = False
        self._tasks:  list[asyncio.Task] = []

        _ensure_table(db_path)
        # S2: one persistent metrics connection (WAL), reused across all writes. The previous
        # code opened a fresh sqlite3.connect() on EVERY ticker evaluation. All writes originate
        # from the single event-loop thread, so one shared connection is safe.
        self._metrics_conn = sqlite3.connect(db_path, check_same_thread=False)
        try:
            self._metrics_conn.execute("PRAGMA journal_mode=WAL")
        except Exception:  # pragma: no cover - defensive
            pass
        logger.info(
            "ScanEngine init: workers=%d shadow=%s db=%s",
            n_workers, shadow_mode, db_path
        )

    # ── Public API ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        workers = [
            asyncio.create_task(self._worker(i), name=f"scan-worker-{i}")
            for i in range(self._n_workers)
        ]
        listeners = [
            asyncio.create_task(self._scheduled_sweeper(),  name="scan-sweeper"),
            asyncio.create_task(self._aging_promoter(),     name="scan-aging"),
            asyncio.create_task(self._heartbeat(),          name="scan-heartbeat"),
        ]
        self._tasks = workers + listeners
        logger.info("ScanEngine started: %d workers + 3 listeners", self._n_workers)

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        try:
            self._metrics_conn.close()   # S2: release the persistent metrics connection
        except Exception:
            pass
        logger.info("ScanEngine stopped")

    async def enqueue(
        self,
        priority: int,          # ScanPriority (an IntEnum) or a raw priority int from priority_fn
        ticker:   str,
        reason:   str,
        metadata: dict | None = None,
    ) -> bool:
        """
        Enqueue a ticker for evaluation.
        Returns False if the queue is full (IMMEDIATE items are always forced in).
        """
        async with self._in_flight_lock:
            if ticker in self._in_flight:
                return False   # already being evaluated

        req = ScanRequest(
            priority=int(priority),
            enqueue_time=time.monotonic(),
            ticker=ticker,
            reason=reason,
            metadata=metadata or {},
        )

        if priority == ScanPriority.IMMEDIATE:
            # IMMEDIATE items bypass the maxsize — use put_nowait on a temp queue
            # Actually, just drain one item if full to make room
            if self._queue.full():
                try:
                    self._queue.get_nowait()   # discard lowest-priority blocked item
                    self._queue.task_done()
                except asyncio.QueueEmpty:
                    pass
            await self._queue.put(req)
            return True

        try:
            self._queue.put_nowait(req)
            return True
        except asyncio.QueueFull:
            logger.debug("ScanQueue full: dropped %s priority=%d", ticker, priority)
            return False

    @property
    def shadow_mode(self) -> bool:
        return self._shadow_mode

    @shadow_mode.setter
    def shadow_mode(self, value: bool) -> None:
        old = self._shadow_mode
        self._shadow_mode = value
        if old != value:
            logger.info("ScanEngine shadow_mode %s → %s", old, value)

    def queue_size(self) -> int:
        return self._queue.qsize()

    def in_flight_count(self) -> int:
        return len(self._in_flight)

    def get_status(self) -> dict:
        return {
            "shadow_mode":    self._shadow_mode,
            "n_workers":      self._n_workers,
            "queue_size":     self.queue_size(),
            "in_flight":      self.in_flight_count(),
            "tickers_seen":   len(self._last_evaluated),
        }

    # ── Workers ────────────────────────────────────────────────────────────────

    async def _worker(self, worker_id: int) -> None:
        while self._running:
            try:
                req: ScanRequest = await asyncio.wait_for(
                    self._queue.get(), timeout=5.0
                )
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            dequeue_time = time.monotonic()
            queue_wait_ms = int((dequeue_time - req.enqueue_time) * 1000)

            async with self._in_flight_lock:
                if req.ticker in self._in_flight:
                    self._write_metric(req, dequeue_time, queue_wait_ms,
                                       worker_id, eval_ms=0, outcome="in_flight_skip")
                    self._queue.task_done()
                    continue
                self._in_flight.add(req.ticker)

            eval_start = time.monotonic()
            outcome = "shadow" if self._shadow_mode else "evaluated"
            try:
                if not self._shadow_mode:
                    await self._evaluator(req.ticker, req.priority, req.reason)
                self._last_evaluated[req.ticker] = time.monotonic()
            except Exception as exc:
                outcome = "error"
                logger.error("Worker %d error on %s: %s", worker_id, req.ticker, exc)
            finally:
                eval_ms = int((time.monotonic() - eval_start) * 1000)
                self._write_metric(req, dequeue_time, queue_wait_ms,
                                   worker_id, eval_ms, outcome)
                async with self._in_flight_lock:
                    self._in_flight.discard(req.ticker)
                self._queue.task_done()

    # ── Scheduled sweeper ──────────────────────────────────────────────────────

    async def _scheduled_sweeper(self) -> None:
        """Enqueues the full universe at time-of-day-appropriate intervals."""
        while self._running:
            sweep_interval_s, stale_threshold_s = self._get_cadence()
            now_mono = time.monotonic()
            universe = self._universe_fn()

            enqueued = 0
            for ticker in universe:
                age = now_mono - self._last_evaluated.get(ticker, 0)
                if age >= stale_threshold_s:
                    # S1: classify priority so Tier1 / market-interest names sweep ahead of the
                    # routine background universe (defensive: any classifier error → BACKGROUND).
                    prio: int = ScanPriority.BACKGROUND
                    if self._priority_fn is not None:
                        try:
                            prio = self._priority_fn(ticker)
                        except Exception:
                            prio = ScanPriority.BACKGROUND
                    queued = await self.enqueue(prio, ticker, reason="scheduled_sweep")
                    if queued:
                        enqueued += 1

            if enqueued:
                logger.debug("Sweeper enqueued %d stale tickers", enqueued)

            try:
                await asyncio.sleep(sweep_interval_s)
            except asyncio.CancelledError:
                break

    # ── Aging promoter ─────────────────────────────────────────────────────────

    async def _aging_promoter(self) -> None:
        """Promotes BACKGROUND tickers to NORMAL if they've been waiting too long."""
        while self._running:
            try:
                await asyncio.sleep(_AGING_INTERVAL_S)
            except asyncio.CancelledError:
                break

            now_mono = time.monotonic()
            async with self._in_flight_lock:
                in_flight_snap = set(self._in_flight)

            promoted = 0
            for ticker, last_eval in self._last_evaluated.items():
                age = now_mono - last_eval
                if age > _AGING_STALE_S and ticker not in in_flight_snap:
                    await self.enqueue(ScanPriority.NORMAL, ticker, reason="aging_promotion")
                    promoted += 1

            if promoted:
                logger.debug("Aging promoter: %d tickers promoted to NORMAL", promoted)

    # ── Heartbeat ──────────────────────────────────────────────────────────────

    async def _heartbeat(self) -> None:
        """Periodic status log — visible in session logs for health monitoring."""
        while self._running:
            try:
                await asyncio.sleep(_HEARTBEAT_S)
            except asyncio.CancelledError:
                break
            logger.debug(
                "ScanEngine: queue=%d in_flight=%d tickers_tracked=%d shadow=%s",
                self.queue_size(), self.in_flight_count(),
                len(self._last_evaluated), self._shadow_mode,
            )

    # ── Cadence lookup ─────────────────────────────────────────────────────────

    def _get_cadence(self) -> tuple[int, int]:
        """Return (sweep_interval_s, stale_threshold_s) for current time of day."""
        now_et = datetime.now(tz=ET)
        t = now_et.hour * 60 + now_et.minute
        for start, end, sweep_s, stale_s in _CADENCE:
            if start <= t < end:
                return sweep_s, stale_s
        return 600, 3600   # fallback: after-hours

    # ── Metrics ────────────────────────────────────────────────────────────────

    def _write_metric(
        self,
        req:          ScanRequest,
        dequeue_time: float,
        queue_wait_ms:int,
        worker_id:    int,
        eval_ms:      int,
        outcome:      str,
    ) -> None:
        try:
            self._metrics_conn.execute(
                """
                INSERT INTO scan_metrics
                    (timestamp_utc, ticker, priority, reason,
                     enqueue_time, dequeue_time, queue_wait_ms,
                     worker_id, eval_ms, outcome)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(UTC).isoformat(),
                    req.ticker,
                    req.priority,
                    req.reason,
                    req.enqueue_time,
                    dequeue_time,
                    queue_wait_ms,
                    worker_id,
                    eval_ms,
                    outcome,
                ),
            )
            self._metrics_conn.commit()
        except Exception as exc:
            logger.debug("scan_metrics write error: %s", exc)
