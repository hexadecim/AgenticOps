"""The allow-list: the only production actions OpsPilot can ever propose or run. Pure.

Add an action: add an `ActionSpec` here, name it in a runbook step, enable it in Admin > Integrations > Ops,
and teach each executor adapter to run it. Anything not listed here is refused at proposal *and* at run time.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from .errors import ActionRefusedError

ParamKind = Literal["service", "region", "site"]


@dataclass(frozen=True, slots=True)
class ParamSpec:
    name: str
    label: str
    kind: ParamKind


@dataclass(frozen=True, slots=True)
class ActionSpec:
    name: str
    title: str  # question shown at gate T2, formatted with the parameters
    params: tuple[ParamSpec, ...]
    effect: str
    rollback: str


ACTIONS: dict[str, ActionSpec] = {
    a.name: a
    for a in (
        ActionSpec(
            "restart_service",
            "Restart {service} on {region}?",
            (ParamSpec("service", "Service", "service"), ParamSpec("region", "Region", "region")),
            effect="The service restarts in about a minute; queued work is retried automatically.",
            rollback="None needed; a restart is stateless. If the probes still fail, escalate to on-call.",
        ),
        ActionSpec(
            "retry_failed_uploads",
            "Retry failed uploads for {site}?",
            (ParamSpec("site", "Site", "site"),),
            effect="Uploads left in 'received' for the site are queued again; nothing is deleted.",
            rollback="None needed; a retry that fails again leaves the upload as it was.",
        ),
    )
}

DEFAULT_ENABLED = ("restart_service", "retry_failed_uploads")


def parse_enabled(raw: str | None) -> frozenset[str]:
    """'restart_service, retry_failed_uploads' → known names only (unknown names are ignored)."""
    if raw is None:
        return frozenset(DEFAULT_ENABLED)
    names = {n.strip() for n in raw.replace(";", ",").split(",") if n.strip()}
    return frozenset(n for n in names if n in ACTIONS)


def signature(action: str, params: Mapping[str, str]) -> str:
    """restart_service(service="upload-worker", region="eu-west-1"), in the spec's parameter order."""
    spec = ACTIONS.get(action)
    order = [p.name for p in spec.params] if spec else sorted(params)
    args = ", ".join(f'{k}="{params[k]}"' for k in order if k in params)
    return f"{action}({args})"


def title(action: str, params: Mapping[str, str]) -> str:
    spec = ACTIONS[action]
    try:
        return spec.title.format(**params)
    except KeyError:
        return f"Run {signature(action, params)}?"


def validate(
    action: str,
    params: Mapping[str, object],
    *,
    enabled: Iterable[str],
    choices: Mapping[str, Iterable[str]],
) -> dict[str, str]:
    """Return clean parameters, or raise ActionRefusedError. `choices` maps a parameter *name* to the values
    allowed for it (from the environment and the runbook). Every parameter is required; extras are refused."""
    spec = ACTIONS.get(action)
    if spec is None:
        raise ActionRefusedError(f"{action} is not an allow-listed action.")
    if action not in set(enabled):
        raise ActionRefusedError(f"{action} is not enabled in Admin > Integrations > Ops.")
    known = {p.name for p in spec.params}
    extra = set(params) - known
    if extra:
        raise ActionRefusedError(f"Unknown parameters for {action}: {', '.join(sorted(extra))}.")
    clean: dict[str, str] = {}
    for p in spec.params:
        value = str(params.get(p.name) or "").strip()
        if not value:
            raise ActionRefusedError(f"{p.label} is required.")
        allowed = set(choices.get(p.name, ()))
        if value not in allowed:
            raise ActionRefusedError(f"{p.label} '{value}' is not allowed here.")
        clean[p.name] = value
    return clean
