from abc import ABC, abstractmethod
from typing import Callable, List
import logging

logger = logging.getLogger(__name__)

class BaseTranscriptionService(ABC):
    def __init__(self):
        self.callbacks: List[Callable[[str, bool], None]] = []

    def on_transcript(self, callback: Callable[[str, bool], None]):
        """Register a callback for transcription events (text, is_final)."""
        self.callbacks.append(callback)

    def fire_transcript_event(self, text: str, is_final: bool):
        for cb in self.callbacks:
            try:
                cb(text, is_final)
            except Exception as e:
                logger.error(f"Error in transcript callback: {e}")

    @abstractmethod
    def start(self):
        pass

    @abstractmethod
    def stop(self):
        pass

    @abstractmethod
    def process_audio(self, payload_base64: str):
        """Process base64-encoded audio payload (legacy interface)."""
        pass

    @abstractmethod
    def process_pcm(self, pcm_data: bytes):
        """Process raw 8kHz 16-bit PCM bytes directly."""
        pass
