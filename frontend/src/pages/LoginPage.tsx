import QRCode from "qrcode";
import { useEffect, useRef, useState, type FormEvent } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { authApi } from "../api/auth";
import { ApiError } from "../api/client";
import type { SignInChallenge } from "../api/types";
import { useSession } from "../auth/session";
import { Icon } from "../components/Icon";
import { ThemeSwitch } from "../components/ThemeSwitch";
import "./login.css";

const COOKIE_KEY = "opspilot-cookie-notice";

function messageFor(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  return "Something went wrong. Try again.";
}

export function LoginPage() {
  const { setUser } = useSession();
  const navigate = useNavigate();
  const location = useLocation();
  const [params] = useSearchParams();
  const [step, setStep] = useState<"password" | "code">("password");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [challenge, setChallenge] = useState<SignInChallenge | null>(null);
  const [qr, setQr] = useState<string | null>(null);
  const [error, setError] = useState(
    params.get("ended") === "timeout" ? "You were signed out after 30 minutes without activity." : "",
  );
  const [busy, setBusy] = useState(false);
  const [lang, setLang] = useState("en");
  const [showCookie, setShowCookie] = useState(() => {
    try {
      return localStorage.getItem(COOKIE_KEY) !== "seen";
    } catch {
      return true;
    }
  });
  const codeRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (challenge?.enrolment) {
      QRCode.toDataURL(challenge.enrolment.otpauth_uri, { margin: 0, width: 272 }).then(setQr, () => setQr(null));
    } else {
      setQr(null);
    }
  }, [challenge]);

  useEffect(() => {
    if (step === "code" && !challenge?.password_change_required) codeRef.current?.focus();
  }, [step, challenge]);

  async function submitPassword(e: FormEvent) {
    e.preventDefault();
    if (!email.includes("@") || !password) {
      setError("Enter your work email and password.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      setChallenge(await authApi.signIn(email, password));
      setStep("code");
      setPassword("");
    } catch (err) {
      setError(messageFor(err));
    } finally {
      setBusy(false);
    }
  }

  async function submitCode(e: FormEvent) {
    e.preventDefault();
    if (!challenge) return;
    if (!/^\d{6}$/.test(code)) {
      setError("Enter the 6-digit code from your authenticator app.");
      return;
    }
    if (challenge.password_change_required && newPassword !== confirmPassword) {
      setError("The two new passwords do not match.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const session = await authApi.verifyCode(
        challenge.challenge_token,
        code,
        challenge.password_change_required ? newPassword : undefined,
      );
      setUser(session.user);
      const from = (location.state as { from?: string } | null)?.from;
      navigate(from && from !== "/sign-in" ? from : "/", { replace: true });
    } catch (err) {
      setCode("");
      if (err instanceof ApiError && (err.code === "challenge_expired" || err.code === "account_locked")) {
        backToPassword();
      }
      setError(messageFor(err));
    } finally {
      setBusy(false);
    }
  }

  function backToPassword() {
    setStep("password");
    setChallenge(null);
    setCode("");
    setNewPassword("");
    setConfirmPassword("");
  }

  function dismissCookie() {
    try {
      localStorage.setItem(COOKIE_KEY, "seen");
    } catch {
      /* ignore */
    }
    setShowCookie(false);
  }

  return (
    <div className="login">
      <aside className="login-aside op-fadein">
        <div>
          <div style={{ fontSize: 24, fontWeight: 600, letterSpacing: "-0.01em" }}>OpsPilot</div>
          <div style={{ fontSize: 14, color: "var(--chrome-muted)" }}>Support console</div>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 28 }}>
          <h1 className="op-enter op-d2">Agents do the legwork.<br />People make the calls.</h1>
          <ul className="op-enter op-d3">
            <li><Icon name="shield" />Every approval and rejection is recorded under your name in the audit log.</li>
            <li><Icon name="lock" />You see pseudonymised emails; real names appear only in messages you approve.</li>
            <li><Icon name="clock" />Sessions end after 30 minutes without activity.</li>
          </ul>
        </div>
        <div style={{ fontSize: 13, color: "var(--chrome-muted)" }}>Local AI model · data stays in the EU · privacy by design</div>
      </aside>

      <main className="login-main">
        <div className="login-bar op-fadein">
          <label htmlFor="ui-lang">Language</label>
          <select id="ui-lang" value={lang} onChange={(e) => setLang(e.target.value)}>
            <option value="en">English</option><option value="de">Deutsch</option><option value="fr">Français</option>
            <option value="es">Español</option><option value="it">Italiano</option><option value="nl">Nederlands</option>
            <option value="pl">Polski</option>
          </select>
          <ThemeSwitch />
        </div>
        {lang !== "en" && (
          <div role="status" className="alert alert-info op-enter" style={{ alignSelf: "flex-end", margin: "0 32px" }}>
            Release 1 is available in English only. Other EU languages will follow.
          </div>
        )}

        <div className="login-center">
          {step === "password" ? (
            <form className="login-form" onSubmit={submitPassword} noValidate>
              <div className="op-enter op-d1" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <h2>Sign in</h2>
                <p className="lead">Use your work account. Your role decides which approvals you see.</p>
              </div>
              {error && <div role="alert" className="alert alert-error op-enter"><Icon name="alert" size={18} /><span>{error}</span></div>}
              <div className="stack op-enter op-d2">
                <div className="field">
                  <label htmlFor="login-email">Work email</label>
                  <input id="login-email" className="input" type="email" autoComplete="username" placeholder="name@company.com"
                    value={email} onChange={(e) => setEmail(e.target.value)} required />
                </div>
                <div className="field">
                  <label htmlFor="login-password">Password</label>
                  <input id="login-password" className="input" type="password" autoComplete="current-password"
                    value={password} onChange={(e) => setPassword(e.target.value)} required />
                </div>
              </div>
              <button type="submit" className="btn btn-primary btn-block op-press" disabled={busy}>
                {busy ? <span className="spinner" aria-label="Signing in" /> : "Sign in"}
              </button>
              <div className="divider"><span />or<span /></div>
              <button type="button" className="btn btn-block op-press" disabled title="Not configured yet">
                Sign in with company single sign-on
              </button>
              <p className="muted" style={{ margin: 0, fontSize: 13 }}>
                Single sign-on is not configured yet. Forgot your password or need a role changed? Ask your admin.
              </p>
            </form>
          ) : (
            <form className="login-form" onSubmit={submitCode} noValidate>
              <div className="op-enter op-d1" style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <h2>{challenge?.enrolment ? "Set up your authenticator" : "Check your authenticator"}</h2>
                <p className="lead">
                  {challenge?.enrolment
                    ? <>Hi {challenge.name}. Approvers need a second factor because they can change production. Scan this once with your authenticator app.</>
                    : <>Enter the 6-digit code for <strong style={{ color: "var(--ink)", fontWeight: 500 }}>{email}</strong>.</>}
                </p>
              </div>
              {error && <div role="alert" className="alert alert-error op-enter"><Icon name="alert" size={18} /><span>{error}</span></div>}

              {challenge?.enrolment && (
                <div className="card enrol op-enter op-d2">
                  {qr ? <img src={qr} alt="QR code for your authenticator app" /> : <div style={{ width: 148, height: 148 }} />}
                  <div className="stack" style={{ gap: 10 }}>
                    <ol>
                      <li>Open Google Authenticator, Microsoft Authenticator or 1Password.</li>
                      <li>Add an account and scan the code.</li>
                      <li>Enter the 6-digit code it shows.</li>
                    </ol>
                    <details>
                      <summary style={{ fontSize: 13, cursor: "pointer", color: "var(--accent)" }}>Can't scan? Enter this key</summary>
                      <div className="secret" style={{ marginTop: 6 }}>{challenge.enrolment.secret}</div>
                    </details>
                  </div>
                </div>
              )}

              {challenge?.password_change_required && (
                <div className="stack op-enter op-d2">
                  <div className="alert alert-info"><Icon name="key" size={18} /><span>Your admin gave you a temporary password. Choose your own to finish signing in.</span></div>
                  <div className="field">
                    <label htmlFor="new-password">New password</label>
                    <input id="new-password" className="input" type="password" autoComplete="new-password"
                      value={newPassword} onChange={(e) => setNewPassword(e.target.value)} />
                    <span className="hint">At least 12 characters. A short sentence works well.</span>
                  </div>
                  <div className="field">
                    <label htmlFor="confirm-password">Repeat new password</label>
                    <input id="confirm-password" className="input" type="password" autoComplete="new-password"
                      value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} />
                  </div>
                </div>
              )}

              <div className="field op-enter op-d3">
                <label htmlFor="login-code">Verification code</label>
                <input id="login-code" ref={codeRef} className="input input-code" type="text" inputMode="numeric"
                  autoComplete="one-time-code" maxLength={6} placeholder="123456" value={code}
                  onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} />
              </div>
              <button type="submit" className="btn btn-primary btn-block op-press" disabled={busy}>
                {busy ? <span className="spinner" aria-label="Verifying" /> : "Verify and continue"}
              </button>
              <button type="button" className="btn btn-link" style={{ alignSelf: "flex-start" }} onClick={backToPassword}>
                ← Use a different account
              </button>
            </form>
          )}
        </div>

        <footer className="login-footer">
          <a href="#privacy">Privacy notice</a><a href="#imprint">Imprint</a><a href="#a11y">Accessibility statement</a><span>Hosted in the EU</span>
        </footer>

        {showCookie && (
          <div role="region" aria-label="Cookie notice" className="cookie op-enter op-d4">
            <Icon name="shield" size={22} />
            <p>We only use essential cookies to keep you signed in and remember your language and theme. No tracking, analytics or advertising cookies. <a href="#privacy">Read the privacy notice</a></p>
            <button type="button" className="btn btn-sm op-press" onClick={dismissCookie}>Understood</button>
          </div>
        )}
      </main>
    </div>
  );
}
