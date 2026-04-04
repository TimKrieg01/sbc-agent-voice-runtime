"""
Real-time end-of-turn scoring engine.

This version is fully dynamic: component scores are recomputed each tick from
recent windows and speaker baselines. We keep only light EMA smoothing for
stability, not max-latching.
"""

from __future__ import annotations

import audioop
import logging
import math
import time
from collections import deque
from typing import Optional

import numpy as np
import webrtcvad

logger = logging.getLogger(__name__)


class ProsodyEngine:
    INPUT_SAMPLE_RATE = 8000
    TARGET_SAMPLE_RATE = 16000
    FRAME_DURATION_MS = 20
    SHORT_WINDOW_FRAMES = 50
    BASELINE_MAX_ENTRIES = 500

    # Rebalanced: less silence-dominant, more multi-cue acoustic voting.
    W_SILENCE = 0.40
    W_PITCH_DROP = 0.10
    W_ENERGY_DROP = 0.2
    W_ENERGY_SLOPE = 0.15
    W_PITCH_SLOPE = 0.10
    W_ZCR_SHIFT = 0.10
    W_SPECTRAL_TILT = 0.05
    W_RATE_SLOWDOWN = 0.0
    W_TEXT_COMPLETENESS = 0.0

    SILENCE_SOFT_CAP_SEC = 0.9
    TURN_COMPLETE_THRESHOLD = 0.62
    SPEAKING_GRACE_SEC = 0.22

    PITCH_UPDATE_MIN_SEC = 0.12
    MIN_TURN_VOICED_FRAMES = 5
    TRANSCRIPT_EVIDENCE_WINDOW_SEC = 2.0

    NOISE_FLOOR_ALPHA = 0.06
    MIN_SPEECH_TO_NOISE_RATIO = 2.2

    # EMA smoothing (dynamic, not latching)
    COMPONENT_EMA_ALPHA = 0.55
    TOTAL_EMA_ALPHA = 0.65
    MAX_ENERGY_SLOPE_ABS = 6.0
    MAX_PITCH_SLOPE_ABS = 600.0

    def __init__(self, vad_aggressiveness: int = 2):
        self.frame_buffer: deque[np.ndarray] = deque(maxlen=self.SHORT_WINDOW_FRAMES)
        self.recent_energy: deque[float] = deque(maxlen=self.SHORT_WINDOW_FRAMES)
        self.recent_voiced_flags: deque[int] = deque(maxlen=self.SHORT_WINDOW_FRAMES)

        self.voiced_energy_history: deque[float] = deque(maxlen=self.BASELINE_MAX_ENTRIES)
        self.voiced_pitch_history: deque[float] = deque(maxlen=self.BASELINE_MAX_ENTRIES)
        self.voiced_zcr_history: deque[float] = deque(maxlen=self.BASELINE_MAX_ENTRIES)
        self.voiced_tilt_history: deque[float] = deque(maxlen=self.BASELINE_MAX_ENTRIES)
        self.transcript_rate_history: deque[float] = deque(maxlen=self.BASELINE_MAX_ENTRIES)

        self.vad = webrtcvad.Vad(vad_aggressiveness)
        self.last_speech_time: float = time.monotonic()
        self.last_valid_speech_time: float = self.last_speech_time
        self._ratecv_state = None

        self.latest_transcript_text: str = ""
        self.latest_transcript_ts: float | None = None
        self.latest_transcript_is_final: bool = False
        self._last_rate_text: str = ""
        self._last_rate_ts: float | None = None
        self._recent_transcript_rates: deque[float] = deque(maxlen=10)

        self._last_pitch_estimate: float | None = None
        self._last_pitch_estimate_ts: float = 0.0

        self.noise_floor_energy: float = 250.0
        self.turn_voiced_frames: int = 0
        # Log-energy series keeps slope numerically stable.
        self.energy_series: deque[tuple[float, float]] = deque(maxlen=100)
        self.pitch_series: deque[tuple[float, float]] = deque(maxlen=100)

        # Smoothed dynamic scores
        self._s_ema = 0.0
        self._p_ema = 0.0
        self._e_ema = 0.0
        self._r_ema = 0.0
        self._t_ema = 0.0
        self._total_ema = 0.0

        logger.info("[ProsodyEngine] Initialized (fully dynamic scoring mode).")

    def process_frame(self, pcm_8k: bytes) -> dict:
        pcm_16k, self._ratecv_state = audioop.ratecv(
            pcm_8k,
            2,
            1,
            self.INPUT_SAMPLE_RATE,
            self.TARGET_SAMPLE_RATE,
            self._ratecv_state,
        )
        audio_f32 = np.frombuffer(pcm_16k, dtype=np.int16).astype(np.float32)

        energy = float(np.mean(audio_f32**2))
        zcr_frame = self._zcr(audio_f32)
        tilt_frame = self._spectral_tilt_ratio(audio_f32)
        try:
            vad_speech = self.vad.is_speech(pcm_16k, self.TARGET_SAMPLE_RATE)
        except Exception:
            vad_speech = False

        if not vad_speech:
            self.noise_floor_energy = (
                (1.0 - self.NOISE_FLOOR_ALPHA) * self.noise_floor_energy
                + self.NOISE_FLOOR_ALPHA * energy
            )

        speech_ratio = energy / max(self.noise_floor_energy, 1.0)
        valid_speech = vad_speech and speech_ratio >= self.MIN_SPEECH_TO_NOISE_RATIO

        if valid_speech:
            now = time.monotonic()
            self.last_speech_time = now
            self.last_valid_speech_time = now
            self.voiced_energy_history.append(energy)
            self.voiced_zcr_history.append(zcr_frame)
            self.voiced_tilt_history.append(tilt_frame)
            self.turn_voiced_frames += 1

        self.frame_buffer.append(audio_f32)
        self.recent_energy.append(energy)
        self.recent_voiced_flags.append(1 if valid_speech else 0)

        return {
            "energy": energy,
            "zcr_frame": round(zcr_frame, 5),
            "spectral_tilt_frame": round(tilt_frame, 5),
            "is_speech": valid_speech,
            "vad_speech": vad_speech,
            "speech_ratio": round(speech_ratio, 3),
            "noise_floor_energy": round(self.noise_floor_energy, 2),
        }

    def update_transcript(self, text: str, is_final: bool, ts_monotonic: float | None = None) -> None:
        ts = ts_monotonic if ts_monotonic is not None else time.monotonic()
        safe_text = (text or "").strip()
        self.latest_transcript_text = safe_text
        self.latest_transcript_ts = ts
        self.latest_transcript_is_final = is_final

        if self._last_rate_ts is None:
            self._last_rate_text = safe_text
            self._last_rate_ts = ts
            return

        dt = max(1e-3, ts - self._last_rate_ts)
        lcp = self._longest_common_prefix(self._last_rate_text, safe_text)
        chars_added = max(0, len(safe_text) - lcp)
        inst_rate = chars_added / dt

        if chars_added > 0 and inst_rate > 0:
            self._recent_transcript_rates.append(inst_rate)
            self.transcript_rate_history.append(inst_rate)

        self._last_rate_text = safe_text
        self._last_rate_ts = ts

    def compute_prosody(self) -> dict:
        now = time.monotonic()
        silence_duration = now - self.last_speech_time
        currently_speaking = silence_duration < self.SPEAKING_GRACE_SEC

        # Audio-first gating for this mode: require enough prior voiced evidence.
        has_turn_evidence = self.turn_voiced_frames >= self.MIN_TURN_VOICED_FRAMES

        voiced_ratio = float(np.mean(list(self.recent_voiced_flags)[-12:])) if self.recent_voiced_flags else 0.0

        # Dynamic silence score
        silence_raw = 0.0
        if has_turn_evidence:
            silence_raw = self._clamp01((silence_duration - 0.03) / max(0.2, self.SILENCE_SOFT_CAP_SEC - 0.03))
        if silence_duration < 0.06:
            silence_raw = 0.0

        # Dynamic energy score against personal baseline (mildly more sensitive)
        v_energies = list(self.voiced_energy_history)
        e_current = float(np.mean(list(self.recent_energy)[-10:])) if self.recent_energy else 0.0
        log_energy_current = float(math.log10(max(e_current, 1.0)))
        self.energy_series.append((now, log_energy_current))
        if len(v_energies) >= 12:
            e_baseline = float(np.median(v_energies))
            e_std = float(np.std(v_energies) + 1e-6)
            e_drop = max(0.0, e_baseline - e_current)
            energy_raw = self._sigmoid01(e_drop / max(e_std, e_baseline * 0.14, 1.0))
        else:
            energy_raw = 0.0

        # Dynamic pitch score (mildly more sensitive)
        window_frames = list(self.frame_buffer)[-25:]
        audio_window = np.concatenate(window_frames) if window_frames else np.array([], dtype=np.float32)
        pitch_current = self._last_pitch_estimate
        if now - self._last_pitch_estimate_ts >= self.PITCH_UPDATE_MIN_SEC:
            pitch_current = self._estimate_pitch_fast(audio_window, voiced_ratio, e_current)
            self._last_pitch_estimate = pitch_current
            self._last_pitch_estimate_ts = now

        if pitch_current is not None and voiced_ratio > 0.15:
            self.voiced_pitch_history.append(pitch_current)
            self.pitch_series.append((now, float(pitch_current)))

        v_pitches = list(self.voiced_pitch_history)
        pitch_delta_hz = 0.0
        if len(v_pitches) >= 12 and pitch_current is not None:
            p_baseline = float(np.median(v_pitches))
            p_std = float(np.std(v_pitches) + 1e-6)
            pitch_delta_hz = p_baseline - pitch_current
            p_drop = max(0.0, p_baseline - pitch_current)
            pitch_raw = self._sigmoid01(p_drop / max(1.2 * p_std, 14.0))
        else:
            pitch_raw = 0.0

        # Dynamic transcript-derived signals
        rate_raw = self._compute_rate_slowdown_score(now)
        text_raw = self._compute_text_completion_score(now)
        transcript_rate_current = (
            float(np.mean(list(self._recent_transcript_rates)[-3:])) if self._recent_transcript_rates else 0.0
        )
        speech_ratio_current = e_current / max(self.noise_floor_energy, 1.0)

        # New acoustic weak signals (stream-only)
        energy_slope_300ms = self._slope_over_window(self.energy_series, 0.30)
        energy_slope_300ms = float(np.clip(energy_slope_300ms, -self.MAX_ENERGY_SLOPE_ABS, self.MAX_ENERGY_SLOPE_ABS))
        pitch_slope_300ms = self._slope_over_window(self.pitch_series, 0.30)
        pitch_slope_300ms = float(np.clip(pitch_slope_300ms, -self.MAX_PITCH_SLOPE_ABS, self.MAX_PITCH_SLOPE_ABS))

        zcr_current = self._zcr(audio_window) if len(audio_window) else 0.0
        voiced_zcr = list(self.voiced_zcr_history)
        zcr_base = float(np.median(voiced_zcr)) if len(voiced_zcr) >= 12 else zcr_current
        zcr_std = float(np.std(voiced_zcr) + 1e-6) if len(voiced_zcr) >= 12 else 0.02

        tilt_current = self._spectral_tilt_ratio(audio_window) if len(audio_window) else 0.0
        voiced_tilt = list(self.voiced_tilt_history)
        tilt_base = float(np.median(voiced_tilt)) if len(voiced_tilt) >= 12 else tilt_current
        tilt_std = float(np.std(voiced_tilt) + 1e-6) if len(voiced_tilt) >= 12 else 0.1

        energy_slope_raw = self._sigmoid01(max(0.0, -energy_slope_300ms) / 0.75)
        pitch_slope_raw = self._sigmoid01(max(0.0, -pitch_slope_300ms) / 120.0)
        zcr_shift_raw = self._sigmoid01(max(0.0, zcr_current - zcr_base) / max(zcr_std, 0.01))
        spectral_tilt_raw = self._sigmoid01(max(0.0, tilt_current - tilt_base) / max(tilt_std, 1.5))

        # EMA smoothing only (no max-latching)
        self._s_ema = self._ema(self._s_ema, silence_raw, self.COMPONENT_EMA_ALPHA)
        self._p_ema = self._ema(self._p_ema, pitch_raw, self.COMPONENT_EMA_ALPHA)
        self._e_ema = self._ema(self._e_ema, energy_raw, self.COMPONENT_EMA_ALPHA)
        self._r_ema = self._ema(self._r_ema, rate_raw, self.COMPONENT_EMA_ALPHA)
        self._t_ema = self._ema(self._t_ema, text_raw, self.COMPONENT_EMA_ALPHA)
        es_ema = self._ema(getattr(self, "_es_ema", 0.0), energy_slope_raw, self.COMPONENT_EMA_ALPHA)
        ps_ema = self._ema(getattr(self, "_ps_ema", 0.0), pitch_slope_raw, self.COMPONENT_EMA_ALPHA)
        zcr_ema = self._ema(getattr(self, "_zcr_ema", 0.0), zcr_shift_raw, self.COMPONENT_EMA_ALPHA)
        tilt_ema = self._ema(getattr(self, "_tilt_ema", 0.0), spectral_tilt_raw, self.COMPONENT_EMA_ALPHA)
        self._es_ema = es_ema
        self._ps_ema = ps_ema
        self._zcr_ema = zcr_ema
        self._tilt_ema = tilt_ema

        combined_raw = self._noisy_or(
            (
                (self.W_SILENCE, self._s_ema),
                (self.W_PITCH_DROP, self._p_ema),
                (self.W_ENERGY_DROP, self._e_ema),
                (self.W_ENERGY_SLOPE, es_ema),
                (self.W_PITCH_SLOPE, ps_ema),
                (self.W_ZCR_SHIFT, zcr_ema),
                (self.W_SPECTRAL_TILT, tilt_ema),
                (self.W_RATE_SLOWDOWN, self._r_ema),
                (self.W_TEXT_COMPLETENESS, self._t_ema),
            )
        )

        # Thinking-pause guard: short pause + still highly voiced + weak release cues.
        if (
            0.15 <= silence_duration <= 0.85
            and voiced_ratio > 0.42
            and es_ema < 0.25
            and self._e_ema < 0.22
            and ps_ema < 0.25
        ):
            combined_raw *= 0.55

        # Bold boost for obvious endpoint acoustics.
        if (
            has_turn_evidence
            and silence_duration >= 0.45
            and voiced_ratio < 0.24
            and (self._e_ema >= 0.24 or es_ema >= 0.32 or zcr_ema >= 0.58)
        ):
            combined_raw = max(combined_raw, 0.82)

        self._total_ema = self._ema(self._total_ema, combined_raw, self.TOTAL_EMA_ALPHA)

        likely_end_of_turn = (
            self._total_ema >= self.TURN_COMPLETE_THRESHOLD
            and silence_duration >= 0.18
            and has_turn_evidence
            and voiced_ratio < 0.35
        )

        # Keep evidence across long trailing silences in-session to avoid
        # collapsing s_score/decision back to zero after a clear ending.

        return {
            "turn_complete_score": round(self._total_ema, 4),
            "end_of_turn_probability": round(self._total_ema, 4),
            "likely_end_of_turn": likely_end_of_turn,
            "s_score": round(self._s_ema, 4),
            "p_score": round(self._p_ema, 4),
            "e_score": round(self._e_ema, 4),
            "r_score": round(self._r_ema, 4),
            "t_score": round(self._t_ema, 4),
            "es_score": round(es_ema, 4),
            "ps_score": round(ps_ema, 4),
            "zcr_score": round(zcr_ema, 4),
            "tilt_score": round(tilt_ema, 4),
            "silence_duration": round(silence_duration, 3),
            "is_speech": currently_speaking,
            "has_turn_evidence": has_turn_evidence,
            "noise_floor_energy": round(self.noise_floor_energy, 2),
            "speech_ratio_current": round(speech_ratio_current, 4),
            "voiced_ratio": round(voiced_ratio, 4),
            "pitch_current": round(pitch_current, 2) if pitch_current is not None else None,
            "pitch_delta_hz": round(pitch_delta_hz, 2),
            "energy_current": round(e_current, 2),
            "log_energy_current": round(log_energy_current, 4),
            "energy_slope_300ms": round(energy_slope_300ms, 4),
            "pitch_slope_300ms": round(pitch_slope_300ms, 4),
            "zcr_current": round(zcr_current, 5),
            "spectral_tilt_ratio": round(tilt_current, 5),
            "transcript_text": self.latest_transcript_text,
            "transcript_is_final": self.latest_transcript_is_final,
            "rate_score_raw": round(rate_raw, 4),
            "text_score_raw": round(text_raw, 4),
            "transcript_rate_current": round(transcript_rate_current, 4),
            "energy_slope_raw": round(energy_slope_raw, 4),
            "pitch_slope_raw": round(pitch_slope_raw, 4),
            "zcr_shift_raw": round(zcr_shift_raw, 4),
            "spectral_tilt_raw": round(spectral_tilt_raw, 4),
        }

    def _compute_rate_slowdown_score(self, now: float) -> float:
        if len(self.transcript_rate_history) < 8 or not self._recent_transcript_rates:
            return 0.0

        baseline = float(np.median(self.transcript_rate_history))
        baseline_std = float(np.std(self.transcript_rate_history) + 1e-6)
        current_rate = float(np.mean(list(self._recent_transcript_rates)[-3:]))

        slowdown = max(0.0, baseline - current_rate)
        score = self._sigmoid01(slowdown / max(1.2 * baseline_std, 2.0))

        if self.latest_transcript_ts is None:
            return 0.0
        age = now - self.latest_transcript_ts
        age_decay = math.exp(-max(0.0, age) / 1.5)
        return self._clamp01(score * age_decay)

    def _compute_text_completion_score(self, now: float) -> float:
        text = (self.latest_transcript_text or "").strip()
        if not text or self.latest_transcript_ts is None:
            return 0.0

        score = 0.0
        lower = text.lower()

        if text[-1:] in ".!?":
            score += 0.7
        elif text[-1:] in ",;:":
            score += 0.25

        hanging_endings = (
            " to",
            " the",
            " and",
            " but",
            " if",
            " or",
            " because",
            " i",
            " a",
            " an",
            " of",
            " in",
            " with",
            " are",
        )
        if any(lower.endswith(suffix) for suffix in hanging_endings):
            score -= 0.4

        if self.latest_transcript_is_final:
            score += 0.15

        age = now - self.latest_transcript_ts
        age_decay = math.exp(-max(0.0, age) / 1.4)
        return self._clamp01(score * age_decay)

    def _estimate_pitch_fast(self, audio_window: np.ndarray, voiced_ratio: float, e_current: float) -> Optional[float]:
        if len(audio_window) < 320:
            return None
        try:
            peak = float(np.max(np.abs(audio_window))) if len(audio_window) else 0.0
            # Relaxed gating for narrow-band telephony.
            if peak < 60.0 or voiced_ratio < 0.08 or e_current < 30.0:
                return None

            x = (audio_window / peak).astype(np.float32)
            x = x - float(np.mean(x))
            # Mild pre-emphasis helps periodicity tracking.
            x = np.append(x[0], x[1:] - 0.97 * x[:-1]).astype(np.float32)

            min_lag = int(self.TARGET_SAMPLE_RATE / 320.0)
            max_lag = int(self.TARGET_SAMPLE_RATE / 80.0)
            if len(x) <= max_lag + 2:
                return None

            autocorr = np.correlate(x, x, mode="full")
            ac = autocorr[len(autocorr) // 2 :]
            if len(ac) <= max_lag:
                return None
            ac[:min_lag] = 0.0

            segment = ac[min_lag:max_lag]
            if len(segment) == 0:
                return None
            peak_idx = int(np.argmax(segment)) + min_lag
            peak_val = float(ac[peak_idx])

            if peak_val < 0.06 * float(ac[0] + 1e-6):
                return None

            freq = self.TARGET_SAMPLE_RATE / float(peak_idx)
            if 80.0 <= freq <= 320.0:
                return float(freq)
            return None
        except Exception:
            return None

    @staticmethod
    def _zcr(audio: np.ndarray) -> float:
        if len(audio) < 2:
            return 0.0
        x = audio.astype(np.float32)
        signs = np.sign(x)
        return float(np.mean(signs[1:] != signs[:-1]))

    def _spectral_tilt_ratio(self, audio: np.ndarray) -> float:
        if len(audio) < 64:
            return 0.0
        x = audio.astype(np.float32)
        x = x - float(np.mean(x))
        n = len(x)
        win = np.hanning(n).astype(np.float32)
        spec = np.fft.rfft(x * win)
        power = (np.abs(spec) ** 2)
        freqs = np.fft.rfftfreq(n, d=1.0 / self.TARGET_SAMPLE_RATE)
        low = float(np.sum(power[(freqs >= 80) & (freqs < 1000)]))
        high = float(np.sum(power[(freqs >= 1000) & (freqs < 4000)]))
        total = float(np.sum(power) + 1e-6)
        floor = max(1e-6, total * 1e-6)
        tilt_db = 10.0 * math.log10((low + floor) / (high + floor))
        return float(np.clip(tilt_db, -30.0, 30.0))

    @staticmethod
    def _slope_over_window(series: deque[tuple[float, float]], window_sec: float) -> float:
        if len(series) < 2:
            return 0.0
        now_t = series[-1][0]
        pts = [p for p in series if p[0] >= (now_t - window_sec)]
        if len(pts) < 2:
            return 0.0
        t0, v0 = pts[0]
        t1, v1 = pts[-1]
        dt = max(1e-3, t1 - t0)
        return float((v1 - v0) / dt)

    @staticmethod
    def _ema(prev: float, current: float, alpha: float) -> float:
        return (1.0 - alpha) * prev + alpha * current

    @staticmethod
    def _sigmoid01(x: float) -> float:
        # Maps 0..inf to ~0..1 smoothly.
        return float(1.0 - math.exp(-max(0.0, x)))

    @staticmethod
    def _longest_common_prefix(a: str, b: str) -> int:
        i = 0
        upper = min(len(a), len(b))
        while i < upper and a[i] == b[i]:
            i += 1
        return i

    @staticmethod
    def _clamp01(value: float) -> float:
        return float(max(0.0, min(1.0, value)))

    @staticmethod
    def _noisy_or(weighted_signals: tuple[tuple[float, float], ...]) -> float:
        miss_prob = 1.0
        for weight, signal in weighted_signals:
            miss_prob *= 1.0 - (max(0.0, weight) * max(0.0, min(1.0, signal)))
        return 1.0 - miss_prob

    def reset(self):
        self.frame_buffer.clear()
        self.recent_energy.clear()
        self.recent_voiced_flags.clear()
        self.voiced_energy_history.clear()
        self.voiced_pitch_history.clear()
        self.voiced_zcr_history.clear()
        self.voiced_tilt_history.clear()
        self.transcript_rate_history.clear()
        self._recent_transcript_rates.clear()
        self.energy_series.clear()
        self.pitch_series.clear()

        self.latest_transcript_text = ""
        self.latest_transcript_ts = None
        self.latest_transcript_is_final = False
        self._last_rate_text = ""
        self._last_rate_ts = None

        self._last_pitch_estimate = None
        self._last_pitch_estimate_ts = 0.0

        self.noise_floor_energy = 250.0
        self.turn_voiced_frames = 0

        self._s_ema = 0.0
        self._p_ema = 0.0
        self._e_ema = 0.0
        self._r_ema = 0.0
        self._t_ema = 0.0
        self._total_ema = 0.0
        self._es_ema = 0.0
        self._ps_ema = 0.0
        self._zcr_ema = 0.0
        self._tilt_ema = 0.0

        self._ratecv_state = None
        self.last_speech_time = time.monotonic()
        self.last_valid_speech_time = self.last_speech_time

        logger.info("[ProsodyEngine] Reset.")
