"""Diagnostics and gate T2: what the model may choose, what the approver sees, how health is judged. Pure.

The model never writes an action or a parameter. ops-service offers candidates (allow-listed runbook steps
with parameters filled from the evidence); the model only picks one of them by number, or none.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, create_model

from .triage import Intake, shorten

NONE = "none"
CAUSE_MAX, WHY_MAX = 200, 240
AFTER = "OpsPilot re-runs the health probes before anyone is told it is fixed."


class FixChoice(BaseModel):
    """Read back from the model's structured answer (whatever the dynamic schema was)."""

    likely_cause: str
    choice: str
    confidence: Literal["low", "medium", "high"]
    explanation: str


def choice_schema(candidates: int) -> type[BaseModel]:
    """A JSON schema whose `choice` can only be one of the offered numbers or 'none' (Ollama turns it into a
    grammar, so an 8B model cannot answer anything else)."""
    options = (*(str(i + 1) for i in range(candidates)), NONE)
    model: type[BaseModel] = create_model(
        "FixDecision",
        likely_cause=(str, Field(max_length=CAUSE_MAX, description="The most likely cause, in one sentence")),
        choice=(
            Literal[options],
            Field(description="Number of the fix the evidence supports, or 'none' if it supports none"),
        ),
        confidence=(Literal["low", "medium", "high"], Field(description="How well the evidence supports it")),
        explanation=(
            str,
            Field(max_length=WHY_MAX, description="Why, citing a probe, a log line or a runbook"),
        ),
    )
    return model


def problem_text(intake: Intake) -> str:
    parts = [intake.category, intake.summary]
    if intake.error:
        parts.append(intake.error)
    if intake.asset:
        parts.append(f"on {intake.asset}")
    return ": ".join(p for p in parts[:2] if p) + (f" ({', '.join(parts[2:])})" if parts[2:] else "")


def render_evidence(intake: Intake, jira_key: str | None, d: dict[str, Any]) -> str:
    """The model's input: tokenised ticket facts, probes, masked logs, runbook excerpts, numbered fixes."""
    lines = [
        f"Ticket {jira_key or '(new)'}: {intake.summary}",
        f"Category: {intake.category} · severity {intake.severity} · site {intake.site or 'unknown'}"
        + (f" · asset {intake.asset}" if intake.asset else ""),
    ]
    if intake.error:
        lines.append(f"Reported error: {intake.error}")
    lines += ["", f"Health probes ({d.get('region') or 'all regions'}):"]
    lines += [f"- {p['name']}: {'pass' if p['ok'] else 'FAIL'}, {p['detail']}" for p in d.get("probes", [])]
    for finding in d.get("logs", []):
        lines += ["", f"Log lines from {finding['service']} (personal data masked):"]
        lines += [f"  {line}" for line in finding.get("lines", [])]
        if finding.get("similar"):
            lines.append(f"  … {finding['similar']} similar lines since {finding.get('since') or 'earlier'}")
    if d.get("deployment"):
        lines += ["", str(d["deployment"])]
    for rb in d.get("runbooks", []):
        lines += ["", f"Runbook {rb['id']} {rb['title']} ({rb['heading']}): {shorten(rb['text'], 400)}"]
    lines += ["", "Fixes you may choose (only these exist):"]
    for i, c in enumerate(d.get("candidates", []), start=1):
        rb = c["runbook"]
        lines.append(f"{i}. {c['signature']}: runbook {rb['id']} step {rb['step']}, when {rb['why']}")
    return "\n".join(lines)


def candidate_for(choice: FixChoice, candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if choice.choice == NONE or not choice.choice.isdigit():
        return None
    index = int(choice.choice) - 1
    return candidates[index] if 0 <= index < len(candidates) else None


def build_fix_proposal(
    *,
    candidate: dict[str, Any],
    choice: FixChoice,
    diagnostics: dict[str, Any],
    jira_key: str | None,
    reporters: int,
    model: dict[str, Any],
) -> dict[str, Any]:
    impact = dict(diagnostics.get("impact") or {})
    sites = list(impact.pop("sites", []))
    return {
        "action": {
            "name": candidate["action"],
            "title": candidate.get("title"),
            "signature": candidate["signature"],
            "params": candidate["params"],
            "params_spec": candidate["params_spec"],
            "effect": candidate["effect"],
            "rollback": candidate["rollback"],
        },
        "runbook": candidate["runbook"],
        "diagnosis": {
            "likely_cause": shorten(choice.likely_cause, CAUSE_MAX),
            "confidence": choice.confidence,
            "explanation": shorten(choice.explanation, WHY_MAX),
        },
        "after": AFTER,
        "evidence": {
            "checked_at": diagnostics.get("checked_at"),
            "probes": diagnostics.get("probes", []),
            "logs": diagnostics.get("logs", []),
            "deployment": diagnostics.get("deployment"),
            "runbooks": diagnostics.get("runbooks", []),
            "notes": diagnostics.get("notes", []),
        },
        "affected": {"sites": sites, "reporters": reporters, "impact": impact},
        "region": diagnostics.get("region"),
        "site": diagnostics.get("site"),
        "services": diagnostics.get("suspects", []),
        "jira_key": jira_key,
        "model": model,
    }


def health_verdict(probes: list[dict[str, Any]]) -> tuple[bool, str]:
    failing = [p for p in probes if not p.get("ok")]
    if not probes:
        return False, "No probes answered"
    if not failing:
        return True, f"All {len(probes)} probes pass"
    names = ", ".join(f"{p['name']} ({p['detail']})" for p in failing)
    return False, f"Still failing: {names}"


def diagnostics_detail(d: dict[str, Any]) -> str:
    probes = d.get("probes", [])
    failing = [p["name"] for p in probes if not p.get("ok")]
    rb = ", ".join(r["id"] for r in d.get("runbooks", [])) or "none"
    head = (
        f"{len(failing)} of {len(probes)} probes failing ({', '.join(failing)})"
        if failing
        else (f"all {len(probes)} probes pass")
    )
    logs = sum(len(f.get("lines", [])) + int(f.get("similar", 0)) for f in d.get("logs", []))
    return f"{head}; {logs} log findings; runbooks {rb}; {len(d.get('candidates', []))} allow-listed fixes"
