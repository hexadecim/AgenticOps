"""Event envelope, catalogue, outbox/inbox and bus adapters."""

from .breaker_listener import BreakerEventListener
from .catalogue import PAYLOADS, Decision, EventType, Gate, parse_payload
from .envelope import EventEnvelope
from .memory import InMemoryEventBus, topic_matches
from .outbox import Inbox, Outbox, OutboxRelay, inbox_table, outbox_table
from .ports import EventHandler, EventPublisher
from .runtime import EventRuntime

__all__ = [
    "PAYLOADS",
    "BreakerEventListener",
    "Decision",
    "EventEnvelope",
    "EventHandler",
    "EventPublisher",
    "EventRuntime",
    "EventType",
    "Gate",
    "InMemoryEventBus",
    "Inbox",
    "Outbox",
    "OutboxRelay",
    "inbox_table",
    "outbox_table",
    "parse_payload",
    "topic_matches",
]
