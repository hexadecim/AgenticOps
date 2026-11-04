"""jira-connector use cases. Jira writes happen only for approved ticket gates, through a job per approval."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import Any

import structlog
from opspilot_common.events.catalogue import (
    ApprovalDecided,
    ApprovalRequested,
    Decision,
    Gate,
    JiraIssueCreated,
    JiraIssueFailed,
    JiraIssueLinked,
)
from opspilot_common.telemetry import current_traceparent, span

from ..domain.errors import SettingsUnavailableError, TrackerRejectedError, TrackerUnavailableError
from ..domain.model import Action, JiraContext, JiraSettings, Job, JobStatus, Proposal, TicketFields
from ..ports import Clock, JiraTracker, SettingsSource, TrackerFactory, UnitOfWork

log = structlog.get_logger(__name__)
UowFactory = Callable[[], UnitOfWork]


class ActiveTracker:
    """The Jira client for the current settings; rebuilt when the admin changes them (no restart)."""

    def __init__(self, source: SettingsSource, factory: TrackerFactory) -> None:
        self._source, self._factory = source, factory
        self._current: tuple[JiraSettings, JiraTracker] | None = None
        self._loaded = False
        self._lock = asyncio.Lock()

    async def get(self, *, refresh: bool = False) -> tuple[JiraSettings, JiraTracker] | None:
        async with self._lock:
            if refresh or not self._loaded:
                settings = await self._source.jira()
                self._loaded = True
                old = self._current
                if settings is None:
                    self._current = None
                elif old is None or old[0].version != settings.version:
                    self._current = (settings, self._factory(settings))
                replaced = old is not None and (self._current is None or self._current[1] is not old[1])
                if replaced and old is not None:
                    with contextlib.suppress(Exception):  # best effort: never block the swap
                        await old[1].aclose()
            return self._current

    async def close(self) -> None:
        if self._current is not None:
            await self._current[1].aclose()
            self._current = None


def proposal_from(p: ApprovalRequested) -> Proposal:
    data: dict[str, Any] = p.proposal
    ticket = data.get("ticket", {})
    duplicate = data.get("duplicate_of") or {}
    action = Action.LINK if data.get("action") == "link" and duplicate.get("key") else Action.CREATE
    return Proposal(
        approval_id=p.approval_id,
        run_id=p.run_id,
        thread_id=p.thread_id,
        action=action,
        ticket=TicketFields(
            summary=str(ticket.get("summary") or p.title)[:240],
            description=str(ticket.get("description", "")),
            severity=ticket.get("severity") or p.severity,
            site=ticket.get("site") or p.site,
            category=ticket.get("category"),
        ),
        link_to=duplicate.get("key") if action is Action.LINK else None,
        reporter_token=data.get("reporter_token"),
    )


class StoreProposal:
    def __init__(self, uow: UowFactory) -> None:
        self._uow = uow

    async def __call__(self, p: ApprovalRequested) -> None:
        if p.gate is not Gate.TICKET:
            return
        async with self._uow() as uow:
            if await uow.proposals.get(p.approval_id) is None:
                await uow.proposals.add(proposal_from(p))
                await uow.commit()


class QueueJob:
    """An approved ticket gate becomes a job in the same transaction as the inbox entry (outbox for Jira)."""

    def __init__(self, uow: UowFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def __call__(self, p: ApprovalDecided) -> None:
        if p.gate is not Gate.TICKET or p.decision is not Decision.APPROVE:
            return
        async with self._uow() as uow:
            if await uow.jobs.get(p.approval_id):
                return
            proposal = await uow.proposals.get(p.approval_id)
            if proposal is None:
                uow.events.record(
                    JiraIssueFailed(
                        approval_id=p.approval_id,
                        thread_id=p.thread_id,
                        run_id=p.run_id,
                        reason="The ticket proposal never reached jira-connector.",
                    ),
                    actor="jira-connector",
                )
            else:
                now = self._clock.now()
                await uow.jobs.add(
                    Job(
                        id=p.approval_id,
                        run_id=p.run_id,
                        thread_id=p.thread_id,
                        action=proposal.action,
                        ticket=proposal.ticket.with_edits(p.edits),
                        created_at=now,
                        next_attempt_at=now,
                        link_to=proposal.link_to,
                        reporter_token=proposal.reporter_token,
                        trace=current_traceparent(),
                    )
                )
            await uow.commit()


def _description(job: Job) -> str:
    reporter = f"reporter [{job.reporter_token}]" if job.reporter_token else "the reporter"
    return (
        f"{job.ticket.description.strip()}\n\n"
        f"Created by OpsPilot after a person approved it (approval {job.id[:8]}, {reporter}). "
        "Personal data is replaced by tokens."
    )


def _comment(job: Job) -> str:
    reporter = f"[{job.reporter_token}]" if job.reporter_token else "another reporter"
    site = f" at {job.ticket.site}" if job.ticket.site else ""
    return (
        f"Another report of this incident from {reporter}{site}: {job.ticket.summary}\n\n"
        f"Linked by OpsPilot after approval {job.id[:8]}."
    )


class ProcessDueJobs:
    def __init__(self, uow: UowFactory, active: ActiveTracker, clock: Clock, *, batch: int = 10) -> None:
        self._uow, self._active, self._clock, self._batch = uow, active, clock, batch

    async def __call__(self) -> int:
        now = self._clock.now()
        async with self._uow() as uow:
            jobs = await uow.jobs.due(now, self._batch)
        if not jobs:
            return 0
        try:
            current = await self._active.get()
        except SettingsUnavailableError as exc:
            log.warning("jira_settings_unavailable", error=exc.message)
            return 0
        for job in jobs:
            with span("jira.job", traceparent=job.trace, job_id=job.id):
                await self._run(job, current)
        return len(jobs)

    async def _run(self, job: Job, current: tuple[JiraSettings, JiraTracker] | None) -> None:
        now = self._clock.now()
        event: Any = None
        if current is None:
            job.wait(now, "Jira is not configured yet (Admin > Integrations).")
        else:
            settings, tracker = current
            try:
                if job.action is Action.CREATE:
                    key = await tracker.find_by_label(job.label)  # a crash after creating must not duplicate
                    if key is None:
                        fields = TicketFields(
                            job.ticket.summary, _description(job), job.ticket.severity, job.ticket.site
                        )
                        key = await tracker.create(fields, job.labels())
                    event = JiraIssueCreated(
                        approval_id=job.id,
                        thread_id=job.thread_id,
                        issue_key=key,
                        project=settings.project_key,
                        severity=job.ticket.severity,
                        run_id=job.run_id,
                    )
                else:
                    assert job.link_to
                    await tracker.comment(job.link_to, _comment(job))
                    key = job.link_to
                    event = JiraIssueLinked(
                        thread_id=job.thread_id,
                        issue_key=key,
                        reporter_token=job.reporter_token,
                        site=job.ticket.site,
                        approval_id=job.id,
                        run_id=job.run_id,
                    )
                job.done(key)
                log.info("jira_job_done", job=job.id, key=key, action=job.action.value)
            except TrackerUnavailableError as exc:
                job.retry_later(now, exc.message, exc.retry_after)
                log.warning("jira_job_deferred", job=job.id, error=exc.message)
            except TrackerRejectedError as exc:
                job.fail(exc.message)
                event = JiraIssueFailed(
                    approval_id=job.id, thread_id=job.thread_id, reason=exc.message, run_id=job.run_id
                )
                log.error("jira_job_failed", job=job.id, error=exc.message)
        async with self._uow() as uow:
            await uow.jobs.save(job)
            if event is not None:
                uow.events.record(event, actor="jira-connector")
            await uow.commit()


class GetContext:
    """What the orchestrator needs for duplicate detection and the ticket preview."""

    def __init__(self, active: ActiveTracker) -> None:
        self._active = active

    async def __call__(self, hours: int) -> JiraContext:
        current = await self._active.get()
        if current is None:
            return JiraContext(configured=False)
        settings, tracker = current
        return JiraContext(True, settings.project_key, settings.issue_type, await tracker.recent(hours))


__all__ = [
    "ActiveTracker",
    "GetContext",
    "JobStatus",
    "ProcessDueJobs",
    "QueueJob",
    "StoreProposal",
    "proposal_from",
]
