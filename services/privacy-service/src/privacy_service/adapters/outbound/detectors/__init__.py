"""Entity detectors. Add one by implementing `EntityDetector` and listing it in bootstrap's composite."""

from __future__ import annotations

from collections.abc import Sequence

from ....domain.entities import Span
from ....ports import EntityDetector


class CompositeDetector:
    """Union of several detectors; overlaps are resolved later by `domain.entities.merge`. Any failure fails
    the whole detection (fail closed): a partial result could miss a name."""

    def __init__(self, detectors: Sequence[EntityDetector]) -> None:
        self._detectors = tuple(detectors)

    async def detect(self, text: str, language: str) -> list[Span]:
        found: list[Span] = []
        for detector in self._detectors:
            found += await detector.detect(text, language)
        return found
