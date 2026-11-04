import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

const TYPES = [
  "approval.requested", "approval.decided", "run.status_changed",
  "jira.issue.created", "jira.issue.linked", "jira.issue.failed", "breaker.state_changed",
  "action.executed", "approval.reminder", "probe.status", "message.sent", "message.failed",
];

/** Live updates: the gateway relays event types and ids only; we refetch what the page shows. */
export function useLiveEvents(): boolean {
  const qc = useQueryClient();
  const [connected, setConnected] = useState(false);
  useEffect(() => {
    const source = new EventSource("/api/events", { withCredentials: true });
    const refresh = (e: MessageEvent<string>) => {
      const data = JSON.parse(e.data) as { approval_id?: string };
      void qc.invalidateQueries({ queryKey: ["approvals"] });
      void qc.invalidateQueries({ queryKey: ["runs-summary"] });
      void qc.invalidateQueries({ queryKey: ["dashboard"] });
      void qc.invalidateQueries({ queryKey: ["timeline"] });
      if (data.approval_id) {
        void qc.invalidateQueries({ queryKey: ["approval", data.approval_id] });
        void qc.invalidateQueries({ queryKey: ["evidence", data.approval_id] });
      } else if (e.type === "run.status_changed") {
        // No approval id on run events, but a fix screen shows the run's health re-check: refresh open ones.
        void qc.invalidateQueries({ queryKey: ["approval"] });
      }
    };
    TYPES.forEach((t) => source.addEventListener(t, refresh as EventListener));
    source.onopen = () => setConnected(true);
    source.onerror = () => setConnected(false); // the browser reconnects by itself
    return () => source.close();
  }, [qc]);
  return connected;
}

/** Re-render every `ms` so "waiting 3 min" stays true. */
export function useNow(ms = 30_000): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(id);
  }, [ms]);
  return now;
}
