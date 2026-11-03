import { api } from "./client";

export interface MailboxStatus {
  level: "ok" | "warning" | "off";
  message: string;
  configured: boolean;
  provider: "imap" | "gmail" | "graph" | null;
  folder: string | null;
  poll_interval_seconds: number | null;
  last_poll_at: string | null;
  last_success_at: string | null;
  last_error: string | null;
  circuit: "closed" | "open" | "half_open";
  total_received: number;
}

export const mailApi = {
  status: () => api<MailboxStatus>("/mail/status"),
};
