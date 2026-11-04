# ADR-0007: Comms and the customer-message gate (T3)

- Status: Accepted · 2026-11-04

## Context
OpsPilot now has to write to customers. An email cannot be recalled, it goes outside the company, and it holds
personal data only at the last moment. The local 8B model is good at wording and unreliable at everything else.
The architecture sketch put the follow-up question before gate T1 and let Comms speak at several points in a run.

## Decision
- **One message per run, at the point where the run has something to say.** The run's outcome picks the kind:
  `resolved` → resolution update; `no_fix` or `linked` with missing details → follow-up question; everything else
  that got a ticket (`no_fix`, `linked`, `fix_rejected`, `fix_failed`) → acknowledgement. A rejected ticket or an
  ignored email gets nothing. T3 waits for a person, so it comes after T1 and T2 rather than holding them up:
  an unanswered message must never delay a ticket or a production fix. This replaces the earlier sketch.
  Cost: the customer hears nothing until diagnostics has finished and T2, if any, has been decided.
- **Recipients.** A resolution goes to everyone linked to the ticket, each person once, each in their own email
  as a reply to the email they sent. The other kinds answer this run's reporter only. Runs store their reporter
  (tokens and email id). A linked report without a stored reporter fails the recipients check rather than being
  silently skipped. A report linked after the approver has seen the recipients gets its own acknowledgement,
  but not that resolution: nobody approved sending it to them.
- **The model writes only the middle.** Greeting, follow-up questions, reference, signature and AI disclosure are
  added by code, so they cannot be forgotten. An unreadable answer falls back to a template that the screen
  labels as such; an outage parks the run as `waiting_llm`.
- **Checks are plain code in `opspilot_common.drafts`.** Facts (every reference and figure must appear in what the
  model was given), no promises or ETAs, no other personal data, recipients = linked reporters, AI disclosure
  present. The orchestrator runs them on the draft; approval-service runs them again on the text about to go out,
  with the approver's edits applied, and refuses to approve while a blocking check fails. A figure the approver
  typed in themselves is a warning, not a stop: they are the source of that fact. Everything else still blocks.
  The checks use patterns: **a name typed as plain text is not caught**; reading the draft is the approver's job.
- **Send back at most twice** (first draft + 2 revisions), enforced in approval-service and again in the graph.
  Each draft is a new approval with its own deterministic id (`message:<revision>`). The wireframe has no way out
  once revisions run out, so the screen adds "Don't send" (a reject with a reason), which also ends the run's
  messaging when the customer was reached another way.
- **mail-service sends only what it has seen approved.** It queues `message.send.requested` per recipient and
  holds each row until it has itself consumed `approval.decided` (approve, gate T3, allowed role), in either
  order, as jira-connector and ops-service do for their actions.
- **Tokens stay tokens until the moment of sending.** Rows and events are tokenised. mail-service detokenises per
  recipient through privacy-service; the result is used once, never stored or logged. privacy-service down ⇒
  the row waits (fail closed); an unknown token ⇒ the row fails rather than a customer receiving `[PERSON_9]`.
- **Last guard on who receives it:** the detokenised address must equal the sender of the email this message
  answers (from `mail.emails`), or the row fails.
- **At most once.** A row is claimed (`sending`) before the provider is called. A row found in that state after a
  crash may have gone out, so it is marked failed and reported, never repeated. Unavailable providers are retried
  with back-off for 24 h, then the row fails. `message.sent` / `message.failed` are written in the same transaction
  as the last row's status. Finished rows are purged with the 30-day retention job.
- **The outcome of the run stands whatever happens to the message.** A failed send does not undo a fix. The health
  verdict is announced as soon as it is known (`run.status_changed`, node `health_check`) and repeated when the run
  ends; reporting keeps it as `verdict`, so a fixed incident counts as resolved while its message waits at T3.
  A sent follow-up ends the run as `waiting_customer` ("Waiting on customer" on the Dashboard) until a new email
  arrives on the thread.

## Consequences
- A retry after a provider *timeout* can duplicate an email if the provider had accepted it. Accepted: the
  alternative is losing the message silently, and the window is small.
- Messages are English only, and the promise check knows English phrases only. An approver editing in another
  language gets no help from it.
- With the mailbox on real Gmail, approving T3 sends real mail from that account. For tests, use the dev mailbox.
