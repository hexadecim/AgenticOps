from __future__ import annotations

import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", secrets_dir=os.environ.get("SECRETS_DIR")
    )

    database_url: str = (
        "postgresql+asyncpg://orchestrator_svc:orchestrator_dev_password@localhost:5432/opspilot"
    )
    db_schema: str = "orchestrator"
    rabbitmq_url: str = "amqp://opspilot:opspilot@localhost:5672/"
    redis_url: str = "redis://localhost:6379/0"
    settings_url: str = "http://localhost:8002"
    privacy_url: str = "http://localhost:8004"
    jira_connector_url: str = "http://localhost:8007"
    ops_url: str = "http://localhost:8010"
    jwt_secret: str = ""  # development; production uses auth_keys_dir (checked by AuthKeys.load)
    auth_keys_dir: str | None = None
    resilience_config: str | None = None
    log_json: bool = True
    worker_enabled: bool = True

    @property
    def checkpoint_url(self) -> str:
        """psycopg (LangGraph's saver) wants a plain postgresql:// URL.
        Its tables go to the role's search_path (the orchestrator schema)."""
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://")
