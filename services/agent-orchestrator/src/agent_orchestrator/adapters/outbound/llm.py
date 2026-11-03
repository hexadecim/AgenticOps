"""The local model through Ollama (ADR-0004): deepseek-r1 fills the `Intake` schema (json_schema output,
reasoning kept out of the answer); nomic-embed-text gives vectors for duplicate detection. Every call runs
under the `ollama` / `ollama-embed` circuit breakers; failures become LlmUnavailableError so the run parks."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from langchain_core.exceptions import OutputParserException
from opspilot_common.auth import ServiceTokenIssuer
from opspilot_common.resilience import CircuitOpenError, ResilienceError, Resilient, ResilientHttpClient
from pydantic import ValidationError

from ...domain.errors import LlmUnavailableError, StopRunError
from ...domain.triage import Intake

MAX_BODY_CHARS = 6000  # keeps a reasoning model's run time bounded
TEMPERATURE = 0.6
MAX_TOKENS = 2048

SYSTEM = """You are Intake, the first step of an IT support team's triage.
You read one support email and fill in the requested fields. The email is pseudonymised: tokens such as
[PERSON_1], [EMAIL_2] or [PHONE_1] stand for personal data. Keep tokens exactly as written, never guess who
they are, and do not put personal data in your answer. Base every field on the email only; leave a field
empty rather than inventing it. Write the summary in English even if the email is in another language."""


@dataclass(frozen=True, slots=True)
class LlmSettings:
    version: int
    url: str
    model: str
    embedding_model: str
    thinking: bool


class LlmSettingsCache:
    """The admin's LLM settings (settings-service), reloaded when `settings.changed` names the llm section."""

    def __init__(self, http: ResilientHttpClient, tokens: ServiceTokenIssuer) -> None:
        self._http, self._tokens = http, tokens
        self._current: LlmSettings | None = None
        self._lock = asyncio.Lock()

    async def get(self, *, refresh: bool = False) -> LlmSettings:
        async with self._lock:
            if refresh or self._current is None:
                try:
                    response = await self._http.get(
                        "/internal/v1/sections/llm/resolved", headers=self._tokens.header()
                    )
                except ResilienceError as exc:
                    if self._current is None:
                        raise LlmUnavailableError(f"Cannot read the model settings: {exc}") from exc
                    return self._current
                if response.status_code != 200:
                    raise LlmUnavailableError(
                        f"settings-service refused the model settings ({response.status_code})"
                    )
                body = response.json()
                v = body["values"]
                self._current = LlmSettings(
                    int(body["version"]), str(v["ollama_url"]).rstrip("/"), str(v["model"]),
                    str(v["embedding_model"]), bool(v.get("thinking", True)),
                )  # fmt: skip
            return self._current


async def _guarded[T](resilient: Resilient, what: str, call: Callable[[], Awaitable[T]]) -> T:
    try:
        return await resilient.call(call)
    except CircuitOpenError as exc:
        raise LlmUnavailableError(
            f"{what} kept failing; paused for {exc.retry_after:.0f} s", retry_after=exc.retry_after
        ) from exc
    except ResilienceError as exc:
        raise LlmUnavailableError(f"{what} is unavailable: {exc}") from exc
    except (httpx.TransportError, ConnectionError) as exc:
        raise LlmUnavailableError(f"Cannot reach {what}: {type(exc).__name__}") from exc


def render(email: dict[str, Any]) -> str:
    sender = email.get("from", {})
    who = (
        f"[{sender['name_token']}] <[{sender['email_token']}]>"
        if sender.get("name_token")
        else f"[{sender.get('email_token')}]"
    )
    attachments = ", ".join(a.get("filename", "") for a in email.get("attachments", [])) or "none"
    body = str(email.get("body", ""))[:MAX_BODY_CHARS]
    return (
        f"From: {who}\nSubject: {email.get('subject', '')}\nReceived: {email.get('received_at', '')}\n"
        f"Attachments: {attachments}\n\n{body}"
    )


class OllamaIntake:
    def __init__(self, settings: LlmSettingsCache, resilient: Resilient) -> None:
        self._settings, self._resilient = settings, resilient

    async def describe(self) -> dict[str, Any]:
        cfg = await self._settings.get()
        return {"model": cfg.model, "thinking": cfg.thinking}

    async def classify(self, email: dict[str, Any]) -> Intake:
        cfg = await self._settings.get()
        try:
            return await self._ask(cfg, email, thinking=cfg.thinking)
        except (OutputParserException, ValidationError, ValueError):
            if not cfg.thinking:
                raise
            # Long emails can use the whole token budget on reasoning, leaving no answer (seen on real mail).
            # One more try without reasoning always leaves room for the answer.
            return await self._ask(cfg, email, thinking=False)

    async def _ask(self, cfg: LlmSettings, email: dict[str, Any], *, thinking: bool) -> Intake:
        from langchain_ollama import ChatOllama
        from ollama import ResponseError

        llm = ChatOllama(
            base_url=cfg.url,
            model=cfg.model,
            reasoning=thinking,
            # DeepSeek-R1 repeats itself endlessly at temperature 0 (seen live); 0.6 is its recommended value.
            temperature=TEMPERATURE,
            # Bound the answer: at ~14 tokens/s on this PC, 2048 tokens end well inside the 180 s timeout.
            num_predict=MAX_TOKENS,
            client_kwargs={"timeout": self._resilient.config.timeout},
        )
        structured = llm.with_structured_output(Intake, method="json_schema")
        messages = [("system", SYSTEM), ("human", render(email))]
        try:
            result = await _guarded(
                self._resilient, f"The local model ({cfg.model})", lambda: structured.ainvoke(messages)
            )
        except ResponseError as exc:
            if exc.status_code == 404:
                raise LlmUnavailableError(f"The model {cfg.model} is not installed in Ollama") from exc
            if exc.status_code >= 500:
                raise LlmUnavailableError(f"Ollama failed: {exc.error}") from exc
            raise StopRunError(f"Ollama refused the request: {exc.error}") from exc
        if not isinstance(result, Intake):
            raise ValueError("The model's answer did not match the intake schema")
        return result


class OllamaEmbedder:
    def __init__(self, settings: LlmSettingsCache, resilient: Resilient) -> None:
        self._settings, self._resilient = settings, resilient

    async def embed(self, texts: list[str]) -> list[list[float]]:
        from langchain_ollama import OllamaEmbeddings

        cfg = await self._settings.get()
        embeddings = OllamaEmbeddings(model=cfg.embedding_model, base_url=cfg.url)
        return await _guarded(
            self._resilient,
            f"The embedding model ({cfg.embedding_model})",
            lambda: embeddings.aembed_documents(texts),
        )


class OllamaCircuit:
    def __init__(self, resilient: Resilient) -> None:
        self._breaker = resilient.breaker

    async def retry_after(self) -> float | None:
        return await self._breaker.retry_after()
