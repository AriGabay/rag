"use client";

import { type RefObject, useEffect, useRef, useState } from "react";
import type {
  ChatAnswer,
  ChatAssumption,
  ChatComputation,
  ChatComputationInput,
  ViewerNav,
  ViewerTarget,
} from "@/lib/chatTypes";
import { VALUE_STATUS, valueStatusText } from "@/lib/format";
import { assumptionText, citedTarget, resultText } from "./Message";
import "./chat.css";

// The calculation breakdown (U7, R14, R16, KTD6): a computed result (C#) opens its formula, its steps, its full value
// and how it is rounded for display, its unit and VAT basis, whether it is conditional, and each input it rests on.
// A document input (V#, M#) opens the source viewer at its own place, stacked above the breakdown; a user assumption
// (A#) is shown as the user's own words with a way back to the message they wrote; an earlier calculation opens its
// own breakdown. A computed number is never presented as written in a document, unless the server found that it
// reproduces a number the report writes.

/** The answer a breakdown belongs to, and where it sits in the thread (to find the user's message again). */
export interface AnswerOrigin {
  answer: ChatAnswer;
  /** The assistant message that holds the answer. */
  messageId: string;
  /** The user message it answered. */
  replyTo: string | null;
}

const RESULT_BADGE: Record<string, string> = {
  reproduces_report_value: "כתוב בשומה",
  scenario: "חושב עכשיו לפי בקשתך",
  computed: "חושב עכשיו",
};
const RESULT_STATEMENT: Record<string, string> = {
  reproduces_report_value: "המספר הזה כתוב במקור; החישוב משחזר אותו מנתוני המסמך.",
  scenario: "תרחיש שחושב עכשיו לפי בקשתך, על סמך הנחה שנתת. המספר עצמו אינו כתוב במסמך.",
  computed: "חושב עכשיו מנתוני המסמכים. המספר עצמו אינו כתוב במסמך.",
};
const VAT_BASIS: Record<string, string> = {
  included: "כל סכומי הכסף שבחישוב כוללים מע״מ",
  excluded: "כל סכומי הכסף שבחישוב אינם כוללים מע״מ",
};
const VAT_MIXED = "לא צוין בסיס מע״מ אחיד לסכומים שבחישוב";
const MODEL_ASSERTED = "חלק ממשמעות הערך (יחידה, תקופה או מע״מ) נקבע ולא נמצא במקור";
const NOT_CONDITIONAL = "התוצאה אינה מותנית";
const MESSAGE_MISSING = "ההודעה אינה מוצגת כרגע בשיחה (ייתכן שהיא בין ההודעות הקודמות שלא נטענו).";
const NO_PLACE = "למקור הזה אין מיקום שאפשר לפתוח";

// --- helpers ------------------------------------------------------------------------------------------------------

/** A full decimal with thousands separated, never rounded. */
function grouped(value: string): string {
  const m = /^(-?)(\d+)(\.\d+)?$/.exec(value.trim());
  if (!m) return value;
  return `${m[1]}${m[2].replace(/\B(?=(\d{3})+(?!\d))/g, ",")}${m[3] ?? ""}`;
}

/** Whether the result reads as a percentage (as `resultText` shows it). */
function shownAsPercent(c: ChatComputation): boolean {
  return Boolean(c.display?.percent) && (c.unit === "" || c.unit === "%");
}

/** How the displayed number is rounded: the server's reading format (`calc.fmt`) keeps 2 decimal places, or 4 below
 * one, rounding half up and dropping trailing zeros; a ratio is shown as a percentage (× 100) first. */
function roundingRule(c: ChatComputation): string {
  const full = Number(c.value ?? c.result);
  if (!Number.isFinite(full)) return "";
  // an answer stored before the calculator shows its full result as it is
  if (!c.display) return "מוצג ללא עיגול: זה הערך המלא";
  const ratio = shownAsPercent(c) && c.unit === "";
  const base = ratio ? full * 100 : full;
  const places = Math.abs(base) >= 1 || base === 0 ? 2 : 4;
  const shown = Number((ratio || c.unit === "%" ? c.display?.percent : c.display?.value)?.replace(/[,%]/g, "") ?? NaN);
  const exact = Number.isFinite(shown) && Math.abs(shown - base) <= Math.abs(base) * 1e-12;
  const lead = ratio ? "מוצג כאחוז (הערך × 100), " : "מוצג ";
  if (exact) return `${lead}ללא עיגול: זה הערך המלא`;
  return `${lead}מעוגל ל-${places} ספרות אחרי הנקודה (חצי ומעלה כלפי מעלה)`;
}

type InputKind = ChatComputationInput["kind"] | "source";

interface InputRow {
  id: string;
  kind: InputKind;
  label: string;
  shown: string;
  input: ChatComputationInput | null;
  /** A document input's place (null: it has none the viewer can open). */
  target: ViewerTarget | null;
  assumption: ChatAssumption | null;
  computation: ChatComputation | null;
}

function kindOfId(id: string): InputKind {
  switch (id[0]) {
    case "V":
      return "value";
    case "M":
      return "measurement";
    case "A":
      return "assumption";
    case "C":
      return "computation";
    default:
      return "source";
  }
}

/** The inputs of a calculation, resolved against the answer; an answer stored before the calculator lists ids only. */
function inputRows(answer: ChatAnswer, c: ChatComputation): InputRow[] {
  return c.inputs.map((raw) => {
    const input = typeof raw === "string" ? null : raw;
    const id = input?.id ?? (raw as string);
    const kind: InputKind = input?.kind ?? kindOfId(id);
    const assumption = answer.assumptions?.find((a) => a.id === id) ?? null;
    const computation = kind === "computation" ? answer.computations.find((x) => x.id === id) ?? null : null;
    const value = answer.values?.find((v) => v.id === id) ?? null;
    const measurement = answer.measurements.find((m) => m.id === id) ?? null;
    const target = kind === "value" || kind === "measurement" || kind === "source" ? citedTarget(answer, id) : null;
    const label =
      input?.label ?? value?.label ?? measurement?.metric ?? assumption?.label ?? computation?.label ?? target?.source.title ?? id;
    let shown = input?.value_text ?? input?.display ?? input?.value ?? "";
    if (!input) {
      shown = value?.value_text ?? measurement?.value_text ?? (assumption ? assumptionText(assumption) : "");
      if (computation) shown = resultText(computation);
    } else if (kind === "assumption" && assumption) {
      shown = assumptionText(assumption);
    } else if (kind === "computation" && computation) {
      shown = resultText(computation);
    }
    return { id, kind, label, shown, input, target, assumption, computation };
  });
}

function normalized(text: string): string {
  return text.replace(/\s+/g, " ").trim();
}

/** The user message an assumption quotes, in the thread: by its stored id, else the message the answer replied to
 * (for the current one), else the latest user message before the answer that contains the quote. */
export function findQuotedMessage(a: Pick<ChatAssumption, "message_id" | "current" | "quote">, origin: AnswerOrigin) {
  if (typeof document === "undefined") return null;
  const byId = (id: string | null | undefined) =>
    id ? document.querySelector<HTMLElement>(`.msg-user[data-message-id="${CSS.escape(id)}"]`) : null;
  const direct = byId(a.message_id) ?? (a.current ? byId(origin.replyTo) : null);
  if (direct) return direct;
  const quote = normalized(a.quote ?? "");
  if (!quote) return null;
  const users: HTMLElement[] = [];
  for (const el of document.querySelectorAll<HTMLElement>(".chat-thread [data-message-id]")) {
    if (el.dataset.messageId === origin.messageId) break;
    if (el.classList.contains("msg-user")) users.push(el);
  }
  return users.reverse().find((el) => normalized(el.textContent ?? "").includes(quote)) ?? null;
}

// --- shared pieces --------------------------------------------------------------------------------------------------

function PanelHeader({
  title,
  subtitle,
  label,
  back,
  closeRef,
  onClose,
}: {
  title: string;
  subtitle: string | null;
  label: string;
  /** The level below, when the panel was opened from it: a back step to it. */
  back: string | null;
  closeRef: RefObject<HTMLButtonElement | null>;
  onClose: () => void;
}) {
  return (
    <header>
      <div className="t">
        {back && (
          <button type="button" className="source-link calc-back" onClick={onClose} data-testid="calc-back">
            → חזרה ל{back}
          </button>
        )}
        <strong data-testid="calc-title">{title}</strong>
        {subtitle && <span>{subtitle}</span>}
      </div>
      <div className="viewer-header-actions">
        <button ref={closeRef} type="button" className="icon-btn" onClick={onClose} aria-label={label} data-testid="calc-close">
          ✕
        </button>
      </div>
    </header>
  );
}

function useInitialFocus(hidden: boolean | undefined): RefObject<HTMLButtonElement | null> {
  const ref = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!hidden) ref.current?.focus();
    // focus moves into the panel when it opens; a level shown again gets focus back from the stack
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return ref;
}

/** "Go to your message": scrolls the thread to the user message an assumption quotes, or says it is not shown. */
function MessageLink({
  assumption,
  origin,
  onShowMessage,
  testId,
}: {
  assumption: Pick<ChatAssumption, "message_id" | "current" | "quote">;
  origin: AnswerOrigin;
  onShowMessage: (el: HTMLElement) => void;
  testId: string;
}) {
  const [missing, setMissing] = useState(false);
  return (
    <>
      <button
        type="button"
        className="source-link"
        data-testid={testId}
        aria-label="מעבר להודעה שבה כתבת את ההנחה"
        onClick={() => {
          const el = findQuotedMessage(assumption, origin);
          setMissing(!el);
          if (el) onShowMessage(el);
        }}
      >
        להודעה שלך
      </button>
      {missing && (
        <span className="muted calc-missing" role="status">
          {" "}
          {MESSAGE_MISSING}
        </span>
      )}
    </>
  );
}

// --- the breakdown --------------------------------------------------------------------------------------------------

interface CalculationViewProps {
  origin: AnswerOrigin;
  /** The C# shown. */
  id: string;
  hidden?: boolean;
  back: string | null;
  onClose: () => void;
  /** Opens the source viewer above the breakdown, moving through the breakdown's document inputs. */
  onOpenSource: (nav: ViewerNav) => void;
  /** Opens the breakdown of an input that is itself a calculation, above this one. */
  onOpenCalculation: (id: string) => void;
  onShowMessage: (el: HTMLElement) => void;
}

export function CalculationView({
  origin,
  id,
  hidden,
  back,
  onClose,
  onOpenSource,
  onOpenCalculation,
  onShowMessage,
}: CalculationViewProps) {
  const closeRef = useInitialFocus(hidden);
  const answer = origin.answer;
  const c = answer.computations.find((x) => x.id === id);
  if (!c) return null;
  const rows = inputRows(answer, c);
  const documents = rows.filter((r) => r.kind === "value" || r.kind === "measurement" || r.kind === "source");
  const assumptions = rows.filter((r) => r.kind === "assumption");
  const chained = rows.filter((r) => r.kind === "computation");
  // previous/next in a viewer opened from here moves through these, in input order
  const navItems = documents.map((r) => r.target).filter((t): t is ViewerTarget => t !== null);
  const openInput = (target: ViewerTarget) => onOpenSource({ items: navItems, index: navItems.indexOf(target) });
  const kind = c.result_kind ?? "computed";
  const formula = c.formula ?? c.operation;
  const full = c.value ?? c.result;
  const rule = roundingRule(c);
  const reproducedAt = c.reproduces ? citedTarget(answer, c.reproduces.source) : null;
  const isMoney = (c.unit ?? "").includes("₪");
  return (
    <aside className="source-panel calc-view" aria-label="פירוט החישוב" data-testid="calc-view" data-id={c.id} hidden={hidden}>
      <PanelHeader
        title={c.label ? `${c.label}` : "חישוב"}
        subtitle={`חישוב ${c.id}`}
        label="סגירת פירוט החישוב"
        back={back}
        closeRef={closeRef}
        onClose={onClose}
      />
      <div className="source-body calc-body">
        <section className="calc-block" data-testid="calc-result" data-result-kind={kind}>
          <div className="calc-headline">
            <span className="calc-value">
              <bdi>{resultText(c)}</bdi>
            </span>{" "}
            <span className={`badge ${kind === "reproduces_report_value" ? "badge-ok" : kind === "scenario" ? "badge-warn" : "badge-info"}`} data-testid="calc-kind">
              {RESULT_BADGE[kind] ?? c.result_kind_label ?? RESULT_BADGE.computed}
            </span>
          </div>
          <p className="calc-statement">{RESULT_STATEMENT[kind] ?? RESULT_STATEMENT.computed}</p>
          {c.reproduces && (
            <p data-testid="calc-reproduces">
              כפי שנכתב במקור: «<bdi>{c.reproduces.as_written}</bdi>»
              {reproducedAt && (
                <>
                  {" "}
                  <button
                    type="button"
                    className="source-link"
                    onClick={() => onOpenSource({ items: [reproducedAt], index: 0 })}
                    aria-label="פתיחת המקום במקור שבו כתוב המספר"
                  >
                    הצגה במקור
                  </button>
                </>
              )}
            </p>
          )}
          <dl className="calc-facts">
            {full != null && (
              <div data-testid="calc-unrounded">
                <dt>ערך מלא (לא מעוגל)</dt>
                <dd>
                  <bdi>{grouped(full)}</bdi>
                  {c.unit && ` ${c.unit}`}
                </dd>
              </div>
            )}
            {rule && (
              <div data-testid="calc-rounding">
                <dt>עיגול לתצוגה</dt>
                <dd>{rule}</dd>
              </div>
            )}
            <div data-testid="calc-unit">
              <dt>יחידה</dt>
              <dd>{c.unit || (shownAsPercent(c) ? "יחס (מוצג כאחוז)" : "ללא יחידה")}</dd>
            </div>
            {(c.vat || isMoney) && (
              <div data-testid="calc-vat">
                <dt>מע״מ</dt>
                <dd>{c.vat ? VAT_BASIS[c.vat] : VAT_MIXED}</dd>
              </div>
            )}
          </dl>
          {c.conditional ? (
            <div className="calc-conditional" data-testid="calc-conditional" data-conditional="true">
              <strong>התוצאה מותנית:</strong>
              <ul>
                {(c.conditions ?? []).map((x, i) => (
                  <li key={i}>
                    <bdi>{x}</bdi>
                  </li>
                ))}
              </ul>
              {c.justification && (
                <p>
                  לפי ההצדקה: <bdi>{c.justification}</bdi>
                </p>
              )}
            </div>
          ) : (
            <p className="muted" data-testid="calc-conditional" data-conditional="false">
              {NOT_CONDITIONAL}
            </p>
          )}
        </section>

        <section className="calc-block" aria-label="הנוסחה">
          <h4>נוסחה</h4>
          <p className="calc-formula" data-testid="calc-formula">
            <bdi>{formula}</bdi> = <bdi>{resultText(c)}</bdi>
          </p>
          {c.expression && c.expression !== formula && (
            <p className="calc-expression muted">
              בסימוני הקלטים: <bdi dir="ltr">{c.expression}</bdi>
            </p>
          )}
          {c.steps && c.steps.length > 0 && (
            <ol className="calc-steps" data-testid="calc-steps" aria-label="שלבי החישוב">
              {c.steps.map((s, i) => (
                <li key={i}>
                  <bdi dir="ltr">{s.expression}</bdi> = <bdi>{grouped(s.value)}</bdi>
                </li>
              ))}
            </ol>
          )}
          {c.note && <p className="calc-note">{c.note}</p>}
        </section>

        {documents.length > 0 && (
          <section className="calc-block">
            <h4>נתונים מהמסמכים</h4>
            <ul className="calc-input-list">
              {documents.map((r) => (
                <li key={r.id} data-testid="calc-input" data-kind={r.kind} data-id={r.id}>
                  {r.target ? (
                    <button
                      type="button"
                      className="calc-input-open"
                      onClick={() => openInput(r.target!)}
                      aria-label={`פתיחת ${r.label} במקור`}
                    >
                      <span className="calc-input-id">{r.id}</span> {r.label}: <bdi>{r.shown}</bdi>
                    </button>
                  ) : (
                    <span title={NO_PLACE}>
                      <span className="calc-input-id">{r.id}</span> {r.label}: <bdi>{r.shown}</bdi>
                    </span>
                  )}
                  {r.target && (
                    <span className="calc-input-where muted">
                      {" — "}
                      {r.target.anchor?.location.title ?? r.target.source.title}
                      {", "}
                      {r.target.anchor?.location.label ?? r.target.source.location}
                    </span>
                  )}
                  {r.target?.valueStatus && (
                    <span className="value-status" title={VALUE_STATUS[r.target.valueStatus].description}>
                      {" "}
                      ({valueStatusText(r.target.valueStatus)})
                    </span>
                  )}
                  {r.input?.certainty === "model_asserted" && (
                    <span className="calc-asserted" data-testid="calc-model-asserted">
                      {" · "}
                      {MODEL_ASSERTED}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </section>
        )}

        {assumptions.length > 0 && (
          <section className="calc-block">
            <h4>הנחות שלך (מהשאלה, לא מהמסמכים)</h4>
            <ul className="calc-input-list">
              {assumptions.map((r) => {
                const quote = r.assumption?.quote ?? r.input?.quote ?? "";
                return (
                  <li key={r.id} data-testid="calc-input" data-kind="assumption" data-id={r.id}>
                    <div data-testid="calc-assumption">
                      <span className="calc-input-id">{r.id}</span> {r.label}: <bdi>{r.shown}</bdi> — לפי מה שכתבת
                      {r.assumption && !r.assumption.current ? " בהודעה קודמת" : ""}
                      {quote && (
                        <>
                          : «<bdi data-testid="calc-assumption-quote">{quote}</bdi>»
                        </>
                      )}{" "}
                      <MessageLink
                        assumption={{ message_id: r.assumption?.message_id, current: r.assumption?.current ?? false, quote }}
                        origin={origin}
                        onShowMessage={onShowMessage}
                        testId="calc-assumption-goto"
                      />
                    </div>
                  </li>
                );
              })}
            </ul>
          </section>
        )}

        {chained.length > 0 && (
          <section className="calc-block">
            <h4>חישובים קודמים שהחישוב נשען עליהם</h4>
            <ul className="calc-input-list">
              {chained.map((r) => (
                <li key={r.id} data-testid="calc-input" data-kind="computation" data-id={r.id}>
                  {r.computation ? (
                    <button
                      type="button"
                      className="calc-input-open"
                      data-testid="calc-nested"
                      onClick={() => onOpenCalculation(r.id)}
                      aria-label={`פירוט החישוב ${r.label}`}
                    >
                      <span className="calc-input-id">{r.id}</span> {r.label}: <bdi>{r.shown}</bdi>
                    </button>
                  ) : (
                    <span>
                      <span className="calc-input-id">{r.id}</span> {r.label}: <bdi>{r.shown}</bdi>
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </aside>
  );
}

// --- an assumption (A#) ---------------------------------------------------------------------------------------------

interface AssumptionViewProps {
  origin: AnswerOrigin;
  id: string;
  hidden?: boolean;
  back: string | null;
  onClose: () => void;
  onOpenCalculation: (id: string) => void;
  onShowMessage: (el: HTMLElement) => void;
}

/** A number the user gave: the user's own words, the message they come from, and the calculations that use it. */
export function AssumptionView({ origin, id, hidden, back, onClose, onOpenCalculation, onShowMessage }: AssumptionViewProps) {
  const closeRef = useInitialFocus(hidden);
  const a = origin.answer.assumptions?.find((x) => x.id === id);
  if (!a) return null;
  const users = origin.answer.computations.filter((c) =>
    c.inputs.some((x) => (typeof x === "string" ? x : x.id) === id),
  );
  return (
    <aside className="source-panel calc-view" aria-label="פירוט ההנחה" data-testid="assumption-view" data-id={a.id} hidden={hidden}>
      <PanelHeader
        title={a.label}
        subtitle={`הנחה ${a.id}`}
        label="סגירת פירוט ההנחה"
        back={back}
        closeRef={closeRef}
        onClose={onClose}
      />
      <div className="source-body calc-body">
        <section className="calc-block" data-testid="calc-assumption">
          <div className="calc-headline">
            <span className="calc-value">
              <bdi>{assumptionText(a)}</bdi>
            </span>{" "}
            <span className="badge badge-warn">הנחה שלך</span>
          </div>
          <p className="calc-statement">מספר שנתת בשאלה — לא נתון מהמסמכים.</p>
          <blockquote className="calc-quote" dir="auto" data-testid="assumption-quote">
            «<bdi>{a.quote}</bdi>»
          </blockquote>
          <p>
            {a.current ? "מתוך השאלה שעליה ענתה התשובה" : "מתוך הודעה קודמת שלך בשיחה"}{" "}
            <MessageLink assumption={a} origin={origin} onShowMessage={onShowMessage} testId="assumption-goto" />
          </p>
        </section>
        {users.length > 0 && (
          <section className="calc-block">
            <h4>חישובים שמשתמשים בהנחה</h4>
            <ul className="calc-input-list">
              {users.map((c) => (
                <li key={c.id}>
                  <button
                    type="button"
                    className="calc-input-open"
                    data-testid="calc-nested"
                    onClick={() => onOpenCalculation(c.id)}
                    aria-label={`פירוט החישוב ${c.label ?? c.id}`}
                  >
                    <span className="calc-input-id">{c.id}</span> {c.label ?? c.formula ?? c.operation}:{" "}
                    <bdi>{resultText(c)}</bdi>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </aside>
  );
}
