"""Session JWTs: identity-service issues, every other service only verifies.

HS256 with a shared secret for development. In production RS256: verifiers hold only the public key, so no
other service can mint a session (`keys.AuthKeys`, ADR-0009).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt

from .principal import Principal
from .roles import Role

ISSUER = "opspilot-identity"


class InvalidTokenError(Exception):
    pass


class TokenIssuer:
    def __init__(
        self, secret: str, *, algorithm: str = "HS256", ttl: timedelta = timedelta(minutes=30)
    ) -> None:
        self._secret, self._algorithm, self._ttl = secret, algorithm, ttl

    def issue(self, principal: Principal) -> str:
        now = datetime.now(UTC)
        claims = {
            "iss": ISSUER,
            "sub": principal.user_id,
            "name": principal.name,
            "roles": sorted(r.value for r in principal.roles),
            "mfa": principal.mfa,
            "iat": now,
            "exp": now + self._ttl,
        }
        return jwt.encode(claims, self._secret, algorithm=self._algorithm)


class TokenVerifier:
    def __init__(self, key: str, *, algorithm: str = "HS256") -> None:
        self._key, self._algorithm = key, algorithm

    def verify(self, token: str) -> Principal:
        try:
            claims = jwt.decode(token, self._key, algorithms=[self._algorithm], issuer=ISSUER)
            return Principal(
                user_id=claims["sub"],
                name=claims.get("name", ""),
                roles=frozenset(Role(r) for r in claims.get("roles", [])),
                mfa=bool(claims.get("mfa", False)),
            )
        except (jwt.PyJWTError, KeyError, ValueError) as exc:
            raise InvalidTokenError(str(exc)) from exc
