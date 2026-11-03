import { api } from "./client";
import type { Role } from "./types";

export type GateId = "ticket" | "fix" | "message";
export type Severity = "sev1" | "sev2" | "sev3" | "sev4";

export interface ApprovalSummary {
  id: string;
  run_id: string;
  thread_id: string;
  gate: GateId;
  title: string;
  detail: string;
  site: string | null;
  severity: Severity | null;
  approver: string;
  requested_at: string;
  status: "pending" | "approved" | "rejected" | "sent_back";
  decided_at: string | null;
  decided_by_role: Role | null;
  reason: string | null;
  outcome: "ticket_pending" | "ticket_created" | "linked" | "ticket_failed" | "fix_pending" | "fix_done" | "fix_failed" | null;
  outcome_detail: string | null;
  /** T2 only: the health re-check after the fix ran. */
  health?: "resolved" | "fix_failed" | null;
  health_detail?: string | null;
  jira_key: string | null;
  reminders: number;
  next_reminder_at: string | null;
}

export interface QueueStats {
  waiting: number;
  waiting_by_gate: Record<GateId, number>;
  oldest_waiting_seconds: number | null;
  sev1_waiting: number;
}

export interface TokenisedAddress {
  email_token: string;
  name_token: string | null;
}

export interface TicketProposal {
  action: "create" | "link";
  ticket: { summary: string; description: string; severity: Severity; site: string | null; category: string };
  duplicate_of: { key: string; summary?: string; score: number; reason: string } | null;
  dedupe: {
    checked: boolean;
    searched: number;
    hours: number;
    threshold: number;
    best: { key: string; summary: string; score: number } | null;
    note: string | null;
  };
  intake: {
    category: string;
    severity: Severity;
    summary: string;
    site: string | null;
    asset: string | null;
    error: string | null;
    facts: { label: string; value: string }[];
    missing_details: string[];
  };
  email: {
    from: TokenisedAddress;
    subject: string;
    body: string;
    received_at: string;
    language: string;
    attachments: { filename: string; content_type: string; size: number }[];
  };
  privacy: { record_id: string; summary: string; signature_removed: boolean };
  jira: { configured: boolean; project: string | null; issue_type: string | null };
  model: { model: string; thinking: boolean };
  reporter_token: string | null;
}

export interface ApprovalDetail extends ApprovalSummary {
  proposal: TicketProposal;
  edits: Record<string, unknown>;
}

export interface Probe {
  name: string;
  ok: boolean;
  detail: string;
  latency_ms: number | null;
  services?: string[];
  region?: string | null;
}

export interface LogFinding {
  service: string;
  lines: string[];
  similar: number;
  since: string | null;
}

/** Gate T2: an allow-listed action proposed by Diagnostics (agent-orchestrator) from ops-service evidence. */
export interface FixProposal {
  action: {
    name: string;
    title: string | null;
    signature: string;
    params: Record<string, string>;
    params_spec: { name: string; label: string; choices: string[] }[];
    effect: string;
    rollback: string;
  };
  runbook: { id: string; title: string; step: number; why: string };
  diagnosis: { likely_cause: string; confidence: "low" | "medium" | "high"; explanation: string };
  after: string;
  evidence: {
    checked_at: string;
    probes: Probe[];
    logs: LogFinding[];
    deployment: string | null;
    runbooks: { id: string; title: string; heading: string; score: number; method: string }[];
    notes: string[];
  };
  affected: { sites: string[]; reporters: number; impact: Record<string, number> };
  region: string | null;
  site: string | null;
  jira_key: string | null;
  model: { model: string; thinking: boolean };
}

export interface FixApprovalDetail extends ApprovalSummary {
  proposal: FixProposal;
  edits: { params?: Record<string, string> };
}

/** Fresh evidence from ops-service for a pending fix (503 when ops-service is unavailable). */
export interface Evidence {
  checked_at: string;
  region: string | null;
  probes: Probe[];
  logs: LogFinding[];
  notes?: string[];
  executor: string;
  enabled: boolean;
  job: { status: "pending" | "done" | "failed"; ok: boolean | null; detail: string | null; attempts: number } | null;
}

export interface RunSummary {
  counts: Record<string, number>;
  ai_paused: boolean;
  ai_retry_after_seconds: number | null;
  in_progress: number;
}

export interface Reveal {
  token: string;
  values: { PERSON?: string; EMAIL?: string; PHONE?: string };
  expires_at: string;
  reason: string;
}

export const approvalsApi = {
  queue: (decided: boolean) =>
    api<{ items: ApprovalSummary[]; stats: QueueStats }>(`/approvals${decided ? "?decided=true" : ""}`),
  get: (id: string) => api<ApprovalDetail>(`/approvals/${id}`),
  getFix: (id: string) => api<FixApprovalDetail>(`/approvals/${id}`),
  evidence: (id: string) => api<Evidence>(`/fixes/${id}/evidence`),
  decide: (id: string, decision: "approve" | "reject", reason?: string, edits: Record<string, unknown> = {}) =>
    api<ApprovalDetail>(`/approvals/${id}/decision`, { method: "POST", body: { decision, reason, edits } }),
  runs: () => api<RunSummary>("/runs/summary"),
  reveal: (token: string, reason: string, threadId: string) =>
    api<Reveal>("/privacy/reveals", { method: "POST", body: { token, reason, thread_id: threadId } }),
};

/** Who may decide each gate (mirrors GATE_APPROVERS in opspilot_common.auth.roles). */
export const GATE_DECIDERS: Record<GateId, Role[]> = {
  ticket: ["l1_analyst", "support_agent", "support_manager"],
  fix: ["support_agent"],
  message: ["l1_analyst", "support_agent", "support_manager"],
};

export const GATES: Record<GateId, { label: string; tier: string; badge: string }> = {
  ticket: { label: "Create ticket", tier: "T1", badge: "badge-accent" },
  fix: { label: "Fix", tier: "T2", badge: "badge-warn" },
  message: { label: "Message", tier: "T3", badge: "badge-msg" },
};

export const SEVERITIES: { value: Severity; label: string }[] = [
  { value: "sev1", label: "sev1 · outage for many users or data loss" },
  { value: "sev2", label: "sev2 · core function broken for a site" },
  { value: "sev3", label: "sev3 · one user, or a workaround exists" },
  { value: "sev4", label: "sev4 · question or minor issue" },
];

export const SEVERITY_BADGE: Record<Severity, string> = {
  sev1: "badge-crit",
  sev2: "badge-warn",
  sev3: "badge-accent",
  sev4: "badge-neutral",
};

export function waitingFor(iso: string, now: number = Date.now()): string {
  const minutes = Math.max(0, Math.floor((now - new Date(iso).getTime()) / 60_000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return hours < 48 ? `${hours} h` : `${Math.floor(hours / 24)} days`;
}
