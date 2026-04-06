from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    AZURE_SPEECH_KEY: str = ""
    AZURE_SPEECH_REGION: str = "eastus"
    AZURE_SPEECH_ENDPOINT: str | None = None

    # Tenant config source for SIP ingress:
    # - env: use SIP_TENANT_RULES_JSON
    # - db: use SIP_CONFIG_DATABASE_URL tables only
    # - auto: prefer DB if configured, else fallback to env
    SIP_TENANT_CONFIG_SOURCE: str = "auto"
    SIP_CONFIG_DATABASE_URL: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # We keep extra env keys for SIP/ARI workers in the same .env file.
        extra="ignore",
    )

settings = Settings()
