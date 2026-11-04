"""mail-service use cases."""

from .dsr import QueueDsrMessage, SendDsrMessages
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
    "QueueDsrMessage",
    "QueueMessage",
    "RecordMessageApproval",
    "SendDsrMessages",
    "SendDueMessages",
]
