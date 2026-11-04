"""Comms and gate T3: which message a finished run owes the customer, what the model may write, and what the
approver sees. Pure.

Routing is deterministic: the outcome of the run picks the kind of message and its recipients. The model
writes only the middle of the message; greeting, reference, signature and AI disclosure are added here, so
they cannot be forgotten. All text stays tokenised until mail-service sends it (`RECIPIENT_TOKEN`).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from opspilot_common.drafts import AI_DISCLOSURE, MAX_REVISIONS, RECIPIENT_TOKEN, check_draft, checks_json
from pydantic import BaseModel, Field

from .diagnosis import ended
from .triage import Intake

DRAFT_MAX = 900
SUBJECT_MAX = 200
ACKNOWLEDGEMENT, FOLLOW_UP, RESOLUTION = "acknowledgement", "follow_up", "resolution"
KIND_LABELS = {
    ACKNOWLEDGEMENT: "Acknowledgement",
    FOLLOW_UP: "Follow-up question",
    RESOLUTION: "Resolution update",
}
_GREETING = re.compile(r"^\s*(hello|hi|dear|good (morning|afternoon|evening))\b.*$", re.IGNORECASE)
_SIGN_OFF = re.compile(
    r"^\s*(kind regards|best regards|regards|best|sincerely|thanks|thank you|opspilot support|"
    r"support team|reference:).*$",
    re.IGNORECASE,
)
_ANY_TOKEN = re.compile(r"\[[A-Z][A-Z_]*(?:_\d+)?\]")


class DraftText(BaseModel):
    """What the model fills in (json_schema output; Ollama turns the limit into a grammar)."""

    body: str = Field(max_length=DRAFT_MAX, description="The message text: one or two short paragraphs")


def kind_for(outcome: str | None, intake: Intake) -> str:
    """A fix that held is a resolution. Where a person takes over, ask for what is missing if that helps them;
    a failed fix is not the moment to ask the customer for more."""
    if outcome == "resolved":
        return RESOLUTION
    if outcome in ("no_fix", "linked") and intake.missing_details:
        return FOLLOW_UP
    return ACKNOWLEDGEMENT


def reporter_of(email: dict[str, Any], email_id: str, site: str | None) -> dict[str, Any] | None:
    sender = email.get("from") or {}
    token = sender.get("email_token")
    if not token:
        return None
    return {
        "email_token": token,
        "name_token": sender.get("name_token"),
        "email_id": email_id,
        "site": site,
    }


def display_token(reporter: dict[str, Any]) -> str:
    return str(reporter.get("name_token") or reporter["email_token"])


def recipients_for(
    kind: str, own: dict[str, Any], linked: list[dict[str, Any] | None]
) -> tuple[list[dict[str, Any]], list[str | None]]:
    """(who gets it, who should get it). A resolution goes to everyone linked to the ticket, each person once
    (answering their latest email); the other kinds answer this run's reporter only: each linked report got
    its own acknowledgement. `linked` has None for a report whose address was not stored."""
    if kind != RESOLUTION:
        return [{**own, "how": "Reported"}], [display_token(own)]
    people: dict[str, dict[str, Any]] = {}
    for reporter in linked:
        if reporter is not None:
            people[reporter["email_token"]] = {**reporter, "how": "Linked duplicate"}
    people[own["email_token"]] = {**own, "how": "Reported"}
    should = [display_token(r) if r else None for r in [*linked, own]]
    return list(people.values()), should


def _clock(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M UTC")
    except ValueError:
        return None


def known_facts(state: dict[str, Any], intake: Intake) -> list[str]:
    """What the message may state: from the ticket and the run, never from the logs. Tokenised."""
    facts = [f"Problem: {intake.summary}", f"Category: {intake.category}"]
    if key := state.get("jira_key"):
        facts.append(f"Ticket: {key}")
    if intake.site:
        facts.append(f"Site: {intake.site}")
    if intake.asset:
        facts.append(f"Machine: {intake.asset}")
    if intake.error:
        facts.append(f"Reported error: {intake.error}")
    facts += [f"{f.label}: {f.value}" for f in intake.facts]
    fix = state.get("fix_result") or {}
    if state.get("outcome") == "resolved":
        # Not the action's `effect`: that is what a runbook expects (future tense), not what happened.
        facts.append("What was done: our team applied a fix, and our health checks now pass")
        if at := _clock(fix.get("at")):
            facts.append(f"Fixed at: {at}")
        impact = ((state.get("fix_proposal") or {}).get("affected") or {}).get("impact") or {}
        facts += [f"{label}: {value}" for label, value in impact.items()]
    return facts


_SITUATION = {
    "resolved": "The fix ran and our health checks now pass: the problem is fixed.",
    "no_fix": "The ticket is open and the support team is looking into it. It is not fixed yet.",
    "linked": "We already know about this problem: the report joined an open ticket. It is not fixed yet.",
    "fix_rejected": "The ticket is open and the support team is working on it. It is not fixed yet.",
    "fix_failed": "The ticket is open and the support team is still working on it. It is not fixed yet.",
}
_ASK = {
    ACKNOWLEDGEMENT: "Tell the customer we received their report and logged it. Do not say it is fixed.",
    FOLLOW_UP: "Tell the customer we logged their report and need a few details to continue. "
    "The list of questions is added below your text: do not write the questions yourself.",
    RESOLUTION: "Tell the customer the problem is fixed and to reply if something still does not work.",
}


def draft_brief(kind: str, state: dict[str, Any], facts: list[str], notes: list[str]) -> str:
    """The model's input. An internal note may guide the wording; it is not for quoting."""
    outcome = str(state.get("outcome") or "")
    lines = [
        f"Task: {_ASK[kind]}",
        f"Situation: {_SITUATION.get(outcome, _SITUATION['no_fix'])}",
        "",
        "Facts you may use (only these):",
        *[f"- {f}" for f in facts],
    ]
    rejected_because = (state.get("fix_decision") or {}).get("reason")
    if outcome == "fix_rejected" and rejected_because:
        lines += ["", f"Internal note from the team, for your understanding only: {rejected_because}"]
    if notes:
        lines += ["", "The approver sent your last draft back with this note. Follow it:"]
        lines += [f"- {n}" for n in notes]
    return "\n".join(lines)


def clean_model_text(text: str, reporter: dict[str, Any]) -> str:
    """Remove what we add ourselves, and turn the reporter's own tokens into the recipient placeholder."""
    for token in (reporter.get("name_token"), reporter.get("email_token")):
        if token:
            text = text.replace(f"[{token}]", RECIPIENT_TOKEN)
    text = text.replace(AI_DISCLOSURE, "")
    lines = text.strip().splitlines()
    while lines and (not lines[0].strip() or _GREETING.match(lines[0])):
        lines.pop(0)
    while lines and (not lines[-1].strip() or _SIGN_OFF.match(lines[-1])):
        lines.pop()
    return ended("\n".join(lines).strip(), DRAFT_MAX)


def fallback_text(kind: str, outcome: str | None, key: str | None) -> str:
    """Used when the model's answer cannot be read. The approver sees that it is a template."""
    logged = f"logged it as {key}" if key else "logged it"
    if kind == RESOLUTION:
        return (
            "The problem you reported has been fixed, and our checks now pass.\n\n"
            "If something still does not work for you, please reply to this email."
        )
    if kind == FOLLOW_UP:
        return f"We have received your report and {logged}. To continue, we need a few more details from you."
    if outcome == "linked":
        return (
            f"We have received your report. We already know about this problem and have {logged}. "
            "Our support team is working on it, and we will write to you again when there is news."
        )
    return (
        f"We have received your report and {logged}. Our support team is working on it, and we will write "
        "to you again when there is news."
    )


def assemble_body(text: str, kind: str, intake: Intake, key: str | None) -> str:
    parts = [f"Hello {RECIPIENT_TOKEN},", text.strip()]
    if kind == FOLLOW_UP and intake.missing_details:
        parts.append("\n".join(f"- {item}" for item in intake.missing_details))
    if key:
        parts.append(f"Reference: {key}")
    parts += ["OpsPilot Support", AI_DISCLOSURE]
    return "\n\n".join(parts)


def subject_for(email: dict[str, Any], intake: Intake, key: str | None) -> str:
    """A reply to the reporter's subject, unless it names someone: then the ticket's own summary."""
    subject = str(email.get("subject") or "").strip()
    if not subject or _ANY_TOKEN.search(subject):
        subject = intake.summary
    subject = re.sub(r"^(re|aw|fwd?):\s*", "", subject, flags=re.IGNORECASE)
    tail = f" [{key}]" if key and key not in subject else ""
    return f"Re: {subject}"[: SUBJECT_MAX - len(tail)] + tail


def build_message_proposal(
    *,
    kind: str,
    subject: str,
    body: str,
    recipients: list[dict[str, Any]],
    should_reach: list[str | None],
    facts: list[str],
    notes: list[str],
    revision: int,
    written_by: str,
    jira_key: str | None,
    model: dict[str, Any],
) -> dict[str, Any]:
    context = {
        "jira_key": jira_key,
        "facts": "\n".join([*facts, *notes]),
        "recipients": [display_token(r) for r in recipients],
        "linked": should_reach,
    }
    return {
        "kind": kind,
        "kind_label": KIND_LABELS[kind],
        "subject": subject,
        "body": body,
        "recipients": recipients,
        "checks": checks_json(check_draft(subject, body, context)),
        "check_context": context,
        "revision": revision,
        "max_revisions": MAX_REVISIONS,
        "notes": notes,
        "written_by": written_by,  # model | template
        "jira_key": jira_key,
        "model": model,
    }


def message_detail(proposal: dict[str, Any]) -> str:
    n = len(proposal["recipients"])
    failing = sum(not c["ok"] for c in proposal["checks"])
    checks = "all checks pass" if not failing else f"{failing} check{'s' if failing > 1 else ''} to fix"
    return f"Comms: {proposal['kind_label']} to {n} reporter{'s' if n > 1 else ''} · {checks}"
