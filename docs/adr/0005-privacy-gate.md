# ADR-0005: Pseudonymisation gate, token vault and append-only audit

- Status: Accepted · 2026-10-08

## Context
Support mail carries names, addresses, phone numbers and bank details. The AI model, Jira and the audit log
must work without them, while a person can still identify a customer when there is a reason, and a reply can
still reach the real address. Every such access has to be provable later (GDPR Art. 5(2)).

## Decision
- **Only privacy-service reads raw mail.** mail-service's internal email API accepts only privacy-service's
  service token; the orchestrator asks privacy-service to pseudonymise an email by id and never sees the
  original.
- **Detection = header identities + patterns + Presidio.** Names and addresses from From/To/Cc are matched
  everywhere in the text (full name and name parts), which is the most reliable source. Regex with validation
  finds e-mail, phone (8–15 digits, international or leading 0) and IBAN (mod-97). Presidio with the small
  spaCy models (`en_core_web_sm`, `de_core_news_sm`, language guessed from common words) finds other names.
  Overlaps keep header > pattern > NER, then the longer span. Greetings and role words ("Hallo Support") are
  not names. Signatures and quoted history are removed before detection.
- **Stable tokens.** One token per person and value across all mail (`[PERSON_7]`), sequential per kind. The
  vault stores values Fernet-encrypted and finds them by HMAC-SHA256 of the normalised value (derived key),
  never by plaintext. A header name and its address share a subject, so a reveal shows both.
- **Fail closed.** Any failure (mail-service down, a detector error) is a 503 `privacy_unavailable` with no
  content. A final leak check re-scans the output with the patterns; if an address, phone or IBAN survived,
  the request fails (`privacy_leak_blocked`) and nothing is stored or announced. Callers must stop, never
  fall back to raw text.
- **Reveal and detokenise are narrow.** People in case-working roles (not Admin) may reveal a token with a
  reason; the values are returned once with a 5-minute display window and the access is audited. Only
  mail-service may detokenise, only to send an approved message; an unknown token fails the whole request.
- **Audit is append-only.** audit-service consumes every event (`#`) through the inbox, keyed by event id,
  and describes it in the wireframe's columns, showing people by role and never by name. Unknown or malformed
  events are still recorded. Postgres triggers reject UPDATE, DELETE and TRUNCATE on `audit.entries` for
  every role, the owner and superuser included.

## Consequences
- Small spaCy models miss some names that the large ones would find, and over-tokenise some capitalised
  words. Header identities and patterns carry most of the load; misses in free text are the residual risk.
- The 5-minute window is enforced by the console, not cryptographically: the values have been sent once.
- A database owner or superuser can still `ALTER TABLE … DISABLE TRIGGER`. Milestone 9 moves table
  ownership to a separate role (audit-service gets INSERT/SELECT only) and may add a hash chain.
- Events published before audit-service existed are not in the log.
