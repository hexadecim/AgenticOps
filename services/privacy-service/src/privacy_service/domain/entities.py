"""Personal data found in text, and how overlapping findings are resolved. Pure logic, no I/O."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import StrEnum


class EntityKind(StrEnum):
    PERSON = "PERSON"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    IBAN = "IBAN"


class Source(StrEnum):
    """Where a finding came from; on overlap the more reliable source wins."""

    HEADER = "header"  # names/addresses from From/To/Cc: certain
    PATTERN = "pattern"  # regular expressions with validation
    NER = "ner"  # statistical named-entity recognition


_PRIORITY = {Source.HEADER: 3, Source.PATTERN: 2, Source.NER: 1}


@dataclass(frozen=True, slots=True)
class Span:
    start: int
    end: int
    kind: EntityKind
    value: str  # what is stored in the vault (for a first name alone: the full name it belongs to)
    source: Source
    subject: str | None = None  # groups values of one person (their email address), if known

    @property
    def length(self) -> int:
        return self.end - self.start


def normalise(kind: EntityKind, value: str) -> str:
    """The comparison key: the same person written differently gets the same token."""
    if kind is EntityKind.EMAIL:
        return value.strip().lower()
    if kind is EntityKind.PHONE:
        digits = re.sub(r"\D", "", value)
        return (
            "+" + digits[2:]
            if digits.startswith("00")
            else ("+" + digits if value.strip()[:1] == "+" else digits)
        )
    if kind is EntityKind.IBAN:
        return re.sub(r"\s", "", value).upper()
    return " ".join(value.split()).casefold()


def merge(spans: list[Span]) -> list[Span]:
    """Non-overlapping spans, left to right. Overlaps keep the more reliable source, then the longer span."""
    ranked = sorted(spans, key=lambda s: (-_PRIORITY[s.source], -s.length, s.start))
    chosen: list[Span] = []
    for span in ranked:
        if span.length > 0 and all(span.end <= c.start or span.start >= c.end for c in chosen):
            chosen.append(span)
    return sorted(chosen, key=lambda s: s.start)


# Words that open a greeting and are not part of the name ("Hello Alex" → "Alex").
_GREETINGS = frozenset(
    [
        "hi",
        "hello",
        "hey",
        "dear",
        "hallo",
        "liebe",
        "lieber",
        "sehr",
        "geehrte",
        "geehrter",
        "frau",
        "herr",
        "mr",
        "mrs",
        "ms",
        "dr",
        "guten",
        "tag",
        "morgen",
        "moin",
        "servus",
        "grüezi",
        "bonjour",
        "ciao",
        "good",
        "morning",
        "afternoon",
        "evening",
    ]
)
# Words that look like names to a statistical model but address a role, not a person.
_NOT_NAMES = frozenset(
    [
        "support",
        "team",
        "kunde",
        "kunden",
        "service",
        "helpdesk",
        "desk",
        "admin",
        "administrator",
        "operations",
        "ops",
        "it",
        "customer",
        "customers",
        "colleagues",
        "kollegen",
        "all",
        "everyone",
        "zusammen",
    ]
)


def clean_person(text: str, start: int, end: int) -> tuple[int, int] | None:
    """Trim greeting words off a PERSON finding; None when nothing name-like is left."""
    segment = text[start:end]
    words = list(re.finditer(r"[^\W\d_][\w'\u2019-]*", segment))
    while words and words[0].group(0).casefold() in _GREETINGS:
        words.pop(0)
    while words and words[-1].group(0).casefold() in _GREETINGS:
        words.pop()
    if not words or all(w.group(0).casefold() in _NOT_NAMES for w in words):
        return None
    new_start, new_end = start + words[0].start(), start + words[-1].end()
    return (new_start, new_end) if new_end - new_start >= 2 else None


def clean_people(text: str, spans: list[Span]) -> list[Span]:
    cleaned: list[Span] = []
    for span in spans:
        if span.kind is not EntityKind.PERSON or span.source is Source.HEADER:
            cleaned.append(span)
            continue
        trimmed = clean_person(text, span.start, span.end)
        if trimmed:
            cleaned.append(
                replace(span, start=trimmed[0], end=trimmed[1], value=text[trimmed[0] : trimmed[1]])
            )
    return cleaned


@dataclass(frozen=True, slots=True)
class Identity:
    """A person named in the headers: their address and display name are certainly personal data."""

    email: str
    name: str = ""

    @property
    def person_name(self) -> str | None:
        """The display name if it names a person; None for "Support", "IT Team" or an address."""
        name = " ".join(self.name.split())
        if not name or "@" in name or all(w.casefold() in _NOT_NAMES for w in re.findall(r"\w+", name)):
            return None
        return name


def identity_spans(text: str, identities: list[Identity]) -> list[Span]:
    """Every place the text mentions a header identity: address, full name, or a name part (≥ 3 letters)."""
    spans: list[Span] = []
    for ident in identities:
        subject = normalise(EntityKind.EMAIL, ident.email)
        if ident.email:
            for m in re.finditer(re.escape(ident.email), text, re.IGNORECASE):
                spans.append(Span(m.start(), m.end(), EntityKind.EMAIL, ident.email, Source.HEADER, subject))
        name = ident.person_name
        if name is None:
            continue
        parts = [name] + [p for p in name.split(" ") if len(p) >= 3 and p[:1].isupper()]
        for part in dict.fromkeys(parts):
            for m in re.finditer(rf"(?<![\w@.]){re.escape(part)}(?![\w@])", text):
                spans.append(Span(m.start(), m.end(), EntityKind.PERSON, name, Source.HEADER, subject))
    return spans
