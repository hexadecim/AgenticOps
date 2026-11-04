"""ops-service entities and value objects. Pure."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from .actions import parse_enabled

MAX_BACKOFF = timedelta(minutes=10)
MAX_ATTEMPTS = 6


@dataclass(frozen=True, slots=True)
class ProbeResult:
    name: str  # what the approver sees: Portal, Login, Upload, ...
    ok: bool
    detail: str
    latency_ms: int | None = None
    services: tuple[str, ...] = ()  # services this probe exercises
    region: str | None = None

    def json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
            "services": list(self.services),
            "region": self.region,
        }


@dataclass(frozen=True, slots=True)
class LogFinding:
    """Masked log lines for one service: the most recent problems plus how many similar lines there were."""

    service: str
    lines: tuple[str, ...]
    similar: int = 0
    since: str | None = None

    def json(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "lines": list(self.lines),
            "similar": self.similar,
            "since": self.since,
        }


@dataclass(frozen=True, slots=True)
class RunbookStep:
    step: int
    action: str
    why: str


@dataclass(frozen=True, slots=True)
class Runbook:
    id: str  # RB-07
    title: str
    services: tuple[str, ...]
    steps: tuple[RunbookStep, ...]
    sections: tuple[tuple[str, str], ...]  # (heading, text)
    source: str = ""

    def chunks(self) -> list[RunbookChunk]:
        """One chunk per section, prefixed with the title so each chunk stands on its own for retrieval."""
        out = []
        for i, (heading, text) in enumerate(self.sections):
            body = re.sub(r"\s+", " ", text).strip()
            if body:
                out.append(RunbookChunk(self.id, i, heading, f"{self.id} {self.title}. {heading}: {body}"))
        return out


@dataclass(frozen=True, slots=True)
class RunbookChunk:
    runbook_id: str
    index: int
    heading: str
    text: str


@dataclass(frozen=True, slots=True)
class RunbookMatch:
    runbook: Runbook
    chunk: RunbookChunk
    score: float
    method: str  # semantic | keyword


def corpus_fingerprint(runbooks: list[Runbook], model: str) -> str:
    """Changes when any runbook text or the embedding model changes: then the index is rebuilt."""
    digest = hashlib.sha256(model.encode())
    for rb in sorted(runbooks, key=lambda r: r.id):
        for chunk in rb.chunks():
            digest.update(chunk.text.encode())
    return digest.hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class Environment:
    """What can be probed and acted on: regions, sites and services (from the environment adapter)."""

    regions: tuple[str, ...]
    sites: dict[str, str]  # site id → region
    services: tuple[str, ...]
    deployment_note: str | None = None
    impact: dict[str, int] = field(default_factory=dict)  # e.g. {"uploads waiting": 41}

    def region_of(self, site: str | None) -> str | None:
        if site and site in self.sites:
            return self.sites[site]
        return self.regions[0] if len(self.regions) == 1 else None

    def sites_in(self, region: str | None) -> list[str]:
        return sorted(s for s, r in self.sites.items() if region is None or r == region)


@dataclass(frozen=True, slots=True)
class OpsSettings:
    version: int = 0
    executor: str = "mock"
    enabled_actions: frozenset[str] = field(default_factory=lambda: parse_enabled(None))
    runbook_folder: str = "/etc/opspilot/ops/runbooks"
    log_folder: str = "/etc/opspilot/ops/logs"
    probe_targets: tuple[tuple[str, str], ...] = ()  # (name, url) for real HTTP probes
    embedding_model: str = "nomic-embed-text"
    ollama_url: str = "http://host.docker.internal:11434"


def parse_targets(raw: str | None) -> tuple[tuple[str, str], ...]:
    """'Portal=https://portal.example.com/health, Login=https://…' → (('Portal', 'https://…'), …)."""
    out = []
    for item in (raw or "").replace("\n", ",").split(","):
        name, _, url = item.partition("=")
        if name.strip() and url.strip().startswith(("http://", "https://")):
            out.append((name.strip()[:40], url.strip()))
    return tuple(out)


@dataclass(slots=True)
class FixProposal:
    """What the orchestrator asked a person to approve at T2 (from `approval.requested`, gate fix)."""

    approval_id: str
    run_id: str
    thread_id: str
    action: str
    params: dict[str, str]
    choices: dict[str, list[str]]
    region: str | None
    site: str | None
    jira_key: str | None
    services: list[str] = field(default_factory=list)  # whose logs and probes are evidence


class JobStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"  # refused or gave up: a person has to act


@dataclass(slots=True)
class FixJob:
    """One approved production action. Its id is the approval id: the idempotency key end to end."""

    id: str
    run_id: str
    thread_id: str
    action: str
    params: dict[str, str]
    created_at: datetime
    next_attempt_at: datetime
    jira_key: str | None = None
    region: str | None = None
    status: JobStatus = JobStatus.PENDING
    attempts: int = 0
    detail: str | None = None
    ok: bool | None = None
    trace: str | None = None  # W3C trace context of the case

    def done(self, ok: bool, detail: str) -> None:
        self.status, self.ok, self.detail = JobStatus.DONE, ok, detail
        self.attempts += 1

    def refuse(self, detail: str) -> None:
        self.status, self.ok, self.detail = JobStatus.FAILED, False, detail

    def retry_later(self, now: datetime, reason: str) -> bool:
        """Back off; False when it has been tried too often (then it fails for good)."""
        self.attempts += 1
        if self.attempts >= MAX_ATTEMPTS:
            self.refuse(f"Gave up after {self.attempts} attempts: {reason}")
            return False
        backoff = min(timedelta(seconds=15 * 2 ** (self.attempts - 1)), MAX_BACKOFF)
        self.next_attempt_at, self.detail = now + backoff, reason
        return True
