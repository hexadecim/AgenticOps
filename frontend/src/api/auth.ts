import { api } from "./client";
import type { Role, SessionInfo, SignInChallenge, User } from "./types";

export const authApi = {
  signIn: (email: string, password: string) =>
    api<SignInChallenge>("/auth/sign-in", { method: "POST", body: { email, password } }),
  verifyCode: (challenge_token: string, code: string, new_password?: string) =>
    api<SessionInfo>("/auth/sign-in/mfa", { method: "POST", body: { challenge_token, code, new_password } }),
  renew: () => api<SessionInfo>("/auth/renew", { method: "POST" }),
  me: () => api<User>("/auth/me"),
  signOut: (reason: "user" | "timeout" = "user") => api<void>(`/auth/sign-out?reason=${reason}`, { method: "POST" }),
};

export const usersApi = {
  list: () => api<User[]>("/admin/users"),
  create: (body: { email: string; name: string; roles: Role[]; temporary_password: string }) =>
    api<User>("/admin/users", { method: "POST", body }),
  update: (id: string, body: { name?: string; roles?: Role[]; active?: boolean }) =>
    api<User>(`/admin/users/${id}`, { method: "PATCH", body }),
  resetMfa: (id: string) => api<User>(`/admin/users/${id}/reset-mfa`, { method: "POST" }),
  resetPassword: (id: string, temporary_password: string) =>
    api<User>(`/admin/users/${id}/reset-password`, { method: "POST", body: { temporary_password } }),
};

/** Generates a readable temporary password that satisfies the server policy. */
export function generateTemporaryPassword(): string {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789";
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  const chars = Array.from(bytes, (b) => alphabet[b % alphabet.length]).join("");
  return `${chars.slice(0, 4)}-${chars.slice(4, 8)}-${chars.slice(8, 12)}-${chars.slice(12, 16)}`;
}
