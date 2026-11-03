from __future__ import annotations

import email.utils
from datetime import UTC, datetime
from email.message import EmailMessage

from mail_service.domain.mail import MailboxConfig

NOW = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)


def raw_mail(
    subject: str = "VPN down in Hamburg",
    *,
    sender: str = "Jonas Weber <jonas.weber@kunde.de>",
    to: str = "support@example.com",
    body: str = "Since 08:10 nobody in Hamburg can connect to the VPN.",
    message_id: str | None = None,
    in_reply_to: str | None = None,
    references: list[str] | None = None,
    attachment: tuple[str, bytes] | None = None,
    html: str | None = None,
    date: datetime | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg["Date"] = email.utils.format_datetime(date or NOW)
    msg["Message-ID"] = message_id or email.utils.make_msgid(domain="kunde.de")
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = " ".join(references)
    if body:
        msg.set_content(body)
    if html:
        if body:
            msg.add_alternative(html, subtype="html")
        else:
            msg.set_content(html, subtype="html")
    if attachment:
        msg.add_attachment(attachment[1], maintype="application", subtype="pdf", filename=attachment[0])
    return msg.as_bytes()


def imap_config(**values: object) -> MailboxConfig:
    base: dict[str, object] = {
        "provider": "imap",
        "folder": "INBOX",
        "poll_interval_seconds": 60,
        "imap_host": "imap.test",
        "imap_port": 993,
        "imap_ssl": True,
        "username": "support@example.com",
        "smtp_host": "smtp.test",
        "smtp_port": 587,
    }
    base.update(values)
    return MailboxConfig(
        version=1,
        provider=str(base["provider"]),
        folder=str(base["folder"]),
        poll_interval_seconds=int(base["poll_interval_seconds"]),  # type: ignore[call-overload]
        values=base,
        secrets={"password": "pw"},
    )
