from __future__ import annotations

from datetime import UTC, datetime

import pytest
from approval_service.domain.approval import Approval, Outcome, Status
from approval_service.domain.errors import AlreadyDecidedError, InvalidDecisionError, NotAllowedError
from opspilot_common.auth.principal import Principal
from opspilot_common.auth.roles import Role
from opspilot_common.events.catalogue import Decision, Gate

NOW = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)


def approval(gate: Gate = Gate.TICKET) -> Approval:
    return Approval("a-1", "r-1", "t-1", gate, "Upload stuck", "Intake: upload failure", NOW)


def person(*roles: Role, user: str = "u-1") -> Principal:
    return Principal(user, "Lena", frozenset(roles))


def test_who_may_decide_which_gate() -> None:
    assert approval().decide(person(Role.L1_ANALYST), Decision.APPROVE, reason=None, edits={}, now=NOW)
    with pytest.raises(NotAllowedError):
        approval(Gate.FIX).decide(person(Role.L1_ANALYST), Decision.APPROVE, reason=None, edits={}, now=NOW)
    for role in (Role.COMPLIANCE_LEAD, Role.ADMIN):
        with pytest.raises(NotAllowedError):
            approval().decide(person(role), Decision.APPROVE, reason=None, edits={}, now=NOW)
    fix = approval(Gate.FIX)
    fix.decide(person(Role.SUPPORT_AGENT, Role.ADMIN), Decision.APPROVE, reason=None, edits={}, now=NOW)
    assert fix.decided_by_role == "support_agent"  # the role that allowed it, not just any role


def test_reject_needs_a_reason_and_send_back_is_for_messages_only() -> None:
    with pytest.raises(InvalidDecisionError, match="Say why"):
        approval().decide(person(Role.L1_ANALYST), Decision.REJECT, reason="  ", edits={}, now=NOW)
    with pytest.raises(InvalidDecisionError, match="messages"):
        approval().decide(person(Role.L1_ANALYST), Decision.SEND_BACK, reason="x", edits={}, now=NOW)
    a = approval()
    a.decide(
        person(Role.L1_ANALYST), Decision.REJECT, reason="Duplicate of KAN-3", edits={"summary": "x"}, now=NOW
    )
    assert a.status is Status.REJECTED and a.edits == {} and a.outcome is None


def test_decisions_are_final_but_a_repeat_is_harmless() -> None:
    a = approval()
    assert a.decide(person(Role.L1_ANALYST), Decision.APPROVE, reason=None, edits={}, now=NOW)
    assert a.outcome is Outcome.TICKET_PENDING
    assert not a.decide(
        person(Role.L1_ANALYST), Decision.APPROVE, reason=None, edits={}, now=NOW
    )  # double click
    with pytest.raises(AlreadyDecidedError):
        a.decide(person(Role.L1_ANALYST, user="u-2"), Decision.APPROVE, reason=None, edits={}, now=NOW)
    with pytest.raises(AlreadyDecidedError):
        a.decide(person(Role.L1_ANALYST), Decision.REJECT, reason="changed my mind", edits={}, now=NOW)


def test_edits_are_limited_to_the_gate() -> None:
    a = approval()
    a.decide(
        person(Role.L1_ANALYST),
        Decision.APPROVE,
        reason=None,
        edits={"severity": "sev1", "summary": " New "},
        now=NOW,
    )
    assert a.edits == {"severity": "sev1", "summary": "New"}
    assert a.severity == "sev1"  # queue and decided list show the approver's severity
    for bad in ({"assignee": "me"}, {"severity": "urgent"}, {"summary": "x"}):
        with pytest.raises(InvalidDecisionError):
            approval().decide(person(Role.L1_ANALYST), Decision.APPROVE, reason=None, edits=bad, now=NOW)


FIX_PROPOSAL = {
    "action": {
        "name": "restart_service",
        "params": {"service": "upload-worker", "region": "eu-west-1"},
        "params_spec": [
            {"name": "service", "label": "Service", "choices": ["upload-worker", "upload-api"]},
            {"name": "region", "label": "Region", "choices": ["eu-west-1"]},
        ],
    }
}


def fix_approval() -> Approval:
    return Approval(
        "a-2", "r-1", "t-1", Gate.FIX, "Restart upload-worker on eu-west-1?", "Diagnostics", NOW,
        proposal=FIX_PROPOSAL,
    )  # fmt: skip


def test_fix_parameters_may_only_change_to_offered_choices() -> None:
    agent = person(Role.SUPPORT_AGENT)
    a = fix_approval()
    a.decide(agent, Decision.APPROVE, reason=None, edits={"params": {"service": "upload-api"}}, now=NOW)
    assert a.edits == {"params": {"service": "upload-api"}} and a.outcome is Outcome.FIX_PENDING
    for bad, message in (
        ({"params": {"service": "postgres"}}, "Service 'postgres' is not one of the allowed values"),
        ({"params": {"force": "yes"}}, "force is not a parameter of restart_service"),
        ({"params": "upload-api"}, "name/value pairs"),
        ({"summary": "x"}, "cannot be changed here"),
    ):
        with pytest.raises(InvalidDecisionError, match=message):
            fix_approval().decide(agent, Decision.APPROVE, reason=None, edits=bad, now=NOW)


def test_fix_gate_reminds_every_15_minutes_while_it_waits() -> None:
    from datetime import timedelta

    a = fix_approval()
    assert a.next_reminder_at == NOW + timedelta(minutes=15)
    assert not a.remind(NOW + timedelta(minutes=14, seconds=59))
    assert a.remind(NOW + timedelta(minutes=15)) and a.reminders == 1
    assert not a.remind(NOW + timedelta(minutes=20))
    assert a.remind(NOW + timedelta(minutes=47)) and a.reminders == 3  # after a pause: one, not three
    assert a.next_reminder_at == NOW + timedelta(minutes=60)
    a.decide(person(Role.SUPPORT_AGENT), Decision.APPROVE, reason=None, edits={}, now=NOW)
    assert a.next_reminder_at is None and not a.remind(NOW + timedelta(hours=2))
    assert approval().next_reminder_at is None  # ticket gate: no reminders
    a.fix_executed(False, "Refused before running")
    assert a.outcome is Outcome.FIX_FAILED and a.outcome_detail == "Refused before running"
