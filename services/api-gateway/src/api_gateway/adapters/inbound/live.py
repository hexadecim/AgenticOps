"""Live updates for the console: approval, run and Jira events are relayed to signed-in browsers as
server-sent events. Only the event type and ids travel; the page refetches what it shows through the
normal, authorised APIs, so no content is pushed to anyone."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

from opspilot_common.events import EventEnvelope

RELAYED = ["approval.#", "run.#", "jira.issue.#", "breaker.#", "action.#"]
_IDS = ("approval_id", "run_id", "thread_id", "issue_key", "status", "gate", "dependency", "new")


class Broadcaster:
    def __init__(self, *, queue_size: int = 100) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._size = queue_size

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)

    async def relay(self, envelope: EventEnvelope) -> None:
        message = {"type": envelope.type, **{k: envelope.payload[k] for k in _IDS if k in envelope.payload}}
        for queue in list(self._subscribers):
            with contextlib.suppress(
                asyncio.QueueFull
            ):  # a stalled browser misses updates, never blocks others
                queue.put_nowait(message)

    async def stream(self, *, heartbeat: float = 15.0) -> AsyncIterator[str]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(self._size)
        self._subscribers.add(queue)
        try:
            yield "retry: 5000\n\n"
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), heartbeat)
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield f"event: {message['type']}\ndata: {json.dumps(message)}\n\n"
        finally:
            self._subscribers.discard(queue)
