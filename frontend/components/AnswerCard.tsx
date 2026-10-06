"use client";

import { useState } from "react";
import { safeApiUrl } from "@/lib/api";
import {
  abstentionHeading,
  ANSWER_MODE_DESCRIPTION,
  ANSWER_MODE_LABEL,
  CLAIM_KIND_BADGE,
  CLAIM_KIND_LABEL,
  factCoverageLine,
  formatDecimal,
  formatPricePerSqm,
  formatQuantity,
  OPERATION_LABEL,
  PROVIDER_LABEL,
  PROVIDER_MODE_BADGE,
} from "@/lib/format";
import type {
  Answer,
  AttributeNumeric,
  Claim,
  Clarification,
  Condition,
  NumericResult,
  Preliminary,
  Source,
} from "@/lib/types";
import { B } from "./ui";

const SOURCES_COLLAPSED = 5;
// The server also states dropped claims as a limitation; the card shows them once, next to the claims.
const DROPPED_LIMITATION = /הושמט(ה|ו) מהתשובה/;

/**
 * Answer text is rendered as plain text. Only [E#] markers that match a server-provided source become links;
 * no markdown or HTML is ever interpreted.
 */
export function AnswerText({ text, sources }: { text: string; sources: Source[] }) {
  const byId = new Map(sources.map((s) => [s.evidence_id, s]));
  const parts = text.split(/(\[E\d+\])/g);
  return (
    <p className="answer-text">
      {parts.map((part, i) => {
        const m = /^\[(E\d+)\]$/.exec(part);
        if (m) {
          const src = byId.get(m[1]);
          const href = safeApiUrl(src?.url);
          if (src && href) {
            return (
              <a key={i} href={href} target="_blank" rel="noopener noreferrer" title={src.title}>
                <bdi>[{m[1]}]</bdi>
              </a>
            );
          }
        }
        return <span key={i}>{part}</span>;
      })}
    </p>
  );
}

function sourceLocation(s: Source): React.ReactNode {
  const bits: React.ReactNode[] = [];
  if (s.page_list && s.page_list.length > 0) {
    bits.push(
      <>
        {s.page_list.length > 1 ? "עמודים " : "עמוד "}
        <B>{s.page_list.join(", ")}</B>
      </>,
    );
  }
  if (s.section) bits.push(<>סעיף: {s.section}</>);
  if (s.row !== null && s.row !== undefined) {
    bits.push(
      <>
        שורה <B>{s.row}</B>
      </>,
    );
  }
  return bits.map((b, i) => (
    <span key={i}>
      {i > 0 && " · "}
      {b}
    </span>
  ));
}

export function SourcesList({ sources }: { sources: Source[] }) {
  const [expanded, setExpanded] = useState(false);
  if (sources.length === 0) return null;
  const shown = expanded ? sources : sources.slice(0, SOURCES_COLLAPSED);
  return (
    <section aria-label="מקורות">
      <h3>מקורות</h3>
      <ul className="sources-list">
        {shown.map((s) => {
          const href = safeApiUrl(s.url);
          return (
            <li key={s.evidence_id}>
              <div className="row">
                <span className="badge">
                  <bdi>{s.evidence_id}</bdi>
                </span>
                {s.label && <span className="badge badge-info">{s.label}</span>}
                {href ? (
                  <a href={href} target="_blank" rel="noopener noreferrer" style={{ overflowWrap: "anywhere" }}>
                    {s.title}
                  </a>
                ) : (
                  <span style={{ overflowWrap: "anywhere" }}>{s.title}</span>
                )}
                <span className="muted small">{sourceLocation(s)}</span>
                {s.tier && (
                  <span className={`badge ${s.tier === "verified" ? "badge-ok" : "badge-warn"}`}>
                    {s.tier === "verified" ? "ערך שאומת" : "ערך ראשוני"}
                  </span>
                )}
              </div>
              {s.snippet && <div className="snippet">{s.snippet}</div>}
            </li>
          );
        })}
      </ul>
      {sources.length > SOURCES_COLLAPSED && (
        <button type="button" className="btn-link" onClick={() => setExpanded((v) => !v)} aria-expanded={expanded}>
          {expanded ? "הצג פחות מקורות" : (
            <>
              הצג את כל <B>{sources.length}</B> המקורות
            </>
          )}
        </button>
      )}
    </section>
  );
}

function ConditionsInline({ conditions }: { conditions: Condition[] }) {
  return (
    <>
      {conditions.map((c, i) => (
        <span key={`${c.label}-${i}`}>
          {i > 0 && " · "}
          {c.label}: <B>{c.value}</B>
        </span>
      ))}
    </>
  );
}

function isPriceNumeric(n: NumericResult | AttributeNumeric): n is NumericResult {
  return "mean_price_per_sqm" in n;
}

function NumericFigures({ numeric }: { numeric: NumericResult }) {
  const cur = numeric.currency || "ILS";
  return (
    <>
      <div className="figures">
        <div className="figure">
          <div className="figure-label">ממוצע מחירי המ״ר</div>
          <div className="figure-value">
            <B>{formatPricePerSqm(numeric.mean_price_per_sqm, cur)}</B>
          </div>
        </div>
        <div className="figure">
          <div className="figure-label">מחיר משוקלל = סך מחירים חלקי סך שטחים</div>
          <div className="figure-value">
            <B>{formatPricePerSqm(numeric.weighted_price_per_sqm, cur)}</B>
          </div>
        </div>
      </div>
      <div>
        מבוסס על <B>{formatDecimal(numeric.record_count, { whole: true })}</B> רשומות ייחודיות
      </div>
      {numeric.conditions.length > 0 && (
        <div>
          <strong>תנאים: </strong>
          <ConditionsInline conditions={numeric.conditions} />
        </div>
      )}
      <div>
        <strong>חציון: </strong>
        <B>{formatPricePerSqm(numeric.median_price_per_sqm, cur)}</B>
        {" · "}
        <strong>טווח: </strong>
        <B>{formatPricePerSqm(numeric.min_price_per_sqm, cur)}</B>
        {" עד "}
        <B>{formatPricePerSqm(numeric.max_price_per_sqm, cur)}</B>
      </div>
    </>
  );
}

function figureText(op: string, value: string | null, unit: string | null, n: AttributeNumeric | null): string {
  if (op === "count") return formatDecimal(value, { whole: true });
  if (op === "range" && n?.minimum != null && n.maximum != null) {
    return `${formatQuantity(n.minimum, unit)} – ${formatQuantity(n.maximum, unit)}`;
  }
  if (op === "values") return (n?.values ?? []).map((v) => formatQuantity(v, unit)).join(", ") || "—";
  return formatQuantity(value, unit);
}

function ValuesLine({ values, unit }: { values: string[] | null | undefined; unit: string | null }) {
  if (!values || values.length === 0) return null;
  return (
    <div className="small">
      הערכים:{" "}
      {values.map((v, i) => (
        <span key={i}>
          {i > 0 && ", "}
          <B>{formatQuantity(v, unit)}</B>
        </span>
      ))}
    </div>
  );
}

/** A computed attribute other than price per sqm. For an extracted attribute the figure covers reviewed values. */
function AttributeFigure({ numeric, extracted }: { numeric: AttributeNumeric; extracted: boolean }) {
  const op = numeric.operation;
  const label = `${OPERATION_LABEL[op] ?? op} ${numeric.attribute}`;
  const hasValue = numeric.record_count > 0;
  return (
    <>
      <div className="figures">
        <div className="figure">
          <div className="figure-label">
            {label}
            {extracted && " — לפי ערכים שאומתו"}
          </div>
          <div className="figure-value">
            {hasValue ? <B>{figureText(op, numeric.value, numeric.unit, numeric)}</B> : "—"}
          </div>
        </div>
      </div>
      <div>
        {hasValue ? (
          <>
            מבוסס על <B>{formatDecimal(numeric.record_count, { whole: true })}</B> תצפיות
          </>
        ) : (
          "אין עדיין ערכים שאומתו בידי אדם."
        )}
      </div>
      {op !== "values" && <ValuesLine values={numeric.values} unit={numeric.unit} />}
    </>
  );
}

/** Values the server validated but no person reviewed: a separate, labeled block directly below the figure. */
function PreliminaryBlock({ preliminary, numeric }: { preliminary: Preliminary; numeric: AttributeNumeric | null }) {
  const op = numeric?.operation ?? "mean";
  const unit = numeric?.unit ?? null;
  return (
    <section className="alert alert-warn stack" style={{ gap: 4 }} aria-label="נתון ראשוני">
      <strong>נתון ראשוני — כולל ערכים שחולצו אוטומטית וטרם נבדקו בידי אדם</strong>
      <div>
        {numeric ? `${OPERATION_LABEL[op] ?? op} ${numeric.attribute}: ` : ""}
        <B>{op === "values" ? formatQuantity(preliminary.value, unit) : figureText(op, preliminary.value, unit, null)}</B>
      </div>
      <div className="small">
        מבוסס על <B>{formatDecimal(preliminary.record_count, { whole: true })}</B> תצפיות
      </div>
      <ValuesLine values={preliminary.values} unit={unit} />
    </section>
  );
}

/** Inline [E#] links for a claim's own evidence (only ids that match a server-provided source become links). */
function Cites({ ids, sources }: { ids: string[]; sources: Source[] }) {
  if (ids.length === 0) return null;
  const byId = new Map(sources.map((s) => [s.evidence_id, s]));
  return (
    <span>
      {ids.map((id) => {
        const src = byId.get(id);
        const href = safeApiUrl(src?.url);
        return (
          <span key={id}>
            {" "}
            {src && href ? (
              <a href={href} target="_blank" rel="noopener noreferrer" title={src.title}>
                <bdi>[{id}]</bdi>
              </a>
            ) : (
              <bdi>[{id}]</bdi>
            )}
          </span>
        );
      })}
    </span>
  );
}

function ClaimsList({ claims, dropped, sources }: { claims: Claim[]; dropped: number; sources: Source[] }) {
  if (claims.length === 0 && dropped === 0) return null;
  return (
    <section aria-label="טענות" className="stack" style={{ gap: 8 }}>
      {claims.length > 0 && (
        <ul className="sources-list">
          {claims.map((c, i) => (
            <li key={i} className="row" style={{ alignItems: "baseline" }}>
              <span className={`badge ${CLAIM_KIND_BADGE[c.kind] ?? ""}`}>{CLAIM_KIND_LABEL[c.kind] ?? c.kind}</span>
              <span className="answer-text" style={{ flex: "1 1 12rem" }}>
                {c.text}
              </span>
              <Cites ids={c.evidence_ids} sources={sources} />
            </li>
          ))}
        </ul>
      )}
      {dropped > 0 && (
        <p className="small muted" role="note" style={{ margin: 0 }}>
          {dropped === 1 ? (
            "טענה אחת הושמטה מהתשובה כי לא נמצאה לה תמיכה במקורות."
          ) : (
            <>
              <B>{dropped}</B> טענות הושמטו מהתשובה כי לא נמצאה להן תמיכה במקורות.
            </>
          )}
        </p>
      )}
    </section>
  );
}

/** Conflicts and limitations are always visible. */
function ConflictsAndLimitations({ answer }: { answer: Answer }) {
  const items: React.ReactNode[] = [];
  const n = answer.numeric && isPriceNumeric(answer.numeric) ? answer.numeric : null;
  if (n && n.uncertain_duplicates > 0) {
    items.push(
      <>
        בין הרשומות <B>{n.uncertain_duplicates}</B> כפילויות אפשריות שלא אומתו (נספרו כרשומות נפרדות).
      </>,
    );
  }
  if (n && n.conflicts > 0) {
    items.push(
      <>
        ב־<B>{n.conflicts}</B> רשומות יש סתירה בין המחיר למ״ר המחושב לבין המחיר המצוין במסמך.
      </>,
    );
  }
  const cmp = answer.compare;
  if (cmp?.incomplete && cmp.missing_sides.length > 0) {
    items.push(<>ההשוואה אינה שלמה: אין ראיות עבור {cmp.missing_sides.join(", ")}.</>);
  }
  const dropped = answer.dropped_claims ?? 0;
  for (const l of answer.limitations ?? []) {
    if (dropped > 0 && DROPPED_LIMITATION.test(l)) continue;
    items.push(l);
  }
  const conflicts = cmp?.conflicts ?? [];
  const covText = answer.coverage && !answer.coverage.facts ? answer.coverage.text : null;
  if (items.length === 0 && conflicts.length === 0 && !covText) return null;
  return (
    <section aria-label="סתירות ומגבלות" className="stack" style={{ gap: 8 }}>
      {conflicts.length > 0 && (
        <div>
          <h3>הבדלים בין המקורות</h3>
          <ul>
            {conflicts.map((c, i) => (
              <li key={i}>
                <strong>{c.datum}: </strong>
                {c.sides.map((s, j) => (
                  <span key={j}>
                    {j > 0 && "; "}
                    {s.label} — {s.text}
                    <Cites ids={s.evidence_ids} sources={answer.sources ?? []} />
                  </span>
                ))}
              </li>
            ))}
          </ul>
        </div>
      )}
      {items.length > 0 && (
        <div>
          <h3>מגבלות</h3>
          <ul>
            {items.map((it, i) => (
              <li key={i}>{it}</li>
            ))}
          </ul>
        </div>
      )}
      {covText && <p className="answer-text small muted">{covText}</p>}
    </section>
  );
}

/** Method, n, conditions and coverage counts: collapsed by default. */
function Details({ answer }: { answer: Answer }) {
  const numeric = answer.numeric ?? null;
  const priceShown = !!numeric && isPriceNumeric(numeric);
  const conditions = priceShown ? [] : (answer.conditions ?? numeric?.conditions ?? []);
  const cov = answer.coverage;
  const docs: string[] = [];
  if (cov?.docs_pending) docs.push(`${cov.docs_pending} מסמכים בעיבוד`);
  if (cov?.docs_failed) docs.push(`${cov.docs_failed} מסמכים שנכשלו`);
  if (cov?.docs_needs_review) docs.push(`${cov.docs_needs_review} מסמכים הדורשים בדיקה`);
  if (cov?.records_awaiting_verification) docs.push(`${cov.records_awaiting_verification} רשומות ממתינות לאימות`);
  const rows: [string, React.ReactNode][] = [];
  if (answer.method) rows.push(["שיטה", answer.method]);
  if (numeric) rows.push(["מספר תצפיות", <B key="n">{formatDecimal(numeric.record_count, { whole: true })}</B>]);
  if (conditions.length > 0) rows.push(["תנאים", <ConditionsInline key="c" conditions={conditions} />]);
  if (cov?.facts) rows.push(["כיסוי", factCoverageLine(cov.facts)]);
  if (docs.length > 0) rows.push(["מצב המאגר", docs.join(" · ")]);
  if (answer.cached) rows.push(["מקור", "תשובה שמורה; המקורות והכיסוי נבדקו מחדש"]);
  if (rows.length === 0) return null;
  return (
    <details className="small">
      <summary>פרטי השיטה והכיסוי</summary>
      <dl className="kv" style={{ marginBlockStart: 8 }}>
        {rows.map(([k, v]) => (
          <div key={k} style={{ display: "contents" }}>
            <dt>{k}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

/** "active": the open clarification with its buttons; "below": still open, answered from the pinned block. */
export type ClarificationState = "active" | "below" | "done";

export function ClarificationBlock({
  clarification,
  state,
  disabled,
  onChoose,
}: {
  clarification: Clarification;
  state: ClarificationState;
  disabled: boolean;
  onChoose: (value: string, label: string) => void;
}) {
  return (
    <div className="stack">
      <p className="answer-text">
        <strong>{clarification.question}</strong>
      </p>
      {state === "active" && (
        <div className="row" role="group" aria-label="אפשרויות הבהרה">
          {clarification.options.map((o) => (
            <button
              key={o.value}
              type="button"
              className="btn btn-primary"
              style={{ whiteSpace: "normal" }}
              disabled={disabled}
              onClick={() => onChoose(o.value, o.label)}
            >
              {o.label}
            </button>
          ))}
        </div>
      )}
      {state === "below" && <p className="small muted" style={{ margin: 0 }}>שאלת ההבהרה עדיין פתוחה; אפשר לענות עליה מעל תיבת השאלה.</p>}
      {state === "done" && <p className="small muted" style={{ margin: 0 }}>שאלת ההבהרה כבר טופלה.</p>}
    </div>
  );
}

function PartialNotice({ answer, busy, onRefresh }: { answer: Answer; busy: boolean; onRefresh?: () => void }) {
  const f = answer.coverage?.facts;
  const pending = answer.pending_extraction ?? 0;
  return (
    <div className="alert alert-warn row" role="note" aria-label="תשובה חלקית">
      <span className="badge badge-warn">חלקי</span>
      <span>
        {f ? factCoverageLine(f) : "חלק מהצעדים לא הושלמו בזמן שהוקצב."}
        {!f && pending > 0 && <> · <B>{pending}</B> מסמכים טרם חולצו</>}
      </span>
      {onRefresh && (
        <button type="button" className="btn" disabled={busy} onClick={onRefresh}>
          רענון תשובה
        </button>
      )}
    </div>
  );
}

interface AnswerCardProps {
  answer: Answer;
  clarificationState?: ClarificationState;
  busy?: boolean;
  /** Disables the clarification buttons only (e.g. while the conversation's open clarification is unknown). */
  choicesDisabled?: boolean;
  onChoose?: (key: string, value: string, label: string) => void;
  /** Re-sends the question as a new turn and replaces this answer (offered for partial answers). */
  onRefresh?: () => void;
  ref?: React.Ref<HTMLElement>;
}

/**
 * Card order: mode badge → direct answer (verified figure, then the labeled preliminary figure) → conflicts and
 * limitations → labeled claims with one dropped-claims notice → sources → collapsed details.
 * When the claims are the whole answer (a content answer), they take the direct-answer place instead of being
 * repeated as text.
 */
export function AnswerCard({
  answer,
  clarificationState = "done",
  busy = false,
  choicesDisabled = false,
  onChoose,
  onRefresh,
  ref,
}: AnswerCardProps) {
  const clar = answer.kind === "clarification" ? answer.clarification : null;
  const abstain = answer.kind === "abstain";
  const numeric = !abstain && (answer.kind === "numeric" || answer.kind === "combined") ? (answer.numeric ?? null) : null;
  const attr = numeric && !isPriceNumeric(numeric) ? numeric : null;
  const extracted = !!answer.preliminary || (answer.sources ?? []).some((s) => !!s.tier);
  const claims = answer.claims ?? [];
  const dropped = answer.dropped_claims ?? 0;
  const claimsAreAnswer = !abstain && claims.length > 0 && answer.kind !== "combined";
  // A combined answer's text is the computed part, a blank line, then the rendered claims.
  const text = abstain || claims.length === 0 ? answer.text : answer.kind === "combined" ? answer.text.split("\n\n")[0] : "";
  const mode = answer.mode ?? (answer.demo ? "demo" : null);
  const sources = answer.sources ?? [];
  const cleared = answer.cleared ?? [];

  return (
    <article ref={ref} tabIndex={-1} className="answer-card" aria-label="תשובה">
      <div className="row">
        {mode && (
          <span className={`badge ${PROVIDER_MODE_BADGE[mode]}`} title={ANSWER_MODE_DESCRIPTION[mode]} data-mode={mode}>
            {ANSWER_MODE_LABEL[mode]}
          </span>
        )}
        {answer.demo && mode !== "demo" && (
          <span className="badge badge-demo" title={ANSWER_MODE_DESCRIPTION.demo}>
            דמו
          </span>
        )}
        {abstain && <span className="badge badge-warn">אין מספיק מידע</span>}
        <span className="spacer" />
        <span className="small muted">מקור התשובה: {PROVIDER_LABEL[answer.provider] ?? answer.provider}</span>
      </div>

      {answer.interpretation_note && (
        <p className="small muted" role="note" style={{ margin: 0 }}>
          {answer.interpretation_note}
        </p>
      )}
      {cleared.length > 0 && (
        <p className="small muted" role="note" style={{ margin: 0 }}>
          נוקו מההקשר: <ConditionsInline conditions={cleared} />
        </p>
      )}

      {clar ? (
        <ClarificationBlock
          clarification={clar}
          state={clarificationState}
          disabled={busy || choicesDisabled}
          onChoose={(value, label) => onChoose?.(clar.key, value, label)}
        />
      ) : (
        <>
          {answer.partial && <PartialNotice answer={answer} busy={busy} onRefresh={onRefresh} />}
          {abstain && <h3>{abstentionHeading(answer.abstention_kind)}</h3>}
          {numeric && isPriceNumeric(numeric) && <NumericFigures numeric={numeric} />}
          {attr && <AttributeFigure numeric={attr} extracted={extracted} />}
          {!abstain && answer.preliminary && <PreliminaryBlock preliminary={answer.preliminary} numeric={attr} />}
          {text && <AnswerText text={text} sources={sources} />}
          {claimsAreAnswer && <ClaimsList claims={claims} dropped={dropped} sources={sources} />}
          <ConflictsAndLimitations answer={answer} />
          {!claimsAreAnswer && !abstain && <ClaimsList claims={claims} dropped={dropped} sources={sources} />}
          <SourcesList sources={sources} />
          <Details answer={answer} />
        </>
      )}
    </article>
  );
}
