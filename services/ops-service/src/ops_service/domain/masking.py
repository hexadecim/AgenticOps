"""Masking for log lines: personal data and secrets never leave ops-service. Pure.

Masking is deliberately aggressive: a log finding is evidence for a fix, and `user=***` is as useful to the
approver as the real name. Every line returned by the log search goes through `mask`.
"""

from __future__ import annotations

import re

MASK = "***"

_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # Secrets first, so a token that looks like an e-mail address is still masked as a secret.
    (re.compile(r"(?i)\b(authorization:\s*bearer)\s+\S+"), rf"\1 {MASK}"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|session)=\S+"), rf"\1={MASK}"),
    # Phone numbers: international (+49 30 1234567) or national with a separator (030 1234567). Not
    # dates or times, which logs are full of.
    (re.compile(r"\+\d[\d ()/-]{7,}\d"), MASK),
    (re.compile(r"\b0\d{2,4}[ /]\d{4,}\b"), MASK),
    # key=value pairs that name a person or a contact detail
    (
        re.compile(
            r"(?i)\b(user|username|user_name|login|customer|name|email|mail|phone|tel|reporter)=[^\s,;)\]]+"
        ),
        rf"\1={MASK}",
    ),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), MASK),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "x.x.x.x"),
    (re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){4,7}[0-9a-fA-F]{1,4}\b"), "x:x:x:x"),
    (re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}\b"), MASK),  # IBAN
)

MAX_LINE = 300


def mask(line: str) -> str:
    text = line.rstrip("\n")[:MAX_LINE]
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text
