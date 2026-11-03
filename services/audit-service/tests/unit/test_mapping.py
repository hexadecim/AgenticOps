from __future__ import annotations

from datetime import UTC, datetime, timedelta

import opspilot_common.events.catalogue as c
from audit_service.adapters.outbound.memory import InMemoryAuditLog
from audit_service.domain.entry import AuditQuery, Kind
from audit_service.domain.mapping import DESCRIBERS, describe
from opspilot_common.events.envelope import EventEnvelope

NOW = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)


def wrap(payload: c.EventPayload, actor: str | None = None, source: str = "svc") -> EventEnvelope:
    return EventEnvelope.wrap(payload, source=source, actor=actor)


def test_every_catalogue_event_has_an_audit_description() -> None:
    assert set(c.PAYLOADS) == set(DESCRIBERS)


def test_people_appear_by_role_never_by_name() -> None:
    entry = describe(
        wrap(c.UserActivity(user_id="u-1", action="signed_in", actor_role="l1_analyst"), actor="u-1")
    )
    assert (entry.kind, entry.actor, entry.event, entry.detail) == (
        Kind.HUMAN,
        "L1 Analyst",
        "signed_in",
        "Signed in",
    )
    admin = describe(
        wrap(
            c.UserActivity(user_id="u-2", action="user_created", detail="Support agent", actor_role="admin"),
            "u-9",
        )
    )
    assert admin.actor == "Admin" and admin.detail == "User created for user u-2 (Support agent)"
    assert admin.actor_id == "u-9"


def test_wireframe_examples() -> None:
    reveal = describe(
        wrap(
            c.PrivacyRecorded(
                action="reveal_identity",
                subject_token="PERSON_1",
                detail="Viewed identity of [PERSON_1] for 5 min: verify the customer's account",
                legal_basis="Art. 5(2) GDPR accountability",
                actor_role="l1_analyst",
            ),
            actor="u-1",
        )
    )
    assert (reveal.kind, reveal.actor, reveal.event, reveal.privacy) == (
        Kind.PRIVACY, "L1 Analyst", "reveal_identity", "Art. 5(2) GDPR accountability",
    )  # fmt: skip
    gate = describe(
        wrap(
            c.PrivacyRecorded(
                action="pseudonymise", detail="PERSON (1) tokenised", record_id="vlt-7Q1", thread_id="t1"
            ),
            actor="privacy-service",
        )
    )
    assert (gate.actor, gate.privacy, gate.thread_id) == ("Privacy gate", "Token map vlt-7Q1 · EU", "t1")
    decided = describe(
        wrap(
            c.ApprovalDecided(
                approval_id="a",
                run_id="r",
                thread_id="t",
                gate=c.Gate.TICKET,
                decision=c.Decision.REJECT,
                decided_by="u-1",
                decided_by_role="support_agent",
                reason="duplicate of KAN-3",
            )
        )
    )
    assert (decided.actor, decided.event, decided.detail) == (
        "Support agent", "reject_ticket", "Rejected ticket: duplicate of KAN-3",
    )  # fmt: skip
    created = describe(
        wrap(
            c.JiraIssueCreated(
                approval_id="a", thread_id="t", issue_key="KAN-12", project="KAN", severity="sev2"
            )
        )
    )
    assert (created.event, created.ticket, created.detail) == (
        "create_jira_issue", "KAN-12", "Incident KAN-12 created in KAN, sev2",
    )  # fmt: skip


def test_settings_entries_list_field_names_only() -> None:
    entry = describe(
        wrap(c.SettingsChanged(section="mailbox", version=3, changed_fields=["imap_host"]), "u-9")
    )
    assert entry.detail == "Mailbox settings saved (v3): imap_host" and entry.actor == "Admin"


def test_unknown_or_malformed_events_are_still_recorded() -> None:
    unknown = describe(EventEnvelope(type="ops.thing_happened", source="ops-service", payload={"a": 1}))
    assert (
        unknown.kind is Kind.SYSTEM
        and unknown.event == "ops_thing_happened"
        and "unknown event type" in unknown.detail
    )
    broken = describe(EventEnvelope(type="user.activity", source="identity-service", payload={"nope": True}))
    assert broken.kind is Kind.SYSTEM and "did not match" in broken.detail


async def test_log_is_append_only_and_pages_newest_first() -> None:
    log = InMemoryAuditLog()
    for n in range(5):
        env = EventEnvelope(
            type="user.activity",
            source="identity-service",
            occurred_at=NOW + timedelta(minutes=n),
            payload={"user_id": f"u-{n}", "action": "signed_in", "actor_role": "l1_analyst"},
        )
        assert await log.append(describe(env))
        assert not await log.append(describe(env))  # redelivery
    first = await log.search(AuditQuery(limit=2))
    assert [e.payload["user_id"] for e in first.entries] == ["u-4", "u-3"] and first.next_before
    second = await log.search(AuditQuery(limit=2, before=first.next_before))
    assert [e.payload["user_id"] for e in second.entries] == ["u-2", "u-1"]


def test_approver_severity_change_is_named() -> None:
    entry = describe(
        wrap(
            c.ApprovalDecided(
                approval_id="a",
                run_id="r",
                thread_id="t",
                gate=c.Gate.TICKET,
                decision=c.Decision.APPROVE,
                decided_by="u-1",
                decided_by_role="l1_analyst",
                edits={"severity": "sev1", "summary": "[PERSON_1] x"},
            )
        )
    )
    assert entry.detail == "Approved ticket (changed: severity → sev1, summary)"
