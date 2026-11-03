import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  approvalsApi, GATE_DECIDERS, GATES, waitingFor, type Evidence, type FixApprovalDetail, type LogFinding, type Probe,
} from "../../api/approvals";
import { ApiError } from "../../api/client";
import { useSession } from "../../auth/session";
import { Icon } from "../../components/Icon";
import { useLiveEvents, useNow } from "../../hooks/useLiveEvents";
import "./approvals.css";

const time = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit" });

function signature(name: string, order: string[], params: Record<string, string>): string {
  return `${name}(${order.filter((k) => k in params).map((k) => `${k}="${params[k]}"`).join(", ")})`;
}

/** The proposal's question with the approver's parameter values ("Restart upload-api on eu-west-1?"). */
function question(a: FixApprovalDetail, params: Record<string, string>): string {
  const { action } = a.proposal;
  let text = action.title ?? `Run ${action.signature}?`;
  for (const [k, v] of Object.entries(action.params)) text = text.replace(v, params[k] ?? v);
  return text;
}

function Probes({ probes, at, stale }: { probes: Probe[]; at: string; stale?: boolean }) {
  return (
    <div className="stack" style={{ gap: 0 }}>
      <div className="evidence-label">Health probes · {time.format(new Date(at))}{stale ? " · at proposal time, not current" : ""}</div>
      {probes.map((p) => (
        <div key={`${p.name}-${p.region ?? ""}`} className="probe-row">
          <span style={{ fontWeight: 500 }}>{p.name}</span>
          <span className="mono muted" style={{ fontSize: 12 }}>{p.detail}</span>
          <span className="row probe-state" style={{ color: p.ok ? "var(--good)" : "var(--crit)" }}>
            <Icon name={p.ok ? "check" : "close"} size={14} />{p.ok ? "Pass" : "Fail"}
          </span>
        </div>
      ))}
    </div>
  );
}

function Logs({ finding }: { finding: LogFinding }) {
  return (
    <div className="stack" style={{ gap: 6 }}>
      <div className="evidence-label">Log findings · {finding.service} · personal data masked</div>
      <div className="log-box">
        {finding.lines.join("\n")}
        {finding.similar > 0 ? `\n… ${finding.similar} similar lines${finding.since ? ` since ${finding.since}` : ""}` : ""}
      </div>
    </div>
  );
}

function EvidencePanel({ a, evidence, error, loading }: {
  a: FixApprovalDetail; evidence: Evidence | undefined; error: unknown; loading: boolean;
}) {
  const proposed = a.proposal.evidence;
  const unavailable = !!error;
  return (
    <section aria-label="Evidence" className="card card-pad stack op-enter op-d3" style={{ gap: 14 }}>
      <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline", gap: 8, flexWrap: "wrap" }}>
        <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>Evidence</h2>
        {loading && <span className="row muted" style={{ fontSize: 13 }}><span className="spinner" />Checking now…</span>}
        {evidence && <span className="badge badge-good">Fresh · checked {time.format(new Date(evidence.checked_at))}</span>}
      </div>
      {unavailable && (
        <div role="alert" className="alert alert-warn">
          <Icon name="alert" size={18} />
          <span>
            <strong>Evidence unavailable.</strong>{" "}
            {error instanceof ApiError && error.status === 503
              ? "ops-service cannot be reached right now, so the probes cannot be re-run."
              : (error as Error).message}{" "}
            A fix is never approved without fresh evidence; Approve is disabled until it is back.
          </span>
        </div>
      )}
      <Probes probes={evidence?.probes ?? proposed.probes} at={evidence?.checked_at ?? proposed.checked_at} stale={!evidence} />
      {(evidence?.logs ?? proposed.logs).map((f) => <Logs key={f.service} finding={f} />)}
      {proposed.deployment && <div className="muted" style={{ fontSize: 13 }}>{proposed.deployment}</div>}
      {proposed.notes.map((n) => <div key={n} className="note"><Icon name="audit" size={16} /><span>{n}</span></div>)}
    </section>
  );
}

function Outcome({ a }: { a: FixApprovalDetail }) {
  const key = a.proposal.jira_key ?? a.jira_key;
  if (a.status === "rejected") {
    return <span className="row" style={{ gap: 8, fontSize: 15, fontWeight: 500, color: "var(--ink-2)" }}>
      <Icon name="close" size={18} />Rejected. Nothing ran; {key ?? "the incident"} stays open with your note{a.reason ? `: ${a.reason}` : ""}.</span>;
  }
  if (a.outcome === "fix_failed") {
    return <span role="alert" className="row" style={{ gap: 8, fontSize: 15, fontWeight: 500, color: "var(--crit)" }}>
      <Icon name="alert" size={18} />Approved, but it did not run: {a.outcome_detail}</span>;
  }
  if (a.outcome === "fix_done") {
    return <span className="row" style={{ gap: 8, fontSize: 15, fontWeight: 500, color: "var(--good)" }}>
      <Icon name="check" size={18} />Approved and run. {a.outcome_detail}. Health re-checks the probes next.</span>;
  }
  return <span className="row" style={{ gap: 8, fontSize: 15, fontWeight: 500, color: "var(--ink-2)" }}>
    <span className="spinner" />Approved. Running now; Health re-checks the probes before anyone is told it is fixed.</span>;
}

export function ApproveFixPage() {
  const { id = "" } = useParams();
  const { user } = useSession();
  const qc = useQueryClient();
  useLiveEvents();
  const now = useNow();
  const query = useQuery({ queryKey: ["approval", id], queryFn: () => approvalsApi.getFix(id) });
  const pending = query.data?.status === "pending";
  const evidence = useQuery({
    queryKey: ["evidence", id],
    queryFn: () => approvalsApi.evidence(id),
    enabled: pending,
    refetchInterval: 30_000, // the screen must not age: probes are re-run while it is open
    retry: false,
  });
  const [editing, setEditing] = useState(false);
  const [params, setParams] = useState<Record<string, string> | null>(null); // null = as proposed
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const decide = useMutation({
    mutationFn: (decision: "approve" | "reject") => {
      const changed = params && query.data
        ? Object.fromEntries(Object.entries(params).filter(([k, v]) => query.data.proposal.action.params[k] !== v))
        : {};
      return approvalsApi.decide(id, decision, decision === "reject" ? reason : undefined,
        decision === "approve" && Object.keys(changed).length ? { params: changed } : {});
    },
    onSuccess: (a) => {
      qc.setQueryData(["approval", id], a);
      void qc.invalidateQueries({ queryKey: ["approvals"] });
      setRejecting(false); setEditing(false);
    },
  });

  if (query.isLoading) return <div className="row muted"><span className="spinner" />Loading…</div>;
  if (query.error || !query.data) {
    return <div role="alert" className="alert alert-error">{(query.error as Error | null)?.message ?? "Not found"}</div>;
  }
  const a = query.data;
  const p = a.proposal;
  const order = p.action.params_spec.map((s) => s.name);
  const chosen = { ...p.action.params, ...(a.status === "pending" ? params ?? {} : a.edits.params ?? {}) };
  const changed = Object.keys(chosen).some((k) => chosen[k] !== p.action.params[k]);
  const canDecide = !!user && GATE_DECIDERS.fix.some((r) => user.roles.includes(r));
  const fresh = !!evidence.data && !evidence.error;
  const disabledByOps = evidence.data ? !evidence.data.enabled : false;
  const decideError = decide.error instanceof ApiError ? decide.error.message : decide.error ? "Could not save the decision." : null;
  const key = p.jira_key ?? a.jira_key;
  const reminderNote = a.next_reminder_at
    ? a.reminders > 0
      ? `Reminder sent${a.reminders > 1 ? ` ${a.reminders}×` : ""} · next at ${time.format(new Date(a.next_reminder_at))}`
      : `Reminder goes out at ${Math.round((new Date(a.next_reminder_at).getTime() - new Date(a.requested_at).getTime()) / 60_000)} min`
    : null;

  return (
    <>
      <header className="op-enter row" style={{ justifyContent: "space-between", alignItems: "flex-end", gap: 16, flexWrap: "wrap" }}>
        <div className="stack" style={{ gap: 6 }}>
          <Link to="/approvals" style={{ fontSize: 14 }}>← Approvals</Link>
          <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
            <h1 style={{ margin: 0, fontSize: 26, fontWeight: 600 }}>{question(a, chosen)}</h1>
            <span className={`badge ${GATES.fix.badge}`}>Production change · {GATES.fix.tier}</span>
          </div>
          <div className="muted" style={{ fontSize: 14 }}>
            Proposed by the Diagnostics agent{key ? <> for <span className="mono">{key}</span></> : null}
            {pending ? <> · waiting {waitingFor(a.requested_at, now)}</> : null}
          </div>
        </div>
        {pending && reminderNote && (
          <div className="reminder-chip"><Icon name="clock" size={16} />{reminderNote}</div>
        )}
      </header>

      <div className="approve-grid">
        <div className="side">
          <section aria-label="Proposed fix" className="card card-pad stack op-enter op-d1" style={{ gap: 12 }}>
            <div className="row" style={{ justifyContent: "space-between", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
              <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>What will run</h2>
              <span className="badge badge-accent">AI proposal · you decide</span>
            </div>
            <div className="cmd-box">{signature(p.action.name, order, chosen)}</div>
            {editing && pending && (
              <div className="param-grid op-enter">
                {p.action.params_spec.map((spec) => (
                  <label key={spec.name} className="stack" style={{ gap: 4 }}>
                    <span style={{ fontSize: 13, color: "var(--ink-2)" }}>{spec.label}</span>
                    <select className="input" value={chosen[spec.name]}
                      onChange={(e) => setParams({ ...chosen, [spec.name]: e.target.value })}>
                      {spec.choices.map((c) => <option key={c}>{c}</option>)}
                    </select>
                  </label>
                ))}
                <span className="muted" style={{ fontSize: 12, gridColumn: "1 / -1" }}>
                  Only values from the runbook and the environment are offered. OpsPilot checks them again before running.
                </span>
              </div>
            )}
            {changed && <span className="muted" style={{ fontSize: 12 }}>You changed the AI's proposal ({p.action.signature}).</span>}
            <dl className="ticket-meta" style={{ gridTemplateColumns: "120px minmax(0, 1fr)" }}>
              <dt>Why</dt>
              <dd>Runbook <strong>{p.runbook.id} {p.runbook.title}, step {p.runbook.step}</strong>: {p.runbook.why}.</dd>
              <dt>Likely cause</dt>
              <dd>{p.diagnosis.likely_cause} <span className="muted" style={{ fontSize: 12 }}>({p.diagnosis.confidence} confidence)</span></dd>
              <dt>Expected effect</dt><dd>{p.action.effect}</dd>
              <dt>Rollback</dt><dd>{p.action.rollback}</dd>
              <dt>After running</dt><dd>{p.after}</dd>
            </dl>
            <div className="note"><Icon name="audit" size={16} /><span>{p.diagnosis.explanation} <span className="mono">· {p.model.model}{p.model.thinking ? " · thinking" : ""}</span></span></div>
          </section>

          <section aria-label="Who is affected" className="card card-pad op-enter op-d2">
            <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>Who is affected</h2>
            <div className="impact-grid">
              <div><div className="impact-value">{p.affected.sites.length}</div>
                <div className="muted" style={{ fontSize: 13 }}>sites{p.affected.sites.length ? `: ${p.affected.sites.map((s) => s.replace(/^SITE-/, "")).join(", ")}` : ""}</div></div>
              <div><div className="impact-value">{p.affected.reporters}</div>
                <div className="muted" style={{ fontSize: 13 }}>reporter{p.affected.reporters === 1 ? "" : "s"} linked to {key ?? "the incident"}</div></div>
              {Object.entries(p.affected.impact).map(([label, value]) => (
                <div key={label}><div className="impact-value">{value}</div><div className="muted" style={{ fontSize: 13 }}>{label}</div></div>
              ))}
            </div>
          </section>
        </div>

        <EvidencePanel a={a} evidence={pending ? evidence.data : undefined} error={pending ? evidence.error : null} loading={pending && evidence.isFetching && !evidence.data} />
      </div>

      <footer className="decision-bar op-enter op-d4" aria-label="Decision">
        {!pending ? (
          <>
            <Outcome a={a} />
            <Link to="/approvals" style={{ marginLeft: "auto", fontSize: 15, fontWeight: 500 }}>Next in queue →</Link>
          </>
        ) : !canDecide ? (
          <span className="muted">Your role can view this request but not decide it. Only a named support agent can approve a T2 change.</span>
        ) : rejecting ? (
          <>
            <label htmlFor="fix-reject-note" style={{ fontSize: 14, fontWeight: 500 }}>Reason</label>
            <input id="fix-reject-note" className="input grow" autoFocus value={reason} placeholder="e.g. Engineering is already on it"
              onChange={(e) => setReason(e.target.value)} onKeyDown={(e) => e.key === "Enter" && reason.trim() && decide.mutate("reject")} />
            <button type="button" className="btn btn-danger op-press" disabled={!reason.trim() || decide.isPending} onClick={() => decide.mutate("reject")}>Confirm reject</button>
            <button type="button" className="btn op-press" onClick={() => setRejecting(false)}>Cancel</button>
            {decideError && <span role="alert" className="field-error">{decideError}</span>}
          </>
        ) : (
          <>
            <button type="button" className="btn btn-primary op-press" disabled={!fresh || disabledByOps || decide.isPending}
              title={!fresh ? "Waiting for fresh evidence" : disabledByOps ? "This action is disabled in Admin › Integrations › Ops" : undefined}
              onClick={() => decide.mutate("approve")}>
              {decide.isPending ? "Saving…" : "Approve and run"}
            </button>
            <button type="button" className="btn op-press" onClick={() => setEditing(!editing)}>{editing ? "Done editing" : "Edit parameters"}</button>
            <button type="button" className="btn btn-danger op-press" onClick={() => setRejecting(true)}>Reject</button>
            <span className="muted" style={{ fontSize: 13 }}>
              {!fresh ? "Approve is enabled once fresh evidence is in." : disabledByOps ? "This action is switched off in Admin." :
                `Only a named support agent can approve a T2 change. Runs on the ${evidence.data?.executor ?? "configured"} executor.`}
            </span>
            {decideError && <span role="alert" className="field-error">{decideError}</span>}
          </>
        )}
      </footer>
    </>
  );
}
