"""Which mailbox adapter is active, and rebuilding it when the admin changes the settings (no restart)."""

from __future__ import annotations

import asyncio
import contextlib

import structlog

from ..domain.mail import MailboxConfig
from ..ports import MailboxBuilder, MailboxConnection, SettingsSource

log = structlog.get_logger(__name__)


class ActiveMailbox:
    """Holds the current configuration and its adapter. Swapped atomically; the old one is closed."""

    def __init__(self) -> None:
        self.config: MailboxConfig | None = None
        self.connection: MailboxConnection | None = None
        self.loaded = False  # settings have been read at least once

    def current(self) -> tuple[MailboxConfig, MailboxConnection] | None:
        if self.config is None or self.connection is None:
            return None
        return self.config, self.connection


class ApplyMailboxSettings:
    """Reads the mailbox settings and (re)builds the adapter if they changed. Safe to call repeatedly."""

    def __init__(self, active: ActiveMailbox, source: SettingsSource, builder: MailboxBuilder) -> None:
        self._active, self._source, self._builder = active, source, builder
        self._lock = asyncio.Lock()

    async def __call__(self) -> bool:
        """Return True when the active adapter changed. Raises SettingsUnavailableError."""
        async with self._lock:
            config = await self._source.mailbox()
            old = self._active.config
            self._active.loaded = True
            if config is not None and old is not None and config.version == old.version:
                return False
            if config is None and old is None:
                return False
            connection = self._builder.build(config) if config else None
            previous = self._active.connection
            self._active.config, self._active.connection = config, connection
            if previous is not None:
                with contextlib.suppress(Exception):  # best effort: never block the swap
                    await previous.close()
            log.info(
                "mailbox_adapter_applied",
                provider=config.provider if config else None,
                version=config.version if config else 0,
            )
            return True
