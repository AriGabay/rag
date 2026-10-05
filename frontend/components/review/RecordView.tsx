"use client";

import { useId, useState } from "react";
import { B, ErrorAlert, Notice } from "@/components/ui";
import { api, ApiError, errorMessage, safeApiUrl } from "@/lib/api";
import { DATE_FIELDS, formatPricePerSqm, NUMERIC_FIELDS, verificationLabel } from "@/lib/format";
import type { RecordDetail } from "@/lib/types";

const ALL_FIELDS: { field: string; label: string }[] = [
  { field: "data_kind", label: "סוג נתון" },
  { field: "city", label: "עיר" },
  { field: "neighborhood", label: "שכונה" },
  { field: "address", label: "כתובת" },
  { field: "block", label: "גוש" },
  { field: "parcel", label: "חלקה" },
  { field: "sub_parcel", label: "תת חלקה" },
  { field: "property_type", label: "סוג נכס" },
  { field: "rooms", label: "חדרים" },
  { field: "transaction_date", label: "תאריך עסקה" },
  { field: "valuation_date", label: "מועד קובע" },
  { field: "report_date", label: "תאריך הדוח" },
  { field: "area", label: "שטח" },
  { field: "area_type", label: "סוג שטח" },
  { field: "price", label: "מחיר / שווי" },
  { field: "currency", label: "מטבע" },
  { field: "vat_basis", label: "בסיס מע״מ" },
  { field: "price_per_sqm_stated", label: "מחיר למ״ר המצוין במסמך" },
];

/** Returns [normalizedValue, error]. Numbers drop thousands separators; dates become ISO YYYY-MM-DD. */
export function validateFieldValue(field: string, raw: string): [string, string | null] {
  const v = raw.trim();
  if (!v) return ["", "יש להזין ערך."];
  if (NUMERIC_FIELDS.has(field)) {
    const n = v.replace(/[,\s₪]/g, "");
    if (!/^\d+(\.\d+)?$/.test(n)) return [v, "יש להזין מספר חיובי (לדוגמה 1250000 או 85.5)."];
    return [n, null];
  }
  if (DATE_FIELDS.has(field)) {
    let y: number, m: number, d: number;
    let match = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(v);
    if (match) {
      [y, m, d] = [Number(match[1]), Number(match[2]), Number(match[3])];
    } else {
      match = /^(\d{1,2})[./](\d{1,2})[./](\d{4})$/.exec(v);
      if (!match) return [v, "יש להזין תאריך בפורמט DD/MM/YYYY או YYYY-MM-DD."];
      [d, m, y] = [Number(match[1]), Number(match[2]), Number(match[3])];
    }
    const dt = new Date(Date.UTC(y, m - 1, d));
    if (dt.getUTCFullYear() !== y || dt.getUTCMonth() !== m - 1 || dt.getUTCDate() !== d) {
      return [v, "התאריך אינו תקין."];
    }
    return [`${String(y).padStart(4, "0")}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`, null];
  }
  return [v, null];
}

function SourcePane({ record }: { record: RecordDetail }) {
  const base = safeApiUrl(record.file_url);
  if (record.is_docx) {
    return (
      <section className="stack" aria-label="הטקסט המצוטט מהמסמך">
        <h3>הטקסט המצוטט מהמסמך</h3>
        <div className="small muted">
          {record.table_index !== null && (
            <span>
              טבלה <B>{record.table_index + 1}</B>
            </span>
          )}
          {record.row_index !== null && (
            <span>
              {" · "}שורה <B>{record.row_index + 1}</B>
            </span>
          )}
        </div>
        <div className="docx-excerpt">{record.text_span || "לא נשמר טקסט מצוטט לרשומה זו."}</div>
        {base && (
          <a href={base} target="_blank" rel="noopener noreferrer">
            הורדת הקובץ המקורי
          </a>
        )}
      </section>
    );
  }
  if (!base) return <p className="muted">קובץ המקור אינו זמין.</p>;
  const src = base.includes("#") || !record.page_no ? base : `${base}#page=${record.page_no}`;
  const label = record.page_no ? `קובץ המקור, עמוד ${record.page_no}` : "קובץ המקור";
  return (
    <section className="stack" aria-label="קובץ המקור">
      <div className="row">
        <h3 style={{ margin: 0 }}>
          המקור{record.page_no ? <> — עמוד <B>{record.page_no}</B></> : null}
        </h3>
        <span className="spacer" />
        <a href={src} target="_blank" rel="noopener noreferrer">
          פתיחה בחלון חדש
        </a>
      </div>
      <iframe key={src} src={src} title={label} aria-label={label} className="source-frame" />
    </section>
  );
}

export function RecordView({ record, onUpdated }: { record: RecordDetail; onUpdated: (r: RecordDetail) => void }) {
  const uid = useId();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [approveNote, setApproveNote] = useState("");
  const [rejectNote, setRejectNote] = useState("");
  const [rejectError, setRejectError] = useState<string | null>(null);

  const fieldOptions = [
    ...record.fields.map((f) => ({ field: f.field, label: f.label })),
    ...ALL_FIELDS.filter((a) => !record.fields.some((f) => f.field === a.field)),
  ];
  const [field, setField] = useState(fieldOptions[0]?.field ?? "price");
  const [value, setValue] = useState("");
  const [note, setNote] = useState("");
  const [touched, setTouched] = useState(false);
  const [serverFieldError, setServerFieldError] = useState<string | null>(null);

  const [, valueError] = validateFieldValue(field, value);
  const noteError = note.trim() ? null : "חובה לרשום הערה המסבירה את התיקון.";

  async function act(fn: () => Promise<RecordDetail>, success: string) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const r = await fn();
      onUpdated(r);
      setNotice(success);
      return true;
    } catch (err) {
      if (err instanceof ApiError && err.status === 422) setServerFieldError(err.message);
      else setError(errorMessage(err));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function submitCorrection(e: React.FormEvent) {
    e.preventDefault();
    setTouched(true);
    setServerFieldError(null);
    const [normalized, err] = validateFieldValue(field, value);
    if (err || noteError) return;
    const ok = await act(() => api.correct(record.id, field, normalized, note.trim()), "התיקון נשמר.");
    if (ok) {
      setValue("");
      setNote("");
      setTouched(false);
    }
  }

  async function submitReject() {
    if (!rejectNote.trim()) {
      setRejectError("חובה לרשום הערה לדחייה.");
      return;
    }
    setRejectError(null);
    const ok = await act(() => api.reject(record.id, rejectNote.trim()), "הרשומה נדחתה.");
    if (ok) setRejectNote("");
  }

  const isNumeric = NUMERIC_FIELDS.has(field);
  const isDate = DATE_FIELDS.has(field);

  return (
    <div className="grid-2">
      <div className="stack">
        <div>
          <h2>{record.document.title}</h2>
          <div className="row small">
            <span className="badge" role="status" aria-label={`סטטוס אימות: ${verificationLabel(record.verification_status)}`}>
              {verificationLabel(record.verification_status)}
            </span>
            {record.ocr && <span className="badge badge-warn">חולץ בזיהוי תווים (OCR)</span>}
            {record.page_no !== null && (
              <span>
                עמוד <B>{record.page_no}</B>
              </span>
            )}
          </div>
        </div>

        {record.conflict_flag && (
          <div className="alert alert-warn" role="status">
            <strong>סתירה במחיר למ״ר: </strong>
            מחושב <B>{formatPricePerSqm(record.computed_price_per_sqm)}</B>
            {" · "}
            מצוין במסמך <B>{formatPricePerSqm(record.stated_price_per_sqm)}</B>
            {record.calc_definition && <div className="small">אופן החישוב: {record.calc_definition}</div>}
          </div>
        )}
        {!record.conflict_flag && (record.computed_price_per_sqm || record.stated_price_per_sqm) && (
          <div className="small">
            מחיר למ״ר מחושב: <B>{formatPricePerSqm(record.computed_price_per_sqm)}</B>
            {record.stated_price_per_sqm && (
              <>
                {" · "}מצוין במסמך: <B>{formatPricePerSqm(record.stated_price_per_sqm)}</B>
              </>
            )}
            {record.calc_definition && <div className="muted">אופן החישוב: {record.calc_definition}</div>}
          </div>
        )}
        {record.missing_critical.length > 0 && (
          <div className="alert alert-warn">
            חסרים שדות קריטיים:{" "}
            {record.missing_critical
              .map((f) => ALL_FIELDS.find((a) => a.field === f)?.label ?? f)
              .join(", ")}
          </div>
        )}
        {record.review_note && <div className="small">הערת בדיקה: {record.review_note}</div>}

        <div className="table-wrap">
          <table className="table">
            <caption className="visually-hidden">שדות הרשומה</caption>
            <thead>
              <tr>
                <th scope="col">שדה</th>
                <th scope="col">כפי שמופיע במקור</th>
                <th scope="col">ערך מנורמל</th>
                <th scope="col">סטטוס</th>
              </tr>
            </thead>
            <tbody>
              {record.fields.map((f) => (
                <tr key={f.field}>
                  <th scope="row">{f.label}</th>
                  <td>
                    <bdi>{f.original_text ?? "—"}</bdi>
                  </td>
                  <td>
                    <bdi>{f.normalized_value ?? "—"}</bdi>
                    {f.previous.length > 0 && (
                      <div className="small muted">
                        תוקן <B>{f.previous.length}</B> פעמים
                      </div>
                    )}
                  </td>
                  <td>{verificationLabel(f.status)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {notice && <Notice kind="ok">{notice}</Notice>}
        <ErrorAlert message={error} />

        <section className="card stack" aria-labelledby={`${uid}-approve`}>
          <h3 id={`${uid}-approve`}>אישור</h3>
          <div className="field">
            <label htmlFor={`${uid}-approve-note`}>הערה (לא חובה)</label>
            <input
              id={`${uid}-approve-note`}
              className="input"
              value={approveNote}
              onChange={(e) => setApproveNote(e.target.value)}
            />
          </div>
          <div className="row">
            <button
              type="button"
              className="btn btn-primary"
              disabled={busy}
              onClick={() =>
                void act(() => api.approve(record.id, approveNote.trim() || undefined), "הרשומה אושרה.").then(
                  (ok) => ok && setApproveNote(""),
                )
              }
            >
              אישור הרשומה
            </button>
          </div>
        </section>

        <form className="card stack" onSubmit={submitCorrection} noValidate aria-labelledby={`${uid}-correct`}>
          <h3 id={`${uid}-correct`}>תיקון שדה</h3>
          <div className="row" style={{ alignItems: "flex-start" }}>
            <div className="field">
              <label htmlFor={`${uid}-field`}>שדה</label>
              <select
                id={`${uid}-field`}
                className="input"
                value={field}
                onChange={(e) => {
                  setField(e.target.value);
                  setServerFieldError(null);
                }}
              >
                {fieldOptions.map((o) => (
                  <option key={o.field} value={o.field}>
                    {o.label}
                  </option>
                ))}
              </select>
            </div>
            <div className="field" style={{ flex: 1 }}>
              <label htmlFor={`${uid}-value`}>ערך חדש</label>
              <input
                id={`${uid}-value`}
                className="input"
                dir={isNumeric || isDate ? "ltr" : undefined}
                inputMode={isNumeric ? "decimal" : undefined}
                placeholder={isDate ? "DD/MM/YYYY" : isNumeric ? "לדוגמה 1250000" : undefined}
                value={value}
                aria-invalid={(touched || value) && (valueError || serverFieldError) ? true : undefined}
                aria-describedby={`${uid}-value-err`}
                onChange={(e) => {
                  setValue(e.target.value);
                  setServerFieldError(null);
                }}
              />
              <span id={`${uid}-value-err`} className="field-error" aria-live="polite">
                {(touched || value) && (serverFieldError ?? valueError)}
              </span>
            </div>
          </div>
          <div className="field">
            <label htmlFor={`${uid}-note`}>הערה (חובה)</label>
            <textarea
              id={`${uid}-note`}
              className="input"
              rows={2}
              value={note}
              required
              aria-invalid={touched && noteError ? true : undefined}
              aria-describedby={`${uid}-note-err`}
              onChange={(e) => setNote(e.target.value)}
            />
            <span id={`${uid}-note-err`} className="field-error" aria-live="polite">
              {touched && noteError}
            </span>
          </div>
          <div className="row">
            <button type="submit" className="btn btn-primary" disabled={busy}>
              שמירת תיקון
            </button>
          </div>
        </form>

        <section className="card stack" aria-labelledby={`${uid}-reject`}>
          <h3 id={`${uid}-reject`}>דחייה</h3>
          <div className="field">
            <label htmlFor={`${uid}-reject-note`}>סיבת הדחייה (חובה)</label>
            <input
              id={`${uid}-reject-note`}
              className="input"
              value={rejectNote}
              aria-invalid={rejectError ? true : undefined}
              aria-describedby={`${uid}-reject-err`}
              onChange={(e) => {
                setRejectNote(e.target.value);
                setRejectError(null);
              }}
            />
            <span id={`${uid}-reject-err`} className="field-error">
              {rejectError}
            </span>
          </div>
          <div className="row">
            <button type="button" className="btn btn-danger" disabled={busy} onClick={() => void submitReject()}>
              דחיית הרשומה
            </button>
          </div>
        </section>
      </div>
      <SourcePane record={record} />
    </div>
  );
}
