"""What Intake must produce, how duplicates are scored, and the ticket proposal shown at gate T1. Pure."""

from __future__ import annotations

import math
import re
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator

DUPLICATE_THRESHOLD = 0.80
SUMMARY_MAX = 100
_EMPTY = frozenset({"", "none", "null", "n/a", "na", "unknown", "-", "not specified", "not mentioned"})
DEDUPE_HOURS = 24
_APPROVAL_NS = uuid.UUID("7b0f3a52-6f1e-4c1c-9a57-0b5f1d2e8c11")


def shorten(text: str, limit: int) -> str:
    """Collapse whitespace and cut at a word boundary."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,.;:")
    return f"{cut}…"


# Length limits are part of the JSON schema the model must follow: Ollama turns them into a grammar, so a
# reasoning model cannot ramble inside a field (it did, at 14 tokens/s, until the 180 s timeout).
LIMITS = {
    "category": 40,
    "summary": SUMMARY_MAX,
    "site": 24,
    "asset": 32,
    "error": 120,
    "label": 30,
    "value": 100,
}
MAX_FACTS, MAX_MISSING, MISSING_LEN = 6, 5, 60


_NOTHING = re.compile(r"^\s*(none|n/?a|not\b|no\b|unknown|unspecified|not specified|-+\s*$)", re.IGNORECASE)


def _says_nothing(value: str) -> bool:
    """'None reported in email', 'Not mentioned', 'N/A' ... mean there is no value."""
    return value.strip().lower() in _EMPTY or bool(_NOTHING.match(value))


def identifier(value: str) -> str:
    """'SITE-A014 (machine M-221' → 'SITE-A014': keep the leading id when the model added prose after it."""
    first = value.strip().split(" ", 1)[0].strip(",.;:()[]")
    has_id_shape = bool(re.search(r"\d", first)) and ("-" in first or first.isalnum())
    return first if " " in value.strip() and has_id_shape else value.strip()


def _fit(value: object, limit: int) -> object:
    """Normalise model output before validation: trim, cut overlong text at a word boundary."""
    return shorten(value, limit) if isinstance(value, str) else value


class Fact(BaseModel):
    label: str = Field(
        max_length=LIMITS["label"], description="Short field name, e.g. 'Machine', 'Upload time'"
    )
    value: str = Field(max_length=LIMITS["value"], description="What the email says; keep tokens unchanged")

    @field_validator("label", "value", mode="before")
    @classmethod
    def _fit(cls, value: object, info: ValidationInfo) -> object:
        return _fit(value, LIMITS[str(info.field_name)])


class Intake(BaseModel):
    """The structured triage the local model fills in (json_schema output). Input and output are tokenised."""

    is_support_request: bool = Field(
        description="True only for a customer or colleague asking support for help with a product or service "
        "problem. False for newsletters, marketing, receipts, notifications, personal or spam mail."
    )
    category: str = Field(
        max_length=LIMITS["category"],
        description="Short problem category, e.g. 'upload failure', 'login or access'",
    )
    severity: Literal["sev1", "sev2", "sev3", "sev4"] = Field(
        description="sev1 = outage for many users or data loss; sev2 = a core function broken for a site; "
        "sev3 = one user or a workaround exists; sev4 = question or minor issue"
    )
    summary: str = Field(max_length=SUMMARY_MAX, description="One short line in English, no personal data")
    site: str | None = Field(
        default=None, max_length=LIMITS["site"], description="Only a site id the email names, e.g. SITE-A014"
    )
    asset: str | None = Field(
        default=None, max_length=LIMITS["asset"], description="Only a machine or system id, e.g. M-2210"
    )
    error: str | None = Field(
        default=None, max_length=LIMITS["error"], description="The error in a few words"
    )
    facts: list[Fact] = Field(
        default_factory=list, max_length=MAX_FACTS, description="Further facts, at most 6"
    )
    missing_details: list[str] = Field(
        default_factory=list,
        max_length=MAX_MISSING,
        description="Details support needs but the email does not give, at most 5 short items",
    )

    # Small models write "None" or "n/a" instead of null and overrun limits: normalise before validating.
    @field_validator("site", "asset", "error", mode="before")
    @classmethod
    def _empty_is_none(cls, value: object, info: ValidationInfo) -> object:
        if isinstance(value, str) and _says_nothing(value):
            return None
        if info.field_name in ("site", "asset") and isinstance(value, str):
            value = identifier(value)
        return _fit(value, LIMITS[str(info.field_name)])

    @field_validator("category", "summary", mode="before")
    @classmethod
    def _fit_text(cls, value: object, info: ValidationInfo) -> object:
        return _fit(value, LIMITS[str(info.field_name)])

    @field_validator("facts", mode="before")
    @classmethod
    def _few_facts(cls, value: object) -> object:
        return value[:MAX_FACTS] if isinstance(value, list) else value

    @field_validator("missing_details", mode="before")
    @classmethod
    def _no_empty_details(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        items = [str(v).strip() for v in value if str(v).strip() and str(v).strip().lower() not in _EMPTY]
        return [shorten(v, MISSING_LEN) for v in items][:MAX_MISSING]


def approval_id_for(run_id: str, gate: str) -> str:
    """Deterministic, so a replayed node cannot create a second approval for the same gate."""
    return str(uuid.uuid5(_APPROVAL_NS, f"{run_id}:{gate}"))


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def best_match(
    query: list[float], candidates: list[tuple[dict[str, Any], list[float]]]
) -> tuple[dict[str, Any], float] | None:
    scored = [(issue, cosine(query, vector)) for issue, vector in candidates]
    return max(scored, key=lambda s: s[1]) if scored else None


def ticket_summary(intake: Intake) -> str:
    summary = intake.summary or intake.category
    return f"[{intake.site}] {summary}" if intake.site else summary


def ticket_description(intake: Intake, email: dict[str, Any]) -> str:
    lines = [intake.summary, ""]
    for label, value in (
        ("Category", intake.category),
        ("Severity", intake.severity),
        ("Site", intake.site),
        ("Asset", intake.asset),
        ("Error", intake.error),
    ):
        if value:
            lines.append(f"{label}: {value}")
    lines += [f"{f.label}: {f.value}" for f in intake.facts[:6]]
    if intake.missing_details:
        lines.append(f"Missing details: {', '.join(intake.missing_details)}")
    lines += ["", f"Customer email (pseudonymised), received {email.get('received_at', '')}:"]
    lines += [f"Subject: {email.get('subject', '')}", "", str(email.get("body", ""))]
    return "\n".join(lines)


def dedupe_detail(dedupe: dict[str, Any]) -> str:
    if dup := dedupe.get("duplicate_of"):
        return f"possible duplicate of {dup['key']} ({dup['score']:.2f})"
    if not dedupe.get("checked"):
        return "duplicates not checked"
    return "no related open ticket"


def build_proposal(
    *,
    email: dict[str, Any],
    privacy: dict[str, Any],
    intake: Intake,
    dedupe: dict[str, Any],
    jira: dict[str, Any],
    model: dict[str, Any],
) -> dict[str, Any]:
    sender = email.get("from", {})
    return {
        "action": "link" if dedupe.get("duplicate_of") else "create",
        "ticket": {
            "summary": ticket_summary(intake),
            "description": ticket_description(intake, email),
            "severity": intake.severity,
            "site": intake.site,
            "category": intake.category,
        },
        "duplicate_of": dedupe.get("duplicate_of"),
        "dedupe": dedupe,
        "intake": intake.model_dump(),
        "email": email,
        "privacy": privacy,
        "jira": jira,
        "model": model,
        "reporter_token": sender.get("name_token") or sender.get("email_token"),
    }
