import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { ApiError } from "../../api/client";
import { applies, settingsApi, type ConnectionReport, type FieldSpec, type SecretEdits, type SectionView } from "../../api/settings";
import { Icon } from "../../components/Icon";

const fmt = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

class SaveError extends Error {
  constructor(readonly status: number, readonly code: string, message: string, readonly errors: Record<string, string>) {
    super(message);
  }
}

function Field({ spec, value, onChange, error, secretSet, secretEdit, onSecret }: {
  spec: FieldSpec; value: unknown; onChange: (v: unknown) => void; error?: string;
  secretSet: boolean; secretEdit: string | undefined; onSecret: (v: string | undefined) => void;
}) {
  const id = `f-${spec.key}`;
  const describedBy = [spec.help && `${id}-help`, error && `${id}-err`].filter(Boolean).join(" ") || undefined;
  const label = <label htmlFor={id}>{spec.label}{spec.required && <span aria-hidden="true" className="muted"> *</span>}</label>;
  let control;

  if (spec.kind === "bool") {
    return (
      <div className="field">
        <label className="check" style={{ alignSelf: "flex-start" }}>
          <input id={id} type="checkbox" checked={Boolean(value)} onChange={(e) => onChange(e.target.checked)} aria-describedby={describedBy} />
          <span>{spec.label}</span>
        </label>
        {spec.help && <span id={`${id}-help`} className="hint">{spec.help}</span>}
        {error && <span id={`${id}-err`} className="field-error">{error}</span>}
      </div>
    );
  }
  if (spec.kind === "select") {
    control = (
      <select id={id} className="select" value={String(value ?? "")} onChange={(e) => onChange(e.target.value)} aria-describedby={describedBy}>
        {spec.options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
      </select>
    );
  } else if (spec.kind === "secret") {
    const replacing = secretEdit !== undefined && secretEdit !== "";
    if (secretSet && secretEdit === undefined) {
      control = (
        <div className="row secret-saved">
          <span className="badge badge-good"><Icon name="lock" size={14} />Saved</span>
          <span className="mono muted" aria-hidden="true">••••••••••</span>
          <button type="button" className="btn btn-sm op-press" onClick={() => onSecret("")}>Replace</button>
        </div>
      );
    } else {
      control = (
        <div className="row" style={{ flexWrap: "nowrap" }}>
          <input id={id} className="input" type="password" autoComplete="new-password" value={secretEdit ?? ""}
            placeholder={secretSet ? "Type a new value" : ""} onChange={(e) => onSecret(e.target.value)} aria-describedby={describedBy} />
          {secretSet && (
            <button type="button" className="btn btn-sm op-press" onClick={() => onSecret(undefined)}>
              {replacing ? "Cancel" : "Keep saved"}
            </button>
          )}
        </div>
      );
    }
  } else {
    control = (
      <input id={id} className="input" value={value === undefined || value === null ? "" : String(value)}
        type={spec.kind === "number" ? "number" : spec.kind === "email" ? "email" : spec.kind === "url" ? "url" : "text"}
        min={spec.minimum ?? undefined} max={spec.maximum ?? undefined} placeholder={spec.placeholder}
        onChange={(e) => onChange(spec.kind === "number" && e.target.value !== "" ? Number(e.target.value) : e.target.value)}
        aria-invalid={Boolean(error)} aria-describedby={describedBy} />
    );
  }
  return (
    <div className="field">
      {label}
      {control}
      {spec.help && <span id={`${id}-help`} className="hint">{spec.help}</span>}
      {error && <span id={`${id}-err`} className="field-error">{error}</span>}
    </div>
  );
}

function Report({ report }: { report: ConnectionReport }) {
  return (
    <div role="status" className={`card card-pad stack report ${report.ok ? "report-ok" : "report-fail"}`} style={{ gap: 10 }}>
      <div className="row">
        <Icon name={report.ok ? "check" : "alert"} />
        <strong>{report.ok ? "Connection works" : "Connection failed"}</strong>
        <span className="muted" style={{ marginLeft: "auto", fontSize: 13 }}>{(report.duration_ms / 1000).toFixed(1)} s</span>
      </div>
      <div>{report.summary}</div>
      {report.checks.length > 0 && (
        <ul className="checklist">
          {report.checks.map((c) => (
            <li key={c.name}>
              <span className={c.ok ? "ok" : "bad"} aria-label={c.ok ? "Passed" : "Failed"}><Icon name={c.ok ? "check" : "close"} size={16} /></span>
              <span><strong>{c.name}</strong> · {c.detail}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function SectionForm({ view }: { view: SectionView }) {
  const qc = useQueryClient();
  const [values, setValues] = useState<Record<string, unknown>>(view.values);
  const [secrets, setSecrets] = useState<SecretEdits>({});
  const [report, setReport] = useState<ConnectionReport | null>(null);
  const [savedVersion, setSavedVersion] = useState<number | null>(null);

  useEffect(() => {
    setValues(view.values);
    setSecrets({});
  }, [view.version, view.values]);

  const visible = view.section.fields.filter((f) => applies(f, values));
  const dirty = useMemo(
    () => JSON.stringify(values) !== JSON.stringify(view.values) || Object.values(secrets).some((v) => v !== undefined && v !== ""),
    [values, secrets, view.values],
  );
  const secretPayload = (): SecretEdits => Object.fromEntries(Object.entries(secrets).filter(([, v]) => v !== undefined && v !== ""));

  const save = useMutation({
    mutationFn: async () => {
      try {
        return await settingsApi.save(view.section.id, view.version, values, secretPayload());
      } catch (err) {
        if (err instanceof ApiError) {
          throw new SaveError(err.status, err.code, err.message, (err.body.errors as Record<string, string>) ?? {});
        }
        throw err;
      }
    },
    onSuccess: (saved) => {
      setSavedVersion(saved.version);
      qc.setQueryData<SectionView[]>(["settings"], (old) => old?.map((s) => (s.section.id === saved.section.id ? saved : s)));
    },
  });
  const test = useMutation({
    mutationFn: () => settingsApi.test(view.section.id, values, secretPayload()),
    onSuccess: setReport,
  });
  const saveError = save.error instanceof SaveError ? save.error : null;
  const fieldErrors = saveError?.errors ?? {};

  function submit(e: FormEvent) {
    e.preventDefault();
    setSavedVersion(null);
    save.mutate();
  }

  return (
    <form className="stack" onSubmit={submit} noValidate>
      <div className="row muted" style={{ fontSize: 13 }}>
        {view.version > 0
          ? <>Version {view.version} · saved {view.updated_at ? fmt.format(new Date(view.updated_at)) : ""}</>
          : <>Not configured yet. Defaults are filled in where OpsPilot has them.</>}
      </div>
      {saveError && saveError.code === "version_conflict" && (
        <div role="alert" className="alert alert-warn">
          <Icon name="alert" size={18} /><span>{saveError.message}</span>
          <button type="button" className="btn btn-sm op-press" style={{ marginLeft: "auto" }}
            onClick={() => { save.reset(); qc.invalidateQueries({ queryKey: ["settings"] }); }}>Reload</button>
        </div>
      )}
      {saveError && saveError.code !== "version_conflict" && (
        <div role="alert" className="alert alert-error"><Icon name="alert" size={18} /><span>{saveError.message}</span></div>
      )}
      {save.error && !saveError && <div role="alert" className="alert alert-error"><Icon name="alert" size={18} />{(save.error as Error).message}</div>}
      {savedVersion !== null && !dirty && (
        <div role="status" className="alert alert-success"><Icon name="check" size={18} />Saved as version {savedVersion}. Services pick up the change automatically.</div>
      )}

      <div className="card card-pad stack">
        {visible.map((spec) => (
          <Field key={spec.key} spec={spec} value={values[spec.key]} error={fieldErrors[spec.key]}
            onChange={(v) => setValues((old) => ({ ...old, [spec.key]: v }))}
            secretSet={Boolean(view.secrets_set[spec.key])} secretEdit={secrets[spec.key]}
            onSecret={(v) => setSecrets((old) => ({ ...old, [spec.key]: v }))} />
        ))}
      </div>

      {test.error && <div role="alert" className="alert alert-error"><Icon name="alert" size={18} />{(test.error as Error).message}</div>}
      {report && !test.isPending && <Report report={report} />}

      <div className="row">
        <button type="button" className="btn op-press" onClick={() => { setReport(null); test.mutate(); }} disabled={test.isPending}>
          {test.isPending ? <><span className="spinner" />Testing…</> : <><Icon name="refresh" size={18} />Test connection</>}
        </button>
        <button type="submit" className="btn btn-primary op-press" disabled={!dirty || save.isPending}>
          {save.isPending ? <span className="spinner" /> : "Save"}
        </button>
        {dirty && <span className="muted" style={{ fontSize: 13 }}>Unsaved changes. Test uses what you typed, plus saved secrets.</span>}
      </div>
    </form>
  );
}

export function IntegrationsPage() {
  const sections = useQuery({ queryKey: ["settings"], queryFn: settingsApi.list });
  const [active, setActive] = useState("mailbox");
  const current = sections.data?.find((s) => s.section.id === active);

  return (
    <>
      <header className="page-header op-enter">
        <div>
          <h1>Integrations</h1>
          <p>Connect the mailbox OpsPilot reads, the Jira project it files incidents in, and the local AI model.</p>
        </div>
      </header>
      {sections.isLoading && <div className="row muted"><span className="spinner" />Loading settings…</div>}
      {sections.error && <div role="alert" className="alert alert-error"><Icon name="alert" size={18} />{(sections.error as Error).message}</div>}
      {sections.data && (
        <div className="integrations op-enter op-d1">
          <div role="tablist" aria-label="Integration" className="tabs">
            {sections.data.map((s) => (
              <button key={s.section.id} type="button" role="tab" aria-selected={s.section.id === active}
                className="tab op-press" onClick={() => setActive(s.section.id)}>
                <span className={`dot ${s.version > 0 ? "dot-good" : "dot-off"}`} aria-hidden="true" />
                {s.section.title}
                <span className="visually-hidden">{s.version > 0 ? "(configured)" : "(not configured)"}</span>
              </button>
            ))}
          </div>
          {current && (
            <section role="tabpanel" aria-label={current.section.title} className="stack" style={{ maxWidth: 760 }}>
              <p style={{ margin: 0, color: "var(--ink-2)" }}>{current.section.description}</p>
              <SectionForm key={current.section.id} view={current} />
            </section>
          )}
        </div>
      )}
    </>
  );
}
