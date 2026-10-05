"use client";

import { useState } from "react";
import { safeApiUrl } from "@/lib/api";
import { formatDecimal, formatPricePerSqm, PROVIDER_LABEL } from "@/lib/format";
import type { Answer, Clarification, Coverage, NumericResult, Source } from "@/lib/types";
import { B } from "./ui";

const SOURCES_COLLAPSED = 5;

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
                {href ? (
                  <a href={href} target="_blank" rel="noopener noreferrer">
                    {s.title}
                  </a>
                ) : (
                  <span>{s.title}</span>
                )}
                <span className="muted small">{sourceLocation(s)}</span>
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
          {numeric.conditions.map((c, i) => (
            <span key={`${c.label}-${i}`}>
              {i > 0 && " · "}
              {c.label}: <B>{c.value}</B>
            </span>
          ))}
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

function Limitations({ answer }: { answer: Answer }) {
  const items: React.ReactNode[] = [];
  const n = answer.numeric;
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
  for (const l of answer.limitations ?? []) items.push(l);
  const cov: Coverage | null | undefined = answer.coverage;
  if (items.length === 0 && !cov) return null;
  return (
    <section aria-label="מגבלות וכיסוי">
      {items.length > 0 && (
        <>
          <h3>מגבלות</h3>
          <ul>
            {items.map((it, i) => (
              <li key={i}>{it}</li>
            ))}
          </ul>
        </>
      )}
      {cov && (
        <div className="small muted">
          {cov.text && <p className="answer-text">{cov.text}</p>}
          <div className="row">
            {cov.docs_pending > 0 && (
              <span>
                מסמכים בעיבוד: <B>{cov.docs_pending}</B>
              </span>
            )}
            {cov.docs_failed > 0 && (
              <span>
                מסמכים שנכשלו: <B>{cov.docs_failed}</B>
              </span>
            )}
            {cov.docs_needs_review > 0 && (
              <span>
                מסמכים הדורשים בדיקה: <B>{cov.docs_needs_review}</B>
              </span>
            )}
            {cov.records_awaiting_verification > 0 && (
              <span>
                רשומות מתאימות הממתינות לאימות: <B>{cov.records_awaiting_verification}</B>
              </span>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

export function ClarificationBlock({
  clarification,
  active,
  disabled,
  onChoose,
}: {
  clarification: Clarification;
  active: boolean;
  disabled: boolean;
  onChoose: (value: string, label: string) => void;
}) {
  return (
    <div className="stack">
      <p className="answer-text">
        <strong>{clarification.question}</strong>
      </p>
      {active ? (
        <div className="row" role="group" aria-label="אפשרויות הבהרה">
          {clarification.options.map((o) => (
            <button
              key={o.value}
              type="button"
              className="btn btn-primary"
              disabled={disabled}
              onClick={() => onChoose(o.value, o.label)}
            >
              {o.label}
            </button>
          ))}
        </div>
      ) : (
        <p className="small muted">שאלת ההבהרה כבר טופלה.</p>
      )}
    </div>
  );
}

interface AnswerCardProps {
  answer: Answer;
  clarificationActive?: boolean;
  busy?: boolean;
  onChoose?: (key: string, value: string, label: string) => void;
  ref?: React.Ref<HTMLElement>;
}

export function AnswerCard({ answer, clarificationActive = false, busy = false, onChoose, ref }: AnswerCardProps) {
  const hasNumeric = !!answer.numeric && (answer.kind === "numeric" || answer.kind === "combined");
  const clar = answer.kind === "clarification" ? answer.clarification : null;
  return (
    <article ref={ref} tabIndex={-1} className="answer-card" aria-label="תשובה">
      <div className="row">
        {answer.demo && (
          <span className="badge badge-demo" aria-label="תשובת דמו">
            דמו
          </span>
        )}
        {answer.kind === "abstain" && <span className="badge badge-warn">אין מספיק מידע</span>}
        <span className="spacer" />
        <span className="small muted">מקור התשובה: {PROVIDER_LABEL[answer.provider] ?? answer.provider}</span>
      </div>

      {clar ? (
        <ClarificationBlock
          clarification={clar}
          active={clarificationActive}
          disabled={busy}
          onChoose={(value, label) => onChoose?.(clar.key, value, label)}
        />
      ) : (
        <>
          {hasNumeric && answer.numeric && <NumericFigures numeric={answer.numeric} />}
          {answer.text && <AnswerText text={answer.text} sources={answer.sources ?? []} />}
          <Limitations answer={answer} />
          <SourcesList sources={answer.sources ?? []} />
        </>
      )}
    </article>
  );
}
