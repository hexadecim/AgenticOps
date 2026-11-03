from __future__ import annotations

from privacy_service.adapters.outbound.detectors.regex import RegexDetector
from privacy_service.domain.entities import (
    EntityKind,
    Identity,
    Source,
    Span,
    clean_person,
    identity_spans,
    merge,
    normalise,
)
from privacy_service.domain.text import (
    QUOTE_REMOVED,
    SIGNATURE_REMOVED,
    guess_language,
    strip_quoted,
    strip_signature,
)
from privacy_service.domain.tokens import parse_token, tokens_in


def test_signature_after_closing_word_is_removed_but_the_closing_kept() -> None:
    body = "Hello support,\n\nUpload stuck at 0%.\n\nRegards,\nAlex Muster\nACME GmbH\n+49 40 1234567"
    text, removed = strip_signature(body)
    assert removed and text == f"Hello support,\n\nUpload stuck at 0%.\n\nRegards,\n{SIGNATURE_REMOVED}"


def test_german_and_inline_closings() -> None:
    text, _ = strip_signature(
        "Hallo,\nVPN geht nicht.\nMit freundlichen Grüßen\nJonas Weber\nTel. 040 1234567"
    )
    assert text.endswith(f"Mit freundlichen Grüßen\n{SIGNATURE_REMOVED}") and "Jonas" not in text
    text, _ = strip_signature("Hi,\nportal is down.\nRegards, Alex Muster")
    assert text == f"Hi,\nportal is down.\nRegards,\n{SIGNATURE_REMOVED}"
    text, _ = strip_signature("Hi,\nportal is down.\n-- \nAlex | ACME | +49 40 1234567")
    assert text == f"Hi,\nportal is down.\n{SIGNATURE_REMOVED}"


def test_a_sentence_starting_with_thanks_is_content_not_a_signature() -> None:
    body = "Hi,\nThanks for looking into it.\nThe error comes back every hour since Monday."
    assert strip_signature(body) == (body, False)


def test_quoted_history_is_cut() -> None:
    for marker in (
        "On Tue, 8 Oct 2026 Alex Muster <a@x.eu> wrote:",
        "Am 08.10.2026 schrieb Jonas Weber:",
        "> old",
    ):
        text, removed = strip_quoted(f"Still broken.\n\n{marker}\n> earlier mail with a@x.eu")
        assert removed and text == f"Still broken.\n\n{QUOTE_REMOVED}"


def test_language_guess() -> None:
    assert guess_language("Hallo, seit heute kann ich mich nicht anmelden. Bitte um Hilfe.") == "de"
    assert guess_language("Hello, since this morning we cannot log in. Please help.") == "en"


def test_merge_prefers_reliable_then_longer_spans() -> None:
    header = Span(0, 11, EntityKind.PERSON, "Alex Muster", Source.HEADER)
    ner = Span(0, 4, EntityKind.PERSON, "Alex", Source.NER)
    phone = Span(20, 33, EntityKind.PHONE, "040 123 45 67", Source.PATTERN)
    short = Span(24, 33, EntityKind.PHONE, "123 45 67", Source.NER)
    assert merge([ner, short, header, phone]) == [header, phone]


def test_greetings_and_role_words_are_not_names() -> None:
    text = "Hallo Support, Hello Alex, Liebe Petra Hoffmann"
    assert clean_person(text, 0, 13) is None  # "Hallo Support"
    start, end = clean_person(text, 15, 25)  # type: ignore[misc]
    assert text[start:end] == "Alex"
    start, end = clean_person(text, 27, 47)  # type: ignore[misc]
    assert text[start:end] == "Petra Hoffmann"


def test_header_identities_are_found_by_address_full_name_and_name_parts() -> None:
    text = "Alex wrote from alex.muster@example.eu; Muster is on leave. Alexander is someone else."
    spans = identity_spans(text, [Identity("alex.muster@example.eu", "Alex Muster")])
    found = sorted((text[s.start : s.end], s.kind.value, s.value) for s in spans)
    assert found == [
        ("Alex", "PERSON", "Alex Muster"),
        ("Muster", "PERSON", "Alex Muster"),
        ("alex.muster@example.eu", "EMAIL", "alex.muster@example.eu"),
    ]


def test_normalisation_makes_variants_one_value() -> None:
    assert normalise(EntityKind.PHONE, "+49 (40) 123-4567") == normalise(EntityKind.PHONE, "0049 40 1234567")
    assert normalise(EntityKind.EMAIL, " Alex.Muster@Example.EU ") == "alex.muster@example.eu"
    assert normalise(EntityKind.PERSON, "Alex  MUSTER") == normalise(EntityKind.PERSON, "alex muster")


def test_patterns_find_identifiers_but_not_dates_versions_or_asset_ids() -> None:
    text = (
        "Call +49 40 1234567 or 040 123 45 67, mail jonas.weber@kunde.de, IBAN DE89 3704 0044 0532 0130 00. "
        "Since 2026-10-08 08:40 on M-2210 (v3.8.16, 10.0.0.12), order 12345678, "
        "bad IBAN DE00 1234 5678 9012 34."
    )
    found = sorted((s.kind.value, s.value) for s in RegexDetector().find(text))
    assert found == [
        ("EMAIL", "jonas.weber@kunde.de"),
        ("IBAN", "DE89 3704 0044 0532 0130 00"),
        ("PHONE", "+49 40 1234567"),
        ("PHONE", "040 123 45 67"),
    ]


def test_tokens_parse_and_list() -> None:
    assert parse_token("[PERSON_1]") == parse_token("PERSON_1") == "PERSON_1"
    assert parse_token("PERSON_X") is None and parse_token("[SECRET_1]") is None
    assert tokens_in("Dear [PERSON_1], we called [PHONE_2] and [PERSON_1].") == ["PERSON_1", "PHONE_2"]
