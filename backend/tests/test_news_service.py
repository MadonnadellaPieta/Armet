"""
Tests for NewsService.

Covers:
- Suppression logic for HIGH-impact events
- Buffer boundary conditions (pre/post)
- Mixed impact levels (LOW/MEDIUM do not suppress)
- Deduplication in load_events (in-memory and DB)
- DB persistence via EventRecord
- next_suppression_window (nearest window, 24-hour horizon, None when empty)
- get_events_in_range
- clear_past_events
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.core.models import ImpactLevel
from backend.database.models import Base, EventRecord
from backend.services.news_service import NewsEvent, NewsService


# ---------------------------------------------------------------------------
# In-memory SQLite fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def session_factory() -> async_sessionmaker:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return factory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _event(
    name: str = "CPI",
    ts: datetime | None = None,
    impact: ImpactLevel = ImpactLevel.HIGH,
    eid: str = "evt-001",
) -> NewsEvent:
    return NewsEvent(
        id=eid,
        name=name,
        timestamp=ts or datetime(2026, 3, 31, 14, 30),
        impact_level=impact,
    )


def _service(
    session_factory: async_sessionmaker,
    now: datetime,
    pre: int = 5,
    post: int = 2,
) -> NewsService:
    return NewsService(
        session_factory=session_factory,
        pre_buffer_minutes=pre,
        post_buffer_minutes=post,
        clock=lambda: now,
    )


# ---------------------------------------------------------------------------
# Suppression logic
# ---------------------------------------------------------------------------


class TestIsSuppressed:
    def test_inside_pre_buffer(self, session_factory):
        ts = datetime(2026, 3, 31, 14, 30)
        # 3 minutes before event — within 5-min pre-buffer
        now = ts - timedelta(minutes=3)
        svc = _service(session_factory, now)
        svc._events.append(_event(ts=ts))
        assert svc.is_suppressed() is True

    def test_at_event_time(self, session_factory):
        ts = datetime(2026, 3, 31, 14, 30)
        svc = _service(session_factory, ts)
        svc._events.append(_event(ts=ts))
        assert svc.is_suppressed() is True

    def test_inside_post_buffer(self, session_factory):
        ts = datetime(2026, 3, 31, 14, 30)
        # 1 minute after event — within 2-min post-buffer
        now = ts + timedelta(minutes=1)
        svc = _service(session_factory, now)
        svc._events.append(_event(ts=ts))
        assert svc.is_suppressed() is True

    def test_outside_pre_buffer(self, session_factory):
        ts = datetime(2026, 3, 31, 14, 30)
        # 6 minutes before event — outside 5-min pre-buffer
        now = ts - timedelta(minutes=6)
        svc = _service(session_factory, now)
        svc._events.append(_event(ts=ts))
        assert svc.is_suppressed() is False

    def test_outside_post_buffer(self, session_factory):
        ts = datetime(2026, 3, 31, 14, 30)
        # 3 minutes after event — outside 2-min post-buffer
        now = ts + timedelta(minutes=3)
        svc = _service(session_factory, now)
        svc._events.append(_event(ts=ts))
        assert svc.is_suppressed() is False

    def test_exact_pre_buffer_boundary(self, session_factory):
        ts = datetime(2026, 3, 31, 14, 30)
        # Exactly at start of pre-buffer (inclusive)
        now = ts - timedelta(minutes=5)
        svc = _service(session_factory, now)
        svc._events.append(_event(ts=ts))
        assert svc.is_suppressed() is True

    def test_exact_post_buffer_boundary(self, session_factory):
        ts = datetime(2026, 3, 31, 14, 30)
        # Exactly at end of post-buffer (inclusive)
        now = ts + timedelta(minutes=2)
        svc = _service(session_factory, now)
        svc._events.append(_event(ts=ts))
        assert svc.is_suppressed() is True

    def test_explicit_dt_overrides_clock(self, session_factory):
        ts = datetime(2026, 3, 31, 14, 30)
        # Clock says "far away" but explicit dt is inside buffer
        svc = _service(session_factory, ts + timedelta(hours=1))
        svc._events.append(_event(ts=ts))
        assert svc.is_suppressed(dt=ts) is True

    def test_no_events(self, session_factory):
        svc = _service(session_factory, datetime(2026, 3, 31, 14, 30))
        assert svc.is_suppressed() is False


# ---------------------------------------------------------------------------
# Mixed impact levels
# ---------------------------------------------------------------------------


class TestMixedImpactLevels:
    def test_medium_does_not_suppress(self, session_factory):
        ts = datetime(2026, 3, 31, 14, 30)
        now = ts - timedelta(minutes=3)
        svc = _service(session_factory, now)
        svc._events.append(_event(ts=ts, impact=ImpactLevel.MEDIUM))
        assert svc.is_suppressed() is False

    def test_low_does_not_suppress(self, session_factory):
        ts = datetime(2026, 3, 31, 14, 30)
        now = ts - timedelta(minutes=3)
        svc = _service(session_factory, now)
        svc._events.append(_event(ts=ts, impact=ImpactLevel.LOW))
        assert svc.is_suppressed() is False

    def test_high_suppresses_even_with_low_present(self, session_factory):
        ts = datetime(2026, 3, 31, 14, 30)
        now = ts - timedelta(minutes=3)
        svc = _service(session_factory, now)
        svc._events.append(_event(name="Retail Sales", ts=ts, impact=ImpactLevel.LOW))
        svc._events.append(_event(name="CPI", ts=ts, impact=ImpactLevel.HIGH, eid="e2"))
        assert svc.is_suppressed() is True

    def test_multiple_high_events_any_window_triggers(self, session_factory):
        ts1 = datetime(2026, 3, 31, 14, 30)
        ts2 = datetime(2026, 3, 31, 16, 0)
        # In window of ts2 only
        now = ts2 - timedelta(minutes=2)
        svc = _service(session_factory, now)
        svc._events.append(_event(name="CPI", ts=ts1, eid="e1"))
        svc._events.append(_event(name="FOMC", ts=ts2, eid="e2"))
        assert svc.is_suppressed() is True


# ---------------------------------------------------------------------------
# load_events — dedup and DB persistence
# ---------------------------------------------------------------------------


class TestLoadEvents:
    async def test_persists_to_db(self, session_factory):
        svc = _service(session_factory, datetime(2026, 3, 31, 10, 0))
        evt = _event()
        await svc.load_events([evt])

        async with session_factory() as session:
            result = await session.execute(select(EventRecord))
            records = result.scalars().all()

        assert len(records) == 1
        assert records[0].name == "CPI"
        assert records[0].impact_level == "HIGH"

    async def test_persists_optional_fields(self, session_factory):
        svc = _service(session_factory, datetime(2026, 3, 31, 10, 0))
        evt = NewsEvent(
            id="e1",
            name="CPI",
            timestamp=datetime(2026, 3, 31, 14, 30),
            impact_level=ImpactLevel.HIGH,
            actual="3.2%",
            forecast="3.1%",
            previous="3.0%",
        )
        await svc.load_events([evt])

        async with session_factory() as session:
            result = await session.execute(select(EventRecord))
            rec = result.scalars().first()

        assert rec.actual == "3.2%"
        assert rec.forecast == "3.1%"
        assert rec.previous == "3.0%"

    async def test_dedup_skips_duplicate_in_db(self, session_factory):
        svc = _service(session_factory, datetime(2026, 3, 31, 10, 0))
        evt = _event()
        await svc.load_events([evt])
        # Load same event a second time — should not create a duplicate row.
        evt2 = _event(eid="evt-002")  # different id, same name+timestamp
        await svc.load_events([evt2])

        async with session_factory() as session:
            result = await session.execute(select(EventRecord))
            records = result.scalars().all()

        assert len(records) == 1

    async def test_dedup_in_memory(self, session_factory):
        svc = _service(session_factory, datetime(2026, 3, 31, 10, 0))
        evt = _event()
        await svc.load_events([evt])
        await svc.load_events([evt])  # same id
        assert len(svc._events) == 1

    async def test_different_events_both_stored(self, session_factory):
        svc = _service(session_factory, datetime(2026, 3, 31, 10, 0))
        e1 = _event(name="CPI", ts=datetime(2026, 3, 31, 14, 30), eid="e1")
        e2 = _event(name="FOMC", ts=datetime(2026, 3, 31, 18, 0), eid="e2")
        await svc.load_events([e1, e2])
        assert len(svc._events) == 2

        async with session_factory() as session:
            result = await session.execute(select(EventRecord))
            records = result.scalars().all()
        assert len(records) == 2


# ---------------------------------------------------------------------------
# next_suppression_window
# ---------------------------------------------------------------------------


class TestNextSuppressionWindow:
    def test_returns_window_for_upcoming_high_event(self, session_factory):
        ts = datetime(2026, 3, 31, 14, 30)
        now = ts - timedelta(hours=1)
        svc = _service(session_factory, now, pre=5, post=2)
        svc._events.append(_event(ts=ts))
        window = svc.next_suppression_window()
        assert window is not None
        start, end = window
        assert start == ts - timedelta(minutes=5)
        assert end == ts + timedelta(minutes=2)

    def test_returns_nearest_when_multiple(self, session_factory):
        now = datetime(2026, 3, 31, 10, 0)
        ts1 = datetime(2026, 3, 31, 14, 30)
        ts2 = datetime(2026, 3, 31, 12, 0)  # closer
        svc = _service(session_factory, now)
        svc._events.append(_event(name="CPI", ts=ts1, eid="e1"))
        svc._events.append(_event(name="FOMC", ts=ts2, eid="e2"))
        window = svc.next_suppression_window()
        assert window is not None
        # Nearest = ts2
        assert window[0] == ts2 - timedelta(minutes=5)

    def test_returns_none_when_no_high_events(self, session_factory):
        ts = datetime(2026, 3, 31, 14, 30)
        now = ts - timedelta(hours=1)
        svc = _service(session_factory, now)
        svc._events.append(_event(ts=ts, impact=ImpactLevel.MEDIUM))
        assert svc.next_suppression_window() is None

    def test_returns_none_when_all_past(self, session_factory):
        ts = datetime(2026, 3, 31, 14, 30)
        now = ts + timedelta(hours=1)  # event is in the past
        svc = _service(session_factory, now)
        svc._events.append(_event(ts=ts))
        assert svc.next_suppression_window() is None

    def test_returns_none_beyond_24_hour_horizon(self, session_factory):
        now = datetime(2026, 3, 31, 10, 0)
        ts = now + timedelta(hours=25)  # outside 24h window
        svc = _service(session_factory, now)
        svc._events.append(_event(ts=ts))
        assert svc.next_suppression_window() is None

    def test_within_24_hour_horizon(self, session_factory):
        now = datetime(2026, 3, 31, 10, 0)
        ts = now + timedelta(hours=23)  # inside 24h window
        svc = _service(session_factory, now)
        svc._events.append(_event(ts=ts))
        assert svc.next_suppression_window() is not None

    def test_currently_suppressed_window_included(self, session_factory):
        ts = datetime(2026, 3, 31, 14, 30)
        # We're inside the window already
        now = ts - timedelta(minutes=3)
        svc = _service(session_factory, now, pre=5, post=2)
        svc._events.append(_event(ts=ts))
        window = svc.next_suppression_window()
        assert window is not None
        # Window end is in the future relative to now
        assert window[1] > now

    def test_explicit_dt_parameter(self, session_factory):
        ts = datetime(2026, 3, 31, 14, 30)
        svc = _service(session_factory, datetime(2026, 3, 31, 1, 0))
        svc._events.append(_event(ts=ts))
        # Pass a different dt explicitly — clock is ignored
        explicit_now = ts - timedelta(hours=2)
        window = svc.next_suppression_window(dt=explicit_now)
        assert window is not None


# ---------------------------------------------------------------------------
# get_events_in_range
# ---------------------------------------------------------------------------


class TestGetEventsInRange:
    def test_returns_matching_events(self, session_factory):
        svc = _service(session_factory, datetime(2026, 3, 31, 10, 0))
        ts1 = datetime(2026, 3, 31, 12, 0)
        ts2 = datetime(2026, 3, 31, 14, 30)
        ts3 = datetime(2026, 3, 31, 17, 0)
        svc._events += [
            _event(name="A", ts=ts1, eid="e1"),
            _event(name="B", ts=ts2, eid="e2"),
            _event(name="C", ts=ts3, eid="e3"),
        ]
        result = svc.get_events_in_range(ts1, ts2)
        names = {e.name for e in result}
        assert names == {"A", "B"}

    def test_inclusive_boundaries(self, session_factory):
        svc = _service(session_factory, datetime(2026, 3, 31, 10, 0))
        ts = datetime(2026, 3, 31, 12, 0)
        svc._events.append(_event(ts=ts))
        result = svc.get_events_in_range(ts, ts)
        assert len(result) == 1

    def test_empty_when_no_match(self, session_factory):
        svc = _service(session_factory, datetime(2026, 3, 31, 10, 0))
        ts = datetime(2026, 3, 31, 12, 0)
        svc._events.append(_event(ts=ts))
        result = svc.get_events_in_range(
            ts + timedelta(hours=1), ts + timedelta(hours=2)
        )
        assert result == []

    def test_all_impact_levels_included(self, session_factory):
        svc = _service(session_factory, datetime(2026, 3, 31, 10, 0))
        base = datetime(2026, 3, 31, 12, 0)
        svc._events += [
            _event(name="H", ts=base, impact=ImpactLevel.HIGH, eid="e1"),
            _event(name="M", ts=base, impact=ImpactLevel.MEDIUM, eid="e2"),
            _event(name="L", ts=base, impact=ImpactLevel.LOW, eid="e3"),
        ]
        result = svc.get_events_in_range(base, base)
        assert len(result) == 3


# ---------------------------------------------------------------------------
# clear_past_events
# ---------------------------------------------------------------------------


class TestClearPastEvents:
    def test_removes_expired_events(self, session_factory):
        svc = _service(session_factory, datetime(2026, 3, 31, 15, 0), post=2)
        old_ts = datetime(2026, 3, 31, 12, 0)  # + 2 min post = 12:02; cutoff = 15:00
        svc._events.append(_event(ts=old_ts))
        svc.clear_past_events()
        assert svc._events == []

    def test_keeps_recent_events(self, session_factory):
        ts = datetime(2026, 3, 31, 14, 59)
        # post_buffer = 2min → window ends 15:01; cutoff = 15:00 → keep
        now = datetime(2026, 3, 31, 15, 0)
        svc = _service(session_factory, now, post=2)
        svc._events.append(_event(ts=ts))
        svc.clear_past_events()
        assert len(svc._events) == 1

    def test_keeps_future_events(self, session_factory):
        now = datetime(2026, 3, 31, 10, 0)
        future_ts = datetime(2026, 3, 31, 16, 0)
        svc = _service(session_factory, now)
        svc._events.append(_event(ts=future_ts))
        svc.clear_past_events()
        assert len(svc._events) == 1

    def test_explicit_dt_parameter(self, session_factory):
        old_ts = datetime(2026, 3, 31, 8, 0)
        svc = _service(session_factory, datetime(2026, 3, 31, 7, 0), post=2)
        svc._events.append(_event(ts=old_ts))
        # clock says 7:00 (before event), but explicit dt = 15:00 → event cleared
        svc.clear_past_events(dt=datetime(2026, 3, 31, 15, 0))
        assert svc._events == []

    def test_mixed_keeps_only_active(self, session_factory):
        now = datetime(2026, 3, 31, 15, 0)
        svc = _service(session_factory, now, post=2)
        old_ts = datetime(2026, 3, 31, 12, 0)      # expired
        future_ts = datetime(2026, 3, 31, 17, 0)   # upcoming
        active_ts = datetime(2026, 3, 31, 14, 59)  # window ends 15:01 > cutoff
        svc._events += [
            _event(name="Old", ts=old_ts, eid="e1"),
            _event(name="Future", ts=future_ts, eid="e2"),
            _event(name="Active", ts=active_ts, eid="e3"),
        ]
        svc.clear_past_events()
        names = {e.name for e in svc._events}
        assert names == {"Future", "Active"}
