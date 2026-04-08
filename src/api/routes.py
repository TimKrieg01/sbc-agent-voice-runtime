from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["service"])


@router.get("/healthz")
async def healthz():
    return {"status": "ok"}


@router.get("/")
async def root():
    return {"service": "agentic-sip-trunk", "status": "ready"}
