"""What privacy-service keeps about its own work: pseudonymisation records and identity reveals."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from opspilot_common.auth.roles import Role

REVEAL_DURATION = timedelta(minutes=5)
# People who work cases may see an identity when they need it; admins run the system, not cases.
REVEAL_ROLES = frozenset({Role.L1_ANALYST, Role.SUPPORT_AGENT, Role.SUPPORT_MANAGER, Role.COMPLIANCE_LEAD})
# Services that may turn tokens back into real values (only to send an approved message).
DETOKENISE_SERVICES = frozenset({"mail-service"})
# Services that may ask for an email to be pseudonymised.
PSEUDONYMISE_SERVICES = frozenset({"agent-orchestrator"})


@dataclass(frozen=True, slots=True)
class TokenisedAddress:
    email_token: str
    name_token: str | None = None

    def display(self) -> str:
        return f"[{self.name_token}] <[{self.email_token}]>" if self.name_token else f"[{self.email_token}]"


@dataclass(frozen=True, slots=True)
class TokenisedEmail:
    email_id: str
    thread_id: str
    sender: TokenisedAddress
    to: tuple[TokenisedAddress, ...]
    cc: tuple[TokenisedAddress, ...]
    subject: str
    body: str
    received_at: datetime
    language: str
    attachments: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class PseudonymisationRecord:
    id: str  # "vlt-…": the token map this email used
    email_id: str
    thread_id: str
    counts: dict[str, int]
    signature_removed: bool
    quoted_removed: bool
    email: TokenisedEmail
    created_at: datetime

    def summary(self) -> str:
        found = ", ".join(f"{k} ({n})" for k, n in sorted(self.counts.items()) if n) or "no identifiers"
        extra = [
            s
            for s, on in (
                ("signature stripped", self.signature_removed),
                ("quoted text removed", self.quoted_removed),
            )
            if on
        ]
        return f"{found} tokenised" + ("; " + "; ".join(extra) if extra else "")


@dataclass(frozen=True, slots=True)
class Reveal:
    id: str
    token: str
    user_id: str
    role: str
    reason: str
    created_at: datetime
    expires_at: datetime
    thread_id: str | None = None
    values: dict[str, str] = field(default_factory=dict, repr=False)  # in memory only, never stored
