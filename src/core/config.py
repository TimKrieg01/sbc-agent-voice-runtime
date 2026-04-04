import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    AZURE_SPEECH_KEY: str = ""
    AZURE_SPEECH_REGION: str = "eastus"
    AZURE_SPEECH_ENDPOINT: str | None = None

    class Config:
        env_file = ".env"
        env_file_encoding = 'utf-8'

settings = Settings()
