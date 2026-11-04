import { api } from "./client";

export type DsrType = "access" | "rectification" | "erasure" | "restriction" | "objection";
export type DsrStatus = "open" | "sending" | "erasing" | "confirming" | "send_failed" | "completed";

export interface DataRequest {
  id: string;
  type: DsrType;
  type_label: string;
  article: string;
  status: DsrStatus;
  /** A token: the address never comes back from the server. Null when it is not in the vault. */
  who: string | null;
  received_on: string;
  due_on: string;
  due: string;
  due_level: "ok" | "soon" | "late" | "closed";
  verified: boolean;
  verified_how: string | null;
  pending_stores: string[];
  outcome: string | null;
  closed_at: string | null;
}

export interface StoreRow {
  store: string;
  label: string;
  what: string;
  count: number;
  erase_action: string;
  available: boolean;
}

export interface RequestDetail extends DataRequest { stores: StoreRow[] }

export interface RequestList {
  requests: DataRequest[];
  open: number;
  retention: { what: string; keep: string }[];
}

const base = "/privacy/requests";
export const privacyApi = {
  list: () => api<RequestList>(base),
  get: (id: string) => api<RequestDetail>(`${base}/${id}`),
  log: (body: { type: DsrType; address: string; received_on?: string }) =>
    api<DataRequest>(base, { method: "POST", body }),
  verify: (id: string, how: string) => api<DataRequest>(`${base}/${id}/verify`, { method: "POST", body: { how } }),
  close: (id: string, outcome: string) => api<DataRequest>(`${base}/${id}/close`, { method: "POST", body: { outcome } }),
  exportData: (id: string) => api<DataRequest>(`${base}/${id}/export`, { method: "POST" }),
  erase: (id: string) => api<DataRequest>(`${base}/${id}/erase`, { method: "POST" }),
  retry: (id: string) => api<DataRequest>(`${base}/${id}/retry`, { method: "POST" }),
};
