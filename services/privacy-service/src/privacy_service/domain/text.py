"""Text clean-up before detection: drop quoted history and signatures (they carry most contact details)."""

from __future__ import annotations

import re

SIGNATURE_REMOVED = "[signature removed]"
QUOTE_REMOVED = "[quoted text removed]"

_CLOSING = re.compile(
    r"^\s*(?P<closing>"
    r"best regards|kind regards|warm regards|regards|many thanks|thanks and regards|thank you|thanks|cheers|"
    r"sincerely|yours sincerely|best|"
    r"mit freundlichen gr(?:ü|ue)(?:ß|ss)en|freundliche gr(?:ü|ue)(?:ß|ss)e|viele gr(?:ü|ue)(?:ß|ss)e|"
    r"beste gr(?:ü|ue)(?:ß|ss)e|liebe gr(?:ü|ue)(?:ß|ss)e|gr(?:ü|ue)(?:ß|ss)e|danke|vielen dank|mfg|lg|vg"
    r")\s*[,.!]?(?P<rest>.*)$",
    re.IGNORECASE,
)
_DELIMITER = re.compile(r"^(--\s*|__+|sent from my .*|von meinem .* gesendet)$", re.IGNORECASE)
_QUOTE_START = re.compile(
    r"^(on .+ wrote:|am .+ schrieb .+:|-{2,}\s*original message\s*-{2,}"
    r"|-{2,}\s*urspr(?:ü|ue)ngliche nachricht\s*-{2,}"
    r"|from:\s.+|von:\s.+)$",
    re.IGNORECASE,
)
_SIGNATURE_WINDOW = 15  # a closing is looked for only near the end of the message


def _looks_like_signature(rest: str) -> bool:
    """After a closing word, only a short name may follow ("Regards, Alex Muster"), not a sentence."""
    rest = rest.strip()
    return not rest or (len(rest.split()) <= 4 and not re.search(r"[.?!:;]$|\d", rest))


def strip_quoted(body: str) -> tuple[str, bool]:
    """Cut previous messages quoted below a reply."""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if i > 0 and (_QUOTE_START.match(stripped) or stripped.startswith(">")):
            kept = "\n".join(lines[:i]).rstrip()
            return f"{kept}\n\n{QUOTE_REMOVED}" if kept else QUOTE_REMOVED, True
    return body, False


def strip_signature(body: str) -> tuple[str, bool]:
    """Keep the closing word ("Regards,"), replace what follows (name, phone, address) with a marker."""
    lines = body.rstrip().splitlines()
    content = [i for i, line in enumerate(lines) if line.strip()]
    if len(content) < 2:
        return body, False
    for i in content[1:][-_SIGNATURE_WINDOW:]:
        stripped = lines[i].strip()
        if _DELIMITER.match(stripped):
            return "\n".join([*lines[:i], SIGNATURE_REMOVED]).rstrip(), True
        match = _CLOSING.match(stripped)
        if match and _looks_like_signature(match.group("rest")):
            closing = stripped[: match.start("rest")].rstrip()  # "Regards," without the name after it
            return "\n".join([*lines[:i], closing, SIGNATURE_REMOVED]).rstrip(), True
    return body, False


_GERMAN = frozenset(
    [
        "der",
        "die",
        "das",
        "und",
        "ist",
        "nicht",
        "ich",
        "wir",
        "sie",
        "mit",
        "bitte",
        "seit",
        "kann",
        "können",
        "keine",
        "eine",
        "einen",
        "auf",
        "für",
        "von",
        "hallo",
        "danke",
        "grüße",
        "gruß",
        "wurde",
        "haben",
        "uns",
        "bei",
    ]
)
_ENGLISH = frozenset(
    [
        "the",
        "and",
        "is",
        "not",
        "we",
        "you",
        "with",
        "please",
        "since",
        "can",
        "cannot",
        "no",
        "an",
        "a",
        "on",
        "for",
        "from",
        "hello",
        "thanks",
        "regards",
        "was",
        "have",
        "us",
        "at",
    ]
)


def guess_language(text: str) -> str:
    """'de' or 'en', by common words; good enough to pick the NER model."""
    words = re.findall(r"[a-zäöüß]+", text.lower())
    german = sum(w in _GERMAN for w in words)
    english = sum(w in _ENGLISH for w in words)
    return "de" if german > english else "en"
