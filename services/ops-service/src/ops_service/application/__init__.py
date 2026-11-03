"""ops-service use cases.

Reading (probes, logs, runbooks) is synchronous for the orchestrator and the approval screen. Writing,
running a production action, happens only after `approval.decided` (gate fix, approve) and goes through a
job per approval, so a crash or an unreachable executor can never run an action twice or without approval.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import structlog
from opspilot_common.auth.principal import Principal
from opspilot_common.auth.roles import Role
from opspilot_common.events.catalogue import (
    ActionExecuted,
    ApprovalDecided,
    ApprovalRequested,
    Decision,
    Gate,
    ProbeStatus,
)

from ..domain import actions as allow_list
from ..domain.diagnostics import (
    Diagnostics,
    best_per_runbook,
    candidates,
    failing_services,
    keyword_score,
    mentioned_services,
    search_text,
    terms,
)
from ..domain.errors import (
    ActionRefusedError,
    EmbeddingUnavailableError,
    EvidenceUnavailableError,
    ExecutorUnavailableError,
    LogsUnavailableError,
    NotAllowedError,
    NotFoundError,
    SettingsUnavailableError,
)
from ..domain.model import (
    FixJob,
    FixProposal,
    JobStatus,
    LogFinding,
    OpsSettings,
    ProbeResult,
    Runbook,
    RunbookMatch,
    corpus_fingerprint,
)
from ..ports import (
    ActionExecutor,
    Clock,
    Embedder,
    EnvironmentSource,
    LogSource,
    ProbeRunner,
    RunbookIndex,
    RunbookSource,
    SettingsSource,
    UnitOfWork,
)

log = structlog.get_logger(__name__)
UowFactory = Callable[[], UnitOfWork]
ACTOR = "ops-service"
# Who may look at evidence (mirrors approval-service: people who work cases, not admins).
VIEWERS = (Role.L1_ANALYST, Role.SUPPORT_AGENT, Role.SUPPORT_MANAGER, Role.COMPLIANCE_LEAD)


class ActiveSettings:
    """The admin's Ops settings, reloaded when `settings.changed` names the ops or llm section."""

    def __init__(self, source: SettingsSource) -> None:
        self._source = source
        self._current: OpsSettings | None = None
        self._lock = asyncio.Lock()

    async def get(self, *, refresh: bool = False) -> OpsSettings:
        async with self._lock:
            if refresh or self._current is None:
                try:
                    self._current = await self._source.ops()
                except SettingsUnavailableError:
                    if self._current is None:
                        raise  # keep using the last good settings while settings-service is away
            return self._current


class RunbookLibrary:
    """Runbooks from the configured folder, embedded into the index (pgvector) when they change.
    When the embedding model is down, retrieval falls back to keyword overlap and says so."""

    def __init__(self, source: RunbookSource, index: RunbookIndex, embedder: Embedder) -> None:
        self._source, self._index, self._embedder = source, index, embedder
        self._runbooks: dict[str, Runbook] = {}
        self._folder: str | None = None
        self.semantic = False  # the index matches the current runbooks and model
        self._lock = asyncio.Lock()

    @property
    def runbooks(self) -> list[Runbook]:
        return list(self._runbooks.values())

    async def refresh(self, settings: OpsSettings, *, force: bool = False) -> bool:
        """Reload and (re)index if anything changed. True when the semantic index is usable."""
        async with self._lock:
            if force or self._folder != settings.runbook_folder or not self._runbooks:
                self._runbooks = {rb.id: rb for rb in await self._source.load(settings.runbook_folder)}
                self._folder = settings.runbook_folder
            books = self.runbooks
            fingerprint = corpus_fingerprint(books, settings.embedding_model)
            if await self._index.fingerprint() == fingerprint:
                self.semantic = True
                return True
            chunks = [c for rb in books for c in rb.chunks()]
            try:
                vectors = await self._embedder.embed(settings, [c.text for c in chunks]) if chunks else []
            except EmbeddingUnavailableError as exc:
                log.warning("runbook_index_stale", error=exc.message)
                self.semantic = False
                return False
            await self._index.replace(fingerprint, list(zip(chunks, vectors, strict=True)))
            log.info("runbooks_indexed", runbooks=len(books), chunks=len(chunks))
            self.semantic = True
            return True

    async def search(self, settings: OpsSettings, text: str) -> tuple[list[RunbookMatch], str | None]:
        """Best chunk per runbook, top 3; plus a note when the keyword fallback was used."""
        if not self._runbooks or self._folder != settings.runbook_folder:
            await self.refresh(settings)
        chunks = {(c.runbook_id, c.index): c for rb in self.runbooks for c in rb.chunks()}
        note = None
        if self.semantic or await self.refresh(settings):
            try:
                (vector,) = await self._embedder.embed(settings, [text])
                hits = await self._index.nearest(vector, 12)
                matches = [
                    RunbookMatch(self._runbooks[rid], chunks[(rid, i)], score, "semantic")
                    for rid, i, score in hits
                    if (rid, i) in chunks
                ]
                return best_per_runbook(matches), None
            except EmbeddingUnavailableError as exc:
                note = f"Runbook search used keywords: the embedding model is unavailable ({exc.message})."
        else:
            note = "Runbook search used keywords: runbooks are not indexed yet (embedding model unavailable)."
        query = terms(text)
        scored = [
            RunbookMatch(self._runbooks[c.runbook_id], c, keyword_score(query, c), "keyword")
            for c in chunks.values()
        ]
        return best_per_runbook([m for m in scored if m.score > 0]), note


class CheckHealth:
    """Runs the probes for a region and publishes `probe.status` only when a probe changes state, or for every
    probe when `announce` is set (the first sweep after start-up: read models that missed history, or were
    added later, learn the current state without a replay)."""

    def __init__(self, uow: UowFactory, probes: ProbeRunner, clock: Clock) -> None:
        self._uow, self._probes, self._clock = uow, probes, clock

    async def __call__(
        self,
        region: str | None,
        *,
        run_id: str | None = None,
        thread_id: str | None = None,
        announce: bool = False,
    ) -> list[ProbeResult]:
        results = await self._probes.run(region)
        now = self._clock.now()
        async with self._uow() as uow:
            for p in results:
                changed = await uow.probe_states.last(p.name, p.region) is not p.ok
                if changed:
                    await uow.probe_states.set(p.name, p.region, p.ok, now)
                if changed or announce:
                    uow.events.record(
                        ProbeStatus(
                            probe=p.name,
                            ok=p.ok,
                            detail=p.detail,
                            latency_ms=p.latency_ms,
                            region=p.region,
                            run_id=run_id,
                            thread_id=thread_id,
                        ),
                        actor="Health",
                    )
            await uow.commit()
        return results


@dataclass(frozen=True, slots=True)
class DiagnoseQuery:
    text: str  # tokenised problem: summary, category, error
    site: str | None = None
    asset: str | None = None
    run_id: str | None = None
    thread_id: str | None = None


class Diagnose:
    """Probes, masked logs and runbooks for one problem, and the allow-listed fixes they support."""

    def __init__(
        self,
        settings: ActiveSettings,
        environment: EnvironmentSource,
        health: CheckHealth,
        logs: LogSource,
        library: RunbookLibrary,
        clock: Clock,
    ) -> None:
        self._settings, self._env, self._health = settings, environment, health
        self._logs, self._library, self._clock = logs, library, clock

    async def __call__(self, q: DiagnoseQuery) -> Diagnostics:
        cfg = await self._settings.get()
        env = await self._env.environment()
        region = env.region_of(q.site)
        probes = await self._health(region, run_id=q.run_id, thread_id=q.thread_id)
        failing = failing_services(probes)
        services = failing or mentioned_services(f"{q.text} {q.asset or ''}", env.services)
        notes: list[str] = []
        logs: list[LogFinding] = []
        if services:
            try:
                logs = await self._logs.search(cfg.log_folder, services)
            except LogsUnavailableError as exc:
                notes.append(f"Logs unavailable ({exc.message}); judged on probes and runbooks only.")
        # A probe exercises several services; the one whose logs show the most problems is the suspect.
        noise = {f.service: len(f.lines) + f.similar for f in logs}
        logs.sort(key=lambda f: noise[f.service], reverse=True)
        failing.sort(key=lambda s: noise.get(s, 0), reverse=True)
        matches, note = await self._library.search(cfg, search_text(q.text, probes, logs))
        found = candidates(
            matches, env=env, enabled=cfg.enabled_actions, region=region, site=q.site, failing=failing,
            ranked_by_logs=any(noise.get(s, 0) for s in failing),
        )  # fmt: skip
        if note:
            notes.insert(0, note)
        if not failing:
            notes.append("All probes pass right now.")
        return Diagnostics(
            region, q.site, probes, logs, matches, found, env, self._clock.now().isoformat(), notes, failing
        )


class GetEvidence:
    """Fresh probes and logs for a pending fix, for the approval screen (never cached: evidence is now)."""

    def __init__(self, uow: UowFactory, settings: ActiveSettings, health: CheckHealth, logs: LogSource,
                 clock: Clock) -> None:  # fmt: skip
        self._uow, self._settings, self._health, self._logs, self._clock = uow, settings, health, logs, clock

    async def __call__(self, principal: Principal, approval_id: str) -> dict[str, Any]:
        if not principal.has_any(*VIEWERS):
            raise NotAllowedError("Your role cannot see production evidence.")
        async with self._uow() as uow:
            proposal = await uow.proposals.get(approval_id)
            job = await uow.jobs.get(approval_id)
        if proposal is None:
            raise NotFoundError("No production fix was proposed under this approval.")
        cfg = await self._settings.get()
        probes = await self._health(proposal.region, run_id=proposal.run_id, thread_id=proposal.thread_id)
        services = proposal.services or [proposal.params.get("service", "")]
        notes: list[str] = []
        try:
            logs = await self._logs.search(cfg.log_folder, [s for s in services if s])
        except LogsUnavailableError as exc:
            logs, notes = [], [f"Logs unavailable ({exc.message})."]
        return {
            "checked_at": self._clock.now().isoformat(),
            "region": proposal.region,
            "probes": [p.json() for p in probes],
            "logs": [f.json() for f in logs],
            "notes": notes,
            "executor": cfg.executor,
            "enabled": proposal.action in cfg.enabled_actions,
            "job": None
            if job is None
            else {"status": job.status.value, "ok": job.ok, "detail": job.detail, "attempts": job.attempts},
        }


def proposal_from(p: ApprovalRequested) -> FixProposal | None:
    data: dict[str, Any] = p.proposal
    action: dict[str, Any] = data.get("action") or {}
    name = action.get("name")
    if not isinstance(name, str):
        return None
    choices = {
        str(spec.get("name")): [str(v) for v in spec.get("choices", [])]
        for spec in action.get("params_spec", [])
        if isinstance(spec, dict)
    }
    return FixProposal(
        approval_id=p.approval_id,
        run_id=p.run_id,
        thread_id=p.thread_id,
        action=name,
        params={str(k): str(v) for k, v in (action.get("params") or {}).items()},
        choices=choices,
        region=data.get("region"),
        site=data.get("site") or p.site,
        jira_key=p.jira_key or data.get("jira_key"),
        services=[str(s) for s in data.get("services", [])],
    )


class StoreFixProposal:
    def __init__(self, uow: UowFactory) -> None:
        self._uow = uow

    async def __call__(self, p: ApprovalRequested) -> None:
        if p.gate is not Gate.FIX:
            return
        proposal = proposal_from(p)
        if proposal is None:
            log.warning("fix_proposal_without_action", approval=p.approval_id)
            return
        async with self._uow() as uow:
            if await uow.proposals.get(p.approval_id) is None:
                await uow.proposals.add(proposal)
                await uow.commit()


def _refused(job: FixJob, detail: str, executor: str | None = None) -> ActionExecuted:
    return ActionExecuted(
        approval_id=job.id, action=job.action, params=job.params, ok=False, detail=detail,
        run_id=job.run_id, thread_id=job.thread_id, jira_key=job.jira_key, executor=executor,
    )  # fmt: skip


class QueueFix:
    """An approved fix gate becomes a job, in the same transaction as the inbox entry. The approver's edited
    parameters replace the proposal's; anything not on the allow-list is refused here already."""

    def __init__(self, uow: UowFactory, settings: ActiveSettings, environment: EnvironmentSource,
                 clock: Clock) -> None:  # fmt: skip
        self._uow, self._settings, self._env, self._clock = uow, settings, environment, clock

    async def __call__(self, p: ApprovalDecided) -> bool:
        if p.gate is not Gate.FIX or p.decision is not Decision.APPROVE:
            return False
        cfg, env = await self._settings.get(), await self._env.environment()
        async with self._uow() as uow:
            if await uow.jobs.get(p.approval_id):
                return False
            proposal = await uow.proposals.get(p.approval_id)
            now = self._clock.now()
            if proposal is None:
                job = FixJob(p.approval_id, p.run_id, p.thread_id, "unknown", {}, now, now)
                job.refuse("The fix proposal never reached ops-service; nothing ran.")
                await uow.jobs.add(job)
                uow.events.record(_refused(job, job.detail or ""), actor=ACTOR)
                await uow.commit()
                return False
            edited = p.edits.get("params")
            params = {**proposal.params, **(edited if isinstance(edited, dict) else {})}
            job = FixJob(
                p.approval_id, p.run_id, p.thread_id, proposal.action, {k: str(v) for k, v in params.items()},
                now, now, jira_key=proposal.jira_key, region=proposal.region,
            )  # fmt: skip
            try:
                job.params = allow_list.validate(
                    job.action,
                    params,
                    enabled=cfg.enabled_actions,
                    choices=_choices(proposal, env_values(env)),
                )
            except ActionRefusedError as exc:
                job.refuse(f"Refused before running: {exc.message}")
                uow.events.record(_refused(job, job.detail or ""), actor=ACTOR)
            await uow.jobs.add(job)
            await uow.commit()
            return job.status is JobStatus.PENDING


def env_values(env: Any) -> dict[str, list[str]]:
    return {"service": list(env.services), "region": list(env.regions), "site": sorted(env.sites)}


def _choices(proposal: FixProposal, env: Mapping[str, list[str]]) -> dict[str, list[str]]:
    """Allowed values per parameter: what the proposal offered AND what the environment still has."""
    spec = allow_list.ACTIONS.get(proposal.action)
    if spec is None:
        return {}
    return {
        p.name: [v for v in proposal.choices.get(p.name, []) if v in env.get(p.kind, [])] for p in spec.params
    }


class RunDueFixes:
    """Executes pending jobs: fresh evidence first, then the configured executor, then `action.executed`."""

    def __init__(
        self,
        uow: UowFactory,
        settings: ActiveSettings,
        executors: Mapping[str, ActionExecutor],
        health: CheckHealth,
        clock: Clock,
        *,
        batch: int = 5,
    ) -> None:
        self._uow, self._settings, self._executors = uow, settings, executors
        self._health, self._clock, self._batch = health, clock, batch

    async def __call__(self) -> int:
        async with self._uow() as uow:
            jobs = await uow.jobs.due(self._clock.now(), self._batch)
        for job in jobs:
            await self._run(job)
        return len(jobs)

    async def _run(self, job: FixJob) -> None:
        cfg = await self._settings.get()
        executor = self._executors.get(cfg.executor)
        event: ActionExecuted | None = None
        now = self._clock.now()
        if executor is None:
            job.refuse(f"Executor '{cfg.executor}' is not installed; nothing ran.")
            event = _refused(job, job.detail or "")
        elif job.action not in cfg.enabled_actions:
            job.refuse(f"{job.action} was disabled after approval; nothing ran.")
            event = _refused(job, job.detail or "", executor.name)
        else:
            try:
                before = await self._health(job.region, run_id=job.run_id, thread_id=job.thread_id)
                result = await executor.execute(job.action, job.params, key=job.id)
            except EvidenceUnavailableError as exc:
                if not job.retry_later(now, f"No fresh evidence, not running yet: {exc.message}"):
                    event = _refused(job, job.detail or "", executor.name)
            except ExecutorUnavailableError as exc:
                if not job.retry_later(now, f"Executor unavailable: {exc.message}"):
                    event = _refused(job, job.detail or "", executor.name)
            else:
                failing = [p.name for p in before if not p.ok]
                context = (
                    f" (before: {', '.join(failing)} failing)" if failing else " (before: all probes passed)"
                )
                job.done(result.ok, result.detail + context)
                event = ActionExecuted(
                    approval_id=job.id, action=job.action, params=job.params, ok=result.ok,
                    detail=job.detail or "", run_id=job.run_id, thread_id=job.thread_id,
                    jira_key=job.jira_key, executor=executor.name,
                )  # fmt: skip
                log.info("fix_executed", job=job.id, action=job.action, ok=result.ok)
        async with self._uow() as uow:
            await uow.jobs.save(job)
            if event is not None:
                uow.events.record(event, actor=ACTOR)
            await uow.commit()


__all__ = [
    "ActiveSettings",
    "CheckHealth",
    "Diagnose",
    "DiagnoseQuery",
    "GetEvidence",
    "QueueFix",
    "RunDueFixes",
    "RunbookLibrary",
    "StoreFixProposal",
    "proposal_from",
]
