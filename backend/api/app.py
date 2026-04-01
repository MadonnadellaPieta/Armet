"""
FastAPI application factory.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.database.db import init_db


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        await init_db()
        yield

    app = FastAPI(
        title="Armet Trading",
        version="1.0.0",
        description="Semi-automated futures trading signal system",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health", tags=["system"])
    async def health() -> dict:
        return {"status": "ok"}

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

    # Serve built React frontend (present after `npm run build`)
    frontend_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
    if frontend_dist.is_dir():
        app.mount(
            "/",
            StaticFiles(directory=str(frontend_dist), html=True),
            name="static",
        )

    return app
