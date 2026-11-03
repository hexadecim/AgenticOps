from __future__ import annotations

from datetime import UTC, datetime, timedelta

from reporting_service.domain.model import (
    Case,
    Dependency,
    Health,
    Stage,
    duration_text,
    service_levels,
    stage_counts,
)

T0 = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)


def case(n: int, status: str, **times: object) -> Case:
    return Case(f"r-{n}", f"t-{n}", status, **times)  # type: ignore[arg-type]


def test_stages_count_open_work_diagnostics_and_resolved_today() -> None:
    cases = [
        case(1, "running"),
        case(2, "waiting_llm"),
        case(3, "waiting_approval"),
        case(4, "ticket_created"),
        case(5, "ignored"),
        case(6, "rejected"),
        case(7, "waiting_ops"),
        case(8, "fix_approved"),
        case(9, "fix_failed"),
        case(10, "resolved", updated_at=T0),
        case(11, "resolved", updated_at=T0 - timedelta(days=1)),
    ]
    stages = {s.stage: s for s in stage_counts(cases, today=T0.replace(hour=0))}
    assert stages[Stage.INTAKE].count == 2 and stages[Stage.INTAKE].note == "1 paused for the AI"
    assert stages[Stage.APPROVAL].count == 1 and stages[Stage.ENGINEERING].count == 2
    assert stages[Stage.DIAGNOSTICS].count == 2 and stages[Stage.DIAGNOSTICS].note == "1 waiting for evidence"
    assert stages[Stage.CLOSED].count == 1  # resolved today only
    assert stages[Stage.CUSTOMER].count is None and "milestone 7" in stages[Stage.CUSTOMER].note


def test_service_levels_from_timings() -> None:
    m = timedelta(minutes=1)
    cases = [
        case(
            1,
            "ticket_created",
            received_at=T0,
            requested_at=T0 + 2 * m,
            decided_at=T0 + 3 * m,
            ticket_at=T0 + 4 * m,
            outcome="created",
        ),
        case(
            2,
            "ticket_created",
            received_at=T0,
            requested_at=T0 + m,
            decided_at=T0 + 9 * m,
            ticket_at=T0 + 10 * m,
            outcome="created",
            paused=True,
        ),
        case(
            3, "ticket_created", received_at=T0, requested_at=T0 + m, ticket_at=T0 + 2 * m, outcome="linked"
        ),
        case(4, "ignored", received_at=T0),
        case(5, "failed", received_at=T0),
        case(
            6, "ticket_created", received_at=T0 - timedelta(days=3), outcome="created", ticket_at=T0
        ),  # too old
    ]
    levels = {s.id: s for s in service_levels(cases, since=T0 - timedelta(hours=24))}
    assert (
        levels["email_to_ticket"].value == "7m 00s" and levels["email_to_ticket"].breached
    )  # median of 4 and 10 min
    assert levels["email_to_ticket"].detail == "target < 5 min · 2 tickets"
    assert levels["email_to_request"].value == "1m 00s"
    assert levels["approval_wait"].value == "4m 30s"
    assert (levels["linked"].value, levels["linked"].detail) == ("1", "of 3 reports")
    assert (
        levels["ignored"].value == "1"
        and levels["needs_person"].value == "1"
        and levels["needs_person"].breached
    )
    assert levels["ai_paused"].value == "1"


def test_a_dependency_is_down_if_any_caller_sees_it_open() -> None:
    d = Dependency("ollama")
    d.record("agent-orchestrator", "open", T0)
    d.record("settings-service", "closed", T0 + timedelta(seconds=5))
    assert d.health is Health.DOWN and d.since == T0 + timedelta(seconds=5)
    d.record("agent-orchestrator", "half_open", T0 + timedelta(minutes=1))
    assert d.health is Health.RECOVERING
    d.record("agent-orchestrator", "closed", T0 + timedelta(minutes=2))
    assert d.health is Health.UP


def test_durations_read_well() -> None:
    assert [duration_text(s) for s in (None, 42, 220, 3 * 3600 + 65)] == ["\u2013", "42s", "3m 40s", "3h 01m"]
