from __future__ import annotations

from mail_service.adapters.outbound.providers.mime import html_to_text, parse_mime
from mail_service.domain.mail import MailboxConfig, normalise_message_id

from ..conftest import raw_mail


def test_html_only_mail_becomes_readable_text() -> None:
    html = (
        "<html><head><style>p{}</style></head>"
        "<body><p>Hello&nbsp;team,</p><p>VPN <b>down</b></p></body></html>"
    )
    mail = parse_mime(raw_mail(body="", html=html), provider_id="1")
    assert mail.body_text == "Hello\xa0team,\nVPN down"
    assert mail.body_html and "<b>down</b>" in mail.body_html


def test_plain_part_is_preferred_over_html() -> None:
    mail = parse_mime(raw_mail(body="plain wins", html="<p>html</p>"), provider_id="1")
    assert mail.body_text == "plain wins"


def test_missing_and_odd_headers_do_not_break_parsing() -> None:
    mail = parse_mime(b"Subject: =?utf-8?q?St=C3=B6rung?=\r\n\r\nbody", provider_id="7")
    assert mail.subject == "Störung" and mail.message_id is None and mail.sender.email == "unknown@invalid"
    assert mail.dedupe_key("imap") == "imap:7"  # no Message-ID: fall back to the provider id


def test_message_ids_compare_case_insensitively_without_brackets() -> None:
    assert normalise_message_id(" <ABC@Host> ") == "abc@host"
    assert normalise_message_id("<>") is None


def test_html_to_text_skips_scripts() -> None:
    assert html_to_text("<script>alert(1)</script><div>a</div><div>b</div>") == "a\nb"


def test_config_repr_never_shows_secrets_and_fingerprint_tracks_mailbox_identity() -> None:
    config = MailboxConfig(
        1, "imap", "INBOX", 60, {"imap_host": "a", "username": "u"}, {"password": "s3cret"}
    )
    assert "s3cret" not in repr(config)
    moved = MailboxConfig(
        2, "imap", "INBOX", 120, {"imap_host": "b", "username": "u"}, {"password": "s3cret"}
    )
    slower = MailboxConfig(3, "imap", "INBOX", 300, {"imap_host": "a", "username": "u"}, {"password": "new"})
    assert config.fingerprint != moved.fingerprint and config.fingerprint == slower.fingerprint


def test_nul_and_control_characters_are_removed() -> None:
    raw = raw_mail("Dussehra\x00 offer", body="Dear Sanjay,\x00\x07 enjoy\tup to 25%\noff")
    mail = parse_mime(raw, provider_id="1")
    assert mail.subject == "Dussehra offer" and mail.body_text == "Dear Sanjay, enjoy\tup to 25%\noff"
