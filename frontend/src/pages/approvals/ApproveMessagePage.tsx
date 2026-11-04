import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { approvalsApi, GATE_DECIDERS, GATES, type DraftCheck, type MessageApprovalDetail } from "../../api/approvals";
import { ApiError } from "../../api/client";
import { useSession } from "../../auth/session";
import { Icon } from "../../components/Icon";
import { useLiveEvents } from "../../hooks/useLiveEvents";
import "./approvals.css";

const RECIPIENT = "[RECIPIENT_NAME]";

function plural(n: number, one: string): string {
  return `${n} ${one}${n === 1 ? "" : "s"}`;
}

/** The body as text, with the placeholder that mail-service fills in at send time set apart. */
function DraftText({ text }: { text: string }) {
  return <>{text.split(RECIPIENT).flatMap((part, i) => (i === 0 ? [part] : [<span key={i} className="recipient-token">{RECIPIENT}</span>, part]))}</>;
}

function Checks({ checks, stale }: { checks: DraftCheck[]; stale: boolean }) {
  return (
    <section aria-label="Checks" className="card card-pad op-enter op-d3" aria-live="polite">
      <h2 style={{ margin: "0 0 8px", fontSize: 16, fontWeight: 600 }}>Checks on the draft</h2>
      {stale && <div className="muted" style={{ fontSize: 13, paddingBottom: 8 }}>You changed the text. Finish editing to check it again.</div>}
      {checks.map((c) => {
        const color = stale ? "var(--muted)" : c.ok ? "var(--good)" : c.blocking ? "var(--crit)" : "var(--warn)";
        return (
          <div key={c.id} className="check-row">
            <span style={{ color, display: "flex" }}><Icon name={c.ok ? "check" : c.blocking ? "close" : "alert"} size={16} /></span>
            <span><span style={{ fontWeight: 500 }}>{c.label}</span><span className="muted"> · {c.detail}</span></span>
          </div>
        );
      })}
    </section>
  );
}

function Outcome({ a }: { a: MessageApprovalDetail }) {
  const p = a.proposal;
  const key = p.jira_key ?? a.jira_key;
  const n = p.recipients.length;
  const line = { gap: 8, fontSize: 15, fontWeight: 500 };
  if (a.status === "sent_back") {
    const next = p.revision + 1;
    return <span className="row" style={{ ...line, color: "var(--ink-2)" }}>Sent back. Comms will redraft with your note (revision {next} of {p.max_revisions}).</span>;
  }
  if (a.status === "rejected") {
    return <span className="row" style={{ ...line, color: "var(--ink-2)" }}><Icon name="close" size={18} />Not sent{a.reason ? `: ${a.reason}` : ""}.</span>;
  }
  if (a.outcome === "message_failed") {
    return <span role="alert" className="row" style={{ ...line, color: "var(--crit)" }}><Icon name="alert" size={18} />Approved, but it could not be sent: {a.outcome_detail}</span>;
  }
  if (a.outcome === "message_partly_sent") {
    return <span role="alert" className="row" style={{ ...line, color: "var(--warn)" }}><Icon name="alert" size={18} />Approved, but not everyone received it: {a.outcome_detail}</span>;
  }
  if (a.outcome === "message_sent") {
    return <span className="row" style={{ ...line, color: "var(--good)" }}><Icon name="check" size={18} />Approved. {plural(n, "email")} sent{key ? ` and logged against ${key}` : ""}.</span>;
  }
  return <span className="row" style={{ ...line, color: "var(--ink-2)" }}><span className="spinner" />Approved. Sending {plural(n, "email")} now.</span>;
}

export function ApproveMessagePage() {
  const { id = "" } = useParams();
  const { user } = useSession();
  const qc = useQueryClient();
  useLiveEvents();
  const query = useQuery({ queryKey: ["approval", id], queryFn: () => approvalsApi.getMessage(id) });
  const [editing, setEditing] = useState(false);
  const [body, setBody] = useState<string | null>(null); // null = as drafted
  const [subject, setSubject] = useState<string | null>(null);
  const [checked, setChecked] = useState<DraftCheck[] | null>(null); // the checks on the approver's edits
  const [mode, setMode] = useState<"none" | "send_back" | "reject">("none");
  const [note, setNote] = useState("");

  const edits = () => {
    const p = query.data?.proposal;
    return {
      ...(subject !== null && subject !== p?.subject ? { subject } : {}),
      ...(body !== null && body !== p?.body ? { body } : {}),
    };
  };
  const recheck = useMutation({
    mutationFn: () => approvalsApi.draftChecks(id, edits()),
    onSuccess: (r) => { setChecked(r.checks); setEditing(false); },
  });
  const decide = useMutation({
    mutationFn: (decision: "approve" | "send_back" | "reject") =>
      approvalsApi.decide(id, decision, decision === "approve" ? undefined : note, decision === "approve" ? edits() : {}),
    onSuccess: (a) => {
      qc.setQueryData(["approval", id], a);
      void qc.invalidateQueries({ queryKey: ["approvals"] });
      setMode("none"); setEditing(false);
    },
  });

  if (query.isLoading) return <div className="row muted"><span className="spinner" />Loading…</div>;
  if (query.error || !query.data) {
    return <div role="alert" className="alert alert-error">{(query.error as Error | null)?.message ?? "Not found"}</div>;
  }
  const a = query.data;
  const p = a.proposal;
  const pending = a.status === "pending";
  const n = p.recipients.length;
  const shownSubject = pending ? subject ?? p.subject : a.edits.subject ?? p.subject;
  const shownBody = pending ? body ?? p.body : a.edits.body ?? p.body;
  const checks = pending ? checked ?? p.checks : p.checks;
  const blocked = checks.some((c) => !c.ok && c.blocking);
  const canDecide = !!user && GATE_DECIDERS.message.some((r) => user.roles.includes(r));
  const canSendBack = p.revision < p.max_revisions;
  const key = p.jira_key ?? a.jira_key;
  const apiError = decide.error ?? recheck.error;
  const errorText = apiError instanceof ApiError ? apiError.message : apiError ? "Could not save that. Try again." : null;
  const revision = p.revision === 0 ? `first draft, up to ${p.max_revisions} revisions` : `revision ${p.revision} of ${p.max_revisions}`;

  return (
    <>
      <header className="op-enter stack" style={{ gap: 6 }}>
        <Link to="/approvals" style={{ fontSize: 14 }}>← Approvals</Link>
        <div className="row" style={{ gap: 12, flexWrap: "wrap" }}>
          <h1 style={{ margin: 0, fontSize: 26, fontWeight: 600 }}>Send this update to {plural(n, "reporter")}?</h1>
          <span className={`badge ${GATES.message.badge}`}>External message · {GATES.message.tier}</span>
        </div>
        <div className="muted" style={{ fontSize: 14 }}>
          {p.kind_label}{key ? <> for <span className="mono">{key}</span></> : null} · {p.written_by === "model" ? "drafted by Comms" : "standard text (the AI's draft could not be used)"} · {revision}
        </div>
      </header>

      <div className="message-grid">
        <section aria-label="Draft" className="draft-card op-enter op-d1">
          <dl className="draft-meta">
            <dt>To</dt><dd>{n === 1 ? "The reporter (1 email)" : `Each reporter separately (${n} emails)`}</dd>
            <dt>Subject</dt>
            <dd style={{ fontWeight: 600 }}>
              {editing && pending ? (
                <input aria-label="Subject" className="input" style={{ width: "100%", boxSizing: "border-box" }} value={shownSubject}
                  onChange={(e) => { setSubject(e.target.value); setChecked(null); }} />
              ) : shownSubject}
            </dd>
          </dl>
          {editing && pending ? (
            <div className="draft-edit">
              <label htmlFor="draft-body" style={{ fontSize: 13, color: "var(--ink-2)" }}>Edit draft (names stay as tokens until sending)</label>
              <textarea id="draft-body" value={shownBody} onChange={(e) => { setBody(e.target.value); setChecked(null); }} />
            </div>
          ) : (
            <div className="draft-body"><DraftText text={shownBody} /></div>
          )}
          {p.notes.length > 0 && (
            <div className="note" style={{ margin: "0 20px 16px" }}><Icon name="audit" size={16} /><span>Your notes for Comms: {p.notes.join(" · ")}</span></div>
          )}
        </section>

        <div className="side">
          <section aria-label="Recipients" className="card card-pad op-enter op-d2">
            <h2 style={{ margin: 0, fontSize: 16, fontWeight: 600 }}>Recipients</h2>
            <p className="muted" style={{ margin: "4px 0 8px", fontSize: 13 }}>Real names and addresses are filled in only at send time.</p>
            {p.recipients.map((r) => (
              <div key={r.email_token} className="recipient-row">
                <span className="mono" style={{ fontSize: 13 }}>[{r.name_token ?? r.email_token}]</span>
                <span className="mono" style={{ fontSize: 13, color: "var(--ink-2)" }}>{r.site ?? "–"}</span>
                <span className="muted" style={{ fontSize: 13 }}>{r.how}</span>
              </div>
            ))}
          </section>
          <Checks checks={checks} stale={pending && editing} />
        </div>
      </div>

      <footer className="decision-bar op-enter op-d4" aria-label="Decision">
        {!pending ? (
          <>
            <Outcome a={a} />
            <Link to="/approvals" style={{ marginLeft: "auto", fontSize: 15, fontWeight: 500 }}>Next in queue →</Link>
          </>
        ) : !canDecide ? (
          <span className="muted">Your role can view this message but not decide it.</span>
        ) : mode !== "none" ? (
          <>
            <label htmlFor="msg-note" style={{ fontSize: 14, fontWeight: 500 }}>{mode === "send_back" ? "Note for Comms" : "Why not send it?"}</label>
            <input id="msg-note" className="input grow" autoFocus value={note}
              placeholder={mode === "send_back" ? "e.g. Mention the 41 retried uploads" : "e.g. Already told the customer by phone"}
              onChange={(e) => setNote(e.target.value)} onKeyDown={(e) => e.key === "Enter" && note.trim() && decide.mutate(mode)} />
            <button type="button" className="btn btn-danger op-press" disabled={!note.trim() || decide.isPending} onClick={() => decide.mutate(mode)}>
              {mode === "send_back" ? "Send back" : "Don't send"}
            </button>
            <button type="button" className="btn op-press" onClick={() => setMode("none")}>Cancel</button>
            {errorText && <span role="alert" className="field-error">{errorText}</span>}
          </>
        ) : (
          <>
            <button type="button" className="btn btn-primary op-press" disabled={blocked || editing || decide.isPending}
              title={editing ? "Finish editing first" : blocked ? "Fix the failed checks first" : undefined}
              onClick={() => decide.mutate("approve")}>
              {decide.isPending ? "Saving…" : `Approve and send ${plural(n, "email")}`}
            </button>
            <button type="button" className="btn op-press" disabled={recheck.isPending}
              onClick={() => (editing ? recheck.mutate() : setEditing(true))}>
              {recheck.isPending ? "Checking…" : editing ? "Done editing" : "Edit draft"}
            </button>
            <button type="button" className="btn btn-danger op-press" disabled={!canSendBack}
              title={canSendBack ? undefined : "All revisions are used: edit the draft yourself, or don't send it"}
              onClick={() => setMode("send_back")}>Send back to Comms</button>
            <button type="button" className="btn op-press" onClick={() => setMode("reject")}>Don't send</button>
            {blocked && !editing && <span className="muted" style={{ fontSize: 13 }}>Approve is enabled once the failed checks are fixed: edit the draft or send it back.</span>}
            {errorText && <span role="alert" className="field-error">{errorText}</span>}
          </>
        )}
      </footer>
    </>
  );
}
