from __future__ import annotations


class ApprovalError(Exception):
    code = "approval_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(ApprovalError):
    code = "not_found"

    def __init__(self, approval_id: str) -> None:
        super().__init__(f"There is no approval {approval_id}.")


class NotAllowedError(ApprovalError):
    code = "not_allowed"


class AlreadyDecidedError(ApprovalError):
    code = "already_decided"

    def __init__(self, status: str) -> None:
        super().__init__(f"Someone already decided this ({status}). Reload to see the outcome.")


class InvalidDecisionError(ApprovalError):
    code = "invalid_decision"
