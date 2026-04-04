from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from src.services.connection.orchestrator import ConnectionOrchestrator
from src.services.sip.rtp_ingress import RtpIngressHandle, start_rtp_ingress

logger = logging.getLogger(__name__)


@dataclass
class SipSession:
    session_id: str
    tenant_id: str
    stt_engine: str
    languages: list[str]
    orchestrator: ConnectionOrchestrator
    ingress: RtpIngressHandle


class SipSessionManager:
    def __init__(self):
        self._sessions: dict[str, SipSession] = {}
        self._lock = asyncio.Lock()

    async def start_session(
        self,
        session_id: str,
        tenant_id: str = "default",
        stt_engine: str = "azure",
        languages: list[str] | None = None,
    ) -> dict:
        langs = languages or ["en-US"]
        loop = asyncio.get_running_loop()

        async with self._lock:
            existing = self._sessions.get(session_id)
            if existing:
                return {
                    "session_id": session_id,
                    "tenant_id": existing.tenant_id,
                    "media_host": "127.0.0.1",
                    "media_port": existing.ingress.port,
                    "already_running": True,
                }

            orchestrator = ConnectionOrchestrator(
                stream_sid=session_id,
                tenant_id=tenant_id,
                stt_engine_str=stt_engine,
                languages=langs,
                loop=loop,
                send_command_cb=None,
            )
            orchestrator.start()

            ingress = await start_rtp_ingress(
                session_id=session_id,
                on_ulaw_payload=orchestrator.process_media,
                host="127.0.0.1",
                port=0,
            )

            self._sessions[session_id] = SipSession(
                session_id=session_id,
                tenant_id=tenant_id,
                stt_engine=stt_engine,
                languages=langs,
                orchestrator=orchestrator,
                ingress=ingress,
            )

            logger.info(
                "[SIP Session %s] Started tenant=%s stt=%s port=%s",
                session_id,
                tenant_id,
                stt_engine,
                ingress.port,
            )

            return {
                "session_id": session_id,
                "tenant_id": tenant_id,
                "media_host": "127.0.0.1",
                "media_port": ingress.port,
                "already_running": False,
            }

    async def stop_session(self, session_id: str) -> bool:
        async with self._lock:
            session = self._sessions.pop(session_id, None)

        if not session:
            return False

        session.ingress.close()
        session.orchestrator.stop()
        logger.info("[SIP Session %s] Stopped", session_id)
        return True

    async def stop_all(self) -> None:
        async with self._lock:
            session_ids = list(self._sessions.keys())

        for session_id in session_ids:
            await self.stop_session(session_id)

    async def snapshot(self) -> list[dict]:
        async with self._lock:
            return [
                {
                    "session_id": s.session_id,
                    "tenant_id": s.tenant_id,
                    "stt_engine": s.stt_engine,
                    "languages": s.languages,
                    "media_port": s.ingress.port,
                }
                for s in self._sessions.values()
            ]


sip_session_manager = SipSessionManager()
