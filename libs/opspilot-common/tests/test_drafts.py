from __future__ import annotations

from typing import Any

from opspilot_common.drafts import AI_DISCLOSURE, RECIPIENT_TOKEN, blocking, check_draft

CONTEXT: dict[str, Any] = {
    "jira_key": "KAN-12",
    "facts": "Problem: Upload stuck at SITE-A014\nFixed at: 10:34 UTC\nuploads waiting: 41",
    "recipients": ["PERSON_1", "PERSON_4"],
    "linked": ["PERSON_4", "PERSON_1"],
}
SUBJECT = "Re: Upload stuck [KAN-12]"


def body(text: str = "Uploads work again since 10:34, and the 41 waiting uploads were processed.") -> str:
    return f"Hello {RECIPIENT_TOKEN},\n\n{text}\n\nReference: KAN-12\n\nOpsPilot Support\n\n{AI_DISCLOSURE}"


def by_id(subject: str, text: str, context: dict[str, Any] = CONTEXT, **kw: Any) -> dict[str, Any]:
    return {c.id: c for c in check_draft(subject, text, context, **kw)}


def test_a_good_draft_passes_every_check() -> None:
    checks = check_draft(SUBJECT, body(), CONTEXT)
    assert all(c.ok for c in checks) and not blocking(checks)
    assert by_id(SUBJECT, body())["recipients"].detail == "2 of 2"


def test_figures_and_references_must_come_from_the_ticket() -> None:
    bad = by_id(SUBJECT, body("Fixed at 11:02 for 41 uploads; see also KAN-99."))["facts"]
    assert not bad.ok and bad.blocking and "11:02" in bad.detail and "KAN-99" in bad.detail
    assert "41" not in bad.detail


def test_an_approver_may_state_a_figure_the_ticket_lacks_but_is_warned() -> None:
    warned = by_id(SUBJECT, body("We retried 7 uploads."), edited=True)["facts"]
    assert not warned.ok and not warned.blocking
    assert not blocking(check_draft(SUBJECT, body("We retried 7 uploads."), CONTEXT, edited=True))


def test_promises_and_etas_are_caught() -> None:
    for promise in (
        "It will be fixed.",
        "ETA is unclear.",
        "Expect news within 2 hours.",
        "We should have it by tomorrow.",
        "We will be back soon.",
        "This takes in 3 days.",
        "Fixed as soon as possible.",
        "We guarantee it.",
        "The service restarts in about a minute.",  # seen live, from deepseek
        "Back within roughly 2 hours.",
        "Expect it in a few more days.",
    ):
        assert not by_id(SUBJECT, body(promise))["promises"].ok, promise
    for fine in (
        "It has been fixed.",
        "We will write to you again when there is news.",
        "It started failing in the morning.",
        "A few minutes ago the checks passed.",
    ):
        assert by_id(SUBJECT, body(fine))["promises"].ok, fine


def test_personal_data_other_than_the_recipient_token_is_caught() -> None:
    for text in (
        "Your colleague [PERSON_2] said so.",
        "Write to anna@kunde.de.",
        "Call +49 30 1234567.",
        "Call 030 1234 5678.",
        "IBAN DE89370400440532013000.",
    ):
        assert not by_id(SUBJECT, body(text))["personal_data"].ok, text
    assert not by_id("Re: [PERSON_1] upload", body())["personal_data"].ok
    assert by_id(SUBJECT, body())["personal_data"].ok


def test_a_date_or_a_site_is_not_mistaken_for_a_phone_number() -> None:
    context = {**CONTEXT, "facts": CONTEXT["facts"] + "\nDate: 2026-11-04"}
    assert by_id(SUBJECT, body("On 2026-11-04 at SITE-A014."), context)["personal_data"].ok


def test_recipients_must_be_exactly_the_linked_reporters() -> None:
    assert not by_id(SUBJECT, body(), {**CONTEXT, "recipients": ["PERSON_1"]})["recipients"].ok
    assert not by_id(SUBJECT, body(), {**CONTEXT, "recipients": []})["recipients"].ok
    extra = by_id(SUBJECT, body(), {**CONTEXT, "recipients": ["PERSON_1", "PERSON_4", "PERSON_9"]})
    assert not extra["recipients"].ok
    unknown = by_id(SUBJECT, body(), {**CONTEXT, "linked": ["PERSON_1", "PERSON_4", None]})["recipients"]
    assert not unknown.ok and "no stored address" in unknown.detail


def test_the_ai_disclosure_cannot_be_edited_away() -> None:
    gone = by_id(SUBJECT, body().replace(AI_DISCLOSURE, ""), edited=True)["disclosure"]
    assert not gone.ok and gone.blocking
    assert by_id(SUBJECT, body().replace(AI_DISCLOSURE, AI_DISCLOSURE.replace(". ", ".\n")))["disclosure"].ok
