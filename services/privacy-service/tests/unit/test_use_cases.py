from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from opspilot_common.auth.principal import Principal
from opspilot_common.auth.roles import Role
from opspilot_common.events.catalogue import PrivacyRecorded
from privacy_service.adapters.outbound.detectors import CompositeDetector
from privacy_service.adapters.outbound.detectors.regex import RegexDetector
from privacy_service.adapters.outbound.memory import InMemoryStore, InMemoryUnitOfWork
from privacy_service.application import Detokenise, PseudonymiseEmail, RevealIdentity
from privacy_service.domain.errors import (
    InvalidRequestError,
    LeakDetectedError,
    NotAllowedError,
    PrivacyUnavailableError,
    UnknownTokenError,
)
from privacy_service.ports import EntityDetector

from ..conftest import NOW, FakeNer, FakeSource, raw_email

ORCHESTRATOR = "agent-orchestrator"


class Clock:
    def now(self) -> datetime:
        return NOW


class World:
    def __init__(self, detector: EntityDetector | None = None) -> None:
        self.store = InMemoryStore()
        self.source = FakeSource(raw_email())
        detector = detector or CompositeDetector([RegexDetector(), FakeNer("Maria Schmidt")])
        self.pseudonymise = PseudonymiseEmail(self.uow, self.source, detector, RegexDetector(), Clock())
        self.reveal = RevealIdentity(self.uow, Clock())
        self.detokenise = Detokenise(self.uow)

    def uow(self) -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(self.store)

    def privacy_events(self) -> list[PrivacyRecorded]:
        return [p for p, _ in self.store.published if isinstance(p, PrivacyRecorded)]


def person(*roles: Role) -> Principal:
    return Principal("u-7", "Lena", frozenset(roles))


async def test_email_is_tokenised_with_signature_removed() -> None:
    world = World()
    record = await world.pseudonymise("e-1", ORCHESTRATOR)
    email = record.email
    assert email.sender.display() == "[PERSON_1] <[EMAIL_1]>"
    assert email.subject == "Upload stuck at 0%, [PERSON_1]"
    assert email.body == (
        "Hello support,\n\n"
        "Since about 08:40 our upload is stuck at 0%. My colleague [PERSON_2] sees the same.\n"
        "[PERSON_1] here again: please call me on [PHONE_1].\n\n"
        "Regards,\n[signature removed]"
    )
    assert email.attachments[0]["filename"] == "[PERSON_1]_screenshot.png"
    assert record.counts == {"PERSON": 2, "EMAIL": 2, "PHONE": 1}  # EMAIL_2 = the support address in To
    assert record.signature_removed and not record.quoted_removed
    assert record.id.startswith("vlt-")

    (event,) = world.privacy_events()
    assert (
        event.action == "pseudonymise" and event.record_id == record.id and event.subject_token == "EMAIL_1"
    )
    dumped = event.model_dump_json()
    assert not any(raw in dumped for raw in ("Alex", "Muster", "example.eu", "1234567", "Maria"))


async def test_the_same_person_keeps_the_same_token_and_repeats_are_idempotent() -> None:
    world = World()
    first = await world.pseudonymise("e-1", ORCHESTRATOR)
    assert await world.pseudonymise("e-1", ORCHESTRATOR) == first  # no second record, no second event
    assert len(world.privacy_events()) == 1

    world.source.emails["e-2"] = raw_email(
        "e-2", "thr-0002-bbbb", subject="Still stuck", body="Hi, it is still stuck. Alex", attachments=()
    )
    second = await world.pseudonymise("e-2", ORCHESTRATOR)
    assert second.email.sender.display() == "[PERSON_1] <[EMAIL_1]>"
    assert second.email.body == "Hi, it is still stuck. [PERSON_1]"


async def test_only_the_orchestrator_may_ask() -> None:
    with pytest.raises(NotAllowedError):
        await World().pseudonymise("e-1", "jira-connector")


async def test_fails_closed_when_mail_service_or_detection_is_down() -> None:
    world = World()
    world.source.down = True
    with pytest.raises(PrivacyUnavailableError):
        await world.pseudonymise("e-1", ORCHESTRATOR)

    world = World(CompositeDetector([RegexDetector(), FakeNer(fail=True)]))
    with pytest.raises(PrivacyUnavailableError):
        await world.pseudonymise("e-1", ORCHESTRATOR)
    assert world.store.records.rows == {} and world.store.vault.entries == {} and world.store.published == []


async def test_leak_check_blocks_output_that_still_contains_identifiers() -> None:
    world = World(FakeNer())  # a detector that misses the phone number
    with pytest.raises(LeakDetectedError, match="PHONE"):
        await world.pseudonymise("e-1", ORCHESTRATOR)
    assert world.store.records.rows == {} and world.store.published == []  # nothing kept, nothing announced


async def test_reveal_shows_name_and_address_for_five_minutes_and_is_recorded() -> None:
    world = World()
    await world.pseudonymise("e-1", ORCHESTRATOR)
    reveal = await world.reveal(
        person(Role.L1_ANALYST), "[PERSON_1]", "Verify the customer's account", "thr-0001"
    )
    assert reveal.values == {"EMAIL": "alex.muster@example.eu", "PERSON": "Alex Muster"}
    assert reveal.expires_at - reveal.created_at == timedelta(minutes=5)
    event = world.privacy_events()[-1]
    assert event.action == "reveal_identity" and event.actor_role == "l1_analyst"
    assert event.detail == "Viewed identity of [PERSON_1] for 5 min: Verify the customer's account"
    assert "Alex" not in event.model_dump_json()
    assert world.store.reveals.rows[0].user_id == "u-7"


async def test_reveal_rules() -> None:
    world = World()
    await world.pseudonymise("e-1", ORCHESTRATOR)
    with pytest.raises(NotAllowedError):
        await world.reveal(person(Role.ADMIN), "[PERSON_1]", "Just curious about it")
    with pytest.raises(InvalidRequestError):
        await world.reveal(person(Role.SUPPORT_AGENT), "[PERSON_1]", "no")
    with pytest.raises(InvalidRequestError):
        await world.reveal(person(Role.SUPPORT_AGENT), "Alex Muster", "Verify the account")
    with pytest.raises(UnknownTokenError):
        await world.reveal(person(Role.SUPPORT_AGENT), "[PERSON_99]", "Verify the account")


async def test_detokenise_only_for_sending_and_never_partially() -> None:
    world = World()
    await world.pseudonymise("e-1", ORCHESTRATOR)
    text = await world.detokenise(
        "mail-service", "Dear [PERSON_1], we will call [PHONE_1].", purpose="customer reply"
    )
    assert text == "Dear Alex Muster, we will call +49 40 1234567."
    assert world.privacy_events()[-1].action == "detokenise"
    with pytest.raises(NotAllowedError):
        await world.detokenise(ORCHESTRATOR, "Dear [PERSON_1]", purpose="peek")
    with pytest.raises(UnknownTokenError):
        await world.detokenise("mail-service", "Dear [PERSON_1] and [PERSON_42]", purpose="customer reply")
