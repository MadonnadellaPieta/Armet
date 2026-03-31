"""
CircuitBreakerService — kill switch that halts all trading when configured
thresholds are breached.

Supported triggers:
- MAX_CONSECUTIVE_LOSSES: N losses in a row
- MAX_DAILY_LOSS: cumulative net daily P&L drops below -max_daily_loss
- MAX_DAILY_PROFIT: cumulative net daily P&L rises above max_daily_profit
- MANUAL: explicit trip via trip_manual()
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.models import CircuitBreakerTrigger
from backend.database.models import CircuitBreakerEventRecord


@dataclass
class CircuitBreakerState:
    is_tripped: bool
    trigger: CircuitBreakerTrigger | None
    trigger_value: str | None
    tripped_at: datetime | None


class CircuitBreakerService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        max_consecutive_losses: int = 3,
        max_daily_loss: float = 1500.0,
        max_daily_profit: float | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._max_consecutive_losses = max_consecutive_losses
        self._max_daily_loss = max_daily_loss
        self._max_daily_profit = max_daily_profit
        self._clock = clock or datetime.utcnow

        # Trip state
        self._is_tripped: bool = False
        self._trigger: CircuitBreakerTrigger | None = None
        self._trigger_value: str | None = None
        self._tripped_at: datetime | None = None

        # Counters (cleared by daily_reset)
        self._consecutive_losses: int = 0
        self._daily_pnl: float = 0.0  # net signed P&L for the session

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> CircuitBreakerState:
        return CircuitBreakerState(
            is_tripped=self._is_tripped,
            trigger=self._trigger,
            trigger_value=self._trigger_value,
            tripped_at=self._tripped_at,
        )

    @property
    def is_tripped(self) -> bool:
        return self._is_tripped

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _persist_event(
        self,
        trigger: CircuitBreakerTrigger,
        trigger_value: str | None = None,
        positions_flattened: int = 0,
    ) -> None:
        record = CircuitBreakerEventRecord(
            trigger_type=trigger.value,
            trigger_value=trigger_value,
            positions_flattened=positions_flattened,
        )
        async with self._session_factory() as session:
            session.add(record)
            await session.commit()

    async def _trip(
        self,
        trigger: CircuitBreakerTrigger,
        trigger_value: str | None = None,
    ) -> bool:
        """Trip the breaker. Returns True only if newly tripped."""
        if self._is_tripped:
            return False

        self._is_tripped = True
        self._trigger = trigger
        self._trigger_value = trigger_value
        self._tripped_at = self._clock()

        await self._persist_event(trigger, trigger_value)
        return True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def record_loss(self, pnl: float) -> bool:
        """Register a realized loss.

        pnl must be negative (standard signed P&L convention).
        Raises ValueError if pnl >= 0.
        Returns True if the breaker was newly tripped by this loss.
        """
        if pnl >= 0:
            raise ValueError(
                f"record_loss requires negative pnl; got {pnl}. "
                "Use record_win() for positive P&L."
            )

        if self._is_tripped:
            return False

        self._consecutive_losses += 1
        self._daily_pnl += pnl  # running net, decreases on losses

        # Consecutive-loss threshold (checked first)
        if self._consecutive_losses >= self._max_consecutive_losses:
            return await self._trip(
                CircuitBreakerTrigger.MAX_CONSECUTIVE_LOSSES,
                str(self._consecutive_losses),
            )

        # Daily loss threshold: net P&L has fallen below -max_daily_loss
        if -self._daily_pnl >= self._max_daily_loss:
            return await self._trip(
                CircuitBreakerTrigger.MAX_DAILY_LOSS,
                f"{self._daily_pnl:.2f}",
            )

        return False

    async def record_win(self, pnl: float) -> None:
        """Register a realized win.

        pnl must be non-negative.
        Raises ValueError if pnl < 0.
        Resets the consecutive-loss counter.
        May trip the breaker if max_daily_profit is configured and reached.
        """
        if pnl < 0:
            raise ValueError(
                f"record_win requires non-negative pnl; got {pnl}. "
                "Use record_loss() for negative P&L."
            )

        self._consecutive_losses = 0
        self._daily_pnl += pnl

        if self._max_daily_profit is not None and not self._is_tripped:
            if self._daily_pnl >= self._max_daily_profit:
                await self._trip(
                    CircuitBreakerTrigger.MAX_DAILY_PROFIT,
                    f"{self._daily_pnl:.2f}",
                )

    async def trip_manual(self, reason: str = "") -> None:
        """Immediately trip the breaker with a MANUAL trigger.

        No-op if already tripped.
        """
        await self._trip(CircuitBreakerTrigger.MANUAL, reason or None)

    async def reset(self, reason: str = "") -> None:
        """Un-trip the breaker, clear all counters, and persist a reset event."""
        self._is_tripped = False
        self._trigger = None
        self._trigger_value = None
        self._tripped_at = None
        self._consecutive_losses = 0
        self._daily_pnl = 0.0

        await self._persist_event(
            CircuitBreakerTrigger.MANUAL,
            f"RESET: {reason}",
        )

    async def daily_reset(self, dt: datetime | None = None) -> None:  # noqa: ARG002
        """Called at the start of a new trading day.

        Clears daily counters but does NOT un-trip an already-tripped breaker.
        The dt parameter is accepted for caller convenience but unused internally.
        """
        self._consecutive_losses = 0
        self._daily_pnl = 0.0
