import { Icon, type IconName } from "../components/Icon";

/** Honest placeholder for screens whose services are built in later milestones. */
export function ComingSoon({ title, lead, milestone, icon, items }: {
  title: string; lead: string; milestone: string; icon: IconName; items: string[];
}) {
  return (
    <>
      <header className="page-header op-enter">
        <div><h1>{title}</h1><p>{lead}</p></div>
      </header>
      <section className="card card-pad op-enter op-d1 stack" style={{ maxWidth: 720 }}>
        <div className="row"><Icon name={icon} size={28} /><span className="badge badge-neutral">Arrives in {milestone}</span></div>
        <p style={{ margin: 0 }}>This screen needs services that are not built yet. When it ships it will show:</p>
        <ul style={{ margin: 0, paddingLeft: 20, display: "flex", flexDirection: "column", gap: 6, color: "var(--ink-2)" }}>
          {items.map((i) => <li key={i}>{i}</li>)}
        </ul>
      </section>
    </>
  );
}
