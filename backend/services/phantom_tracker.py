"""
Phantom Tracker.

Monitors rejected/expired signals against subsequent live price action to
compute counterfactual outcomes: would the trade have been profitable if taken?

Usage:
    tracker = PhantomTracker(session_factory=async_session)
    tracker.track(rejected_signal)          # begin monitoring
    await tracker.on_tick(tick)             # call on every market tick
    await tracker.flush_expired()           # call periodically for sparse feeds
    tracker.active_count()                  # how many signals are still open
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from backend.core.models import Direction, Signal, SignalDecision, Tick
from backend.database.models import PhantomTradeRecord

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tick specifications per instrument  (tick_size, tick_value per contract)
# ---------------------------------------------------------------------------

_TICK_SPECS: dict[str, tuple[float, float]] = {
    "ES":  (0.25, 12.50),
    "MES": (0.25,  1.25),
    "NQ":  (0.25,  5.00),
    "MNQ": (0.25,  0.50),
}
_DEFAULT_TICK_SPEC = (0.25, 12.50)


def _tick_spec(instrument: str) -> tuple[float, float]:
    """Return (tick_size, tick_value) for the given instrument."""
    base = instrument.split(".")[0].upper()
    return _TICK_SPECS.get(base, _DEFAULT_TICK_SPEC)


def _dollar_pnl(
    direction: Direction,
    entry: float,
    exit_price: float,
    tick_size: float,
    tick_value: float,
) -> float:
    """Compute dollar P&L for qty=1 given entry and exit prices."""
    ticks = (exit_price - entry) / tick_size
    if direction is Direction.SHORT:
        ticks = -ticks
    return round(ticks * tick_value, 2)


# ---------------------------------------------------------------------------
# Internal per-signal monitoring state
# ---------------------------------------------------------------------------


@dataclass
class _PhantomState:
    signal: Signal
    window_end: datetime        # signal.timestamp + window_seconds
    tick_size: float
    tick_value: float

    # Set when conclusion is reached
    would_have_hit_tp: bool | None = field(default=None)
    would_have_hit_sl: bool | None = field(default=None)
    concluded: bool = field(default=False)
    conclusion_time: datetime | None = field(default=None)

    # Dollar excursions (always ≥ 0)
    max_favorable_excursion: float = field(default=0.0)
    max_adverse_excursion: float = field(default=0.0)

    # Last observed price (used for window-expiry P&L)
    last_price: float | None = field(default=None)

    def process_tick(self, tick: Tick) -> bool:
        """Update excursions and check for SL/TP hit.

        Returns True if this tick concludes monitoring (TP or SL hit).
        """
        price = tick.price
        self.last_price = price
        sig = self.signal

        # Update max favorable / adverse excursions (in dollars, qty=1)
        if sig.direction is Direction.LONG:
            favorable = price - sig.entry_price
            adverse = sig.entry_price - price
        else:
            favorable = sig.entry_price - price
            adverse = price - sig.entry_price

        fav_dollars = max(0.0, favorable / self.tick_size * self.tick_value)
        adv_dollars = max(0.0, adverse / self.tick_size * self.tick_value)

        if fav_dollars > self.max_favorable_excursion:
            self.max_favorable_excursion = fav_dollars
        if adv_dollars > self.max_adverse_excursion:
            self.max_adverse_excursion = adv_dollars

        # Check TP / SL breaches
        if sig.direction is Direction.LONG:
            hit_tp = price >= sig.take_profit_price
            hit_sl = price <= sig.stop_loss_price
        else:
            hit_tp = price <= sig.take_profit_price
            hit_sl = price >= sig.stop_loss_price

        if hit_tp or hit_sl:
            # If both simultaneously (gap), SL takes precedence (conservative)
            if hit_tp and hit_sl:
                self.would_have_hit_tp = False
                self.would_have_hit_sl = True
            else:
                self.would_have_hit_tp = hit_tp
                self.would_have_hit_sl = hit_sl
            self.concluded = True
            self.conclusion_time = tick.timestamp
            return True

        return False

    def conclude_expired(self, now: datetime) -> None:
        """Mark the signal as window-expired without hitting TP or SL."""
        self.concluded = True
        self.conclusion_time = now
        self.would_have_hit_tp = False
        self.would_have_hit_sl = False

    def phantom_pnl(self) -> float:
        """Compute dollar P&L for qty=1 based on the concluded outcome."""
        sig = self.signal
        if self.would_have_hit_tp:
            exit_price = sig.take_profit_price
        elif self.would_have_hit_sl:
            exit_price = sig.stop_loss_price
        else:
            # Window expired — use last observed price, or entry if no ticks seen
            exit_price = self.last_price if self.last_price is not None else sig.entry_price
        return _dollar_pnl(sig.direction, sig.entry_price, exit_price, self.tick_size, self.tick_value)

    def phantom_duration(self) -> float:
        """Seconds from signal timestamp to conclusion."""
        end = self.conclusion_time if self.conclusion_time is not None else datetime.utcnow()
        return (end - self.signal.timestamp).total_seconds()


# ---------------------------------------------------------------------------
# PhantomTracker
# ---------------------------------------------------------------------------


class PhantomTracker:
    """Monitor rejected/expired signals against live price action.

    Args:
        session_factory: Callable returning an async context manager for
            ``AsyncSession`` (e.g. ``async_session`` from ``backend.database.db``).
        window_seconds: How long (seconds) to monitor each signal after its
            timestamp before concluding it as expired.  Default: 300.
    """

    def __init__(
        self,
        session_factory: Any,
        window_seconds: int = 300,
    ) -> None:
        self._session_factory = session_factory
        self._window_seconds = window_seconds
        # instrument → list of active _PhantomState
        self._active: dict[str, list[_PhantomState]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def track(self, signal: Signal) -> None:
        """Begin monitoring a rejected or expired signal.

        Raises:
            ValueError: If ``signal.decision`` is not REJECTED or EXPIRED.
        """
        if signal.decision not in (SignalDecision.REJECTED, SignalDecision.EXPIRED):
            raise ValueError(
                f"PhantomTracker only tracks REJECTED/EXPIRED signals, "
                f"got {signal.decision}"
            )

        tick_size, tick_value = _tick_spec(signal.instrument)
        state = _PhantomState(
            signal=signal,
            window_end=signal.timestamp + timedelta(seconds=self._window_seconds),
            tick_size=tick_size,
            tick_value=tick_value,
        )

        self._active.setdefault(signal.instrument, []).append(state)
        logger.debug(
            "Phantom tracking started: signal=%s instrument=%s window=%ds",
            signal.id,
            signal.instrument,
            self._window_seconds,
        )

    async def on_tick(self, tick: Tick) -> None:
        """Process an incoming market tick.

        Concludes any signals whose TP/SL is hit or whose monitoring window
        has expired, then persists a PhantomTradeRecord for each.
        """
        states = self._active.get(tick.instrument)
        if not states:
            return

        to_conclude: list[_PhantomState] = []

        for state in list(states):
            if state.concluded:
                to_conclude.append(state)
                continue

            if tick.timestamp >= state.window_end:
                state.conclude_expired(tick.timestamp)
                to_conclude.append(state)
                continue

            if state.process_tick(tick):
                to_conclude.append(state)

        for state in to_conclude:
            states.remove(state)
            await self._persist(state)

        if not states:
            del self._active[tick.instrument]

    async def flush_expired(self, now: datetime | None = None) -> int:
        """Conclude all signals whose monitoring window has elapsed.

        Call this periodically when tick flow may be sparse (e.g. outside
        trading hours) so records are not left hanging indefinitely.

        Returns:
            Number of signals concluded by this flush.
        """
        if now is None:
            now = datetime.utcnow()

        count = 0
        for instrument in list(self._active.keys()):
            states = self._active[instrument]
            expired = [s for s in states if not s.concluded and now >= s.window_end]
            for state in expired:
                state.conclude_expired(now)
                states.remove(state)
                await self._persist(state)
                count += 1
            if not states:
                del self._active[instrument]

        return count

    def active_count(self) -> int:
        """Return the number of signals currently being monitored."""
        return sum(len(v) for v in self._active.values())

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _persist(self, state: _PhantomState) -> None:
        """Write a concluded PhantomState to the database."""
        record = PhantomTradeRecord(
            signal_id=state.signal.id,
            would_have_hit_tp=state.would_have_hit_tp,
            would_have_hit_sl=state.would_have_hit_sl,
            phantom_pnl=state.phantom_pnl(),
            phantom_duration=state.phantom_duration(),
            max_favorable_excursion=round(state.max_favorable_excursion, 2),
            max_adverse_excursion=round(state.max_adverse_excursion, 2),
        )
        try:
            async with self._session_factory() as session:
                session.add(record)
                await session.commit()
            logger.info(
                "Phantom record persisted: signal=%s tp=%s sl=%s pnl=%.2f dur=%.1fs",
                state.signal.id,
                state.would_have_hit_tp,
                state.would_have_hit_sl,
                state.phantom_pnl(),
                state.phantom_duration(),
            )
        except Exception:
            logger.exception(
                "Failed to persist phantom record for signal=%s", state.signal.id
            )
