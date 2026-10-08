"use client";

import { useState } from "react";
import { ErrorAlert, Notice } from "@/components/ui";
import { errorMessage, request } from "@/lib/api";
import { JOB_STATUS_LABEL, KEPT_PREVIOUS_TITLE, keptPreviousText, type KeptPrevious } from "@/lib/format";
import { useApi, useInterval } from "@/lib/useApi";

interface JobRow {
  kind: string;
  status: string;
  count: number;
}

/** A new reading held back because it read a document worse than its current reading (the current one stays). */
interface HeldReading {
  version_id: string;
  title: string;
  summary: string;
}

/** A version whose last reprocess kept its current reading, with why (KTD9). ``can_accept``: the kept reading was a
 * regression an admin may still apply. */
interface KeptReading extends KeptPrevious {
  version_id: string;
  title: string;
  can_accept: boolean;
}

const KIND_LABEL: Record<string, string> = {
  process: "עיבוד מסמך",
  "process:reindex": "קריאה מחדש",
  extract_measurements: "חילוץ נתונים כמותיים",
  extract_facts: "חילוץ עובדות (מנוע קודם)",
  positions: "הוספת מיקומי מקור",
};

/** Reading the office's documents again after the reader changed, and the background queue's state. */
const fetchJobs = () =>
  request<{ jobs: JobRow[]; regressions?: HeldReading[]; kept_previous?: KeptReading[] }>("/api/admin/jobs");

export function ReprocessPanel() {
  const jobsApi = useApi(fetchJobs);
  const jobs = jobsApi.data?.jobs ?? null;
  const held = jobsApi.data?.regressions ?? [];
  // kept for another reason than a regression (a transient failure after the job's attempts): nothing to accept
  const keptOther = (jobsApi.data?.kept_previous ?? []).filter((k) => !k.can_accept);
  const load = jobsApi.reload;
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useInterval(load, 5000, true);

  const run = async (path: string, all: boolean, label: string, acceptRegression = false) => {
    setBusy(true);
    setError(null);
    try {
      const body = acceptRegression ? { all, accept_regression: true } : { all };
      const res = await request<{ queued: number }>(path, { method: "POST", body });
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
      {held.length > 0 && (
        <div className="stack small" role="note">
          <strong>
            {KEPT_PREVIOUS_TITLE}: קריאות חדשות שאיבדו מידע (<bdi>{held.length}</bdi>)
          </strong>
          <span className="muted">
            הקריאה החדשה של המסמכים האלה איבדה מידע ביחס לקריאה הקיימת, ולכן הקריאה הקיימת נשארה והמסמכים זמינים כרגיל.
            אין צורך בפעולה; אפשר להחיל את הקריאה החדשה בכל זאת, לאחר בדיקה.
          </span>
          <ul>
            {held.map((h) => (
              <li key={h.version_id}>
                <bdi>{h.title}</bdi>: <bdi>{h.summary}</bdi>
              </li>
            ))}
          </ul>
          <div className="row">
            <button
              type="button"
              className="btn"
              disabled={busy}
              onClick={() => void run("/api/admin/reprocess", false, "החלת הקריאות החדשות", true)}
            >
              החלת הקריאות החדשות בכל זאת
            </button>
          </div>
        </div>
      )}
      {keptOther.length > 0 && (
        <div className="stack small" role="note">
          <strong>
            {KEPT_PREVIOUS_TITLE}: קריאה מחדש שלא הושלמה (<bdi>{keptOther.length}</bdi>)
          </strong>
          <ul>
            {keptOther.map((k) => (
              <li key={k.version_id}>
                <bdi>{k.title}</bdi>: <bdi>{keptPreviousText(k)}</bdi>
              </li>
            ))}
          </ul>
        </div>
      )}
      {notice && <Notice kind="ok">{notice}</Notice>}
      <ErrorAlert message={error ?? jobsApi.error} onRetry={load} />
      <div className="small">
        {active.length === 0 ? (
          <span className="muted">אין עבודות פעילות ברקע.</span>
        ) : (
          <ul>
            {active.map((j) => (
              <li key={`${j.kind}-${j.status}`}>
                {KIND_LABEL[j.kind] ?? j.kind}: <bdi>{j.count}</bdi> {JOB_STATUS_LABEL[j.status] ?? j.status}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
