"""
Session Manager — trading hours enforcement and daily session lifecycle.

Enforces the configured trading window (default 09:30–16:00 ET).
Provides a hook for daily reset logic (counters, strategy state, etc.).

All datetime comparisons are done in ET (America/New_York).  For
portability in tests, the current time can be injected via the
``now_fn`` constructor parameter.
"""

from __future__ import annotations

from datetime import datetime, time
from typing import Callable
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

_DEFAULT_START = time(9, 30)
_DEFAULT_END = time(16, 0)


class SessionManager:
    """Manages the intraday trading session lifecycle."""

    def __init__(
        self,
        start_time: time = _DEFAULT_START,
        end_time: time = _DEFAULT_END,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._start = start_time
        self._end = end_time
        # Allow tests to inject a fixed "now" for deterministic behaviour.
        self._now_fn: Callable[[], datetime] = now_fn or datetime.utcnow

        # Daily reset state — set True once daily reset has run for today.
        self._last_reset_date: datetime | None = None

    # ------------------------------------------------------------------
    # Trading hours
    # ------------------------------------------------------------------

    def is_trading_hours(self, dt: datetime | None = None) -> bool:
        """Return True if *dt* (or now) falls within the trading window.

        *dt* may be UTC or naive; it is converted to ET for the comparison.
        """
        if dt is None:
            dt = self._now_fn()

        et_dt = self._to_et(dt)
        current_time = et_dt.time()
        return self._start <= current_time < self._end

    def should_suppress_signal(self, dt: datetime | None = None) -> bool:
        """Return True if signals should be suppressed (outside trading hours)."""
        return not self.is_trading_hours(dt)

    # ------------------------------------------------------------------
    # Daily reset
    # ------------------------------------------------------------------

    def needs_daily_reset(self, dt: datetime | None = None) -> bool:
        """Return True if a daily reset should be performed for this session date."""
        if dt is None:
            dt = self._now_fn()
        et_dt = self._to_et(dt)
        session_date = et_dt.date()

        if self._last_reset_date is None:
            return True
        return self._last_reset_date.date() < session_date

    def mark_daily_reset_done(self, dt: datetime | None = None) -> None:
        """Record that the daily reset has been performed."""
        self._last_reset_date = self._to_et(dt or self._now_fn())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_et(dt: datetime) -> datetime:
        """Convert *dt* to America/New_York.  If naive, assume UTC."""
        if dt.tzinfo is None:
            from zoneinfo import ZoneInfo as _ZI
            dt = dt.replace(tzinfo=_ZI("UTC"))
        return dt.astimezone(_ET)
