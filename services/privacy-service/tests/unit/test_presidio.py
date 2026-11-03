"""The real Presidio + spaCy detector (loads the small English and German models once, ~1 s)."""

from __future__ import annotations

from typing import Any

import pytest
from privacy_service.adapters.outbound.detectors.presidio import PresidioDetector, build_engine
from privacy_service.domain.entities import EntityKind, clean_people, merge
from privacy_service.domain.errors import PrivacyUnavailableError


@pytest.fixture(scope="module")
def engine() -> Any:
    return build_engine()


async def test_finds_names_in_english_and_german(engine: Any) -> None:
    detector = PresidioDetector(engine)
    en = "Hello support, my colleague Maria Schmidt saw the same error. Call +49 40 1234567."
    de = "Hallo Support, Frau Petra Hoffmann aus der Buchhaltung hat Fehler 809."
    found_en = {
        (s.kind, en[s.start : s.end]) for s in merge(clean_people(en, await detector.detect(en, "en")))
    }
    found_de = {
        (s.kind, de[s.start : s.end]) for s in merge(clean_people(de, await detector.detect(de, "de")))
    }
    assert (EntityKind.PERSON, "Maria Schmidt") in found_en
    assert (EntityKind.PHONE, "+49 40 1234567") in found_en
    assert (EntityKind.PERSON, "Petra Hoffmann") in found_de
    assert not any("Support" in text for _, text in found_de)  # the greeting is not a person


async def test_engine_failure_fails_closed() -> None:
    class Broken:
        def analyze(self, **_: Any) -> list[Any]:
            raise RuntimeError("model crashed")

    with pytest.raises(PrivacyUnavailableError):
        await PresidioDetector(Broken()).detect("Hello Maria", "en")
