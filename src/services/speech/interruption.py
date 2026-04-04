import logging

logger = logging.getLogger(__name__)

class InterruptionFilter:
    """
    Acts as the Semantic Layer to decide if PARTIAL transcript events should 
    cut off the Twilio audio stream (Interruption), or be ignored (Backchannel).
    """
    def __init__(self):
        # We prepare this space for an NLP/LLM injection later.
        # For now, we use a simple heuristic list of backchannels.
        self.backchannels = ["mhm", "yeah", "ok", "okay", "got it", "i see", "right", "sure", "ah", "hm", "yes"]

    def is_interruption(self, partial_text: str) -> bool:
        """
        Returns True if the text represents a valid interruption that should halt TTS.
        Returns False if it is just a backchannel agreement.
        """
        clean_text = partial_text.strip().lower().strip(",.?!")
        
        if not clean_text:
            return False
            
        if clean_text in self.backchannels:
            logger.info(f"[SemanticFilter] Ignored backchannel text: '{clean_text}'")
            return False
            
        logger.warning(f"[SemanticFilter] Valid interruption detected: '{clean_text}'")
        return True
