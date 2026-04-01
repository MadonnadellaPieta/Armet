"""
Circuit-breaker router — state inspection and manual trip/reset.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.api.deps import get_circuit_breaker
from backend.services.circuit_breaker import CircuitBreakerService

router = APIRouter(prefix="/circuit-breaker", tags=["circuit-breaker"])


class CircuitBreakerState(BaseModel):
    is_tripped: bool
    trigger: str | None
    trigger_value: str | None
    tripped_at: datetime | None


class TripBody(BaseModel):
    reason: str = ""


class ResetBody(BaseModel):
    reason: str = ""


@router.get("", response_model=CircuitBreakerState)
async def get_state(
    cb: CircuitBreakerService = Depends(get_circuit_breaker),
) -> dict[str, Any]:
    return cb.get_state()


@router.post("/trip", response_model=CircuitBreakerState)
async def trip(
    body: TripBody = TripBody(),
    cb: CircuitBreakerService = Depends(get_circuit_breaker),
) -> dict[str, Any]:
    return await cb.trip_manual(body.reason)


@router.post("/reset", response_model=CircuitBreakerState)
async def reset(
    body: ResetBody = ResetBody(),
    cb: CircuitBreakerService = Depends(get_circuit_breaker),
) -> dict[str, Any]:
    return await cb.reset(body.reason)
