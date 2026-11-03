export type Role = "l1_analyst" | "support_agent" | "support_manager" | "compliance_lead" | "admin";

export const ROLE_LABELS: Record<Role, string> = {
  l1_analyst: "L1 Analyst",
  support_agent: "Support agent",
  support_manager: "Support manager",
  compliance_lead: "Compliance lead",
  admin: "Admin",
};

export const ROLE_HELP: Record<Role, string> = {
  l1_analyst: "Approves Jira tickets and customer messages",
  support_agent: "Also approves production fixes (T2)",
  support_manager: "Approves tickets and messages; sees the dashboard",
  compliance_lead: "Audit log and GDPR requests",
  admin: "Users and integration settings",
};

export const ALL_ROLES = Object.keys(ROLE_LABELS) as Role[];

export interface User {
  id: string;
  email: string;
  name: string;
  roles: Role[];
  active: boolean;
  mfa_enrolled: boolean;
  password_change_required: boolean;
  locked: boolean;
  created_at: string;
  last_login_at: string | null;
}

export interface SignInChallenge {
  challenge_token: string;
  name: string;
  mfa_enrolled: boolean;
  enrolment: { secret: string; otpauth_uri: string } | null;
  password_change_required: boolean;
}

export interface SessionInfo {
  user: User;
  expires_in: number;
}

export function roleLabel(user: Pick<User, "roles">): string {
  const order: Role[] = ["admin", "compliance_lead", "support_manager", "support_agent", "l1_analyst"];
  const top = order.find((r) => user.roles.includes(r));
  return top ? ROLE_LABELS[top] : "No role";
}
