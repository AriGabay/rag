"use client";

import { useCallback, useMemo, useState } from "react";
import type {
  ChatAnswer,
  ChatAssumption,
  ChatComputation,
  ChatComputationInput,
  ChatLedger,
  ChatMessage,
  ChatRequirement,
  ChatSource,
  ChatValue,
  ChatVerification,
  LedgerDocument,
  LedgerTable,
  ViewerNav,
  ViewerTarget,
} from "@/lib/chatTypes";
import {
  VALUE_STATUS,
  type ValueStatus,
  chatValueStatus,
  measurementValueStatus,
  valueStatusText,
} from "@/lib/format";
import { type CitationTarget, Markdown, citationOrder, plainAnswer } from "./Markdown";
import { documentAnchor } from "./SourceViewer";

export interface CitedItem {
  /** A passage, or the document passage behind a measurement or computation input. */
  source: ChatSource | null;
  label: string;
  id: string;
}

const KIND_LABEL: Record<string, string> = { M: "נתון", C: "חישוב", S: "מקור", V: "ערך", A: "הנחה" };
const RESULT_KIND: Record<string, string> = {
  scenario: "תרחיש לפי בקשה",
  reproduces_report_value: "משחזר ערך מהשומה",
  computed: "חישוב",
};
const VAT: Record<string, string> = { included: "כולל מע״מ", excluded: "ללא מע״מ", unknown: "מע״מ לא צוין", not_applicable: "" };
const PERIOD: Record<string, string> = { month: "לחודש", year: "לשנה", one_time: "חד-פעמי", none: "", unknown: "תקופה לא צוינה" };
const REQUIREMENT_STATUS: Record<string, string> = {
  full: "ניתן במלואו",
  partial: "ניתן בחלקו",
  missing: "חסר",
  undeterminable: "לא ניתן להכריע",
};
const COMPLETENESS_LEAD: Record<string, string> = {
  partial: "התשובה חלקית",
  missing: "התשובה אינה עונה על השאלה",
  undeterminable: "לא ניתן להכריע מהמסמכים",
};
const CORRECTNESS_LINE: Record<string, string> = {
  partial: "חלק מהטענות הוסרו או אומתו רק בחלקן מול המקורות (פירוט ב«מקורות ופרטים»).",
  unverified: "הטענות בתשובה לא אומתו מול המקורות; יש לבדוק במקור לפני שימוש.",
};

// an answer's source is bound to the reading it was read from (null: an answer from before reading ids)
function withReading(src: ChatSource): ChatSource {
  return { ...src, reading_id: src.reading_id ?? null };
}

/** The source a citation opens: a passage itself, or the passage of the value or measurement it cites; null for a
 * calculation (C#) or an assumption (A#), which have no place in a document (`citedView` opens their own views). */
export function citedSource(answer: ChatAnswer, id: string): ChatSource | null {
  const direct = answer.sources.find((s) => s.id === id);
  if (direct) return withReading(direct);
  // a value opens the passage it was verified in
  const v = answer.values?.find((x) => x.id === id);
  if (v) {
    const src = answer.sources.find((s) => s.id === v.source_id);
    return src ? withReading(src) : null;
  }
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
      reading_id: m.reading_id ?? null,
    };
  }
  return null;
}

/** A value's status (V#): checked automatically in its passage, uncertain, or unread (never a person's decision). */
function valueStatusOf(answer: ChatAnswer, v: ChatValue): ValueStatus {
  const src = answer.sources.find((s) => s.id === v.source_id);
  return chatValueStatus(v.certainty, src?.status, v.reading);
}

/** What the source viewer opens for a citation: its passage (the text view) and its anchor (the page or structured
 * view), with a value's or measurement's status; null for an id with no place in a document (C#, A#). */
export function citedTarget(answer: ChatAnswer, id: string): ViewerTarget | null {
  const source = citedSource(answer, id);
  if (!source) return null;
  const s = answer.sources.find((x) => x.id === id);
  if (s) return { id, source, anchor: documentAnchor(s.anchor) };
  const v = answer.values?.find((x) => x.id === id);
  if (v) {
    // the cell or quoted words it was taken from; else the passage it was verified in
    const passage = answer.sources.find((x) => x.id === v.source_id);
    return {
      id,
      source,
      anchor: documentAnchor(v.anchor) ?? documentAnchor(passage?.anchor),
      valueStatus: valueStatusOf(answer, v),
    };
  }
  const m = answer.measurements.find((x) => x.id === id);
  if (m) return { id, source, anchor: documentAnchor(m.anchor), valueStatus: measurementValueStatus(m) };
  return null;
}

/** The viewer's targets for a chip: the answer's citations in answer order (those with a place in a document), the
 * clicked one current. */
export function answerNav(answer: ChatAnswer, id: string): ViewerNav | null {
  const items: ViewerTarget[] = [];
  for (const cid of citationOrder(answer.markdown)) {
    const t = citedTarget(answer, cid);
    if (t) items.push(t);
  }
  let index = items.findIndex((t) => t.id === id);
  if (index < 0) {
    // cited only in the details (not in the text): it opens alone
    const t = citedTarget(answer, id);
    if (!t) return null;
    items.splice(0, items.length, t);
    index = 0;
  }
  return { items, index };
}

/** What a chip opens: S#, V# and M# the source viewer at their place (moving through the answer's citations); C# its
 * calculation breakdown; A# the user's assumption with the words it was quoted from (U7). */
export type CitedView = { kind: "source"; nav: ViewerNav } | { kind: "calculation"; id: string } | { kind: "assumption"; id: string };

export function citedView(answer: ChatAnswer, id: string): CitedView | null {
  if (answer.computations.some((c) => c.id === id)) return { kind: "calculation", id };
  if (answer.assumptions?.some((a) => a.id === id)) return { kind: "assumption", id };
  const nav = answerNav(answer, id);
  return nav ? { kind: "source", nav } : null;
}

function citationTargets(answer: ChatAnswer): Map<string, CitationTarget> {
  const out = new Map<string, CitationTarget>();
  citationOrder(answer.markdown).forEach((id, i) => {
    const s = answer.sources.find((x) => x.id === id);
    const m = answer.measurements.find((x) => x.id === id);
    const c = answer.computations.find((x) => x.id === id);
    const v = answer.values?.find((x) => x.id === id);
    const a = answer.assumptions?.find((x) => x.id === id);
    let title = KIND_LABEL[id[0]] ?? "מקור";
    if (s) title = `${s.title} — ${s.location}`;
    else if (m) title = `${m.title} — ${m.metric}: ${m.value_text}`;
    else if (v) title = `${v.label}: ${v.value_text} — ${v.title}`;
    else if (a) title = `הנחה שלך: ${a.label} = ${assumptionText(a)}`;
    else if (c?.formula) title = `חישוב: ${c.label ?? c.formula}`;
    else if (c) title = `חישוב: ${c.operation} על ${c.inputs.length} ערכים`;
    out.set(id, { id, n: i + 1, label: id, title });
  });
  return out;
}

interface AssistantProps {
  message: ChatMessage;
  /** Only the conversation's last answer can be generated again (an earlier one would land out of order). */
  isLast: boolean;
  /** A citation: what it opens depends on the answer and on where the answer sits in the thread (an assumption's
   * message). */
  onCite: (answer: ChatAnswer, id: string, message: ChatMessage) => void;
  onRetry: (message: ChatMessage) => void;
  onStop: (message: ChatMessage) => void;
}

export function AssistantMessage({ message, isLast, onCite, onRetry, onStop }: AssistantProps) {
  const [copied, setCopied] = useState(false);
  const answer = message.answer;
  const citations = useMemo(() => (answer ? citationTargets(answer) : new Map()), [answer]);
  const cite = useCallback((id: string) => answer && onCite(answer, id, message), [answer, onCite, message]);

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
    <div className="msg msg-assistant" data-message-id={message.id}>
      {answer.kind === "search_only" && (
        <div className="limited-note">תוצאות חיפוש בלבד — המודל לא ניתח את המקורות.</div>
      )}
      {message.stale && (
        <div className="stale-note">המסמכים או ההרשאות השתנו מאז התשובה. מומלץ לשאול שוב לקבלת תשובה עדכנית.</div>
      )}
      <div className="body" dir="rtl">
        <Markdown markdown={answer.markdown} citations={citations} onCite={cite} />
      </div>
      {answer.verification && <AnswerQuality verification={answer.verification} />}
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

/** Claim correctness and answer completeness, apart (R19): nothing for a complete answer whose claims all held;
 * otherwise one short line for each that falls short. Answers stored before either was reported show nothing new. */
function AnswerQuality({ verification }: { verification: ChatVerification }) {
  const completeness = verification.completeness;
  const correctness = verification.correctness;
  const incomplete = completeness && completeness.status !== "full";
  const doubtful = correctness && correctness !== "verified";
  if (!incomplete && !doubtful) return null;
  return (
    <div className="answer-quality">
      {incomplete && (
        <p className="completeness-line" data-testid="completeness" data-status={completeness.status}>
          <strong>{COMPLETENESS_LEAD[completeness.status] ?? "התשובה חלקית"}:</strong>{" "}
          {completeness.missing.map((m, i) => (
            <span key={m.id} data-testid="completeness-gap">
              {i > 0 && "; "}
              <bdi>{m.text}</bdi>
              {m.status === "partial" && " (ניתן בחלקו)"}
              {m.reason_text && <> — {m.reason_text}</>}
            </span>
          ))}
        </p>
      )}
      {doubtful && (
        <p className="correctness-line" data-testid="correctness" data-status={correctness}>
          {CORRECTNESS_LINE[correctness]}
        </p>
      )}
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
                {m.title}{" "}
                <ValueStatusBadge status={measurementValueStatus(m)} />
              </li>
            ))}
          </ul>
        </div>
      )}
      {answer.computations.length > 0 && <CalculationsSection answer={answer} onCite={onCite} />}
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
        <div className="details-section" data-testid="verification">
          <h4>אימות</h4>
          <p>{checkedText(verification)}</p>
        </div>
      )}
      {answer.ledger?.requirements && answer.ledger.requirements.length > 0 && (
        <RequirementsSection requirements={answer.ledger.requirements} />
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

/** What verification says when it changed nothing: "every claim held" only when the claims were verified and the
 * answer gives everything the question asked; never a blanket claim over an incomplete or partly verified answer. */
function checkedText(v: ChatVerification): string {
  if (!v.judged) {
    return "הטענות בתשובה זו לא נבדקו מול המקורות (תשובה מגרסה קודמת, שבה בדיקה שנכשלה לא עצרה את התשובה). יש לבדוק במקור לפני שימוש.";
  }
  if (v.correctness && v.correctness !== "verified") return CORRECTNESS_LINE[v.correctness];
  if (v.completeness && v.completeness.status !== "full") {
    return "הטענות שבתשובה נבדקו מול המקורות המצוטטים, אך התשובה אינה עונה על כל מה שנשאל (פירוט בדרישות השאלה).";
  }
  return "כל הטענות נבדקו מול המקורות המצוטטים.";
}

/** Each requirement of the question and how the answer gave it, with the reason a part is missing (R19, R21). */
function RequirementsSection({ requirements }: { requirements: ChatRequirement[] }) {
  return (
    <div className="details-section" data-testid="requirements">
      <h4>דרישות השאלה</h4>
      <ul>
        {requirements.map((r) => (
          <li key={r.id} data-testid="requirement" data-status={r.status}>
            <bdi>{r.text}</bdi>
            {r.calculation && " (חישוב)"} — {REQUIREMENT_STATUS[r.status] ?? r.status}
            {r.status !== "full" && r.limitation_text && <> · {r.limitation_text}</>}
            {r.status !== "full" && r.reason && <span className="muted"> · {r.reason}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** A value's status with its icon and words, and its meaning on hover (R25). */
function ValueStatusBadge({ status }: { status: ValueStatus }) {
  return (
    <span className="value-status" data-testid="value-status" data-status={status} title={VALUE_STATUS[status].description}>
      ({valueStatusText(status)})
    </span>
  );
}

export function assumptionText(a: ChatAssumption): string {
  return a.unit === "percent" && !a.value_text.includes("%") ? `${a.value_text}%` : a.value_text;
}

export function resultText(c: ChatComputation): string {
  const d = c.display;
  if (!d) return `${c.result ?? ""} ${c.unit}`.trim();
  // a ratio reads as a percentage; an amount with its unit
  if (d.percent && (c.unit === "" || c.unit === "%")) return d.percent;
  return `${d.value} ${c.unit}`.trim();
}

function InputLine({ input, onCite }: { input: ChatComputationInput; onCite: (id: string) => void }) {
  const shown = input.value_text ?? input.display ?? "";
  // every input opens: a document input its place, an assumption the user's words, a calculation its breakdown
  const link = (
    <button type="button" className="source-link" onClick={() => onCite(input.id)}>
      {input.id}
    </button>
  );
  if (input.kind === "assumption") {
    return (
      <li>
        {link} {input.label}: <bdi>{shown}</bdi> — הנחה שלך
      </li>
    );
  }
  return (
    <li>
      {link}{" "}
      {input.label}: <bdi>{shown}</bdi>
      {input.kind === "value" && input.certainty === "model_asserted" && " · חלק ממשמעות הערך נקבע ולא נמצא במקור"}
    </li>
  );
}

/** Each calculation with its formula in Hebrew labels, its inputs linked to their sources, what it is (a scenario
 * on request, a value the report writes, or a computation) and whether it is conditional; then the document data
 * it rests on, and apart from it, the user's own assumptions with the words they were quoted from. */
function CalculationsSection({ answer, onCite }: { answer: ChatAnswer; onCite: (id: string) => void }) {
  const values: ChatValue[] = answer.values ?? [];
  const assumptions: ChatAssumption[] = answer.assumptions ?? [];
  return (
    <div className="details-section calculations" data-testid="calculations">
      <h4>חישובים</h4>
      <ul>
        {answer.computations.map((c) =>
          c.formula ? (
            <li key={c.id} data-testid="calculation">
              <div>
                <strong>
                  {c.id} · {c.label}
                </strong>{" "}
                {c.result_kind && (
                  <span
                    className={`badge ${c.result_kind === "scenario" ? "badge-warn" : c.result_kind === "reproduces_report_value" ? "badge-ok" : "badge-info"}`}
                    data-testid="calc-kind"
                  >
                    {RESULT_KIND[c.result_kind] ?? c.result_kind_label}
                  </span>
                )}
              </div>
              <div className="calc-formula">
                <bdi>{c.formula}</bdi> = <bdi>{resultText(c)}</bdi>
              </div>
              <ul className="calc-inputs">
                {c.inputs
                  .filter((x): x is ChatComputationInput => typeof x !== "string")
                  .map((x) => (
                    <InputLine key={x.id} input={x} onCite={onCite} />
                  ))}
              </ul>
              {c.conditional && (
                <div className="calc-conditional" data-testid="calc-conditional">
                  <strong>מותנה:</strong> {(c.conditions ?? []).join("; ")}
                  {c.justification ? ` — לפי ההצדקה: ${c.justification}` : ""}
                </div>
              )}
              {c.reproduces && (
                <div>
                  שווה לערך <bdi>{c.reproduces.as_written}</bdi> שכתוב במקור
                </div>
              )}
              {c.note && <div className="calc-note">{c.note}</div>}
            </li>
          ) : (
            <li key={c.id}>
              {c.operation} = <bdi>{c.result}</bdi> {c.unit} · {c.inputs.length} ערכים מ-{c.documents} מסמכים
              {c.note ? ` · ${c.note}` : ""}
            </li>
          ),
        )}
      </ul>
      {values.length > 0 && (
        <div className="calc-group" data-testid="calc-document-data">
          <h4>נתונים מהמסמכים</h4>
          <ul>
            {values.map((v) => (
              <li key={v.id}>
                <button type="button" className="source-link" onClick={() => onCite(v.id)}>
                  {v.id}
                </button>{" "}
                {v.label}: <bdi>{v.value_text}</bdi>
                {[v.unit_label, PERIOD[v.period], VAT[v.vat], v.area_basis ? `בסיס שטח: ${v.area_basis}` : ""]
                  .filter(Boolean)
                  .map((x) => ` · ${x}`)
                  .join("")}
                {" — "}
                {v.title}, {v.locator.row ? `שורה «${v.locator.row}», עמודה «${v.locator.column}»` : v.location}
                {v.certainty === "model_asserted" && " · חלק ממשמעות הערך נקבע ולא נמצא במקור"}{" "}
                <ValueStatusBadge status={valueStatusOf(answer, v)} />
              </li>
            ))}
          </ul>
        </div>
      )}
      {assumptions.length > 0 && (
        <div className="calc-group calc-assumptions" data-testid="calc-assumptions">
          <h4>הנחות שלך (לא מהמסמכים)</h4>
          <ul>
            {assumptions.map((a) => (
              <li key={a.id}>
                {a.id} {a.label}: <bdi>{assumptionText(a)}</bdi> — לפי מה שכתבת{a.current ? "" : " בהודעה קודמת"}: «
                <bdi>{a.quote}</bdi>»
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

export function UserMessage({ message, error, onResend }: { message: ChatMessage; error?: string | null; onResend?: () => void }) {
  return (
    // the target of an assumption's "go to your message" (U7): found by its id, focused without a tab stop
    <div className="msg msg-user" data-message-id={message.id} tabIndex={-1}>
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
  if (ledger.membership) {
    return (
      <div className="details-section ledger" data-testid="ledger">
        <h4>כיסוי</h4>
        <p data-testid="ledger-documents">
          {ledger.scope_query ? (
            <>
              תחום: <bdi>{ledger.scope_query}</bdi> ·{" "}
            </>
          ) : null}
          {ledger.matching?.length ?? 0} מסמכים ברשימה · נבדקו לפי התאמת המונחים; תוכן המסמכים לא נקרא
          {ledger.complete
            ? " · כל עמודי הרשימה נקראו"
            : ` · נקראו ${ledger.pages_read ?? 0} מתוך ${ledger.pages ?? 1} עמודי הרשימה (ספירה חלקית)`}
        </p>
      </div>
    );
  }
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
