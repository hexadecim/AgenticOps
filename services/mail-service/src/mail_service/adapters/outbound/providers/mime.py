"""RFC 5322 / MIME parsing and building, shared by every provider: IMAP returns MIME, Gmail returns it with
`format=raw`, Graph with `/$value`. One parser means every provider yields identical `IncomingMail`s."""

from __future__ import annotations

import email
import email.policy
import email.utils
import re
from datetime import UTC, datetime
from email.message import EmailMessage
from html.parser import HTMLParser

from ....domain.mail import Address, Attachment, IncomingMail, OutgoingMail, normalise_message_id

MAX_BODY_CHARS = 200_000  # protects storage and the LLM context from pathological messages


class _TextExtractor(HTMLParser):
    _SKIP = frozenset({"script", "style", "head"})
    _BREAK = frozenset({"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "blockquote"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(parser.parts))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean(text: str) -> str:
    """Drop NUL and other control characters (keeping tab and newlines). Real mail contains them, and
    Postgres cannot store NUL in text: one such message would otherwise block the mailbox."""
    return _CONTROL.sub("", text)


def _addresses(values: list[str]) -> tuple[Address, ...]:
    return tuple(
        Address(clean(addr).lower(), clean(name)) for name, addr in email.utils.getaddresses(values) if addr
    )


def _ids(value: str | None) -> tuple[str, ...]:
    return tuple(n for raw in re.findall(r"<[^>]+>", value or "") if (n := normalise_message_id(raw)))


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _content(part: EmailMessage) -> str:
    try:
        return str(part.get_content())
    except (LookupError, ValueError, AssertionError):  # unknown charset or broken encoding
        payload = part.get_payload(decode=True)
        return payload.decode("utf-8", "replace") if isinstance(payload, bytes) else ""


def parse_mime(
    raw: bytes,
    *,
    provider_id: str,
    provider_thread_id: str | None = None,
    received_at: datetime | None = None,
) -> IncomingMail:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)
    plain = msg.get_body(preferencelist=("plain",))
    html_part = msg.get_body(preferencelist=("html",))
    html = _content(html_part) if isinstance(html_part, EmailMessage) else None
    text = _content(plain) if isinstance(plain, EmailMessage) else html_to_text(html or "")
    attachments = tuple(
        Attachment(
            clean(part.get_filename() or "unnamed"),
            part.get_content_type(),
            len(payload) if isinstance(payload := part.get_payload(decode=True), bytes) else 0,
        )
        for part in msg.iter_attachments()
        if isinstance(part, EmailMessage)
    )
    senders = _addresses(msg.get_all("From", []))
    reply_to = _ids(msg.get("In-Reply-To"))
    return IncomingMail(
        provider_id=provider_id,
        message_id=normalise_message_id(msg.get("Message-ID")),
        subject=clean(str(msg.get("Subject", "") or "")).strip(),
        sender=senders[0] if senders else Address("unknown@invalid"),
        received_at=received_at or _date(msg.get("Date")) or datetime.now(UTC),
        body_text=clean(text).strip()[:MAX_BODY_CHARS],
        to=_addresses(msg.get_all("To", [])),
        cc=_addresses(msg.get_all("Cc", [])),
        in_reply_to=reply_to[0] if reply_to else None,
        references=_ids(msg.get("References")),
        provider_thread_id=provider_thread_id,
        body_html=clean(html)[:MAX_BODY_CHARS] if html else None,
        attachments=attachments,
    )


def build_mime(mail: OutgoingMail, sender: str) -> tuple[bytes, str]:
    """Return the message bytes and its Message-ID (normalised)."""
    msg = EmailMessage()
    domain = sender.rpartition("@")[2] or "opspilot.local"
    message_id = email.utils.make_msgid(domain=domain)
    msg["From"] = sender
    msg["To"] = ", ".join(email.utils.formataddr((a.name, a.email)) for a in mail.to)
    msg["Subject"] = mail.subject
    msg["Date"] = email.utils.format_datetime(datetime.now(UTC))
    msg["Message-ID"] = message_id
    if mail.in_reply_to:
        msg["In-Reply-To"] = f"<{mail.in_reply_to}>"
    refs = [*mail.references, *([mail.in_reply_to] if mail.in_reply_to else [])]
    if refs:
        msg["References"] = " ".join(f"<{r}>" for r in dict.fromkeys(refs))
    msg.set_content(mail.body_text)
    for item in mail.attachments:
        main, _, sub = item.content_type.partition("/")
        msg.add_attachment(item.content.encode(), maintype=main, subtype=sub, filename=item.filename)
    return msg.as_bytes(), normalise_message_id(message_id) or message_id
