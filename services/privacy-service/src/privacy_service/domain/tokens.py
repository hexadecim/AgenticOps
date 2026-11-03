"""Tokens like `[PERSON_7]`: stable per person across all emails, meaningless without the vault."""

from __future__ import annotations

import re

from .entities import EntityKind

TOKEN_IN_TEXT = re.compile(r"\[(PERSON|EMAIL|PHONE|IBAN)_(\d+)\]")


def make_token(kind: EntityKind, number: int) -> str:
    return f"{kind.value}_{number}"


def bracket(token: str) -> str:
    return f"[{token}]"


def parse_token(value: str) -> str | None:
    """Accept `PERSON_7` or `[PERSON_7]`; return the bare token or None."""
    match = re.fullmatch(r"\[?((?:PERSON|EMAIL|PHONE|IBAN)_\d+)\]?", value.strip())
    return match.group(1) if match else None


def tokens_in(text: str) -> list[str]:
    return list(dict.fromkeys(f"{m.group(1)}_{m.group(2)}" for m in TOKEN_IN_TEXT.finditer(text)))
