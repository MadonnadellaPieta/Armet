"""
Strategies router — expose strategy and filter config from default_config.yaml.
Enable/disable mutations are in-memory only for the running process; a
production version would persist changes via ConfigHistoryRecord.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/strategies", tags=["strategies"])

_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent.parent / "config" / "default_config.yaml"
)

# In-memory enable/disable overrides for the current process.
_strategy_overrides: dict[str, bool] = {}
_filter_overrides: dict[str, bool] = {}


def _load_config() -> dict:
    with _CONFIG_PATH.open() as f:
        return yaml.safe_load(f)


class StrategyInfo(BaseModel):
    name: str
    enabled: bool
    params: dict


class FilterInfo(BaseModel):
    name: str
    enabled: bool
    weight: float


class StrategiesResponse(BaseModel):
    strategies: list[StrategyInfo]
    filters: list[FilterInfo]


@router.get("", response_model=StrategiesResponse)
async def get_strategies() -> StrategiesResponse:
    cfg = _load_config()
    strategies_cfg = cfg.get("strategies", {})
    filters_cfg = cfg.get("filters", {})

    strategies = []
    for name, params in strategies_cfg.items():
        params = dict(params or {})
        default_enabled = params.pop("enabled", True)
        enabled = _strategy_overrides.get(name, default_enabled)
        strategies.append(StrategyInfo(name=name, enabled=enabled, params=params))

    filters = []
    for name, params in filters_cfg.items():
        params = dict(params or {})
        default_enabled = params.pop("enabled", True)
        weight = params.pop("weight", 0.25)
        enabled = _filter_overrides.get(name, default_enabled)
        filters.append(FilterInfo(name=name, enabled=enabled, weight=weight))

    return StrategiesResponse(strategies=strategies, filters=filters)


@router.post("/{strategy_name}/enable")
async def enable_strategy(strategy_name: str) -> dict:
    cfg = _load_config()
    if strategy_name not in cfg.get("strategies", {}):
        raise HTTPException(
            status_code=404, detail=f"Strategy '{strategy_name}' not found"
        )
    _strategy_overrides[strategy_name] = True
    return {"strategy": strategy_name, "enabled": True}


@router.post("/{strategy_name}/disable")
async def disable_strategy(strategy_name: str) -> dict:
    cfg = _load_config()
    if strategy_name not in cfg.get("strategies", {}):
        raise HTTPException(
            status_code=404, detail=f"Strategy '{strategy_name}' not found"
        )
    _strategy_overrides[strategy_name] = False
    return {"strategy": strategy_name, "enabled": False}
