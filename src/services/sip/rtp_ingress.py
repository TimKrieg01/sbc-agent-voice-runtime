from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)


def _extract_rtp_payload(packet: bytes) -> bytes:
    """
    Parse a minimal RTP packet and return payload bytes.
    Assumes RTP over UDP with no SRTP for initial deployment.
    """
    if len(packet) < 12:
        return b""

    b0 = packet[0]
    version = b0 >> 6
    if version != 2:
        return b""

    cc = b0 & 0x0F
    has_extension = (b0 & 0x10) != 0
    header_len = 12 + (cc * 4)
    if len(packet) < header_len:
        return b""

    if has_extension:
        if len(packet) < header_len + 4:
            return b""
        ext_len_words = int.from_bytes(packet[header_len + 2 : header_len + 4], "big")
        header_len += 4 + (ext_len_words * 4)
        if len(packet) < header_len:
            return b""

    return packet[header_len:]


class _RtpDatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self, session_id: str, on_ulaw_payload: Callable[[str], None]):
        self.session_id = session_id
        self.on_ulaw_payload = on_ulaw_payload

    def datagram_received(self, data: bytes, addr):
        payload = _extract_rtp_payload(data)
        if not payload:
            return
        payload_b64 = base64.b64encode(payload).decode("ascii")
        self.on_ulaw_payload(payload_b64)

    def error_received(self, exc: Exception):
        logger.warning("[SIP RTP %s] Datagram error: %s", self.session_id, exc)


@dataclass
class RtpIngressHandle:
    session_id: str
    port: int
    transport: asyncio.DatagramTransport

    def close(self) -> None:
        self.transport.close()


async def start_rtp_ingress(
    session_id: str,
    on_ulaw_payload: Callable[[str], None],
    host: str = "127.0.0.1",
    port: int = 0,
) -> RtpIngressHandle:
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _RtpDatagramProtocol(session_id=session_id, on_ulaw_payload=on_ulaw_payload),
        local_addr=(host, port),
    )
    sockname = transport.get_extra_info("sockname")
    bound_port = int(sockname[1])
    logger.info("[SIP RTP %s] Listening on %s:%s", session_id, host, bound_port)
    return RtpIngressHandle(session_id=session_id, port=bound_port, transport=transport)
