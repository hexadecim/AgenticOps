import { useQuery } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { mailApi } from "../api/mail";
import { roleLabel } from "../api/types";
import { useSession } from "../auth/session";
import { Icon, type IconName } from "./Icon";
import { ThemeSwitch } from "./ThemeSwitch";
import "./shell.css";

interface NavItem {
  to: string;
  label: string;
  icon: IconName;
  soon?: string;
  adminOnly?: boolean;
}

const NAV: NavItem[] = [
  { to: "/approvals", label: "Approvals", icon: "approvals" },
  { to: "/dashboard", label: "Dashboard", icon: "dashboard" },
  { to: "/audit", label: "Audit log", icon: "audit" },
  { to: "/privacy", label: "Privacy and data requests", icon: "shield", soon: "M8" },
  { to: "/admin/users", label: "Users and roles", icon: "users", adminOnly: true },
  { to: "/admin/integrations", label: "Integrations", icon: "key", adminOnly: true },
  { to: "/admin/system", label: "System health", icon: "refresh", adminOnly: true },
];

/** "Mailbox polled N min ago"; amber when the provider is paused, failing or polls have gone stale. */
function MailboxRow() {
  const status = useQuery({ queryKey: ["mail-status"], queryFn: mailApi.status, refetchInterval: 30_000, retry: false });
  if (status.isError) {
    return <div><span>Mailbox</span><span className="status-warn"><Icon name="alert" size={14} />Status unavailable</span></div>;
  }
  const s = status.data;
  if (!s) return <div><span>Mailbox</span><span>…</span></div>;
  const text = s.message.replace(/^Mailbox /, "");
  return (
    <div>
      <span>Mailbox</span>
      {s.level === "warning" ? (
        <span className="status-warn" title={s.last_error ?? undefined} role="status">
          <Icon name="alert" size={14} />{text.charAt(0).toUpperCase() + text.slice(1)}
        </span>
      ) : (
        <span title={s.folder ? `${s.provider?.toUpperCase()} · ${s.folder}` : undefined}>
          {text.charAt(0).toUpperCase() + text.slice(1)}
        </span>
      )}
    </div>
  );
}

export function AppShell({ title, children }: { title: string; children: ReactNode }) {
  const { user, signOut, idleWarning, stayActive } = useSession();
  const [open, setOpen] = useState(false);
  const location = useLocation();

  useEffect(() => setOpen(false), [location.pathname]);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  if (!user) return null;
  const isAdmin = user.roles.includes("admin");
  const role = roleLabel(user);

  return (
    <div className="shell">
      <header className="topbar">
        <button type="button" className="burger op-press" aria-label="Open menu" aria-expanded={open}
          aria-controls="op-drawer" onClick={() => setOpen(true)}>
          <span /><span /><span />
        </button>
        <NavLink to="/" className="brand">OpsPilot</NavLink>
        <span className="crumb-sep" aria-hidden="true">/</span>
        <span className="crumb">{title}</span>
        <div className="spacer" />
        <span className="pill"><Icon name="shield" size={16} />EU data region</span>
        <span className="topbar-role">{user.name} · {role}</span>
      </header>

      <button type="button" tabIndex={-1} aria-label="Close menu" className="scrim"
        style={{ opacity: open ? 1 : 0, pointerEvents: open ? "auto" : "none" }} onClick={() => setOpen(false)} />
      <nav id="op-drawer" aria-label="Main" className={`drawer ${open ? "open" : ""}`}>
        <div className="drawer-head">
          <div><div className="drawer-title">OpsPilot</div><div className="drawer-sub">Support console</div></div>
          <button type="button" className="burger op-press" aria-label="Close menu" onClick={() => setOpen(false)}>
            <Icon name="close" />
          </button>
        </div>
        {NAV.filter((n) => !n.adminOnly || isAdmin).map((n) => (
          <NavLink key={n.to} to={n.to} className={({ isActive }) => `navlink ${isActive ? "active" : ""}`}>
            <Icon name={n.icon} />
            {n.label}
            {n.soon && <span className="soon">{n.soon}</span>}
          </NavLink>
        ))}
        <div className="grow" />
        <div style={{ display: "flex", flexDirection: "column", gap: 8, padding: "4px 6px 10px" }}>
          <span className="drawer-label">Appearance</span>
          <ThemeSwitch variant="chrome" />
        </div>
        <div className="status-card">
          <div><span>Mode</span><span>Assisted</span></div>
          <div><span>Model</span><span className="mono" style={{ fontSize: 12 }}>deepseek-r1:8b · local</span></div>
          <MailboxRow />
          <div><span>Data region</span><span>EU only</span></div>
          <div><span>Signed in</span><span>{role}</span></div>
        </div>
        <button type="button" className="navlink" style={{ fontSize: 15 }} onClick={() => signOut("user")}>
          <Icon name="signOut" />Sign out
        </button>
        <div className="drawer-links">
          <a href="#privacy">Privacy notice</a><a href="#imprint">Imprint</a><a href="#a11y">Accessibility statement</a>
        </div>
      </nav>

      <main className="main">{children}</main>

      {idleWarning && (
        <div role="alertdialog" aria-live="assertive" aria-label="Session ending" className="idle-banner">
          <Icon name="clock" />
          <span>You will be signed out in about 2 minutes because there has been no activity.</span>
          <button type="button" className="btn btn-primary btn-sm op-press" onClick={stayActive}>Stay signed in</button>
        </div>
      )}
    </div>
  );
}
