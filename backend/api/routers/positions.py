"""
Positions router — live positions from the broker client.
In paper mode, PaperBrokerClient manages in-memory positions.  Without a
running engine this endpoint returns an empty list; a future version can
wire in the broker singleton via app state.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(prefix="/positions", tags=["positions"])


class PositionOut(BaseModel):
    instrument: str
    quantity: int
    avg_entry_price: float
    unrealized_pnl: float
    side: str  # LONG | SHORT


@router.get("", response_model=list[PositionOut])
async def list_positions() -> list[PositionOut]:
    # Positions are managed in-memory by the broker client during a live run.
    # The API returns an empty list when no engine is attached.
    return []
