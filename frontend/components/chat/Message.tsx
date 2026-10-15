"use client";

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { B } from "@/components/ui";
import type {
  ChatAnswer,
  ChatAnswerTime,
  ChatBreakdownBlock,
  ChatAssumption,
  ChatComponent,
  ChatComputation,
  ChatComputationInput,
  ChatLedger,
  ChatMessage,
  ChatProgressStep,
  ChatRemoval,
  ChatRequirement,
  ChatSource,
  ChatTimeCoverage,
  ChatTimelineGroup,
  ChatTimelineMark,
  ChatTimelineRow,
  ChatUnitScope,
  ChatValue,
  ChatVerification,
  LedgerDocument,
  LedgerTable,
  ViewerNav,
  ViewerTarget,
} from "@/lib/chatTypes";
import {
  COMPONENT_KIND_LABEL,
  COMPONENT_STATUS_LABEL,
  CONTENT_ACTIONS,
  VALUE_STATUS,
  type ChangeAction,
  type ValueStatus,
  contentActionsText,
  failureKindText,
  formatIsoDate,
  gapReasonText,
  measurementValueStatus,
  rowChangeAction,
  unitLabel,
  valueStatusText,
} from "@/lib/format";
import { type CitationTarget, Markdown, citationOrder, plainAnswer } from "./Markdown";
import { assumptionText, citedTarget, standardChipTitle, valueStatusOf } from "./cited";
import { ActionLines, type CutFocus, type ExpandAll, RemovalsSection } from "./Changes";

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
  derived: "נגזר מהערכים שצוטטו",
};
const VAT: Record<string, string> = { included: "כולל מע״מ", excluded: "ללא מע״מ", unknown: "מע״מ לא צוין", not_applicable: "" };
const PERIOD: Record<string, string> = { month: "לחודש", year: "לשנה", one_time: "חד-פעמי", none: "", unknown: "תקופה לא צוינה" };
const COMPLETENESS_LEAD: Record<string, string> = {
  partial: "התשובה חלקית",
  missing: "התשובה אינה עונה על השאלה",
  undeterminable: "לא ניתן להכריע מהמסמכים",
};
const CORRECTNESS_LINE: Record<string, string> = {
  partial: "חלק מהטענות הוסרו או אומתו רק בחלקן מול המקורות (פירוט ב«מקורות ופרטים»).",
  unverified: "הטענות בתשובה לא אומתו מול המקורות; יש לבדוק במקור לפני שימוש.",
};
// the only reason the answer is not verified: a result shown with the server's conditional qualifier
const CONDITIONAL_LINE = "חלק מהתוצאות מחושבות מערכים שלא אומתו מול המקור, ומוצגות כמותנות (ההסתייגות כתובה ליד המספר).";

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
    if (s) title = standardChipTitle(s) ?? `${s.title}${s.unit ? ` (${unitLabel(s.unit)})` : ""} — ${s.location}`;
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
  /** Opens the source viewer at a place that is not one of the answer's citations (a removed claim's checked source,
   * as the diagnostics route returned it). Without it those places are listed but not opened. */
  onOpenSource?: (nav: ViewerNav) => void;
}

/** The lines the progress list shows: a tool's finished event (round 9: what it found) replaces its start event, so a
 * call is one line; the timestamps are kept in the data and not shown. */
export function progressLines(progress: ChatProgressStep[]): ChatProgressStep[] {
  const lines: ChatProgressStep[] = [];
  for (const p of progress) {
    if (p.step === "queued") continue;
    const prev = lines[lines.length - 1];
    if (p.done && prev && !prev.done && prev.step === p.step) lines[lines.length - 1] = p;
    else lines.push(p);
  }
  return lines;
}

export function AssistantMessage({ message, isLast, onCite, onRetry, onStop, onOpenSource }: AssistantProps) {
  const [copied, setCopied] = useState(false);
  const answer = message.answer;
  // a hidden answer keeps only its kind: it has no text or sources to cite
  const citations = useMemo(() => (answer && !answer.hidden ? citationTargets(answer) : new Map()), [answer]);
  const cite = useCallback((id: string) => answer && onCite(answer, id, message), [answer, onCite, message]);
  // a cut marker in the body opens its own entry in the details (round 8 U8); `tick` makes a second click count
  const [focus, setFocus] = useState<CutFocus | null>(null);
  const cutOrdinals = useMemo(
    () => new Set(cutRows(answer?.verification?.removals).map((r) => r.ordinal as number)),
    [answer],
  );
  const openCut = useCallback((ordinal: number) => setFocus((f) => ({ ordinal, tick: (f?.tick ?? 0) + 1 })), []);
  // the removal note's button opens the details with every removal expanded (round 13 U12, KTD14)
  const [expandAll, setExpandAll] = useState<ExpandAll | null>(null);
  // idempotent "open all": every press opens the details and expands every removal, never closes them
  const openAllRemovals = useCallback(
    () => setExpandAll((c) => ({ tick: (c?.tick ?? 0) + 1, open: true })),
    [],
  );
  // the button's aria-expanded follows the real <details> state, synced through its toggle event
  const [detailsOpen, setDetailsOpen] = useState(false);
  const removalsId = useId();
  const noteParts = useMemo(() => (answer ? removalNoteParts(answer) : null), [answer]);

  if (message.status === "running" || message.status === "cancelling") {
    const steps = progressLines(message.progress);
    const lastStep = steps.length ? steps[steps.length - 1] : null;
    const last = lastStep ? lastStep.label : "מעבד את הבקשה";
    return (
      <div className="msg msg-assistant" aria-live="polite">
        <div className="status-line">
          <span className="dot-pulse" aria-hidden="true" />
          <span>
            {message.status === "cancelling"
              ? "עוצר… (ממתין לסיום שלב שכבר נשלח למודל)"
              : lastStep?.done
                ? last
                : `${last}…`}
          </span>
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

  const timeNote = timePreferenceNote(message, answer);
  const renderMarkdown = (markdown: string) => (
    <Markdown markdown={markdown} citations={citations} onCite={cite} cuts={cutOrdinals} onCut={openCut} />
  );

  const copy = async () => {
    try {
      const plain = plainAnswer(answer.markdown);
      await navigator.clipboard.writeText([plain, ...copiedExtras(answer.time)].join("\n\n"));
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
        {noteParts ? (
          <>
            {noteParts.head.trim() && renderMarkdown(noteParts.head)}
            <div className="removal-note" data-testid="removal-note">
              {renderMarkdown(noteParts.note)}
              <button
                type="button"
                className="source-link"
                data-testid="removal-note-action"
                aria-expanded={detailsOpen}
                aria-controls={removalsId}
                onClick={openAllRemovals}
              >
                פירוט ההסרות
              </button>
            </div>
            {noteParts.tail.trim() && renderMarkdown(noteParts.tail)}
          </>
        ) : (
          renderMarkdown(answer.markdown)
        )}
      </div>
      <AnswerQuality answer={answer} />
      <Timeline groups={answer.time?.timeline} onCite={cite} />
      <Breakdown blocks={answer.time?.breakdown} onCite={cite} />
      <CoverageNote coverage={answer.time?.coverage} />
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
        {timeNote && (
          <span className="note" data-testid="time-note">
            {timeNote}
          </span>
        )}
      </div>
      <AnswerDetails
        answer={answer}
        messageId={message.id}
        citations={citations}
        onCite={cite}
        focus={focus}
        expandAll={expandAll}
        onDetailsToggle={setDetailsOpen}
        removalsId={removalsId}
        onOpenSource={onOpenSource}
      />
    </div>
  );
}

/** The saved text split around the server's removal note (round 13 U12, KTD14): the text before it, the note and the
 * text after it, so the note carries its action. Only when the answer has removal rows to open and the registry's span
 * (code-point offsets, as Python wrote them) holds a blockquote; otherwise the text is shown whole, as before. */
export function removalNoteParts(answer: ChatAnswer): { head: string; note: string; tail: string } | null {
  if (!answer.verification?.removals?.length) return null;
  const spans = (answer.server_spans ?? []).filter((s) => s.kind === "removal_note");
  if (spans.length !== 1) return null;
  const { start, end } = spans[0];
  const chars = Array.from(answer.markdown);
  if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end <= start || end > chars.length) return null;
  const note = chars.slice(start, end).join("");
  if (!note.trimStart().startsWith(">")) return null;
  return { head: chars.slice(0, start).join(""), note, tail: chars.slice(end).join("") };
}

/** The time preference an answer was produced with, worded as the setting and not as a claim that ordering happened
 * (round 11 U10, R15). The state is the one recorded on the message, never the conversation's current switch. An
 * explicit period in the question overrides the switch; a message from before round 11 shows nothing. */
export function timePreferenceNote(message: ChatMessage, answer: ChatAnswer): string | null {
  if (answer.time?.mode === "explicit") return "לפי הזמן שביקשת";
  const pref = message.time_preference;
  if (!pref) return null;
  return pref.prefer_recent ? "העדפת זמן: עדכני תחילה" : "העדפת זמן: ללא";
}

const DATE_KIND_LABEL: Record<string, string> = {
  valuation: "מועד קובע",
  transaction: "מועד עסקה",
  stated: "מועד שצוין",
  range: "תקופה",
};
const TIMELINE_MARK: Record<ChatTimelineMark, { label: string; cls: string }> = {
  forecast: { label: "תחזית", cls: "badge-info" },
  conflict: { label: "סתירה בין מקורות", cls: "badge-warn" },
  undecided: { label: "השוואה לא הוכרעה", cls: "badge-info" },
  outside_requested: { label: "מחוץ לזמן שביקשת", cls: "" },
  uncertain: { label: "קריאה לא ודאית", cls: "badge-warn" },
  unknown_date: { label: "מועד לא ידוע", cls: "" },
};

/** One ISO date at the precision the source gave: a month or a year is never shown as a day. */
function timelinePoint(iso: string, precision: ChatTimelineRow["precision"]): string {
  const m = /^(\d{4})(?:-(\d{2}))?(?:-(\d{2}))?/.exec(iso);
  if (!m) return iso;
  const p = precision ?? (m[3] ? "day" : m[2] ? "month" : "year");
  if (p === "year" || !m[2]) return m[1];
  if (p === "month" || !m[3]) return `${m[2]}/${m[1]}`;
  return formatIsoDate(iso);
}

/** A timeline row's date: the start, and for a period (a table caption's range) its end in the same precision, joined by an
 * en dash like the server's plain-text copy (`shown_period`). */
function timelineDate(row: Pick<ChatTimelineRow, "date" | "precision" | "end">): string {
  if (!row.date) return "ללא תאריך";
  const first = timelinePoint(row.date, row.precision);
  return row.end ? `${first}–${timelinePoint(row.end, row.precision)}` : first;
}

/** The value as the row shows it: the server's `unit` is display text ("₪", "מ״ר"), appended only when the value text does
 * not already carry it, as the plain-text copy does. */
function timelineValue(row: Pick<ChatTimelineRow, "value_text" | "unit">): string {
  return row.unit && !row.value_text.includes(row.unit) ? `${row.value_text} ${row.unit}` : row.value_text;
}

/** What the copy button adds after the answer's text: the server's plain-text copy of the timeline (which ends with the
 * coverage note), and the note itself when a payload carries the note without it (round 12 U8). */
function copiedExtras(time: ChatAnswerTime | null | undefined): string[] {
  const text = time?.text || "";
  const note = time?.coverage?.note || "";
  return [text, note && !text.includes(note) ? note : ""].filter(Boolean);
}

/** The server-written coverage note (round 12 U8, R12): what the answer's time data checked and did not. Its own element
 * after the answer body and the timeline slot, whether or not the timeline has rows. Plain text, not interactive (no tab
 * stop); nothing when the answer has no coverage record (an older message or a turn with no time mode), and never a
 * "complete" label of its own: the words are the server's. */
function CoverageNote({ coverage }: { coverage: ChatTimeCoverage | null | undefined }) {
  const note = coverage?.note;
  if (!note) return null;
  return (
    <div className="coverage-note" role="note" data-testid="coverage-note" aria-label="כיסוי החיפוש" dir="rtl">
      {note}
    </div>
  );
}

/** The marks that name the values they are about: the key of the row's list, and the accessible text of its chip. */
const MARK_WITH: Partial<Record<ChatTimelineMark, { key: "undecided_with" | "conflict_with"; label: string }>> = {
  undecided: { key: "undecided_with", label: TIMELINE_MARK.undecided.label },
  conflict: { key: "conflict_with", label: "סתירה" },
};

/** A row's marks as text badges. The undecided and the conflict one are followed by a citation chip for each value they
 * are about (round 13 U6, R6), which opens the viewer like any other source chip; the chip reads to assistive technology
 * as "השוואה לא הוכרעה מול <id>" or "סתירה מול <id>" (the badge beside it is then not read twice). */
function MarkBadges({
  marks,
  counterparts,
  onCite,
}: {
  marks: ChatTimelineMark[];
  counterparts: { undecided_with?: string[]; conflict_with?: string[] };
  onCite: (id: string) => void;
}) {
  return (
    <>
      {marks.map((k) => {
        const named = MARK_WITH[k];
        const sids = (named && counterparts[named.key]) || [];
        if (!named || sids.length === 0) {
          return (
            <span key={k} className={`badge ${TIMELINE_MARK[k]?.cls ?? ""}`} data-mark={k}>
              {TIMELINE_MARK[k]?.label ?? k}
            </span>
          );
        }
        return (
          <span key={k} className="tl-mark-with" data-mark={k}>
            <span className={`badge ${TIMELINE_MARK[k].cls}`} aria-hidden="true">
              {TIMELINE_MARK[k].label}
            </span>
            {sids.map((sid) => (
              <button
                key={sid}
                type="button"
                className="cite tl-cite"
                data-testid={`${k}-with`}
                onClick={() => onCite(sid)}
                aria-label={`${named.label} מול ${sid}`}
              >
                {sid}
              </button>
            ))}
          </span>
        );
      })}
    </>
  );
}

/** The three cells the timeline and the components table share. The `td` (its class) stays with each table. */
function SourceCell({ row, onCite }: { row: Pick<ChatTimelineRow, "sid" | "source">; onCite: (id: string) => void }) {
  return (
    <>
      <button
        type="button"
        className="cite tl-cite"
        onClick={() => onCite(row.sid)}
        title={[row.source.title, row.source.location].filter(Boolean).join(" — ")}
        aria-label={`מקור ${row.sid}: ${row.source.title}`}
      >
        {row.sid}
      </button>
      <span className="tl-title">{row.source.title}</span>
    </>
  );
}

function ValueCell({ row }: { row: Pick<ChatTimelineRow, "value_text" | "unit"> }) {
  return <B>{timelineValue(row)}</B>;
}

function MarksCell({
  row,
  onCite,
}: {
  row: Pick<ChatTimelineRow, "marks" | "undecided_with" | "conflict_with">;
  onCite: (id: string) => void;
}) {
  return <MarkBadges marks={row.marks} counterparts={row} onCite={onCite} />;
}

/** The server-built timeline of comparable dated values (round 11 U9, U10): groups and rows in the payload's order,
 * never re-sorted here. A table at desktop width, stacked rows on a phone (CSS only, so the order and the roles stay
 * the same); a mark is a text badge, never colour alone; the source chip opens the viewer like an answer citation. */
function Timeline({ groups, onCite }: { groups: ChatTimelineGroup[] | undefined; onCite: (id: string) => void }) {
  const shown = (groups ?? []).filter((g) => g.rows.length > 0);
  if (shown.length === 0) return null;
  return (
    <section className="timeline" data-testid="timeline" aria-label="ציר זמן">
      <h4 className="timeline-heading">ציר זמן</h4>
      {shown.map((g, gi) => (
        <div key={`${gi}-${g.label}`} className="timeline-group" data-testid="timeline-group">
          <div className="timeline-label">{g.label}</div>
          <table className="timeline-table" role="table" aria-label={`ציר זמן: ${g.label}`}>
            <thead role="rowgroup">
              <tr role="row">
                <th role="columnheader" scope="col">תאריך</th>
                <th role="columnheader" scope="col">ערך</th>
                <th role="columnheader" scope="col">מקור</th>
                <th role="columnheader" scope="col">סימון</th>
              </tr>
            </thead>
            <tbody role="rowgroup">
              {g.rows.map((r, ri) => (
                <tr key={`${ri}-${r.sid}`} role="row" data-testid="timeline-row">
                  <td role="cell" className="tl-date">
                    <B>{timelineDate(r)}</B>
                    {r.date_kind && DATE_KIND_LABEL[r.date_kind] && <span className="tl-kind">{DATE_KIND_LABEL[r.date_kind]}</span>}
                  </td>
                  <td role="cell" className="tl-value">
                    <ValueCell row={r} />
                  </td>
                  <td role="cell" className="tl-source">
                    <SourceCell row={r} onCite={onCite} />
                  </td>
                  <td role="cell" className="tl-marks">
                    <MarksCell row={r} onCite={onCite} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </section>
  );
}

/** The components table (round 13 U6, KTD7, R8-R10): the parallel components of one appraisal unit at one date, side by side
 * instead of as competing values. Blocks and rows in the payload's order, never re-sorted here; each row shows the facet in
 * the source's own words (long ones wrap), the value, the source chip and the marks. Built like the timeline (same table,
 * stacked rows on a phone); an older message has no `breakdown` and renders nothing. */
function Breakdown({ blocks, onCite }: { blocks: ChatBreakdownBlock[] | undefined; onCite: (id: string) => void }) {
  const shown = (blocks ?? []).filter((b) => b.rows.length > 0);
  if (shown.length === 0) return null;
  return (
    <section className="timeline breakdown" data-testid="breakdown" aria-label="רכיבי השומה">
      <h4 className="timeline-heading">רכיבי השומה</h4>
      {shown.map((b, bi) => (
        <div key={`${bi}-${b.label}`} className="timeline-group" data-testid="breakdown-block">
          <div className="timeline-label">
            {b.label}
            <span className="tl-kind">{timelineDate(b.as_of)}</span>
            {b.as_of.date_kind && DATE_KIND_LABEL[b.as_of.date_kind] && (
              <span className="tl-kind">{DATE_KIND_LABEL[b.as_of.date_kind]}</span>
            )}
          </div>
          <table className="timeline-table breakdown-table" role="table" aria-label={`רכיבי השומה: ${b.label}`}>
            <thead role="rowgroup">
              <tr role="row">
                <th role="columnheader" scope="col">רכיב או שיקול (כלשון המקור)</th>
                <th role="columnheader" scope="col">ערך</th>
                <th role="columnheader" scope="col">מקור</th>
                <th role="columnheader" scope="col">סימון</th>
              </tr>
            </thead>
            <tbody role="rowgroup">
              {b.rows.map((r, ri) => (
                <tr key={`${ri}-${r.sid}`} role="row" data-testid="breakdown-row">
                  <td role="cell" className="tl-facet" data-testid="breakdown-facet">
                    {r.facet}
                  </td>
                  <td role="cell" className="tl-value">
                    <ValueCell row={r} />
                  </td>
                  <td role="cell" className="tl-source">
                    <SourceCell row={r} onCite={onCite} />
                  </td>
                  <td role="cell" className="tl-marks">
                    <MarksCell row={r} onCite={onCite} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </section>
  );
}

/** The summary rows of cuts that carry the ordinal of their marker in the text. */
function cutRows(rows: ChatRemoval[] | undefined): ChatRemoval[] {
  return (rows ?? []).filter((r) => r.action === "cut" && typeof r.ordinal === "number");
}

/** An answer stored with round 7's component outcomes: its markdown ends with the server's gap paragraph (KTD4). */
function hasComponents(answer: ChatAnswer): boolean {
  return answer.components !== undefined || answer.gaps !== undefined;
}

/** How many claims each content-affecting action touched (removed, cut, rewritten, withheld): the payload's counts; for
 * an answer stored without them, its rows. Qualified and cited claims change no content and are not counted. */
function contentCounts(v: ChatVerification | null | undefined): Partial<Record<ChangeAction, number>> {
  if (!v) return {};
  if (v.actions) {
    const actions = v.actions;
    return Object.fromEntries(CONTENT_ACTIONS.map((a) => [a, actions[a]]));
  }
  const out: Partial<Record<ChangeAction, number>> = {};
  for (const r of v.removals ?? []) {
    const a = rowChangeAction(r);
    out[a] = (out[a] ?? 0) + 1;
  }
  return out;
}

function sum(counts: Partial<Record<ChangeAction, number>>): number {
  return Object.values(counts).reduce((a, b) => a + (b ?? 0), 0);
}

/** The most frequent failure kind of the removals (the first one on a tie). */
function dominantKind(removals: ChatRemoval[]): string {
  const counts = new Map<string, number>();
  for (const r of removals) counts.set(r.failure_kind, (counts.get(r.failure_kind) ?? 0) + 1);
  let best = removals[0]?.failure_kind ?? "";
  for (const [kind, n] of counts) if (n > (counts.get(best) ?? 0)) best = kind;
  return best;
}

/** The detail a clarification waits for: the components needing it, else what the answer says is missing. */
function pendingDetail(answer: ChatAnswer): string {
  const waiting = (answer.components ?? []).filter((c) => c.status === "needs_clarification").map((c) => c.text);
  return waiting.length ? waiting.join("; ") : (answer.missing ?? "").trim();
}

/** Above the collapsed details: claim correctness and answer completeness, apart (R19); nothing for a complete answer
 * whose claims all held. A round-7 answer's gap paragraph is the one gap summary (KTD4), so its completeness line
 * shows the status only; one short notice says how many claims verification removed and mostly why (no draft text);
 * a clarification shows what it waits for instead of the partial-answer wording. Answers stored before any of these
 * show what they showed. */
function AnswerQuality({ answer }: { answer: ChatAnswer }) {
  const verification = answer.verification;
  const clarification = answer.status === "clarification";
  const completeness = verification?.completeness;
  const correctness = verification?.correctness;
  const removals = verification?.removals ?? [];
  // what changed the text (removed, cut, rewritten, withheld); qualified and cited claims appear in the details only
  const affected = sum(contentCounts(verification));
  const incomplete = !clarification && completeness && completeness.status !== "full";
  // claims only removed (none partly supported): the removal notice says it, with its kind, instead of the
  // generic correctness line
  const onlyRemoved = correctness === "partial" && affected > 0 && !verification?.partial;
  // results kept with the server's conditional qualifier, and nothing else partial or removed
  const onlyConditional =
    correctness === "partial" && (verification?.conditional ?? 0) > 0 && affected === 0 &&
    !verification?.partial && !verification?.removed;
  const doubtful = correctness && correctness !== "verified" && !onlyRemoved;
  // claims whose unsupported qualifier was edited out of a number the sources back (round 13 U12, R17)
  const edited = verification?.edited ?? 0;
  if (!clarification && !incomplete && !doubtful && affected === 0 && edited === 0) return null;
  const structured = hasComponents(answer);
  const pending = clarification ? pendingDetail(answer) : "";
  return (
    <div className="answer-quality">
      {clarification && (
        <p className="clarification-line" data-testid="clarification-lead">
          <strong>נדרשת הבהרה{pending ? ":" : ""}</strong> {pending && <bdi>{pending}</bdi>}
          {pending ? ". " : " "}
          אפשר להשיב בתיבת ההודעה, והמשימה תמשיך עם מה שכבר נמצא.
        </p>
      )}
      {incomplete && structured && (
        <p className="completeness-line" data-testid="completeness" data-status={completeness.status}>
          <strong>{COMPLETENESS_LEAD[completeness.status] ?? "התשובה חלקית"}.</strong> הפירוט לפי רכיבי הבקשה
          בפרטים שמתחת לתשובה.
        </p>
      )}
      {incomplete && !structured && (
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
      {affected > 0 && (
        <p className="removal-line" data-testid="removal-notice" data-kind={dominantKind(removals)}>
          {verification?.actions ? changesNotice(contentCounts(verification), removals) : removalNotice(removals)}
        </p>
      )}
      {edited > 0 && (
        <p className="removal-line" data-testid="edited-notice" data-count={edited}>
          {edited === 1
            ? "האימות ערך טענה אחת: הוסר ממנה ניסוח שלא נכתב במקור, והנתון עצמו נשאר בתשובה."
            : `האימות ערך ${edited} טענות: הוסר מהן ניסוח שלא נכתב במקור, והנתונים עצמם נשארו בתשובה.`}
        </p>
      )}
      {doubtful && (
        <p className="correctness-line" data-testid="correctness" data-status={correctness}>
          {onlyConditional ? CONDITIONAL_LINE : CORRECTNESS_LINE[correctness]}
        </p>
      )}
    </div>
  );
}

/** What verification did to the text, counted by action — "הושמטו חלקים שלא אומתו מ-2 משפטים" when nothing was removed
 * — with the dominant kind's label of the rows that name one, never a claim's words (round 8 U8). */
function changesNotice(counts: Partial<Record<ChangeAction, number>>, rows: ChatRemoval[]): string {
  const head = contentActionsText(counts);
  if (rows.length === 0) return `${head}. הפירוט בפרטים שמתחת לתשובה.`;
  const mixed = new Set(rows.map((r) => r.failure_kind)).size > 1;
  return `${head}; ${mixed ? "הסיבה העיקרית" : "הסיבה"}: ${failureKindText(dominantKind(rows))}. הפירוט בפרטים שמתחת לתשובה.`;
}

/** "Verification removed 2 claims; mostly: …" — the count and the dominant kind's label, never a claim's words (an
 * answer stored before round 8 has no per-action counts). */
function removalNotice(removals: ChatRemoval[]): string {
  const kind = failureKindText(dominantKind(removals));
  const mixed = new Set(removals.map((r) => r.failure_kind)).size > 1;
  const head = removals.length === 1 ? "האימות הסיר טענה אחת" : `האימות הסיר ${removals.length} טענות`;
  return `${head}; ${mixed ? "הסיבה העיקרית" : "הסיבה"}: ${kind}. הפירוט בפרטים שמתחת לתשובה.`;
}

function AnswerDetails({
  answer,
  messageId,
  citations,
  onCite,
  focus,
  expandAll,
  onDetailsToggle,
  removalsId,
  onOpenSource,
}: {
  answer: ChatAnswer;
  messageId: string;
  citations: Map<string, CitationTarget>;
  onCite: (id: string) => void;
  focus: CutFocus | null;
  /** The removal note's button: opens the details with every removal expanded. */
  expandAll: ExpandAll | null;
  /** Reports the real open state of the <details>, so the note's button can mirror it in `aria-expanded`. */
  onDetailsToggle: (open: boolean) => void;
  /** The id of the removals list the note's button controls. */
  removalsId: string;
  onOpenSource?: (nav: ViewerNav) => void;
}) {
  const ordered = [...citations.values()];
  const verification = answer.verification;
  const actions = verification?.actions;
  const changed = verification
    ? verification.removed + verification.partial + verification.annotated + (actions ? sum(actions) : 0)
    : 0;
  const otherChanges = actions ? actions.rewritten + actions.qualified + actions.cited : 0;
  const coverage = answer.coverage.at(-1);
  // a cut marker clicked in the body opens the details, then its own entry (`RemovalItem`)
  const detailsRef = useRef<HTMLDetailsElement>(null);
  const focusTick = focus?.tick;
  useEffect(() => {
    if (focusTick && detailsRef.current) detailsRef.current.open = true;
  }, [focusTick]);
  const expandTick = expandAll?.open ? expandAll.tick : 0;
  useEffect(() => {
    if (expandTick && detailsRef.current) detailsRef.current.open = true;
  }, [expandTick]);
  return (
    <details
      className="msg-details"
      ref={detailsRef}
      onToggle={(e) => onDetailsToggle((e.currentTarget as HTMLDetailsElement).open)}
    >
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
      {answer.unit_scope && <UnitScopeLine scope={answer.unit_scope} />}
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
            {verification.removed > 0 && !verification.removals && (
              <li>הוסרו {verification.removed} טענות שלא נמצאה להן תמיכה במקורות.</li>
            )}
            {actions ? (
              <ActionLines actions={actions} />
            ) : (
              verification.removals &&
              verification.removals.length > 0 && (
                <li>
                  {verification.removals.length === 1
                    ? "האימות הסיר טענה אחת מהתשובה."
                    : `האימות הסיר ${verification.removals.length} טענות מהתשובה.`}
                </li>
              )
            )}
            {verification.partial > 0 && <li>{verification.partial} טענות אומתו חלקית (מסומנות בתשובה).</li>}
            {verification.annotated > 0 && (
              <li>ל-{verification.annotated} נתונים נוסף ליד המספר תיאור שנכתב במקור (מסומן «כפי שנכתב במקור»).</li>
            )}
          </ul>
          {((verification.removals && verification.removals.length > 0) ||
            otherChanges > 0) && (
            <RemovalsSection
              removals={verification.removals ?? []}
              otherChanges={otherChanges}
              components={answer.components ?? []}
              messageId={messageId}
              focus={focus}
              expandAll={expandAll}
              listId={removalsId}
              onOpenSource={onOpenSource}
            />
          )}
        </div>
      )}
      {verification && changed === 0 && (
        <div className="details-section" data-testid="verification">
          <h4>אימות</h4>
          <p>{checkedText(verification)}</p>
        </div>
      )}
      {answer.components ? (
        answer.components.length > 0 && <ComponentsSection answer={answer} components={answer.components} onCite={onCite} />
      ) : (
        answer.ledger?.requirements &&
        answer.ledger.requirements.length > 0 && <RequirementsSection requirements={answer.ledger.requirements} />
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
            {r.calculation && " (חישוב)"} — {COMPONENT_STATUS_LABEL[r.status] ?? r.status}
            {r.status !== "full" && r.limitation_text && <> · {r.limitation_text}</>}
            {r.status !== "full" && r.reason && <span className="muted"> · {r.reason}</span>}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** The components of the request that were not given in full, each with its status, its reason (one label table,
 * never the model's words), where it was looked for, and its claims: those verification removed and the citations
 * tied to it. Components given in full are one count line; components not relevant to the answer are not shown.
 * The gap paragraph itself is in the answer's text, so no gap sentence is repeated here (KTD4). */
function ComponentsSection({
  answer,
  components,
  onCite,
}: {
  answer: ChatAnswer;
  components: ChatComponent[];
  onCite: (id: string) => void;
}) {
  const byId = new Map(components.map((c) => [c.id, c]));
  const parents = new Set(components.map((c) => c.parent).filter(Boolean));
  const leaves = components.filter((c) => !parents.has(c.id) && c.status !== "not_relevant");
  const unmet = leaves.filter((c) => c.status !== "full");
  const full = leaves.length - unmet.length;
  const removals = answer.verification?.removals ?? [];
  // the ids the components' claims cite that open something (`citedView`), each checked once per answer
  const citable = useMemo(
    () => new Set([...new Set(components.flatMap((c) => c.related ?? []))].filter((id) => citedView(answer, id) !== null)),
    [answer, components],
  );
  if (leaves.length === 0) return null;
  return (
    <div className="details-section components" data-testid="components">
      <h4>רכיבי הבקשה</h4>
      {unmet.length > 0 && (
        <ul>
          {unmet.map((c) => {
            const kinds = [
              ...new Set([
                ...removals.filter((r) => r.component === c.id).map((r) => r.failure_kind),
                ...(c.removal_kinds ?? []),
              ]),
            ];
            const reason = gapReasonText(c.limitation) || c.limitation_text || "";
            const where = [c.document_title ?? c.document, c.place?.name].filter(Boolean).join(", ");
            const parent = c.parent ? byId.get(c.parent) : undefined;
            const cited = (c.related ?? []).filter((id) => citable.has(id));
            const kind = COMPONENT_KIND_LABEL[c.kind] ?? "";
            return (
              <li key={c.id} data-testid="component" data-id={c.id} data-status={c.status}>
                {parent && (
                  <span className="muted">
                    <bdi>{parent.text}</bdi> ›{" "}
                  </span>
                )}
                <bdi>{c.text}</bdi>
                {kind && ` (${kind})`}
                {c.conditional && " (מותנה)"} — {COMPONENT_STATUS_LABEL[c.status] ?? c.status}
                {reason && <> · {reason}</>}
                {c.evidence_state === "undeterminable" && c.limitation !== "not_verifiable" && " · המקורות אינם מכריעים"}
                {c.detail && (
                  <span className="muted">
                    {" "}
                    · <bdi>{c.detail}</bdi>
                  </span>
                )}
                {where && (
                  <span className="muted">
                    {" "}
                    · נבדק ב: <bdi>{where}</bdi>
                  </span>
                )}
                {c.uncited && c.uncited.length > 0 && (
                  <span className="muted"> · {c.uncited.length} טענות בלי מקור</span>
                )}
                {(kinds.length > 0 || cited.length > 0) && (
                  <ul className="component-claims">
                    {kinds.map((k) => (
                      <li key={k} data-testid="component-removal">
                        טענה שהוסרה באימות: {failureKindText(k)}
                      </li>
                    ))}
                    {cited.length > 0 && (
                      <li>
                        נתמך ב:{" "}
                        {cited.map((id, i) => (
                          <span key={id}>
                            {i > 0 && " "}
                            <button type="button" className="source-link" onClick={() => onCite(id)}>
                              {id}
                            </button>
                          </span>
                        ))}
                      </li>
                    )}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {full > 0 && (
        <p data-testid="components-full">
          ניתנו במלואם: {full} מתוך {leaves.length} רכיבים.
        </p>
      )}
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


/** The answer's scope line when the conversation stayed in the same appraisal unit as the turn before: the unit by its
 * title (or that it has none) and its pages, beside the file it is in. */
function UnitScopeLine({ scope }: { scope: ChatUnitScope }) {
  const label = unitLabel(scope);
  return (
    <div className="details-section" data-testid="unit-scope">
      <h4>התחום</h4>
      <p>
        נשארתי באותה שומה: <bdi>{label}</bdi>
        {scope.file_title ? (
          <>
            {" "}
            · בקובץ <bdi>{scope.file_title}</bdi>
          </>
        ) : null}
      </p>
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
