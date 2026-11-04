"""Where a service's token keys come from: one shared secret in development, key files in production.

`AUTH_KEYS_DIR` holds (see deploy/prod/generate-secrets.sh, ADR-0009):
    session_public.pem      every service
    session_private.pem     identity-service only
    service_private.pem     this service's own key
    services/<name>.pem     public keys of the services allowed to call anyone
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from .service import ServiceTokenIssuer, ServiceTokenVerifier
from .tokens import TokenIssuer, TokenVerifier

MIN_SECRET_LENGTH = 32


@dataclass(frozen=True, slots=True)
class AuthKeys:
    service: str
    secret: str
    directory: Path | None

    @classmethod
    def load(cls, service: str, secret: str, directory: str | None = None) -> AuthKeys:
        """Refuses to start without usable key material: a service must never run with no or a weak key."""
        path = Path(directory) if directory else None
        if path is None and len(secret) < MIN_SECRET_LENGTH:
            raise ValueError(f"Set AUTH_KEYS_DIR or a JWT_SECRET of at least {MIN_SECRET_LENGTH} characters")
        if path is not None and not (path / "session_public.pem").is_file():
            raise ValueError(f"No session_public.pem in AUTH_KEYS_DIR ({path})")
        return cls(service, secret, path)

    def _read(self, name: str) -> str:
        assert self.directory is not None
        return (self.directory / name).read_text()

    def session_issuer(self, ttl: timedelta) -> TokenIssuer:
        if self.directory is None:
            return TokenIssuer(self.secret, ttl=ttl)
        return TokenIssuer(self._read("session_private.pem"), algorithm="RS256", ttl=ttl)

    def session_verifier(self) -> TokenVerifier:
        if self.directory is None:
            return TokenVerifier(self.secret)
        return TokenVerifier(self._read("session_public.pem"), algorithm="RS256")

    def service_issuer(self) -> ServiceTokenIssuer:
        if self.directory is None:
            return ServiceTokenIssuer(self.secret, self.service)
        return ServiceTokenIssuer("", self.service, private_key=self._read("service_private.pem"))

    def service_verifier(self) -> ServiceTokenVerifier:
        if self.directory is None:
            return ServiceTokenVerifier(self.secret)
        keys = {p.stem: p.read_text() for p in (self.directory / "services").glob("*.pem")}
        return ServiceTokenVerifier("", public_keys=keys)
