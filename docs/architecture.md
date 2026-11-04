# OpsPilot architecture

## Context
Build "OpsPilot" in an empty repo (`/Users/sanjayrana/Projects/AgenticOps`). AI agents read a mailbox, classify emails and propose actions. A person approves every action before anything reaches Jira, production or a customer. The design source is the wireframe canvas https://claude.ai/artifact/9MVMvgd7L4LeEs1yPFUbV7, which has 8 screens: Login, Approvals queue, Approve ticket (T1), Approve fix (T2), Approve message (T3), Dashboard, Audit log, and Privacy/GDPR. A new Admin screen holds the configurable mailbox and integrations.

User decisions:
- Build the **full wireframe**.
- The mailbox provider is **configurable by an admin** (IMAP, Gmail API or Microsoft Graph).
- Stack is **FastAPI + React**.
- The LLM is **deepseek-r1:8b on local Ollama**.
- Incidents go to **Jira Cloud**.
- The system is split into **microservices**.
- Every external and inter-service call is protected by the **circuit breaker** pattern.
- Code follows **SOLID**.
- A **CLAUDE.md** file holds the project instructions and memory.

## Architecture overview
```
                   ┌──────────── React SPA (Vite + TS) ────────────┐
                   └───────────────┬───────────────────────────────┘
                                   │ REST + SSE
                         ┌─────────▼─────────┐
                         │   api-gateway     │  BFF: authN check, routing, SSE fan-out, rate limit
                         └─┬───┬───┬───┬───┬─┘  (every downstream call through a circuit breaker)
     ┌─────────────────────┘   │   │   │   └──────────────────────┐
┌────▼─────┐ ┌──────────┐ ┌────▼───▼──┐ ┌───────────┐ ┌──────────▼──┐ ┌────────────┐
│ identity │ │ settings │ │ approval  │ │  audit    │ │  reporting  │ │  privacy   │
│ users,   │ │ admin    │ │ queue,    │ │ append-   │ │ dashboard   │ │ tokenise,  │
│ TOTP,    │ │ config,  │ │ decisions,│ │ only log  │ │ read model  │ │ vault, DSR,│
│ roles    │ │ secrets  │ │ RBAC      │ │           │ │ (CQRS)      │ │ retention  │
└──────────┘ └──────────┘ └─────┬─────┘ └─────▲─────┘ └──────▲──────┘ └─────▲──────┘
                                │ approval.decided  │ all events  │ all events     │ sync HTTP (CB)
                          ┌─────▼─────────────────────────────────────────────────┴───┐
   RabbitMQ (topic exch.) │              agent-orchestrator (LangGraph)               │
   + DLQ per queue        │ nodes: privacy_gate → intake → dedupe → T1 → diagnostics  │
                          │        → T2 → health_check → comms → T3 → send           │
                          │ ChatOllama(deepseek-r1:8b) behind CB · PostgresSaver      │
                          └───▲──────────────┬──────────────────┬─────────────────────┘
               email.received │              │ HTTP (CB)        │ HTTP (CB)
                  ┌───────────┴───┐   ┌──────▼────────┐   ┌─────▼────────┐
                  │ mail-service  │   │ jira-connector│   │ ops-service  │
                  │ IMAP/Gmail/   │   │ Jira Cloud v3 │   │ probes, logs,│
                  │ Graph adapters│   │ create/search/│   │ runbook RAG, │
                  │ poll + send   │   │ link/comment  │   │ actions(mock)│
                  └───────┬───────┘   └──────┬────────┘   └──────────────┘
                     (CB) │ providers   (CB) │ Jira Cloud          Ollama on host :11434 (CB)
```

### Services and ownership (each service owns its own Postgres schema and never reads another's)
| Service | Responsibility | Owns data | Publishes / consumes |
|---|---|---|---|
| `api-gateway` | BFF for the SPA: validates the session JWT, routes requests, aggregates data for pages, relays events to the browser over SSE | none | consumes `*.changed` → SSE |
| `identity-service` | Users, password hashes, TOTP MFA, roles, issuing JWTs, 30-min idle timeout | `identity` | `user.*` |
| `settings-service` | Admin config for mailbox, Jira, LLM and ops; Fernet-encrypted secrets; test-connection endpoints | `settings` | `settings.changed` |
| `mail-service` | Mailbox adapters, poller, outbound send, raw email storage (30-day retention) | `mail` | pub `email.received`; consumes `message.send.requested` |
| `privacy-service` | Pseudonymise (Presidio + regex), token vault, detokenise at send time, identity reveal (audited), DSR export/erasure, retention jobs | `privacy` | `privacy.*` |
| `agent-orchestrator` | LangGraph workflow; LLM calls; `interrupt()` at each gate | `orchestrator` (checkpoints, runs) | consumes `email.received`, `approval.decided`; pub `approval.requested`, `run.*` |
| `approval-service` | Approval queue, enforcing which role may decide which gate, decisions with reason, edits, reminders (15 min for T2), idempotent decisions | `approval` | consumes `approval.requested`; pub `approval.decided` |
| `jira-connector` | Jira Cloud REST v3: create issue, JQL search, link issues, comment, transition. Writes go through an outbox | `jira` (outbox, idempotency keys) | `jira.issue.created` |
| `ops-service` | Health probes, log search (masked), runbook retrieval (pgvector + Ollama embeddings), allow-listed actions (mock executor by default) | `ops` | `probe.status`, `action.executed` |
| `audit-service` | Append-only audit log (a trigger blocks UPDATE/DELETE), search, filters | `audit` | consumes **all** events |
| `reporting-service` | Read models for the dashboard: queue stats, tickets by stage, outage timelines, SLAs, breaker states | `reporting` | consumes all events |

**Communication rules.** Calls that need an answer right away (gateway→service, orchestrator→jira/ops/privacy) are synchronous HTTP, always through a circuit breaker. State changes travel as asynchronous events over RabbitMQ, using the **transactional outbox** pattern (a DB row is written in the same transaction, and a relay publishes it). Consumers are idempotent: each records the event ids it has processed. Poison messages go to a DLQ after 5 retries.

## Circuit breaker and resilience design (shared lib `libs/opspilot-common/resilience`)
- `ResilientClient`: one wrapper composing **timeout → retry (tenacity, exponential backoff with jitter, only for idempotent or transient errors) → circuit breaker (pybreaker, state stored in Redis so all replicas share it) → bulkhead (asyncio.Semaphore per dependency)**.
- Breaker per dependency, configured in YAML, for example: `fail_max=5`, `reset_timeout=30s`, with a half-open probe of 1 call. A breaker counts only 5xx responses, timeouts and connection errors as failures; 4xx responses do not trip it.
- Each breaker publishes its state (`breaker.state_changed`) to reporting so the dashboard can show it, exposes a Prometheus metric `circuit_state{dependency}`, and reports on `/health/ready`.
- **Fallbacks per dependency:**
  - **Ollama open**: the run is parked in state `waiting_llm`, and a scheduler resumes it once the breaker is half-open. The queue shows the badge "AI paused".
  - **Jira open**: the approval is still recorded. The create request sits in the jira-connector outbox and is replayed when the breaker closes, using an idempotency key per approval id so no duplicate issue is created. The UI shows "Approved · ticket creation pending".
  - **Mailbox provider open**: that poll cycle is skipped and the "Mailbox polled N min ago" warning turns amber. Outbound sends stay queued.
  - **ops-service open**: the fix gate shows "Evidence unavailable" and Approve is disabled. A fix is never run without fresh evidence.
  - **privacy-service open**: processing stops. Raw email never goes to the LLM, so this path fails closed.
  - **Downstream service open at the gateway**: the gateway returns a 503 with a structured error, and the SPA shows a non-blocking banner.
- Tests use a fault-injection fixture (toxiproxy in docker-compose, or a respx fake) to trip and recover each breaker.

## SOLID: how it is applied (enforced in CLAUDE.md and code review)
- **Hexagonal layout in every service**: `domain/` (entities, value objects, pure logic), `application/` (use cases), `ports/` (abstract interfaces), `adapters/` (inbound: FastAPI routers and event consumers; outbound: DB, HTTP, broker), and `bootstrap.py` (composition root). Dependencies point inward only; `import-linter` contracts enforce this in CI.
- **S (single responsibility)**: one use case per class (`CreateIncidentFromApproval`, `PollMailbox`), and routers contain no business logic.
- **O (open/closed)**: a new mailbox provider, LLM or ticket system is added by registering a new adapter (`MailboxAdapterRegistry.register("imap", ImapAdapter)`), with no edits to the use cases.
- **L (Liskov substitution)**: every adapter of a port must pass a shared contract test suite (`tests/contract/test_mailbox_port.py` runs against IMAP, Gmail, Graph and the fake).
- **I (interface segregation)**: narrow ports such as `MailReader` and `MailSender`; `IssueCreator`, `IssueSearcher` and `IssueLinker`; `Pseudonymiser` and `Detokeniser`.
- **D (dependency inversion)**: use cases depend on ports, and `bootstrap.py` wires the concrete adapters (via `dependency-injector` or plain FastAPI `Depends` factories). LangGraph nodes receive ports through `RunnableConfig["configurable"]`, never by importing clients.

## LangGraph workflow (inside agent-orchestrator)
State: `email` (tokenised), `intake`, `duplicate_of`, `jira_key`, `diagnosis`, `proposed_fix`, `fix_result`, `draft_message`, `revision`, `decisions[]`. The checkpointer is `PostgresSaver`, with `thread_id` set to the mail thread id.
1. `privacy_gate` calls privacy-service to tokenise. **Fail closed.**
2. `intake`: deepseek fills a typed schema via `with_structured_output(IntakeResult, method="json_schema")`, temperature 0, with the `<think>` reasoning kept separate and stored in the trace.
3. `dedupe` runs a Jira JQL search over the last 24 h plus embedding similarity (threshold 0.80). A match links the email to the existing issue.
4. (Changed by [ADR-0007](adr/0007-comms-and-message-gate.md): a follow-up question for missing details is drafted at the end of the run, not here, so T3 never holds up a ticket.)
5. `gate_ticket` (T1) calls `interrupt()`, which publishes `approval.requested`. Approve calls jira-connector to create the issue; reject ends the run.
6. `diagnostics` calls ops-service for probes, logs and runbooks. It proposes a fix only from the runbook allow-list.
7. `gate_fix` (T2) calls `interrupt()`. Approve executes the action, then `health_check` re-runs the probes. Reject goes to comms with the note (as an internal hint, not for quoting).
8. `comms` drafts the message and runs automated checks: facts match the ticket, no ETAs, no other personal data, recipients match the linked reporters, AI disclosure present.
9. `gate_message` (T3) calls `interrupt()`. Approve publishes `message.send.requested`, mail-service detokenises through privacy-service and sends the emails. Send-back allows at most 2 revisions.

On `approval.decided`, the orchestrator calls `graph.ainvoke(Command(resume=decision), {"configurable": {"thread_id": ...}})`.

The design favours deterministic routing plus structured output over free-form tool choice, because an 8B reasoning model is unreliable at picking tools. Diagnostics can become a `create_react_agent` subgraph later if testing shows tool calling is reliable.

## Repo layout (monorepo)
```
CLAUDE.md                          project instructions + memory (see below)
docs/adr/0001-microservices.md     0002-circuit-breaker.md, 0003-event-bus-rabbitmq.md, 0004-local-llm.md
docs/architecture.md               diagram above, event catalogue, service contracts
libs/opspilot-common/              resilience/, events/ (pydantic event schemas + outbox relay),
                                   auth/ (JWT verify), logging/ (structlog + OTel), testing/ (contract base)
services/<name>/                   app/{domain,application,ports,adapters,bootstrap.py,main.py}
                                   tests/{unit,contract,integration}, Dockerfile, pyproject.toml, alembic/
frontend/                          src/{theme/tokens.css (from wireframe), components/, pages/, api/}
deploy/docker-compose.yml          postgres, redis, rabbitmq, toxiproxy, jaeger, all services, frontend
                                   (Ollama on host via host.docker.internal:11434)
Makefile                           up, down, test, lint, contract, e2e
```

## CLAUDE.md (created in milestone 0). Outline of its contents:
1. **Project summary**: what OpsPilot does, the wireframe link, the screen list.
2. **Architecture memory**: the services table, the event catalogue, ports per service, breaker config, and links to the ADRs.
3. **Non-negotiable rules**:
   - Hexagonal layout, with no imports from `adapters` inside `domain` or `application`.
   - SOLID checklist per pull request.
   - Every outbound call goes through `ResilientClient`.
   - Every state change goes through the outbox and is audited.
   - The LLM sees only tokenised text.
   - No action runs without an approval event.
   - Services never share database schemas.
4. **Conventions**: Python 3.12, uv, ruff + mypy --strict, pytest, pydantic v2, async SQLAlchemy 2; TS strict, ESLint, TanStack Query; commit and branch naming.
5. **Commands**: `make up`, `make test`, how to run one service, how to seed fixtures, how to check Ollama.
6. **How to add things**: a mailbox provider, a gate, an event, a service (step-by-step recipes).
7. **Project memory log**: a dated "Decisions & status" section updated at the end of every milestone (what was built, open issues, next step), plus a "Gotchas" list (for example deepseek `<think>` handling, Jira ADF body format).

## Admin screen (new; Admin role)
- **Mailbox**: provider `IMAP | Gmail API | Microsoft Graph`, with dynamic fields for each, folder, poll interval and a "Test connection" button. Changes take effect without a restart: settings-service publishes `settings.changed` and mail-service rebuilds its adapter from the registry.
- **Jira**: URL, email, API token, project key, issue type, field mapping, test button.
- **LLM**: Ollama URL, model `deepseek-r1:8b`, thinking toggle, test prompt.
- **Ops**: probe targets, runbook folder, enabled actions.
- **Users and roles**: the five roles below.
- **Circuit breaker thresholds**: view only.

## Screens
These follow the wireframe tokens and layout: IBM Plex, light/dark/auto themes, sidebar shell.
- **Login + TOTP**
- **Approvals**: stat cards, gate tabs, table with Gate / What / Site / Severity / Waiting / Approver; live via SSE.
- **ApproveTicket**: the email, the "Show identity" reveal (5 min, audited), intake fields, ticket preview, dedupe result.
- **ApproveFix**: the command with editable parameters, evidence, probes, masked logs; T2 restricted to the Support agent role.
- **ApproveMessage**: the draft (editable), recipient tokens, draft checks, send-back.
- **Dashboard**: platform health, open tickets by stage, outage timeline, SLAs, plus a dependency-health panel showing breaker states.
- **Audit**: tabs All / People / Agents and tools / Privacy, plus search.
- **Privacy**: the DSR list, the stores holding a person's data, export, erasure, retention schedule (compliance lead only; design in [ADR-0008](adr/0008-data-requests-and-retention.md)). Privacy notice, imprint and accessibility statement are public pages at `/legal/*`.
- **Admin**

Roles: L1 Analyst, Support agent, Support manager, Compliance lead, Admin.

## Build order
0. **Foundations**: monorepo, CLAUDE.md, ADRs, `opspilot-common` (resilience, events/outbox, auth, logging), docker-compose infra, CI with lint, type checks, import-linter and tests.
1. **identity, settings and api-gateway**, plus the React shell with theme tokens, Login/TOTP and the Admin screen.
2. **mail-service** with the IMAP adapter and contract tests; then the Gmail and Graph adapters.
3. **privacy-service** (tokenise, vault) and **audit-service**.
4. **agent-orchestrator** (privacy_gate → intake → dedupe → T1), **approval-service** and **jira-connector** with its outbox, plus the Approvals and ApproveTicket UI. **MVP checkpoint: email → Jira works end-to-end.**
5. **reporting-service** and the Dashboard (including breaker states); the Audit UI.
6. **ops-service**, the diagnostics node, T2, the ApproveFix UI and the reminder job.
7. comms, T3, the ApproveMessage UI and the outbound send path.
8. Privacy/GDPR screens: DSR workflows and the retention jobs.
9. **Hardening**: chaos tests through toxiproxy for each breaker, OTel tracing end to end, load test of the poller.

After each milestone, update the CLAUDE.md memory log.

## Key libraries
**Backend:**
- LangChain and LangGraph: `langgraph`, `langgraph-checkpoint-postgres`, `langchain-core`, `langchain-ollama`
- API and data: `fastapi`, `pydantic-settings`, `sqlalchemy[asyncio]`, `asyncpg`, `alembic`
- Messaging and resilience: `aio-pika` (RabbitMQ), `pybreaker`, `tenacity`, `redis`, `httpx`
- Mailbox and Jira: `imapclient`, `aiosmtplib`, `google-api-python-client`, `msal`, `atlassian-python-api` or raw httpx
- Privacy and auth: `presidio-analyzer`, `cryptography`, `pyotp`, `passlib[argon2]`
- Retrieval: `pgvector`
- Server, observability, architecture checks: `apscheduler`, `sse-starlette`, `structlog`, `opentelemetry-*`, `prometheus-client`, `dependency-injector`, `import-linter`

**Frontend:** react, vite, typescript, react-router, @tanstack/react-query.

## Verification
- **Unit tests** per service, using fakes for ports. Graph nodes are tested with `FakeListChatModel`.
- **Contract tests**: every adapter of a port passes the shared suite (Liskov substitution). Event schemas are versioned and checked for compatibility.
- **Graph tests**: with a MemorySaver, the graph stops at each `interrupt`, approve and reject take the right branches, and a duplicate `approval.decided` is idempotent.
- **Circuit breaker tests**: cut Ollama, Jira and mail through toxiproxy. Assert that the breaker opens after N failures, the fallback behaviour matches the list above, it goes half-open and then closes after recovery, there are no duplicate Jira issues (idempotency key), and the dashboard shows the state.
- **Architecture tests**: import-linter enforces the layer boundaries and that no service imports another service's code.
- **End-to-end**: `make up`. In Admin, configure IMAP (Gmail app password), Jira Cloud (sandbox project) and Ollama. Send a test email; it appears in Approvals. Approve it; the KAN-n issue appears in Jira; the audit log shows pseudonymise → classify → approve_ticket → create_jira_issue. Then exercise T2 with the mock executor and T3 against the GreenMail dev mailbox with synthetic mail (never send test mail to a real inbox).
- **UI check**: compare each page with the wireframe artboards in light and dark themes.
