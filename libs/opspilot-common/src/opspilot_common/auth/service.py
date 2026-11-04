"""Service-to-service tokens for internal APIs (`/internal/...`), never exposed through the gateway.

Their own issuer and audience, so a user session token is never accepted as a service token and a service
token never as a user session. Development: HS256 with a key *derived* from the shared secret. Production:
RS256 with one private key per service, so a compromised one cannot name another (`keys.AuthKeys`, ADR-0009).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Collection, Mapping

import jwt

from .tokens import InvalidTokenError

SERVICE_ISSUER = "opspilot-service"
INTERNAL_AUDIENCE = "opspilot-internal"


def _derive(secret: str) -> bytes:
    return hmac.new(secret.encode(), b"opspilot-service-token-v1", hashlib.sha256).digest()


class ServiceTokenIssuer:
    """Mints short-lived tokens naming the calling service; reuses one until shortly before it expires."""

    def __init__(
        self, secret: str, service: str, *, ttl_seconds: int = 300, private_key: str | None = None
    ) -> None:
        self._key: str | bytes = private_key or _derive(secret)
        self._algorithm = "RS256" if private_key else "HS256"
        self._service, self._ttl = service, ttl_seconds
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
            self._token = jwt.encode(claims, self._key, algorithm=self._algorithm)
        return self._token

    def header(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}


class ServiceTokenVerifier:
    def __init__(self, secret: str, *, public_keys: Mapping[str, str] | None = None) -> None:
        self._public_keys = public_keys
        self._key = _derive(secret) if public_keys is None else b""

    def _decode(self, token: str) -> dict[str, object]:
        if self._public_keys is None:
            key: str | bytes = self._key
            algorithm = "HS256"
        else:
            # The token names its service; only that service's public key can confirm it.
            named = jwt.decode(token, options={"verify_signature": False}).get("sub")
            found = self._public_keys.get(str(named))
            if found is None:
                raise InvalidTokenError(f"unknown service {named}")
            key, algorithm = found, "RS256"
        return dict(
            jwt.decode(token, key, algorithms=[algorithm], issuer=SERVICE_ISSUER, audience=INTERNAL_AUDIENCE)
        )

    def verify(self, token: str, allowed: Collection[str] | None = None) -> str:
        """Return the calling service's name; raise InvalidTokenError when invalid or not allowed."""
        try:
            service = str(self._decode(token)["sub"])
        except (jwt.PyJWTError, KeyError) as exc:
            raise InvalidTokenError(str(exc)) from exc
        if allowed is not None and service not in allowed:
            raise InvalidTokenError(f"service {service} is not allowed here")
        return service
