"""Security ports, segregated by responsibility (ISP)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from opspilot_common.auth.principal import Principal


class PasswordHasher(Protocol):
    def hash(self, password: str) -> str: ...
    def verify(self, password_hash: str, password: str) -> bool: ...
    def needs_rehash(self, password_hash: str) -> bool: ...


class TotpProvider(Protocol):
    def new_secret(self) -> str: ...
    def provisioning_uri(self, secret: str, account: str) -> str: ...
    def verify(self, secret: str, code: str, now: datetime) -> int | None:
        """Return the matched time step, or None when the code is wrong."""
        ...


class SecretCipher(Protocol):
    def encrypt(self, plaintext: str) -> str: ...
    def decrypt(self, ciphertext: str) -> str: ...


class SessionTokenIssuer(Protocol):
    @property
    def ttl_seconds(self) -> int: ...
    def issue(self, principal: Principal) -> str: ...


class ChallengeTokens(Protocol):
    """Short-lived token proving the password step succeeded; required for the MFA step."""

    def issue(self, user_id: str) -> str: ...
    def verify(self, token: str) -> str: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
