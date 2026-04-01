"""
Circuit Breaker — kill switch for the trading engine.

Monitors daily P&L, consecutive losses, and daily profit targets.
Once tripped, the engine stops accepting new signals until reset.

Persists every trip event to CircuitBreakerEventRecord.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.models import CircuitBreakerTrigger
from backend.database.models import CircuitBreakerEventRecord


def _uid() -> str:
    return uuid.uuid4().hex


class CircuitBreaker:
    """Monitors trading conditions and trips when safety limits are breached."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        params: dict[str, Any] | None = None,
    ) -> None:
        self._session_factory = session_factory
        defaults = self.default_params()
        self._params: dict[str, Any] = {**defaults, **(params or {})}

        self._tripped = False
        self._trip_reason: CircuitBreakerTrigger | None = None
        self._consecutive_losses: int = 0

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    @staticmethod
    def default_params() -> dict[str, Any]:
        return {
            "max_daily_loss": 1500.0,
            "max_consecutive_losses": 3,
            "max_daily_profit": 5000.0,
            "enabled": True,
        }

    def update_params(self, new_params: dict[str, Any]) -> None:
        for k, v in new_params.items():
            if k in self._params:
                self._params[k] = v

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def is_tripped(self) -> bool:
        return self._tripped

    @property
    def trip_reason(self) -> CircuitBreakerTrigger | None:
        return self._trip_reason

    def reset(self) -> None:
        """Reset the circuit breaker (manual intervention required)."""
        self._tripped = False
        self._trip_reason = None
        self._consecutive_losses = 0

    def reset_consecutive_losses(self) -> None:
        """Reset consecutive loss counter (e.g. on a winning trade)."""
        self._consecutive_losses = 0

    # ------------------------------------------------------------------
    # Checks
    # ------------------------------------------------------------------

    async def check_daily_loss(
        self, daily_pnl: float, positions_flattened: int = 0
    ) -> bool:
        """Trip if daily loss exceeds configured limit.

        Returns True if the circuit breaker tripped.
        """
        if not self._params["enabled"]:
            return False
        limit = self._params["max_daily_loss"]
        if daily_pnl <= -limit:
            await self._trip(
                CircuitBreakerTrigger.MAX_DAILY_LOSS,
                f"daily_pnl={daily_pnl:.2f}",
                positions_flattened,
            )
            return True
        return False

    async def check_daily_profit(
        self, daily_pnl: float, positions_flattened: int = 0
    ) -> bool:
        """Trip if daily profit target is reached (protect gains).

        Returns True if the circuit breaker tripped.
        """
        if not self._params["enabled"]:
            return False
        limit = self._params["max_daily_profit"]
        if daily_pnl >= limit:
            await self._trip(
                CircuitBreakerTrigger.MAX_DAILY_PROFIT,
                f"daily_pnl={daily_pnl:.2f}",
                positions_flattened,
            )
            return True
        return False

    async def record_trade_result(
        self, pnl: float, positions_flattened: int = 0
    ) -> bool:
        """Update consecutive loss counter and trip if threshold reached.

        Returns True if the circuit breaker tripped.
        """
        if not self._params["enabled"]:
            return False
        if pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        limit = self._params["max_consecutive_losses"]
        if self._consecutive_losses >= limit:
            await self._trip(
                CircuitBreakerTrigger.MAX_CONSECUTIVE_LOSSES,
                str(self._consecutive_losses),
                positions_flattened,
            )
            return True
        return False

    async def manual_trip(self, positions_flattened: int = 0) -> None:
        """Manually trip the circuit breaker."""
        await self._trip(
            CircuitBreakerTrigger.MANUAL,
            "manual",
            positions_flattened,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _trip(
        self,
        trigger: CircuitBreakerTrigger,
        trigger_value: str,
        positions_flattened: int,
    ) -> None:
        if self._tripped:
            return  # already tripped; don't duplicate records
        self._tripped = True
        self._trip_reason = trigger

        async with self._session_factory() as sess:
            record = CircuitBreakerEventRecord(
                id=_uid(),
                timestamp=datetime.utcnow(),
                trigger_type=trigger.value,
                trigger_value=trigger_value,
                positions_flattened=positions_flattened,
            )
            sess.add(record)
            await sess.commit()
