import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { approvalsApi } from "../../api/approvals";
import { AppShell } from "../../components/AppShell";
import { ApproveFixPage } from "./ApproveFixPage";
import { ApproveMessagePage } from "./ApproveMessagePage";
import { ApproveTicketPage } from "./ApproveTicketPage";

const TITLES = { ticket: "Approvals · Create ticket", fix: "Approvals · Production fix", message: "Approvals · Customer message" };

/** One route for every gate: the approval's gate decides which decision screen is shown. */
export function ApprovalPage() {
  const { id = "" } = useParams();
  const query = useQuery({ queryKey: ["approval", id], queryFn: () => approvalsApi.get(id) });
  const gate = query.data?.gate;
  return (
    <AppShell title={gate ? TITLES[gate] : "Approvals"}>
      {query.isLoading ? <div className="row muted"><span className="spinner" />Loading…</div>
        : gate === "fix" ? <ApproveFixPage /> : gate === "message" ? <ApproveMessagePage /> : <ApproveTicketPage />}
    </AppShell>
  );
}
