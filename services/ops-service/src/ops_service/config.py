from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://ops_svc:ops_dev_password@localhost:5432/opspilot"
    db_schema: str = "ops"
    rabbitmq_url: str = "amqp://opspilot:opspilot@localhost:5672/"
    redis_url: str = "redis://localhost:6379/0"
    settings_url: str = "http://localhost:8002"
    jwt_secret: str = Field(min_length=32)
    resilience_config: str | None = None
    # The simulated environment (regions, sites, services, probes, initial faults).
    mock_environment: str = "/etc/opspilot/ops/environment.yaml"
    log_json: bool = True
    worker_enabled: bool = True
