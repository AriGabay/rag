"use client";

import { useState } from "react";
import { api, ApiError, errorMessage, GENERIC_ERROR, LOGIN_NEXT_KEY } from "@/lib/api";

function nextPath(): string {
  try {
    const next = window.sessionStorage.getItem(LOGIN_NEXT_KEY);
    window.sessionStorage.removeItem(LOGIN_NEXT_KEY);
    // Only same-site relative paths are honored.
    if (next && next.startsWith("/") && !next.startsWith("//") && !next.startsWith("/login")) return next;
  } catch {
    // ignore
  }
  return "/chat";
}

export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    if (!email.trim() || !password) {
      setError("יש להזין דוא״ל וסיסמה.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.login(email.trim(), password);
      window.location.href = nextPath();
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError(err.message !== GENERIC_ERROR ? err.message : "דוא״ל או סיסמה שגויים.");
      } else {
        setError(errorMessage(err));
      }
      setBusy(false);
    }
  }

  return (
    <main className="login-wrap">
      <form className="card login-card stack" onSubmit={onSubmit} noValidate aria-labelledby="login-title">
        <h1 id="login-title">כניסה למאגר הידע</h1>
        <div className="field">
          <label htmlFor="email">דוא״ל</label>
          <input
            id="email"
            className="input"
            type="email"
            dir="ltr"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
        </div>
        <div className="field">
          <label htmlFor="password">סיסמה</label>
          <input
            id="password"
            className="input"
            type="password"
            dir="ltr"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </div>
        {error && (
          <div className="alert alert-error" role="alert">
            {error}
          </div>
        )}
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? "מתחבר..." : "כניסה"}
        </button>
      </form>
    </main>
  );
}
