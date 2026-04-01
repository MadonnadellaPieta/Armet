"""
Signals router — read-only access to signal records persisted by the engine.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db
from backend.database.models import SignalRecord

router = APIRouter(prefix="/signals", tags=["signals"])


class SignalOut(BaseModel):
    id: str
    timestamp: datetime
    instrument: str
    strategy_name: str
    direction: str
    entry_price: float
    stop_loss_price: float
    take_profit_price: float
    rr_ratio: float
    confidence_score: float
    confluence_factors: dict | None = None
    indicator_state: dict | None = None
    expiry_seconds: float = 10.0
    decision: str | None = None
    decision_timestamp: datetime | None = None

    model_config = {"from_attributes": False}

    @classmethod
    def from_record(cls, rec: SignalRecord) -> "SignalOut":
        return cls(
            id=rec.id,
            timestamp=rec.timestamp,
            instrument=rec.instrument,
            strategy_name=rec.strategy_name,
            direction=rec.direction,
            entry_price=rec.entry_price,
            stop_loss_price=rec.sl_price,
            take_profit_price=rec.tp_price,
            rr_ratio=rec.rr_ratio,
            confidence_score=rec.confidence_score,
            confluence_factors=rec.confluence_factors,
            indicator_state=rec.indicator_state,
            decision=rec.decision,
            decision_timestamp=rec.decision_timestamp,
        )


@router.get("", response_model=list[SignalOut])
async def list_signals(
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
) -> list[SignalOut]:
    result = await db.execute(
        select(SignalRecord)
        .order_by(SignalRecord.timestamp.desc())
        .limit(limit)
        .offset(offset)
    )
    return [SignalOut.from_record(r) for r in result.scalars().all()]


@router.get("/{signal_id}", response_model=SignalOut)
async def get_signal(
    signal_id: str,
    db: AsyncSession = Depends(get_db),
) -> SignalOut:
    row = await db.get(SignalRecord, signal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Signal not found")
    return SignalOut.from_record(row)
