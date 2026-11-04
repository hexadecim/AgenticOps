from __future__ import annotations

import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", secrets_dir=os.environ.get("SECRETS_DIR")
    )

    database_url: str = "postgresql+asyncpg://settings_svc:settings_dev_password@localhost:5432/opspilot"
    db_schema: str = "settings"
    rabbitmq_url: str = "amqp://opspilot:opspilot@localhost:5672/"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = ""  # development; production uses auth_keys_dir (checked by AuthKeys.load)
    auth_keys_dir: str | None = None
    # Hosts settings may point to, comma-separated (`.example.com` for a domain). Empty: any host.
    allowed_target_hosts: str = ""
    fernet_key: str = Field(min_length=44)
    resilience_config: str | None = None
    log_json: bool = True
