"""Optional limit on where saved settings may point (admins are trusted, but an address they enter is fetched
by settings-service from inside the network). Empty: any host, as in development."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .errors import InvalidSettingsError
from .spec import SectionSpec

_URL = re.compile(r"https?://[^\s,]+")


@dataclass(frozen=True, slots=True)
class TargetPolicy:
    """`allowed`: exact host names, or `.example.com` for any host below it."""

    allowed: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> TargetPolicy:
        return cls(tuple(h.strip().lower() for h in text.split(",") if h.strip()))

    def _permits(self, host: str) -> bool:
        host = host.lower()
        return any(host.endswith(a) if a.startswith(".") else host == a for a in self.allowed)

    def _hosts(self, kind: str, value: Any) -> Iterable[str]:
        text = str(value or "")
        if kind == "host":
            return [text] if text else []
        return [urlsplit(u).hostname or "" for u in _URL.findall(text)]

    def check(self, spec: SectionSpec, values: Mapping[str, Any]) -> None:
        """Raise InvalidSettingsError naming each field that points to a host outside the list."""
        if not self.allowed:
            return
        errors = {
            f.key: "This host is not on the list of allowed hosts (ALLOWED_TARGET_HOSTS)."
            for f in spec.fields
            if any(not self._permits(h) for h in self._hosts(f.kind, values.get(f.key)))
        }
        if errors:
            raise InvalidSettingsError(errors)
