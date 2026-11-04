/** Synthetic API responses for component tests and the accessibility audit. No real people. */
import type { ApprovalDetail, ApprovalSummary, Evidence, FixApprovalDetail, MessageApprovalDetail, QueueStats, RunSummary } from "../api/approvals";
import type { MailboxStatus } from "../api/mail";
import type { DataRequest, RequestDetail, RequestList } from "../api/privacy";
import type { AuditEntry, DashboardData } from "../api/reporting";
import type { Circuit, SectionView } from "../api/settings";
import type { Role, User } from "../api/types";

const NOW = "2026-11-04T09:00:00Z";

export function user(...roles: Role[]): User {
  return {
    id: "u-1", email: "lena@example.test", name: "Lena Test", roles, active: true, mfa_enrolled: true,
    password_change_required: false, locked: false, created_at: NOW, last_login_at: NOW,
  } as User;
}

const summary: ApprovalSummary = {
  id: "ap-ticket", run_id: "run-1", thread_id: "thr-1", gate: "ticket", title: "Uploads fail at site Berlin",
  detail: "Report from [PERSON_1]", site: "Berlin", severity: "sev2", approver: "l1_analyst", requested_at: NOW,
  status: "pending", decided_at: null, decided_by_role: null, reason: null, outcome: null, outcome_detail: null,
  jira_key: null, reminders: 0, next_reminder_at: null,
};

export const queue: { items: ApprovalSummary[]; stats: QueueStats } = {
  items: [
    summary,
    { ...summary, id: "ap-fix", gate: "fix", title: "Restart upload-worker", severity: "sev1", approver: "support_agent", jira_key: "KAN-1" },
    { ...summary, id: "ap-message", gate: "message", title: "Resolution for KAN-1", jira_key: "KAN-1" },
  ],
  stats: { waiting: 3, waiting_by_gate: { ticket: 1, fix: 1, message: 1 }, oldest_waiting_seconds: 300, sev1_waiting: 1 },
};

export const ticket: ApprovalDetail = {
  ...summary,
  edits: {},
  proposal: {
    action: "create",
    ticket: { summary: "Uploads fail at site Berlin", description: "Uploads fail with error 500.", severity: "sev2", site: "Berlin", category: "outage" },
    duplicate_of: null,
    dedupe: { checked: true, searched: 3, hours: 48, threshold: 0.8, best: null, note: null },
    intake: {
      category: "outage", severity: "sev2", summary: "Uploads fail", site: "Berlin", asset: null, error: "500",
      facts: [{ label: "Error", value: "500" }], missing_details: [],
    },
    email: {
      from: { email_token: "[EMAIL_1]", name_token: "[PERSON_1]" }, subject: "Uploads fail", body: "Hello, uploads fail at Berlin.",
      received_at: NOW, language: "en", attachments: [],
    },
    privacy: { record_id: "rec-1", summary: "1 name, 1 address replaced", signature_removed: true },
    jira: { configured: true, project: "KAN", issue_type: "Incident" },
    model: { model: "deepseek-r1:8b", thinking: true },
    reporter_token: "[PERSON_1]",
  },
};

export const fix: FixApprovalDetail = {
  ...summary, id: "ap-fix", gate: "fix", title: "Restart upload-worker", severity: "sev1", approver: "support_agent", jira_key: "KAN-1",
  edits: {},
  proposal: {
    action: {
      name: "restart_service", title: "Restart a service", signature: "restart_service(upload-worker, eu-west-1)",
      params: { service: "upload-worker", region: "eu-west-1" },
      params_spec: [{ name: "service", label: "Service", choices: ["upload-worker", "upload-api"] }, { name: "region", label: "Region", choices: ["eu-west-1"] }],
      effect: "Workers restart in about a minute.", rollback: "None needed.",
    },
    runbook: { id: "uploads", title: "Uploads failing", step: 2, why: "Worker queue is stuck." },
    diagnosis: { likely_cause: "Stuck worker", confidence: "high", explanation: "Queue depth is rising and workers are idle." },
    after: "Probes run again after the fix.",
    evidence: {
      checked_at: NOW,
      probes: [{ name: "upload", ok: false, detail: "HTTP 503", latency_ms: 120, region: "eu-west-1" }],
      logs: [{ service: "upload-worker", lines: ["ERROR queue stalled"], similar: 4, since: NOW }],
      deployment: null,
      runbooks: [{ id: "uploads", title: "Uploads failing", heading: "Restart", score: 0.9, method: "embedding" }],
      notes: [],
    },
    affected: { sites: ["Berlin"], reporters: 2, impact: { users: 120 } },
    region: "eu-west-1", site: "Berlin", jira_key: "KAN-1", model: { model: "deepseek-r1:8b", thinking: true },
  },
};

export const evidence: Evidence = {
  checked_at: NOW, region: "eu-west-1",
  probes: [{ name: "upload", ok: false, detail: "HTTP 503", latency_ms: 120, region: "eu-west-1" }],
  logs: [{ service: "upload-worker", lines: ["ERROR queue stalled"], similar: 4, since: NOW }],
  notes: [], executor: "mock", enabled: true, job: null,
};

export const message: MessageApprovalDetail = {
  ...summary, id: "ap-message", gate: "message", title: "Resolution for KAN-1", jira_key: "KAN-1",
  edits: {},
  proposal: {
    kind: "resolution", kind_label: "Resolution", subject: "Re: Uploads fail",
    body: "Hello [RECIPIENT_NAME],\n\nUploads work again.\n\nThis reply was prepared with AI assistance and checked by a member of our support team. How we handle your data: see our privacy notice.",
    recipients: [{ email_token: "[EMAIL_1]", name_token: "[PERSON_1]", email_id: "e-1", site: "Berlin", how: "reporter" }],
    checks: [
      { id: "facts", label: "Facts match the ticket", ok: true, detail: "every reference is in KAN-1", blocking: true },
      { id: "promises", label: "No promises or ETAs", ok: false, detail: "remove: soon", blocking: true },
    ],
    revision: 0, max_revisions: 2, notes: [], written_by: "model", jira_key: "KAN-1", model: { model: "deepseek-r1:8b", thinking: true },
  },
};

export const runs: RunSummary = { counts: { queued: 2 }, ai_paused: false, ai_retry_after_seconds: null, in_progress: 1 };

export const mail: MailboxStatus = {
  level: "ok", message: "Mailbox polled 1 min ago", configured: true, provider: "imap", folder: "INBOX", poll_interval_seconds: 60,
  last_poll_at: NOW, last_success_at: NOW, last_error: null, circuit: "closed", total_received: 12,
};

export const dashboard: DashboardData = {
  generated_at: NOW, window_hours: 24,
  dependencies: [{ name: "jira", label: "Jira", health: "up", since: NOW, callers: { "jira-connector": "closed" } }],
  probes: [{ name: "upload", region: "eu-west-1", health: "down", detail: "HTTP 503", latency_ms: 120, since: NOW }],
  stages: [{ id: "intake", label: "Intake", count: 2, note: "Reading" }],
  service_levels: [{ id: "ack", label: "First reply", value: "12 min", detail: "Target 1 h", breached: false }],
  incidents: [{ key: "KAN-1", title: "Uploads fail", severity: "sev1", site: "Berlin", reports: 2, first_seen: NOW, status: "open" }],
  timeline: { key: "KAN-1", entries: [{ at: NOW, actor: "Support agent", text: "Approved restart", system: false }] },
};

export const audit: { entries: AuditEntry[]; next_cursor: string | null } = {
  entries: [
    { id: "a-1", time: NOW, kind: "human", actor: "L1 Analyst", event: "Approved ticket", detail: "KAN-1", ticket: "KAN-1", privacy: "tokens only", thread_id: "thr-1" },
    { id: "a-2", time: NOW, kind: "privacy", actor: "privacy-service", event: "Pseudonymised email", detail: "2 values", ticket: null, privacy: "tokens only", thread_id: "thr-1" },
  ],
  next_cursor: null,
};

const dsr: DataRequest = {
  id: "dsr-1", type: "access", type_label: "Access", article: "Art. 15", status: "open", who: "[EMAIL_1]",
  received_on: "2026-11-01", due_on: "2026-12-01", due: "27 days left", due_level: "ok", verified: false, verified_how: null,
  pending_stores: [], outcome: null, closed_at: null,
};

export const requests: RequestList = {
  requests: [{ ...dsr }], open: 1,
  retention: [{ what: "Raw emails", keep: "30 days" }, { what: "Audit log", keep: "Kept" }],
};

export const request: RequestDetail = {
  ...dsr,
  stores: [{ store: "mail", label: "Emails", what: "Raw copies", count: 2, erase_action: "Deleted", available: true }],
};

export const users: User[] = [user("admin"), { ...user("l1_analyst"), id: "u-2", email: "max@example.test", name: "Max Test" }];

export const settings: SectionView[] = [{
  section: {
    id: "llm", title: "Language model", description: "Local model",
    fields: [
      { key: "url", label: "Address", kind: "url", required: true, default: "", help: "Where Ollama runs", placeholder: "http://", minimum: null, maximum: null, options: [], when: {} },
      { key: "key", label: "API key", kind: "secret", required: false, default: "", help: "", placeholder: "", minimum: null, maximum: null, options: [], when: {} },
      { key: "mode", label: "Mode", kind: "select", required: true, default: "a", help: "", placeholder: "", minimum: null, maximum: null, options: [{ value: "a", label: "A" }], when: {} },
      { key: "on", label: "Enabled", kind: "bool", required: false, default: true, help: "", placeholder: "", minimum: null, maximum: null, options: [], when: {} },
    ],
  },
  version: 1, values: { url: "http://host:11434", mode: "a", on: true }, secrets_set: { key: true }, updated_at: NOW, updated_by: "Admin",
}];

export const circuits: Circuit[] = [
  { dependency: "jira", state: "closed", failures: 0, retry_after_seconds: null, fail_max: 5, reset_timeout_seconds: 60, timeout_seconds: 15, retry_attempts: 3, bulkhead: 20 },
  { dependency: "ollama", state: "open", failures: 3, retry_after_seconds: 40, fail_max: 3, reset_timeout_seconds: 60, timeout_seconds: 180, retry_attempts: 2, bulkhead: 2 },
];

/** Response for a GET under /api. Undefined: no fixture, the caller decides. */
export function respond(path: string, me: User): unknown {
  const p = path.split("?")[0];
  if (p === "/auth/me") return me;
  if (p === "/approvals") return queue;
  if (p === "/approvals/ap-ticket") return ticket;
  if (p === "/approvals/ap-fix") return fix;
  if (p === "/approvals/ap-message") return message;
  if (p === "/fixes/ap-fix/evidence") return evidence;
  if (p === "/runs/summary") return runs;
  if (p === "/mail/status") return mail;
  if (p === "/dashboard") return dashboard;
  if (p?.startsWith("/incidents/")) return { key: "KAN-1", entries: dashboard.timeline.entries };
  if (p === "/audit/entries") return audit;
  if (p === "/privacy/requests") return requests;
  if (p === "/privacy/requests/dsr-1") return request;
  if (p === "/admin/users") return users;
  if (p === "/admin/settings") return settings;
  if (p === "/admin/system/circuits") return circuits;
  if (p === "/roles") return [];
  return undefined;
}
