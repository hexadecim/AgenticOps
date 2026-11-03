import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { reportingApi, type DependencyHealth, type ProbeHealth } from "../../api/reporting";
import { SEVERITY_BADGE, type Severity } from "../../api/approvals";
import { Icon } from "../../components/Icon";
import { useLiveEvents } from "../../hooks/useLiveEvents";
import "./dashboard.css";

const time = new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit" });
const today = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" });
const HEALTH = {
  up: { text: "Up", badge: "badge-good", icon: "check" },
  recovering: { text: "Recovering", badge: "badge-warn", icon: "refresh" },
  down: { text: "Down", badge: "badge-crit", icon: "alert" },
} as const;

function Health({ d }: { d: DependencyHealth }) {
  const h = HEALTH[d.health];
  const detail = !d.since ? "no failures recorded"
    : d.health === "up" ? `recovered ${time.format(new Date(d.since))}`
    : `since ${time.format(new Date(d.since))}`;
  const callers = Object.entries(d.callers).map(([svc, state]) => `${svc}: ${state.replace("_", "-")}`).join(", ");
  return (
    <div className={`health ${d.health === "down" ? "down" : ""}`} title={callers || undefined}>
      <div className="health-head">
        <span className="health-name">{d.label}</span>
        <span className={`badge ${h.badge}`}><Icon name={h.icon} size={14} />{h.text}</span>
      </div>
      <span className="health-detail">{detail}</span>
    </div>
  );
}

function ProbeTile({ p }: { p: ProbeHealth }) {
  const h = HEALTH[p.health];
  const since = p.since ? `${p.health === "up" ? "ok" : "failing"} since ${time.format(new Date(p.since))}` : "";
  return (
    <div className={`health ${p.health === "down" ? "down" : ""}`} title={since || undefined}>
      <div className="health-head">
        <span className="health-name">{p.name}</span>
        <span className={`badge ${h.badge}`}><Icon name={h.icon} size={14} />{p.health === "up" ? "Pass" : "Fail"}</span>
      </div>
      <span className="health-detail">{p.region ? `${p.region} · ` : ""}{p.detail}</span>
    </div>
  );
}

export function DashboardPage() {
  useLiveEvents();
  const dash = useQuery({ queryKey: ["dashboard"], queryFn: reportingApi.dashboard, refetchInterval: 60_000 });
  const [picked, setPicked] = useState<string | null>(null);
  const key = picked ?? dash.data?.timeline.key ?? null;
  const timeline = useQuery({
    queryKey: ["timeline", key],
    queryFn: () => reportingApi.timeline(key ?? ""),
    enabled: !!key && key !== dash.data?.timeline.key,
  });
  const d = dash.data;
  const entries = key && key === d?.timeline.key ? d.timeline.entries : timeline.data?.entries ?? [];
  const incident = d?.incidents.find((i) => i.key === key);

  return (
    <>
      <header className="page-header op-enter">
        <div>
          <h1>Operations</h1>
          <p>Today, {today.format(new Date())} · service levels over the last {d?.window_hours ?? 24} h · updates live</p>
        </div>
      </header>
      {dash.error && <div role="alert" className="alert alert-error">{(dash.error as Error).message}</div>}
      {dash.isLoading && <div className="row muted"><span className="spinner" />Loading…</div>}

      {d && (
        <>
          <section aria-labelledby="deps-h" className="dash-section op-enter op-d1">
            <h2 id="deps-h">Platform health</h2>
            {d.probes.length > 0 ? (
              <div className="health-grid">
                {d.probes.map((p) => <ProbeTile key={`${p.name}/${p.region ?? ""}`} p={p} />)}
              </div>
            ) : (
              <div className="muted" style={{ fontSize: 13 }}>No probe results yet: ops-service probes your systems every minute.</div>
            )}
            <span className="muted" style={{ fontSize: 12 }}>Health probes of the supported systems (ops-service), changes shown live.</span>
            <h3 className="dash-sub">OpsPilot dependencies</h3>
            <div className="health-grid">
              {d.dependencies.map((x) => <Health key={x.name} d={x} />)}
            </div>
            <span className="muted" style={{ fontSize: 12 }}>From circuit-breaker events of every service.</span>
          </section>

          <section aria-labelledby="stages-h" className="dash-section op-enter op-d2">
            <h2 id="stages-h">Open tickets by stage</h2>
            <div className="stage-grid">
              {d.stages.map((s) => (
                <div key={s.id} className="stage">
                  <div className="stage-label">{s.label}</div>
                  <div className="stage-count" style={s.count === null ? { color: "var(--muted)" } : undefined}>{s.count ?? "–"}</div>
                  {s.note && <div className="stage-note">{s.note}</div>}
                </div>
              ))}
            </div>
          </section>

          <div className="dash-bottom">
            <section aria-labelledby="timeline-h" className="dash-card op-enter op-d3">
              <div className="dash-card-head">
                <h2 id="timeline-h">{incident ? `Timeline · ${incident.key}` : "Incident timeline"}</h2>
                <span className="row" style={{ gap: 8 }}>
                  {incident?.severity && <span className={`badge mono ${SEVERITY_BADGE[incident.severity as Severity] ?? "badge-neutral"}`}>{incident.severity} · {incident.status}</span>}
                  {d.incidents.length > 1 && (
                    <select aria-label="Incident" className="input" style={{ minHeight: 32, fontSize: 13 }} value={key ?? ""}
                      onChange={(e) => setPicked(e.target.value)}>
                      {d.incidents.map((i) => <option key={i.key} value={i.key}>{i.key} · {i.title ?? ""}</option>)}
                    </select>
                  )}
                </span>
              </div>
              {incident && <div className="muted" style={{ fontSize: 13 }}>{incident.title} · {incident.reports} report{incident.reports === 1 ? "" : "s"}{incident.site ? ` · ${incident.site}` : ""}</div>}
              {!key && <div className="muted" style={{ padding: "16px 0" }}>No incident yet. Approved tickets appear here with their full history.</div>}
              {timeline.isLoading && <div className="row muted"><span className="spinner" />Loading…</div>}
              {entries.map((e, i) => (
                <div key={i} className={`tl-row ${e.system ? "system" : ""}`}>
                  <span className="tl-time">{time.format(new Date(e.at))}</span>
                  <span className="tl-actor">{e.actor}</span>
                  <span>{e.text}</span>
                </div>
              ))}
            </section>

            <section aria-labelledby="sla-h" className="dash-card op-enter op-d4">
              <h2 id="sla-h">Service levels · last {d.window_hours} h</h2>
              {d.service_levels.map((s) => (
                <div key={s.id} className="sla-row">
                  <span className="sla-label">{s.label}</span>
                  <span className={`sla-value ${s.breached ? "breached" : ""}`}>{s.value}{s.detail && <span className="sla-detail"> {s.detail}</span>}</span>
                </div>
              ))}
            </section>
          </div>
        </>
      )}
    </>
  );
}
