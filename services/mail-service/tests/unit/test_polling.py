from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from mail_service.adapters.outbound.memory import InMemoryStore, InMemoryUnitOfWork
from mail_service.adapters.outbound.providers.fake import FakeMailbox
from mail_service.application import (
    ActiveMailbox,
    ApplyMailboxSettings,
    GetEmail,
    GetMailboxStatus,
    Outcome,
    PollMailbox,
    PurgeExpiredEmails,
)
from mail_service.domain.errors import (
    EmailNotFoundError,
    MailboxRejectedError,
    MailboxUnavailableError,
    SettingsUnavailableError,
)
from mail_service.domain.mail import MailboxConfig
from mail_service.domain.status import Level
from mail_service.ports import MailboxConnection
from opspilot_common.events.catalogue import EmailReceived, PrivacyRecorded

from ..conftest import NOW, imap_config, raw_mail


class Clock:
    def __init__(self) -> None:
        self.at = NOW

    def now(self) -> datetime:
        return self.at


class Source:
    def __init__(self, config: MailboxConfig | None) -> None:
        self.config, self.calls, self.down = config, 0, False

    async def mailbox(self) -> MailboxConfig | None:
        self.calls += 1
        if self.down:
            raise SettingsUnavailableError("settings-service is unavailable")
        return self.config


class Builder:
    def __init__(self) -> None:
        self.boxes: list[FakeMailbox] = []
        self.closed = 0

    def build(self, config: MailboxConfig) -> MailboxConnection:
        box = FakeMailbox()
        self.boxes.append(box)

        async def close() -> None:
            self.closed += 1

        return MailboxConnection(config.provider, box, box, close)


class Circuit:
    def __init__(self) -> None:
        self.value: tuple[str, float | None] = ("closed", None)

    async def state(self) -> tuple[str, float | None]:
        return self.value


class World:
    def __init__(self, config: MailboxConfig | None = None) -> None:
        self.store, self.clock, self.circuit = InMemoryStore(), Clock(), Circuit()
        self.source, self.builder, self.active = Source(config), Builder(), ActiveMailbox()
        self.apply = ApplyMailboxSettings(self.active, self.source, self.builder)
        self.poll = PollMailbox(self.uow, self.active, self.apply, self.clock, batch_size=2, max_batches=3)
        self.status = GetMailboxStatus(self.uow, self.active, self.circuit, self.clock)

    def uow(self) -> InMemoryUnitOfWork:
        return InMemoryUnitOfWork(self.store)

    @property
    def box(self) -> FakeMailbox:
        return self.builder.boxes[-1]

    def received(self) -> list[EmailReceived]:
        return [p for p, _ in self.store.published if isinstance(p, EmailReceived)]


@pytest.fixture
def world() -> World:
    return World(imap_config())


async def test_new_mail_is_stored_once_and_announced_with_ids_only(world: World) -> None:
    await world.apply()
    world.box.deliver(raw_mail("VPN down", message_id="<a@kunde.de>"), NOW)
    result = await world.poll()
    assert result.outcome is Outcome.POLLED and result.new == 1
    (event,) = world.received()
    assert set(event.model_dump()) == {"email_id", "thread_id", "provider", "received_at"}  # no content
    stored = world.store.emails.rows[event.email_id]
    assert stored.mail.subject == "VPN down" and stored.thread_id == event.thread_id

    assert (await world.poll()).new == 0  # nothing new
    world.box.deliver(raw_mail("VPN down", message_id="<a@kunde.de>"), NOW)  # same message seen again
    assert (await world.poll()).new == 0
    assert len(world.received()) == 1


async def test_replies_join_their_thread_others_start_a_new_one(world: World) -> None:
    await world.apply()
    world.box.deliver(raw_mail("VPN down", message_id="<root@kunde.de>"), NOW)
    world.box.deliver(
        raw_mail("Re: VPN down", message_id="<r1@kunde.de>", in_reply_to="<root@kunde.de>"), NOW
    )
    world.box.deliver(
        raw_mail("Re: Re: VPN down", message_id="<r2@x.de>", references=["<root@kunde.de>", "<r1@kunde.de>"]),
        NOW,
    )
    world.box.deliver(raw_mail("Printer jam", message_id="<p@kunde.de>"), NOW)
    assert (await world.poll()).new == 4  # 2 batches of 2 in one cycle
    threads = [e.thread_id for e in world.received()]
    assert threads[0] == threads[1] == threads[2] != threads[3]


async def test_large_backlog_is_read_over_several_cycles(world: World) -> None:
    await world.apply()
    for n in range(7):
        world.box.deliver(raw_mail(f"m{n}"), NOW)
    assert (await world.poll()).new == 6  # batch_size 2 x max_batches 3
    assert (await world.poll()).new == 1


async def test_provider_failure_skips_the_cycle_and_turns_status_amber(world: World) -> None:
    await world.apply()
    world.box.deliver(raw_mail("one"), NOW)
    await world.poll()
    world.box.fail_with = MailboxUnavailableError("Cannot reach the IMAP server imap.test.")
    world.clock.at = NOW + timedelta(minutes=5)
    result = await world.poll()
    assert result.outcome is Outcome.FAILED and "Cannot reach" in result.detail
    status = await world.status()
    assert (
        status.level is Level.WARNING and status.message == "Mailbox polled 5 min ago · last attempt failed"
    )
    assert status.last_error == "Cannot reach the IMAP server imap.test."

    world.circuit.value = ("open", 90.0)
    status = await world.status()
    assert (
        status.level is Level.WARNING and "paused after repeated failures; next try in 90 s" in status.message
    )

    world.box.fail_with, world.circuit.value = None, ("closed", None)
    await world.poll()
    status = await world.status()
    assert status.level is Level.OK and status.message == "Mailbox polled just now"
    assert status.last_error is None and status.total_received == 1


async def test_rejected_credentials_are_reported_not_raised(world: World) -> None:
    await world.apply()
    world.box.fail_with = MailboxRejectedError("The mail server rejected the user name or password.")
    result = await world.poll()
    assert result.outcome is Outcome.FAILED and "rejected" in result.detail


async def test_status_turns_amber_when_polls_stop_succeeding(world: World) -> None:
    await world.apply()
    await world.poll()
    world.clock.at = NOW + timedelta(seconds=3 * 60 + 1)  # three 60-s intervals without a successful poll
    status = await world.status()
    assert status.level is Level.WARNING and status.message == "Mailbox polled 3 min ago"


async def test_no_mailbox_configured() -> None:
    world = World(None)
    assert (await world.poll()).outcome is Outcome.NOT_CONFIGURED
    status = await world.status()
    assert status.level is Level.OFF and not status.configured


async def test_settings_service_down_at_start_is_retried_next_cycle(world: World) -> None:
    world.source.down = True
    assert (await world.poll()).outcome is Outcome.FAILED
    world.source.down = False
    assert (await world.poll()).outcome is Outcome.POLLED


async def test_settings_change_rebuilds_the_adapter_and_new_mailbox_starts_fresh(world: World) -> None:
    assert await world.apply() is True
    assert await world.apply() is False  # same version: nothing to do
    world.box.deliver(raw_mail("old mailbox"), NOW)
    await world.poll()
    old_fingerprint = world.store.poll_state.state.fingerprint

    other = imap_config(imap_host="imap.other.test").values
    world.source.config = MailboxConfig(2, "imap", "INBOX", 60, other, {"password": "x"})
    assert await world.apply() is True
    assert world.builder.closed == 1 and len(world.builder.boxes) == 2
    world.box.deliver(raw_mail("new mailbox"), NOW)
    await world.poll()
    # Both read: reusing the old read position ({"last": 1}) would have skipped the new mailbox's message 1.
    assert [e.mail.subject for e in world.store.emails.rows.values()] == ["old mailbox", "new mailbox"]
    assert world.store.poll_state.state.fingerprint != old_fingerprint

    world.source.config = None  # mailbox section cleared
    assert await world.apply() is True and world.active.current() is None


async def test_retention_purges_raw_email_after_30_days(world: World) -> None:
    await world.apply()
    world.box.deliver(raw_mail("old"), NOW)
    await world.poll()
    purge = PurgeExpiredEmails(world.uow, world.clock, days=30)
    world.clock.at = NOW + timedelta(days=29)
    assert await purge() == 0
    world.clock.at = NOW + timedelta(days=30, seconds=1)
    assert await purge() == 1
    (record,) = [p for p, _ in world.store.published if isinstance(p, PrivacyRecorded)]
    assert record.action == "retention_purge" and "1 raw emails" in record.detail

    email_id = world.received()[0].email_id
    with pytest.raises(EmailNotFoundError):
        await GetEmail(world.uow)(email_id)


async def test_a_message_that_cannot_be_stored_is_reported_not_swallowed(world: World) -> None:
    await world.apply()
    world.box.deliver(raw_mail("poison"), NOW)

    async def broken_add(email: object) -> None:
        raise ValueError("invalid byte sequence for encoding UTF8: 0x00")

    world.store.emails.add = broken_add  # type: ignore[method-assign]
    result = await world.poll()
    assert result.outcome is Outcome.FAILED and "could not be stored (ValueError)" in result.detail
    status = await world.status()
    assert status.level is Level.WARNING and "could not be stored" in (status.last_error or "")
