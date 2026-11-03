"""Mailbox provider registry. Add a provider: write an adapter that passes
tests/contract/test_mailbox_port.py, then `register` it here. Use cases never change (open/closed)."""

from __future__ import annotations

from collections.abc import Callable

from opspilot_common.resilience import ResilienceRegistry, ResilientHttpClient

from ....domain.errors import UnknownProviderError
from ....domain.mail import MailboxConfig
from ....ports import MailboxConnection
from .gmail import GmailMailbox
from .graph import GraphMailbox
from .imap import ImapMailbox

ProviderFactory = Callable[[MailboxConfig], MailboxConnection]
DEPENDENCY = "mail-provider"  # one circuit for whichever provider is configured


async def _nothing() -> None:
    return None


class ProviderRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, ProviderFactory] = {}

    def register(self, provider: str, factory: ProviderFactory) -> None:
        self._factories[provider] = factory

    @property
    def providers(self) -> list[str]:
        return sorted(self._factories)

    def build(self, config: MailboxConfig) -> MailboxConnection:
        factory = self._factories.get(config.provider)
        if factory is None:
            raise UnknownProviderError(config.provider)
        return factory(config)


def default_registry(resilience: ResilienceRegistry) -> ProviderRegistry:
    registry = ProviderRegistry()

    def imap(config: MailboxConfig) -> MailboxConnection:
        box = ImapMailbox(config, resilience.get(DEPENDENCY))
        return MailboxConnection("imap", box, box if box.can_send else None, _nothing)

    def http() -> ResilientHttpClient:
        return ResilientHttpClient.from_registry(resilience, DEPENDENCY, follow_redirects=False)

    def gmail(config: MailboxConfig) -> MailboxConnection:
        client = http()
        box = GmailMailbox(config, client)
        return MailboxConnection("gmail", box, box, client.aclose)

    def graph(config: MailboxConfig) -> MailboxConnection:
        client = http()
        box = GraphMailbox(config, client)
        return MailboxConnection("graph", box, box, client.aclose)

    registry.register("imap", imap)
    registry.register("gmail", gmail)
    registry.register("graph", graph)
    return registry
