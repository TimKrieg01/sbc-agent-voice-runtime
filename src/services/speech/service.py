from enum import Enum
import logging
from .transcription.azure_stt import AzureTranscriptionService
from .transcription.base import BaseTranscriptionService

logger = logging.getLogger(__name__)

class TranscriptionEngine(Enum):
    AZURE = "azure"
    DEEPGRAM = "deepgram"
    ELEVENLABS = "elevenlabs"

class SpeechService:
    def __init__(self, engine: TranscriptionEngine = TranscriptionEngine.AZURE, languages: list = None):
        self.engine = engine
        self.languages = languages or ["en-US", "de-DE"]
        self._stt: BaseTranscriptionService = None
        self._initialize_engines()

    def _initialize_engines(self):
        if self.engine == TranscriptionEngine.AZURE:
            self._stt = AzureTranscriptionService(languages=self.languages)
        else:
            raise NotImplementedError(f"Transcription engine {self.engine.value} not implemented yet.")

    @property
    def stt(self) -> BaseTranscriptionService:
        return self._stt
