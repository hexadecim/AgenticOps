"""Domain errors. Messages are safe to show to the person signing in."""

from __future__ import annotations

from datetime import datetime


class IdentityError(Exception):
    code = "identity_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidCredentialsError(IdentityError):
    """Same error for unknown email, wrong password and deactivated account (no user enumeration)."""

    code = "invalid_credentials"

    def __init__(self) -> None:
        super().__init__("Email or password is incorrect.")


class AccountLockedError(IdentityError):
    code = "account_locked"

    def __init__(self, until: datetime) -> None:
        super().__init__("Too many failed attempts. Try again later or ask your support manager.")
        self.until = until


class InvalidMfaCodeError(IdentityError):
    code = "invalid_mfa_code"

    def __init__(self) -> None:
        super().__init__(
            "That code is not valid. Enter the current 6-digit code from your authenticator app."
        )


class ChallengeExpiredError(IdentityError):
    code = "challenge_expired"

    def __init__(self) -> None:
        super().__init__("Your sign-in took too long. Start again.")


class PasswordChangeRequiredError(IdentityError):
    code = "password_change_required"

    def __init__(self) -> None:
        super().__init__("Choose a new password to finish signing in.")


class WeakPasswordError(IdentityError):
    code = "weak_password"


class EmailAlreadyRegisteredError(IdentityError):
    code = "email_taken"

    def __init__(self) -> None:
        super().__init__("A user with this email already exists.")


class UserNotFoundError(IdentityError):
    code = "user_not_found"

    def __init__(self) -> None:
        super().__init__("User not found.")


class LastAdminError(IdentityError):
    code = "last_admin"

    def __init__(self) -> None:
        super().__init__("At least one active admin must remain.")


class SelfLockoutError(IdentityError):
    code = "self_lockout"

    def __init__(self) -> None:
        super().__init__("You cannot deactivate your own account or remove your own admin role.")


class SessionNotRenewableError(IdentityError):
    code = "session_not_renewable"

    def __init__(self) -> None:
        super().__init__("Your session can no longer be renewed. Sign in again.")
