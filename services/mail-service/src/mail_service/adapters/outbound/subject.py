"""What mail-service holds on a person, and erasing it. Raw email is real data, so this answers privacy-
service only. A tombstone (hash of the dedupe key) stops the poller from fetching an erased email again.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from opspilot_common.events import Outbox
from opspilot_common.events.catalogue import SubjectErasureRequested
from opspilot_common.subjects import StoreReport, SubjectQuery
from opspilot_common.subjects_http import erased_envelope
from sqlalchemy import String, cast, delete, func, insert, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .sql import MailTables

SERVICE = "mail-service"


def dedupe_hash(dedupe_key: str) -> str:
    return hashlib.sha256(dedupe_key.encode()).hexdigest()


class SqlMailSubjectStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], tables: MailTables) -> None:
        self._sessions, self._t = sessions, tables
        self._outbox = Outbox(tables.outbox)

    async def report(self, query: SubjectQuery) -> StoreReport:
        e, o = self._t.emails, self._t.outgoing
        async with self._sessions() as session:
            found = set(query.own_email_ids)
            if query.address:
                # JSON text: the address as a whole value, any case. Emails not tokenised yet are found here.
                wanted = f'"email": "{query.address.strip().lower()}"'
                rows = await session.execute(
                    select(e.c.id).where(
                        func.lower(cast(e.c.sender, String)).contains(wanted, autoescape=True)
                    )
                )
                found |= {str(r.id) for r in rows}
            rows = await session.execute(
                select(e.c.id, e.c.subject, e.c.body_text, e.c.received_at).where(e.c.id.in_(found))
            )
            own = list(rows)
            mentioned = (
                await session.execute(
                    select(func.count()).where(
                        e.c.id.in_(set(query.mentioned_email_ids) - {str(r.id) for r in own})
                    )
                )
            ).scalar_one()
            sent = list(
                await session.execute(
                    select(o.c.subject, o.c.body, o.c.status, o.c.created_at).where(
                        or_(o.c.email_token.in_(query.tokens), o.c.name_token.in_(query.tokens))
                    )
                )
            )
        items: list[dict[str, str | None]] = []
        if query.include_items:
            items += [
                {
                    "type": "email you wrote",
                    "subject": r.subject,
                    "body": r.body_text,
                    "received_at": r.received_at.isoformat(),
                }
                for r in own
            ]
            items += [
                {
                    "type": "reply to you",
                    "subject": r.subject,
                    "body": r.body,
                    "status": r.status,
                    "created_at": r.created_at.isoformat(),
                }
                for r in sent
            ]
        what = "emails they wrote and replies to them"
        if mentioned:
            plural = "email" if mentioned == 1 else "emails"
            what += f"; named in {mentioned} other {plural} (goes in the 30-day purge)"
        return StoreReport(
            store="mail",
            label="Mail store",
            what=what,
            count=len(own) + len(sent),
            email_ids=sorted(str(r.id) for r in own),
            items=items,
        )

    async def erase(self, request: SubjectErasureRequested) -> None:
        e, o, tomb = self._t.emails, self._t.outgoing, self._t.erased_emails
        async with self._sessions() as session, session.begin():
            ids = request.own_email_ids
            keys = await session.execute(select(e.c.dedupe_key).where(e.c.id.in_(ids)))
            for row in keys:
                hashed = dedupe_hash(row.dedupe_key)
                seen = await session.execute(select(tomb.c.key_hash).where(tomb.c.key_hash == hashed))
                if seen.first() is None:
                    await session.execute(insert(tomb).values(key_hash=hashed, erased_at=datetime.now(UTC)))
            emails = await session.execute(delete(e).where(e.c.id.in_(ids)))
            replies = await session.execute(
                delete(o).where(or_(o.c.email_token.in_(request.tokens), o.c.name_token.in_(request.tokens)))
            )
            count = int(emails.rowcount) + int(replies.rowcount)  # type: ignore[attr-defined]
            detail = "The mailbox itself is not changed: delete the originals there by hand."
            await self._outbox.add(session, erased_envelope(SERVICE, request, "mail", count, detail))
