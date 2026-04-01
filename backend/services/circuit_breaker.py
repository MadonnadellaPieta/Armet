"""
CircuitBreakerService — in-memory kill-switch state with DB persistence.

Supports manual trip/reset and auto-trigger hooks (to be called by the
signal engine in Task 8).  State is intentionally in-memory so reads are
zero-latency; every state change is persisted to circuit_breaker_events.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from backend.database.models import CircuitBreakerEventRecord


class CircuitBreakerState:
    __slots__ = ("is_tripped", "trigger", "trigger_value", "tripped_at")

    def __init__(self) -> None:
        self.is_tripped: bool = False
        self.trigger: str | None = None
        self.trigger_value: str | None = None
        self.tripped_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "is_tripped": self.is_tripped,
            "trigger": self.trigger,
            "trigger_value": self.trigger_value,
            "tripped_at": self.tripped_at,
        }


class CircuitBreakerService:
    """
    Manages circuit-breaker state.

    Parameters
    ----------
    session_factory:
        Callable returning an async context-manager ``AsyncSession``.
        Pass ``async_session`` from ``backend.database.db``.
    thresholds:
        Dict loaded from the ``circuit_breakers`` block of default_config.yaml.
    """

    def __init__(self, session_factory: Any, thresholds: dict[str, Any]) -> None:
        self._session_factory = session_factory
        self.thresholds = thresholds
        self._state = CircuitBreakerState()

    # ------------------------------------------------------------------
    # Public read
    # ------------------------------------------------------------------

    def get_state(self) -> dict[str, Any]:
        return self._state.as_dict()

    # ------------------------------------------------------------------
    # Manual controls
    # ------------------------------------------------------------------

    async def trip_manual(self, reason: str = "") -> dict[str, Any]:
        """Trip the circuit breaker manually."""
        self._state.is_tripped = True
        self._state.trigger = "MANUAL"
        self._state.trigger_value = reason or None
        self._state.tripped_at = datetime.utcnow()

        await self._persist_event("MANUAL", reason or None)
        return self._state.as_dict()

    async def reset(self, reason: str = "") -> dict[str, Any]:
        """Reset the circuit breaker (clears tripped state)."""
        self._state.is_tripped = False
        self._state.trigger = None
        self._state.trigger_value = None
        self._state.tripped_at = None
        return self._state.as_dict()

    # ------------------------------------------------------------------
    # Auto-trigger hooks (called by signal engine, Task 8)
    # ------------------------------------------------------------------

    async def trip_auto(
        self,
        trigger_type: str,
        trigger_value: str | None = None,
        positions_flattened: int = 0,
    ) -> dict[str, Any]:
        """Trip automatically (e.g. MAX_CONSECUTIVE_LOSSES)."""
        self._state.is_tripped = True
        self._state.trigger = trigger_type
        self._state.trigger_value = trigger_value
        self._state.tripped_at = datetime.utcnow()

        await self._persist_event(trigger_type, trigger_value, positions_flattened)
        return self._state.as_dict()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _persist_event(
        self,
        trigger_type: str,
        trigger_value: str | None = None,
        positions_flattened: int = 0,
    ) -> None:
        try:
            async with self._session_factory() as session:
                record = CircuitBreakerEventRecord(
                    id=uuid.uuid4().hex,
                    trigger_type=trigger_type,
                    trigger_value=trigger_value,
                    positions_flattened=positions_flattened,
                )
                session.add(record)
                await session.commit()
        except Exception:
            # Never let DB errors block the kill-switch
            pass
