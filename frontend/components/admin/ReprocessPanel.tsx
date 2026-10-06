"use client";

import { useState } from "react";
import { ErrorAlert, Notice } from "@/components/ui";
import { errorMessage, request } from "@/lib/api";
import { useApi, useInterval } from "@/lib/useApi";

interface JobRow {
  kind: string;
  status: string;
  count: number;
}

const KIND_LABEL: Record<string, string> = {
  process: "עיבוד מסמך",
  "process:reindex": "קריאה מחדש",
  extract_measurements: "חילוץ נתונים כמותיים",
  extract_facts: "חילוץ עובדות (מנוע קודם)",
};
const STATUS_LABEL: Record<string, string> = { queued: "בתור", running: "רץ", done: "הסתיים", failed: "נכשל" };

/** Reading the office's documents again after the reader changed, and the background queue's state. */
const fetchJobs = () => request<{ jobs: JobRow[] }>("/api/admin/jobs");

export function ReprocessPanel() {
  const jobsApi = useApi(fetchJobs);
  const jobs = jobsApi.data?.jobs ?? null;
  const load = jobsApi.reload;
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useInterval(load, 5000, true);

  const run = async (path: string, all: boolean, label: string) => {
    setBusy(true);
    setError(null);
    try {
      const res = await request<{ queued: number }>(path, { method: "POST", body: { all } });
      setNotice(`${label}: ${res.queued} מסמכים נוספו לתור.`);
      load();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const active = (jobs ?? []).filter((j) => j.status === "queued" || j.status === "running");
  return (
    <section className="card stack" aria-labelledby="reprocess-title">
      <h2 id="reprocess-title">קריאה מחדש של מסמכים</h2>
      <p className="small muted" style={{ margin: 0 }}>
        קריאה מחדש מחליפה את מה שנקרא מהמסמכים (טקסט, טבלאות, תמונות, אינדקס) ומחלצת מחדש נתונים כמותיים. רשומות והחלטות
        סוקרים נשמרות; תשובות קודמות מסומנות כמבוססות על מידע ישן.
      </p>
      <div className="row">
        <button type="button" className="btn" disabled={busy} onClick={() => void run("/api/admin/reprocess", false, "קריאה מחדש")}>
          קריאה מחדש של מסמכים שנקראו בגרסה ישנה
        </button>
        <button type="button" className="btn" disabled={busy} onClick={() => void run("/api/admin/reprocess", true, "קריאה מחדש")}>
          קריאה מחדש של כל המסמכים
        </button>
        <button type="button" className="btn" disabled={busy} onClick={() => void run("/api/admin/measurements", false, "חילוץ נתונים")}>
          חילוץ נתונים כמותיים חסרים
        </button>
      </div>
      {notice && <Notice kind="ok">{notice}</Notice>}
      <ErrorAlert message={error ?? jobsApi.error} onRetry={load} />
      <div className="small">
        {active.length === 0 ? (
          <span className="muted">אין עבודות פעילות ברקע.</span>
        ) : (
          <ul>
            {active.map((j) => (
              <li key={`${j.kind}-${j.status}`}>
                {KIND_LABEL[j.kind] ?? j.kind}: {j.count} {STATUS_LABEL[j.status] ?? j.status}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
