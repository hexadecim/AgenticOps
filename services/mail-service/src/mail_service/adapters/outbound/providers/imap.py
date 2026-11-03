"""IMAP (read) + SMTP (send). The standard-library clients block, so each operation runs in a worker thread
under the `mail-provider` resilience policy. The folder is opened read-only: OpsPilot never changes flags.

Read position: (UIDVALIDITY, last UID). A changed UIDVALIDITY means the server renumbered the folder, so
reading restarts from the look-back date and the dedupe key absorbs anything seen before.
"""

from __future__ import annotations

import asyncio
import contextlib
import imaplib
import re
import smtplib
import socket
import ssl
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from opspilot_common.resilience import DependencyUnreachableError, ResilienceError, Resilient

from ....domain.errors import MailboxRejectedError
from ....domain.mail import FetchBatch, IncomingMail, MailboxConfig, OutgoingMail
from ._errors import unavailable
from .mime import build_mime, parse_mime

ImapFactory = Callable[[str, int, bool, float], imaplib.IMAP4]
SmtpFactory = Callable[[str, int, float], smtplib.SMTP]
_NETWORK_ERRORS = (socket.gaierror, ConnectionError, ssl.SSLError, OSError, imaplib.IMAP4.abort, EOFError)


def _imap(host: str, port: int, use_ssl: bool, timeout: float) -> imaplib.IMAP4:
    if use_ssl:
        return imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=timeout)
    return imaplib.IMAP4(host, port, timeout=timeout)


def _smtp(host: str, port: int, timeout: float) -> smtplib.SMTP:
    if port == 465:
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
    return smtplib.SMTP(host, port, timeout=timeout)


def _quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _status_numbers(data: list[Any]) -> dict[str, int]:
    text = b" ".join(d for d in data if isinstance(d, bytes)).decode(errors="replace")
    return {k.upper(): int(v) for k, v in re.findall(r"(UIDNEXT|UIDVALIDITY)\s+(\d+)", text, re.I)}


def _internaldate(meta: bytes) -> datetime | None:
    match = re.search(rb'INTERNALDATE "([^"]+)"', meta)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1).decode().strip(), "%d-%b-%Y %H:%M:%S %z")
    except ValueError:
        return None


class ImapMailbox:
    """MailReader + MailSender for any IMAP/SMTP provider."""

    def __init__(
        self,
        config: MailboxConfig,
        resilient: Resilient,
        *,
        imap_factory: ImapFactory = _imap,
        smtp_factory: SmtpFactory = _smtp,
    ) -> None:
        self._v, self._password = config.values, config.secrets.get("password", "")
        self._folder, self._address = config.folder, config.address
        self._resilient, self._imap, self._smtp = resilient, imap_factory, smtp_factory
        self._where = f"the IMAP server {self._v.get('imap_host')}"

    @property
    def can_send(self) -> bool:
        return bool(self._v.get("smtp_host"))

    # --- MailReader ----------------------------------------------------------------------------------------
    async def fetch_new(self, cursor: Mapping[str, Any] | None, *, limit: int, since: datetime) -> FetchBatch:
        try:
            return await self._resilient.call(
                lambda: asyncio.to_thread(self._fetch, dict(cursor) if cursor else None, limit, since)
            )
        except ResilienceError as exc:
            raise unavailable(exc, self._where) from exc

    def _connect(self) -> imaplib.IMAP4:
        try:
            client = self._imap(
                self._v["imap_host"],
                int(self._v["imap_port"]),
                bool(self._v.get("imap_ssl", True)),
                self._resilient.config.timeout,
            )
        except _NETWORK_ERRORS as exc:
            raise DependencyUnreachableError("mail-provider", type(exc).__name__) from exc
        try:
            client.login(self._v["username"], self._password)
        except imaplib.IMAP4.error as exc:
            with contextlib.suppress(Exception):
                client.logout()
            raise MailboxRejectedError("The mail server rejected the user name or password.") from exc
        return client

    def _fetch(self, cursor: dict[str, Any] | None, limit: int, since: datetime) -> FetchBatch:
        client = self._connect()
        try:
            try:
                status, data = client.status(_quote(self._folder), "(UIDNEXT UIDVALIDITY)")
                if status != "OK":
                    raise MailboxRejectedError(f"The folder '{self._folder}' does not exist.")
                numbers = _status_numbers(data)
                validity, uidnext = numbers.get("UIDVALIDITY", 0), numbers.get("UIDNEXT", 1)
                status, _ = client.select(_quote(self._folder), readonly=True)
                if status != "OK":
                    raise MailboxRejectedError(f"The folder '{self._folder}' cannot be opened.")
                if cursor and cursor.get("uidvalidity") == validity:
                    last = int(cursor["last_uid"])
                    status, found = client.uid("SEARCH", None, f"UID {last + 1}:*")  # type: ignore[arg-type]
                else:
                    last = 0
                    status, found = client.uid("SEARCH", None, "SINCE", since.strftime("%d-%b-%Y"))  # type: ignore[arg-type]
                if status != "OK":
                    raise MailboxRejectedError("The mail server refused to search the folder.")
                # `N:*` always matches the newest message, even when its UID is below N: filter.
                uids = sorted(u for u in (int(x) for x in (found[0] or b"").split()) if u > last)
                batch, more = uids[:limit], len(uids) > limit
                mails = [m for uid in batch if (m := self._message(client, uid)) is not None]
                # Without a cursor and nothing new, start after everything present when we looked.
                next_uid = batch[-1] if batch else (last if cursor and last else uidnext - 1)
                return FetchBatch(mails, {"uidvalidity": validity, "last_uid": next_uid}, more)
            except _NETWORK_ERRORS as exc:
                raise DependencyUnreachableError("mail-provider", type(exc).__name__) from exc
            except imaplib.IMAP4.error as exc:
                raise MailboxRejectedError(f"The mail server refused a command: {exc}") from exc
        finally:
            with contextlib.suppress(Exception):  # best effort
                client.logout()

    def _message(self, client: imaplib.IMAP4, uid: int) -> IncomingMail | None:
        status, data = client.uid("FETCH", str(uid), "(INTERNALDATE BODY.PEEK[])")
        part = next((d for d in data or [] if isinstance(d, tuple)), None)
        if status != "OK" or part is None:
            return None  # expunged between SEARCH and FETCH
        meta, raw = part[0], part[1]
        return parse_mime(raw, provider_id=str(uid), received_at=_internaldate(meta))

    # --- MailSender ----------------------------------------------------------------------------------------
    async def send(self, mail: OutgoingMail) -> str:
        if not self.can_send:
            raise MailboxRejectedError("No SMTP server is configured for replies.")
        raw, message_id = build_mime(mail, self._address)
        recipients = [a.email for a in mail.to]
        where = f"the SMTP server {self._v.get('smtp_host')}"
        try:
            # Sending is not idempotent: never retried here; the caller's queue retries later.
            await self._resilient.call(lambda: asyncio.to_thread(self._send, recipients, raw), retry=False)
        except ResilienceError as exc:
            raise unavailable(exc, where) from exc
        return message_id

    def _send(self, recipients: list[str], raw: bytes) -> None:
        port = int(self._v.get("smtp_port") or 587)
        try:
            client = self._smtp(self._v["smtp_host"], port, self._resilient.config.timeout)
        except _NETWORK_ERRORS as exc:
            raise DependencyUnreachableError("mail-provider", type(exc).__name__) from exc
        try:
            if port != 465 and client.has_extn("starttls"):
                client.starttls(context=ssl.create_default_context())
            if client.has_extn("auth"):
                try:
                    client.login(self._v["username"], self._password)
                except smtplib.SMTPAuthenticationError as exc:
                    raise MailboxRejectedError("The SMTP server rejected the user name or password.") from exc
            try:
                client.sendmail(self._address, recipients, raw)
            except smtplib.SMTPRecipientsRefused as exc:
                raise MailboxRejectedError("The SMTP server refused the recipients.") from exc
            except smtplib.SMTPResponseException as exc:
                if exc.smtp_code >= 500:  # permanent: retrying will not help
                    raise MailboxRejectedError(
                        f"The SMTP server refused the message ({exc.smtp_code})."
                    ) from exc
                raise DependencyUnreachableError("mail-provider", f"SMTP {exc.smtp_code}") from exc
        except (smtplib.SMTPServerDisconnected, *_NETWORK_ERRORS) as exc:
            raise DependencyUnreachableError("mail-provider", type(exc).__name__) from exc
        finally:
            with contextlib.suppress(Exception):
                client.quit()
