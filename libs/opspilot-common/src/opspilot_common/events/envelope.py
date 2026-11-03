"""The envelope every event travels in. Routing key on the bus = `type`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field


def _now() -> datetime:
    return datetime.now(UTC)


class EventPayload(BaseModel):
    """Base for catalogue payloads; subclasses set EVENT_TYPE (and EVENT_VERSION when breaking)."""

    EVENT_TYPE: ClassVar[str]
    EVENT_VERSION: ClassVar[int] = 1


class EventEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: str
    version: int = 1
    source: str
    occurred_at: datetime = Field(default_factory=_now)
    correlation_id: str | None = None
    causation_id: str | None = None
    actor: str | None = Field(default=None, description="User id or agent name that caused the event")
    payload: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def wrap(
        cls,
        payload: EventPayload,
        *,
        source: str,
        correlation_id: str | None = None,
        causation: EventEnvelope | None = None,
        actor: str | None = None,
    ) -> EventEnvelope:
        """Build an envelope from a catalogue payload; type and version come from the payload class."""
        return cls(
            type=payload.EVENT_TYPE,
            version=payload.EVENT_VERSION,
            source=source,
            correlation_id=correlation_id or (causation.correlation_id if causation else None),
            causation_id=causation.id if causation else None,
            actor=actor,
            payload=payload.model_dump(mode="json"),
        )

    def to_bytes(self) -> bytes:
        return self.model_dump_json().encode()

    @classmethod
    def from_bytes(cls, data: bytes) -> EventEnvelope:
        return cls.model_validate_json(data)
