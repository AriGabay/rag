"use client";

import { useCallback, useMemo, useState } from "react";
import { reportCategoryLabel } from "@/components/chat/ReportDialog";
import { ErrorAlert } from "@/components/ui";
import { answerReportsApi, errorMessage, type AnswerReportDetail } from "@/lib/api";
import { copyText } from "@/lib/clipboard";
import { formatTimestamp } from "@/lib/format";
import { useApi } from "@/lib/useApi";

const STATUS_LABEL: Record<string, string> = {
  done: "הושלמה",
  failed: "נכשלה",
  cancelled: "בוטלה",
};

/** The office's problem reports, newest first (round 25 U5, R11): the category, the status the answer had, when it was reported and
 * the report's id to copy. A report opens its detail (the note and the structural snapshot) and its export (the same snapshot without the
 * note: no document or answer text), the one an office sends to the developer. */
export function AnswerReportsPanel() {
  const { data, error, reload } = useApi(answerReportsApi.list);
  const [open, setOpen] = useState<AnswerReportDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  const show = useCallback(async (id: string) => {
    setDetailError(null);
    try {
      setOpen(await answerReportsApi.detail(id));
    } catch (err) {
      setOpen(null);
      setDetailError(errorMessage(err));
    }
  }, []);

  const copy = async (id: string) => {
    setCopied((await copyText(id)) ? id : null);
  };

  const snapshotText = useMemo(() => (open ? JSON.stringify(open.snapshot, null, 2) : ""), [open]);

  const reports = data?.reports ?? null;
  return (
    <section className="card stack" aria-labelledby="answer-reports-title" data-testid="answer-reports">
      <div className="row">
        <h2 id="answer-reports-title">דיווחים על תשובות</h2>
        <button type="button" className="btn btn-small" onClick={reload}>
          רענון
        </button>
      </div>
      <ErrorAlert message={error} onRetry={reload} />
      <ErrorAlert message={detailError} />
      {reports === null && !error && <p className="muted">טוען…</p>}
      {reports !== null && reports.length === 0 && <p className="muted">עדיין לא דווחו תשובות בעייתיות.</p>}
      {reports !== null && reports.length > 0 && (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>קטגוריה</th>
                <th>מצב התשובה</th>
                <th>דווח ב</th>
                <th>מספר דיווח</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {reports.map((r) => (
                <tr key={r.id} className={open?.id === r.id ? "selected" : undefined}>
                  <td>{reportCategoryLabel(r.category)}</td>
                  <td>
                    {STATUS_LABEL[r.message_status] ?? r.message_status}
                    {!r.message_exists && <div className="small muted">ההודעה אינה קיימת עוד</div>}
                  </td>
                  <td>
                    <bdi>{formatTimestamp(r.created_at)}</bdi>
                  </td>
                  <td>
                    <bdi className="small" data-testid="report-row-id">
                      {r.id}
                    </bdi>{" "}
                    <button type="button" className="btn btn-small" onClick={() => void copy(r.id)}>
                      {copied === r.id ? "הועתק" : "העתקה"}
                    </button>
                  </td>
                  <td>
                    <button type="button" className="btn btn-small" onClick={() => void show(r.id)}>
                      פרטים
                    </button>{" "}
                    <a className="btn btn-small" href={answerReportsApi.exportUrl(r.id)} download>
                      הורדת ייצוא
                    </a>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {open && (
        <div className="stack" data-testid="report-detail">
          <h3>פרטי דיווח</h3>
          <dl className="kv">
            <dt>קטגוריה</dt>
            <dd>{reportCategoryLabel(open.category)}</dd>
            <dt>מצב התשובה</dt>
            <dd>{STATUS_LABEL[open.message_status] ?? open.message_status}</dd>
            <dt>הערת המדווח</dt>
            <dd data-testid="report-detail-note">{open.note || "—"}</dd>
            <dt>גרסת המערכת</dt>
            <dd>
              <bdi>{open.build_sha}</bdi>
            </dd>
            <dt>מודלים</dt>
            <dd>
              <bdi>{Object.entries(open.model_ids).map(([k, v]) => `${k}: ${v}`).join(" · ") || "—"}</bdi>
            </dd>
            <dt>הודעה</dt>
            <dd>{open.message_exists ? <bdi>{open.message_id}</bdi> : "ההודעה אינה קיימת עוד"}</dd>
          </dl>
          <details>
            <summary>תמונת מצב מבנית של הריצה (קודים, זמנים ומיקומים בלבד)</summary>
            <pre dir="ltr" className="small" data-testid="report-snapshot">
              {snapshotText}
            </pre>
          </details>
          <div className="row">
            <a className="btn" href={answerReportsApi.exportUrl(open.id)} download>
              הורדת ייצוא (בלי ההערה)
            </a>
            <button type="button" className="btn" onClick={() => setOpen(null)}>
              סגירה
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
