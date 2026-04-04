# Dummy lookup table for tenant configuration
# Keyed by the dialed Twilio Number (To) or SIP Domain

DUMMY_TENANT_DB = {
    # The default configuration that will apply to all calls for now
    "default": {
        "tenant_id": "tenant_123",
        "stt_engine": "azure",
        "languages": "en-US,de-DE"
    }
}

def get_tenant_config(sip_domain_or_number: str) -> dict:
    """Returns the tenant config. Defaults to the same one for now."""
    return DUMMY_TENANT_DB.get(sip_domain_or_number, DUMMY_TENANT_DB["default"])
