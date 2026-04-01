"""
Circuit-breaker router — state inspection and manual trip/reset.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_circuit_breaker, get_db
from backend.database.models import CircuitBreakerEventRecord
from backend.services.circuit_breaker import CircuitBreaker

router = APIRouter(prefix="/circuit-breaker", tags=["circuit-breaker"])


class CircuitBreakerStatus(BaseModel):
    is_tripped: bool
    trigger: str | None = None
    tripped_at: datetime | None = None
    reason: str | None = None


class TripBody(BaseModel):
    reason: str = ""


async def _last_trip_record(db: AsyncSession) -> CircuitBreakerEventRecord | None:
    result = await db.execute(
        select(CircuitBreakerEventRecord)
        .order_by(CircuitBreakerEventRecord.timestamp.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


@router.get("", response_model=CircuitBreakerStatus)
async def get_state(
    cb: CircuitBreaker = Depends(get_circuit_breaker),
    db: AsyncSession = Depends(get_db),
) -> CircuitBreakerStatus:
    tripped_at: datetime | None = None
    if cb.is_tripped:
        rec = await _last_trip_record(db)
        tripped_at = rec.timestamp if rec else None

    return CircuitBreakerStatus(
        is_tripped=cb.is_tripped,
        trigger=cb.trip_reason.value if cb.trip_reason else None,
        tripped_at=tripped_at,
        reason=cb.trip_reason.value if cb.trip_reason else None,
    )


@router.post("/trip", response_model=CircuitBreakerStatus)
async def trip(
    body: TripBody = TripBody(),
    cb: CircuitBreaker = Depends(get_circuit_breaker),
    db: AsyncSession = Depends(get_db),
) -> CircuitBreakerStatus:
    await cb.manual_trip()
    rec = await _last_trip_record(db)
    return CircuitBreakerStatus(
        is_tripped=cb.is_tripped,
        trigger=cb.trip_reason.value if cb.trip_reason else None,
        tripped_at=rec.timestamp if rec else None,
        reason=body.reason or (cb.trip_reason.value if cb.trip_reason else None),
    )


@router.post("/reset", response_model=CircuitBreakerStatus)
async def reset(
    cb: CircuitBreaker = Depends(get_circuit_breaker),
) -> CircuitBreakerStatus:
    cb.reset()
    return CircuitBreakerStatus(is_tripped=False)
