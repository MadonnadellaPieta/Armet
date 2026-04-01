"""
FastAPI application factory.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.database.db import init_db


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        await init_db()
        yield

    app = FastAPI(title="Armet Trading", version="1.0.0", lifespan=lifespan)

    # CORS — all origins for paper-mode dev convenience
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Health check
    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    # Routers
    from backend.api.routers import (
        account,
        circuit_breaker,
        positions,
        signals,
        strategies,
    )

    prefix = "/api/v1"
    app.include_router(signals.router, prefix=prefix)
    app.include_router(positions.router, prefix=prefix)
    app.include_router(account.router, prefix=prefix)
    app.include_router(strategies.router, prefix=prefix)
    app.include_router(circuit_breaker.router, prefix=prefix)

    return app
