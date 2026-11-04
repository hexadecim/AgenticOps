"""Settings use cases: read (secrets masked), save (validated, versioned, audited), test connection."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import structlog
from opspilot_common.events.catalogue import SettingsChanged

from ..domain.catalogue import READERS, SECTIONS
from ..domain.document import SettingsDocument
from ..domain.errors import (
    InvalidSettingsError,
    ReaderNotAllowedError,
    UnknownSectionError,
    VersionConflictError,
)
from ..domain.spec import SectionSpec
from ..domain.targets import TargetPolicy
from ..ports import Check, Clock, ConnectionReport, ConnectionTester, UnitOfWork

log = structlog.get_logger(__name__)

UowFactory = Callable[[], UnitOfWork]
TesterKey = str  # section id, or "<section>:<provider>" for sections with a provider choice


@dataclass(frozen=True, slots=True)
class SectionView:
    spec: SectionSpec
    version: int
    values: dict[str, Any]
    secrets_set: dict[str, bool]
    updated_at: Any
    updated_by: str | None


def _spec(section: str) -> SectionSpec:
    spec = SECTIONS.get(section)
    if spec is None:
        raise UnknownSectionError(section)
    return spec


def _view(spec: SectionSpec, doc: SettingsDocument | None) -> SectionView:
    doc = doc or SettingsDocument(spec.id)
    return SectionView(
        spec=spec,
        version=doc.version,
        values={**spec.defaults(), **doc.values},
        secrets_set={k: k in doc.secrets for k in sorted(spec.secret_keys)},
        updated_at=doc.updated_at,
        updated_by=doc.updated_by,
    )


class GetSection:
    def __init__(self, uow: UowFactory) -> None:
        self._uow = uow

    async def __call__(self, section: str) -> SectionView:
        spec = _spec(section)
        async with self._uow() as uow:
            return _view(spec, await uow.settings.get(spec.id))

    async def all(self) -> list[SectionView]:
        return [await self(section) for section in SECTIONS]


class SaveSection:
    def __init__(self, uow: UowFactory, clock: Clock, targets: TargetPolicy | None = None) -> None:
        self._uow, self._clock, self._targets = uow, clock, targets or TargetPolicy()

    async def __call__(
        self,
        actor: str,
        section: str,
        values: dict[str, Any],
        secrets: dict[str, str | None],
        expected_version: int,
    ) -> SectionView:
        spec = _spec(section)
        async with self._uow() as uow:
            doc = await uow.settings.get(spec.id) or SettingsDocument(spec.id)
            if doc.version != expected_version:
                raise VersionConflictError()
            clean_values, clean_secrets = spec.normalise(*doc.candidate(values, secrets))
            self._targets.check(spec, clean_values)
            changed = doc.replace(spec, clean_values, clean_secrets, now=self._clock.now(), actor=actor)
            if changed:
                await uow.settings.save(doc, expected_version=expected_version)
                uow.events.record(
                    SettingsChanged(section=spec.id, version=doc.version, changed_fields=changed), actor=actor
                )
                await uow.commit()
            return _view(spec, doc)


class RunConnectionTest:
    """Tests the *candidate* configuration (unsaved edits merged over stored secrets), so an admin can check
    before saving without retyping passwords."""

    def __init__(
        self,
        uow: UowFactory,
        testers: Mapping[TesterKey, ConnectionTester],
        targets: TargetPolicy | None = None,
    ) -> None:
        self._uow, self._testers, self._targets = uow, testers, targets or TargetPolicy()

    async def __call__(
        self, section: str, values: dict[str, Any] | None = None, secrets: dict[str, str | None] | None = None
    ) -> ConnectionReport:
        spec = _spec(section)
        async with self._uow() as uow:
            doc = await uow.settings.get(spec.id) or SettingsDocument(spec.id)
        base_values = values if values is not None else doc.values
        try:
            clean_values, clean_secrets = spec.normalise(*doc.candidate(base_values, secrets or {}))
            self._targets.check(spec, clean_values)
        except InvalidSettingsError as exc:
            checks = [
                Check(spec.field(k).label if spec.field(k) else k, False, msg)  # type: ignore[union-attr]
                for k, msg in exc.errors.items()
            ]
            return ConnectionReport(ok=False, summary="Fill in the highlighted fields first.", checks=checks)
        provider = clean_values.get("provider")
        tester = self._testers.get(f"{spec.id}:{provider}") or self._testers.get(spec.id)
        if tester is None:
            return ConnectionReport(ok=False, summary="No connection test exists for this configuration yet.")
        started = time.perf_counter()
        report = await tester.test(clean_values, clean_secrets)
        return ConnectionReport(
            report.ok, report.summary, report.checks, int((time.perf_counter() - started) * 1000)
        )


@dataclass(frozen=True, slots=True)
class ResolvedSection:
    """What a service needs to act on a section: values with defaults and decrypted secrets."""

    section: str
    version: int
    configured: bool
    values: dict[str, Any]
    secrets: dict[str, str]

    def __repr__(self) -> str:  # never let secrets reach a log line
        return f"ResolvedSection({self.section!r}, version={self.version}, secrets={sorted(self.secrets)})"


class ResolveSection:
    """Internal read for services. Only the services listed in READERS may read a section."""

    def __init__(self, uow: UowFactory) -> None:
        self._uow = uow

    async def __call__(self, service: str, section: str) -> ResolvedSection:
        spec = _spec(section)
        if service not in READERS.get(spec.id, frozenset()):
            raise ReaderNotAllowedError(service, spec.id)
        async with self._uow() as uow:
            doc = await uow.settings.get(spec.id)
        log.info("settings_resolved", section=spec.id, reader=service, version=doc.version if doc else 0)
        if doc is None:
            return ResolvedSection(spec.id, 0, False, spec.defaults(), {})
        return ResolvedSection(
            spec.id, doc.version, True, {**spec.defaults(), **doc.values}, dict(doc.secrets)
        )
