# OpsPilot — implementation plan and status

Living tracker for building OpsPilot. Update it at the end of every work session, together with the
memory log in [CLAUDE.md](../CLAUDE.md). Architecture and rationale: [architecture.md](architecture.md),
decisions: [adr/](adr/). Wireframe: https://claude.ai/artifact/9MVMvgd7L4LeEs1yPFUbV7

**Legend:** `[x]` done and verified · `[~]` partly done (see note) · `[ ]` not started

_Last updated: 2026-11-03_

## Summary

| # | Milestone | Status | Verified by |
|---|---|---|---|
| 0 | Foundations: repo, shared library, infrastructure | ✅ Done | 31 unit tests · live demo `scripts/demo_foundations.py` |
| 1a | Authentication: identity-service, api-gateway, sign-in UI, user admin | ✅ Done | 27 service tests · live API walkthrough on :8080 |
| 1b | Admin settings: settings-service, Admin › Integrations, System health | ✅ Done | 21 service + 2 gateway tests · live checks against Ollama and dev mailbox |
| 2 | mail-service: IMAP / Gmail / Microsoft Graph | ✅ Done | 49 service tests (30 contract runs incl. live GreenMail) · live SMTP → `email.received` in 37 s |
| 3 | privacy-service + audit-service | ✅ Done | 36 service tests (incl. real Presidio) · live sign-in in audit API, tampering rejected, fail-closed with mail-service down |
| 4 | Agent core + T1 gate → Jira (**MVP**) | ✅ Done | 31 new service tests · live: 2 emails → Approvals → approved in the UI → KAN-18, KAN-19 created; audit chain complete |
| 5 | reporting-service, Dashboard, Audit log UI | ✅ Done | 5 service + 3 shared + 1 gateway tests · live: dashboard from real history, breaker trip/recovery shown |
| 6 | ops-service, diagnostics, T2 production-fix gate | ✅ Done | 22 ops + 11 orchestrator + 4 approval + 1 gateway tests · live in the UI: T1 → KAN-29 → T2 → resolved; Platform health probes; T2 reminder fired |
| 7 | Comms agent, T3 customer-message gate, outbound email | ⬜ | – |
| 8 | Privacy / GDPR screens and retention jobs | ⬜ | – |
| 9 | Hardening: chaos tests, tracing, load, production config | ⬜ | – |

Progress: **8 of 11** milestones done (0–6; 4 = **MVP**). 254 automated tests.

## Dev environment (current state)

| What | Where |
|---|---|
| App | http://localhost:8080 (`make up`; Docker Desktop must be running) |
| Gateway API docs | http://127.0.0.1:8000/api/docs |
| RabbitMQ / Jaeger | http://localhost:15672 (opspilot / opspilot) · http://localhost:16686 |
| Admin sign-in | `admin@opspilot.local`; password reset on 2026-10-08 to the one you chose (not stored in the repo; `.env`'s `BOOTSTRAP_ADMIN_PASSWORD` no longer matches). Authenticator reset: first sign-in shows the QR code |
| Demo users | analyst@, agent@, manager@, compliance@opspilot.local — set a temporary password via Users and roles › Manage › Reset password |
| Deactivated helper users | walkthrough@, check-1b@, check-m3-…@, m6-walk@ and m6-gaps@opspilot.local (used for automated checks; leave deactivated) |
| Approver for tests | `analyst@opspilot.local` (L1 Analyst), password and authenticator set by you on 2026-10-08 |
| Jira | `supportautomation.atlassian.net`, project KAN, type Incident. OpsPilot created KAN-18 (you deleted it), KAN-19, KAN-28 and KAN-29 (M6 walk-through) |
| Agent services | approval-service :8006, jira-connector :8007, agent-orchestrator :8008, reporting-service :8009 (compose-internal) |
| Screens | Approvals http://localhost:8080/approvals · Dashboard /dashboard · Audit log /audit (compliance lead, support manager, admin) |
| Dev mailbox | GreenMail `devmail`: IMAP localhost:3143, SMTP localhost:3025, `support@opspilot.local` / `support-dev-pass` |
| Saved settings | Mailbox → **your Gmail via IMAP** (`imap.gmail.com`, v5, poll 360 s; you changed it from the dev mailbox) · Local AI model → Ollama defaults (v1) · Jira → not configured |
| privacy-service / audit-service | Ports 8004 / 8005 inside compose only. Reveal: `POST /api/privacy/reveals`; audit: `GET /api/audit/entries?kind=human\|tool\|privacy&q=&cursor=` (Compliance lead, Support manager, Admin) |
| mail-service | Port 8003 inside compose only (not published). Polls the dev mailbox every 60 s; status at `/api/mail/status`, Admin "check now" at `POST /api/admin/mail/poll` |
| Ollama | Host PC, `deepseek-r1:8b` + `nomic-embed-text`; ~70 s per triage call with thinking on |
| Git | `github.com/hexadecim/AgenticOps`; one feature branch per milestone, squash-merged into `main` by PR | The MVP checkpoint (email → approval → Jira) is milestone 4.

---

## Milestone 0 — Foundations ✅

**Goal:** everything every service needs, so services contain only their own logic.

- [x] Monorepo: uv workspace, ruff, mypy `--strict`, pytest-asyncio, Makefile, `.env.example`
- [x] `CLAUDE.md`: rules, architecture memory, conventions, recipes, memory log
- [x] ADRs: 0001 microservices · 0002 circuit breaker · 0003 RabbitMQ + outbox · 0004 local LLM
- [x] `opspilot-common/resilience`
  - [x] Native asyncio circuit breaker (closed → open → half-open), state shared in Redis
  - [x] Retry (transient errors only), per-attempt timeout, bulkhead, fallback helper
  - [x] `ResilientHttpClient`: 5xx trips breaker, 4xx passes through, POST retried only with idempotency key,
        transport errors surface as `ResilienceError`
  - [x] Listeners: logs, Prometheus `opspilot_circuit_state`, `breaker.state_changed` event
  - [x] Per-dependency config `deploy/resilience.yaml` (Ollama 180 s timeout, bulkhead 2)
- [x] `opspilot-common/events`: envelope, versioned catalogue (16 event types), transactional outbox + relay,
      inbox dedupe, RabbitMQ topic exchange with retry queue and DLQ, in-memory bus, `EventRuntime`
- [x] `opspilot-common/auth`: roles, gate → approver map (T2 = Support agent only), JWT issue/verify
- [x] `opspilot-common`: structlog JSON logs with correlation id, FastAPI health/metrics/auth helpers
- [x] Infrastructure compose: Postgres 16 + pgvector (10 schemas, 1 login per service, cross-schema writes
      denied), Redis, RabbitMQ, Toxiproxy (Ollama fault injection), Jaeger
- [x] Architecture checks (import-linter): layers per service, framework-free core, services independent
- [x] Live demo against real infra: outbox → RabbitMQ, poison message → DLQ, Ollama breaker open/half-open/
      closed via Toxiproxy, deepseek-r1:8b structured intake (~70 s per call on this PC)
- [x] CI workflow (`.github/workflows/ci.yml`) — runs on every PR and push to `main` on GitHub, green since PR #1

## Milestone 1a — Authentication ✅

**Goal:** people sign in with password + authenticator code; admins manage users and roles.

**identity-service**
- [x] Hexagonal layout: domain (User aggregate, policies) → use cases → ports ← adapters
- [x] Password step → TOTP step; first sign-in enrols the authenticator (QR + manual key)
- [x] argon2id hashing with rehash-on-login; identical error for unknown email / wrong password
- [x] TOTP replay guard (each 30-s code usable once), ±1 step clock drift
- [x] Lockout after 5 failures (password or code) for 15 min
- [x] Temporary password → forced change at first sign-in; weak new password does not burn the code
- [x] Password policy: ≥12 chars, not repetitive, no email name
- [x] Session JWT 30 min; renewal re-reads the user (role changes and deactivation apply within 5 min)
- [x] MFA challenge token signed with a separate derived key (cannot be used as a session)
- [x] TOTP secrets Fernet-encrypted at rest
- [x] Admin: list / create / edit roles / deactivate / reset password / reset authenticator
- [x] Guards: cannot deactivate yourself or remove the last active admin
- [x] Every sign-in and admin change → `user.activity` event via outbox (verified in RabbitMQ)
- [x] Alembic migration in schema `identity`; bootstrap admin from `.env`; dev seed for 4 demo roles
- [x] Tests: 19 (unit with in-memory fakes, repository contract suite run on in-memory + SQL, API)

**api-gateway (BFF)**
- [x] Session only in HttpOnly, SameSite=Strict cookie (token never visible to JavaScript)
- [x] CSRF: unsafe methods need `X-OpsPilot-Client: web`
- [x] Sign-in rate limit in Redis (fails open if Redis is down)
- [x] identity-service behind circuit breaker → 503 `service_unavailable` with `Retry-After`
- [x] Security headers; health `/api/health/ready` shows circuit states; OpenAPI at `/api/docs`
- [x] Tests: 8 (cookie flags, forwarding, CSRF, error pass-through, rate limit, breaker opens, sign-out)

**Web app (React + Vite + TS)**
- [x] Theme tokens copied from the wireframe; Light / Dark / Auto, IBM Plex
- [x] Sign-in page as in the wireframe: password step, code step, QR enrolment, new-password step,
      language note, cookie notice
- [x] App shell: top bar, slide-in drawer, appearance switch, status card, sign out
- [x] Idle rule: renew on activity, warning at 28 min, sign-out at 30 min
- [x] Users and roles screen: table, add user (temp password shown once), manage panel
- [x] Placeholders for Approvals, Dashboard, Audit log, Privacy (state their milestone)
- [x] Docker image (nginx) serving the SPA and proxying `/api` on the same origin

**Deferred from 1a** (tracked in Known issues / later milestones)
- [ ] Company single sign-on (button present, disabled)
- [ ] "Change my password" screen for signed-in users (only forced change at first sign-in exists)
- [ ] RS256 tokens with public-key-only verifiers (HS256 shared secret today, ADR-0001)
- [ ] Immediate session revocation (today: deactivation takes effect at next renewal, ≤5 min)

## Milestone 1b — Admin settings ✅

**Goal:** an admin configures mailbox, Jira and LLM in the UI; services pick changes up without a restart.

**settings-service**
- [x] Hexagonal service, schema `settings`; sections `mailbox`, `jira`, `llm` declared as field specs
      (kind, required, secret, default, help, provider condition) — the UI renders forms from them
- [x] Mailbox providers: IMAP/SMTP, Gmail API, Microsoft Graph; fields appear only for the chosen provider,
      and switching provider drops the old provider's values and secrets
- [x] Validation with a message per field; all problems reported at once
- [x] Secrets write-only: Fernet-encrypted at rest, API returns only "set / not set"; omit = keep, text = replace
- [x] Versioned saves with optimistic locking (stale save → 409 "someone else saved")
- [x] `settings.changed` event via outbox with field **names** only (verified: no secret in events)
- [x] Test connection on unsaved edits merged with saved secrets, through separate `probe-*` circuits:
  - [x] Ollama: reachable, chat model installed, embedding model installed (verified live against this PC)
  - [x] IMAP + SMTP: sign-in, folder exists + message count (verified live against dev mailbox)
  - [x] Jira: credentials, project, issue type, CREATE_ISSUES permission (unit-tested with mocks)
  - [x] Gmail API: refresh token, mailbox address, label (unit-tested with mocks)
  - [x] Microsoft Graph: client credentials, mailbox/folder, permission hint (unit-tested with mocks)
- [x] Tests: 21 (field rules, versioning, secret handling, every tester, API incl. encryption at rest)

**api-gateway**
- [x] `/api/admin/settings/*` forwarded behind the `settings-service` breaker
- [x] `/api/admin/system/circuits`: every dependency's live state from shared Redis + configured thresholds
- [x] All services now keep breaker state in Redis (`redis_registry` in opspilot-common)
- [x] Fixed: sign-in rate limit keyed on the real client address (nginx overwrites `X-Forwarded-For`,
      uvicorn trusts it, gateway port bound to localhost) — verified live, spoofed header ignored

**Web app**
- [x] Admin › Integrations: tabs with configured indicator, generated forms, saved-secret display with
      Replace, field errors, Test connection with a check list, Save with version and conflict handling
- [x] Admin › System health: circuit table, auto-refresh every 5 s

**Infrastructure**
- [x] GreenMail dev mailbox in compose (IMAP 3143, SMTP 3025, `support@opspilot.local`)

**Deferred from 1b**
- [x] Ops section (probe targets, runbook folder, executor) → done in milestone 6
- [x] Internal "resolved settings" API for services, with service-to-service authentication → done in milestone 2
- [ ] Gmail "Connect with Google" consent flow (today: paste client id/secret + refresh token)
- [ ] Jira, Gmail and Graph testers verified against real accounts (needs your credentials)

## Milestone 2 — mail-service ✅

**Goal:** new support mail is read once, stored, grouped into threads and announced as `email.received`.

**Service-to-service authentication (opspilot-common + settings-service)**
- [x] Service tokens: short-lived JWTs naming the calling service, signed with a key *derived* from the shared
      secret, own issuer + audience → a user session is never accepted as a service token and vice versa (tested)
- [x] settings-service `GET /internal/v1/sections/{id}/resolved`: values + decrypted secrets, `no-store`; per-section
      reader allow-list (`mailbox` → mail-service, `jira` → jira-connector, `llm` → orchestrator/ops) — verified live
- [x] Breaker `retry_after()` for "next try in N s"; `Inbox.guard` now typed as `EventHandler`

**mail-service** (hexagonal, schema `mail`, port 8003)
- [x] Ports `MailReader` / `MailSender` (separate), `MailboxBuilder`, `SettingsSource`, repositories, unit of work
- [x] Provider registry (open/closed): `imap`, `gmail`, `graph`; one `mail-provider` circuit for whichever is active
- [x] Shared MIME parser/builder for all providers (IMAP MIME, Gmail `format=raw`, Graph `/$value`): plain over HTML,
      HTML→text fallback, attachment **metadata only**, Message-ID / In-Reply-To / References normalised
- [x] IMAP + SMTP adapter: read-only folder, read position (UIDVALIDITY, last UID), `N:*` quirk handled, SMTP 5xx =
      rejected / 4xx = transient — **verified live against GreenMail**
- [x] Gmail API adapter (list newest-first → oldest-first, `after:` + same-second ids) — stub-verified only
- [x] Microsoft Graph adapter (`receivedDateTime ge`, `$value` MIME, `sendMail` MIME) — stub-verified only
- [x] Shared contract suite `tests/contract/test_mailbox_port.py`: 6 behaviours × fake, IMAP stub, Gmail stub,
      Graph stub and **real GreenMail** (skips when not running) = 30 runs
- [x] Poller: interval from settings, batches of 25 (up to 4 per cycle), 24 h look-back on first poll or new mailbox
- [x] Dedupe by Message-ID (else provider id), unique key in SQL — verified live after a read-position reset
- [x] Threads: In-Reply-To/References → parent's thread, else provider conversation id, else new
- [x] Each batch + its `email.received` outbox rows + read position commit in one transaction
- [x] `email.received` carries ids only (no subject, no addresses) — asserted in tests and seen live
- [x] Raw email stored in `mail.emails`; hourly retention job deletes after 30 days and records `privacy.recorded`
      (`retention_purge`) — unit-tested (job not yet observed live: nothing is 30 days old)
- [x] `settings.changed` (mailbox) → adapter rebuilt without restart, immediate poll; inbox-deduped — verified live
- [x] Internal `GET /internal/v1/emails/{id}` for agent-orchestrator / privacy-service only (people's sessions get
      403) — verified live
- [x] Status `GET /v1/status` (any signed-in role) and Admin `POST /v1/poll`: "Mailbox polled N min ago", amber when
      the last poll failed, polls are stale (> 3 intervals) or the circuit is open ("next try in N s") — verified live:
      broken host → 3 failures → circuit open → amber via gateway → restored → closed after 120 s → OK

**api-gateway + web**
- [x] `/api/mail/status`, `/api/admin/mail/poll` behind the `mail-service` breaker (503 when down) — 1 test
- [x] Drawer status card: "Mailbox · Polled N min ago", amber `--chrome-warn` with the last error as tooltip
      (bundle served live; **not yet looked at in a browser**)

**Deferred from 2**
- [ ] Consumer for `message.send.requested` (send path) → milestone 7; the `MailSender` adapters exist and pass the
      contract suite, and SMTP send is verified against GreenMail
- [ ] Gmail and Graph adapters against real accounts (needs your credentials)
- [ ] Single active poller is by configuration (`POLLER_ENABLED`); a leader lock comes with scaling (milestone 9)
- [ ] Mailbox "Check now" button in the UI (API exists)

## Milestone 3 — privacy-service + audit-service ✅

**Goal:** the agents only ever see tokenised mail; every event is in a log nobody can change. Design: ADR-0005.

**privacy-service** (hexagonal, schema `privacy`, port 8004)
- [x] Only privacy-service may read raw mail: mail-service's `RAW_EMAIL_READERS` narrowed to it (orchestrator
      gets 403 — verified live)
- [x] `POST /internal/v1/emails/{id}/pseudonymise` (agent-orchestrator only): fetches the raw mail, strips quoted
      history and signatures, detects, tokenises subject, body, From/To/Cc and attachment file names; idempotent
      per email (same record, no second audit entry) — verified live
- [x] Detection: header identities (full name, name parts, address) + validated patterns (e-mail, phone, IBAN
      mod-97) + Presidio with spaCy `en_core_web_sm` / `de_core_news_sm`; overlap rules; greetings and role words
      ("Hallo Support", a "Support" display name) are not people — Presidio verified in the Docker image
- [x] Token vault: stable token per person/value (`[PERSON_7]`), Fernet-encrypted values, HMAC lookup, name and
      address linked as one subject; contract suite on in-memory + SQL vault (no plaintext in SQL rows)
- [x] Fail closed: mail-service down or detector error → 503 `privacy_unavailable`, no content (verified live by
      stopping mail-service); leak check on the output → `privacy_leak_blocked`, nothing stored
- [x] Identity reveal `POST /v1/reveals` (L1 Analyst, Support agent, Support manager, Compliance lead — not
      Admin): reason required, values for 5 minutes, `privacy.recorded` with role only — verified live via gateway
- [x] Detokenise `POST /internal/v1/detokenise` (mail-service only, for sending; unknown token fails the request)
      — tested; first real use in milestone 7
- [x] Every pseudonymisation / reveal / detokenisation → `privacy.recorded` (token map id, legal basis)
- [x] Tests: 28 (+1 SQL-only skip): text clean-up, patterns incl. false-positive cases, merge rules, use cases
      incl. every fail-closed path, real Presidio EN/DE, vault contract, API

**audit-service** (hexagonal, schema `audit`, port 8005)
- [x] Consumes `#` through the inbox; entry id = event id (recorded exactly once)
- [x] Append-only: Postgres triggers reject UPDATE, DELETE, TRUNCATE — **verified live as table owner and as
      superuser**
- [x] Mapping for all 16 catalogue events (test fails if one is missing) to time · kind · actor · event · detail ·
      ticket · privacy record; people shown by role (new optional `actor_role` on `user.activity` and
      `privacy.recorded`), never by name; unknown/malformed events still recorded as `system`
- [x] `GET /v1/entries`: tabs (`human` / `tool` / `privacy`), search over actor, event, detail, ticket, thread
      (e.g. `[PERSON_1]`), keyset pagination; readers: Compliance lead, Support manager, Admin (L1 → 403)
- [x] **Done-criterion:** a real sign-in (throwaway analyst, password + TOTP, then deactivated) appears as
      "L1 Analyst · signed_in" via `/api/audit/entries`; UPDATE on `audit.entries` fails
- [x] Tests: 8

**api-gateway**: `/api/audit/entries`, `/api/privacy/reveals` behind their breakers — 1 test

**Fixed along the way (milestone 2 bug, found live on your Gmail)**
- [x] A real email containing a NUL byte could not be stored, and the poller retried it forever without saying
      why. The parser now strips control characters; any store failure is recorded as a failed poll with a reason
      (amber status) — 2 tests; Gmail polling verified recovered

**Deferred from 3**
- [ ] Audit log screen (milestone 5) and "Show identity" in Approve ticket (milestone 4) — APIs exist
- [ ] Retention for tokenised emails and reveal records → milestone 8 (DSR / retention)
- [ ] Table ownership split + optional hash chain for the audit log → milestone 9
- [ ] Events from before audit-service existed (earlier sign-ins) are not in the log

## Milestone 4 — Agent core + T1 → Jira (MVP) ✅

**Goal:** an email becomes a proposed Jira incident that a person approves; only then is it created.

**agent-orchestrator** (schema `orchestrator`, port 8008)
- [x] LangGraph `StateGraph`, `AsyncPostgresSaver` checkpoints in the orchestrator schema (verified live, runs survive
      restarts: a parked run resumed after a container rebuild)
- [x] Nodes: privacy_gate → intake (deepseek-r1:8b, `json_schema` output) → dedupe (Jira search + nomic-embed-text,
      threshold 0.80, plus "same mail thread already has a ticket") → request_ticket_approval → gate_ticket
      (`interrupt()`); ports injected via `config["configurable"]["ports"]`; the approval request is a separate node
      so it runs once (test: not re-sent on resume, earlier nodes not re-run)
- [x] Not a support request (newsletter, personal mail) → run `ignored`, no approval (tested; matters for your Gmail)
- [x] Runs: queued → running → waiting_approval → approved → ticket_created; rejected / failed with reason; a worker
      is the only executor, events only create or wake runs; interrupted runs re-queued at start-up
- [x] **Ollama fallback verified live**: two 180 s timeouts → run parked `waiting_llm` ("AI paused" banner via
      `/api/runs/summary`), skipped while the circuit is open, resumed automatically after the fix
- [x] privacy-service down → `waiting_privacy` (fail closed, nothing reaches the model) — tested
- [x] LLM hardening after live failure (ADR-0004 amendment): temperature 0.6, bounded output schema, 2048-token cap;
      23–58 s per intake instead of timeouts; "None"/prose-after-id normalised
- [x] `agent.tool_called` for classify and search_jira, `run.status_changed` for every transition → audit
- [x] Tests: 11 (real graph + fake ports + in-memory checkpointer + SQL runs)

**approval-service** (schema `approval`, port 8006)
- [x] Queue from `approval.requested` (idempotent), oldest first, stats (waiting by gate, oldest, sev1)
- [x] Role check per gate (T1/T3 analyst, agent, manager; T2 support agent only; compliance and admin cannot
      decide), reject needs a reason, edits limited per gate, repeat = harmless, other decision = 409
- [x] `approval.decided` via outbox with the deciding role; ticket outcome from jira-connector events
      ("Approved · ticket creation pending" → "KAN-n created" / "Jira refused: …")
- [x] Tests: 6

**jira-connector** (schema `jira`, port 8007)
- [x] Proposals from `approval.requested`; approved ticket gate → job in the same transaction (outbox for Jira)
- [x] Job worker: label `opspilot-<approval id>` searched before creating, so a replay after a crash never
      duplicates (tested); ADF description; 5xx/timeout/open circuit → retry with backoff ("creation pending");
      4xx → `jira.issue.failed` (new catalogue event, audited); waits while Jira is not configured
- [x] Duplicate → comment on the existing issue + `jira.issue.linked`
- [x] Internal context API (project, type, recent open incidents) for dedupe — **verified live against your Jira**
- [x] Tests: 12 (jobs with a fake tracker; Jira Cloud client against a simulated REST API)

**api-gateway + web**
- [x] `/api/approvals`, `/api/approvals/{id}`, `/api/approvals/{id}/decision`, `/api/runs/summary`; `/api/events`
      server-sent events with event type and ids only (test: no title or tokens leak); nginx unbuffered
- [x] Approvals queue as in the wireframe: summary cards, gate tabs with counts, rows, "Decided" tab with outcomes,
      AI-paused banner, live refresh
- [x] Approve ticket screen: tokenised email with "Show identity…" (reason, 5 min, audited), what Intake
      understood, ticket preview, dedupe result, approve / reject with reason, live outcome
- [x] **Used by you in the browser** to approve both test requests

**Done-criterion** — met twice: email → Approvals → approved in the UI → **KAN-18** and **KAN-19** created in Jira;
audit shows pseudonymise → classify → approve_ticket → create_jira_issue. (Test emails were synthetic: you asked for
nothing to be sent to your Gmail, so the second one was stored directly in mail-service and announced; mail
ingestion itself was verified in milestone 2.)

**Deferred from 4**
- [x] Severity can be changed on the Approve ticket screen before approving (dropdown with sev1–sev4 meanings,
      "AI suggested …" hint); the approval, the Jira ticket, the audit entry ("severity → sev1") and the dashboard
      use the approver's value — added 2026-10-08, 3 tests; not yet exercised live (needs a real approval)
- [ ] Editing the ticket summary on the same screen (API accepts it; no UI yet)
- [ ] Assignee / component mapping ("Assign to Support engineer" in the wireframe)
- [x] Approval reminders (15 min for T2) → done in milestone 6 (seen live 2026-11-03)
- [ ] Direct link from the approval to the Jira issue (needs the site URL in the approval)

## Milestone 5 — reporting-service, Dashboard, Audit UI ✅

**Goal:** see how OpsPilot is doing (health, stages, timings) and what happened (incident timeline, audit log).

**Breaker events, wired for real** (opspilot-common)
- [x] Found: `BreakerEventListener` existed but no service used it, so `breaker.state_changed` was never published
- [x] `EventRuntime` now registers it for every service; it publishes in the background and only when connected
- [x] Registry and breakers share one listener list: a listener added later also observes existing breakers
      (removes the "add_listener before any client" pitfall) — 2 tests

**reporting-service** (schema `reporting`, port 8009; consumes `#`, publishes nothing, calls nothing)
- [x] Read models from events, built in any arrival order: a case per run (stage, received/queued/triaged/requested/
      decided/ticket times, outcome, title, severity, site), dependency health per calling service, timeline entries
- [x] Stages: Intake (incl. AI paused), Waiting for approval, With engineering; Diagnostics, Waiting on customer and
      Resolved today shown as "not tracked yet" (milestones 6/7, Jira status sync) instead of fake zeros
- [x] Service levels (last 24 h): email → ticket median (target 5 min, flagged when breached), email → approval
      request, waiting for a person, duplicates linked, ignored, needing a person, waited for the AI
- [x] Incident timeline per Jira key: the cases' events plus dependency failures while the incident was open
- [x] Dependency health: down if any calling service has the circuit open; key dependencies always listed
- [x] `run.status_changed` gains `email_id` (additive) so email → ticket can be measured
- [x] Tests: 5 (domain timings/stages/health; out-of-order events with redelivery through the inbox → API)

**api-gateway + web**
- [x] `/api/dashboard`, `/api/incidents/{key}/timeline` behind the reporting breaker; live relay includes breaker events
- [x] Dashboard as in the wireframe: platform health cards, stages, incident timeline with picker, service levels;
      live refresh
- [x] Audit log: tabs All / People / Agents and tools / Privacy, search (token, site, ticket, thread), "Show older
      entries" paging, local time zone; non-readers get a clear message

**Verified live**
- [x] Backfill: orchestrator, approval, Jira and privacy outboxes re-published (not mail-service, so old Gmail mail is
      not triaged); reporting built 4 cases (KAN-18, KAN-19, 2 ignored Gmail messages); other consumers ignored the
      repeats (no new approvals, Jira calls or audit duplicates)
- [x] Dashboard via gateway: KAN-19 timeline (privacy gate → 2× AI paused → classify → search → request → L1 Analyst
      approved → KAN-19 created); email → ticket median 15m 38s, flagged as breaching the 5 min target
- [x] Breaker trip: mail-service stopped → gateway circuit opened → "Mail service: Down" + timeline entry → restarted
      → half-open → closed → "recovered"
- [ ] The two screens have not been looked at in a browser yet

**Real-mail test (your Gmail, 08 Oct 2026, on request)**
- [x] 16 emails of the day (IST): 14 had never been triaged (they arrived before the orchestrator existed) and were
      re-announced from mail-service's outbox (same event ids; audit/reporting ignored repeats)
- [x] Outcome: 15 ignored as not support, 1 sent to Approvals (IT Support, sev3) for you to decide; all 16
      pseudonymised first; nothing reached Jira
- [x] Found and fixed: 3 empty answers (reasoning used the whole token budget on long emails; the retry saved them) →
      the adapter now retries once without reasoning; a site of "None reported in email," → phrases that say
      "nothing" become empty — 2 tests

**Deferred from 5**
- [x] Health probes of your own systems (wireframe "Platform health") → done in milestone 6
- [ ] Resolved-today and ticket stage sync from Jira status
- [ ] Incident end time (today all system events after the start show on open incidents)
- [ ] A "rebuild read model" command (the backfill was a manual outbox replay)

## Milestone 6 — ops-service + T2 production fix ✅

Design: [ADR-0006](adr/0006-diagnostics-and-fix-gate.md). Mock environment, runbooks, logs: `deploy/ops/`.

- [x] Health probes (mock environment + optional HTTP targets, one circuit each; `probe.status` on change), masked
      log search, runbook RAG (pgvector + `nomic-embed-text`, keyword fallback) — live: 3 runbooks / 13 chunks
      indexed, RB-07 ranked first (0.886), no e-mail/IP/phone in returned lines
- [x] Allow-listed actions (`restart_service`, `retry_failed_uploads`), mock executor by default, idempotent per
      approval, re-validated and fresh probes before running — unit/integration tests
- [x] Diagnostics node proposes fixes only from the allow-list (model picks a number or `none`); health re-check
      after running → `resolved` / `fix_failed` — tests; live: deepseek chose `restart_service(upload-worker)`
- [x] T2 gate: Support agent only, parameter edits limited to offered choices, reminder every 15 min, "Evidence
      unavailable" + Approve disabled when ops-service is down (gateway 503) — tests
- [x] Admin › Integrations › Ops section (executor, enabled actions, folders, HTTP probes) + connection test
- [x] UI: Approve production fix screen — walked through in the browser (2026-11-03): fresh evidence, 15-min reminder
      notice, Edit parameters (only offered choices; "You changed the AI's proposal" note), outcome footer after deciding
- [x] Live end-to-end in the UI (2026-11-03, throwaway Support agent `m6-walk@`, since deactivated): synthetic
      "Telemetry upload stuck" email → T1 approved → **KAN-29** → diagnostics (~2 min) → T2 → service edited from the
      model's `upload-api` to `upload-worker` → mock executor cleared the fault → health re-check "All 5 probes pass" →
      run `resolved`; audit chain complete with people shown by role

**Gaps closed after the walk-through (2026-11-03)**
- [x] Platform health shows the supported systems' probes: ops-service sweeps them every minute
      (`PROBE_INTERVAL_SECONDS`, 0 = off) and announces every probe once after start-up; reporting-service keeps a
      `probes` read model (newest wins, older events ignored) and puts sweep changes on open incidents' timelines;
      gateway relays `probe.status` live — live: fault re-opened → Upload "Fail" on the Dashboard within a minute
- [x] Wrong service in the proposal (upload-api): the cause was **not** the model. `deploy/ops` had been deleted and
      recreated by a git branch switch, leaving ops-service's bind mount empty, so the log search silently found
      nothing and the candidate fell back to the probe's first service. Now an unreadable log folder is a note in
      the evidence ("Logs unavailable …"), and without log evidence the service follows the runbook's own order
- [x] Explanation cut mid-word by the schema's `max_length`: trimmed to the last full sentence (or word + "…");
      the model label sits on its own line
- [x] Fix screen after the run: approval-service records the health re-check from `run.status_changed`
      (`resolved` / `fix_failed`) and the footer says "Resolved … Health re-check: All 5 probes pass" (or asks to
      escalate) — live on KAN-29's approval
- [x] T2 reminder seen live: a labelled synthetic T2 request, back-dated 16 min, got `approval.reminder` within a
      minute (audit: "Approval service · remind_approver"); then rejected with a reason as a throwaway Support
      agent (`m6-gaps@`, deactivated)
- Reject at T2 ends the run with the reason; continuing to Comms is milestone 7.

## Milestone 7 — Comms + T3 customer message ⬜

- [ ] Comms node drafts acknowledgements / follow-ups / resolution updates with tokens
- [ ] Draft checks: facts match ticket, no ETAs, no other personal data, recipients = linked reporters,
      AI disclosure present
- [ ] T3 gate: approve / edit / send back (max 2 revisions)
- [ ] mail-service sends on `message.send.requested`, detokenised via privacy-service; `message.sent`
- [ ] UI: Approve customer message screen

## Milestone 8 — Privacy / GDPR ⬜

- [ ] DSR requests (Art. 15–21) with due dates; where a person's data is held across stores
- [ ] Export (JSON to verified address) and erasure across stores (audit keeps token only)
- [ ] Retention schedule and jobs; privacy notice / imprint / accessibility pages
- [ ] UI: Privacy and data requests screen

## Milestone 9 — Hardening ⬜

- [ ] Automated chaos tests via Toxiproxy for every breaker (today only the manual demo script)
- [ ] OpenTelemetry tracing across services into Jaeger (container runs; no instrumentation yet)
- [ ] Integration test suite against real Postgres/RabbitMQ in CI (service tests use SQLite today)
- [ ] Frontend tests (component + one end-to-end sign-in), accessibility audit
- [ ] Production config: HTTPS, `cookie_secure=true`, RS256 keys, secrets manager, per-service DB passwords
- [ ] Load test of poller and approval queue

---

## Known issues and tech debt

| # | Issue | Impact | Plan |
|---|---|---|---|
| 1 | ~~Gateway rate limit keyed on nginx's address~~ | ~~All users shared one bucket~~ | **Fixed in 1b** (verified live) |
| 2 | Nothing committed; no git remote | CI has never run | Commit milestone 0 + 1a when you're ready, then add a remote |
| 3 | Service DB passwords are dev defaults in `init.sql` and compose | Fine locally, not for any shared environment | Milestone 9 |
| 4 | Alembic migrations run on every identity container start | Race if scaled to several replicas | Separate migration job in milestone 9 |
| 5 | `make test` runs one pytest process per package (each service has its own `tests` package) | Slightly slower | Acceptable |
| 6 | Wireframe screens for later milestones show placeholders | Expected | Filled milestone by milestone |
| 8 | Admin-entered URLs (Jira site, Ollama address) are fetched by settings-service | Admins are trusted; an admin could point probes at internal hosts | Restrict to http(s) today; add an allow-list option in milestone 9 |
| 9 | Breaker state is written to Redis only on failures | Healthy dependencies show "No failures recorded" rather than "closed" | Acceptable; reads as healthy |
| 7 | Demo queues `demo.*` left in RabbitMQ by `scripts/demo_foundations.py` | Cosmetic | Delete via RabbitMQ UI or ignore |
| 10 | ~~Demo script used and emptied `mail.outbox`~~ | ~~Would have deleted mail-service's real pending events~~ | **Fixed in 2**: demo uses `mail.demo_outbox`; the old demo table was dropped (with your OK) |
| 11 | Service tokens use a key derived from the shared `JWT_SECRET` | Any service holding the secret could mint a token naming another service | Per-service RS256 keys with session tokens (milestone 9) |
| 12 | The demo script still publishes fake `email.received` events to the real exchange | From milestone 4 the orchestrator would receive ids that do not exist (→ 404 → DLQ) | Don't run the demo after milestone 4, or switch it to a demo event type |
| 13 | Audit triggers can be disabled by the table owner or a superuser (`ALTER TABLE … DISABLE TRIGGER`) | Tampering is possible for a DB administrator | Separate owner role, INSERT/SELECT grant only, optional hash chain (milestone 9) |
| 14 | privacy-service vault key is derived from the shared `FERNET_KEY` | Compromise of one service's env exposes the vault | Per-service keys from a secrets manager (milestone 9) |
| 15 | Two first-time pseudonymisations of the same new person at the same moment | One fails on the vault's unique key (500); a retry succeeds | Acceptable; the orchestrator retries (milestone 4) |
| 16 | Small spaCy models | Some names in free text are missed or capitalised words over-tokenised | Header identities + patterns carry most; consider larger models if the PC allows |
| 17 | The mailbox now reads your real Gmail | Real mail is stored in `mail.emails` (30-day retention) and announced as events | Intended by your settings; switch back to the dev mailbox in Admin › Integrations for tests |
| 18 | Every new Gmail message now runs through the local model (~25–60 s each) | Personal mail is triaged; mail the model misjudges as support would appear in Approvals | Reject such items; newsletters are `ignored`. Consider a sender allow-list or a dedicated support mailbox |
| 19 | deepseek-r1:8b answers vary at temperature 0.6 | Severity and wording can differ between runs | Approver checks ("AI-generated"); edit-before-approve UI is deferred |
| 20 | Two synthetic test emails were added for milestone 4 | One was stored directly in `mail.emails` (not via a mailbox) | Test data; removed by the 30-day retention job |
| 21 | Live-event queue `api-gateway.live` is durable | If the gateway is down for long, notifications pile up in RabbitMQ | Use an exclusive auto-delete queue per replica (milestone 9) |
| 22 | Service tests run on SQLite, so Postgres-only paths (pgvector `<=>`) are untested in CI | A pgvector query bug reached the live stack in M6 (fixed) | Integration suite on real Postgres (milestone 9) |
| 23 | Real Gmail backlog after the stack was off since 21 Oct: ~550 runs queued at ~70 s each | Takes hours; synthetic tests had to be moved to the front of the queue | Dedicated support mailbox or sender allow-list (see #18) |
| 24 | deepseek-r1:8b fix choice can take ~4 min (reasoning hits the budget, then a retry without it) | T2 requests appear minutes after the ticket | Acceptable (background); consider thinking off for diagnostics |
| 22 | Breaker events are published directly (not via an outbox) | A transition while RabbitMQ is down is not reported (still visible in Redis/metrics/logs) | Acceptable: health is a live signal; the System health page reads Redis |
| 23 | Reporting has no event store | Read models built before a consumer existed need an outbox replay; a *new* read model inside an existing consumer cannot be backfilled that way (its inbox already holds the event ids) — probes avoid this by ops-service announcing their state at start-up | Add an event archive or rebuild command in milestone 9 |
