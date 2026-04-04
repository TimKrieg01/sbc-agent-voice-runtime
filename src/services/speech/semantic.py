import logging
import asyncio

logger = logging.getLogger(__name__)

class SemanticClassifier:
    """
    Hybrid NLP Architecture.
    Uses an ultra-fast local Cross-Encoder Transformer for Semantic Interruptions,
    and a 0ms grammatical heuristic for Completeness checks (since sub-8B models struggle with syntax formatting).
    """
    def __init__(self):
        logger.info("Initializing Local NLP Transformer model in memory... (This may take a moment to download initially)")
        from transformers import pipeline
        self.classifier = pipeline(
            "zero-shot-classification",
            model="cross-encoder/nli-distilroberta-base",
            device=-1  # Force CPU execution for maximum compatibility
        )
        logger.info("Local NLP Transformer loaded successfully!")
    
    async def is_barge_in(self, partial_text: str, agent_context: str = "") -> bool:
        """
        Runs the partial text through the neural network to determine if it is an interruption.
        """
        clean_text = partial_text.strip().lower().strip(",.?!")
        if not clean_text:
            return False
            
        def _classify():
            return self.classifier(
                clean_text,
                candidate_labels=["interruption", "agreement or backchannel"],
            )

        # Dispatch the synchronous PyTorch inference to a background thread
        result = await asyncio.to_thread(_classify)
        
        top_label = result['labels'][0]
        logger.info(f"[SemanticModel] NLP Classified '{clean_text}' as: '{top_label}' (Confidence: {result['scores'][0]:.2f})")
        
        return top_label == "interruption"

    async def is_complete_thought(self, text: str, agent_context: str = "") -> bool:
        """
        Uses a lightning-fast trailing grammar heuristic to check if the human finished their sentence.
        Tiny Models struggle with grammatical syntax instructions, so hardcoded evaluation is the industry standard!
        """
        clean_text = text.strip()
        if not clean_text:
            return False
            
        # If the transcript ends abruptly on a conjunction/preposition, it's a hanging thought
        hanging_endings = [" to", " the", " and", " but", " if", " or", " because", " wait", " i", " a", " an", " of", " in", " with", " are"]
        for ending in hanging_endings:
            if clean_text.lower().endswith(ending):
                logger.info(f"[SemanticModel] Grammatical heuristic flagged incomplete thought: '{clean_text}'")
                return False
                
        logger.info(f"[SemanticModel] Grammatical heuristic cleared complete thought: '{clean_text}'")
        return True
