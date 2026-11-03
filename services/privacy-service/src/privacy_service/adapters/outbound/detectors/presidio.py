"""Microsoft Presidio with small spaCy models for English and German. Runs in a worker thread (CPU-bound).
Any error is a detection failure: the request fails closed instead of passing on undetected names."""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from ....domain.entities import EntityKind, Source, Span
from ....domain.errors import PrivacyUnavailableError

log = structlog.get_logger(__name__)

MODELS = {"en": "en_core_web_sm", "de": "de_core_news_sm"}
REGIONS = ("DE", "AT", "CH", "GB", "IE", "US", "FR", "NL", "BE", "IT", "ES", "PL", "SE", "DK")
_KINDS = {
    "PERSON": (EntityKind.PERSON, Source.NER),
    "EMAIL_ADDRESS": (EntityKind.EMAIL, Source.PATTERN),
    "PHONE_NUMBER": (EntityKind.PHONE, Source.PATTERN),
    "IBAN_CODE": (EntityKind.IBAN, Source.PATTERN),
}


def build_engine() -> Any:
    """Load once at start-up (~1 s, ~150 MB)."""
    from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from presidio_analyzer.predefined_recognizers import (
        EmailRecognizer,
        IbanRecognizer,
        PhoneRecognizer,
        SpacyRecognizer,
    )

    nlp = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": lang, "model_name": model} for lang, model in MODELS.items()],
        }
    ).create_engine()
    registry = RecognizerRegistry(supported_languages=list(MODELS))
    for lang in MODELS:
        registry.add_recognizer(EmailRecognizer(supported_language=lang))
        registry.add_recognizer(PhoneRecognizer(supported_language=lang, supported_regions=REGIONS))
        registry.add_recognizer(IbanRecognizer(supported_language=lang))
        registry.add_recognizer(SpacyRecognizer(supported_language=lang, supported_entities=["PERSON"]))
    return AnalyzerEngine(nlp_engine=nlp, registry=registry, supported_languages=list(MODELS))


class PresidioDetector:
    def __init__(self, engine: Any, *, threshold: float = 0.4) -> None:
        self._engine, self._threshold = engine, threshold

    async def detect(self, text: str, language: str) -> list[Span]:
        if not text.strip():
            return []
        try:
            results = await asyncio.to_thread(
                self._engine.analyze,
                text=text,
                language=language if language in MODELS else "en",
                entities=list(_KINDS),
                score_threshold=self._threshold,
            )
        except Exception as exc:
            log.exception("presidio_failed")
            raise PrivacyUnavailableError("Name detection failed; the email was not passed on.") from exc
        spans = []
        for r in results:
            kind, source = _KINDS[r.entity_type]
            spans.append(Span(r.start, r.end, kind, text[r.start : r.end], source))
        return spans
