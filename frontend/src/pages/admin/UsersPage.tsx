import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent, type ReactNode } from "react";
import { generateTemporaryPassword, usersApi } from "../../api/auth";
import { ApiError } from "../../api/client";
import { ALL_ROLES, ROLE_HELP, ROLE_LABELS, type Role, type User } from "../../api/types";
import { useSession } from "../../auth/session";
import { Icon } from "../../components/Icon";

const fmt = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

function StatusBadges({ user }: { user: User }) {
  return (
    <span className="row" style={{ gap: 6 }}>
      {!user.active ? <span className="badge badge-neutral">Deactivated</span>
        : user.locked ? <span className="badge badge-crit"><Icon name="lock" size={14} />Locked</span>
        : <span className="badge badge-good">Active</span>}
      {user.active && !user.mfa_enrolled && <span className="badge badge-warn">Authenticator not set up</span>}
      {user.active && user.password_change_required && <span className="badge badge-accent">Temporary password</span>}
    </span>
  );
}

function RolePicker({ value, onChange }: { value: Role[]; onChange: (roles: Role[]) => void }) {
  return (
    <fieldset style={{ border: 0, padding: 0, margin: 0 }} className="field">
      <legend className="label" style={{ marginBottom: 6 }}>Roles</legend>
      <div className="stack" style={{ gap: 8 }}>
        {ALL_ROLES.map((role) => (
          <label key={role} className="check">
            <input type="checkbox" checked={value.includes(role)}
              onChange={(e) => onChange(e.target.checked ? [...value, role] : value.filter((r) => r !== role))} />
            <span style={{ display: "flex", flexDirection: "column" }}>
              <span style={{ fontWeight: 500 }}>{ROLE_LABELS[role]}</span>
              <span className="muted" style={{ fontSize: 13 }}>{ROLE_HELP[role]}</span>
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

function TempPasswordNotice({ password, email }: { password: string; email: string }) {
  return (
    <div className="alert alert-success" role="status" style={{ flexDirection: "column", gap: 6 }}>
      <strong>Share this temporary password with {email} through a separate channel.</strong>
      <span className="mono" style={{ fontSize: 16, color: "var(--ink)" }}>{password}</span>
      <span>It is shown only now. They will choose their own password and set up an authenticator at first sign-in.</span>
    </div>
  );
}

function CreateUserPanel({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [roles, setRoles] = useState<Role[]>(["l1_analyst"]);
  const [temp] = useState(generateTemporaryPassword);
  const create = useMutation({
    mutationFn: () => usersApi.create({ email, name, roles, temporary_password: temp }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["users"] }),
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    create.mutate();
  }

  return (
    <Panel title="Add a user" onClose={onClose}>
      {create.isSuccess ? (
        <>
          <TempPasswordNotice password={temp} email={create.data.email} />
          <button type="button" className="btn btn-primary op-press" onClick={onClose}>Done</button>
        </>
      ) : (
        <form className="stack" onSubmit={submit}>
          {create.error && <div role="alert" className="alert alert-error"><Icon name="alert" size={18} />{(create.error as ApiError).message}</div>}
          <div className="field">
            <label htmlFor="u-name">Full name</label>
            <input id="u-name" className="input" value={name} onChange={(e) => setName(e.target.value)} required />
          </div>
          <div className="field">
            <label htmlFor="u-email">Work email</label>
            <input id="u-email" className="input" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
          </div>
          <RolePicker value={roles} onChange={setRoles} />
          <p className="muted" style={{ margin: 0, fontSize: 13 }}>A temporary password is generated for you and shown once after saving.</p>
          <div className="row">
            <button type="submit" className="btn btn-primary op-press" disabled={create.isPending || !roles.length || !name || !email}>
              {create.isPending ? <span className="spinner" /> : "Add user"}
            </button>
            <button type="button" className="btn op-press" onClick={onClose}>Cancel</button>
          </div>
        </form>
      )}
    </Panel>
  );
}

function EditUserPanel({ user, onClose }: { user: User; onClose: () => void }) {
  const qc = useQueryClient();
  const { user: me } = useSession();
  const [name, setName] = useState(user.name);
  const [roles, setRoles] = useState<Role[]>(user.roles);
  const [tempPassword, setTempPassword] = useState<string | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["users"] });
  const save = useMutation({ mutationFn: () => usersApi.update(user.id, { name, roles }), onSuccess: refresh });
  const toggle = useMutation({ mutationFn: () => usersApi.update(user.id, { active: !user.active }), onSuccess: () => { refresh(); onClose(); } });
  const resetMfa = useMutation({ mutationFn: () => usersApi.resetMfa(user.id), onSuccess: refresh });
  const resetPassword = useMutation({
    mutationFn: (pw: string) => usersApi.resetPassword(user.id, pw),
    onSuccess: (_, pw) => { setTempPassword(pw); refresh(); },
  });
  const error = [save, toggle, resetMfa, resetPassword].map((m) => m.error).find(Boolean) as ApiError | undefined;
  const isMe = me?.id === user.id;

  return (
    <Panel title={user.name} subtitle={user.email} onClose={onClose}>
      {error && <div role="alert" className="alert alert-error"><Icon name="alert" size={18} />{error.message}</div>}
      {save.isSuccess && !save.isPending && <div role="status" className="alert alert-success"><Icon name="check" size={18} />Saved. New roles apply at their next session renewal (within 5 minutes).</div>}
      {resetMfa.isSuccess && <div role="status" className="alert alert-success"><Icon name="check" size={18} />Authenticator reset. They will scan a new code at next sign-in.</div>}
      {tempPassword && <TempPasswordNotice password={tempPassword} email={user.email} />}

      <form className="stack" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
        <div className="field">
          <label htmlFor="e-name">Full name</label>
          <input id="e-name" className="input" value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <RolePicker value={roles} onChange={setRoles} />
        <div><button type="submit" className="btn btn-primary op-press" disabled={save.isPending || !roles.length}>Save changes</button></div>
      </form>

      <div className="card card-pad stack" style={{ gap: 12 }}>
        <strong>Account recovery</strong>
        <div className="row">
          <button type="button" className="btn btn-sm op-press" onClick={() => resetPassword.mutate(generateTemporaryPassword())} disabled={resetPassword.isPending}>
            <Icon name="key" size={16} />Reset password
          </button>
          <button type="button" className="btn btn-sm op-press" onClick={() => resetMfa.mutate()} disabled={resetMfa.isPending || !user.mfa_enrolled}>
            <Icon name="refresh" size={16} />Reset authenticator
          </button>
        </div>
        <span className="muted" style={{ fontSize: 13 }}>Resetting the password also lifts a lockout. Every action is recorded in the audit log under your name.</span>
      </div>

      <div className="card card-pad stack" style={{ gap: 12 }}>
        <strong>{user.active ? "Deactivate account" : "Reactivate account"}</strong>
        <span className="muted" style={{ fontSize: 13 }}>
          {user.active ? "They can no longer sign in, and any open session ends within 5 minutes. Their past decisions stay in the audit log." : "They can sign in again with their existing password and authenticator."}
        </span>
        <div>
          <button type="button" className={`btn btn-sm op-press ${user.active ? "btn-danger" : ""}`} onClick={() => toggle.mutate()}
            disabled={toggle.isPending || isMe} title={isMe ? "You cannot deactivate yourself" : undefined}>
            {user.active ? "Deactivate" : "Reactivate"}
          </button>
        </div>
      </div>
    </Panel>
  );
}

function Panel({ title, subtitle, onClose, children }: { title: string; subtitle?: string; onClose: () => void; children: ReactNode }) {
  return (
    <>
      <div className="panel-scrim" onClick={onClose} aria-hidden="true" />
      <section className="panel" role="dialog" aria-modal="true" aria-label={title}>
        <div className="row" style={{ justifyContent: "space-between", alignItems: "flex-start" }}>
          <div>
            <h2>{title}</h2>
            {subtitle && <div className="muted" style={{ fontSize: 14 }}>{subtitle}</div>}
          </div>
          <button type="button" className="btn btn-sm op-press" aria-label="Close" onClick={onClose}><Icon name="close" size={16} /></button>
        </div>
        {children}
      </section>
    </>
  );
}

export function UsersPage() {
  const users = useQuery({ queryKey: ["users"], queryFn: usersApi.list });
  const [creating, setCreating] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const editing = users.data?.find((u) => u.id === editingId) ?? null;

  return (
    <>
      <header className="page-header op-enter">
        <div>
          <h1>Users and roles</h1>
          <p>Who can sign in, and which approval gates each person may decide.</p>
        </div>
        <button type="button" className="btn btn-primary op-press" onClick={() => setCreating(true)}>
          <Icon name="plus" size={18} />Add user
        </button>
      </header>

      <section className="card op-enter op-d1" aria-label="Users" style={{ overflow: "hidden" }}>
        {users.isLoading && <div className="card-pad muted row"><span className="spinner" />Loading users…</div>}
        {users.error && <div className="card-pad"><div role="alert" className="alert alert-error"><Icon name="alert" size={18} />{(users.error as ApiError).message}</div></div>}
        {users.data && (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr><th>Person</th><th>Roles</th><th>Status</th><th>Last sign-in</th><th><span className="visually-hidden">Actions</span></th></tr>
              </thead>
              <tbody>
                {users.data.map((u) => (
                  <tr key={u.id} className="op-row">
                    <td>
                      <div style={{ fontWeight: 500, fontSize: 15 }}>{u.name}</div>
                      <div className="muted" style={{ fontSize: 13 }}>{u.email}</div>
                    </td>
                    <td><span className="row" style={{ gap: 6 }}>{u.roles.map((r) => <span key={r} className="badge badge-accent">{ROLE_LABELS[r]}</span>)}</span></td>
                    <td><StatusBadges user={u} /></td>
                    <td className="muted">{u.last_login_at ? fmt.format(new Date(u.last_login_at)) : "Never"}</td>
                    <td style={{ textAlign: "right" }}>
                      <button type="button" className="btn btn-sm op-press" onClick={() => setEditingId(u.id)} aria-label={`Manage ${u.name}`}>Manage</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {creating && <CreateUserPanel onClose={() => setCreating(false)} />}
      {editing && <EditUserPanel key={editing.id} user={editing} onClose={() => setEditingId(null)} />}
    </>
  );
}
