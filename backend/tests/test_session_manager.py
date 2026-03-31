"""
Tests for SessionManager.

All time-sensitive logic is exercised via clock injection — no global
datetime.utcnow patching is required.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.core.models import AccountInfo, Phase, SignalDecision
from backend.database.models import AccountSnapshotRecord, Base, EventRecord
from backend.services.session_manager import (
    SessionCloseEvent,
    SessionManager,
    SessionOpenEvent,
)


# ---------------------------------------------------------------------------
# Database fixtures (in-memory SQLite, isolated per test)
# ---------------------------------------------------------------------------

@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Return a naive UTC datetime."""
    return datetime(year, month, day, hour, minute)


def _account(
    balance: float = 110_000.0,
    eod_threshold: float = 107_000.0,
    daily_pnl: float = 250.0,
) -> AccountInfo:
    return AccountInfo(
        account_id="TEST",
        balance=balance,
        equity=balance,
        daily_pnl=daily_pnl,
        eod_threshold=eod_threshold,
        buying_power=50_000.0,
        open_positions_count=0,
    )


def _make_manager(
    session_factory,
    clock=None,
    phase=Phase.EVAL,
    account_info: AccountInfo | None = None,
) -> SessionManager:
    get_account = (lambda: account_info) if account_info else None
    return SessionManager(
        session_factory=session_factory,
        phase=phase,
        get_account_info=get_account,
        clock=clock,
    )


# ---------------------------------------------------------------------------
# is_trading_hours — pure logic, no DB needed
# ---------------------------------------------------------------------------

class TestIsTradingHours:
    """Verify trading-hours detection across timezones, DST, and weekends."""

    def _sm(self, clock=None) -> SessionManager:
        return SessionManager(
            session_factory=MagicMock(),
            phase=Phase.EVAL,
            clock=clock,
        )

    # --- Summer (EDT, UTC-4) ---
    # 2026-03-30 is a Monday; DST sprang forward on 2026-03-08

    def test_summer_at_session_open_is_true(self):
        # 09:30 EDT = 13:30 UTC
        assert self._sm().is_trading_hours(_utc(2026, 3, 30, 13, 30)) is True

    def test_summer_one_minute_before_open_is_false(self):
        # 09:29 EDT = 13:29 UTC
        assert self._sm().is_trading_hours(_utc(2026, 3, 30, 13, 29)) is False

    def test_summer_mid_session_is_true(self):
        # 12:00 EDT = 16:00 UTC
        assert self._sm().is_trading_hours(_utc(2026, 3, 30, 16, 0)) is True

    def test_summer_at_close_is_false(self):
        # 16:00 EDT = 20:00 UTC  (close is exclusive)
        assert self._sm().is_trading_hours(_utc(2026, 3, 30, 20, 0)) is False

    def test_summer_one_minute_before_close_is_true(self):
        # 15:59 EDT = 19:59 UTC
        assert self._sm().is_trading_hours(_utc(2026, 3, 30, 19, 59)) is True

    def test_summer_after_close_is_false(self):
        assert self._sm().is_trading_hours(_utc(2026, 3, 30, 21, 0)) is False

    # --- Winter (EST, UTC-5) ---
    # 2026-01-05 is a Monday (standard time)

    def test_winter_at_session_open_is_true(self):
        # 09:30 EST = 14:30 UTC
        assert self._sm().is_trading_hours(_utc(2026, 1, 5, 14, 30)) is True

    def test_winter_one_minute_before_open_is_false(self):
        # 09:29 EST = 14:29 UTC
        assert self._sm().is_trading_hours(_utc(2026, 1, 5, 14, 29)) is False

    def test_winter_at_close_is_false(self):
        # 16:00 EST = 21:00 UTC
        assert self._sm().is_trading_hours(_utc(2026, 1, 5, 21, 0)) is False

    def test_winter_one_minute_before_close_is_true(self):
        # 15:59 EST = 20:59 UTC
        assert self._sm().is_trading_hours(_utc(2026, 1, 5, 20, 59)) is True

    # --- Weekends ---

    def test_saturday_is_always_false(self):
        # 2026-04-04 is a Saturday
        assert self._sm().is_trading_hours(_utc(2026, 4, 4, 15, 0)) is False

    def test_sunday_is_always_false(self):
        # 2026-04-05 is a Sunday
        assert self._sm().is_trading_hours(_utc(2026, 4, 5, 15, 0)) is False

    # --- Friday ---

    def test_friday_during_session_is_true(self):
        # 2026-04-03 is a Friday; 15:00 EDT = 19:00 UTC
        assert self._sm().is_trading_hours(_utc(2026, 4, 3, 19, 0)) is True

    # --- Clock injection ---

    def test_uses_injected_clock_when_dt_is_none(self):
        fixed = _utc(2026, 3, 30, 15, 0)  # Monday, within trading hours (UTC)
        sm = self._sm(clock=lambda: fixed)
        assert sm.is_trading_hours() is True

    def test_uses_injected_clock_outside_hours(self):
        fixed = _utc(2026, 4, 4, 15, 0)  # Saturday
        sm = self._sm(clock=lambda: fixed)
        assert sm.is_trading_hours() is False

    def test_explicit_dt_overrides_clock(self):
        # Clock says Saturday, but explicit dt is Monday in session
        sm = self._sm(clock=lambda: _utc(2026, 4, 4, 15, 0))
        assert sm.is_trading_hours(_utc(2026, 3, 30, 15, 0)) is True


# ---------------------------------------------------------------------------
# open_session
# ---------------------------------------------------------------------------

class TestOpenSession:

    async def test_sets_is_session_open(self, session_factory):
        sm = _make_manager(session_factory, clock=lambda: _utc(2026, 3, 30, 14, 0))
        assert sm.is_session_open is False
        await sm.open_session()
        assert sm.is_session_open is True

    async def test_resets_daily_signal_count(self, session_factory):
        sm = _make_manager(session_factory, clock=lambda: _utc(2026, 3, 30, 14, 0))
        sm.record_signal(SignalDecision.ACCEPTED)
        sm.record_signal(SignalDecision.REJECTED)
        assert sm.daily_signal_count == 2
        await sm.open_session()
        assert sm.daily_signal_count == 0

    async def test_emits_open_event_to_listeners(self, session_factory):
        clock_dt = _utc(2026, 3, 30, 13, 30)
        sm = _make_manager(session_factory, clock=lambda: clock_dt)

        received: list[SessionOpenEvent] = []
        sm.add_open_listener(received.append)
        await sm.open_session()

        assert len(received) == 1
        ev = received[0]
        assert ev.timestamp == clock_dt
        assert ev.session_date == "2026-03-30"

    async def test_writes_event_record_to_db(self, session_factory, engine):
        sm = _make_manager(session_factory, clock=lambda: _utc(2026, 3, 30, 13, 30))
        await sm.open_session()

        async with engine.connect() as conn:
            result = await conn.execute(select(EventRecord))
            rows = result.fetchall()

        assert len(rows) == 1
        assert "session_open:2026-03-30" in rows[0].name
        assert rows[0].impact_level == "LOW"

    async def test_listener_exception_does_not_propagate(self, session_factory):
        sm = _make_manager(session_factory, clock=lambda: _utc(2026, 3, 30, 14, 0))
        sm.add_open_listener(lambda _: (_ for _ in ()).throw(RuntimeError("boom")))
        # Should not raise
        await sm.open_session()
        assert sm.is_session_open is True


# ---------------------------------------------------------------------------
# close_session
# ---------------------------------------------------------------------------

class TestCloseSession:

    async def test_clears_is_session_open(self, session_factory):
        sm = _make_manager(session_factory, clock=lambda: _utc(2026, 3, 30, 14, 0))
        await sm.open_session()
        assert sm.is_session_open is True
        await sm.close_session()
        assert sm.is_session_open is False

    async def test_idempotent_when_already_closed(self, session_factory, engine):
        sm = _make_manager(session_factory, clock=lambda: _utc(2026, 3, 30, 14, 0))
        # Call twice without opening in between
        await sm.close_session()
        await sm.close_session()

        # No snapshots should have been written
        async with engine.connect() as conn:
            result = await conn.execute(select(AccountSnapshotRecord))
            assert result.fetchall() == []

    async def test_persists_account_snapshot(self, session_factory, engine):
        acct = _account(balance=112_500.0, eod_threshold=108_000.0, daily_pnl=300.0)
        sm = _make_manager(
            session_factory,
            clock=lambda: _utc(2026, 3, 30, 20, 0),
            phase=Phase.PA,
            account_info=acct,
        )
        await sm.open_session()
        await sm.close_session()

        async with engine.connect() as conn:
            result = await conn.execute(select(AccountSnapshotRecord))
            rows = result.fetchall()

        assert len(rows) == 1
        row = rows[0]
        assert row.balance == 112_500.0
        assert row.eod_threshold == 108_000.0
        assert row.daily_pnl == 300.0
        assert row.phase == "PA"

    async def test_persists_snapshot_with_zeros_when_no_account_info(
        self, session_factory, engine
    ):
        sm = _make_manager(session_factory, clock=lambda: _utc(2026, 3, 30, 20, 0))
        await sm.open_session()
        await sm.close_session()

        async with engine.connect() as conn:
            result = await conn.execute(select(AccountSnapshotRecord))
            rows = result.fetchall()

        assert len(rows) == 1
        assert rows[0].balance == 0.0
        assert rows[0].daily_pnl == 0.0

    async def test_emits_close_event_to_listeners(self, session_factory):
        clock_dt = _utc(2026, 3, 30, 20, 0)
        sm = _make_manager(
            session_factory,
            clock=lambda: clock_dt,
            account_info=_account(daily_pnl=500.0),
        )
        await sm.open_session()
        sm.record_signal(SignalDecision.ACCEPTED)
        sm.record_signal(SignalDecision.EXPIRED)

        received: list[SessionCloseEvent] = []
        sm.add_close_listener(received.append)
        await sm.close_session()

        assert len(received) == 1
        ev = received[0]
        assert ev.timestamp == clock_dt
        assert ev.session_date == "2026-03-30"
        assert ev.daily_signal_count == 2
        assert ev.daily_pnl == 500.0

    async def test_calls_phantom_tracker_flush(self, session_factory):
        sm = _make_manager(session_factory, clock=lambda: _utc(2026, 3, 30, 20, 0))
        tracker = AsyncMock()
        sm.attach_phantom_tracker(tracker)

        await sm.open_session()
        await sm.close_session()

        tracker.flush_expired.assert_awaited_once()

    async def test_phantom_tracker_exception_does_not_propagate(self, session_factory):
        sm = _make_manager(session_factory, clock=lambda: _utc(2026, 3, 30, 20, 0))
        tracker = AsyncMock()
        tracker.flush_expired.side_effect = RuntimeError("tracker broke")
        sm.attach_phantom_tracker(tracker)

        await sm.open_session()
        # Should not raise
        await sm.close_session()
        assert sm.is_session_open is False

    async def test_close_event_listener_exception_does_not_propagate(
        self, session_factory
    ):
        sm = _make_manager(session_factory, clock=lambda: _utc(2026, 3, 30, 20, 0))
        sm.add_close_listener(
            lambda _: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        await sm.open_session()
        await sm.close_session()   # should not raise


# ---------------------------------------------------------------------------
# record_signal
# ---------------------------------------------------------------------------

class TestRecordSignal:

    def test_increments_on_each_decision(self):
        sm = SessionManager(
            session_factory=MagicMock(), phase=Phase.EVAL
        )
        assert sm.daily_signal_count == 0
        sm.record_signal(SignalDecision.ACCEPTED)
        assert sm.daily_signal_count == 1
        sm.record_signal(SignalDecision.REJECTED)
        assert sm.daily_signal_count == 2
        sm.record_signal(SignalDecision.EXPIRED)
        assert sm.daily_signal_count == 3

    async def test_count_persists_within_session(self, session_factory):
        sm = _make_manager(session_factory, clock=lambda: _utc(2026, 3, 30, 14, 0))
        await sm.open_session()
        sm.record_signal(SignalDecision.ACCEPTED)
        sm.record_signal(SignalDecision.REJECTED)
        assert sm.daily_signal_count == 2

    async def test_count_resets_on_open_session(self, session_factory):
        sm = _make_manager(session_factory, clock=lambda: _utc(2026, 3, 30, 14, 0))
        await sm.open_session()
        sm.record_signal(SignalDecision.ACCEPTED)
        sm.record_signal(SignalDecision.ACCEPTED)
        await sm.close_session()
        await sm.open_session()   # second open resets
        assert sm.daily_signal_count == 0


# ---------------------------------------------------------------------------
# attach_phantom_tracker
# ---------------------------------------------------------------------------

class TestAttachPhantomTracker:

    async def test_no_tracker_does_not_raise_on_close(self, session_factory):
        sm = _make_manager(session_factory, clock=lambda: _utc(2026, 3, 30, 20, 0))
        await sm.open_session()
        await sm.close_session()   # no tracker attached — must not raise

    async def test_tracker_not_called_if_session_never_opened(self, session_factory):
        sm = _make_manager(session_factory, clock=lambda: _utc(2026, 3, 30, 20, 0))
        tracker = AsyncMock()
        sm.attach_phantom_tracker(tracker)
        await sm.close_session()   # session was never opened
        tracker.flush_expired.assert_not_awaited()


# ---------------------------------------------------------------------------
# seconds_to_next_event (private, tested via observable behaviour)
# ---------------------------------------------------------------------------

class TestSecondsToNextEvent:
    """Verify the internal scheduler calculates sleep durations correctly."""

    def _sm(self) -> SessionManager:
        return SessionManager(session_factory=MagicMock(), phase=Phase.EVAL)

    def test_before_open_returns_positive(self):
        # Monday 2026-03-30 at 09:00 EDT = 13:00 UTC → 30 min until open
        now = _utc(2026, 3, 30, 13, 0)
        secs = self._sm()._seconds_to_next_event(now)
        assert 29 * 60 < secs <= 30 * 60

    def test_inside_session_returns_seconds_to_close(self):
        # Monday 2026-03-30 at 10:00 EDT = 14:00 UTC → 6h until 16:00 EDT
        now = _utc(2026, 3, 30, 14, 0)
        secs = self._sm()._seconds_to_next_event(now)
        assert 6 * 3600 - 1 < secs <= 6 * 3600

    def test_after_close_returns_seconds_to_next_day_open(self):
        # Monday 2026-03-30 at 21:00 UTC (17:00 EDT) → next open Tuesday 09:30 EDT
        now = _utc(2026, 3, 30, 21, 0)
        secs = self._sm()._seconds_to_next_event(now)
        # Tuesday 09:30 EDT = 13:30 UTC; from 21:00 = 16.5 hours
        expected = 16.5 * 3600
        assert abs(secs - expected) < 60

    def test_friday_after_close_skips_weekend(self):
        # Friday 2026-04-03 after 16:00 EDT; next open is Monday 2026-04-06 09:30 EDT
        now = _utc(2026, 4, 4, 0, 0)   # Friday 20:00 EDT = Saturday 00:00 UTC
        secs = self._sm()._seconds_to_next_event(now)
        # Monday 09:30 EDT = 13:30 UTC on 2026-04-06
        # From 2026-04-04 00:00 UTC: 2 days + 13.5 hours = 61.5 hours
        expected = (2 * 24 + 13.5) * 3600
        assert abs(secs - expected) < 120   # within 2 min


# ---------------------------------------------------------------------------
# run_scheduler / stop
# ---------------------------------------------------------------------------

class TestScheduler:

    async def test_stop_exits_cleanly(self, session_factory):
        """stop() must cause run_scheduler() to return promptly."""
        # Clock is set outside trading hours so no open/close is triggered
        fixed = _utc(2026, 4, 4, 12, 0)  # Saturday
        sm = _make_manager(session_factory, clock=lambda: fixed)

        task = asyncio.create_task(sm.run_scheduler())
        await asyncio.sleep(0.05)
        sm.stop()
        await asyncio.wait_for(task, timeout=2.0)
        assert not sm.is_session_open

    async def test_scheduler_opens_session_at_trading_hours(self, session_factory):
        """Scheduler opens the session when the clock enters trading hours."""
        # Start outside hours (Saturday), then flip to Monday mid-session
        times = [
            _utc(2026, 4, 4, 12, 0),   # Saturday — not trading
            _utc(2026, 3, 30, 15, 0),   # Monday in session
        ]
        idx = 0

        def advancing_clock():
            nonlocal idx
            return times[min(idx, len(times) - 1)]

        sm = _make_manager(session_factory, clock=advancing_clock)

        # Patch _seconds_to_next_event to return near-zero so the loop ticks fast
        original = sm._seconds_to_next_event
        sm._seconds_to_next_event = lambda _: 0.01   # type: ignore[method-assign]

        task = asyncio.create_task(sm.run_scheduler())
        await asyncio.sleep(0.05)
        idx = 1   # advance clock into trading hours
        await asyncio.sleep(0.1)
        sm.stop()
        await asyncio.wait_for(task, timeout=2.0)

        assert sm.is_session_open is True

    async def test_scheduler_closes_session_outside_trading_hours(
        self, session_factory
    ):
        """Scheduler closes the session when the clock leaves trading hours."""
        times = [
            _utc(2026, 3, 30, 15, 0),   # Monday in session
            _utc(2026, 3, 30, 21, 0),   # Monday after close
        ]
        idx = 0

        def advancing_clock():
            return times[min(idx, len(times) - 1)]

        sm = _make_manager(session_factory, clock=advancing_clock)
        sm._seconds_to_next_event = lambda _: 0.01   # type: ignore[method-assign]

        task = asyncio.create_task(sm.run_scheduler())
        await asyncio.sleep(0.05)   # should open
        assert sm.is_session_open is True

        idx = 1   # advance clock past close
        await asyncio.sleep(0.1)
        sm.stop()
        await asyncio.wait_for(task, timeout=2.0)

        assert sm.is_session_open is False

    async def test_stop_before_start_does_not_raise(self):
        sm = SessionManager(session_factory=MagicMock(), phase=Phase.EVAL)
        sm.stop()   # should be a no-op
