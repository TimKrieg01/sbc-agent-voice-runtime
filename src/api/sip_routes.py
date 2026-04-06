from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.core.db import fetch_effective_source_cidrs_from_db, has_database_config
from src.services.sip.session_manager import sip_session_manager

router = APIRouter(prefix="/internal/sip", tags=["sip-internal"])


class StartSipSessionRequest(BaseModel):
    session_id: str = Field(min_length=1)
    tenant_id: str = "default"
    stt_engine: str = "azure"
    languages: list[str] = Field(default_factory=lambda: ["en-US"])


class StopSipSessionRequest(BaseModel):
    session_id: str = Field(min_length=1)


@router.post("/session/start")
async def start_sip_session(req: StartSipSessionRequest):
    return await sip_session_manager.start_session(
        session_id=req.session_id,
        tenant_id=req.tenant_id,
        stt_engine=req.stt_engine,
        languages=req.languages,
    )


@router.post("/session/stop")
async def stop_sip_session(req: StopSipSessionRequest):
    stopped = await sip_session_manager.stop_session(req.session_id)
    if not stopped:
        raise HTTPException(status_code=404, detail=f"session_id '{req.session_id}' not found")
    return {"session_id": req.session_id, "stopped": True}


@router.get("/session")
async def list_sip_sessions():
    return {"sessions": await sip_session_manager.snapshot()}


@router.get("/config/source-cidrs")
async def get_effective_source_cidrs():
    if not has_database_config():
        return {"source": "none", "cidrs": []}
    return {
        "source": "db",
        "cidrs": fetch_effective_source_cidrs_from_db(),
    }
