import { useInfiniteQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { ApiError } from "../../api/client";
import { reportingApi } from "../../api/reporting";
import { Icon } from "../../components/Icon";
import "../approvals/approvals.css";

type Kind = "" | "human" | "tool" | "privacy";
const TABS: [Kind, string][] = [["", "All"], ["human", "People"], ["tool", "Agents and tools"], ["privacy", "Privacy"]];
const when = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;

export function AuditPage() {
  const [kind, setKind] = useState<Kind>("");
  const [text, setText] = useState("");
  const [q, setQ] = useState("");
  useEffect(() => {
    const id = window.setTimeout(() => setQ(text.trim()), 300);
    return () => window.clearTimeout(id);
  }, [text]);

  const log = useInfiniteQuery({
    queryKey: ["audit", kind, q],
    queryFn: ({ pageParam }) => reportingApi.audit({ kind, q, cursor: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const rows = log.data?.pages.flatMap((p) => p.entries) ?? [];
  const forbidden = log.error instanceof ApiError && log.error.status === 403;

  return (
    <>
      <header className="page-header op-enter">
        <div>
          <h1>Audit log</h1>
          <p>Append-only. Every classification, tool call, approval and email sent. Entries cannot be edited or deleted. Times in {zone}.</p>
        </div>
      </header>
      {forbidden ? (
        <div role="alert" className="alert alert-warn"><Icon name="lock" size={18} />Your role cannot read the audit log. The compliance lead, support managers and admins can.</div>
      ) : (
        <section aria-label="Audit entries" className="queue op-enter op-d1">
          <div className="queue-tabs" style={{ justifyContent: "space-between", alignItems: "center" }}>
            <div role="tablist" aria-label="Filter" className="row" style={{ gap: 8, flexWrap: "wrap" }}>
              {TABS.map(([id, label]) => (
                <button key={id || "all"} type="button" role="tab" aria-selected={kind === id} className="pill-tab op-press" onClick={() => setKind(id)}>{label}</button>
              ))}
            </div>
            <input type="search" className="input" style={{ maxWidth: 320 }} aria-label="Search" value={text}
              placeholder="Find by user token, site or ticket" onChange={(e) => setText(e.target.value)} />
          </div>
          {log.error && !forbidden && <div role="alert" className="alert alert-error" style={{ margin: 16 }}>{(log.error as Error).message}</div>}
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr><th>Time</th><th>Actor</th><th>Event</th><th>Detail</th><th>Ticket</th><th>Privacy record</th></tr>
              </thead>
              <tbody>
                {rows.map((e) => (
                  <tr key={e.id}>
                    <td className="mono" style={{ fontSize: 13, whiteSpace: "nowrap" }}>{when.format(new Date(e.time))}</td>
                    <td style={{ fontWeight: e.kind === "human" ? 600 : 400 }}>{e.actor}</td>
                    <td className="mono" style={{ fontSize: 13 }}>{e.event}</td>
                    <td style={{ overflowWrap: "anywhere" }}>{e.detail}</td>
                    <td className="mono" style={{ fontSize: 13 }}>{e.ticket ?? "–"}</td>
                    <td className="muted" style={{ fontSize: 13 }}>{e.privacy}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {log.isLoading && <div className="queue-empty"><span className="spinner" /> Loading…</div>}
          {!log.isLoading && rows.length === 0 && !log.error && <div className="queue-empty">No entries match.</div>}
          {log.hasNextPage && (
            <div style={{ padding: 16, textAlign: "center" }}>
              <button type="button" className="btn op-press" disabled={log.isFetchingNextPage} onClick={() => void log.fetchNextPage()}>
                {log.isFetchingNextPage ? "Loading…" : "Show older entries"}
              </button>
            </div>
          )}
        </section>
      )}
    </>
  );
}
