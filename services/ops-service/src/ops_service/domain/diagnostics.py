"""From probes, logs and runbooks to the fixes a person may be asked to approve. Pure.

The model never invents an action: candidates come only from runbook steps whose action is on the allow-list
and enabled, with parameter choices limited to the environment and the runbook's services.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from .actions import ACTIONS, signature, title
from .model import Environment, LogFinding, ProbeResult, Runbook, RunbookChunk, RunbookMatch

TOP_RUNBOOKS = 3
_WORD = re.compile(r"[a-z0-9][a-z0-9_-]{2,}")
_STOP_WORDS = """the and for with from that this our has have not are was were but you your all any can
will into since after before when what which there their they them then than also just only very hello
regards please thanks thank team support"""
_STOP = frozenset(_STOP_WORDS.split())


@dataclass(frozen=True, slots=True)
class Candidate:
    """One allow-listed fix a runbook step suggests, with the values each parameter may take."""

    action: str
    runbook_id: str
    runbook_title: str
    step: int
    why: str
    params: dict[str, str]  # suggested values
    choices: dict[str, list[str]]  # allowed values per parameter (the approver may pick another)

    def json(self) -> dict[str, Any]:
        spec = ACTIONS[self.action]
        return {
            "action": self.action,
            "signature": signature(self.action, self.params),
            "title": title(self.action, self.params),
            "runbook": {
                "id": self.runbook_id,
                "title": self.runbook_title,
                "step": self.step,
                "why": self.why,
            },
            "params": self.params,
            "params_spec": [
                {"name": p.name, "label": p.label, "choices": self.choices.get(p.name, [])}
                for p in spec.params
            ],
            "effect": spec.effect,
            "rollback": spec.rollback,
        }


@dataclass(frozen=True, slots=True)
class Diagnostics:
    region: str | None
    site: str | None
    probes: list[ProbeResult]
    logs: list[LogFinding]
    runbooks: list[RunbookMatch]
    candidates: list[Candidate]
    environment: Environment
    checked_at: str
    notes: list[str] = field(default_factory=list)
    suspects: list[str] = field(default_factory=list)  # failing services, the noisiest in the logs first

    @property
    def failing(self) -> list[ProbeResult]:
        return [p for p in self.probes if not p.ok]

    def json(self) -> dict[str, Any]:
        sites = self.environment.sites_in(self.region)
        return {
            "region": self.region,
            "site": self.site,
            "checked_at": self.checked_at,
            "probes": [p.json() for p in self.probes],
            "logs": [f.json() for f in self.logs],
            "runbooks": [
                {
                    "id": m.runbook.id,
                    "title": m.runbook.title,
                    "heading": m.chunk.heading,
                    "text": m.chunk.text[:600],
                    "score": round(m.score, 3),
                    "method": m.method,
                }
                for m in self.runbooks
            ],
            "candidates": [c.json() for c in self.candidates],
            "deployment": self.environment.deployment_note,
            "impact": {"sites": sites, **self.environment.impact},
            "notes": self.notes,
            "suspects": self.suspects,
        }


def failing_services(probes: list[ProbeResult]) -> list[str]:
    seen: list[str] = []
    for p in probes:
        if not p.ok:
            seen += [s for s in p.services if s not in seen]
    return seen


def mentioned_services(text: str, services: tuple[str, ...]) -> list[str]:
    lowered = text.lower()
    return [s for s in services if s.lower() in lowered or s.split("-")[0].lower() in lowered.split()]


def search_text(problem: str, probes: list[ProbeResult], logs: list[LogFinding]) -> str:
    """What runbook retrieval looks for: the (tokenised) problem plus what the probes and logs show."""
    parts = [problem]
    parts += [f"{p.name} probe failing: {p.detail}" for p in probes if not p.ok]
    for finding in logs:
        parts += list(finding.lines[:3])
    return "\n".join(parts)[:4000]


def terms(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def keyword_score(query: set[str], chunk: RunbookChunk) -> float:
    words = terms(chunk.text)
    if not query or not words:
        return 0.0
    return len(query & words) / math.sqrt(len(query) * len(words))


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def best_per_runbook(matches: list[RunbookMatch], limit: int = TOP_RUNBOOKS) -> list[RunbookMatch]:
    best: dict[str, RunbookMatch] = {}
    for m in sorted(matches, key=lambda m: m.score, reverse=True):
        best.setdefault(m.runbook.id, m)
    return list(best.values())[:limit]


def candidates(
    matches: list[RunbookMatch],
    *,
    env: Environment,
    enabled: frozenset[str],
    region: str | None,
    site: str | None,
    failing: list[str],
) -> list[Candidate]:
    out: list[Candidate] = []
    if failing:  # a probe fails: only runbooks for the failing services are relevant, not every close match
        matches = [m for m in matches if set(m.runbook.services) & set(failing)] or matches
    for match in matches:
        rb: Runbook = match.runbook
        services = [s for s in rb.services if s in env.services] or list(env.services)
        choices: dict[str, list[str]] = {
            "service": services,
            "region": list(env.regions),
            "site": env.sites_in(region) or sorted(env.sites),
        }
        for step in rb.steps:
            spec = ACTIONS.get(step.action)
            if spec is None or step.action not in enabled:
                continue
            suggested: dict[str, str] = {}
            for p in spec.params:
                options = choices[p.kind]
                if p.kind == "service":
                    pick = next((s for s in failing if s in options), options[0] if options else None)
                elif p.kind == "region":
                    pick = region if region in options else (options[0] if len(options) == 1 else None)
                else:
                    pick = site if site in options else None
                if pick:
                    suggested[p.name] = pick
            if len(suggested) < len(spec.params):
                continue  # a parameter cannot be filled from the evidence: not proposable
            if any(c.action == step.action and c.params == suggested for c in out):
                continue
            out.append(
                Candidate(
                    step.action,
                    rb.id,
                    rb.title,
                    step.step,
                    step.why,
                    suggested,
                    {p.name: choices[p.kind] for p in spec.params},
                )
            )
    return out
