/**
 * Session state for the SPA. The JWT is in an HttpOnly cookie, invisible to JavaScript; this provider
 * only knows *who* is signed in. It keeps the 30-minute idle rule from the wireframe:
 * activity renews the session, 28 minutes of inactivity shows a warning, 30 minutes signs out.
 */
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { authApi } from "../api/auth";
import { ApiError } from "../api/client";
import type { User } from "../api/types";

const IDLE_LIMIT_MS = 30 * 60_000;
const WARN_AT_MS = 28 * 60_000;
const RENEW_EVERY_MS = 5 * 60_000;

interface SessionContextValue {
  user: User | null;
  loading: boolean;
  idleWarning: boolean;
  setUser: (user: User | null) => void;
  signOut: (reason?: "user" | "timeout") => Promise<void>;
  stayActive: () => void;
}

const SessionContext = createContext<SessionContextValue | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [idleWarning, setIdleWarning] = useState(false);
  const lastActivity = useRef(Date.now());
  const lastRenewal = useRef(Date.now());
  const navigate = useNavigate();

  useEffect(() => {
    authApi
      .me()
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setLoading(false));
  }, []);

  const signOut = useCallback(
    async (reason: "user" | "timeout" = "user") => {
      await authApi.signOut(reason).catch(() => undefined);
      setUser(null);
      setIdleWarning(false);
      navigate(reason === "timeout" ? "/sign-in?ended=timeout" : "/sign-in", { replace: true });
    },
    [navigate],
  );

  const stayActive = useCallback(() => {
    lastActivity.current = Date.now();
    setIdleWarning(false);
  }, []);

  useEffect(() => {
    if (!user) return;
    lastActivity.current = Date.now();
    lastRenewal.current = Date.now();
    const onActivity = () => {
      lastActivity.current = Date.now();
    };
    const events = ["pointerdown", "keydown", "scroll"] as const;
    events.forEach((e) => window.addEventListener(e, onActivity, { passive: true }));

    const timer = window.setInterval(async () => {
      const idle = Date.now() - lastActivity.current;
      if (idle >= IDLE_LIMIT_MS) {
        await signOut("timeout");
        return;
      }
      setIdleWarning(idle >= WARN_AT_MS);
      if (idle < RENEW_EVERY_MS && Date.now() - lastRenewal.current >= RENEW_EVERY_MS) {
        try {
          const session = await authApi.renew();
          lastRenewal.current = Date.now();
          setUser(session.user);
        } catch (err) {
          if (err instanceof ApiError && err.status === 401) await signOut("timeout");
        }
      }
    }, 15_000);

    return () => {
      events.forEach((e) => window.removeEventListener(e, onActivity));
      window.clearInterval(timer);
    };
  }, [user, signOut]);

  return (
    <SessionContext.Provider value={{ user, loading, idleWarning, setUser, signOut, stayActive }}>
      {children}
    </SessionContext.Provider>
  );
}

export function useSession(): SessionContextValue {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession must be used inside <SessionProvider>");
  return ctx;
}
