"""
Session Manager — trading-hours enforcement and daily session lifecycle.

Responsibilities
----------------
* ``is_trading_hours(dt)``   — True if *dt* (naive UTC) falls within the
  regular trading window: 09:30–16:00 US/Eastern, Monday–Friday.
* ``open_session()``         — Reset daily counters, emit
  ``SessionOpenEvent``, record a session-open ``EventRecord`` in the DB.
* ``close_session()``        — Persist ``AccountSnapshotRecord``, call
  phantom-tracker flush, emit ``SessionCloseEvent``, mark session closed.
* ``record_signal(decision)``— Increment the per-session signal counter.
* ``run_scheduler()``        — Async background task that opens/closes the
  session at the correct wall-clock times using computed sleep intervals.
* ``stop()``                 — Signal the scheduler to exit cleanly.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.core.models import AccountInfo, Phase, SignalDecision
from backend.database.models import AccountSnapshotRecord, EventRecord

ET = ZoneInfo("America/New_York")

# 9:30 and 16:00 as minutes-since-midnight for tz-safe comparison
_OPEN_MINS = 9 * 60 + 30   # 570
_CLOSE_MINS = 16 * 60       # 960

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public event dataclasses (NOT ORM models)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SessionOpenEvent:
    """Emitted when the trading session opens."""
    timestamp: datetime    # naive UTC
    session_date: str      # "YYYY-MM-DD" in ET


@dataclass(frozen=True)
class SessionCloseEvent:
    """Emitted when the trading session closes."""
    timestamp: datetime    # naive UTC
    session_date: str      # "YYYY-MM-DD" in ET
    daily_signal_count: int
    daily_pnl: float


# ---------------------------------------------------------------------------
# PhantomTracker protocol (avoids circular import)
# ---------------------------------------------------------------------------

class PhantomTrackerProtocol(Protocol):
    """Minimal interface the SessionManager needs from PhantomTracker."""
    async def flush_expired(self) -> None: ...


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------

class SessionManager:
    """
    Manages the intra-day trading session lifecycle.

    Parameters
    ----------
    session_factory:
        SQLAlchemy ``async_sessionmaker`` used for all DB writes.
    phase:
        Current account phase (EVAL | PA), stored in end-of-day snapshots.
    get_account_info:
        Optional callable that returns the current ``AccountInfo``.  Used
        when persisting the end-of-day snapshot.  If *None*, zeros are
        written for balance / eod_threshold / daily_pnl.
    clock:
        Callable returning the *current* naive UTC ``datetime``.  Defaults
        to ``datetime.utcnow``.  Inject a fixed or advancing callable in
        tests to avoid patching globals.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker,
        phase: Phase,
        get_account_info: Callable[[], AccountInfo] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._phase = phase
        self._get_account_info = get_account_info
        self._clock: Callable[[], datetime] = clock or datetime.utcnow

        self._is_open: bool = False
        self._daily_signal_count: int = 0
        self._phantom_tracker: PhantomTrackerProtocol | None = None
        self._running: bool = False
        self._stop_event: asyncio.Event | None = None

        self._open_listeners: list[Callable[[SessionOpenEvent], Any]] = []
        self._close_listeners: list[Callable[[SessionCloseEvent], Any]] = []

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def is_session_open(self) -> bool:
        """True while the session is open."""
        return self._is_open

    @property
    def daily_signal_count(self) -> int:
        """Signals generated (any decision) since the last open_session()."""
        return self._daily_signal_count

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    def attach_phantom_tracker(self, tracker: PhantomTrackerProtocol) -> None:
        """Register a PhantomTracker whose ``flush_expired`` is called on close."""
        self._phantom_tracker = tracker

    def add_open_listener(self, fn: Callable[[SessionOpenEvent], Any]) -> None:
        """Register a callback invoked on every SessionOpenEvent."""
        self._open_listeners.append(fn)

    def add_close_listener(self, fn: Callable[[SessionCloseEvent], Any]) -> None:
        """Register a callback invoked on every SessionCloseEvent."""
        self._close_listeners.append(fn)

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def is_trading_hours(self, dt: datetime | None = None) -> bool:
        """
        Return *True* if *dt* (naive UTC) falls within the regular trading
        window: 09:30–16:00 US/Eastern on Monday–Friday.

        If *dt* is *None* the injected clock is used.
        """
        if dt is None:
            dt = self._clock()
        dt_et = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ET)
        if dt_et.weekday() >= 5:   # 5=Saturday, 6=Sunday
            return False
        current_mins = dt_et.hour * 60 + dt_et.minute
        return _OPEN_MINS <= current_mins < _CLOSE_MINS

    def record_signal(self, decision: SignalDecision) -> None:
        """Increment the daily signal counter for any signal decision."""
        self._daily_signal_count += 1

    async def open_session(self) -> None:
        """
        Open the trading session.

        * Resets ``daily_signal_count`` to zero.
        * Emits ``SessionOpenEvent`` to all registered listeners.
        * Persists a session-open ``EventRecord`` in the database.
        """
        now = self._clock()
        now_et = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(ET)
        session_date = now_et.strftime("%Y-%m-%d")

        self._is_open = True
        self._daily_signal_count = 0

        event = SessionOpenEvent(timestamp=now, session_date=session_date)
        self._emit_open(event)

        async with self._session_factory() as db:
            db.add(
                EventRecord(
                    timestamp=now,
                    name=f"session_open:{session_date}",
                    impact_level="LOW",
                )
            )
            await db.commit()

        log.info("Session opened for %s", session_date)

    async def close_session(self) -> None:
        """
        Close the trading session.

        * Calls ``phantom_tracker.flush_expired()`` if one is attached.
        * Persists an ``AccountSnapshotRecord`` to the database.
        * Emits ``SessionCloseEvent`` to all registered listeners.
        * Marks the session as closed.

        Idempotent — does nothing if the session is already closed.
        """
        if not self._is_open:
            return

        now = self._clock()
        now_et = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(ET)
        session_date = now_et.strftime("%Y-%m-%d")

        self._is_open = False

        # Flush phantom tracker first so its state can inform the snapshot
        if self._phantom_tracker is not None:
            try:
                await self._phantom_tracker.flush_expired()
            except Exception:
                log.exception("phantom_tracker.flush_expired() raised unexpectedly")

        # Gather account state for the end-of-day snapshot
        account_info = self._get_account_info() if self._get_account_info else None
        balance = account_info.balance if account_info else 0.0
        eod_threshold = account_info.eod_threshold if account_info else 0.0
        daily_pnl = account_info.daily_pnl if account_info else 0.0

        async with self._session_factory() as db:
            db.add(
                AccountSnapshotRecord(
                    timestamp=now,
                    balance=balance,
                    eod_threshold=eod_threshold,
                    daily_pnl=daily_pnl,
                    phase=self._phase.value,
                )
            )
            await db.commit()

        close_event = SessionCloseEvent(
            timestamp=now,
            session_date=session_date,
            daily_signal_count=self._daily_signal_count,
            daily_pnl=daily_pnl,
        )
        self._emit_close(close_event)

        log.info(
            "Session closed for %s (signals=%d, pnl=%.2f)",
            session_date,
            self._daily_signal_count,
            daily_pnl,
        )

    # ------------------------------------------------------------------
    # Scheduler
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Signal the background scheduler to exit on its next wake-up."""
        self._running = False
        if self._stop_event is not None:
            self._stop_event.set()

    async def run_scheduler(self) -> None:
        """
        Background task that opens and closes the session at the correct
        wall-clock times.

        Computes seconds-until-next-event and uses ``asyncio.wait_for``
        so the loop wakes precisely at 09:30 and 16:00 ET each trading day.
        Responds to ``stop()`` within milliseconds.
        """
        self._stop_event = asyncio.Event()
        self._running = True
        log.debug("Session scheduler started")

        try:
            while self._running:
                now = self._clock()

                # Open if we're in trading hours but session is not yet open
                if self.is_trading_hours(now) and not self._is_open:
                    await self.open_session()
                    continue

                # Close if we've left trading hours while session was open
                if not self.is_trading_hours(now) and self._is_open:
                    await self.close_session()
                    continue

                sleep_secs = self._seconds_to_next_event(now)
                log.debug("Scheduler sleeping %.1fs until next event", sleep_secs)

                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=sleep_secs,
                    )
                    break  # stop() was called
                except asyncio.TimeoutError:
                    pass   # Time to re-evaluate
        finally:
            self._running = False
            log.debug("Session scheduler stopped")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _seconds_to_next_event(self, now: datetime) -> float:
        """Return seconds from *now* (naive UTC) to the next open or close."""
        now_utc = now.replace(tzinfo=ZoneInfo("UTC"))
        now_et = now_utc.astimezone(ET)
        today = now_et.date()

        candidates: list[datetime] = []
        for day_offset in range(8):   # search up to a week ahead
            d = today + timedelta(days=day_offset)
            if d.weekday() >= 5:
                continue
            open_dt = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
            close_dt = datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET)
            if open_dt > now_utc:
                candidates.append(open_dt)
            if close_dt > now_utc:
                candidates.append(close_dt)

        if not candidates:
            return 3600.0   # fallback: check again in an hour

        next_event = min(candidates)
        return max(0.0, (next_event - now_utc).total_seconds())

    def _emit_open(self, event: SessionOpenEvent) -> None:
        for fn in self._open_listeners:
            try:
                fn(event)
            except Exception:
                log.exception("SessionOpenEvent listener raised")

    def _emit_close(self, event: SessionCloseEvent) -> None:
        for fn in self._close_listeners:
            try:
                fn(event)
            except Exception:
                log.exception("SessionCloseEvent listener raised")
