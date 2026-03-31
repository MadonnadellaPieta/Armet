"""
News Service.

Integrates an economic-calendar feed and suppresses trading signals around
high-impact events.

Usage:
    service = NewsService(session_factory)
    await service.load_events([...])
    if service.is_suppressed():
        # skip signal generation
    window = service.next_suppression_window()
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.core.models import ImpactLevel
from backend.database.models import EventRecord


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class NewsEvent:
    """An economic-calendar event (in-memory, not an ORM model)."""

    id: str
    name: str
    timestamp: datetime          # naive UTC
    impact_level: ImpactLevel
    actual: str | None = None
    forecast: str | None = None
    previous: str | None = None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class NewsService:
    """Ingests NewsEvents and suppresses signals around HIGH-impact releases."""

    def __init__(
        self,
        session_factory: async_sessionmaker,
        pre_buffer_minutes: int = 5,
        post_buffer_minutes: int = 2,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._pre_buffer = timedelta(minutes=pre_buffer_minutes)
        self._post_buffer = timedelta(minutes=post_buffer_minutes)
        self._clock: Callable[[], datetime] = clock or datetime.utcnow
        self._events: list[NewsEvent] = []

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------

    async def load_events(self, events: list[NewsEvent]) -> None:
        """Ingest events into memory and persist new ones to the database.

        Deduplication is by (name, timestamp) — existing DB rows are skipped.
        """
        async with self._session_factory() as session:
            for event in events:
                # Check whether this (name, timestamp) already exists in DB.
                existing = await session.execute(
                    select(EventRecord).where(
                        EventRecord.name == event.name,
                        EventRecord.timestamp == event.timestamp,
                    )
                )
                if existing.scalars().first() is not None:
                    # Already in DB — still add/update in-memory store below.
                    pass
                else:
                    record = EventRecord(
                        id=uuid.uuid4().hex,
                        timestamp=event.timestamp,
                        name=event.name,
                        impact_level=event.impact_level.value,
                        actual=event.actual,
                        forecast=event.forecast,
                        previous=event.previous,
                    )
                    session.add(record)

            await session.commit()

        # Merge into in-memory store (dedup by id).
        existing_ids = {e.id for e in self._events}
        # Also dedup by (name, timestamp) for in-memory duplicates.
        existing_keys = {(e.name, e.timestamp) for e in self._events}
        for event in events:
            key = (event.name, event.timestamp)
            if event.id not in existing_ids and key not in existing_keys:
                self._events.append(event)
                existing_ids.add(event.id)
                existing_keys.add(key)

    # ------------------------------------------------------------------
    # Suppression queries
    # ------------------------------------------------------------------

    def is_suppressed(self, dt: datetime | None = None) -> bool:
        """Return True if *dt* falls within the buffer window of a HIGH-impact event."""
        now = dt if dt is not None else self._clock()
        for event in self._events:
            if event.impact_level is not ImpactLevel.HIGH:
                continue
            window_start = event.timestamp - self._pre_buffer
            window_end = event.timestamp + self._post_buffer
            if window_start <= now <= window_end:
                return True
        return False

    def next_suppression_window(
        self, dt: datetime | None = None
    ) -> tuple[datetime, datetime] | None:
        """Return (start, end) of the nearest upcoming HIGH-impact window.

        Looks up to 24 hours ahead. Returns None if nothing is scheduled.
        """
        now = dt if dt is not None else self._clock()
        horizon = now + timedelta(hours=24)

        nearest_start: datetime | None = None
        nearest_end: datetime | None = None

        for event in self._events:
            if event.impact_level is not ImpactLevel.HIGH:
                continue
            window_start = event.timestamp - self._pre_buffer
            window_end = event.timestamp + self._post_buffer
            # Only consider windows that end after *now* and start before horizon.
            if window_end <= now or window_start > horizon:
                continue
            if nearest_start is None or window_start < nearest_start:
                nearest_start = window_start
                nearest_end = window_end

        if nearest_start is None:
            return None
        return (nearest_start, nearest_end)  # type: ignore[return-value]

    def get_events_in_range(
        self, start: datetime, end: datetime
    ) -> list[NewsEvent]:
        """Return all events (any impact level) with timestamp in [start, end]."""
        return [e for e in self._events if start <= e.timestamp <= end]

    def clear_past_events(self, dt: datetime | None = None) -> None:
        """Remove events whose suppression window has fully elapsed before *dt*."""
        cutoff = dt if dt is not None else self._clock()
        self._events = [
            e for e in self._events
            if e.timestamp + self._post_buffer >= cutoff
        ]
