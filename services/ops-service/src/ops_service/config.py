from __future__ import annotations

import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", secrets_dir=os.environ.get("SECRETS_DIR")
    )

    database_url: str = "postgresql+asyncpg://ops_svc:ops_dev_password@localhost:5432/opspilot"
    db_schema: str = "ops"
    rabbitmq_url: str = "amqp://opspilot:opspilot@localhost:5672/"
    redis_url: str = "redis://localhost:6379/0"
    settings_url: str = "http://localhost:8002"
    jwt_secret: str = ""  # development; production uses auth_keys_dir (checked by AuthKeys.load)
    auth_keys_dir: str | None = None
    resilience_config: str | None = None
    # The simulated environment (regions, sites, services, probes, initial faults).
    mock_environment: str = "/etc/opspilot/ops/environment.yaml"
    log_json: bool = True
    worker_enabled: bool = True
    # Health probes run on this interval for the dashboard (0 = only during diagnostics and health checks).
    probe_interval_seconds: float = 60.0
