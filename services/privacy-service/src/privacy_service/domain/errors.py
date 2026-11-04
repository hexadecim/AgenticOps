"""Errors of privacy-service. Every failure on the pseudonymise path is fatal for the request: fail closed."""

from __future__ import annotations


class PrivacyError(Exception):
    code = "privacy_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PrivacyUnavailableError(PrivacyError):
    """Something needed to pseudonymise safely is missing (mail-service, a detector).
    Never answer with raw text instead."""

    code = "privacy_unavailable"


class LeakDetectedError(PrivacyError):
    """The tokenised text still contained an identifier: refuse rather than pass it on."""

    code = "privacy_leak_blocked"

    def __init__(self, kinds: list[str]) -> None:
        super().__init__(f"Pseudonymisation stopped: {', '.join(kinds)} still present after tokenising.")


class EmailNotFoundError(PrivacyError):
    code = "email_not_found"

    def __init__(self, email_id: str) -> None:
        super().__init__(f"Email {email_id} does not exist or was deleted after the retention period.")


class UnknownTokenError(PrivacyError):
    code = "unknown_token"

    def __init__(self, token: str) -> None:
        super().__init__(f"There is no identity behind {token}, or it was erased.")


class NotAllowedError(PrivacyError):
    code = "not_allowed"


class InvalidRequestError(PrivacyError):
    code = "invalid_request"


class RequestNotFoundError(PrivacyError):
    code = "dsr_not_found"

    def __init__(self, dsr_id: str) -> None:
        super().__init__(f"There is no data request {dsr_id}.")


class StoresUnavailableError(PrivacyError):
    """A store could not answer: an export or erasure would be incomplete, so it does not start."""

    code = "stores_unavailable"

    def __init__(self, stores: list[str]) -> None:
        super().__init__(
            f"Not available right now: {', '.join(stores)}. Nothing was changed. Try again shortly."
        )


class SubjectBusyError(PrivacyError):
    code = "subject_busy"

    def __init__(self, runs: int) -> None:
        super().__init__(f"{runs} agent run(s) for this person are in progress. Try again in a few minutes.")
