"""
Strategies router — read config and enable/disable stubs.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/strategies", tags=["strategies"])

_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "default_config.yaml"


def _load_strategies() -> dict:
    with _CONFIG_PATH.open() as f:
        cfg = yaml.safe_load(f)
    return cfg.get("strategies", {})


@router.get("")
async def get_strategies() -> dict:
    return _load_strategies()


@router.post("/{strategy_name}/enable")
async def enable_strategy(strategy_name: str) -> dict:
    strategies = _load_strategies()
    if strategy_name not in strategies:
        raise HTTPException(status_code=404, detail=f"Strategy '{strategy_name}' not found")
    return {"strategy": strategy_name, "enabled": True}


@router.post("/{strategy_name}/disable")
async def disable_strategy(strategy_name: str) -> dict:
    strategies = _load_strategies()
    if strategy_name not in strategies:
        raise HTTPException(status_code=404, detail=f"Strategy '{strategy_name}' not found")
    return {"strategy": strategy_name, "enabled": False}
