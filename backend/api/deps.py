"""
Shared FastAPI dependencies.
"""

from __future__ import annotations

from pathlib import Path
from typing import AsyncGenerator

import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.db import async_session
from backend.services.circuit_breaker import CircuitBreakerService

# ---------------------------------------------------------------------------
# Database session
# ---------------------------------------------------------------------------


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield an AsyncSession, rolling back on error."""
    async with async_session() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


# ---------------------------------------------------------------------------
# Circuit-breaker singleton
# ---------------------------------------------------------------------------

def _load_cb_thresholds() -> dict:
    config_path = Path(__file__).resolve().parent.parent / "config" / "default_config.yaml"
    with config_path.open() as f:
        cfg = yaml.safe_load(f)
    return cfg.get("circuit_breakers", {})


_circuit_breaker_instance: CircuitBreakerService | None = None


def get_circuit_breaker() -> CircuitBreakerService:
    """Return the module-level CircuitBreakerService singleton."""
    global _circuit_breaker_instance
    if _circuit_breaker_instance is None:
        _circuit_breaker_instance = CircuitBreakerService(
            session_factory=async_session,
            thresholds=_load_cb_thresholds(),
        )
    return _circuit_breaker_instance
