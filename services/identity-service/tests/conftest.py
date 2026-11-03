from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyotp
import pytest
from cryptography.fernet import Fernet
from identity_service.config import Settings


class FakeClock:
    def __init__(self) -> None:
        self.current = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, **kwargs: float) -> None:
        self.current += timedelta(**kwargs)


def totp_code(secret: str, clock: FakeClock) -> str:
    return pyotp.TOTP(secret).at(clock.now())


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        jwt_secret="test-jwt-secret-that-is-at-least-32-chars",
        fernet_key=Fernet.generate_key().decode(),
        database_url="sqlite+aiosqlite:///:memory:",
        log_json=False,
    )
