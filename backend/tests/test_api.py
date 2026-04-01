"""
Tests for the FastAPI backend (Task 14).

Uses httpx.AsyncClient with ASGITransport — no real server needed.
Dependencies get_db and get_circuit_breaker are overridden via
app.dependency_overrides to use an in-memory SQLite database and an
isolated CircuitBreakerService instance.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.api.app import create_app
from backend.api.deps import get_circuit_breaker, get_db
from backend.database.models import Base, SignalRecord
from backend.services.circuit_breaker import CircuitBreakerService

# ---------------------------------------------------------------------------
# In-memory database fixtures
# ---------------------------------------------------------------------------

TEST_DB_URL = "sqlite+aiosqlite://"  # in-memory


@pytest_asyncio.fixture()
async def test_engine():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture()
async def test_session_factory(test_engine):
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture()
async def client(test_session_factory) -> AsyncGenerator[AsyncClient, None]:
    app = create_app()

    # Override get_db
    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with test_session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    # Override get_circuit_breaker with isolated instance using in-memory DB
    cb_instance = CircuitBreakerService(
        session_factory=test_session_factory,
        thresholds={"max_consecutive_losses": 3, "max_daily_loss": 1500, "max_daily_profit": 5000},
    )

    def override_get_circuit_breaker() -> CircuitBreakerService:
        return cb_instance

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_circuit_breaker] = override_get_circuit_breaker

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_signal(instrument: str = "ES") -> SignalRecord:
    return SignalRecord(
        id=uuid.uuid4().hex,
        timestamp=datetime.utcnow(),
        instrument=instrument,
        strategy_name="vwap_reversion",
        direction="LONG",
        entry_price=5000.0,
        sl_price=4998.0,
        tp_price=5004.0,
        rr_ratio=2.0,
        confidence_score=0.75,
        decision=None,
        decision_timestamp=None,
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


async def test_health(client: AsyncClient) -> None:
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


async def test_signals_empty(client: AsyncClient) -> None:
    r = await client.get("/api/v1/signals")
    assert r.status_code == 200
    assert r.json() == []


async def test_signals_pagination(
    client: AsyncClient, test_session_factory
) -> None:
    # Insert 5 records
    async with test_session_factory() as session:
        for _ in range(5):
            session.add(_make_signal())
        await session.commit()

    r = await client.get("/api/v1/signals?limit=2&offset=0")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2


async def test_signal_not_found(client: AsyncClient) -> None:
    r = await client.get("/api/v1/signals/doesnotexist")
    assert r.status_code == 404


async def test_signal_found(
    client: AsyncClient, test_session_factory
) -> None:
    sig = _make_signal()
    async with test_session_factory() as session:
        session.add(sig)
        await session.commit()

    r = await client.get(f"/api/v1/signals/{sig.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == sig.id
    assert body["instrument"] == sig.instrument
    assert body["direction"] == "LONG"
    assert body["entry_price"] == sig.entry_price
    assert body["rr_ratio"] == sig.rr_ratio
    assert body["confidence_score"] == sig.confidence_score
    assert body["decision"] is None
    assert body["decision_timestamp"] is None


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


async def test_positions(client: AsyncClient) -> None:
    r = await client.get("/api/v1/positions")
    assert r.status_code == 200
    body = r.json()
    assert body["positions"] == []
    assert "note" in body


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


async def test_account(client: AsyncClient) -> None:
    r = await client.get("/api/v1/account")
    assert r.status_code == 200
    body = r.json()
    assert body["account"] is None
    assert "note" in body


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


async def test_strategies(client: AsyncClient) -> None:
    r = await client.get("/api/v1/strategies")
    assert r.status_code == 200
    body = r.json()
    assert "vwap_reversion" in body
    assert "ema_crossover" in body
    assert "opening_range" in body


async def test_strategy_enable(client: AsyncClient) -> None:
    r = await client.post("/api/v1/strategies/vwap_reversion/enable")
    assert r.status_code == 200
    body = r.json()
    assert body["strategy"] == "vwap_reversion"
    assert body["enabled"] is True


async def test_strategy_disable(client: AsyncClient) -> None:
    r = await client.post("/api/v1/strategies/vwap_reversion/disable")
    assert r.status_code == 200
    body = r.json()
    assert body["strategy"] == "vwap_reversion"
    assert body["enabled"] is False


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


async def test_circuit_breaker_initial_state(client: AsyncClient) -> None:
    r = await client.get("/api/v1/circuit-breaker")
    assert r.status_code == 200
    body = r.json()
    assert body["is_tripped"] is False
    assert body["trigger"] is None


async def test_circuit_breaker_trip(client: AsyncClient) -> None:
    r = await client.post("/api/v1/circuit-breaker/trip", json={"reason": "manual test"})
    assert r.status_code == 200
    body = r.json()
    assert body["is_tripped"] is True
    assert body["trigger"] == "MANUAL"
    assert body["trigger_value"] == "manual test"
    assert body["tripped_at"] is not None


async def test_circuit_breaker_reset(client: AsyncClient) -> None:
    # Trip first
    await client.post("/api/v1/circuit-breaker/trip", json={"reason": "test"})
    # Then reset
    r = await client.post("/api/v1/circuit-breaker/reset", json={"reason": "all clear"})
    assert r.status_code == 200
    body = r.json()
    assert body["is_tripped"] is False
    assert body["trigger"] is None
    assert body["tripped_at"] is None
