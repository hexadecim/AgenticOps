"""Pattern detection for structured identifiers. Also used as the leak check on tokenised output."""

from __future__ import annotations

import re
from collections.abc import Iterable

from ....domain.entities import EntityKind, Source, Span

EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
# International (+49 / 0049) or national (0…) numbers with optional separators; 8-15 digits.
PHONE = re.compile(r"(?<![\w+])(?:\+|00)?\(?0?\d[\d\s()./-]{6,}\d(?![\w])")
IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){2,7}(?:[ ]?[A-Z0-9]{1,3})?\b")


def _iban_valid(candidate: str) -> bool:
    compact = candidate.replace(" ", "")
    if not 15 <= len(compact) <= 34:
        return False
    rearranged = compact[4:] + compact[:4]
    digits = "".join(str(int(c, 36)) for c in rearranged)
    return int(digits) % 97 == 1


def _phone_valid(candidate: str) -> bool:
    digits = re.sub(r"\D", "", candidate)
    international = candidate.lstrip("(").startswith(("+", "00"))
    return 8 <= len(digits) <= 15 and (international or digits.startswith("0"))


class RegexDetector:
    def __init__(
        self, kinds: Iterable[EntityKind] = (EntityKind.EMAIL, EntityKind.PHONE, EntityKind.IBAN)
    ) -> None:
        self._kinds = frozenset(kinds)

    async def detect(self, text: str, language: str) -> list[Span]:
        return self.find(text)

    def find(self, text: str) -> list[Span]:
        spans: list[Span] = []
        if EntityKind.EMAIL in self._kinds:
            spans += [
                Span(m.start(), m.end(), EntityKind.EMAIL, m.group(0), Source.PATTERN)
                for m in EMAIL.finditer(text)
            ]
        if EntityKind.IBAN in self._kinds:
            spans += [
                Span(m.start(), m.end(), EntityKind.IBAN, m.group(0), Source.PATTERN)
                for m in IBAN.finditer(text)
                if _iban_valid(m.group(0))
            ]
        if EntityKind.PHONE in self._kinds:
            for m in PHONE.finditer(text):
                value = m.group(0).rstrip(" .-/")
                if _phone_valid(value):
                    spans.append(
                        Span(m.start(), m.start() + len(value), EntityKind.PHONE, value, Source.PATTERN)
                    )
        return spans
