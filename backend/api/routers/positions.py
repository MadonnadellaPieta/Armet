"""
Positions router — placeholder (live data sourced from broker at runtime).
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/positions", tags=["positions"])


@router.get("")
async def list_positions() -> dict:
    return {
        "positions": [],
        "note": "live positions sourced from broker at runtime",
    }
