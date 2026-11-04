# ADR-0008: Data requests and retention

Status: accepted (2026-11-04)

## Context

GDPR Articles 15 to 21 give people rights over their data, due within one month (Art. 12(3)). A person's data sits in
several services, each owning its schema, and no service may read another's. Erasure cannot be undone. The audit log is
append-only by rule, and pseudonymised text only loses its link to a person when the vault entry goes.

## Decision

**privacy-service owns the request.** A compliance lead logs it with the requester's address; the only role that can act.
The person is found through the vault's keyed hash. The address is encrypted on the request, is never returned by the API
or put in an event, and is deleted when the request closes. Nothing is added to the vault for an unknown address.

**Each service answers for its own data** through one shared contract (`opspilot_common.subjects`): an internal route
`POST /internal/v1/subject-data` (privacy-service only) and a consumer of `subject.erasure_requested`. Order matters:
mail-service first (it finds the emails a person wrote by address, including those not tokenised yet), then the
orchestrator (the runs on those emails), then stores keyed by run.

**Whose data is whose.** A person's data means the emails they wrote and the runs on them. Someone else's case that merely
names them stays: their run is not deleted, the token in it stops resolving, and the raw email goes in the 30-day purge. The
screen shows the count. Exports copy only what the person wrote, and other people stay tokens (Art. 15(4)).

**Verification is a recorded statement.** The compliance lead states how the identity was checked before export or
erasure can start. OpsPilot does not run a challenge email itself. An export can only go to the stored address.

**Export.** The decision publishes `dsr.send_requested`; mail-service queues a row with no personal data and asks
privacy-service for the content at the moment of sending. The package is built then and never stored. Any store that
cannot answer means no export: never a partial one. Sending is at most once, like customer messages.

**Erasure.** It refuses to start if a store cannot answer, or if a run is being worked on. Then, in one transaction, the
vault entries and tokenised emails go and `subject.erasure_requested` is written, so the tokens die at once. Erased email
ids are remembered, so a queued run cannot pseudonymise them again; a hash of each dedupe key stops the poller fetching them
again. Stores delete and acknowledge in one transaction, and the confirmation goes out only after every store has
acknowledged.

**What stays, deliberately.**
- The audit log: legal obligation and rule 8. No path to change it was added; `audit-service` only reads for this.
- Jira issues hold tokens only. We do not edit them; the link in `jira-connector` goes and the token resolves to nothing.
- The mailbox itself (for example Gmail): OpsPilot only reads it, so the originals must be deleted there by hand. The
  screen says so, and so does the store's acknowledgement.
- Backups are outside this design.

**Rule 6 and these emails.** Rule 6 asks for `approval.decided` before outbound email. A data-request answer is decided here
by the compliance lead on the Privacy screen, not at a gate in the approval queue: `dsr.send_requested` carries that
decision and role, mail-service refuses any other role, and the content is released only if the request is in the matching
state. The erasure confirmation follows from the erasure decision.

**Other request types.** Rectification, restriction and objection are tracked with due dates and closed by a person with a
recorded outcome. OpsPilot does not carry them out, and the screen says so. Any request can be refused the same way (Art. 12(5)).

**Retention.** The wireframe fixes the vault at 30 days after close and leaves the rest "[agree with client]".
- Raw email: 30 days (existing job).
- Vault: unused for 30 days and no open run. If the orchestrator cannot answer, nothing goes. The job is **off by default**
  (`VAULT_PURGE_ENABLED`), because it permanently removes identities from a database that holds real data.
- Tokenised emails, checkpoints: no job until a period is agreed. The schedule on the screen says what really happens.
- Audit log: never deleted, which conflicts with the wireframe's "90 days". Rule 8 wins until the client decides.

## Consequences

- A store added later must implement the contract and be listed in `bootstrap.py` and `ERASING_STORES`.
- reporting-service gains an outbox, only to acknowledge erasures.
- Erased cases leave the dashboard's counts.
- A store that cannot answer gives 409, not 503: privacy-service is healthy, and a 5xx would trip callers' breakers.
- A run still in progress delays erasure by a few minutes. If it outlasts RabbitMQ's retries after the request started, the
  event lands in the DLQ and the request stays "Erasing" with that store listed: visible, and fixable by replaying it.
- The wireframe's "Traces", "Site memory" and "Knowledge base" rows are left out: those stores do not exist.
