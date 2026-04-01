"""
Signals router — read-only access to historical signal records.
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
    sl_price: float
    tp_price: float
    rr_ratio: float
    confidence_score: float
    decision: str | None
    decision_timestamp: datetime | None

    model_config = {"from_attributes": True}


@router.get("", response_model=list[SignalOut])
async def list_signals(
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
) -> list[SignalRecord]:
    result = await db.execute(
        select(SignalRecord)
        .order_by(SignalRecord.timestamp.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all())


@router.get("/{signal_id}", response_model=SignalOut)
async def get_signal(
    signal_id: str,
    db: AsyncSession = Depends(get_db),
) -> SignalRecord:
    row = await db.get(SignalRecord, signal_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Signal not found")
    return row
