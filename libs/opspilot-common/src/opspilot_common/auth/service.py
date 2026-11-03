"""Service-to-service tokens for internal APIs (`/internal/...`), never exposed through the gateway.

Signed with a key *derived* from the shared secret, with their own issuer and audience, so a user session
token is never accepted as a service token and a service token is never accepted as a user session.
HS256 for development; moves to RS256 with per-service keys together with session tokens (ADR-0001).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Collection

import jwt

from .tokens import InvalidTokenError

SERVICE_ISSUER = "opspilot-service"
INTERNAL_AUDIENCE = "opspilot-internal"


def _derive(secret: str) -> bytes:
    return hmac.new(secret.encode(), b"opspilot-service-token-v1", hashlib.sha256).digest()


class ServiceTokenIssuer:
    """Mints short-lived tokens naming the calling service; reuses one until shortly before it expires."""

    def __init__(self, secret: str, service: str, *, ttl_seconds: int = 300) -> None:
        self._key, self._service, self._ttl = _derive(secret), service, ttl_seconds
        self._token, self._expires = "", 0.0

    @property
    def service(self) -> str:
        return self._service

    def token(self) -> str:
        now = time.time()
        if now > self._expires - 60:
            self._expires = now + self._ttl
            claims = {
                "iss": SERVICE_ISSUER,
                "aud": INTERNAL_AUDIENCE,
                "sub": self._service,
                "iat": int(now),
                "exp": int(self._expires),
            }
            self._token = jwt.encode(claims, self._key, algorithm="HS256")
        return self._token

    def header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}


class ServiceTokenVerifier:
    def __init__(self, secret: str) -> None:
        self._key = _derive(secret)

    def verify(self, token: str, allowed: Collection[str] | None = None) -> str:
        """Return the calling service's name; raise InvalidTokenError when invalid or not allowed."""
        try:
            claims = jwt.decode(
                token, self._key, algorithms=["HS256"], issuer=SERVICE_ISSUER, audience=INTERNAL_AUDIENCE
            )
            service = str(claims["sub"])
        except (jwt.PyJWTError, KeyError) as exc:
            raise InvalidTokenError(str(exc)) from exc
        if allowed is not None and service not in allowed:
            raise InvalidTokenError(f"service {service} is not allowed here")
        return service
