import { api } from "./client";

export interface DependencyHealth {
  name: string;
  label: string;
  health: "up" | "down" | "recovering";
  since: string | null;
  callers: Record<string, string>;
}

/** A health probe of the supported systems (ops-service), latest state. */
export interface ProbeHealth {
  name: string;
  region: string | null;
  health: "up" | "down";
  detail: string;
  latency_ms: number | null;
  since: string | null;
}

export interface TimelineEntry { at: string; actor: string; text: string; system: boolean }

export interface Incident {
  key: string;
  title: string | null;
  severity: string | null;
  site: string | null;
  reports: number;
  first_seen: string | null;
  status: string;
}

export interface DashboardData {
  generated_at: string;
  window_hours: number;
  dependencies: DependencyHealth[];
  probes: ProbeHealth[];
  stages: { id: string; label: string; count: number | null; note: string }[];
  service_levels: { id: string; label: string; value: string; detail: string; breached: boolean }[];
  incidents: Incident[];
  timeline: { key: string | null; entries: TimelineEntry[] };
}

export interface AuditEntry {
  id: string;
  time: string;
  kind: "human" | "tool" | "privacy" | "system";
  actor: string;
  event: string;
  detail: string;
  ticket: string | null;
  privacy: string;
  thread_id: string | null;
}

export const reportingApi = {
  dashboard: () => api<DashboardData>("/dashboard"),
  timeline: (key: string) => api<{ key: string; entries: TimelineEntry[] }>(`/incidents/${encodeURIComponent(key)}/timeline`),
  audit: (params: { kind?: string; q?: string; cursor?: string }) => {
    const query = new URLSearchParams(Object.entries(params).filter(([, v]) => v) as [string, string][]);
    return api<{ entries: AuditEntry[]; next_cursor: string | null }>(`/audit/entries?${query.toString()}`);
  },
};
