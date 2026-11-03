from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://identity_svc:identity_dev_password@localhost:5432/opspilot"
    db_schema: str = "identity"
    rabbitmq_url: str = "amqp://opspilot:opspilot@localhost:5672/"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = Field(min_length=32)
    fernet_key: str = Field(min_length=44)
    resilience_config: str | None = None
    session_minutes: int = 30
    challenge_minutes: int = 5
    totp_issuer: str = "OpsPilot"
    bootstrap_admin_email: str | None = None
    bootstrap_admin_password: str | None = None
    bootstrap_admin_name: str = "Administrator"
    log_json: bool = True
