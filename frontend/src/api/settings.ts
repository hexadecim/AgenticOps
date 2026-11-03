import { api } from "./client";

export type FieldKind = "text" | "secret" | "number" | "bool" | "select" | "url" | "email" | "host";

export interface FieldSpec {
  key: string;
  label: string;
  kind: FieldKind;
  required: boolean;
  default: unknown;
  help: string;
  placeholder: string;
  minimum: number | null;
  maximum: number | null;
  options: { value: string; label: string }[];
  when: Record<string, string>;
}

export interface SectionView {
  section: { id: string; title: string; description: string; fields: FieldSpec[] };
  version: number;
  values: Record<string, unknown>;
  secrets_set: Record<string, boolean>;
  updated_at: string | null;
  updated_by: string | null;
}

export interface ConnectionReport {
  ok: boolean;
  summary: string;
  duration_ms: number;
  checks: { name: string; ok: boolean; detail: string }[];
}

export interface Circuit {
  dependency: string;
  state: "closed" | "open" | "half_open" | "unused";
  failures: number;
  retry_after_seconds: number | null;
  fail_max: number;
  reset_timeout_seconds: number;
  timeout_seconds: number;
  retry_attempts: number;
  bulkhead: number;
}

/** Secrets: undefined = keep, "" = remove, text = replace. */
export type SecretEdits = Record<string, string | undefined>;

export const settingsApi = {
  list: () => api<SectionView[]>("/admin/settings"),
  save: (id: string, version: number, values: Record<string, unknown>, secrets: SecretEdits) =>
    api<SectionView>(`/admin/settings/${id}`, { method: "PUT", body: { version, values, secrets } }),
  test: (id: string, values: Record<string, unknown>, secrets: SecretEdits) =>
    api<ConnectionReport>(`/admin/settings/${id}/test`, { method: "POST", body: { values, secrets } }),
  circuits: () => api<Circuit[]>("/admin/system/circuits"),
};

export function applies(field: FieldSpec, values: Record<string, unknown>): boolean {
  return Object.entries(field.when).every(([k, v]) => String(values[k]) === v);
}
