"use client";

import { useCallback, useMemo, useState } from "react";
import type { ChatAnswer, ChatLedger, ChatMessage, ChatSource, LedgerDocument, LedgerTable } from "@/lib/chatTypes";
import { type CitationTarget, Markdown, citationOrder, plainAnswer } from "./Markdown";

export interface CitedItem {
  /** A passage, or the document passage behind a measurement or computation input. */
  source: ChatSource | null;
  label: string;
  id: string;
}

const KIND_LABEL: Record<string, string> = { M: "נתון", C: "חישוב", S: "מקור" };
const VAT: Record<string, string> = { included: "כולל מע״מ", excluded: "ללא מע״מ", unknown: "מע״מ לא צוין", not_applicable: "" };
const PERIOD: Record<string, string> = { month: "לחודש", year: "לשנה", one_time: "חד-פעמי", none: "", unknown: "תקופה לא צוינה" };
const STATUS: Record<string, string> = {
  verified: "מאומת",
  corrected: "תוקן ידנית",
  auto_validated: "ראשוני (טרם אומת)",
  needs_review: "ממתין לבדיקה",
};

/** The source a citation opens: a passage itself, or the passage of the measurement it cites. */
export function citedSource(answer: ChatAnswer, id: string): ChatSource | null {
  const direct = answer.sources.find((s) => s.id === id);
  if (direct) return direct;
  const m = answer.measurements.find((x) => x.id === id);
  if (m) {
    return {
      id: m.id,
      document_id: m.document_id,
      version_id: m.version_id,
      title: m.title,
      section: m.section,
      location: m.section ? `סעיף "${m.section}"` : "נתון כמותי",
      kind: "measurement",
      text: m.quote,
      block_start: m.block_index,
      block_end: m.block_index,
      table_index: m.table_index,
      page_list: null,
      chunk_id: null,
    };
  }
  return null;
}

function citationTargets(answer: ChatAnswer): Map<string, CitationTarget> {
  const out = new Map<string, CitationTarget>();
  citationOrder(answer.markdown).forEach((id, i) => {
    const s = answer.sources.find((x) => x.id === id);
    const m = answer.measurements.find((x) => x.id === id);
    const c = answer.computations.find((x) => x.id === id);
    let title = KIND_LABEL[id[0]] ?? "מקור";
    if (s) title = `${s.title} — ${s.location}`;
    else if (m) title = `${m.title} — ${m.metric}: ${m.value_text}`;
    else if (c) title = `חישוב: ${c.operation} על ${c.inputs.length} ערכים`;
    out.set(id, { id, n: i + 1, label: id, title });
  });
  return out;
}

interface AssistantProps {
  message: ChatMessage;
  /** Only the conversation's last answer can be generated again (an earlier one would land out of order). */
  isLast: boolean;
  onCite: (answer: ChatAnswer, id: string) => void;
  onRetry: (message: ChatMessage) => void;
  onStop: (message: ChatMessage) => void;
}

export function AssistantMessage({ message, isLast, onCite, onRetry, onStop }: AssistantProps) {
  const [copied, setCopied] = useState(false);
  const answer = message.answer;
  const citations = useMemo(() => (answer ? citationTargets(answer) : new Map()), [answer]);
  const cite = useCallback((id: string) => answer && onCite(answer, id), [answer, onCite]);

  if (message.status === "running" || message.status === "cancelling") {
    const steps = message.progress.filter((p) => p.step !== "queued");
    const last = steps.length ? steps[steps.length - 1].label : "מעבד את הבקשה";
    return (
      <div className="msg msg-assistant" aria-live="polite">
        <div className="status-line">
          <span className="dot-pulse" aria-hidden="true" />
          <span>{message.status === "cancelling" ? "עוצר… (ממתין לסיום שלב שכבר נשלח למודל)" : `${last}…`}</span>
          {message.status === "running" && (
            <button type="button" className="btn btn-small" onClick={() => onStop(message)}>
              עצירה
            </button>
          )}
        </div>
        {steps.length > 1 && (
          <ul className="progress-steps" aria-label="שלבי העיבוד">
            {steps.slice(-6).map((p, i) => (
              <li key={`${i}-${p.label}`}>{p.label}</li>
            ))}
          </ul>
        )}
      </div>
    );
  }
  if (message.status === "failed") {
    return (
      <div className="msg msg-assistant">
        <div className="msg-error" role="alert">
          <span>{message.error ?? "אירעה תקלה בעיבוד התשובה."}</span>
          {isLast && (
            <button type="button" className="btn btn-small" onClick={() => onRetry(message)}>
              ניסיון חוזר
            </button>
          )}
        </div>
      </div>
    );
  }
  if (message.status === "cancelled") {
    return (
      <div className="msg msg-assistant">
        <div className="msg-cancelled">
          <span>העיבוד נעצר. לא נוצרה תשובה.</span>
          {isLast && (
            <button type="button" className="btn btn-small" onClick={() => onRetry(message)}>
              לנסות שוב
            </button>
          )}
        </div>
      </div>
    );
  }
  if (!answer) return null;
  if (answer.hidden) {
    return (
      <div className="msg msg-assistant">
        <div className="limited-note">התשובה הוסתרה: אחד המקורות שלה נמחק או שאין עוד הרשאה אליו.</div>
      </div>
    );
  }

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(plainAnswer(answer.markdown));
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="msg msg-assistant">
      {answer.kind === "search_only" && (
        <div className="limited-note">תוצאות חיפוש בלבד — המודל לא ניתח את המקורות.</div>
      )}
      {message.stale && (
        <div className="stale-note">המסמכים או ההרשאות השתנו מאז התשובה. מומלץ לשאול שוב לקבלת תשובה עדכנית.</div>
      )}
      <div className="body" dir="rtl">
        <Markdown markdown={answer.markdown} citations={citations} onCite={cite} />
      </div>
      <div className="msg-actions">
        <button type="button" className="icon-btn" onClick={copy} aria-label="העתקת התשובה" title="העתקה">
          {copied ? "✓" : "⧉"}
        </button>
        {isLast && (
          <button type="button" className="icon-btn" onClick={() => onRetry(message)} aria-label="יצירת תשובה מחדש" title="תשובה מחדש">
            ↻
          </button>
        )}
        {answer.status === "partial" && <span className="note">תשובה חלקית</span>}
        {answer.status === "not_found" && <span className="note">לא נמצא במקורות</span>}
      </div>
      <AnswerDetails answer={answer} citations={citations} onCite={cite} />
    </div>
  );
}

function AnswerDetails({
  answer,
  citations,
  onCite,
}: {
  answer: ChatAnswer;
  citations: Map<string, CitationTarget>;
  onCite: (id: string) => void;
}) {
  const ordered = [...citations.values()];
  const verification = answer.verification;
  const changed = verification ? verification.removed + verification.partial + verification.annotated : 0;
  const coverage = answer.coverage.at(-1);
  return (
    <details className="msg-details">
      <summary>מקורות ופרטים ({ordered.length})</summary>
      {ordered.length > 0 && (
        <div className="details-section">
          <h4>מקורות</h4>
          <ol>
            {ordered.map((c) => (
              <li key={c.id}>
                <button type="button" className="source-link" onClick={() => onCite(c.id)}>
                  {c.title}
                </button>
              </li>
            ))}
          </ol>
        </div>
      )}
      {answer.measurements.length > 0 && (
        <div className="details-section">
          <h4>נתונים שבהם השתמשה התשובה</h4>
          <ul>
            {answer.measurements.map((m) => (
              <li key={m.id}>
                {m.metric}: <bdi>{m.value_text}</bdi>
                {[PERIOD[m.period], VAT[m.vat], m.area_basis ? `בסיס שטח: ${m.area_basis}` : ""]
                  .filter(Boolean)
                  .map((x) => ` · ${x}`)
                  .join("")}
                {" — "}
                {m.title} ({STATUS[m.status] ?? m.status})
              </li>
            ))}
          </ul>
        </div>
      )}
      {answer.computations.length > 0 && (
        <div className="details-section">
          <h4>חישובים</h4>
          <ul>
            {answer.computations.map((c) => (
              <li key={c.id}>
                {c.operation} = <bdi>{c.result}</bdi> {c.unit} · {c.inputs.length} ערכים מ-{c.documents} מסמכים
                {c.note ? ` · ${c.note}` : ""}
              </li>
            ))}
          </ul>
        </div>
      )}
      {answer.ledger && <LedgerSection ledger={answer.ledger} />}
      {coverage && (
        <div className="details-section">
          <h4>נתונים כמותיים בתחום</h4>
          <p>
            {coverage.extracted} מתוך {coverage.documents} מסמכים בתחום חולצו לנתונים כמותיים.
            {coverage.not_extracted.length > 0 && ` טרם חולצו: ${coverage.not_extracted.join("; ")}.`}
            {coverage.partially_read.length > 0 && ` נקראו חלקית: ${coverage.partially_read.join("; ")}.`}
          </p>
        </div>
      )}
      {answer.missing && (
        <div className="details-section">
          <h4>מה חסר</h4>
          <p>{answer.missing}</p>
        </div>
      )}
      {verification && changed > 0 && (
        <div className="details-section" data-testid="verification">
          <h4>אימות</h4>
          <ul>
            {verification.removed > 0 && <li>הוסרו {verification.removed} טענות שלא נמצאה להן תמיכה במקורות.</li>}
            {verification.partial > 0 && <li>{verification.partial} טענות אומתו חלקית (מסומנות בתשובה).</li>}
            {verification.annotated > 0 && (
              <li>ל-{verification.annotated} נתונים נוסף ליד המספר תיאור שנכתב במקור (מסומן «כפי שנכתב במקור»).</li>
            )}
          </ul>
        </div>
      )}
      {verification && changed === 0 && (
        <div className="details-section">
          <h4>אימות</h4>
          <p>{verification.judged ? "כל הטענות נבדקו מול המקורות המצוטטים." : "הטענות בתשובה זו לא נבדקו מול המקורות (תשובה מגרסה קודמת, שבה בדיקה שנכשלה לא עצרה את התשובה). יש לבדוק במקור לפני שימוש."}</p>
        </div>
      )}
      {answer.searches.length > 0 && (
        <div className="details-section">
          <h4>חיפושים שבוצעו</h4>
          <ul>
            {answer.searches.map((q, i) => (
              <li key={i}>
                <bdi>{q}</bdi>
              </li>
            ))}
          </ul>
        </div>
      )}
    </details>
  );
}

export function UserMessage({ message, error, onResend }: { message: ChatMessage; error?: string | null; onResend?: () => void }) {
  return (
    <div className="msg msg-user">
      <div>
        <div className="bubble" dir="auto">
          {message.content}
        </div>
        {error && (
          <div className="send-error" role="alert">
            {error}{" "}
            {onResend && (
              <button type="button" className="source-link" onClick={onResend}>
                שליחה חוזרת
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}


function titles(docs: LedgerDocument[] | undefined): string {
  return (docs ?? []).map((d) => d.title ?? "").filter(Boolean).join("; ");
}

/** The cited tables the answer presented only part of. */
function shortTables(tables: LedgerTable[] | undefined): LedgerTable[] {
  return (tables ?? []).filter((t) => t.presented < t.rows);
}

/** Rows of the cited tables the answer did not present. */
function TableLines({ tables }: { tables: LedgerTable[] | undefined }) {
  const short = shortTables(tables);
  return (
    <>
      {short.map((t, i) => (
        <li key={`t${i}`} data-testid="ledger-table">
          טבלה ב<bdi>{t.title}</bdi>: הוצגו ערכים מ-{t.presented} מתוך {t.rows} שורות
        </li>
      ))}
    </>
  );
}

/** The coverage ledger: the set the question was about, how deep each document was read, what the answer used,
 * and what it did not. Document coverage (a verified datum from each document) is shown apart from data
 * coverage (sections and tables read, or only passages retrieved). */
function LedgerSection({ ledger }: { ledger: ChatLedger }) {
  if (ledger.scope_kind === "focused") {
    if (!ledger.also_matching?.length && !shortTables(ledger.tables).length) return null;
    return (
      <div className="details-section ledger" data-testid="ledger">
        <h4>כיסוי</h4>
        <ul>
          {ledger.also_matching?.length ? (
            <li>מסמכים נוספים שכותרתם מתאימה לשאלה ולא נבדקו: {titles(ledger.also_matching)}.</li>
          ) : null}
          <TableLines tables={ledger.tables} />
        </ul>
      </div>
    );
  }
  const rows: [string, LedgerDocument[] | undefined][] = [
    ["לא נבדקו", ledger.not_checked],
    ["נשלפו מהם קטעים בלבד (הסעיף או הטבלה לא נקראו)", ledger.retrieved_only],
    ["נבדקו, לא נמצא בהם נתון שנכלל בתשובה", ledger.unused],
    ["נקראו חלקית", ledger.partially_read],
  ];
  return (
    <div className="details-section ledger" data-testid="ledger">
      <h4>כיסוי</h4>
      <p data-testid="ledger-documents">
        תחום: <bdi>{ledger.scope_query}</bdi> · {ledger.matching?.length ?? 0} מסמכים מתאימים · נתון מאומת מ-
        {ledger.with_data?.length ?? 0}
        {ledger.complete ? " · התשובה מכסה את כל מסמכי התחום" : " · התשובה אינה מכסה את כל מסמכי התחום"}
      </p>
      {/* answers stored before reading levels were recorded have no data-coverage figures to show */}
      {ledger.levels && (
        <p data-testid="ledger-data">
          קריאה: נקראו (סעיף/טבלה) {ledger.read?.length ?? 0} · נשלפו קטעים בלבד מ-{ledger.retrieved_only?.length ?? 0}
        </p>
      )}
      <ul>
        {rows
          .filter(([, docs]) => docs && docs.length > 0)
          .map(([label, docs]) => (
            <li key={label}>
              {label}: {titles(docs)}
            </li>
          ))}
        {(ledger.omitted ?? []).map((o, i) => (
          <li key={`o${i}`}>
            הושמט{o.title ? ` (${o.title})` : ""}: {o.what} — {o.why}
          </li>
        ))}
        <TableLines tables={ledger.tables} />
      </ul>
    </div>
  );
}
