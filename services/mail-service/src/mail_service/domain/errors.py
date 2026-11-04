"""Errors the core understands. Adapters translate driver and resilience errors into these."""

from __future__ import annotations


class MailError(Exception):
    code = "mail_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class MailboxUnavailableError(MailError):
    """The provider could not be reached, timed out, failed, or its circuit is open. Try again later."""

    code = "mailbox_unavailable"

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class MailboxRejectedError(MailError):
    """The provider answered but refused: wrong credentials, missing permission, unknown folder."""

    code = "mailbox_rejected"


class UnknownProviderError(MailError):
    code = "unknown_provider"

    def __init__(self, provider: str) -> None:
        super().__init__(f"No mailbox adapter is registered for provider '{provider}'.")


class SettingsUnavailableError(MailError):
    code = "settings_unavailable"


class EmailNotFoundError(MailError):
    code = "email_not_found"

    def __init__(self, email_id: str) -> None:
        super().__init__(f"Email {email_id} does not exist or was deleted after the retention period.")


class DetokeniseUnavailableError(MailError):
    """privacy-service cannot resolve tokens right now: the message waits. Nothing goes out with tokens."""

    code = "detokenise_unavailable"


class DetokeniseRefusedError(MailError):
    """privacy-service refused for good (e.g. an unknown or erased token): this message cannot go out."""

    code = "detokenise_refused"
