import logging

logger = logging.getLogger(__name__)

class CustomerBackendClient:
    """
    Simulates the connection to the external Tenant's "Brain" (via Webhook or WebSocket).
    """
    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id

    def send_turn(self, transcript: str):
        """
        Fired when Azure STT detects the end of the user's speech.
        Sends the compiled text to the LLM to process.
        """
        logger.info(f"[CustomerBackend API - {self.tenant_id}] User Finished Speaking: '{transcript}'")
        # TODO: Implement actual HTTP POST or WebSocket push to customer here.

    def send_interruption(self):
        """
        Fired when the user interrupts the active TTS playback.
        Alerts the backend to stop any active LLM generation.
        """
        logger.info(f"[CustomerBackend API - {self.tenant_id}] ALARM: User interrupted! Cancel active generations.")
        # TODO: Implement actual HTTP POST or WebSocket push to customer here.
