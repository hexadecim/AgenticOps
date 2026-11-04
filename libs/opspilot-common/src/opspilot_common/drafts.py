"""Checks on a customer message at gate T3 (pure). The orchestrator runs them on Comms' draft;
approval-service runs the same code again on what the approver is about to send, so an edit cannot slip
past them.

A draft holds tokens until mail-service sends it: `[RECIPIENT_NAME]` is the only token allowed, because the
same text can go to several reporters and any other token would be another person's data. Detection is by
pattern, so a name typed as plain text is not caught: reading the draft remains the approver's job.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any

RECIPIENT_TOKEN = "[RECIPIENT_NAME]"
MAX_REVISIONS = 2  # how many times an approver may send a draft back to Comms
AI_DISCLOSURE = (
    "This reply was prepared with AI assistance and checked by a member of our support team. "
    "How we handle your data: see our privacy notice."
)

_ANY_TOKEN = re.compile(r"\[[A-Z][A-Z_]*(?:_\d+)?\]")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_PHONE = re.compile(r"(?<![\w-])\+\d[\d\s()/-]{6,}\d|(?<![\d-])\d{3,}[\s/-]\d{3,}[\s/-]?\d{2,}")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
_REFERENCE = re.compile(r"\b[A-Z][A-Z0-9]*-\d+\b")
_FIGURE = re.compile(r"\d+(?:[:.,]\d+)*")
_PROMISES = re.compile(
    r"\beta\b|\bby (?:tomorrow|today|tonight|the end|end of|next|\d"
    r"|(?:mon|tues|wednes|thurs|fri|satur|sun)day)"
    r"|\bwithin (?:the next )?(?:about |around |roughly |approximately )?"
    r"(?:\d+|an?|one|two|three|a few|few|several)\b"
    r"|\b(?:in|after) (?:the next )?(?:about |around |roughly |approximately |under |less than |another )?"
    r"(?:\d+|one|two|three|a few|a couple of|several|an?) (?:\w+ )?(?:minutes?|hours?|days?|weeks?)\b"
    r"|\b(?:will|shall|should) be (?:fixed|resolved|restored|back|working|available|done)\b"
    r"|\bguarantee|\bwe promise\b|\bas soon as\b|\bshortly\b|\bsoon\b|\bno later than\b"
    r"|\bexpected (?:to|by|at)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Check:
    id: str
    label: str
    ok: bool
    detail: str
    blocking: bool = True  # False: shown as a warning; the approver may still send


def _figures(text: str) -> set[str]:
    return set(_FIGURE.findall(text))


def _unsupported(text: str, facts: str) -> list[str]:
    """References and figures in the text that the facts do not contain."""
    text = _ANY_TOKEN.sub(" ", text.replace(AI_DISCLOSURE, " "))
    refs = set(_REFERENCE.findall(text))
    bad = refs - set(_REFERENCE.findall(facts))
    bare = _REFERENCE.sub(" ", text)
    known = _figures(facts)
    return sorted(bad | {f for f in _figures(bare) if f not in known})


def _personal_data(subject: str, body: str) -> list[str]:
    found: list[str] = []
    for text in (subject, body):
        found += [t for t in _ANY_TOKEN.findall(text) if t != RECIPIENT_TOKEN and t not in found]
    text = f"{subject}\n{body}"
    for label, pattern in (("an email address", _EMAIL), ("a phone number", _PHONE), ("an IBAN", _IBAN)):
        if pattern.search(text):
            found.append(label)
    return found


def _recipients(recipients: Iterable[str], linked: Iterable[str | None]) -> Check:
    to, should = set(recipients), list(linked)
    known = {r for r in should if r}
    matching = len(to & known)
    ok = bool(to) and to == known and len(known) == len(set(should))
    detail = f"{matching} of {len(set(should))}"
    if None in should:
        detail += "; a linked report has no stored address"
    return Check("recipients", "Recipients match linked reporters", ok, detail)


def check_draft(subject: str, body: str, context: Mapping[str, Any], *, edited: bool = False) -> list[Check]:
    """`context`: jira_key, facts (text the draft may state), recipients (tokens), linked (tokens, None for a
    linked report without a stored address). `edited`: the approver changed the text, so a figure the ticket
    does not contain is their statement to make: a warning, not a stop."""
    key = str(context.get("jira_key") or "")
    facts = f"{context.get('facts') or ''}\n{key}"
    unsupported = _unsupported(f"{subject}\n{body}", facts)
    promises = sorted({m.group(0).lower() for m in _PROMISES.finditer(body.replace(AI_DISCLOSURE, " "))})
    personal = _personal_data(subject, body)
    flat = " ".join(body.split())
    return [
        Check(
            "facts",
            "Facts match the ticket",
            not unsupported,
            f"not in {key or 'the ticket'}: {', '.join(unsupported)}"
            if unsupported
            else f"every reference and figure is in {key or 'the ticket'}",
            blocking=not edited,
        ),
        Check(
            "promises",
            "No promises or ETAs",
            not promises,
            f"remove: {', '.join(promises)}" if promises else "none found",
        ),
        Check(
            "personal_data",
            "No other personal data",
            not personal,
            f"remove: {', '.join(personal)}" if personal else "only the recipient token",
        ),
        _recipients(context.get("recipients") or [], context.get("linked") or []),
        Check(
            "disclosure",
            "AI disclosure and privacy notice included",
            AI_DISCLOSURE in flat,
            "EU AI Act and GDPR transparency" if AI_DISCLOSURE in flat else "restore the closing sentences",
        ),
    ]


def blocking(checks: Iterable[Check]) -> list[Check]:
    return [c for c in checks if not c.ok and c.blocking]


def checks_json(checks: Iterable[Check]) -> list[dict[str, Any]]:
    return [asdict(c) for c in checks]
