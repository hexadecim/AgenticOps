from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://privacy_svc:privacy_dev_password@localhost:5432/opspilot"
    db_schema: str = "privacy"
    rabbitmq_url: str = "amqp://opspilot:opspilot@localhost:5672/"
    redis_url: str = "redis://localhost:6379/0"
    mail_url: str = "http://localhost:8003"
    jwt_secret: str = Field(min_length=32)
    fernet_key: str = Field(min_length=44)
    resilience_config: str | None = None
    log_json: bool = True
    ner_enabled: bool = True  # Presidio + spaCy; off only in tests that do not need names from free text
