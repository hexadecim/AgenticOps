from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://mail_svc:mail_dev_password@localhost:5432/opspilot"
    db_schema: str = "mail"
    rabbitmq_url: str = "amqp://opspilot:opspilot@localhost:5672/"
    redis_url: str = "redis://localhost:6379/0"
    settings_url: str = "http://localhost:8002"
    privacy_url: str = "http://localhost:8004"
    jwt_secret: str = Field(min_length=32)
    resilience_config: str | None = None
    log_json: bool = True
    poller_enabled: bool = True
    batch_size: int = Field(default=25, ge=1, le=200)
    initial_lookback_hours: int = Field(default=24, ge=0, le=24 * 30)
    retention_days: int = Field(default=30, ge=1)
