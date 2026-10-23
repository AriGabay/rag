"use client";

import { CoveragePanel, ProviderPanel, UsersAndGroups } from "@/components/admin/AdminPanels";
import { ReprocessPanel } from "@/components/admin/ReprocessPanel";
import { useSession } from "@/components/AppShell";

export default function AdminPage() {
  const me = useSession();
  if (me?.user.role !== "admin") {
    return (
      <div className="alert alert-error" role="alert">
        אין לך הרשאה לצפות בעמוד זה.
      </div>
    );
  }
  return (
    <div className="stack">
      <h1>ניהול המשרד</h1>
      <CoveragePanel />
      <ProviderPanel />
      <ReprocessPanel />
      <UsersAndGroups />
    </div>
  );
}
