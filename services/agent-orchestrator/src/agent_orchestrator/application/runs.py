"""Run use cases. Events only create or wake runs; the worker (AdvanceRuns) is the one place that executes the
graph, so a slow local model never blocks the event consumers."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import structlog
from langgraph.types import Command
from opspilot_common.events.catalogue import (
    ApprovalDecided,
    EmailReceived,
    JiraIssueCreated,
    JiraIssueFailed,
    JiraIssueLinked,
    RunStatusChanged,
)

from ..domain.errors import LlmUnavailableError, PrivacyUnavailableError, StopRunError
from ..domain.run import Run, RunStatus
from ..ports import AgentPorts, Clock, LlmCircuit, UnitOfWork

log = structlog.get_logger(__name__)
UowFactory = Callable[[], UnitOfWork]
ACTOR = "agent-orchestrator"


def _status_event(run: Run) -> RunStatusChanged:
    return RunStatusChanged(
        run_id=run.id,
        thread_id=run.thread_id,
        status=run.status.value,
        node=run.node,
        detail=run.detail,
        jira_key=run.jira_key,
        email_id=run.email_id,
    )


class StartRun:
    """`email.received` → a queued run (once per email)."""

    def __init__(self, uow: UowFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def __call__(self, p: EmailReceived) -> Run | None:
        async with self._uow() as uow:
            if await uow.runs.by_email(p.email_id):
                return None
            now = self._clock.now()
            run = Run(str(uuid.uuid4()), p.email_id, p.thread_id, RunStatus.QUEUED, now, now, now)
            await uow.runs.add(run)
            uow.events.record(_status_event(run), actor=ACTOR)
            await uow.commit()
            return run


class RecordDecision:
    """`approval.decided` → wake the run waiting at that gate."""

    def __init__(self, uow: UowFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def __call__(self, p: ApprovalDecided) -> bool:
        async with self._uow() as uow:
            run = await uow.runs.get(p.run_id) or await uow.runs.by_approval(p.approval_id)
            if run is None or run.approval_id != p.approval_id:
                return False
            decision = {
                "decision": p.decision.value,
                "reason": p.reason,
                "edits": p.edits,
                "role": p.decided_by_role,
            }
            if not run.decided(self._clock.now(), decision):
                return False
            await uow.runs.save(run)
            await uow.commit()
            return True


class RecordTicketResult:
    """jira-connector's outcome closes the run."""

    def __init__(self, uow: UowFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def __call__(self, p: JiraIssueCreated | JiraIssueLinked | JiraIssueFailed) -> None:
        async with self._uow() as uow:
            run = (await uow.runs.get(p.run_id) if p.run_id else None) or (
                await uow.runs.by_approval(p.approval_id) if p.approval_id else None
            )
            if run is None:
                return
            now = self._clock.now()
            if isinstance(p, JiraIssueFailed):
                run.finish(RunStatus.FAILED, now, f"Jira refused the ticket: {p.reason}")
            else:
                run.ticket(now, p.issue_key)
            await uow.runs.save(run)
            uow.events.record(_status_event(run), actor=ACTOR)
            await uow.commit()


class AdvanceRuns:
    """One worker step: take due runs and move each as far as it can go."""

    def __init__(
        self,
        uow: UowFactory,
        graph: Any,  # compiled LangGraph with a checkpointer
        ports: AgentPorts,
        circuit: LlmCircuit,
        clock: Clock,
        *,
        batch: int = 5,
    ) -> None:
        self._uow, self._graph, self._ports, self._circuit, self._clock = uow, graph, ports, circuit, clock
        self._batch = batch

    async def recover(self) -> int:
        """At start-up: runs a crash left half-done go back to the queue.
        The checkpoint keeps their progress, so they continue where they stopped."""
        async with self._uow() as uow:
            count = await uow.runs.requeue_interrupted(self._clock.now())
            await uow.commit()
        return count

    async def __call__(self) -> int:
        async with self._uow() as uow:
            due = await uow.runs.due(self._clock.now(), self._batch)
        for run in due:
            if run.status is RunStatus.WAITING_LLM and (wait := await self._circuit.retry_after()):
                await self._still_paused(run, wait)  # the model's circuit is still open: do not even try
                continue
            await self._advance(run)
        return len(due)

    async def _still_paused(self, run: Run, seconds: float) -> None:
        def repark(r: Run) -> None:
            r.park(
                RunStatus.WAITING_LLM, self._clock.now(), timedelta(seconds=seconds), r.detail or "AI paused"
            )

        await self._save(run, repark, announce=False)

    async def _advance(self, run: Run) -> None:
        config = {"configurable": {"thread_id": run.id, "ports": self._ports}}
        snapshot = await self._graph.aget_state(config)
        if not snapshot.values:
            graph_input: Any = {"run_id": run.id, "email_id": run.email_id, "thread_id": run.thread_id}
        elif run.status is RunStatus.RESUMING and run.decision is not None:
            graph_input = Command(resume=run.decision)
        else:
            graph_input = None  # continue (or retry the node that failed)
        await self._save(run, lambda r: r.start(self._clock.now()), announce=False)
        now = self._clock.now
        try:
            result = await self._graph.ainvoke(graph_input, config)
        except LlmUnavailableError as llm_down:
            wait, paused = (
                timedelta(seconds=max(60.0, llm_down.retry_after or 0)),
                f"AI paused: {llm_down.message}",
            )
            await self._save(run, lambda r: r.park(RunStatus.WAITING_LLM, now(), wait, paused))
            return
        except PrivacyUnavailableError as privacy_down:
            closed = privacy_down.message
            await self._save(
                run, lambda r: r.park(RunStatus.WAITING_PRIVACY, now(), timedelta(seconds=30), closed)
            )
            return
        except StopRunError as stop:
            stopped = stop.message
            await self._save(run, lambda r: r.finish(RunStatus.FAILED, now(), stopped))
            return
        except Exception as unexpected:
            log.exception("run_step_failed", run=run.id)
            failure = f"{type(unexpected).__name__}: {unexpected}"[:300]
            await self._save(run, lambda r: r.error(now(), failure))
            return
        if result.get("__interrupt__"):
            await self._save(run, lambda r: r.waiting_for_approval(now(), result["approval_id"]))
        elif result.get("outcome") == "ignored":
            await self._save(run, lambda r: r.finish(RunStatus.IGNORED, now(), "Not a support request"))
        elif result.get("outcome") == "approved":
            await self._save(
                run, lambda r: r.finish(RunStatus.APPROVED, now(), "Ticket approved; creating it in Jira")
            )
        else:
            reason = (result.get("decision") or {}).get("reason")
            await self._save(run, lambda r: r.finish(RunStatus.REJECTED, now(), reason))

    async def _save(self, run: Run, change: Callable[[Run], None], *, announce: bool = True) -> None:
        async with self._uow() as uow:
            current = await uow.runs.get(run.id) or run
            change(current)
            await uow.runs.save(current)
            if announce:
                uow.events.record(_status_event(current), actor=ACTOR)
            await uow.commit()
        run.status = current.status


class RunSummary:
    """For the queue's banner: is the AI working, paused, or failing?"""

    def __init__(self, uow: UowFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def __call__(self) -> dict[str, int]:
        async with self._uow() as uow:
            counts = await uow.runs.counts(self._clock.now() - timedelta(hours=24))
        return {status.value: counts.get(status, 0) for status in RunStatus}
