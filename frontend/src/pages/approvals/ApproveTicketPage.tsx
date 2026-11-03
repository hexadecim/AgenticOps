import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import {
  approvalsApi, GATE_DECIDERS, GATES, SEVERITIES, SEVERITY_BADGE, waitingFor, type ApprovalDetail, type Reveal,
  type Severity,
} from "../../api/approvals";
import { ApiError } from "../../api/client";
import { useSession } from "../../auth/session";
import { Icon } from "../../components/Icon";
import { useLiveEvents, useNow } from "../../hooks/useLiveEvents";
import "./approvals.css";

const REVEAL_REASONS = ["Verify the customer's account", "Customer asked for a phone call", "Check a possible duplicate reporter"] as const;
const time = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit" });
const day = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" });
const date = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" });

/** Render tokens and removal markers distinctly; everything else is plain text. */
function Tokenised({ text }: { text: string }) {
  const parts = text.split(/(\[(?:PERSON|EMAIL|PHONE|IBAN)_\d+\]|\[signature removed\]|\[quoted text removed\])/g);
  return <>{parts.map((p, i) => (/^\[(PERSON|EMAIL|PHONE|IBAN)_\d+\]$/.test(p) ? <span key={i} className="token">{p}</span>
    : /^\[(signature|quoted text) removed\]$/.test(p) ? <span key={i} className="removed">{p}</span> : p))}</>;
}

function Sender({ approval }: { approval: ApprovalDetail }) {
  const from = approval.proposal.email.from;
  const [asking, setAsking] = useState(false);
  const [reason, setReason] = useState<string>(REVEAL_REASONS[0]);
  const [shown, setShown] = useState<Reveal | null>(null);
  const reveal = useMutation({
    mutationFn: () => approvalsApi.reveal(from.name_token ?? from.email_token, reason, approval.thread_id),
    onSuccess: (r) => { setShown(r); setAsking(false); },
  });
  useEffect(() => {
    if (!shown) return;
    const id = window.setTimeout(() => setShown(null), new Date(shown.expires_at).getTime() - Date.now());
    return () => window.clearTimeout(id);
  }, [shown]);

  if (shown) {
    const name = shown.values.PERSON, email = shown.values.EMAIL;
    return (
      <span className="stack" style={{ gap: 6 }}>
        <span className="row" style={{ gap: 10, flexWrap: "wrap" }}>
          <span style={{ fontWeight: 500 }}>{name ? `${name} <${email ?? ""}>` : email}</span>
          <button type="button" className="btn btn-sm op-press" onClick={() => setShown(null)}>Hide</button>
        </span>
        <span style={{ fontSize: 12, color: "var(--warn)" }}>
          Visible to you until {time.format(new Date(shown.expires_at))} · access recorded in the audit log
        </span>
      </span>
    );
  }
  return (
    <span className="stack" style={{ gap: 8 }}>
      <span className="row" style={{ gap: 10, flexWrap: "wrap" }}>
        <span className="token">{from.name_token ? `[${from.name_token}] <[${from.email_token}]>` : `[${from.email_token}]`}</span>
        {!asking && <button type="button" className="btn btn-sm op-press" onClick={() => setAsking(true)}>Show identity…</button>}
      </span>
      {asking && (
        <div className="reveal-box op-enter">
          <label htmlFor="reveal-reason" style={{ fontSize: 13, fontWeight: 500 }}>Why do you need the sender's identity? This is logged.</label>
          <select id="reveal-reason" className="input" value={reason} onChange={(e) => setReason(e.target.value)}>
            {REVEAL_REASONS.map((r) => <option key={r}>{r}</option>)}
          </select>
          {reveal.error && <span className="field-error">{(reveal.error as Error).message}</span>}
          <span className="row" style={{ gap: 8 }}>
            <button type="button" className="btn btn-primary btn-sm op-press" disabled={reveal.isPending} onClick={() => reveal.mutate()}>
              {reveal.isPending ? "Opening…" : "Show for 5 minutes"}
            </button>
            <button type="button" className="btn btn-sm op-press" onClick={() => setAsking(false)}>Cancel</button>
          </span>
        </div>
      )}
    </span>
  );
}

function Fact({ label, children, wide, mono }: { label: string; children: ReactNode; wide?: boolean; mono?: boolean }) {
  return (
    <div style={wide ? { gridColumn: "span 2" } : undefined}>
      <dt>{label}</dt>
      <dd className={mono ? "mono" : undefined} style={mono ? { fontSize: 13 } : undefined}>{children}</dd>
    </div>
  );
}

function Outcome({ approval }: { approval: ApprovalDetail }) {
  if (approval.status === "rejected") {
    return <span className="row" style={{ gap: 8, fontSize: 15, fontWeight: 500, color: "var(--ink-2)" }}>
      <Icon name="close" size={18} />Rejected{approval.reason ? `: ${approval.reason}` : ""}. No ticket created; the run has ended.</span>;
  }
  const { outcome, jira_key: key } = approval;
  if (outcome === "ticket_failed") {
    return <span role="alert" className="row" style={{ gap: 8, fontSize: 15, fontWeight: 500, color: "var(--crit)" }}>
      <Icon name="alert" size={18} />Approved, but Jira refused the ticket: {approval.outcome_detail}</span>;
  }
  if (outcome === "ticket_created" || outcome === "linked") {
    return <span className="row" style={{ gap: 8, fontSize: 15, fontWeight: 500, color: "var(--good)" }}>
      <Icon name="check" size={18} />{outcome === "linked" ? `Approved. Linked to ${key} as another report.` : `Approved. ${key} created.`}</span>;
  }
  return <span className="row" style={{ gap: 8, fontSize: 15, fontWeight: 500, color: "var(--ink-2)" }}>
    <span className="spinner" />Approved · ticket creation pending (OpsPilot keeps trying if Jira is unavailable)</span>;
}

export function ApproveTicketPage() {
  const { id = "" } = useParams();
  const { user } = useSession();
  const qc = useQueryClient();
  useLiveEvents();
  const now = useNow();
  const query = useQuery({ queryKey: ["approval", id], queryFn: () => approvalsApi.get(id) });
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [severity, setSeverity] = useState<Severity | null>(null); // null = keep what the AI proposed
  const decide = useMutation({
    mutationFn: (decision: "approve" | "reject") =>
      approvalsApi.decide(id, decision, decision === "reject" ? reason : undefined,
        decision === "approve" && severity ? { severity } : {}),
    onSuccess: (a) => { qc.setQueryData(["approval", id], a); void qc.invalidateQueries({ queryKey: ["approvals"] }); setRejecting(false); },
  });

  if (query.isLoading) return <div className="row muted"><span className="spinner" />Loading…</div>;
  if (query.error || !query.data) {
    return <div role="alert" className="alert alert-error">{(query.error as Error | null)?.message ?? "Not found"}</div>;
  }
  const a = query.data;
  const p = a.proposal;
  const canDecide = !!user && GATE_DECIDERS[a.gate].some((r) => user.roles.includes(r));
  const retention = new Date(new Date(p.email.received_at).getTime() + 30 * 86_400_000);
  const linking = p.action === "link" && p.duplicate_of;
  const best = p.dedupe.best;
  const dedupeNote = p.duplicate_of
    ? `Possible duplicate of ${p.duplicate_of.key} (${p.duplicate_of.score.toFixed(2)}, ${p.duplicate_of.reason}). Approving adds this report to ${p.duplicate_of.key} instead of creating a new ticket.`
    : !p.dedupe.checked ? (p.dedupe.note ?? "Duplicates were not checked.")
    : `No duplicate found: searched ${p.dedupe.searched} open incidents from the last ${p.dedupe.hours} h${best ? `, closest match ${best.key} ${best.score.toFixed(2)}` : ""} (threshold ${p.dedupe.threshold.toFixed(2)}).`;
  const proposed = p.ticket.severity;
  const chosen = severity ?? proposed;
  const finalSeverity = (a.edits.severity as Severity | undefined) ?? proposed;
  const editable = a.status === "pending" && canDecide && !rejecting;
  const decideError = decide.error instanceof ApiError ? decide.error.message : decide.error ? "Could not save the decision." : null;

  return (
    <>
      <header className="op-enter stack" style={{ gap: 6 }}>
        <Link to="/approvals" style={{ fontSize: 14 }}>← Approvals</Link>
        <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
          <h1 style={{ margin: 0, fontSize: 26, fontWeight: 600 }}>
            {linking ? `Add this email to ${p.duplicate_of?.key}?` : "Create Jira Incident"}
          </h1>
          <span className={`badge ${GATES[a.gate].badge}`}>{GATES[a.gate].label} · {GATES[a.gate].tier}</span>
        </div>
        <div className="muted" style={{ fontSize: 14 }}>
          Thread <span className="mono">{a.thread_id.slice(0, 8)}</span>
          {a.status === "pending" ? <> · paused at <span className="mono">gate_ticket</span> for {waitingFor(a.requested_at, now)}</> : null}
        </div>
      </header>

      <div className="approve-grid">
        <section aria-label="Email" className="email-card op-enter op-d1">
          <div className="privacy-bar">
            <Icon name="shield" size={16} />
            Pseudonymised: {p.privacy.summary ?? "identifiers tokenised"} · raw email deleted {date.format(retention)} (30-day retention)
          </div>
          <dl className="email-meta">
            <dt>From</dt><dd><Sender approval={a} /></dd>
            <dt>Received</dt><dd>{day.format(new Date(p.email.received_at))}</dd>
            <dt>Subject</dt><dd style={{ fontWeight: 600 }}><Tokenised text={p.email.subject} /></dd>
            {p.email.attachments.length > 0 && <><dt>Attachments</dt><dd className="mono" style={{ fontSize: 13 }}>
              {p.email.attachments.map((f) => f.filename).join(", ")}</dd></>}
          </dl>
          <div className="email-body"><Tokenised text={p.email.body} /></div>
        </section>

        <div className="side">
          <section aria-label="Intake result" className="card card-pad op-enter op-d2">
            <div className="row" style={{ justifyContent: "space-between", alignItems: "baseline", flexWrap: "wrap", gap: 8 }}>
              <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>What Intake understood</h2>
              <span className="row" style={{ gap: 8 }}>
                <span className="badge badge-accent">AI-generated · check before approving</span>
                <span className="mono muted" style={{ fontSize: 12 }}>{p.model.model}{p.model.thinking ? " · thinking" : ""}</span>
              </span>
            </div>
            <dl className="facts">
              <Fact label="Category">{p.intake.category}</Fact>
              <Fact label="Severity"><span className={`badge mono ${SEVERITY_BADGE[p.intake.severity]}`}>{p.intake.severity}</span></Fact>
              <Fact label="Site" mono>{p.intake.site ?? "–"}</Fact>
              {p.intake.asset && <Fact label="Machine / system" mono>{p.intake.asset}</Fact>}
              {p.intake.facts.map((f) => <Fact key={f.label} label={f.label}><Tokenised text={f.value} /></Fact>)}
              {p.intake.error && <Fact label="Error" wide>{p.intake.error}</Fact>}
              <Fact label="Missing details">
                {p.intake.missing_details.length
                  ? <span style={{ color: "var(--warn)" }}>{p.intake.missing_details.join(", ")}</span>
                  : <span className="row" style={{ gap: 6, color: "var(--good)" }}><Icon name="check" size={14} />None</span>}
              </Fact>
            </dl>
          </section>

          <section aria-label="Proposed ticket" className="card card-pad stack op-enter op-d3" style={{ gap: 12 }}>
            <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>{linking ? "Report OpsPilot will add" : "Ticket OpsPilot will create"}</h2>
            <dl className="ticket-meta">
              <dt>Project</dt>
              <dd className="mono" style={{ fontSize: 13 }}>{p.jira.configured ? `${p.jira.project} · ${p.jira.issue_type}` : "Jira not configured yet (created once it is)"}</dd>
              {linking ? <><dt>Add to</dt><dd className="mono">{p.duplicate_of?.key}{p.duplicate_of?.summary ? ` · ${p.duplicate_of.summary}` : ""}</dd></> : null}
              <dt>Summary</dt><dd style={{ fontWeight: 500 }}><Tokenised text={p.ticket.summary} /></dd>
              <dt><label htmlFor="ticket-severity">Severity</label></dt>
              <dd>
                {editable ? (
                  <span className="stack" style={{ gap: 4 }}>
                    <select id="ticket-severity" className="input" style={{ minHeight: 36, fontSize: 14 }} value={chosen}
                      onChange={(e) => setSeverity(e.target.value === proposed ? null : (e.target.value as Severity))}>
                      {SEVERITIES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
                    </select>
                    <span className="muted" style={{ fontSize: 12 }}>
                      {chosen === proposed ? "Suggested by the AI. Change it if it is wrong." : `AI suggested ${proposed} · you changed it to ${chosen}`}
                    </span>
                  </span>
                ) : (
                  <span className="row" style={{ gap: 8 }}>
                    <span className={`badge mono ${SEVERITY_BADGE[finalSeverity]}`}>{finalSeverity}</span>
                    {finalSeverity !== proposed && <span className="muted" style={{ fontSize: 12 }}>changed by the approver (AI suggested {proposed})</span>}
                  </span>
                )}
              </dd>
              <dt>Reporter</dt><dd className="mono" style={{ fontSize: 13 }}>[{p.reporter_token}] · no contact details in Jira</dd>
            </dl>
            <div className="note"><Icon name="audit" size={16} /><span>{dedupeNote}</span></div>
          </section>
        </div>
      </div>

      <footer className="decision-bar op-enter op-d4" aria-label="Decision">
        {a.status !== "pending" ? (
          <>
            <Outcome approval={a} />
            <Link to="/approvals" style={{ marginLeft: "auto", fontSize: 15, fontWeight: 500 }}>Back to queue →</Link>
          </>
        ) : !canDecide ? (
          <span className="muted">Your role can view this request but not decide it. {a.approver}s decide ticket requests.</span>
        ) : rejecting ? (
          <>
            <label htmlFor="reject-note" style={{ fontSize: 14, fontWeight: 500 }}>Reason</label>
            <input id="reject-note" className="input grow" autoFocus value={reason} placeholder="e.g. Not a support request"
              onChange={(e) => setReason(e.target.value)} onKeyDown={(e) => e.key === "Enter" && reason.trim() && decide.mutate("reject")} />
            <button type="button" className="btn btn-danger op-press" disabled={!reason.trim() || decide.isPending} onClick={() => decide.mutate("reject")}>Confirm reject</button>
            <button type="button" className="btn op-press" onClick={() => setRejecting(false)}>Cancel</button>
            {decideError && <span role="alert" className="field-error">{decideError}</span>}
          </>
        ) : (
          <>
            <button type="button" className="btn btn-primary op-press" disabled={decide.isPending} onClick={() => decide.mutate("approve")}>
              {decide.isPending ? "Saving…" : linking ? `Approve and add to ${p.duplicate_of?.key}` : `Approve and create ${chosen} ticket`}
            </button>
            <button type="button" className="btn btn-danger op-press" onClick={() => setRejecting(true)}>Reject</button>
            <span className="muted" style={{ fontSize: 13 }}>Your decision and role are written to the audit log.</span>
            {decideError && <span role="alert" className="field-error">{decideError}</span>}
          </>
        )}
      </footer>
    </>
  );
}
