"""
Shared pytest fixtures for all backend tests.

Provides:
  - db_engine  : async in-memory SQLite engine with all tables created
  - db_session : scoped session (auto-rollback after each test)
  - paper_broker: connected PaperBrokerClient
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database.models import Base


# ---------------------------------------------------------------------------
# Database fixtures (used by test_database.py and test_integration.py)
# ---------------------------------------------------------------------------


@pytest.fixture
async def db_engine():
    """In-memory SQLite engine with schema pre-created."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db_session(db_engine):
    """Async session that is rolled back after each test."""
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as sess:
        yield sess
        await sess.rollback()


@pytest.fixture
async def db_session_factory(db_engine):
    """Session factory bound to the in-memory engine."""
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Broker fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def paper_broker():
    """Connected PaperBrokerClient with default ES instrument settings."""
    from backend.core.paper_broker_client import PaperBrokerClient

    broker = PaperBrokerClient(
        initial_balance=110_000.0,
        slippage_ticks=0.0,
        tick_size=0.25,
        tick_value=12.50,
    )
    await broker.connect()
    yield broker
    await broker.disconnect()
