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
