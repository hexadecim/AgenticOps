"""Vault cryptography: Fernet for values at rest, HMAC-SHA256 (derived key) to find a value without storing it
in plaintext. Neither key ever leaves this service."""

from __future__ import annotations

import hashlib
import hmac

from cryptography.fernet import Fernet

from ...domain.entities import EntityKind, normalise


class VaultCrypto:
    def __init__(self, fernet_key: str) -> None:
        self._fernet = Fernet(fernet_key.encode())
        self._hmac_key = hmac.new(fernet_key.encode(), b"opspilot-vault-lookup-v1", hashlib.sha256).digest()

    def seal(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def open(self, blob: str) -> str:
        return self._fernet.decrypt(blob.encode()).decode()

    def lookup_hash(self, kind: EntityKind, value: str) -> str:
        message = f"{kind.value}\x1f{normalise(kind, value)}".encode()
        return hmac.new(self._hmac_key, message, hashlib.sha256).hexdigest()
