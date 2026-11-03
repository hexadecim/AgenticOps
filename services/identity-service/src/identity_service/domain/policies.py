"""Security policies as value objects, so they can be tuned without touching the entity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from .errors import WeakPasswordError


@dataclass(frozen=True, slots=True)
class LockoutPolicy:
    max_attempts: int = 5
    lock_for: timedelta = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    min_length: int = 12
    max_length: int = 128

    def validate(self, password: str, *, email: str = "") -> None:
        if len(password) < self.min_length:
            raise WeakPasswordError(f"Use at least {self.min_length} characters.")
        if len(password) > self.max_length:
            raise WeakPasswordError(f"Use at most {self.max_length} characters.")
        if password.strip() != password:
            raise WeakPasswordError("Passwords cannot start or end with spaces.")
        local = email.split("@")[0].lower()
        if len(local) >= 4 and local in password.lower():
            raise WeakPasswordError("Do not include your email name in the password.")
        if len(set(password)) < 6:
            raise WeakPasswordError("Use a less repetitive password.")
