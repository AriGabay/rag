"use client";

import { useSearchParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { SourcePanel } from "@/components/chat/SourcePanel";
import { ErrorAlert, Notice } from "@/components/ui";
import { errorMessage, request } from "@/lib/api";
import type { ChatSource } from "@/lib/chatTypes";

interface Measurement {
  id: string;
  document_id: string;
  version_id: string;
  title: string;
  metric: string;
  metric_kind: string;
  metric_kind_label: string;
  value_text: string;
  value_form_label: string;
  unit: string | null;
  unit_label: string;
  period: string;
  period_label: string;
  vat: string;
  vat_label: string;
  area_basis: string | null;
  subject: string | null;
  subject_role_label: string;
  value_role_label: string;
  quote: string;
  section: string | null;
  block_index: number | null;
  table_index: number | null;
  status: string;
  status_label: string;
  issues: string[];
  conflict: ConflictEntry[] | ConflictEntry | null;
  review_note: string | null;
}

interface ConflictEntry {
  measurement_id: string;
  value_text: string;
}

interface ListResponse {
  items: Measurement[];
  total: number;
  counts: Record<string, number>;
  kinds: Record<string, string>;
  units: Record<string, string>;
  periods: Record<string, string>;
  vats: Record<string, string>;
}

const STATUS_FILTERS: [string, string][] = [
  ["", "הכול (ללא נדחו)"],
  ["needs_review", "ממתין לבדיקה"],
  ["auto_validated", "ראשוני"],
  ["verified", "אומת"],
  ["corrected", "תוקן"],
  ["rejected", "נדחה"],
];

function qs(params: Record<string, string | undefined>) {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) sp.set(k, v);
  const s = sp.toString();
  return s ? `?${s}` : "";
}

/** Measurements with their meaning: the metric and value exactly as written, and what they mean — kind, unit,
 * period, VAT, area basis, form, role and subject — each shown on its own, never folded into a "price". */
export function MeasurementsReview() {
  const params = useSearchParams();
  const documentId = params.get("document_id") ?? undefined;
  const [status, setStatus] = useState("needs_review");
  const [kind, setKind] = useState("");
  const [q, setQ] = useState("");
  const [data, setData] = useState<ListResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [source, setSource] = useState<ChatSource | null>(null);

  const load = useCallback(async () => {
    try {
      setData(
        await request<ListResponse>(
          `/api/review/measurements${qs({ document_id: documentId, status, kind, q: q.trim() || undefined })}`,
        ),
      );
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [documentId, status, kind, q]);

  useEffect(() => {
    const h = window.setTimeout(() => void load(), q ? 300 : 0);
    return () => window.clearTimeout(h);
  }, [load, q]);

  const act = async (m: Measurement, action: "verify" | "reject" | "correct", body: Record<string, unknown> = {}) => {
    try {
      await request(`/api/review/measurements/${encodeURIComponent(m.id)}/${action}`, {
        method: "POST",
        body: { expected_status: m.status, ...body },
      });
      setNotice(action === "verify" ? "הנתון אומת." : action === "reject" ? "הנתון נדחה." : "הנתון תוקן.");
      await load();
    } catch (err) {
      setError(errorMessage(err));
    }
  };

  const open = (m: Measurement) =>
    setSource({
      id: m.id,
      document_id: m.document_id,
      version_id: m.version_id,
      title: m.title,
      section: m.section,
      location: m.section ? `סעיף "${m.section}"` : "המסמך",
      kind: "measurement",
      text: m.quote,
      block_start: m.block_index,
      block_end: m.block_index,
      table_index: m.table_index,
      page_list: null,
      chunk_id: null,
    });

  return (
    <div className="stack chat-root-review">
      <p className="muted small" style={{ margin: 0 }}>
        כל נתון מוצג כפי שנכתב במסמך, ולצידו משמעותו: סוג המדד, היחידה, התקופה, מע״מ, בסיס השטח, התפקיד והנכס שאליו
        הוא מתייחס. נתון ״ראשוני״ חולץ ואומת אוטומטית מול הטקסט; ״ממתין לבדיקה״ — נמצאה בו בעיה שדורשת עין אנושית.
      </p>
      <div className="row">
        <label>
          סטטוס{" "}
          <select className="input" value={status} onChange={(e) => setStatus(e.target.value)}>
            {STATUS_FILTERS.map(([v, l]) => (
              <option key={v} value={v}>
                {l}
                {v && data?.counts[v] != null ? ` (${data.counts[v]})` : ""}
              </option>
            ))}
          </select>
        </label>
        <label>
          סוג מדד{" "}
          <select className="input" value={kind} onChange={(e) => setKind(e.target.value)}>
            <option value="">הכול</option>
            {data &&
              Object.entries(data.kinds).map(([k, l]) => (
                <option key={k} value={k}>
                  {l}
                </option>
              ))}
          </select>
        </label>
        <input className="input" placeholder="חיפוש בתיאור, בציטוט או בנכס" value={q} onChange={(e) => setQ(e.target.value)} />
        {data && <span className="small muted">{data.total} נתונים</span>}
      </div>
      {notice && <Notice kind="ok">{notice}</Notice>}
      <ErrorAlert message={error} onRetry={load} />
      {data && data.items.length === 0 && <p className="muted">אין נתונים בסינון הזה.</p>}
      <ul className="measure-list">
        {data?.items.map((m) => (
          <MeasurementCard key={m.id} m={m} data={data} onAct={act} onOpen={open} />
        ))}
      </ul>
      {source && (
        <div className="review-source">
          <SourcePanel key={source.id} source={source} onClose={() => setSource(null)} />
        </div>
      )}
    </div>
  );
}

function MeasurementCard({
  m,
  data,
  onAct,
  onOpen,
}: {
  m: Measurement;
  data: ListResponse;
  onAct: (m: Measurement, action: "verify" | "reject" | "correct", body?: Record<string, unknown>) => Promise<void>;
  onOpen: (m: Measurement) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const [note, setNote] = useState("");
  const [form, setForm] = useState({
    value_text: m.value_text,
    unit: m.unit ?? "other",
    period: m.period,
    vat: m.vat,
    area_basis: m.area_basis ?? "",
    metric: m.metric,
    metric_kind: m.metric_kind,
  });
  const meaning: [string, string][] = [
    ["סוג המדד", m.metric_kind_label],
    ["יחידה", m.unit_label || "—"],
    ["תקופה", m.period_label || "—"],
    ["מע״מ", m.vat_label],
    ["בסיס שטח", m.area_basis ?? "לא צוין"],
    ["צורת הערך", m.value_form_label],
    ["תפקיד", m.value_role_label],
    ["מתייחס ל", m.subject ?? m.subject_role_label],
  ];
  return (
    <li className="card measure-card">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <strong>{m.metric}</strong>: <bdi className="measure-value">{m.value_text}</bdi>
        </div>
        <span className={`badge ${m.status === "needs_review" ? "badge-warn" : m.status === "verified" || m.status === "corrected" ? "badge-ok" : ""}`}>
          {m.status_label}
        </span>
      </div>
      <div className="small muted">
        {m.title}
        {m.section ? ` · סעיף "${m.section}"` : ""}
        {m.table_index != null ? " · מתוך טבלה" : ""}
      </div>
      <dl className="measure-meaning">
        {meaning.map(([k, v]) => (
          <div key={k}>
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
      <div className="small">
        כפי שנכתב: <q dir="auto">{m.quote}</q>{" "}
        <button type="button" className="btn-link" onClick={() => onOpen(m)}>
          פתיחה במקור
        </button>
      </div>
      {m.issues.length > 0 && (
        <ul className="small" style={{ color: "var(--warn)" }}>
          {m.issues.map((i) => (
            <li key={i}>{i}</li>
          ))}
        </ul>
      )}
      {m.conflict && (
        <div className="alert alert-warn small">
          חילוץ חדש סותר את ההחלטה שלכם:{" "}
          {(Array.isArray(m.conflict) ? m.conflict : [m.conflict]).map((c, i) => (
            <bdi key={c.measurement_id}>
              {i > 0 && ", "}
              {c.value_text}
            </bdi>
          ))}
          . אשרו שוב כדי להשאיר את החלטתכם, או תקנו.
        </div>
      )}
      {m.review_note && <div className="small muted">הערת סוקר: {m.review_note}</div>}
      {editing ? (
        <div className="stack small">
          <div className="row">
            <label>
              ערך כפי שנכתב <input className="input" value={form.value_text} onChange={(e) => setForm({ ...form, value_text: e.target.value })} />
            </label>
            <label>
              תיאור <input className="input" value={form.metric} onChange={(e) => setForm({ ...form, metric: e.target.value })} />
            </label>
          </div>
          <div className="row">
            <Select label="סוג מדד" value={form.metric_kind} options={data.kinds} onChange={(v) => setForm({ ...form, metric_kind: v })} />
            <Select label="יחידה" value={form.unit} options={data.units} onChange={(v) => setForm({ ...form, unit: v })} />
            <Select label="תקופה" value={form.period} options={data.periods} onChange={(v) => setForm({ ...form, period: v })} />
            <Select label="מע״מ" value={form.vat} options={{ ...data.vats, not_applicable: "לא רלוונטי" }} onChange={(v) => setForm({ ...form, vat: v })} />
            <label>
              בסיס שטח <input className="input" value={form.area_basis} onChange={(e) => setForm({ ...form, area_basis: e.target.value })} />
            </label>
          </div>
          <label>
            הערה <input className="input" value={note} onChange={(e) => setNote(e.target.value)} />
          </label>
          <div className="row">
            <button type="button" className="btn btn-primary" onClick={() => void onAct(m, "correct", { ...form, note })}>
              שמירת תיקון
            </button>
            <button type="button" className="btn" onClick={() => setEditing(false)}>
              ביטול
            </button>
          </div>
        </div>
      ) : rejecting ? (
        <div className="row small">
          <input className="input" placeholder="סיבת הדחייה (חובה)" value={note} onChange={(e) => setNote(e.target.value)} />
          <button type="button" className="btn" disabled={!note.trim()} onClick={() => void onAct(m, "reject", { note })}>
            דחייה
          </button>
          <button type="button" className="btn" onClick={() => setRejecting(false)}>
            ביטול
          </button>
        </div>
      ) : (
        m.status !== "rejected" && (
          <div className="row">
            <button type="button" className="btn btn-small" onClick={() => void onAct(m, "verify")}>
              אישור
            </button>
            <button type="button" className="btn btn-small" onClick={() => setEditing(true)}>
              תיקון
            </button>
            <button type="button" className="btn btn-small" onClick={() => setRejecting(true)}>
              דחייה
            </button>
          </div>
        )
      )}
    </li>
  );
}

function Select({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: Record<string, string>;
  onChange: (v: string) => void;
}) {
  return (
    <label>
      {label}{" "}
      <select className="input" value={value} onChange={(e) => onChange(e.target.value)}>
        {Object.entries(options).map(([k, l]) => (
          <option key={k} value={k}>
            {l || k}
          </option>
        ))}
      </select>
    </label>
  );
}
