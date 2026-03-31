"""
Tests for the CircuitBreakerService.

Covers: manual trip, consecutive-loss trip, daily-loss trip, daily-profit cap
trip, record_win resets counter, reset un-trips, daily_reset clears counters
without un-tripping, no double-trip, DB persistence for each trigger type, and
positions_flattened default.

Uses an in-memory SQLite database so tests are fast and isolated.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.core.models import CircuitBreakerTrigger
from backend.database.models import Base, CircuitBreakerEventRecord
from backend.services.circuit_breaker import CircuitBreakerService, CircuitBreakerState


# ---------------------------------------------------------------------------
# Fixtures
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


@pytest.fixture
def fixed_clock():
    """A clock that always returns the same deterministic datetime."""
    ts = datetime(2026, 1, 15, 9, 30, 0)
    return lambda: ts


@pytest.fixture
async def cb(session_factory, fixed_clock):
    """Default CircuitBreakerService with tight thresholds for testing."""
    return CircuitBreakerService(
        session_factory=session_factory,
        max_consecutive_losses=3,
        max_daily_loss=1500.0,
        max_daily_profit=5000.0,
        clock=fixed_clock,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _db_events(session_factory) -> list[CircuitBreakerEventRecord]:
    async with session_factory() as session:
        result = await session.execute(
            select(CircuitBreakerEventRecord).order_by(
                CircuitBreakerEventRecord.timestamp
            )
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


class TestInitialState:
    async def test_not_tripped_at_start(self, cb):
        assert cb.is_tripped is False

    async def test_state_returns_dataclass(self, cb):
        s = cb.state
        assert isinstance(s, CircuitBreakerState)
        assert s.is_tripped is False
        assert s.trigger is None
        assert s.trigger_value is None
        assert s.tripped_at is None


# ---------------------------------------------------------------------------
# Manual trip
# ---------------------------------------------------------------------------


class TestManualTrip:
    async def test_trip_manual_trips_breaker(self, cb):
        await cb.trip_manual("testing")
        assert cb.is_tripped is True

    async def test_trip_manual_sets_trigger(self, cb):
        await cb.trip_manual("testing")
        s = cb.state
        assert s.trigger is CircuitBreakerTrigger.MANUAL
        assert s.trigger_value == "testing"

    async def test_trip_manual_records_timestamp(self, cb, fixed_clock):
        await cb.trip_manual()
        assert cb.state.tripped_at == fixed_clock()

    async def test_trip_manual_no_reason_stores_none(self, cb):
        await cb.trip_manual()
        assert cb.state.trigger_value is None

    async def test_trip_manual_persists_to_db(self, cb, session_factory):
        await cb.trip_manual("my reason")
        events = await _db_events(session_factory)
        assert len(events) == 1
        assert events[0].trigger_type == CircuitBreakerTrigger.MANUAL.value
        assert events[0].trigger_value == "my reason"

    async def test_trip_manual_positions_flattened_default(self, cb, session_factory):
        await cb.trip_manual()
        events = await _db_events(session_factory)
        assert events[0].positions_flattened == 0


# ---------------------------------------------------------------------------
# No double-trip
# ---------------------------------------------------------------------------


class TestNoDoubleTrip:
    async def test_second_manual_trip_is_noop(self, cb, session_factory):
        await cb.trip_manual("first")
        await cb.trip_manual("second")
        # Still tripped with original trigger_value
        assert cb.state.trigger_value == "first"
        # Only one DB event persisted
        events = await _db_events(session_factory)
        assert len(events) == 1

    async def test_record_loss_after_trip_is_noop(self, cb, session_factory):
        await cb.trip_manual("pre-trip")
        result = await cb.record_loss(-100.0)
        assert result is False
        events = await _db_events(session_factory)
        assert len(events) == 1  # only the manual trip

    async def test_record_win_after_trip_does_not_persist(self, cb, session_factory):
        await cb.trip_manual()
        await cb.record_win(99999.0)  # would exceed max_daily_profit if not blocked
        events = await _db_events(session_factory)
        assert len(events) == 1  # only the manual trip


# ---------------------------------------------------------------------------
# Consecutive-loss trip
# ---------------------------------------------------------------------------


class TestConsecutiveLossTrip:
    async def test_single_loss_does_not_trip(self, cb):
        result = await cb.record_loss(-100.0)
        assert result is False
        assert cb.is_tripped is False

    async def test_two_losses_do_not_trip(self, cb):
        await cb.record_loss(-100.0)
        result = await cb.record_loss(-100.0)
        assert result is False
        assert cb.is_tripped is False

    async def test_three_consecutive_losses_trip(self, cb):
        await cb.record_loss(-100.0)
        await cb.record_loss(-100.0)
        result = await cb.record_loss(-100.0)
        assert result is True
        assert cb.is_tripped is True

    async def test_consecutive_loss_trigger_type(self, cb):
        for _ in range(3):
            await cb.record_loss(-50.0)
        assert cb.state.trigger is CircuitBreakerTrigger.MAX_CONSECUTIVE_LOSSES

    async def test_consecutive_loss_trigger_value(self, cb):
        for _ in range(3):
            await cb.record_loss(-50.0)
        assert cb.state.trigger_value == "3"

    async def test_consecutive_loss_persists_to_db(self, cb, session_factory):
        for _ in range(3):
            await cb.record_loss(-50.0)
        events = await _db_events(session_factory)
        assert len(events) == 1
        assert events[0].trigger_type == CircuitBreakerTrigger.MAX_CONSECUTIVE_LOSSES.value
        assert events[0].trigger_value == "3"

    async def test_record_loss_raises_on_nonnegative_pnl(self, cb):
        with pytest.raises(ValueError):
            await cb.record_loss(100.0)

    async def test_record_loss_raises_on_zero_pnl(self, cb):
        with pytest.raises(ValueError):
            await cb.record_loss(0.0)


# ---------------------------------------------------------------------------
# Daily-loss trip
# ---------------------------------------------------------------------------


class TestDailyLossTrip:
    async def test_small_loss_does_not_trip(self, cb):
        result = await cb.record_loss(-500.0)
        assert result is False

    async def test_cumulative_loss_at_threshold_trips(self, cb):
        # Two losses totalling exactly -1500
        await cb.record_loss(-750.0)
        result = await cb.record_loss(-750.0)
        assert result is True
        assert cb.state.trigger is CircuitBreakerTrigger.MAX_DAILY_LOSS

    async def test_daily_loss_trigger_value_is_formatted_pnl(self, cb):
        await cb.record_loss(-750.0)
        await cb.record_loss(-750.0)
        assert cb.state.trigger_value == "-1500.00"

    async def test_daily_loss_persists_to_db(self, cb, session_factory):
        await cb.record_loss(-800.0)
        await cb.record_loss(-800.0)
        events = await _db_events(session_factory)
        assert len(events) == 1
        assert events[0].trigger_type == CircuitBreakerTrigger.MAX_DAILY_LOSS.value

    async def test_consecutive_loss_wins_over_daily_loss_when_both_hit(
        self, session_factory, fixed_clock
    ):
        """If a single loss triggers both thresholds, consecutive-loss wins."""
        cb = CircuitBreakerService(
            session_factory=session_factory,
            max_consecutive_losses=2,
            max_daily_loss=100.0,
            clock=fixed_clock,
        )
        await cb.record_loss(-50.0)
        await cb.record_loss(-60.0)  # 2 consecutive AND -110 > 100 daily loss
        assert cb.state.trigger is CircuitBreakerTrigger.MAX_CONSECUTIVE_LOSSES


# ---------------------------------------------------------------------------
# Daily-profit cap trip
# ---------------------------------------------------------------------------


class TestDailyProfitTrip:
    async def test_small_win_does_not_trip(self, cb):
        await cb.record_win(100.0)
        assert cb.is_tripped is False

    async def test_win_reaching_profit_cap_trips(self, cb):
        await cb.record_win(5000.0)
        assert cb.is_tripped is True
        assert cb.state.trigger is CircuitBreakerTrigger.MAX_DAILY_PROFIT

    async def test_cumulative_wins_reaching_cap_trip(self, cb):
        await cb.record_win(2500.0)
        await cb.record_win(2500.0)
        assert cb.state.trigger is CircuitBreakerTrigger.MAX_DAILY_PROFIT

    async def test_profit_trip_trigger_value(self, cb):
        await cb.record_win(5000.0)
        assert cb.state.trigger_value == "5000.00"

    async def test_profit_trip_persists_to_db(self, cb, session_factory):
        await cb.record_win(5000.0)
        events = await _db_events(session_factory)
        assert len(events) == 1
        assert events[0].trigger_type == CircuitBreakerTrigger.MAX_DAILY_PROFIT.value

    async def test_no_profit_cap_configured_never_trips_on_wins(
        self, session_factory, fixed_clock
    ):
        cb = CircuitBreakerService(
            session_factory=session_factory,
            max_daily_profit=None,
            clock=fixed_clock,
        )
        await cb.record_win(999_999.0)
        assert cb.is_tripped is False

    async def test_record_win_raises_on_negative_pnl(self, cb):
        with pytest.raises(ValueError):
            await cb.record_win(-100.0)


# ---------------------------------------------------------------------------
# record_win resets consecutive-loss counter
# ---------------------------------------------------------------------------


class TestRecordWinResetsCounter:
    async def test_win_after_two_losses_resets_counter(self, cb):
        await cb.record_loss(-100.0)
        await cb.record_loss(-100.0)
        await cb.record_win(50.0)
        # Now need 3 more losses to trip (counter was reset)
        await cb.record_loss(-100.0)
        await cb.record_loss(-100.0)
        result = await cb.record_loss(-100.0)
        assert result is True
        assert cb.state.trigger is CircuitBreakerTrigger.MAX_CONSECUTIVE_LOSSES

    async def test_single_win_breaks_loss_streak(self, cb):
        await cb.record_loss(-100.0)
        await cb.record_loss(-100.0)
        await cb.record_win(50.0)
        # Two more losses should NOT trip (only 2 consecutive, need 3)
        await cb.record_loss(-100.0)
        result = await cb.record_loss(-100.0)
        assert result is False


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


class TestReset:
    async def test_reset_untrips_breaker(self, cb):
        await cb.trip_manual("test")
        await cb.reset()
        assert cb.is_tripped is False

    async def test_reset_clears_state(self, cb):
        await cb.trip_manual("test")
        await cb.reset("clear")
        s = cb.state
        assert s.trigger is None
        assert s.trigger_value is None
        assert s.tripped_at is None

    async def test_reset_clears_counters(self, cb):
        await cb.record_loss(-100.0)
        await cb.record_loss(-100.0)
        await cb.trip_manual()
        await cb.reset()
        # After reset we need 3 new consecutive losses to trip again
        await cb.record_loss(-100.0)
        await cb.record_loss(-100.0)
        result = await cb.record_loss(-100.0)
        assert result is True

    async def test_reset_persists_to_db(self, cb, session_factory):
        await cb.trip_manual("trip")
        await cb.reset("manual clear")
        events = await _db_events(session_factory)
        # Two events: trip + reset
        assert len(events) == 2
        reset_event = events[1]
        assert reset_event.trigger_type == CircuitBreakerTrigger.MANUAL.value
        assert reset_event.trigger_value == "RESET: manual clear"

    async def test_reset_with_empty_reason(self, cb, session_factory):
        await cb.trip_manual()
        await cb.reset()
        events = await _db_events(session_factory)
        reset_event = events[1]
        assert reset_event.trigger_value == "RESET: "


# ---------------------------------------------------------------------------
# Daily reset
# ---------------------------------------------------------------------------


class TestDailyReset:
    async def test_daily_reset_clears_consecutive_losses(self, cb):
        await cb.record_loss(-100.0)
        await cb.record_loss(-100.0)
        await cb.daily_reset()
        # Counter cleared; need 3 more
        await cb.record_loss(-100.0)
        await cb.record_loss(-100.0)
        result = await cb.record_loss(-100.0)
        assert result is True

    async def test_daily_reset_clears_daily_pnl(self, cb):
        await cb.record_loss(-1400.0)
        await cb.daily_reset()
        # One more loss that would have tipped us over, but counter was cleared
        result = await cb.record_loss(-200.0)
        assert result is False  # still under threshold after reset

    async def test_daily_reset_does_not_untrip(self, cb):
        await cb.trip_manual("staying tripped")
        await cb.daily_reset()
        assert cb.is_tripped is True
        assert cb.state.trigger is CircuitBreakerTrigger.MANUAL

    async def test_daily_reset_accepts_datetime_arg(self, cb):
        dt = datetime(2026, 1, 16, 9, 30, 0)
        # Should not raise
        await cb.daily_reset(dt=dt)
        assert cb.is_tripped is False

    async def test_daily_reset_does_not_persist_event(self, cb, session_factory):
        await cb.daily_reset()
        events = await _db_events(session_factory)
        assert len(events) == 0


# ---------------------------------------------------------------------------
# DB persistence — positions_flattened default
# ---------------------------------------------------------------------------


class TestDbPersistence:
    async def test_all_trip_events_have_positions_flattened_zero(
        self, session_factory, fixed_clock
    ):
        cb = CircuitBreakerService(
            session_factory=session_factory,
            max_consecutive_losses=1,
            max_daily_loss=100.0,
            max_daily_profit=100.0,
            clock=fixed_clock,
        )
        await cb.record_loss(-200.0)  # trips MAX_DAILY_LOSS (1 loss < 1 consecutive)
        events = await _db_events(session_factory)
        assert all(e.positions_flattened == 0 for e in events)

    async def test_manual_trip_db_record(self, session_factory, fixed_clock):
        cb = CircuitBreakerService(
            session_factory=session_factory,
            clock=fixed_clock,
        )
        await cb.trip_manual("halt")
        events = await _db_events(session_factory)
        assert len(events) == 1
        e = events[0]
        assert e.trigger_type == "MANUAL"
        assert e.trigger_value == "halt"
        assert e.positions_flattened == 0

    async def test_consecutive_loss_db_record(self, session_factory, fixed_clock):
        cb = CircuitBreakerService(
            session_factory=session_factory,
            max_consecutive_losses=2,
            max_daily_loss=99999.0,
            clock=fixed_clock,
        )
        await cb.record_loss(-10.0)
        await cb.record_loss(-10.0)
        events = await _db_events(session_factory)
        assert len(events) == 1
        e = events[0]
        assert e.trigger_type == "MAX_CONSECUTIVE_LOSSES"
        assert e.trigger_value == "2"
        assert e.positions_flattened == 0

    async def test_daily_loss_db_record(self, session_factory, fixed_clock):
        cb = CircuitBreakerService(
            session_factory=session_factory,
            max_consecutive_losses=999,
            max_daily_loss=500.0,
            clock=fixed_clock,
        )
        await cb.record_loss(-600.0)
        events = await _db_events(session_factory)
        assert len(events) == 1
        e = events[0]
        assert e.trigger_type == "MAX_DAILY_LOSS"
        assert e.trigger_value == "-600.00"
        assert e.positions_flattened == 0

    async def test_daily_profit_db_record(self, session_factory, fixed_clock):
        cb = CircuitBreakerService(
            session_factory=session_factory,
            max_daily_profit=1000.0,
            clock=fixed_clock,
        )
        await cb.record_win(1000.0)
        events = await _db_events(session_factory)
        assert len(events) == 1
        e = events[0]
        assert e.trigger_type == "MAX_DAILY_PROFIT"
        assert e.trigger_value == "1000.00"
        assert e.positions_flattened == 0
