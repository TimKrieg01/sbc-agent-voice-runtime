from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from urllib import error, parse, request

import websockets

logger = logging.getLogger(__name__)


ASTERISK_ARI_BASE = os.getenv("ASTERISK_ARI_BASE", "http://127.0.0.1:8088/ari")
ASTERISK_ARI_WS = os.getenv("ASTERISK_ARI_WS", "ws://127.0.0.1:8088/ari/events")
ASTERISK_ARI_USER = os.getenv("ASTERISK_ARI_USER", "agentic")
ASTERISK_ARI_PASS = os.getenv("ASTERISK_ARI_PASS", "agentic-secret")
ASTERISK_ARI_APP = os.getenv("ASTERISK_ARI_APP", "agentic")
PYTHON_APP_BASE = os.getenv("PYTHON_APP_BASE", "http://127.0.0.1:8000")

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


def _extract_sip_host_from_to_header(to_header: str | None) -> str:
    """
    Parse host from SIP To header.
    Example:
    - "<sip:+123@customer1.sip.agentvoiceruntime.com>;tag=abc" -> "customer1.sip.agentvoiceruntime.com"
    """
    if not to_header:
        return ""

    compact = to_header.strip()
    match = re.search(r"sips?:([^;>\s]+)", compact, re.IGNORECASE)
    if not match:
        return ""
    uri_part = match.group(1).strip()
    if not uri_part:
        return ""

    # sip URI userinfo is optional; when present use host[:port] after "@"
    host_port = uri_part.rsplit("@", 1)[-1].strip()
    if not host_port:
        return ""

    # IPv6 literal host: [2001:db8::1]:5061
    if host_port.startswith("["):
        end = host_port.find("]")
        if end == -1:
            return ""
        return host_port[1:end].strip().lower()

    # Regular host[:port]
    host = host_port.split(":", 1)[0].strip().lower()
    return host


def _safe_arg(args: list[str], index: int) -> str:
    if index >= len(args):
        return ""
    return (args[index] or "").strip()


def _default_session_profile() -> tuple[str, str, list[str]]:
    tenant_id = (os.getenv("SIP_DEFAULT_TENANT_ID") or "default").strip() or "default"
    stt_engine = (os.getenv("SIP_DEFAULT_STT_ENGINE") or "azure").strip() or "azure"
    languages_csv = (os.getenv("SIP_DEFAULT_LANGUAGES") or "en-US").strip()
    languages = [x.strip() for x in languages_csv.split(",") if x.strip()] or ["en-US"]
    return tenant_id, stt_engine, languages


class AriBridgeWorker:
    def __init__(self):
        self.calls: dict[str, dict] = {}

    def _start_media_session(
        self,
        session_id: str,
        tenant_id: str,
        stt_engine: str,
        languages: list[str],
    ) -> dict:
        return _json_http(
            "POST",
            f"{PYTHON_APP_BASE}/internal/sip/session/start",
            {
                "session_id": session_id,
                "tenant_id": tenant_id,
                "stt_engine": stt_engine,
                "languages": languages,
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

    async def handle_stasis_start(self, event: dict) -> None:
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        channel_name = channel.get("name", "")
        args = [str(x) for x in (event.get("args") or [])]

        # externalMedia channels also emit StasisStart. Skip those.
        if channel_name.startswith("UnicastRTP/"):
            return
        if not channel_id:
            return
        if channel_id in self.calls:
            return

        logger.info("Inbound SIP channel entered Stasis: id=%s name=%s", channel_id, channel_name)

        session_id = channel_id
        called_number = _safe_arg(args, 0) or channel.get("dialplan", {}).get("exten", "")
        to_header = _safe_arg(args, 1)
        auth_user = _safe_arg(args, 2)
        trunk_id_arg = _safe_arg(args, 3)
        backend_url_arg = _safe_arg(args, 4)
        route_id_arg = _safe_arg(args, 5)
        tenant_id_arg = _safe_arg(args, 6)
        stt_engine_arg = _safe_arg(args, 7)
        languages_arg = _safe_arg(args, 8)
        ingress_host = _extract_sip_host_from_to_header(to_header)

        # Dialplan/DB precheck must provide at least trunk and backend route.
        # If these are missing, refuse channel startup to avoid blind call acceptance.
        if not trunk_id_arg or not backend_url_arg:
            logger.error(
                "Missing route metadata from dialplan precheck for channel=%s trunk_id='%s' backend_url='%s'; dropping call.",
                channel_id,
                trunk_id_arg,
                backend_url_arg,
            )
            try:
                self._ari_delete(f"/channels/{channel_id}")
            except Exception as drop_exc:
                logger.warning("Failed to hang up channel %s after missing-route metadata: %s", channel_id, drop_exc)
            return

        default_tenant_id, default_stt_engine, default_languages = _default_session_profile()
        tenant_id = tenant_id_arg or default_tenant_id
        stt_engine = stt_engine_arg or default_stt_engine
        languages = [x.strip() for x in languages_arg.split(",") if x.strip()] if languages_arg else default_languages

        logger.info(
            "Inbound route approved: channel=%s tenant_id=%s trunk_id=%s route_id=%s backend_url=%s host='%s' called='%s' auth_user='%s'",
            channel_id,
            tenant_id,
            trunk_id_arg,
            route_id_arg,
            backend_url_arg,
            ingress_host,
            called_number,
            auth_user,
        )

        media_session = self._start_media_session(
            session_id=session_id,
            tenant_id=tenant_id,
            stt_engine=stt_engine,
            languages=languages,
        )
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
            "tenant_id": tenant_id,
            "trunk_id": trunk_id_arg,
            "route_id": route_id_arg,
            "backend_url": backend_url_arg,
            "ingress_host": ingress_host,
            "called_number": called_number,
        }
        logger.info(
            "Call bridged: channel=%s tenant=%s bridge=%s external=%s media=%s:%s",
            channel_id,
            tenant_id,
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

    def _log_hangup_event(self, event: dict) -> None:
        channel = event.get("channel", {}) or {}
        channel_id = channel.get("id")
        if not channel_id:
            return

        # Try to correlate either by inbound channel id or by known externalMedia id.
        related = None
        if channel_id in self.calls:
            related = self.calls[channel_id]
        else:
            for call in self.calls.values():
                if call.get("external_id") == channel_id:
                    related = call
                    break

        cause = event.get("cause") or channel.get("cause")
        cause_txt = event.get("cause_txt") or channel.get("cause_txt")
        state = channel.get("state")
        name = channel.get("name")
        dialplan = channel.get("dialplan", {}) or {}
        exten = dialplan.get("exten")

        if related:
            logger.warning(
                "Hangup signal: channel=%s name=%s state=%s cause=%s cause_txt=%s "
                "linked_session=%s linked_tenant=%s linked_bridge=%s exten=%s",
                channel_id,
                name,
                state,
                cause,
                cause_txt,
                related.get("session_id"),
                related.get("tenant_id"),
                related.get("bridge_id"),
                exten,
            )
        else:
            logger.warning(
                "Hangup signal: channel=%s name=%s state=%s cause=%s cause_txt=%s exten=%s",
                channel_id,
                name,
                state,
                cause,
                cause_txt,
                exten,
            )

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
                if event_type == "StasisStart":
                    await self.handle_stasis_start(event)
                elif event_type == "StasisEnd":
                    await self.handle_stasis_end(event.get("channel", {}))
                elif event_type in {"ChannelHangupRequest", "ChannelDestroyed"}:
                    self._log_hangup_event(event)


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
