"""
Connection Orchestrator - Observation-Only Mode.

Receives Twilio media frames, decodes them once, and distributes the raw PCM
to both the Azure STT pipeline and the ProsodyEngine. All output is logged;
no turn-taking decisions are made by the orchestrator itself.
"""

from __future__ import annotations

import asyncio
import audioop
import base64
import json
import logging
import threading
import time
from pathlib import Path

from src.services.speech.prosody import ProsodyEngine
from src.services.speech.service import SpeechService, TranscriptionEngine

logger = logging.getLogger(__name__)

# How often (in seconds) to run prosody computation
PROSODY_INTERVAL = 0.15
CURVE_OUTPUT_DIR = Path("logs") / "turn_curves"


class ConnectionOrchestrator:
    def __init__(
        self,
        stream_sid: str,
        tenant_id: str,
        stt_engine_str: str,
        languages: list,
        loop: asyncio.AbstractEventLoop = None,
        send_command_cb: callable = None,
    ):
        self.stream_sid = stream_sid
        self.tenant_id = tenant_id
        self.loop = loop
        self.send_command_cb = send_command_cb

        try:
            engine_enum = TranscriptionEngine(stt_engine_str.lower())
        except ValueError:
            logger.warning(f"Unknown engine '{stt_engine_str}', defaulting to AZURE.")
            engine_enum = TranscriptionEngine.AZURE

        self.speech_service = SpeechService(engine=engine_enum, languages=languages)
        self.speech_service.stt.on_transcript(self._handle_transcript)

        self.prosody = ProsodyEngine(vad_aggressiveness=2)
        self._prosody_task = None

        self._session_start_monotonic: float | None = None
        self._session_start_wall: float | None = None

        self._curve_samples: list[dict] = []
        self._transcript_events: list[dict] = []
        self._latest_transcript_text: str = ""
        self._latest_transcript_is_final: bool = False
        self._last_score_sample: dict | None = None
        self._lock = threading.Lock()

    def start(self):
        logger.info(f"[Orchestrator {self.stream_sid}] Session starting (tenant={self.tenant_id}).")
        self._session_start_monotonic = time.monotonic()
        self._session_start_wall = time.time()
        self.speech_service.stt.start()

        if self.loop:
            self._prosody_task = asyncio.run_coroutine_threadsafe(self._prosody_timer(), self.loop)

    def stop(self):
        logger.info(f"[Orchestrator {self.stream_sid}] Session stopped.")

        if self._prosody_task and not self._prosody_task.done():
            self._prosody_task.cancel()

        self.speech_service.stt.stop()
        self._flush_session_curve()
        self.prosody.reset()

    def process_media(self, payload_base64: str):
        """
        Called for every Twilio media frame (~20ms).
        Decodes base64 -> mulaw -> PCM once, then fans out to STT + Prosody.
        """
        mulaw_data = base64.b64decode(payload_base64)
        pcm_data = audioop.ulaw2lin(mulaw_data, 2)

        self.speech_service.stt.process_pcm(pcm_data)

        frame_features = self.prosody.process_frame(pcm_data)
        if frame_features["is_speech"]:
            logger.debug(
                f"[Prosody {self.stream_sid}] FRAME speech=True energy={frame_features['energy']:.0f}"
            )

    def _handle_transcript(self, text: str, is_final: bool):
        """
        Synchronous callback from Azure C++ thread.
        We forward transcript deltas into the prosody engine and enrich transcript
        logging with the nearest score snapshot.
        """
        now_mono = time.monotonic()
        self.prosody.update_transcript(text=text, is_final=is_final, ts_monotonic=now_mono)

        tag = "FINAL" if is_final else "PARTIAL"
        rel_ts = self._relative_ts(now_mono)

        with self._lock:
            self._latest_transcript_text = text
            self._latest_transcript_is_final = is_final

            linked_score = None
            if self._last_score_sample:
                linked_score = {
                    "t_rel_sec": self._last_score_sample.get("t_rel_sec"),
                    "turn_complete_score": self._last_score_sample.get("turn_complete_score"),
                    "likely_end_of_turn": self._last_score_sample.get("likely_end_of_turn"),
                }

            event = {
                "t_rel_sec": rel_ts,
                "type": tag,
                "text": text,
                "linked_score": linked_score,
            }
            self._transcript_events.append(event)

        if linked_score:
            logger.info(
                f"[STT {self.stream_sid}] [{tag}] t={rel_ts:.3f}s score={linked_score['turn_complete_score']:.3f} "
                f"eot={linked_score['likely_end_of_turn']} text={text}"
            )
        else:
            logger.info(f"[STT {self.stream_sid}] [{tag}] t={rel_ts:.3f}s text={text}")

    async def _prosody_timer(self):
        """
        Periodically computes aggregate prosody features and logs them.
        Runs in the asyncio event loop and offloads computation to a thread.
        """
        logger.info(f"[Prosody {self.stream_sid}] Timer started (interval={PROSODY_INTERVAL}s)")

        try:
            while True:
                await asyncio.sleep(PROSODY_INTERVAL)
                result = await asyncio.to_thread(self.prosody.compute_prosody)

                now_mono = time.monotonic()
                rel_ts = self._relative_ts(now_mono)

                sample = {
                    "t_rel_sec": rel_ts,
                    "turn_complete_score": result["turn_complete_score"],
                    "likely_end_of_turn": result["likely_end_of_turn"],
                    "s_score": result["s_score"],
                    "p_score": result["p_score"],
                    "e_score": result["e_score"],
                    "r_score": result["r_score"],
                    "t_score": result["t_score"],
                    "es_score": result.get("es_score"),
                    "ps_score": result.get("ps_score"),
                    "zcr_score": result.get("zcr_score"),
                    "tilt_score": result.get("tilt_score"),
                    "silence_duration": result["silence_duration"],
                    "energy_current": result.get("energy_current"),
                    "log_energy_current": result.get("log_energy_current"),
                    "pitch_current": result.get("pitch_current"),
                    "pitch_delta_hz": result.get("pitch_delta_hz"),
                    "energy_slope_300ms": result.get("energy_slope_300ms"),
                    "pitch_slope_300ms": result.get("pitch_slope_300ms"),
                    "zcr_current": result.get("zcr_current"),
                    "spectral_tilt_ratio": result.get("spectral_tilt_ratio"),
                    "noise_floor_energy": result.get("noise_floor_energy"),
                    "speech_ratio_current": result.get("speech_ratio_current"),
                    "voiced_ratio": result.get("voiced_ratio"),
                    "rate_score_raw": result.get("rate_score_raw"),
                    "text_score_raw": result.get("text_score_raw"),
                    "energy_slope_raw": result.get("energy_slope_raw"),
                    "pitch_slope_raw": result.get("pitch_slope_raw"),
                    "zcr_shift_raw": result.get("zcr_shift_raw"),
                    "spectral_tilt_raw": result.get("spectral_tilt_raw"),
                    "transcript_rate_current": result.get("transcript_rate_current"),
                    "transcript_text": self._latest_transcript_text,
                    "transcript_is_final": self._latest_transcript_is_final,
                }

                with self._lock:
                    self._curve_samples.append(sample)
                    self._last_score_sample = sample

                logger.info(
                    f"[Prosody {self.stream_sid}] t={rel_ts:.3f}s score={result['turn_complete_score']:.3f} "
                    f"eot={result['likely_end_of_turn']} "
                    f"(S:{result['s_score']:.2f} P:{result['p_score']:.2f} E:{result['e_score']:.2f} "
                    f"R:{result['r_score']:.2f} T:{result['t_score']:.2f}) "
                    f"silence={result['silence_duration']:.2f}s "
                    f"pitch={result.get('pitch_current')} energy={result.get('energy_current'):.1f}"
                )

        except asyncio.CancelledError:
            logger.info(f"[Prosody {self.stream_sid}] Timer stopped.")
        except Exception as e:
            logger.error(f"[Prosody {self.stream_sid}] Timer error: {e}", exc_info=True)

    def _flush_session_curve(self) -> None:
        with self._lock:
            payload = {
                "stream_sid": self.stream_sid,
                "tenant_id": self.tenant_id,
                "session_start_epoch": self._session_start_wall,
                "session_end_epoch": time.time(),
                "prosody_interval_sec": PROSODY_INTERVAL,
                "curve": self._curve_samples,
                "transcript_events": self._transcript_events,
            }

        try:
            CURVE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            out_file = CURVE_OUTPUT_DIR / f"{self.stream_sid}.json"
            out_file.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
            logger.info(
                f"[SessionCurve {self.stream_sid}] Saved curve points={len(self._curve_samples)} "
                f"transcript_events={len(self._transcript_events)} path={out_file}"
            )
        except Exception as e:
            logger.error(f"[SessionCurve {self.stream_sid}] Failed to write curve log: {e}", exc_info=True)

    def _relative_ts(self, now_monotonic: float) -> float:
        if self._session_start_monotonic is None:
            return 0.0
        return round(max(0.0, now_monotonic - self._session_start_monotonic), 3)
