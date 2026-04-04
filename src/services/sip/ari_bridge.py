from __future__ import annotations

import asyncio
import json
import logging
import os
from urllib import error, parse, request

import websockets

logger = logging.getLogger(__name__)


ASTERISK_ARI_BASE = os.getenv("ASTERISK_ARI_BASE", "http://127.0.0.1:8088/ari")
ASTERISK_ARI_WS = os.getenv("ASTERISK_ARI_WS", "ws://127.0.0.1:8088/ari/events")
ASTERISK_ARI_USER = os.getenv("ASTERISK_ARI_USER", "agentic")
ASTERISK_ARI_PASS = os.getenv("ASTERISK_ARI_PASS", "agentic-secret")
ASTERISK_ARI_APP = os.getenv("ASTERISK_ARI_APP", "agentic")
PYTHON_APP_BASE = os.getenv("PYTHON_APP_BASE", "http://127.0.0.1:8000")
SIP_TENANT_ID = os.getenv("SIP_TENANT_ID", "default")
SIP_STT_ENGINE = os.getenv("SIP_STT_ENGINE", "azure")
SIP_LANGUAGES = [x.strip() for x in os.getenv("SIP_LANGUAGES", "en-US").split(",") if x.strip()]

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _json_http(method: str, url: str, payload: dict | None = None) -> dict:
    body = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")

    req = request.Request(url=url, method=method, headers=headers, data=body)
    try:
        with request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            if not raw:
                return {}
            return json.loads(raw)
    except error.HTTPError as exc:
        err_body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {exc.code} for {url}: {err_body}") from exc


def _ari_url(path: str, query: dict | None = None) -> str:
    params = query.copy() if query else {}
    params["api_key"] = f"{ASTERISK_ARI_USER}:{ASTERISK_ARI_PASS}"
    return f"{ASTERISK_ARI_BASE}{path}?{parse.urlencode(params)}"


class AriBridgeWorker:
    def __init__(self):
        self.calls: dict[str, dict] = {}

    def _start_media_session(self, session_id: str) -> dict:
        return _json_http(
            "POST",
            f"{PYTHON_APP_BASE}/internal/sip/session/start",
            {
                "session_id": session_id,
                "tenant_id": SIP_TENANT_ID,
                "stt_engine": SIP_STT_ENGINE,
                "languages": SIP_LANGUAGES,
            },
        )

    def _stop_media_session(self, session_id: str) -> None:
        try:
            _json_http("POST", f"{PYTHON_APP_BASE}/internal/sip/session/stop", {"session_id": session_id})
        except Exception as exc:
            logger.warning("Failed to stop SIP media session %s: %s", session_id, exc)

    def _ari_post(self, path: str, query: dict | None = None, payload: dict | None = None) -> dict:
        return _json_http("POST", _ari_url(path, query), payload)

    def _ari_delete(self, path: str, query: dict | None = None) -> None:
        _json_http("DELETE", _ari_url(path, query))

    async def handle_stasis_start(self, channel: dict) -> None:
        channel_id = channel.get("id")
        channel_name = channel.get("name", "")

        # externalMedia channels also emit StasisStart. Skip those.
        if channel_name.startswith("UnicastRTP/"):
            return
        if not channel_id:
            return
        if channel_id in self.calls:
            return

        logger.info("Inbound SIP channel entered Stasis: id=%s name=%s", channel_id, channel_name)

        session_id = channel_id
        media_session = self._start_media_session(session_id)
        media_host = media_session.get("media_host", "127.0.0.1")
        media_port = media_session["media_port"]

        bridge = self._ari_post("/bridges", {"type": "mixing"})
        bridge_id = bridge["id"]
        self._ari_post(f"/bridges/{bridge_id}/addChannel", {"channel": channel_id})

        external = self._ari_post(
            "/channels/externalMedia",
            {
                "app": ASTERISK_ARI_APP,
                "external_host": f"{media_host}:{media_port}",
                "format": "ulaw",
            },
        )
        external_id = external["id"]
        self._ari_post(f"/bridges/{bridge_id}/addChannel", {"channel": external_id})

        self.calls[channel_id] = {
            "session_id": session_id,
            "bridge_id": bridge_id,
            "external_id": external_id,
        }
        logger.info(
            "Call bridged: channel=%s bridge=%s external=%s media=%s:%s",
            channel_id,
            bridge_id,
            external_id,
            media_host,
            media_port,
        )

    async def handle_stasis_end(self, channel: dict) -> None:
        channel_id = channel.get("id")
        if not channel_id:
            return
        call = self.calls.pop(channel_id, None)
        if not call:
            return

        logger.info("Inbound SIP channel left Stasis: id=%s", channel_id)

        try:
            self._ari_delete(f"/channels/{call['external_id']}")
        except Exception as exc:
            logger.warning("Failed to delete external channel %s: %s", call["external_id"], exc)

        try:
            self._ari_delete(f"/bridges/{call['bridge_id']}")
        except Exception as exc:
            logger.warning("Failed to delete bridge %s: %s", call["bridge_id"], exc)

        self._stop_media_session(call["session_id"])

    async def run(self):
        ws_query = parse.urlencode(
            {
                "app": ASTERISK_ARI_APP,
                "api_key": f"{ASTERISK_ARI_USER}:{ASTERISK_ARI_PASS}",
            }
        )
        ws_url = f"{ASTERISK_ARI_WS}?{ws_query}"

        logger.info("Connecting to ARI events: %s", ws_url)
        async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
            logger.info("Connected to ARI event stream.")
            async for raw in ws:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                event_type = event.get("type")
                channel = event.get("channel", {})
                if event_type == "StasisStart":
                    await self.handle_stasis_start(channel)
                elif event_type == "StasisEnd":
                    await self.handle_stasis_end(channel)


async def _main():
    worker = AriBridgeWorker()
    while True:
        try:
            await worker.run()
        except Exception as exc:
            logger.error("ARI bridge worker crashed: %s", exc, exc_info=True)
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(_main())
