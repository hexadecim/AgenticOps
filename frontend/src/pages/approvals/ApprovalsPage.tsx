import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { approvalsApi, GATES, SEVERITY_BADGE, waitingFor, type ApprovalSummary, type GateId } from "../../api/approvals";
import { mailApi } from "../../api/mail";
import { ROLE_LABELS } from "../../api/types";
import { Icon } from "../../components/Icon";
import { useLiveEvents, useNow } from "../../hooks/useLiveEvents";
import "./approvals.css";

type Tab = "all" | GateId | "decided";

function outcomeText(a: ApprovalSummary): string {
  if (a.status === "rejected") return `Rejected${a.reason ? `: ${a.reason}` : ""}`;
  switch (a.outcome) {
    case "ticket_created": return `Approved · ${a.jira_key} created`;
    case "linked": return `Approved · linked to ${a.jira_key}`;
    case "ticket_failed": return `Approved · Jira refused: ${a.outcome_detail ?? ""}`;
    case "ticket_pending": return "Approved · ticket creation pending";
    case "fix_pending": return "Approved · fix running";
    case "fix_done": return `Approved · fix ran${a.jira_key ? ` for ${a.jira_key}` : ""}`;
    case "fix_failed": return `Approved · not run: ${a.outcome_detail ?? ""}`;
    default: return a.status === "approved" ? "Approved" : a.detail;
  }
}

export function ApprovalsPage() {
  const [tab, setTab] = useState<Tab>("all");
  const live = useLiveEvents();
  const now = useNow();
  const pending = useQuery({ queryKey: ["approvals", "pending"], queryFn: () => approvalsApi.queue(false), refetchInterval: 60_000 });
  const decided = useQuery({ queryKey: ["approvals", "decided"], queryFn: () => approvalsApi.queue(true), enabled: tab === "decided" });
  const runs = useQuery({ queryKey: ["runs-summary"], queryFn: approvalsApi.runs, refetchInterval: 60_000, retry: false });
  const mail = useQuery({ queryKey: ["mail-status"], queryFn: mailApi.status, refetchInterval: 30_000, retry: false });

  const stats = pending.data?.stats;
  const items = pending.data?.items ?? [];
  const rows = tab === "decided" ? decided.data?.items ?? [] : tab === "all" ? items : items.filter((a) => a.gate === tab);
  const byGate = stats?.waiting_by_gate;
  const tabs: [Tab, string, number | undefined][] = [
    ["all", "All", stats?.waiting], ["ticket", "Create ticket", byGate?.ticket], ["fix", "Fix", byGate?.fix],
    ["message", "Message", byGate?.message], ["decided", "Decided", undefined],
  ];
  const paused = runs.data?.ai_paused;
  const reading = runs.data?.in_progress ?? 0;

  return (
    <>
      <header className="page-header op-enter">
        <div>
          <h1>Approvals</h1>
          <p>Nothing leaves OpsPilot or touches production until someone on this list approves it.</p>
        </div>
        <div className="row muted" style={{ fontSize: 13 }}>
          <Icon name="refresh" size={16} />
          {mail.data ? mail.data.message : "Mailbox status unavailable"}
          {live ? " · live" : ""}
        </div>
      </header>

      {pending.error && <div role="alert" className="alert alert-error">{(pending.error as Error).message}</div>}
      {paused && (
        <div role="status" className="alert alert-warn op-enter">
          <Icon name="alert" size={18} />
          AI paused: the local model is not answering, so {runs.data?.counts.waiting_llm ?? 0} email(s) wait for triage.
          They continue automatically{runs.data?.ai_retry_after_seconds ? ` (next try in ${runs.data.ai_retry_after_seconds} s)` : ""}.
        </div>
      )}

      <section aria-label="Summary" className="stat-grid op-enter op-d1">
        <div className="stat">
          <div className="stat-label">Waiting for approval</div>
          <div className="stat-value">{stats?.waiting ?? "–"}</div>
          <div className="stat-foot">{byGate ? `${byGate.ticket} tickets · ${byGate.fix} fixes · ${byGate.message} messages` : " "}</div>
        </div>
        <div className="stat">
          <div className="stat-label">Oldest waiting</div>
          <div className="stat-value">{stats?.oldest_waiting_seconds != null ? waitingFor(new Date(now - stats.oldest_waiting_seconds * 1000).toISOString(), now) : "–"}</div>
          <div className="stat-foot">Since the request reached the queue</div>
        </div>
        <div className="stat">
          <div className="stat-label">AI triage</div>
          <div className="stat-value">{paused ? "Paused" : reading}</div>
          <div className={`stat-foot ${paused ? "warn" : "good"}`}>
            <Icon name={paused ? "alert" : "check"} size={14} />
            {paused ? "Local model unavailable" : reading ? `email${reading === 1 ? "" : "s"} being read by deepseek-r1:8b` : "Nothing waiting for the model"}
          </div>
        </div>
        <div className="stat">
          <div className="stat-label">Sev-1 waiting</div>
          <div className="stat-value">{stats?.sev1_waiting ?? "–"}</div>
          <div className={`stat-foot ${stats?.sev1_waiting ? "warn" : "good"}`}>
            <Icon name={stats?.sev1_waiting ? "alert" : "check"} size={14} />
            {stats?.sev1_waiting ? "Decide these first" : "No sev-1 in the queue"}
          </div>
        </div>
      </section>

      <section aria-label="Queue" className="queue op-enter op-d2">
        <div role="tablist" aria-label="Filter by gate" className="queue-tabs">
          {tabs.map(([id, label, count]) => (
            <button key={id} type="button" role="tab" aria-selected={tab === id} className="pill-tab op-press" onClick={() => setTab(id)}>
              {label}{count !== undefined ? ` · ${count}` : ""}
            </button>
          ))}
        </div>
        <div className="queue-head" aria-hidden="true">
          <span>Gate</span><span>What needs deciding</span><span>Site</span><span>Severity</span>
          <span>{tab === "decided" ? "Decided" : "Waiting"}</span><span>{tab === "decided" ? "By" : "Approver"}</span>
        </div>
        {pending.isLoading && <div className="queue-empty"><span className="spinner" /> Loading…</div>}
        {!pending.isLoading && rows.length === 0 && (
          <div className="queue-empty">{tab === "decided" ? "Nothing decided yet." : "Nothing is waiting for you. New support emails appear here after triage."}</div>
        )}
        {rows.map((a) => (
          <Link key={a.id} to={`/approvals/${a.id}`} className="queue-row">
            <span className="row" style={{ gap: 8 }}>
              <span className={`badge ${GATES[a.gate].badge}`}>{GATES[a.gate].label}</span>
              <span className="tier">{GATES[a.gate].tier}</span>
            </span>
            <span className="queue-title">
              <strong>{a.title}</strong>
              <span>{tab === "decided" ? outcomeText(a) : a.detail}</span>
            </span>
            <span className="mono" style={{ fontSize: 13 }}>{a.site ?? "–"}</span>
            <span>{a.severity ? <span className={`badge mono ${SEVERITY_BADGE[a.severity]}`}>{a.severity}</span> : "–"}</span>
            <span style={{ fontSize: 14 }}>
              {waitingFor(tab === "decided" ? a.decided_at ?? a.requested_at : a.requested_at, now)}
              {tab !== "decided" && a.reminders > 0 && (
                <span className="badge badge-warn" style={{ marginLeft: 6 }} title={`${a.reminders} reminder(s) sent`}>reminded</span>
              )}
            </span>
            <span style={{ fontSize: 14, color: "var(--ink-2)" }}>{tab === "decided" ? (a.decided_by_role ? ROLE_LABELS[a.decided_by_role] : "–") : a.approver}</span>
          </Link>
        ))}
      </section>
    </>
  );
}
