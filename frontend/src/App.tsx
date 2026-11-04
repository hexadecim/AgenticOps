import type { ReactNode } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import type { Role } from "./api/types";
import { useSession } from "./auth/session";
import { AppShell } from "./components/AppShell";
import { IntegrationsPage } from "./pages/admin/IntegrationsPage";
import { SystemHealthPage } from "./pages/admin/SystemHealthPage";
import { UsersPage } from "./pages/admin/UsersPage";
import { LegalPage } from "./pages/legal/LegalPage";
import { PrivacyPage } from "./pages/privacy/PrivacyPage";
import { ApprovalsPage } from "./pages/approvals/ApprovalsPage";
import { AuditPage } from "./pages/audit/AuditPage";
import { DashboardPage } from "./pages/dashboard/DashboardPage";
import { ApprovalPage } from "./pages/approvals/ApprovalPage";
import { LoginPage } from "./pages/LoginPage";

function Protected({ title, role, children, shell = true }: { title: string; role?: Role; children: ReactNode; shell?: boolean }) {
  const { user, loading } = useSession();
  const location = useLocation();
  if (loading) return <div className="row muted" style={{ padding: 40 }}><span className="spinner" />Loading…</div>;
  if (!user) return <Navigate to="/sign-in" replace state={{ from: location.pathname }} />;
  if (role && !user.roles.includes(role)) {
    return (
      <AppShell title={title}>
        <div role="alert" className="alert alert-warn">Your role cannot open this page. Ask an admin if you need access.</div>
      </AppShell>
    );
  }
  return shell ? <AppShell title={title}>{children}</AppShell> : <>{children}</>;
}

export function App() {
  const { user, loading } = useSession();
  return (
    <Routes>
      <Route path="/sign-in" element={!loading && user ? <Navigate to="/" replace /> : <LoginPage />} />
      <Route path="/" element={<Navigate to={user?.roles.includes("admin") ? "/admin/users" : "/approvals"} replace />} />
      <Route path="/approvals" element={<Protected title="Approvals"><ApprovalsPage /></Protected>} />
      <Route path="/approvals/:id" element={<Protected title="Approvals" shell={false}><ApprovalPage /></Protected>} />
      <Route path="/dashboard" element={<Protected title="Dashboard"><DashboardPage /></Protected>} />
      <Route path="/audit" element={<Protected title="Audit log"><AuditPage /></Protected>} />
      <Route path="/privacy" element={<Protected title="Privacy and data requests"><PrivacyPage /></Protected>} />
      <Route path="/legal/:page" element={<LegalPage />} />
      <Route path="/admin/users" element={<Protected title="Users and roles" role="admin"><UsersPage /></Protected>} />
      <Route path="/admin/integrations" element={<Protected title="Integrations" role="admin"><IntegrationsPage /></Protected>} />
      <Route path="/admin/system" element={<Protected title="System health" role="admin"><SystemHealthPage /></Protected>} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
