"""In-memory event bus for unit tests and single-process development."""

from __future__ import annotations

from collections import defaultdict

from .envelope import EventEnvelope
from .ports import EventHandler


def topic_matches(pattern: str, routing_key: str) -> bool:
    """AMQP topic semantics: `*` = exactly one word, `#` = zero or more words."""
    return _match(pattern.split("."), routing_key.split("."))


def _match(p: list[str], k: list[str]) -> bool:
    if not p:
        return not k
    head, rest = p[0], p[1:]
    if head == "#":
        return any(_match(rest, k[n:]) for n in range(len(k) + 1))
    if not k:
        return False
    return (head == "*" or head == k[0]) and _match(rest, k[1:])


class InMemoryEventBus:
    def __init__(self) -> None:
        self.published: list[EventEnvelope] = []
        self._subscribers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, pattern: str, handler: EventHandler) -> None:
        self._subscribers[pattern].append(handler)

    async def publish(self, envelope: EventEnvelope) -> None:
        self.published.append(envelope)
        for pattern, handlers in list(self._subscribers.items()):
            if topic_matches(pattern, envelope.type):
                for handler in handlers:
                    await handler(envelope)

    def of_type(self, event_type: str) -> list[EventEnvelope]:
        return [e for e in self.published if e.type == event_type]
