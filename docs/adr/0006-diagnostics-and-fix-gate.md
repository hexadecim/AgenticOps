# ADR-0006: Diagnostics, allow-listed actions and the production-fix gate (T2)

- Status: Accepted · 2026-11-03

## Context
After a ticket exists, OpsPilot should look at the production evidence (health probes, logs, runbooks) and
propose a fix. A wrong production change does far more harm than a wrong ticket, the local model is an 8B
reasoning model that is unreliable at free tool use, and the wireframe requires that only a named Support
agent approves, can adjust parameters, sees fresh evidence, and that "Evidence unavailable" disables Approve.

## Decision
- **ops-service owns evidence and execution** (schema `ops`, port 8010). Ports are narrow: `EnvironmentSource`,
  `ProbeRunner`, `LogSource`, `RunbookSource`, `RunbookIndex`, `Embedder`, `ActionExecutor`. The default
  adapter set is the **mock environment** (`deploy/ops/environment.yaml`): simulated regions, sites, services
  and probes, and a mock executor that changes only the simulation. Real probes can be added as
  `Name=URL` targets in Admin › Integrations › Ops; real executors are new adapters + a registry entry.
- **The allow-list is code** (`ops_service/domain/actions.py`: `restart_service(service, region)`,
  `retry_failed_uploads(site)`). An action becomes proposable only when (1) it is in the allow-list, (2) a
  runbook step names it in its front matter, and (3) the admin enabled it. Parameter choices come from the
  environment ∩ the runbook's services. The same validation runs again when the job executes.
- **Logs are masked in ops-service** before anyone sees them (e-mail, `user=`/`phone=`-style pairs, phone
  numbers, IPs, bearer tokens, `password=`/`token=`, IBAN). Personal data never reaches the model or the UI.
- **Runbook RAG** = one chunk per Markdown section, embedded with `nomic-embed-text` into pgvector
  (`ops.runbook_chunks`, cosine distance); the index is rebuilt when the corpus fingerprint (text + model)
  changes. Embeddings down ⇒ keyword ranking, flagged in the evidence notes.
- **The model only chooses.** The `diagnostics` node sends tokenised ticket facts, probes, masked logs, runbook
  excerpts and the numbered candidates to deepseek with a JSON schema whose `choice` is an enum of the offered
  numbers plus `none`. It never writes an action or a parameter. No candidates ⇒ the model is not asked.
- **Graph**: `gate_ticket → await_ticket → diagnostics → request_fix_approval → gate_fix → await_fix →
  health_check`. Waiting for another service's result is an `interrupt()` (`await_*`); results that arrive
  before the run parks are kept on the run (`runs.results`) and resume it immediately.
- **Execution follows the event, never HTTP.** ops-service stores the proposal from `approval.requested`
  (gate fix) and creates a job (id = approval id) only on `approval.decided` with `decision=approve`; the
  approver's edited parameters must be among the offered choices (approval-service checks, ops-service
  re-checks). Before running, the job re-runs the probes; no fresh evidence ⇒ not run. The mock executor is
  idempotent per approval id. Result: `action.executed` (with run, thread and Jira key).
- **Health decides "resolved".** After the action, `health_check` re-runs the probes; only a pass makes the run
  `resolved`; otherwise `fix_failed` ("escalate to on-call").
- **Reminders**: approval-service publishes `approval.reminder` every 15 minutes while a T2 gate waits.
- `probe.status` is published only when a probe changes state, not on every check.

## Consequences
- A fix can never be proposed outside the runbooks and never run without an approval event, fresh probes and
  re-validated parameters, even if an event were forged with other parameters.
- The approval screen re-runs the probes every 30 s while it is open; with ops-service down it falls back to the
  proposal-time evidence, clearly marked as not current, and disables Approve.
- Reject ends the run with the reason today; from milestone 7 the run continues to Comms with the note.
