"""Concrete security adapters: argon2id, RFC 6238 TOTP, Fernet, JWT."""

from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta

import jwt
import pyotp
from argon2 import PasswordHasher as Argon2
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.fernet import Fernet
from opspilot_common.auth import AuthKeys, Principal

from ...domain.errors import ChallengeExpiredError


class Argon2PasswordHasher:
    def __init__(self) -> None:
        self._argon = Argon2()  # argon2id, library defaults (RFC 9106 low-memory profile)

    def hash(self, password: str) -> str:
        return self._argon.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        try:
            return self._argon.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        return self._argon.check_needs_rehash(password_hash)


class PyOtpTotpProvider:
    """6-digit, 30-second TOTP. Accepts one step of clock drift either way and reports the matched step."""

    def __init__(self, issuer: str = "OpsPilot", drift_steps: int = 1) -> None:
        self._issuer, self._drift = issuer, drift_steps

    def new_secret(self) -> str:
        return pyotp.random_base32()

    def provisioning_uri(self, secret: str, account: str) -> str:
        return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=self._issuer)

    def verify(self, secret: str, code: str, now: datetime) -> int | None:
        if not (len(code) == 6 and code.isdigit()):
            return None
        totp = pyotp.TOTP(secret)
        current = int(now.timestamp()) // totp.interval
        for step in range(current - self._drift, current + self._drift + 1):
            if hmac.compare_digest(totp.generate_otp(step), code):
                return step
        return None


class FernetSecretCipher:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode()).decode()


class JwtSessionIssuer:
    def __init__(self, keys: AuthKeys, ttl: timedelta) -> None:
        self._issuer = keys.session_issuer(ttl)
        self._ttl = ttl

    @property
    def ttl_seconds(self) -> int:
        return int(self._ttl.total_seconds())

    def issue(self, principal: Principal) -> str:
        return self._issuer.issue(principal)


class JwtChallengeTokens:
    """Signed with a key derived for this purpose only, so a challenge can never pass as a session."""

    _AUDIENCE = "opspilot-mfa-challenge"

    def __init__(self, secret: str, ttl: timedelta = timedelta(minutes=5)) -> None:
        self._key = hmac.new(secret.encode(), b"mfa-challenge", "sha256").hexdigest()
        self._ttl = ttl

    def issue(self, user_id: str) -> str:
        now = datetime.now(UTC)
        return jwt.encode(
            {"sub": user_id, "aud": self._AUDIENCE, "iat": now, "exp": now + self._ttl},
            self._key,
            algorithm="HS256",
        )

    def verify(self, token: str) -> str:
        try:
            return str(jwt.decode(token, self._key, algorithms=["HS256"], audience=self._AUDIENCE)["sub"])
        except jwt.PyJWTError as exc:
            raise ChallengeExpiredError() from exc


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
