import { useQuery } from "@tanstack/react-query";
import { settingsApi, type Circuit } from "../../api/settings";
import { Icon } from "../../components/Icon";

const LABELS: Record<string, string> = {
  "identity-service": "Sign-in service", "settings-service": "Settings service", redis: "Redis (sessions, limits)",
  rabbitmq: "Event bus (RabbitMQ)", ollama: "Local AI model", "ollama-embed": "AI embeddings", jira: "Jira",
  "mail-provider": "Mailbox provider", "privacy-service": "Privacy service", "ops-service": "Ops service",
  "jira-connector": "Jira connector", "mail-service": "Mail service", "approval-service": "Approval service",
  "audit-service": "Audit service", "reporting-service": "Reporting service",
  "probe-mail": "Connection test · mailbox", "probe-jira": "Connection test · Jira", "probe-google": "Connection test · Google",
  "probe-microsoft": "Connection test · Microsoft", "probe-ollama": "Connection test · Ollama",
};

function State({ c }: { c: Circuit }) {
  switch (c.state) {
    case "closed": return <span className="badge badge-good"><Icon name="check" size={14} />Healthy</span>;
    case "half_open": return <span className="badge badge-warn">Testing recovery</span>;
    case "open": return <span className="badge badge-crit"><Icon name="alert" size={14} />Paused · retry in {c.retry_after_seconds} s</span>;
    default: return <span className="badge badge-neutral">No failures recorded</span>;
  }
}

export function SystemHealthPage() {
  const circuits = useQuery({ queryKey: ["circuits"], queryFn: settingsApi.circuits, refetchInterval: 5000 });
  const rows = circuits.data ?? [];
  const open = rows.filter((c) => c.state === "open").length;

  return (
    <>
      <header className="page-header op-enter">
        <div>
          <h1>System health</h1>
          <p>Every outbound dependency runs behind a circuit breaker. When one keeps failing, OpsPilot pauses calls to it instead of piling up errors.</p>
        </div>
        <div className="row muted" style={{ fontSize: 13 }}><Icon name="refresh" size={16} />Updates every 5 s</div>
      </header>
      {circuits.data && (
        <div role="status" className={`alert ${open ? "alert-error" : "alert-success"} op-enter op-d1`}>
          <Icon name={open ? "alert" : "check"} size={18} />
          {open ? `${open} dependenc${open === 1 ? "y is" : "ies are"} paused.` : "All dependencies in use are healthy."}
        </div>
      )}
      {circuits.error && <div role="alert" className="alert alert-error"><Icon name="alert" size={18} />{(circuits.error as Error).message}</div>}
      <section className="card op-enter op-d2" aria-label="Circuit breakers" style={{ overflow: "hidden" }}>
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr><th>Dependency</th><th>State</th><th>Recent failures</th><th>Opens after</th><th>Pause</th><th>Timeout</th><th>Attempts</th></tr>
            </thead>
            <tbody>
              {rows.map((c) => (
                <tr key={c.dependency} className="op-row">
                  <td><div style={{ fontWeight: 500 }}>{LABELS[c.dependency] ?? c.dependency}</div><div className="mono muted" style={{ fontSize: 12 }}>{c.dependency}</div></td>
                  <td><State c={c} /></td>
                  <td className="mono">{c.failures}</td>
                  <td className="mono">{c.fail_max} failures</td>
                  <td className="mono">{c.reset_timeout_seconds} s</td>
                  <td className="mono">{c.timeout_seconds} s</td>
                  <td className="mono">{c.retry_attempts}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </>
  );
}
