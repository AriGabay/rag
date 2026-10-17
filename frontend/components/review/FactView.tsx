"use client";

import { useId, useState } from "react";
import { B, ErrorAlert, Notice } from "@/components/ui";
import { ApiError, errorMessage, factsApi, safeApiUrl, type FactPrecondition } from "@/lib/api";
import { formatDecimal } from "@/lib/format";
import { useApi } from "@/lib/useApi";
import type { FactAttribute, FactBrief, FactDetail, FactReviewGroup, FactStatus, ReviewFact } from "@/lib/types";

const FACT_STATUS_LABEL: Record<FactStatus, string> = {
  needs_review: "דורש בדיקה",
  auto_validated: "אומת אוטומטית — ממתין לאישור",
  verified: "מאומת",
  corrected: "תוקן",
  rejected: "נדחה",
};

const FACT_STATUS_BADGE: Record<FactStatus, string> = {
  needs_review: "badge-warn",
  auto_validated: "badge-info",
  verified: "badge-ok",
  corrected: "badge-ok",
  rejected: "badge-danger",
};

const NUMBER_RE = /^(\d{1,3}(,\d{3})+(\.\d+)?|\d+(\.\d+)?)$/;
const MSG_NUMBER = "יש להזין מספר תקין (לדוגמה 12 או 12.5).";
const MSG_UNIT = "יש לבחור יחידה.";
const MSG_REJECT_NOTE = "חובה לרשום את סיבת הדחייה.";
const MSG_STALE = "הערך השתנה בינתיים; הרשימה נטענה מחדש.";

function valueText(f: FactBrief): string {
  const v = formatDecimal(f.value);
  return f.unit_label ? `${v} ${f.unit_label}` : v;
}

function PageLink({ fact }: { fact: FactBrief }) {
  const href = safeApiUrl(fact.url);
  if (!href) return null;
  return (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {fact.page ? (
        <>
          עמוד <B>{fact.page}</B> במקור
        </>
      ) : (
        "פתיחת המקור"
      )}
    </a>
  );
}

function Quote({ text }: { text: string }) {
  return (
    <blockquote className="docx-excerpt" style={{ margin: 0 }}>
      <bdi>{text}</bdi>
    </blockquote>
  );
}

function Conflicts({ fact }: { fact: ReviewFact }) {
  if (fact.conflicts.length === 0) return null;
  return (
    <div className="alert alert-warn stack" role="note" aria-label="ערכים סותרים לאותו נכס">
      <strong>ערכים סותרים לאותו נכס:</strong>
      <ul className="stack" style={{ margin: 0, paddingInlineStart: 18 }}>
        {fact.conflicts.map((c) => (
          <li key={c.id}>
            <B>{valueText(c)}</B> · {c.document.title} · {FACT_STATUS_LABEL[c.status] ?? c.status} · <PageLink fact={c} />
            <div className="small muted">
              <bdi>{c.quote}</bdi>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

function CorrectForm({
  fact,
  attribute,
  busy,
  onSubmit,
  onCancel,
}: {
  fact: ReviewFact;
  attribute: FactAttribute;
  busy: boolean;
  onSubmit: (value: string, unit: string, note: string) => Promise<string | null>;
  onCancel: () => void;
}) {
  const uid = useId();
  const options = attribute.unit_options;
  const [value, setValue] = useState("");
  const [unit, setUnit] = useState(options[0]?.code ?? "");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    const v = value.trim();
    if (!NUMBER_RE.test(v)) {
      setError(MSG_NUMBER);
      return;
    }
    if (!options.some((o) => o.code === unit)) {
      setError(MSG_UNIT);
      return;
    }
    setError(null);
    setError(await onSubmit(v.replace(/,/g, ""), unit, note.trim()));
  }

  return (
    <form className="card stack" onSubmit={submit} noValidate aria-label={`תיקון הערך של ${fact.document.title}`}>
      <div className="row" style={{ alignItems: "flex-start" }}>
        <div className="field" style={{ flex: 1 }}>
          <label htmlFor={`${uid}-value`}>ערך מתוקן</label>
          <input
            id={`${uid}-value`}
            className="input"
            dir="ltr"
            inputMode="decimal"
            placeholder="לדוגמה 12.5"
            value={value}
            aria-invalid={error ? true : undefined}
            aria-describedby={`${uid}-err`}
            onChange={(e) => {
              setValue(e.target.value);
              setError(null);
            }}
          />
        </div>
        <div className="field">
          <label htmlFor={`${uid}-unit`}>יחידה</label>
          <select
            id={`${uid}-unit`}
            className="input"
            value={unit}
            onChange={(e) => {
              setUnit(e.target.value);
              setError(null);
            }}
          >
            {options.map((o) => (
              <option key={o.code} value={o.code}>
                {o.label}
              </option>
            ))}
          </select>
        </div>
      </div>
      <span id={`${uid}-err`} className="field-error" aria-live="polite">
        {error}
      </span>
      <div className="field">
        <label htmlFor={`${uid}-note`}>הערה (לא חובה)</label>
        <input id={`${uid}-note`} className="input" value={note} onChange={(e) => setNote(e.target.value)} />
      </div>
      <div className="row">
        <button type="submit" className="btn btn-primary" disabled={busy}>
          שמירת התיקון
        </button>
        <button type="button" className="btn" onClick={onCancel} disabled={busy}>
          ביטול
        </button>
      </div>
    </form>
  );
}

type ChangedKind = "ok" | "warn";

function FactRow({
  fact,
  attribute,
  onChanged,
}: {
  fact: ReviewFact;
  attribute: FactAttribute;
  onChanged: (message: string, kind?: ChangedKind) => void;
}) {
  const uid = useId();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<"idle" | "reject" | "correct">("idle");
  const [rejectNote, setRejectNote] = useState("");
  const [rejectError, setRejectError] = useState<string | null>(null);
  const canCorrect = attribute.value_type === "numeric" && attribute.unit_options.length > 0;

  // Every action carries the status and value this row shows: a fact someone else reviewed meanwhile is a 409.
  const pre: FactPrecondition = { expected_status: fact.status, expected_value: fact.value };

  /**
   * Runs an action; a 422 is returned as an inline message for the open form, a 409 (the fact changed since the
   * list was loaded) reloads the list with a notice above it, anything else is shown above the row.
   */
  async function act(fn: () => Promise<FactDetail>, success: string): Promise<string | null> {
    setBusy(true);
    setError(null);
    try {
      await fn();
      onChanged(success);
      return null;
    } catch (err) {
      if (err instanceof ApiError && err.status === 422) return err.message;
      if (err instanceof ApiError && err.status === 409) {
        setMode("idle");
        onChanged(MSG_STALE, "warn");
        return null;
      }
      setError(errorMessage(err));
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function submitReject() {
    const note = rejectNote.trim();
    if (!note) {
      setRejectError(MSG_REJECT_NOTE);
      return;
    }
    setRejectError(await act(() => factsApi.reject(fact.id, note, pre), "הערך נדחה."));
  }

  const label = `${attribute.label}: ${valueText(fact)} — ${fact.document.title}`;
  return (
    <article className="card stack" aria-label={label}>
      <div className="row">
        <strong>
          <B>{valueText(fact)}</B>
        </strong>
        <span className={`badge ${FACT_STATUS_BADGE[fact.status] ?? ""}`}>{FACT_STATUS_LABEL[fact.status] ?? fact.status}</span>
        {fact.entity_role !== "subject" && <span className="badge">נכס השוואה או אחר</span>}
        <span className="spacer" />
        <PageLink fact={fact} />
      </div>
      {fact.entity_descriptor && (
        <div className="small muted">
          נכס: <bdi>{fact.entity_descriptor}</bdi>
        </div>
      )}
      <Quote text={fact.quote} />
      {fact.original.value_text && (
        <div className="small muted">
          כפי שנכתב: <bdi>{[fact.original.value_text, fact.original.unit_label].filter(Boolean).join(" ")}</bdi>
        </div>
      )}
      <Conflicts fact={fact} />
      <ErrorAlert message={error} />
      {mode === "idle" && (
        <div className="row">
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy}
            onClick={() => void act(() => factsApi.approve(fact.id, pre), "הערך אושר.")}
          >
            אישור
          </button>
          {canCorrect && (
            <button type="button" className="btn" disabled={busy} onClick={() => setMode("correct")}>
              תיקון
            </button>
          )}
          <button type="button" className="btn btn-danger" disabled={busy} onClick={() => setMode("reject")}>
            דחייה
          </button>
        </div>
      )}
      {mode === "correct" && (
        <CorrectForm
          fact={fact}
          attribute={attribute}
          busy={busy}
          onCancel={() => setMode("idle")}
          onSubmit={(value, unit, note) =>
            act(() => factsApi.correct(fact.id, value, unit, pre, note || undefined), "התיקון נשמר.")
          }
        />
      )}
      {mode === "reject" && (
        <div className="card stack">
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
            <span id={`${uid}-reject-err`} className="field-error" aria-live="polite">
              {rejectError}
            </span>
          </div>
          <div className="row">
            <button type="button" className="btn btn-danger" disabled={busy} onClick={() => void submitReject()}>
              דחיית הערך
            </button>
            <button type="button" className="btn" disabled={busy} onClick={() => setMode("idle")}>
              ביטול
            </button>
          </div>
        </div>
      )}
    </article>
  );
}

function AttributeGroup({
  group,
  onChanged,
}: {
  group: FactReviewGroup;
  onChanged: (message: string, kind?: ChangedKind) => void;
}) {
  const uid = useId();
  const count = group.documents.reduce((n, d) => n + d.facts.length, 0);
  return (
    <section className="stack" aria-labelledby={`${uid}-title`}>
      <div className="row">
        <h2 id={`${uid}-title`} style={{ margin: 0 }}>
          {group.attribute.label}
        </h2>
        <span className="badge">
          <bdi>{count}</bdi>
        </span>
      </div>
      {group.documents.map((d) => (
        <section key={d.document.id} className="stack" aria-label={`${group.attribute.label} — ${d.document.title}`}>
          <h3 style={{ margin: 0 }}>{d.document.title}</h3>
          {d.facts.map((f) => (
            <FactRow key={f.id} fact={f} attribute={group.attribute} onChanged={onChanged} />
          ))}
        </section>
      ))}
    </section>
  );
}

/** The "extracted facts" review tab: grouped by attribute, then document, needs_review first (server order). */
export function FactsReview() {
  const facts = useApi(factsApi.list);
  const [notice, setNotice] = useState<{ text: string; kind: ChangedKind } | null>(null);
  const groups = facts.data?.attributes ?? null;

  /** After an action (or a stale-list 409) the list is reloaded, so every row shows the server's current state. */
  function changed(text: string, kind: ChangedKind = "ok") {
    setNotice({ text, kind });
    facts.reload();
  }

  return (
    <div className="stack">
      {notice && <Notice kind={notice.kind}>{notice.text}</Notice>}
      <ErrorAlert message={facts.error} onRetry={facts.reload} />
      {groups === null && !facts.error && <p className="muted">טוען...</p>}
      {groups && groups.length === 0 && <p className="muted">אין עובדות שממתינות לבדיקה</p>}
      {groups?.map((g) => (
        <AttributeGroup key={g.attribute.id} group={g} onChanged={changed} />
      ))}
    </div>
  );
}
