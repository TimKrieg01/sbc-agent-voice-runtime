from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    AZURE_SPEECH_KEY: str = ""
    AZURE_SPEECH_REGION: str = "eastus"
    AZURE_SPEECH_ENDPOINT: str | None = None

    # Runtime config database used for SIP policy/realtime integration.
    SIP_CONFIG_DATABASE_URL: str = ""

    # Session profile fallback values for ARI worker if Stasis args are incomplete.
    SIP_DEFAULT_TENANT_ID: str = "default"
    SIP_DEFAULT_STT_ENGINE: str = "azure"
    SIP_DEFAULT_LANGUAGES: str = "en-US"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # Keep additional worker env keys in the same .env file.
        extra="ignore",
    )

settings = Settings()
