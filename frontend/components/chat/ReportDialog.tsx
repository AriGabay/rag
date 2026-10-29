"use client";

import { useRef, useState } from "react";
import { Dialog } from "@/components/ui";
import {
  ApiError,
  chatApi,
  errorMessage,
  REPORT_CATEGORY_VALUES,
  REPORT_NOTE_MAX,
  type AnswerReport,
  type AnswerReportResult,
  type ReportCategory,
} from "@/lib/api";
import { copyText } from "@/lib/clipboard";

const CATEGORY_LABELS: Record<ReportCategory, string> = {
  wrong: "התשובה שגויה",
  missing: "חסר מידע בתשובה",
  wrong_source: "המקור שצוטט שגוי",
  wrong_property: "התשובה על נכס אחר",
  other: "אחר",
};

/** The five categories of a report (`REPORT_CATEGORY_VALUES`, in its order), with the labels the dialog and the admin's list show (round 25 U5, R11). */
export const REPORT_CATEGORIES: { value: ReportCategory; label: string }[] = REPORT_CATEGORY_VALUES.map((value) => ({
  value,
  label: CATEGORY_LABELS[value],
}));

export function reportCategoryLabel(value: string): string {
  return REPORT_CATEGORIES.find((c) => c.value === value)?.label ?? value;
}

/** What each failed report call says, by the status the server answered with. */
export function reportErrorText(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 409) return "התשובה עדיין בעיבוד. אפשר לדווח עליה כשתסתיים.";
    if (err.status === 404) return "התשובה כבר לא קיימת (נוצרה מחדש או שהשיחה נמחקה), ולכן אי אפשר לדווח עליה.";
    if (err.status === 422) return "הדיווח אינו תקין: יש לבחור קטגוריה ולכתוב הערה של עד 1,000 תווים.";
  }
  return errorMessage(err);
}

/** The report dialog of one answer. `existing`: the user's earlier report of it (the form opens prefilled and a resubmit reads as an
 * update). `onSaved` hands the saved report to the conversation's list, so the answer shows its marker. */
export function ReportDialog({
  messageId,
  existing,
  onClose,
  onSaved,
}: {
  /** The answer being reported; null keeps the dialog closed. */
  messageId: string | null;
  existing: AnswerReport | null;
  onClose: () => void;
  onSaved: (report: AnswerReport) => void;
}) {
  return (
    <Dialog open={messageId !== null} title={existing ? "עדכון דיווח על תשובה" : "דיווח על תשובה"} onClose={onClose}>
      {messageId !== null && (
        <ReportForm key={messageId} messageId={messageId} existing={existing} onClose={onClose} onSaved={onSaved} />
      )}
    </Dialog>
  );
}

function ReportForm({
  messageId,
  existing,
  onClose,
  onSaved,
}: {
  messageId: string;
  existing: AnswerReport | null;
  onClose: () => void;
  onSaved: (report: AnswerReport) => void;
}) {
  const [category, setCategory] = useState<ReportCategory | "">(existing?.category ?? "");
  const [note, setNote] = useState(existing?.note ?? "");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<AnswerReportResult | null>(null);
  const [copied, setCopied] = useState(false);
  const sendingRef = useRef(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!category || sendingRef.current) return;
    sendingRef.current = true;
    setSending(true);
    setError(null);
    try {
      const res = await chatApi.report(messageId, { category, note: note.trim() });
      setDone(res);
      onSaved({ id: res.id, message_id: messageId, category: res.category, note: res.note });
    } catch (err) {
      setError(reportErrorText(err));
    } finally {
      sendingRef.current = false;
      setSending(false);
    }
  };

  const copyId = async () => {
    if (!done) return;
    setCopied(await copyText(done.id));
  };

  return (
    <form className="stack report-form" onSubmit={(e) => void submit(e)} data-testid="report-dialog">
      {/* announced when the report is saved: the region exists before its text does */}
      <div aria-live="polite" role="status" data-testid="report-status">
        {done && (
          <div className="alert alert-ok stack">
            <span>{done.updated ? "הדיווח עודכן." : "הדיווח נשלח."} תודה.</span>
            <span className="row">
              <span>
                מספר הדיווח: <bdi data-testid="report-id">{done.id}</bdi>
              </span>
              <button type="button" className="btn btn-small" onClick={() => void copyId()}>
                {copied ? "המספר הועתק" : "העתקת מספר הדיווח"}
              </button>
            </span>
          </div>
        )}
      </div>
      {!done && (
        <>
          <fieldset className="field" disabled={sending}>
            <legend className="field-label">מה הבעיה בתשובה?</legend>
            {REPORT_CATEGORIES.map((c) => (
              <label key={c.value} className="checkbox">
                <input
                  type="radio"
                  name="report-category"
                  value={c.value}
                  checked={category === c.value}
                  onChange={() => setCategory(c.value)}
                />
                {c.label}
              </label>
            ))}
          </fieldset>
          <div className="field">
            <label htmlFor="report-note">הערה (לא חובה)</label>
            <textarea
              id="report-note"
              className="input"
              rows={4}
              maxLength={REPORT_NOTE_MAX}
              value={note}
              disabled={sending}
              onChange={(e) => setNote(e.target.value)}
              aria-describedby="report-note-count"
            />
            <span id="report-note-count" className="small muted" data-testid="report-note-count">
              <bdi>{note.length}</bdi> מתוך <bdi>{REPORT_NOTE_MAX.toLocaleString("he-IL")}</bdi> תווים. הדיווח כולל פרטים טכניים של
              הריצה, בלי טקסט התשובה או המסמכים.
            </span>
          </div>
          {error && (
            <div className="alert alert-error" role="alert" data-testid="report-error">
              {error}
            </div>
          )}
        </>
      )}
      <div className="row">
        {!done && (
          <button type="submit" className="btn btn-primary" disabled={!category || sending} data-testid="report-submit">
            {sending ? "שולח…" : existing ? "עדכון הדיווח" : "שליחת הדיווח"}
          </button>
        )}
        <button type="button" className="btn" onClick={onClose}>
          {done ? "סגירה" : "ביטול"}
        </button>
      </div>
    </form>
  );
}
