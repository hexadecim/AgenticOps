"""HTTP adapters: settings-service (Ops + LLM sections), Ollama embeddings, and real HTTP health probes.
Every call goes through a circuit breaker; resilience errors become ops-service domain errors."""

from __future__ import annotations

import asyncio
import time

import httpx
from opspilot_common.auth import ServiceTokenIssuer
from opspilot_common.logging import correlation_id
from opspilot_common.resilience import (
    CircuitOpenError,
    ResilienceError,
    ResilienceRegistry,
    ResilientHttpClient,
)

from ...domain.actions import parse_enabled
from ...domain.errors import EmbeddingUnavailableError, SettingsUnavailableError
from ...domain.model import OpsSettings, ProbeResult, parse_targets
from ...ports import ProbeRunner


class SettingsServiceSource:
    def __init__(self, http: ResilientHttpClient, tokens: ServiceTokenIssuer) -> None:
        self._http, self._tokens = http, tokens

    async def _section(self, name: str) -> dict[str, object]:
        try:
            response = await self._http.get(
                f"/internal/v1/sections/{name}/resolved",
                headers={**self._tokens.header(), "X-Correlation-ID": correlation_id.get() or ""},
            )
        except ResilienceError as exc:
            raise SettingsUnavailableError(f"settings-service is unavailable: {exc}") from exc
        if response.status_code != 200:
            raise SettingsUnavailableError(
                f"settings-service refused the {name} settings ({response.status_code})"
            )
        body = response.json()
        return {"version": body.get("version", 0), **(body.get("values") or {})}

    async def ops(self) -> OpsSettings:
        ops, llm = await self._section("ops"), await self._section("llm")
        defaults = OpsSettings()
        return OpsSettings(
            version=int(str(ops.get("version") or 0)) * 1000 + int(str(llm.get("version") or 0)),
            executor=str(ops.get("executor") or defaults.executor),
            enabled_actions=parse_enabled(str(ops["enabled_actions"]) if "enabled_actions" in ops else None),
            runbook_folder=str(ops.get("runbook_folder") or defaults.runbook_folder),
            log_folder=str(ops.get("log_folder") or defaults.log_folder),
            probe_targets=parse_targets(str(ops.get("probe_targets") or "")),
            embedding_model=str(llm.get("embedding_model") or defaults.embedding_model),
            ollama_url=str(llm.get("ollama_url") or defaults.ollama_url).rstrip("/"),
        )


class OllamaEmbedder:
    """nomic-embed-text through Ollama's /api/embed, on the `ollama-embed` circuit."""

    def __init__(self, registry: ResilienceRegistry) -> None:
        self._registry = registry
        self._clients: dict[str, ResilientHttpClient] = {}

    def _client(self, url: str) -> ResilientHttpClient:
        if url not in self._clients:
            self._clients[url] = ResilientHttpClient.from_registry(
                self._registry, "ollama-embed", base_url=url
            )
        return self._clients[url]

    async def embed(self, settings: OpsSettings, texts: list[str]) -> list[list[float]]:
        try:
            response = await self._client(settings.ollama_url).post(
                "/api/embed",
                json={"model": settings.embedding_model, "input": texts},
                idempotency_key="embed",  # embedding is a pure function: safe to retry
            )
        except ResilienceError as exc:
            raise EmbeddingUnavailableError(str(exc)) from exc
        if response.status_code == 404:
            raise EmbeddingUnavailableError(
                f"the model {settings.embedding_model} is not installed in Ollama"
            )
        if response.status_code != 200:
            raise EmbeddingUnavailableError(f"Ollama answered {response.status_code}")
        vectors = response.json().get("embeddings") or []
        if len(vectors) != len(texts):
            raise EmbeddingUnavailableError("Ollama returned the wrong number of embeddings")
        return [[float(v) for v in vector] for vector in vectors]

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()


class HttpProbes:
    """Real probes the admin listed (`Name=https://…/health`): healthy when the answer is below 400.
    Each target has its own circuit (`probe-target-<name>`), so one dead target does not hide the others."""

    def __init__(self, registry: ResilienceRegistry, targets: tuple[tuple[str, str], ...]) -> None:
        self._registry, self._targets = registry, targets
        self._client = httpx.AsyncClient(timeout=10, follow_redirects=False)

    async def _one(self, name: str, url: str, region: str | None) -> ProbeResult:
        http = ResilientHttpClient(self._registry.get(f"probe-target-{name.lower()}"), self._client)
        started = time.monotonic()
        try:
            response = await http.get(url)
        except CircuitOpenError as exc:
            return ProbeResult(name, False, f"failing; next check in {exc.retry_after:.0f} s", region=region)
        except ResilienceError as exc:
            return ProbeResult(name, False, str(exc)[:120], region=region)
        ms = int((time.monotonic() - started) * 1000)
        return ProbeResult(
            name, response.status_code < 400, f"{response.status_code} in {ms} ms", ms, region=region
        )

    async def run(self, region: str | None) -> list[ProbeResult]:
        return list(await asyncio.gather(*(self._one(n, u, region) for n, u in self._targets)))

    async def aclose(self) -> None:
        await self._client.aclose()


class CombinedProbes:
    """The environment's probes plus any real HTTP targets from the admin's Ops settings."""

    def __init__(self, environment: ProbeRunner, registry: ResilienceRegistry, settings: object) -> None:
        self._environment, self._registry, self._settings = environment, registry, settings
        self._http: HttpProbes | None = None

    async def run(self, region: str | None) -> list[ProbeResult]:
        results = await self._environment.run(region)
        cfg: OpsSettings = await self._settings.get()  # type: ignore[attr-defined]
        if cfg.probe_targets:
            if self._http is None or self._http._targets != cfg.probe_targets:
                if self._http:
                    await self._http.aclose()
                self._http = HttpProbes(self._registry, cfg.probe_targets)
            results += await self._http.run(region)
        return results

    async def aclose(self) -> None:
        if self._http:
            await self._http.aclose()
