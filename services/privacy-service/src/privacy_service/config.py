from __future__ import annotations

import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", secrets_dir=os.environ.get("SECRETS_DIR")
    )

    database_url: str = "postgresql+asyncpg://privacy_svc:privacy_dev_password@localhost:5432/opspilot"
    db_schema: str = "privacy"
    rabbitmq_url: str = "amqp://opspilot:opspilot@localhost:5672/"
    redis_url: str = "redis://localhost:6379/0"
    mail_url: str = "http://localhost:8003"
    orchestrator_url: str = "http://localhost:8008"
    approval_url: str = "http://localhost:8006"
    jira_url: str = "http://localhost:8007"
    reporting_url: str = "http://localhost:8009"
    audit_url: str = "http://localhost:8005"
    raw_email_days: int = 30  # shown on the schedule; mail-service's RETENTION_DAYS does the deleting
    vault_retention_days: int = 30
    vault_purge_enabled: bool = False  # the job deletes real identities for good: switched on deliberately
    jwt_secret: str = ""  # development; production uses auth_keys_dir (checked by AuthKeys.load)
    auth_keys_dir: str | None = None
    fernet_key: str = Field(min_length=44)
    resilience_config: str | None = None
    log_json: bool = True
    ner_enabled: bool = True  # Presidio + spaCy; off only in tests that do not need names from free text
