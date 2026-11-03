from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    identity_url: str = "http://localhost:8001"
    settings_url: str = "http://localhost:8002"
    mail_url: str = "http://localhost:8003"
    privacy_url: str = "http://localhost:8004"
    audit_url: str = "http://localhost:8005"
    approval_url: str = "http://localhost:8006"
    orchestrator_url: str = "http://localhost:8008"
    reporting_url: str = "http://localhost:8009"
    ops_url: str = "http://localhost:8010"
    rabbitmq_url: str = "amqp://opspilot:opspilot@localhost:5672/"
    live_events: bool = True  # relay approval/run/jira events to browsers (SSE)
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = Field(min_length=32)
    resilience_config: str | None = None
    cookie_secure: bool = False  # True behind HTTPS in production
    sign_in_limit_per_minute: int = 10
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    log_json: bool = True
