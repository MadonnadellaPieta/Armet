"""
Account router — serves last persisted account snapshot from the DB.
In a running system the OrderManager writes AccountSnapshotRecord after each
trade close; this endpoint reflects that latest snapshot.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import get_db
from backend.database.models import AccountSnapshotRecord

router = APIRouter(prefix="/account", tags=["account"])


class AccountInfoOut(BaseModel):
    balance: float
    equity: float
    daily_pnl: float
    eod_threshold: float
    phase: str


@router.get("", response_model=AccountInfoOut | None)
async def get_account(
    db: AsyncSession = Depends(get_db),
) -> AccountInfoOut | None:
    result = await db.execute(
        select(AccountSnapshotRecord)
        .order_by(AccountSnapshotRecord.timestamp.desc())
        .limit(1)
    )
    snap = result.scalar_one_or_none()
    if snap is None:
        return None
    return AccountInfoOut(
        balance=snap.balance,
        equity=snap.balance,
        daily_pnl=snap.daily_pnl,
        eod_threshold=snap.eod_threshold,
        phase=snap.phase,
    )
