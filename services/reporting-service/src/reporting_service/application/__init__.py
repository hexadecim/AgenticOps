"""reporting-service use cases: project events into read models, and answer the dashboard."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import opspilot_common.events.catalogue as c
from opspilot_common.auth.roles import ROLE_LABELS, Role
from opspilot_common.events.envelope import EventEnvelope

from ..domain.model import (
    KEY_DEPENDENCIES,
    LABELS,
    Case,
    Dependency,
    Health,
    Probe,
    ServiceLevel,
    StageCount,
    TimelineEntry,
    service_levels,
    stage_counts,
)
from ..ports import Clock, UnitOfWork

UowFactory = Callable[[], UnitOfWork]
WINDOW = timedelta(hours=24)
_PAUSE_NOTES = {
    "waiting_llm": "AI paused: waiting for the local model",
    "waiting_privacy": "Paused: privacy service unavailable (nothing passed on)",
    "failed": "Run stopped",
    "ignored": "Not a support request; no ticket proposed",
    "rejected": "Ticket request rejected",
    "waiting_ops": "Paused: ops-service unavailable (no fix without fresh evidence)",
    "fix_rejected": "Fix rejected",
    "fix_failed": "Fix did not resolve it; escalate to on-call",
    "resolved": "Resolved: health probes pass after the fix",
}
_WITH_DETAIL = ("failed", "rejected", "fix_rejected", "fix_failed")


def _role(value: str) -> str:
    try:
        return ROLE_LABELS[Role(value)]
    except ValueError:
        return "Approver"


class ProjectEvent:
    """Keeps the read models current. Events from different services arrive in any order: every handler
    only fills in what its event knows."""

    def __init__(self, uow: UowFactory) -> None:
        self._uow = uow

    async def __call__(self, envelope: EventEnvelope) -> None:
        try:
            payload = c.parse_payload(envelope)
        except (KeyError, ValueError):
            return  # unknown or malformed: audit-service records it; nothing to measure here
        async with self._uow() as uow:
            await self._apply(uow, envelope, payload)
            await uow.commit()

    async def _case(self, uow: UnitOfWork, run_id: str, thread_id: str) -> Case:
        return await uow.cases.get(run_id) or Case(run_id, thread_id)

    async def _note(self, uow: UnitOfWork, env: EventEnvelope, actor: str, text: str, **ids: Any) -> None:
        await uow.timeline.add(TimelineEntry(env.id, env.occurred_at, actor, text[:300], **ids))

    async def _apply(self, uow: UnitOfWork, env: EventEnvelope, p: Any) -> None:
        at = env.occurred_at
        if isinstance(p, c.EmailReceived):
            await uow.emails.record(p.email_id, at)
            for case in await uow.cases.by_email(p.email_id):
                case.received_at = case.received_at or at
                await uow.cases.save(case)
            await self._note(
                uow, env, "Mailbox", f"Email received via {p.provider.upper()}", thread_id=p.thread_id
            )
        elif isinstance(p, c.RunStatusChanged):
            case = await self._case(uow, p.run_id, p.thread_id)
            case.status, case.updated_at = p.status, at
            if p.email_id and not case.email_id:
                case.email_id = p.email_id
                case.received_at = case.received_at or await uow.emails.received(p.email_id)
            if p.status == "queued":
                case.queued_at = case.queued_at or at
            case.paused = case.paused or p.status == "waiting_llm"
            case.jira_key = p.jira_key or case.jira_key
            await uow.cases.save(case)
            if p.status in _PAUSE_NOTES:
                detail = f": {p.detail}" if p.detail and p.status in _WITH_DETAIL else ""
                await self._note(
                    uow,
                    env,
                    "Orchestrator",
                    _PAUSE_NOTES[p.status] + detail,
                    run_id=p.run_id,
                    thread_id=p.thread_id,
                )
        elif isinstance(p, c.AgentToolCalled):
            if p.tool == "classify":
                case = await self._case(uow, p.run_id, p.thread_id)
                case.triaged_at = case.triaged_at or at
                await uow.cases.save(case)
            await self._note(
                uow, env, p.agent, f"{p.tool}: {p.detail}", run_id=p.run_id, thread_id=p.thread_id
            )
        elif isinstance(p, c.ApprovalRequested):
            case = await self._case(uow, p.run_id, p.thread_id)
            if p.gate is c.Gate.TICKET or not case.title:  # the case keeps its ticket's title
                case.requested_at = case.requested_at or at
                case.title, case.severity, case.site = p.title, p.severity, p.site
            await uow.cases.save(case)
            await self._note(
                uow,
                env,
                "Triage" if p.gate is c.Gate.TICKET else "Diagnostics",
                f"Approval requested ({p.gate.value}): {p.title}",
                run_id=p.run_id,
                thread_id=p.thread_id,
            )
        elif isinstance(p, c.ApprovalDecided):
            case = await self._case(uow, p.run_id, p.thread_id)
            if p.gate is c.Gate.TICKET:  # service levels measure the ticket gate
                case.decided_at, case.decision = case.decided_at or at, p.decision.value
            await uow.cases.save(case)
            verb = {"approve": "approved", "reject": "rejected", "send_back": "sent back"}[p.decision.value]
            await self._note(
                uow,
                env,
                _role(p.decided_by_role),
                f"{verb.capitalize()} the {p.gate.value} request",
                run_id=p.run_id,
                thread_id=p.thread_id,
            )
        elif isinstance(p, c.JiraIssueCreated | c.JiraIssueLinked | c.JiraIssueFailed):
            await self._jira(uow, env, p)
        elif isinstance(p, c.BreakerStateChanged):
            dependency = await uow.dependencies.get(p.dependency) or Dependency(p.dependency)
            dependency.record(p.service, p.new, at)
            await uow.dependencies.save(dependency)
            label = LABELS.get(p.dependency, p.dependency)
            text = {"open": f"{label} failing: calls paused", "half_open": f"{label}: testing recovery",
                    "closed": f"{label} recovered"}.get(p.new, f"{label}: {p.new}")  # fmt: skip
            await self._note(uow, env, LABELS.get(p.service, p.service), text, system=True)
        elif isinstance(p, c.ActionExecuted) and p.thread_id:
            outcome = "done" if p.ok else "not run"
            await self._note(
                uow, env, "Ops executor", f"{p.action}: {outcome}. {p.detail}", run_id=p.run_id,
                thread_id=p.thread_id, jira_key=p.jira_key,
            )  # fmt: skip
        elif isinstance(p, c.ProbeStatus):
            region = p.region or ""
            probe = await uow.probes.get(p.probe, region) or Probe(p.probe, region, p.ok)
            if probe.record(p.ok, p.detail, p.latency_ms, at):
                await uow.probes.save(probe)
            state = "healthy" if p.ok else "failing"
            where = f" ({region})" if region else ""
            # During a run the change belongs to that case; a scheduled sweep is a system change shown on
            # every incident open at the time, like a circuit breaker.
            await self._note(
                uow, env, "Health", f"{p.probe}{where} probe {state}: {p.detail}", run_id=p.run_id,
                thread_id=p.thread_id, system=not p.thread_id,
            )  # fmt: skip
        elif isinstance(p, c.PrivacyRecorded) and p.action == "pseudonymise" and p.thread_id:
            await self._note(uow, env, "Privacy gate", p.detail, thread_id=p.thread_id)

    async def _jira(
        self,
        uow: UnitOfWork,
        env: EventEnvelope,
        p: c.JiraIssueCreated | c.JiraIssueLinked | c.JiraIssueFailed,
    ) -> None:
        if not p.run_id:
            return
        case = await self._case(uow, p.run_id, p.thread_id)
        if isinstance(p, c.JiraIssueFailed):
            case.outcome = "failed"
            text, key = f"Jira refused the ticket: {p.reason}", None
        else:
            case.outcome = "created" if isinstance(p, c.JiraIssueCreated) else "linked"
            case.jira_key, case.ticket_at, key = p.issue_key, case.ticket_at or env.occurred_at, p.issue_key
            if isinstance(p, c.JiraIssueCreated) and p.severity:
                case.severity = p.severity  # the approver may have changed the AI's suggestion
            text = (
                f"{p.issue_key} created" if case.outcome == "created" else f"Report linked to {p.issue_key}"
            )
        await uow.cases.save(case)
        await self._note(
            uow, env, "Jira connector", text, run_id=p.run_id, thread_id=p.thread_id, jira_key=key
        )


@dataclass(frozen=True, slots=True)
class DependencyView:
    name: str
    label: str
    health: Health
    since: datetime | None
    callers: dict[str, str]


@dataclass(frozen=True, slots=True)
class ProbeView:
    name: str
    region: str
    health: Health
    detail: str
    latency_ms: int | None
    since: datetime | None


@dataclass(frozen=True, slots=True)
class IncidentView:
    key: str
    title: str | None
    severity: str | None
    site: str | None
    reports: int
    first_seen: datetime | None
    status: str


@dataclass(frozen=True, slots=True)
class Dashboard:
    generated_at: datetime
    dependencies: list[DependencyView]
    probes: list[ProbeView]
    stages: list[StageCount]
    service_levels: list[ServiceLevel]
    incidents: list[IncidentView]


class GetDashboard:
    def __init__(self, uow: UowFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def __call__(self) -> Dashboard:
        now = self._clock.now()
        async with self._uow() as uow:
            cases = await uow.cases.active_or_since(now - WINDOW)
            known = {d.name: d for d in await uow.dependencies.all()}
            ticketed = await uow.cases.incidents(50)
            probes = [
                ProbeView(p.name, p.region, p.health, p.detail, p.latency_ms, p.since)
                for p in await uow.probes.all()
            ]
        names = list(KEY_DEPENDENCIES) + sorted(n for n in known if n not in KEY_DEPENDENCIES)
        dependencies = [
            DependencyView(
                n, LABELS.get(n, n), (d := known.get(n) or Dependency(n)).health, d.since, dict(d.states)
            )
            for n in names
        ]
        incidents: dict[str, IncidentView] = {}
        for case in ticketed:
            key = case.jira_key or ""
            current = incidents.get(key)
            first = min(
                filter(None, [case.started_at, current.first_seen if current else None]), default=None
            )
            incidents[key] = IncidentView(
                key=key,
                title=current.title if current and current.title else case.title,
                severity=current.severity if current and current.severity else case.severity,
                site=current.site if current and current.site else case.site,
                reports=(current.reports if current else 0) + 1,
                first_seen=first,
                status="open in Jira",
            )
        return Dashboard(
            generated_at=now,
            dependencies=dependencies,
            probes=probes,
            stages=stage_counts(cases, today=now.replace(hour=0, minute=0, second=0, microsecond=0)),
            service_levels=service_levels(cases, since=now - WINDOW),
            incidents=sorted(incidents.values(), key=lambda i: i.first_seen or now, reverse=True)[:10],
        )


class GetIncidentTimeline:
    def __init__(self, uow: UowFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def __call__(self, key: str) -> list[TimelineEntry]:
        async with self._uow() as uow:
            cases = await uow.cases.by_ticket(key)
            if not cases:
                return []
            starts = [d for c in cases for d in (c.started_at, c.queued_at, c.updated_at) if d]
            start = min(starts) if starts else self._clock.now() - WINDOW
            return await uow.timeline.for_cases(
                [c.run_id for c in cases], [c.thread_id for c in cases], start=start, end=self._clock.now()
            )
