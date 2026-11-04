import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { ApiError } from "../../api/client";
import { privacyApi, type DataRequest, type DsrStatus, type DsrType, type RequestDetail, type StoreRow } from "../../api/privacy";
import { Icon } from "../../components/Icon";
import "./privacy.css";

const TYPES: [DsrType, string][] = [
  ["access", "Access (Art. 15, 20)"], ["erasure", "Erasure (Art. 17)"], ["rectification", "Rectification (Art. 16)"],
  ["restriction", "Restriction (Art. 18)"], ["objection", "Objection (Art. 21)"],
];
const STATUS: Record<DsrStatus, [string, string]> = {
  open: ["Open", "badge-accent"],
  sending: ["Sending", "badge-accent"],
  erasing: ["Erasing", "badge-warn"],
  confirming: ["Confirming", "badge-warn"],
  send_failed: ["Not sent", "badge-crit"],
  completed: ["Completed", "badge-good"],
};
const day = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" });
const short = (iso: string) => day.format(new Date(iso));
const IN_PROGRESS: DsrStatus[] = ["sending", "erasing", "confirming"];

function message(error: unknown): string {
  return error instanceof ApiError ? error.message : "Something went wrong. Nothing was changed.";
}

function NewRequest({ onCreated }: { onCreated: (id: string) => void }) {
  const qc = useQueryClient();
  const [type, setType] = useState<DsrType>("access");
  const [address, setAddress] = useState("");
  const [received, setReceived] = useState("");
  const log = useMutation({
    mutationFn: () => privacyApi.log({ type, address, ...(received ? { received_on: received } : {}) }),
    onSuccess: (r) => {
      setAddress("");
      setReceived("");
      void qc.invalidateQueries({ queryKey: ["dsr"] });
      onCreated(r.id);
    },
  });
  const submit = (e: FormEvent) => { e.preventDefault(); log.mutate(); };
  return (
    <form className="dsr-form" onSubmit={submit}>
      <label>Type
        <select className="select" value={type} onChange={(e) => setType(e.target.value as DsrType)}>
          {TYPES.map(([id, label]) => <option key={id} value={id}>{label}</option>)}
        </select>
      </label>
      <label>Requester's registered email address
        <input className="input" type="email" required autoComplete="off" value={address} onChange={(e) => setAddress(e.target.value)} />
      </label>
      <label>Date received (today if empty)
        <input className="input" type="date" value={received} onChange={(e) => setReceived(e.target.value)} />
      </label>
      {log.isError && <div role="alert" className="alert alert-error">{message(log.error)}</div>}
      <button type="submit" className="btn btn-primary op-press" disabled={log.isPending || !address}>Log request</button>
    </form>
  );
}

function Actions({ request }: { request: RequestDetail }) {
  const qc = useQueryClient();
  const [confirming, setConfirming] = useState(false);
  const [text, setText] = useState("");
  useEffect(() => { setConfirming(false); setText(""); }, [request.id]);
  const step = useMutation({
    mutationFn: (run: () => Promise<DataRequest>) => run(),
    onSuccess: () => {
      setConfirming(false);
      setText("");
      void qc.invalidateQueries({ queryKey: ["dsr"] });
    },
  });
  const held = request.stores.filter((s) => s.count > 0 && s.erase_action !== "Keep, token only").length;
  const anyDown = request.stores.some((s) => !s.available);
  const error = step.isError && <div role="alert" className="alert alert-error" style={{ flexBasis: "100%" }}>{message(step.error)}</div>;

  if (request.status === "completed") {
    return (
      <footer className="dsr-footer">
        <span className="row" style={{ gap: 8, fontSize: 15, fontWeight: 500, color: "var(--good)" }}>
          <Icon name="check" size={18} />{request.outcome}{request.closed_at ? ` ${short(request.closed_at)}.` : ""}
        </span>
      </footer>
    );
  }
  if (request.status === "send_failed") {
    return (
      <footer className="dsr-footer">
        {error}
        <span className="dsr-note" style={{ color: "var(--crit)" }}>{request.outcome}</span>
        <button type="button" className="btn btn-primary op-press" disabled={step.isPending} onClick={() => step.mutate(() => privacyApi.retry(request.id))}>Send again</button>
      </footer>
    );
  }
  if (request.status !== "open") {
    const waiting = request.status === "erasing" ? `Waiting for: ${request.pending_stores.join(", ")}.` : "The email is on its way.";
    return (
      <footer className="dsr-footer" role="status"><span className="spinner" /><span>{waiting}</span></footer>
    );
  }
  if (!request.verified) {
    return (
      <footer className="dsr-footer">
        {error}
        <input aria-label="How the identity was checked" className="input dsr-note" placeholder="How was the identity checked? For example: replied from the registered address"
          value={text} onChange={(e) => setText(e.target.value)} />
        <button type="button" className="btn btn-primary op-press" disabled={step.isPending || text.trim().length < 5}
          onClick={() => step.mutate(() => privacyApi.verify(request.id, text))}>Mark identity verified</button>
      </footer>
    );
  }
  if (request.type === "access") {
    return (
      <footer className="dsr-footer">
        {error}
        <button type="button" className="btn btn-primary op-press" disabled={step.isPending || anyDown} onClick={() => step.mutate(() => privacyApi.exportData(request.id))}>Export and send data</button>
        <span className="muted" style={{ fontSize: 13 }}>Machine-readable JSON, sent to the verified address only.</span>
      </footer>
    );
  }
  if (request.type === "erasure") {
    return (
      <footer className="dsr-footer">
        {error}
        {confirming ? (
          <>
            <span className="op-enter dsr-note" style={{ fontSize: 14, fontWeight: 500, color: "var(--crit)" }}>
              This cannot be undone. Erase data held on {request.who ?? "this address"} in {held} {held === 1 ? "store" : "stores"}?
            </span>
            <button type="button" className="btn btn-danger op-press" disabled={step.isPending} onClick={() => step.mutate(() => privacyApi.erase(request.id))}>Erase now</button>
            <button type="button" className="btn op-press" onClick={() => setConfirming(false)}>Cancel</button>
          </>
        ) : (
          <>
            <button type="button" className="btn btn-danger op-press" disabled={anyDown} onClick={() => setConfirming(true)}>Erase across all stores…</button>
            <span className="muted" style={{ fontSize: 13 }}>Audit entries stay (legal obligation) with the identity replaced by the token. The mailbox itself is not changed: delete the originals there by hand.</span>
          </>
        )}
      </footer>
    );
  }
  return (
    <footer className="dsr-footer">
      {error}
      <input aria-label="What was done" className="input dsr-note" placeholder="OpsPilot does not carry this out for you. Record what you did or why it was refused."
        value={text} onChange={(e) => setText(e.target.value)} />
      <button type="button" className="btn btn-primary op-press" disabled={step.isPending || text.trim().length < 5}
        onClick={() => step.mutate(() => privacyApi.close(request.id, text))}>Close request</button>
    </footer>
  );
}

function storeAction(r: RequestDetail, s: StoreRow): string {
  const erasure = r.type === "erasure";
  if (erasure && r.status === "completed") return s.count > 0 ? "Kept, token only" : s.store === "jira" ? "Unlinked" : "Deleted";
  if (s.count === 0) return "Nothing held";
  if (erasure) return s.erase_action;
  if (r.type !== "access") return "Review by hand";
  return r.status === "completed" ? "Exported" : "Include in export";
}

function Detail({ id }: { id: string }) {
  const detail = useQuery({
    queryKey: ["dsr", id],
    queryFn: () => privacyApi.get(id),
    refetchInterval: (q) => (q.state.data && IN_PROGRESS.includes(q.state.data.status) ? 3000 : false),
  });
  if (detail.isError) return <section className="privacy-card card-pad"><div role="alert" className="alert alert-error">{message(detail.error)}</div></section>;
  const r = detail.data;
  if (!r) return <section className="privacy-card card-pad"><span className="spinner" /></section>;
  const [label, badge] = STATUS[r.status];
  return (
    <section aria-labelledby="detail-h" className="privacy-card op-enter op-d3">
      <div className="dsr-detail-head">
        <div className="row">
          <h2 id="detail-h">{r.type_label} request <span className="mono muted" style={{ fontSize: 16, fontWeight: 500 }}>{r.id}</span></h2>
          <span className={`badge ${badge}`}>{label}</span>
        </div>
        <div style={{ fontSize: 14, color: "var(--ink-2)" }}>
          {r.article} · requester <span className="mono">{r.who ?? "not in the vault"}</span> · {r.verified_how ? `verified: ${r.verified_how}` : "identity not verified yet"} · due <strong>{short(r.due_on)}</strong>
        </div>
      </div>
      <div className="section-label">Where this person's data is held</div>
      <div>
        {r.stores.map((s) => (
          <div key={s.store} className="store-row">
            <span><span style={{ fontWeight: 500 }}>{s.label}</span><span className="muted"> · {s.what}</span></span>
            <span className="store-count">{s.available ? s.count : "–"}</span>
            {s.available ? (
              <span style={{ fontSize: 13, color: r.status === "completed" ? "var(--good)" : "var(--ink-2)" }}>{storeAction(r, s)}</span>
            ) : (
              <span className="row" style={{ gap: 6, fontSize: 13, color: "var(--warn)" }}><Icon name="alert" size={14} />Cannot be asked now</span>
            )}
          </div>
        ))}
      </div>
      <Actions request={r} />
    </section>
  );
}

export function PrivacyPage() {
  const list = useQuery({ queryKey: ["dsr"], queryFn: privacyApi.list, retry: false });
  const [selected, setSelected] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const requests = list.data?.requests ?? [];
  const current = selected ?? requests[0]?.id ?? null;
  const forbidden = list.error instanceof ApiError && list.error.status === 403;

  return (
    <>
      <header className="page-header op-enter">
        <div>
          <h1>Privacy and data requests</h1>
          <p>Requests under GDPR Articles 15 to 21. Answer within one month of receipt; every action is recorded in the audit log.</p>
        </div>
        <div className="privacy-chips">
          <Link className="privacy-chip" to="/legal/privacy">Privacy notice</Link>
          <span className="privacy-chip">Record of processing (Art. 30): [to be provided]</span>
          <span className="privacy-chip">DPIA: [date approved by DPO]</span>
        </div>
      </header>
      {forbidden ? (
        <div role="alert" className="alert alert-warn"><Icon name="lock" size={18} />Only the compliance lead can handle data requests.</div>
      ) : list.isError ? (
        <div role="alert" className="alert alert-error">{message(list.error)}</div>
      ) : (
        <div className="privacy-grid">
          <div className="privacy-side">
            <section aria-labelledby="dsr-h" className="privacy-card op-enter op-d1">
              <div className="privacy-card-head">
                <h2 id="dsr-h">Requests</h2>
                <div className="row" style={{ gap: 12 }}>
                  <span className="muted" style={{ fontSize: 13 }}>{list.data?.open ?? 0} open</span>
                  <button type="button" className="btn btn-sm op-press" aria-expanded={adding} onClick={() => setAdding(!adding)}><Icon name="plus" size={16} />Log request</button>
                </div>
              </div>
              {adding && <NewRequest onCreated={(id) => { setSelected(id); setAdding(false); }} />}
              {list.isSuccess && requests.length === 0 && !adding && (
                <p className="muted" style={{ margin: 0, padding: "12px 20px 16px", fontSize: 14 }}>No data requests yet.</p>
              )}
              {requests.map((r) => {
                const [label, badge] = STATUS[r.status];
                return (
                  <button key={r.id} type="button" className="dsr-row" aria-pressed={r.id === current} onClick={() => setSelected(r.id)}>
                    <span className="row" style={{ gap: 8 }}><span className="mono" style={{ fontSize: 13, fontWeight: 500 }}>{r.id}</span><span style={{ fontSize: 14, fontWeight: 600 }}>{r.type_label}</span></span>
                    <span className={`badge ${badge}`} style={{ justifySelf: "end" }}>{label}</span>
                    <span className="muted" style={{ fontSize: 13 }}><span className="mono">{r.who ?? "not in the vault"}</span> · received {short(r.received_on)}</span>
                    <span className={`dsr-due ${r.due_level}`}>{r.due}</span>
                  </button>
                );
              })}
            </section>
            <section aria-labelledby="ret-h" className="privacy-card op-enter op-d2" style={{ paddingBottom: 10 }}>
              <div className="privacy-card-head"><h2 id="ret-h">Retention schedule</h2></div>
              {list.data?.retention.map((t) => <div key={t.what} className="retention-row"><span>{t.what}</span><span>{t.keep}</span></div>)}
            </section>
          </div>
          {current ? <Detail id={current} /> : (
            <section className="privacy-card card-pad muted" style={{ fontSize: 14 }}>Log a request to see where that person's data is held.</section>
          )}
        </div>
      )}
    </>
  );
}
