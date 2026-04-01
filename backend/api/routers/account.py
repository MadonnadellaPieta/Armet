"""
Account router — placeholder (live data sourced from broker at runtime).
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/account", tags=["account"])


@router.get("")
async def get_account() -> dict:
    return {
        "account": None,
        "note": "live account data sourced from broker at runtime",
    }
