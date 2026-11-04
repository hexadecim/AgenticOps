from __future__ import annotations

import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", secrets_dir=os.environ.get("SECRETS_DIR")
    )

    database_url: str = "postgresql+asyncpg://jira_svc:jira_dev_password@localhost:5432/opspilot"
    db_schema: str = "jira"
    rabbitmq_url: str = "amqp://opspilot:opspilot@localhost:5672/"
    redis_url: str = "redis://localhost:6379/0"
    settings_url: str = "http://localhost:8002"
    jwt_secret: str = ""  # development; production uses auth_keys_dir (checked by AuthKeys.load)
    auth_keys_dir: str | None = None
    resilience_config: str | None = None
    log_json: bool = True
    worker_enabled: bool = True
