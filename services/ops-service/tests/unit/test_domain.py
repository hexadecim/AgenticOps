"""Pure rules: allow-list, masking, runbook parsing, log findings, fix candidates."""

from __future__ import annotations

from pathlib import Path

import pytest
from ops_service.adapters.outbound.files import findings, parse_runbook
from ops_service.domain import actions
from ops_service.domain.diagnostics import candidates
from ops_service.domain.errors import ActionRefusedError
from ops_service.domain.masking import mask
from ops_service.domain.model import Environment, RunbookMatch, parse_targets

RUNBOOKS = Path(__file__).parents[4] / "deploy" / "ops" / "runbooks"
ENV = Environment(
    regions=("eu-west-1",),
    sites={"SITE-A014": "eu-west-1", "SITE-B207": "eu-west-1"},
    services=("upload-api", "upload-worker", "login"),
)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "10:02:12 ERROR write failed (user=jane.doe@example.com)",
            "10:02:12 ERROR write failed (user=***)",
        ),
        ("10:09:14 ERROR x (user=a.schmidt, phone=+49 30 1234567)", "10:09:14 ERROR x (user=***, phone=***)"),
        ("from 10.24.3.17 to ops@corp.example", "from x.x.x.x to ***"),
        ("Authorization: Bearer eyJhbGciOi.secret done", "Authorization: Bearer *** done"),
        ("call +49 30 1234567 or 030 1234567", "call *** or ***"),
        ("password=hunter2 token=abc", "password=*** token=***"),
        ("2026-10-08 10:09:14 upload_id=u-88213 left", "2026-10-08 10:09:14 upload_id=u-88213 left"),
    ],
)
def test_masking_removes_people_and_secrets_but_keeps_ids_and_times(line: str, expected: str) -> None:
    assert mask(line) == expected


def test_only_allow_listed_enabled_actions_with_allowed_values_pass() -> None:
    choices = {"service": ["upload-worker", "upload-api"], "region": ["eu-west-1"]}
    enabled = {"restart_service"}
    ok = actions.validate(
        "restart_service",
        {"service": "upload-worker", "region": "eu-west-1"},
        enabled=enabled,
        choices=choices,
    )
    assert ok == {"service": "upload-worker", "region": "eu-west-1"}
    bad = [
        ("drop_database", {}, "not an allow-listed action"),
        ("retry_failed_uploads", {"site": "SITE-A014"}, "not enabled"),
        ("restart_service", {"service": "postgres", "region": "eu-west-1"}, "'postgres' is not allowed"),
        ("restart_service", {"service": "upload-worker"}, "Region is required"),
        ("restart_service", {"service": "upload-worker", "region": "eu-west-1", "force": "1"}, "Unknown"),
    ]
    for action, params, message in bad:
        with pytest.raises(ActionRefusedError, match=message):
            actions.validate(action, params, enabled=enabled, choices=choices)


def test_signature_title_and_enabled_parsing() -> None:
    params = {"region": "eu-west-1", "service": "upload-worker"}
    assert actions.signature("restart_service", params) == (
        'restart_service(service="upload-worker", region="eu-west-1")'
    )
    assert actions.title("restart_service", params) == "Restart upload-worker on eu-west-1?"
    assert actions.parse_enabled("restart_service, rm_rf ,") == {"restart_service"}
    assert actions.parse_enabled("none") == frozenset()
    assert actions.parse_enabled(None) == set(actions.DEFAULT_ENABLED)
    assert parse_targets("Portal=https://p/h, bad, Login=ftp://x") == (("Portal", "https://p/h"),)


def test_runbook_front_matter_names_steps_and_sections_become_chunks() -> None:
    rb = parse_runbook((RUNBOOKS / "RB-07-upload-storage-errors.md").read_text(), "RB-07.md")
    assert rb is not None and rb.id == "RB-07" and rb.services == ("upload-worker", "upload-api")
    assert [(s.step, s.action) for s in rb.steps] == [(3, "restart_service"), (4, "retry_failed_uploads")]
    chunks = rb.chunks()
    assert chunks[0].heading == "Symptoms" and chunks[0].text.startswith(
        "RB-07 Upload storage errors. Symptoms:"
    )
    assert parse_runbook("no front matter") is None


def test_log_findings_are_masked_deduplicated_and_counted() -> None:
    lines = [
        "09:58:02 INFO  started",
        "10:02:12 ERROR storage write failed: EIO (user=jane@example.com)",
        "10:05:31 ERROR storage write failed: EIO (user=m.keller)",
        "10:09:15 WARN  retry 3/3 failed, upload_id=u-88213 left in 'received'",
        "10:12:40 ERROR storage write failed: EIO (user=t.nguyen)",
    ]
    found = findings("upload-worker", lines)
    assert found is not None
    assert found.lines == (
        "10:09:15 WARN  retry 3/3 failed, upload_id=u-88213 left in 'received'",
        "10:12:40 ERROR storage write failed: EIO (user=***)",
    )
    assert found.similar == 2 and found.since == "10:02:12"
    assert findings("x", ["10:00:00 INFO all good"]) is None


def test_candidates_come_only_from_enabled_runbook_steps_with_fillable_parameters() -> None:
    rb = parse_runbook((RUNBOOKS / "RB-07-upload-storage-errors.md").read_text())
    assert rb is not None
    match = RunbookMatch(rb, rb.chunks()[0], 0.9, "semantic")
    found = candidates(
        [match], env=ENV, enabled=frozenset({"restart_service", "retry_failed_uploads"}),
        region="eu-west-1", site="SITE-A014", failing=["upload-worker"],
    )  # fmt: skip
    assert [(c.action, c.params) for c in found] == [
        ("restart_service", {"service": "upload-worker", "region": "eu-west-1"}),
        ("retry_failed_uploads", {"site": "SITE-A014"}),
    ]
    assert found[0].choices == {"service": ["upload-worker", "upload-api"], "region": ["eu-west-1"]}
    assert found[0].json()["signature"] == 'restart_service(service="upload-worker", region="eu-west-1")'
    # No site known → retry_failed_uploads cannot be filled; disabled actions never appear.
    found = candidates(
        [match],
        env=ENV,
        enabled=frozenset({"retry_failed_uploads"}),
        region="eu-west-1",
        site=None,
        failing=[],
    )
    assert found == []
