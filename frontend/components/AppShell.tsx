"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { createContext, useCallback, useContext, useEffect, useState } from "react";
import { api, errorMessage } from "@/lib/api";
import type { MeResponse } from "@/lib/types";

const SessionContext = createContext<MeResponse | null>(null);

/** The signed-in user's session. Only available inside authenticated pages. */
export function useSession(): MeResponse | null {
  return useContext(SessionContext);
}

const NAV: { href: string; label: string; adminOnly?: boolean }[] = [
  { href: "/chat", label: "שאלות" },
  { href: "/documents", label: "מסמכים" },
  { href: "/review", label: "בדיקת נתונים" },
  { href: "/admin", label: "ניהול", adminOnly: true },
];

export default function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname() ?? "";
  const isLogin = pathname.startsWith("/login");
  const [me, setMe] = useState<MeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (isLogin) return;
    let cancelled = false;
    api
      .me()
      .then((data) => {
        if (!cancelled) {
          setMe(data);
          setError(null);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(errorMessage(err));
      });
    return () => {
      cancelled = true;
    };
  }, [isLogin, attempt]);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      // Even if the server call fails, leave the authenticated area.
    }
    // A full page load is intentional: it drops all client state of the ended session.
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination
    window.location.href = "/login";
  }, []);

  if (isLogin) return <>{children}</>;

  if (!me) {
    return (
      <main className="page" aria-busy={!error}>
        {error ? (
          <div className="alert alert-error row" role="alert">
            <span>{error}</span>
            <button type="button" className="btn" onClick={() => setAttempt((a) => a + 1)}>
              נסו שוב
            </button>
          </div>
        ) : (
          <p className="muted">טוען...</p>
        )}
      </main>
    );
  }

  const isAdmin = me.user.role === "admin";

  return (
    <SessionContext.Provider value={me}>
      <header className="app-header">
        <span className="brand">{me.office.name}</span>
        {me.demo_mode && (
          <span className="badge badge-demo" aria-label="המערכת פועלת במצב דמו">
            דמו
          </span>
        )}
        <nav aria-label="ניווט ראשי">
          {NAV.filter((n) => !n.adminOnly || isAdmin).map((n) => (
            <Link key={n.href} href={n.href} aria-current={pathname.startsWith(n.href) ? "page" : undefined}>
              {n.label}
            </Link>
          ))}
        </nav>
        <div className="user-box">
          <span>
            {me.user.full_name}
            {isAdmin && <span className="small"> (מנהל)</span>}
          </span>
          <button type="button" className="btn" onClick={logout}>
            יציאה
          </button>
        </div>
      </header>
      <main className="page">{children}</main>
    </SessionContext.Provider>
  );
}
