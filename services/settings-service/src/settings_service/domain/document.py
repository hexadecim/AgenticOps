"""SettingsDocument: one section's saved configuration. Secrets are plaintext only in memory; the repository
adapter encrypts them at rest."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .spec import SectionSpec


@dataclass(slots=True)
class SettingsDocument:
    section: str
    version: int = 0
    values: dict[str, Any] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)
    updated_at: datetime | None = None
    updated_by: str | None = None

    def candidate(
        self, values: dict[str, Any], secret_updates: dict[str, str | None]
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Merge an edit: secrets not mentioned keep their value, `""` clears one, a string replaces it."""
        secrets = dict(self.secrets)
        for key, value in secret_updates.items():
            if value is None:
                continue
            if value == "":
                secrets.pop(key, None)
            else:
                secrets[key] = value
        return values, secrets

    def replace(
        self, spec: SectionSpec, values: dict[str, Any], secrets: dict[str, str], *, now: datetime, actor: str
    ) -> list[str]:
        """Apply validated values; return the names of fields that changed (never their values)."""
        # Keep only fields that apply to the chosen options (e.g. drop IMAP fields after switching to Graph).
        new_values = {
            k: v for k, v in values.items() if (f := spec.field(k)) is not None and f.applies(values)
        }
        new_secrets = {
            k: v for k, v in secrets.items() if (f := spec.field(k)) is not None and f.applies(values)
        }
        changed = sorted(
            {k for k in new_values.keys() | self.values.keys() if new_values.get(k) != self.values.get(k)}
            | {
                k
                for k in new_secrets.keys() | self.secrets.keys()
                if new_secrets.get(k) != self.secrets.get(k)
            }
        )
        if changed:
            self.values, self.secrets = new_values, new_secrets
            self.version += 1
            self.updated_at, self.updated_by = now, actor
        return changed
