"""Ops section: checks that each extra HTTP probe target answers. Actions and folders are ops-service's
business (it refuses unknown actions itself), so they are not second-guessed here."""

from __future__ import annotations

from typing import Any

from opspilot_common.resilience import ResilienceError, ResilientHttpClient

from ....ports import Check, ConnectionReport
from ._common import explain


def targets(raw: str | None) -> list[tuple[str, str]]:
    out = []
    for item in (raw or "").replace("\n", ",").split(","):
        name, _, url = item.partition("=")
        if name.strip() or url.strip():
            out.append((name.strip(), url.strip()))
    return out


class OpsTester:
    def __init__(self, http: ResilientHttpClient) -> None:
        self._http = http

    async def test(self, values: dict[str, Any], secrets: dict[str, str]) -> ConnectionReport:
        checks = [Check("Executor", True, "Mock environment: approved actions change only the simulation")]
        listed = targets(values.get("probe_targets"))
        if not listed:
            checks.append(
                Check("Probes", True, "No extra HTTP probes; the environment's own probes are used")
            )
        for name, url in listed:
            if not name or not url.startswith(("http://", "https://")):
                checks.append(Check(name or url, False, "Write each probe as Name=https://host/path"))
                continue
            try:
                status = (await self._http.get(url)).status_code
            except ResilienceError as exc:
                checks.append(Check(name, False, explain(exc, url)))
                continue
            checks.append(Check(name, status < 400, f"{url} answered {status}"))
        return ConnectionReport.from_checks(checks, "Ops settings look good.")
