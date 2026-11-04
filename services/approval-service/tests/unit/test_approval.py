from __future__ import annotations

from datetime import UTC, datetime

import pytest
from approval_service.domain.approval import Approval, Outcome, Status
from approval_service.domain.errors import AlreadyDecidedError, InvalidDecisionError, NotAllowedError
from opspilot_common.auth.principal import Principal
from opspilot_common.auth.roles import Role
from opspilot_common.drafts import AI_DISCLOSURE, RECIPIENT_TOKEN
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


def test_only_an_approved_fix_takes_a_health_verdict() -> None:
    from datetime import UTC, datetime

    from approval_service.domain.approval import Approval, Status
    from opspilot_common.events.catalogue import Gate

    def approval(gate: Gate, status: Status) -> Approval:
        return Approval("a", "r", "t", gate, "x", "y", datetime(2026, 11, 3, tzinfo=UTC), status=status)

    assert approval(Gate.FIX, Status.APPROVED).health_checked(True, "All 5 probes pass")
    assert not approval(Gate.FIX, Status.PENDING).health_checked(True, None)
    assert not approval(Gate.FIX, Status.REJECTED).health_checked(False, None)
    assert not approval(Gate.TICKET, Status.APPROVED).health_checked(True, None)


MESSAGE_BODY = (
    f"Hello {RECIPIENT_TOKEN},\n\nThe upload problem is fixed.\n\nReference: KAN-12\n\n{AI_DISCLOSURE}"
)


def message_approval(revision: int = 0, body: str = MESSAGE_BODY) -> Approval:
    proposal = {
        "subject": "Re: Upload stuck [KAN-12]",
        "body": body,
        "revision": revision,
        "check_context": {
            "jira_key": "KAN-12", "facts": "Problem: Upload stuck", "recipients": ["PERSON_1"],
            "linked": ["PERSON_1"],
        },
    }  # fmt: skip
    return Approval("a-3", "r-1", "t-1", Gate.MESSAGE, "Resolution update", "Comms", NOW, proposal=proposal)


def test_message_approve_runs_the_checks_on_what_will_go_out() -> None:
    analyst = person(Role.L1_ANALYST)
    a = message_approval()
    assert a.decide(analyst, Decision.APPROVE, reason=None, edits={}, now=NOW)
    assert a.outcome is Outcome.MESSAGE_PENDING

    bad_draft = message_approval(body=MESSAGE_BODY.replace("is fixed", "will be fixed by 12:00"))
    with pytest.raises(InvalidDecisionError, match=r"cannot be sent yet.*No promises"):
        bad_draft.decide(analyst, Decision.APPROVE, reason=None, edits={}, now=NOW)
    assert bad_draft.status is Status.PENDING

    for edits, message in (
        ({"body": MESSAGE_BODY.replace(AI_DISCLOSURE, "")}, "AI disclosure"),
        ({"body": MESSAGE_BODY + " Ask [PERSON_2]."}, "personal data"),
        ({"body": MESSAGE_BODY + " Write to anna@kunde.de."}, "personal data"),
        ({"subject": "Re: [PERSON_2] upload"}, "personal data"),
        ({"subject": "x"}, "subject must be"),
        ({"subject": "two\nlines"}, "one line"),
        ({"body": "short"}, "message must be"),
    ):
        with pytest.raises(InvalidDecisionError, match=message):
            message_approval().decide(analyst, Decision.APPROVE, reason=None, edits=edits, now=NOW)


def test_an_approvers_own_figure_warns_but_does_not_block() -> None:
    a = message_approval()
    edited = {"body": MESSAGE_BODY + " We retried 41 uploads."}
    facts = next(c for c in a.message_checks(edited) if c.id == "facts")
    assert not facts.ok and not facts.blocking
    assert a.decide(person(Role.L1_ANALYST), Decision.APPROVE, reason=None, edits=edited, now=NOW)
    assert a.edits == {"body": edited["body"]}


def test_send_back_needs_a_note_and_stops_after_two_revisions() -> None:
    analyst = person(Role.L1_ANALYST)
    with pytest.raises(InvalidDecisionError, match="Say why"):
        message_approval().decide(analyst, Decision.SEND_BACK, reason=" ", edits={}, now=NOW)
    a = message_approval(revision=1)
    assert a.decide(analyst, Decision.SEND_BACK, reason="Shorter", edits={"body": "ignored"}, now=NOW)
    assert a.status is Status.SENT_BACK and a.edits == {} and a.outcome is None
    with pytest.raises(InvalidDecisionError, match="All 2 revisions"):
        message_approval(revision=2).decide(analyst, Decision.SEND_BACK, reason="Again", edits={}, now=NOW)
    last = message_approval(revision=2)
    assert last.decide(analyst, Decision.REJECT, reason="Told by phone", edits={}, now=NOW)  # still a way out


def test_only_message_gates_have_draft_checks_and_who_may_decide() -> None:
    with pytest.raises(InvalidDecisionError):
        approval().message_checks({})
    with pytest.raises(NotAllowedError):
        message_approval().decide(person(Role.ADMIN), Decision.APPROVE, reason=None, edits={}, now=NOW)


def test_send_results_become_the_outcome() -> None:
    a = message_approval()
    a.message_result(["PERSON_1"], [], None)
    assert a.outcome is Outcome.MESSAGE_SENT
    a.message_result(["PERSON_1"], ["EMAIL_4"], "refused")
    assert a.outcome is Outcome.MESSAGE_PARTLY_SENT and a.outcome_detail == "refused"
    a.message_result([], ["PERSON_1"], "refused")
    assert a.outcome is Outcome.MESSAGE_FAILED


def test_the_first_health_verdict_stands() -> None:
    a = fix_approval()
    a.decide(person(Role.SUPPORT_AGENT), Decision.APPROVE, reason=None, edits={}, now=NOW)
    assert a.health_checked(True, "All 5 probes pass")
    assert not a.health_checked(True, "All 5 probes pass. Customer message sent")
    assert a.health_detail == "All 5 probes pass"
