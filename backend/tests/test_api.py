"""
Tests for the FastAPI backend (Task 14).

Uses httpx.AsyncClient with ASGITransport — no real server needed.
Dependencies get_db and get_circuit_breaker are overridden via
app.dependency_overrides to use an in-memory SQLite database and an
isolated CircuitBreaker instance.
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
from backend.database.models import (
    AccountSnapshotRecord,
    Base,
    CircuitBreakerEventRecord,
    SignalRecord,
)
from backend.services.circuit_breaker import CircuitBreaker

TEST_DB_URL = "sqlite+aiosqlite://"  # in-memory


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
    return async_sessionmaker(
        test_engine, class_=AsyncSession, expire_on_commit=False
    )


@pytest_asyncio.fixture()
async def client(
    test_session_factory,
) -> AsyncGenerator[AsyncClient, None]:
    app = create_app()

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with test_session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()

    cb_instance = CircuitBreaker(session_factory=test_session_factory)

    def override_get_circuit_breaker() -> CircuitBreaker:
        return cb_instance

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_circuit_breaker] = override_get_circuit_breaker

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Helpers
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


def _make_snapshot(phase: str = "EVAL") -> AccountSnapshotRecord:
    return AccountSnapshotRecord(
        timestamp=datetime.utcnow(),
        balance=100_000.0,
        eod_threshold=97_000.0,
        daily_pnl=250.0,
        phase=phase,
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class TestHealth:
    async def test_health_ok(self, client: AsyncClient) -> None:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


class TestSignals:
    async def test_list_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/signals")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_list_returns_signals(
        self, client: AsyncClient, test_session_factory
    ) -> None:
        async with test_session_factory() as sess:
            sess.add(_make_signal("ES"))
            sess.add(_make_signal("NQ"))
            await sess.commit()

        resp = await client.get("/api/v1/signals")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        instruments = {s["instrument"] for s in data}
        assert instruments == {"ES", "NQ"}

    async def test_signal_has_mapped_fields(
        self, client: AsyncClient, test_session_factory
    ) -> None:
        """DB uses sl_price/tp_price; API must expose stop_loss_price/take_profit_price."""
        async with test_session_factory() as sess:
            sess.add(_make_signal())
            await sess.commit()

        resp = await client.get("/api/v1/signals")
        sig = resp.json()[0]
        assert "stop_loss_price" in sig
        assert "take_profit_price" in sig
        assert sig["stop_loss_price"] == 4998.0
        assert sig["take_profit_price"] == 5004.0

    async def test_get_signal_by_id(
        self, client: AsyncClient, test_session_factory
    ) -> None:
        rec = _make_signal()
        async with test_session_factory() as sess:
            sess.add(rec)
            await sess.commit()

        resp = await client.get(f"/api/v1/signals/{rec.id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == rec.id

    async def test_get_signal_not_found(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/signals/doesnotexist")
        assert resp.status_code == 404

    async def test_list_respects_limit(
        self, client: AsyncClient, test_session_factory
    ) -> None:
        async with test_session_factory() as sess:
            for _ in range(5):
                sess.add(_make_signal())
            await sess.commit()

        resp = await client.get("/api/v1/signals?limit=2")
        assert resp.status_code == 200
        assert len(resp.json()) == 2


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


class TestAccount:
    async def test_account_no_snapshot(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/account")
        assert resp.status_code == 200
        assert resp.json() is None

    async def test_account_returns_last_snapshot(
        self, client: AsyncClient, test_session_factory
    ) -> None:
        async with test_session_factory() as sess:
            sess.add(_make_snapshot())
            await sess.commit()

        resp = await client.get("/api/v1/account")
        assert resp.status_code == 200
        data = resp.json()
        assert data["balance"] == 100_000.0
        assert data["daily_pnl"] == 250.0
        assert data["phase"] == "EVAL"


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------


class TestPositions:
    async def test_positions_empty(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/positions")
        assert resp.status_code == 200
        assert resp.json() == []


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


class TestStrategies:
    async def test_get_strategies(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/strategies")
        assert resp.status_code == 200
        data = resp.json()
        assert "strategies" in data
        assert "filters" in data
        strategy_names = {s["name"] for s in data["strategies"]}
        assert strategy_names == {"vwap_reversion", "ema_crossover", "opening_range"}

    async def test_strategy_has_enabled_and_params(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/strategies")
        for s in resp.json()["strategies"]:
            assert "enabled" in s
            assert "params" in s

    async def test_enable_strategy(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/strategies/vwap_reversion/enable")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

    async def test_disable_strategy(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/strategies/ema_crossover/disable")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    async def test_unknown_strategy_404(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/strategies/unknown/enable")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    async def test_default_state_not_tripped(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/circuit-breaker")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_tripped"] is False
        assert data["trigger"] is None

    async def test_manual_trip(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/circuit-breaker/trip", json={"reason": "test trip"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_tripped"] is True
        assert data["trigger"] == "MANUAL"

    async def test_reset_after_trip(self, client: AsyncClient) -> None:
        await client.post("/api/v1/circuit-breaker/trip", json={"reason": "x"})
        resp = await client.post("/api/v1/circuit-breaker/reset")
        assert resp.status_code == 200
        assert resp.json()["is_tripped"] is False

    async def test_state_reflects_trip(self, client: AsyncClient) -> None:
        await client.post("/api/v1/circuit-breaker/trip", json={"reason": "x"})
        resp = await client.get("/api/v1/circuit-breaker")
        assert resp.json()["is_tripped"] is True

    async def test_trip_persists_db_record(
        self, client: AsyncClient, test_session_factory
    ) -> None:
        await client.post("/api/v1/circuit-breaker/trip", json={"reason": "db test"})
        async with test_session_factory() as sess:
            from sqlalchemy import select

            result = await sess.execute(select(CircuitBreakerEventRecord))
            records = result.scalars().all()
        assert len(records) == 1
        assert records[0].trigger_type == "MANUAL"
