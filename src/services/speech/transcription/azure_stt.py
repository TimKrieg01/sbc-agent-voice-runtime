import base64
import logging
import audioop
import azure.cognitiveservices.speech as speechsdk
from src.core.config import settings
from .base import BaseTranscriptionService

logger = logging.getLogger(__name__)

class AzureTranscriptionService(BaseTranscriptionService):
    def __init__(self, languages: list = None):
        super().__init__()
        self.languages = languages or ["en-US", "de-DE"]
        
        # Use a dummy text if empty so we don't crash on init if env is not configured yet
        speech_key = settings.AZURE_SPEECH_KEY or "DUMMY"
        speech_region = settings.AZURE_SPEECH_REGION or "DUMMY"
        
        if settings.AZURE_SPEECH_ENDPOINT:
            self.speech_config = speechsdk.SpeechConfig(
                endpoint=settings.AZURE_SPEECH_ENDPOINT,
                subscription=speech_key
            )
        else:
            self.speech_config = speechsdk.SpeechConfig(
                subscription=speech_key, 
                region=speech_region
            )
            
        # Increase the continuous recognition segmentation silence timeout
        # (How long Azure waits before packaging the partials into a FINAL transcript)
        self.speech_config.set_property(speechsdk.PropertyId.Speech_SegmentationSilenceTimeoutMs, "1000")
        
        # Twilio sends 8000Hz PCM mu-law. 
        # We'll convert it using python's audioop to 8000Hz 16-bit PCM for Azure
        self.audio_format = speechsdk.audio.AudioStreamFormat(samples_per_second=8000, bits_per_sample=16, channels=1)
        self.push_stream = speechsdk.audio.PushAudioInputStream(stream_format=self.audio_format)
        self.audio_config = speechsdk.audio.AudioConfig(stream=self.push_stream)
        
        # Setup continuous language detection (English and German)
        auto_detect_source_language_config = speechsdk.AutoDetectSourceLanguageConfig(languages=self.languages)
        
        self.recognizer = speechsdk.SpeechRecognizer(
            speech_config=self.speech_config, 
            auto_detect_source_language_config=auto_detect_source_language_config,
            audio_config=self.audio_config
        )
        
        # Wire up events
        self.recognizer.recognized.connect(self._on_recognized)
        self.recognizer.recognizing.connect(self._on_recognizing)
        self.recognizer.session_started.connect(lambda evt: logger.info("Azure STT Session Started"))
        self.recognizer.session_stopped.connect(lambda evt: logger.info("Azure STT Session Stopped"))
        self.recognizer.canceled.connect(self._on_canceled)

    def start(self):
        if not settings.AZURE_SPEECH_KEY or settings.AZURE_SPEECH_KEY == "DUMMY":
            logger.warning("Azure Speech Key is empty! Transcription will fail.")
        else:
            logger.info("Starting Azure STT recognition...")
        self.recognizer.start_continuous_recognition_async()

    def stop(self):
        logger.info("Stopping Azure STT recognition...")
        self.recognizer.stop_continuous_recognition_async()
        self.push_stream.close()

    def process_audio(self, payload_base64: str):
        """Legacy interface: decode base64 mulaw and push to Azure."""
        mulaw_data = base64.b64decode(payload_base64)
        pcm_data = audioop.ulaw2lin(mulaw_data, 2)
        self.process_pcm(pcm_data)

    def process_pcm(self, pcm_data: bytes):
        """Push raw 8kHz 16-bit PCM bytes directly to the Azure stream."""
        self.push_stream.write(pcm_data)

    def _on_recognized(self, evt: speechsdk.SpeechRecognitionEventArgs):
        if evt.result.reason == speechsdk.ResultReason.RecognizedSpeech:
            text = evt.result.text.strip()
            if text:
                self.fire_transcript_event(text, is_final=True)

    def _on_recognizing(self, evt: speechsdk.SpeechRecognitionEventArgs):
        if evt.result.reason == speechsdk.ResultReason.RecognizingSpeech:
            text = evt.result.text.strip()
            if text:
                self.fire_transcript_event(text, is_final=False)

    def _on_canceled(self, evt: speechsdk.SpeechRecognitionCanceledEventArgs):
        logger.warning(f"Azure STT Canceled: {evt.reason}")
        if evt.reason == speechsdk.CancellationReason.Error:
            logger.error(f"Error details: {evt.error_details}")
