"""IMAP + SMTP tester. The standard-library clients are blocking, so they run in a worker thread under the
`mail-provider` resilience policy (timeouts, circuit breaker)."""

from __future__ import annotations

import asyncio
import contextlib
import imaplib
import smtplib
import socket
import ssl
from collections.abc import Callable
from typing import Any

from opspilot_common.resilience import DependencyUnreachableError, ResilienceError, Resilient

from ....ports import Check, ConnectionReport
from ._common import explain

ImapFactory = Callable[[str, int, bool, float], imaplib.IMAP4]
SmtpFactory = Callable[[str, int, float], smtplib.SMTP]


def _imap(host: str, port: int, use_ssl: bool, timeout: float) -> imaplib.IMAP4:
    if use_ssl:
        return imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=timeout)
    return imaplib.IMAP4(host, port, timeout=timeout)


def _smtp(host: str, port: int, timeout: float) -> smtplib.SMTP:
    if port == 465:
        return smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
    return smtplib.SMTP(host, port, timeout=timeout)


class _LoginRejectedError(Exception):
    pass


class ImapTester:
    def __init__(
        self, resilient: Resilient, *, imap_factory: ImapFactory = _imap, smtp_factory: SmtpFactory = _smtp
    ) -> None:
        self._resilient, self._imap, self._smtp = resilient, imap_factory, smtp_factory

    async def test(self, values: dict[str, Any], secrets: dict[str, str]) -> ConnectionReport:
        host, port = values["imap_host"], values["imap_port"]
        checks: list[Check] = []
        try:
            count = await self._resilient.call(
                lambda: asyncio.to_thread(self._check_imap, values, secrets["password"]), retry=False
            )
            checks.append(Check("IMAP sign-in", True, f"Signed in to {host}:{port}"))
            checks.append(Check("Folder", True, f"{values['folder']} has {count} messages"))
        except _LoginRejectedError as exc:
            checks.append(Check("IMAP sign-in", False, str(exc)))
        except ResilienceError as exc:
            checks.append(Check("IMAP sign-in", False, explain(exc, f"the IMAP server {host}:{port}")))
        if values.get("smtp_host") and all(c.ok for c in checks):
            smtp_host, smtp_port = values["smtp_host"], values.get("smtp_port", 587)
            try:
                await self._resilient.call(
                    lambda: asyncio.to_thread(self._check_smtp, values, secrets["password"]), retry=False
                )
                checks.append(
                    Check("SMTP sign-in", True, f"Signed in to {smtp_host}:{smtp_port} for replies")
                )
            except _LoginRejectedError as exc:
                checks.append(Check("SMTP sign-in", False, str(exc)))
            except ResilienceError as exc:
                checks.append(
                    Check("SMTP sign-in", False, explain(exc, f"the SMTP server {smtp_host}:{smtp_port}"))
                )
        return ConnectionReport.from_checks(checks, f"OpsPilot can read {values['folder']} at {host}.")

    def _check_imap(self, values: dict[str, Any], password: str) -> int:
        timeout = self._resilient.config.timeout
        try:
            client = self._imap(
                values["imap_host"], values["imap_port"], bool(values.get("imap_ssl", True)), timeout
            )
        except (socket.gaierror, ConnectionError, ssl.SSLError, OSError) as exc:
            raise DependencyUnreachableError("mail-provider", type(exc).__name__) from exc
        try:
            try:
                client.login(values["username"], password)
            except imaplib.IMAP4.error as exc:
                raise _LoginRejectedError(
                    "The mail server rejected the user name or password. "
                    "For Gmail or Outlook.com use an app password."
                ) from exc
            status, data = client.select(values["folder"], readonly=True)
            if status != "OK":
                raise _LoginRejectedError(f"Signed in, but the folder '{values['folder']}' does not exist.")
            return int(data[0] or 0)
        finally:
            with contextlib.suppress(Exception):  # best effort
                client.logout()

    def _check_smtp(self, values: dict[str, Any], password: str) -> None:
        timeout = self._resilient.config.timeout
        port = int(values.get("smtp_port") or 587)
        try:
            client = self._smtp(values["smtp_host"], port, timeout)
        except (socket.gaierror, ConnectionError, ssl.SSLError, OSError) as exc:
            raise DependencyUnreachableError("mail-provider", type(exc).__name__) from exc
        try:
            if port != 465 and client.has_extn("starttls"):
                client.starttls(context=ssl.create_default_context())
            if client.has_extn("auth"):
                try:
                    client.login(values["username"], password)
                except smtplib.SMTPAuthenticationError as exc:
                    raise _LoginRejectedError("The SMTP server rejected the user name or password.") from exc
        finally:
            with contextlib.suppress(Exception):  # best effort
                client.quit()
