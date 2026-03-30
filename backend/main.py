"""
FastAPI application entry point.

Stub — will be fleshed out in Task 14 (FastAPI backend).
"""

from fastapi import FastAPI

app = FastAPI(
    title="Futures Trading Signal System",
    version="0.1.0",
    description="Semi-automated futures trading signals with Apex rule enforcement",
)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
