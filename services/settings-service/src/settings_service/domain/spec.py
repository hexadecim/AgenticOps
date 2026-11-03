"""Field specifications: what each settings section contains, which fields are secret, and which only apply
to a given provider. The UI renders forms from these specs, so a new provider is a spec change only."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import InvalidSettingsError

Kind = Literal["text", "secret", "number", "bool", "select", "url", "email", "host"]

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+$")
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


@dataclass(frozen=True, slots=True)
class Option:
    value: str
    label: str


@dataclass(frozen=True, slots=True)
class FieldSpec:
    key: str
    label: str
    kind: Kind = "text"
    required: bool = False
    default: Any = None
    help: str = ""
    options: tuple[Option, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    when: tuple[tuple[str, str], ...] = ()  # e.g. (("provider", "imap"),): field applies only then
    placeholder: str = ""

    @property
    def secret(self) -> bool:
        return self.kind == "secret"

    def applies(self, values: dict[str, Any]) -> bool:
        return all(str(values.get(k)) == v for k, v in self.when)

    def coerce(self, raw: Any) -> Any:
        """Return the typed value or raise ValueError with a message for the person."""
        if raw is None or raw == "":
            return None
        if self.kind == "number":
            try:
                number = int(raw)
            except (TypeError, ValueError):
                raise ValueError("Enter a whole number.") from None
            if self.minimum is not None and number < self.minimum:
                raise ValueError(f"Use {self.minimum} or more.")
            if self.maximum is not None and number > self.maximum:
                raise ValueError(f"Use {self.maximum} or less.")
            return number
        if self.kind == "bool":
            if isinstance(raw, bool):
                return raw
            if str(raw).lower() in {"true", "1", "yes", "on"}:
                return True
            if str(raw).lower() in {"false", "0", "no", "off"}:
                return False
            raise ValueError("Choose yes or no.")
        text = str(raw).strip()
        if len(text) > 2000:
            raise ValueError("This value is too long.")
        if self.kind == "select" and text not in {o.value for o in self.options}:
            raise ValueError("Choose one of the listed options.")
        if self.kind == "url":
            if not re.match(r"^https?://[^\s/]+", text):
                raise ValueError(
                    "Enter a full address starting with https:// (or http:// for local services)."
                )
            return text.rstrip("/")
        if self.kind == "email" and not _EMAIL.match(text):
            raise ValueError("Enter a valid email address.")
        if self.kind == "host" and not _HOST.match(text):
            raise ValueError("Enter a host name such as imap.example.com.")
        return text


@dataclass(frozen=True, slots=True)
class SectionSpec:
    id: str
    title: str
    description: str
    fields: tuple[FieldSpec, ...] = field(default_factory=tuple)

    def field(self, key: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.key == key), None)

    @property
    def secret_keys(self) -> frozenset[str]:
        return frozenset(f.key for f in self.fields if f.secret)

    def defaults(self) -> dict[str, Any]:
        return {f.key: f.default for f in self.fields if not f.secret and f.default is not None}

    def normalise(
        self, values: dict[str, Any], secrets: dict[str, str]
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Coerce and validate a full candidate configuration. Unknown keys are dropped. Raises with every
        problem at once so the form can mark all fields."""
        errors: dict[str, str] = {}
        clean: dict[str, Any] = {}
        merged = {**self.defaults(), **{k: v for k, v in values.items() if v is not None}}
        for spec in self.fields:
            if spec.secret:
                continue
            try:
                value = spec.coerce(merged.get(spec.key))
            except ValueError as exc:
                errors[spec.key] = str(exc)
                continue
            if value is not None:
                clean[spec.key] = value
        clean_secrets = {k: v for k, v in secrets.items() if k in self.secret_keys and v}
        for spec in self.fields:
            if not spec.required or not spec.applies(clean) or spec.key in errors:
                continue
            present = clean_secrets.get(spec.key) if spec.secret else clean.get(spec.key)
            if present in (None, ""):
                errors[spec.key] = "This field is required."
        if errors:
            raise InvalidSettingsError(errors)
        return clean, clean_secrets
