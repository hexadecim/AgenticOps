"""mail-service use cases."""

from .mailbox import ActiveMailbox, ApplyMailboxSettings
from .polling import Outcome, PollMailbox, PollResult
from .queries import GetEmail, GetMailboxStatus
from .retention import PurgeExpiredEmails
from .sending import QueueMessage, RecordMessageApproval, SendDueMessages

__all__ = [
    "ActiveMailbox",
    "ApplyMailboxSettings",
    "GetEmail",
    "GetMailboxStatus",
    "Outcome",
    "PollMailbox",
    "PollResult",
    "PurgeExpiredEmails",
    "QueueMessage",
    "RecordMessageApproval",
    "SendDueMessages",
]
