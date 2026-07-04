"""Liveness / readiness probes (no auth)."""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict:
    """Liveness probe reporting the process is up.

    Returns:
        A constant ``{"status": "ok"}`` body.
    """
    return {"status": "ok"}


@router.get("/readyz")
async def readyz() -> dict:
    """Readiness probe reporting the app is ready to serve.

    Returns:
        A constant ``{"status": "ready"}`` body.
    """
    return {"status": "ready"}
