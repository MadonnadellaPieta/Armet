"""
Async SQLAlchemy engine and session factory.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.config.settings import DATABASE_URL


def _ensure_db_dir(url: str) -> None:
    """Create the parent directory for a SQLite database file if needed."""
    if url.startswith("sqlite"):
        # Extract the file path from the URL (after "///")
        parts = url.split("///", 1)
        if len(parts) == 2:
            db_path = Path(parts[1])
            db_path.parent.mkdir(parents=True, exist_ok=True)


_ensure_db_dir(DATABASE_URL)

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
)

async_session = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    """Create all tables. Used for initial setup and tests."""
    from backend.database.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_db() -> None:
    """Drop all tables. Used in tests only."""
    from backend.database.models import Base

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
