"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { AnswerCard, ClarificationBlock, type ClarificationState } from "@/components/AnswerCard";
import { ContextStrip } from "@/components/chat/ContextStrip";
import { B, ErrorAlert } from "@/components/ui";
import {
  ACTIVE_CONVERSATION_KEY,
  api,
  ApiError,
  errorMessage,
  isAbortError,
  newTurnId,
  UNSENT_QUESTION_KEY,
} from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { DATA_KIND_OPTIONS, DATE_FIELD_OPTIONS, formatTimestamp } from "@/lib/format";
import type {
  AskFilters,
  AskRequest,
  Clarification,
  ContextChip,
  ConversationContext,
  ConversationDetail,
  ConversationListItem,
  Message,
} from "@/lib/types";

interface Entry extends Message {
  /** Stable local key: a refreshed answer keeps its place and focus target. */
  key: string;
  /** The question text actually sent (a button choice is displayed as "בחירה: ..."). */
  sent?: string;
}

interface Filters {
  city: string;
  neighborhood: string;
  data_kind: string;
  date_field: string;
  year_from: string;
  year_to: string;
}

const EMPTY_FILTERS: Filters = { city: "", neighborhood: "", data_kind: "", date_field: "", year_from: "", year_to: "" };
const TEXT_KEYS = ["city", "neighborhood", "data_kind", "date_field"] as const;
const SLOW_TURN_MS = 10_000;
// A 409 the user can retry with the same turn id (the server marked that turn failed).
const STATE_CONFLICT_PREFIX = "השיחה עודכנה";
const CONTEXT_STALE =
  "התשובה התקבלה, אך מצב השיחה לא נטען מחדש. אפשרויות ההבהרה מושבתות עד שהמצב ייטען.";

interface TurnOptions {
  /** Replace this entry's answer in place (refresh) instead of appending. */
  replaceKey?: string;
  /** The request carries the filter panel's edits (a typed question or "apply filters"). */
  sendsFilters?: boolean;
}

/** The filter panel mirrors the server context; only what the user changes is sent, as condition edits. */
function filtersFromContext(c: ConversationContext | null | undefined): Filters {
  if (!c) return EMPTY_FILTERS;
  return {
    city: c.city ?? "",
    neighborhood: c.neighborhood ?? "",
    data_kind: c.data_kind ?? "",
    date_field: c.date_field ?? "",
    year_from: c.years?.from != null ? String(c.years.from) : "",
    year_to: c.years?.to != null ? String(c.years.to) : "",
  };
}

function filterEdits(cur: Filters, base: Filters): Pick<AskRequest, "filters" | "remove"> {
  const filters: AskFilters = {};
  const remove: string[] = [];
  for (const k of TEXT_KEYS) {
    const v = cur[k].trim();
    if (v === base[k].trim()) continue;
    if (v) filters[k] = v;
    else remove.push(k);
  }
  if (cur.year_from !== base.year_from || cur.year_to !== base.year_to) {
    if (!cur.year_from && !cur.year_to) remove.push("years");
    if (/^\d{4}$/.test(cur.year_from)) filters.year_from = Number(cur.year_from);
    if (/^\d{4}$/.test(cur.year_to)) filters.year_to = Number(cur.year_to);
  }
  return {
    ...(Object.keys(filters).length > 0 ? { filters } : {}),
    ...(remove.length > 0 ? { remove } : {}),
  };
}

/**
 * The filter panel after a turn: the server's new context, except fields the user edited and did not send — typed
 * while the turn ran (they differ from the snapshot taken when it started), or already dirty but not part of the
 * turn (a clarification button, a chip removal) — which stay as typed and are sent with the next turn.
 */
function keepUnsent(cur: Filters, sentFrom: Filters, sentBase: Filters, sendsFilters: boolean, next: Filters): Filters {
  const out = { ...next };
  for (const k of Object.keys(next) as (keyof Filters)[]) {
    const editedMeanwhile = cur[k] !== sentFrom[k];
    const unsentEdit = !sendsFilters && sentFrom[k] !== sentBase[k];
    if (editedMeanwhile || unsentEdit) out[k] = cur[k];
  }
  return out;
}

function yearError(v: string): string | null {
  if (!v) return null;
  return /^\d{4}$/.test(v) ? null : "יש להזין שנה בת 4 ספרות";
}

function sameClarification(a: Clarification | null | undefined, b: Clarification | null): boolean {
  return !!a && !!b && a.key === b.key && a.question === b.question;
}

function readSession(key: string): string {
  try {
    return window.sessionStorage.getItem(key) ?? "";
  } catch {
    return "";
  }
}

function writeSession(key: string, value: string | null) {
  try {
    if (value) window.sessionStorage.setItem(key, value);
    else window.sessionStorage.removeItem(key);
  } catch {
    // sessionStorage may be unavailable; only the convenience is lost.
  }
}

function toEntries(messages: Message[]): Entry[] {
  return messages.map((m) => ({ ...m, key: m.question_id }));
}

function canRetry(err: unknown): boolean {
  if (!(err instanceof ApiError)) return false;
  return err.isServerError || (err.status === 409 && err.message.startsWith(STATE_CONFLICT_PREFIX));
}

export default function ChatPage() {
  const convList = useApi(api.conversations);
  const conversations: ConversationListItem[] = convList.data?.conversations ?? [];
  const listError = convList.error;
  const loadConversations = convList.reload;

  // The active conversation survives a reload of the tab (R17: the server holds its state).
  // Authenticated pages render only on the client (AppShell waits for /api/auth/me), so reading
  // sessionStorage in an initializer cannot cause a hydration mismatch.
  const [restoreId] = useState(() => (typeof window === "undefined" ? "" : readSession(ACTIVE_CONVERSATION_KEY)));
  const [activeId, setActiveId] = useState<string | null>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [pending, setPending] = useState<Clarification | null>(null);
  const [chips, setChips] = useState<ContextChip[]>([]);
  const [loadingConv, setLoadingConv] = useState(() => restoreId !== "");
  const [convError, setConvError] = useState<string | null>(null);

  // A question left unsent by an expired session (401 → login → back here) is restored.
  const [input, setInput] = useState(() => (typeof window === "undefined" ? "" : readSession(UNSENT_QUESTION_KEY)));
  const [restored, setRestored] = useState(() => typeof window !== "undefined" && readSession(UNSENT_QUESTION_KEY) !== "");
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [baseFilters, setBaseFilters] = useState<Filters>(EMPTY_FILTERS);
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);

  const [busy, setBusy] = useState(false);
  const [slow, setSlow] = useState(false);
  const [askError, setAskError] = useState<{ message: string; retry?: () => void } | null>(null);
  const [focusId, setFocusId] = useState<string | null>(null);
  const answerRefs = useRef(new Map<string, HTMLElement>());
  const questionRef = useRef<HTMLTextAreaElement>(null);

  // Opening, creating and restoring a conversation (and reloading its context) bump this token before they await;
  // a result whose token is no longer current belongs to a superseded navigation and is dropped.
  const navSeq = useRef(0);
  const [navigating, setNavigating] = useState(false);
  // One controller per running turn; all are aborted on unmount, which also stops the 409 polling.
  const turnAborts = useRef(new Set<AbortController>());
  // The context could not be reloaded after a turn: the open clarification shown may already be resolved, so its
  // buttons stay disabled until the context loads again.
  const [contextError, setContextError] = useState<string | null>(null);
  const contextStale = contextError !== null;
  const locked = busy || loadingConv || navigating;

  useEffect(() => {
    const controllers = turnAborts.current;
    return () => {
      for (const c of controllers) c.abort();
      controllers.clear();
    };
  }, []);

  useEffect(() => {
    if (!focusId) return;
    // A draft typed while the answer was coming keeps the focus; the live region still announces the answer.
    const q = questionRef.current;
    if (q && document.activeElement === q && q.value.trim() !== "") return;
    answerRefs.current.get(focusId)?.focus();
  }, [focusId, entries]);

  const selectConversation = useCallback((id: string | null) => {
    setActiveId(id);
    writeSession(ACTIVE_CONVERSATION_KEY, id);
  }, []);

  /**
   * Server state → context chips, the open clarification and the filter panel. `merge` decides which of the
   * panel's current values survive (default: the panel is reset to the server context).
   */
  const applyContext = useCallback((d: ConversationDetail, merge?: (cur: Filters, next: Filters) => Filters) => {
    setChips(d.context?.chips ?? []);
    setPending(d.pending_clarification ?? null);
    const base = filtersFromContext(d.context);
    setBaseFilters(base);
    setFilters((cur) => (merge ? merge(cur, base) : base));
    setContextError(null);
  }, []);

  const showConversation = useCallback(
    (d: ConversationDetail) => {
      selectConversation(d.id);
      setEntries(toEntries(d.messages));
      applyContext(d);
    },
    [applyContext, selectConversation],
  );

  useEffect(() => {
    if (!restoreId) return;
    let cancelled = false;
    const seq = ++navSeq.current;
    const current = () => !cancelled && seq === navSeq.current;
    api
      .conversation(restoreId)
      .then(
        (d) => {
          if (current()) showConversation(d);
        },
        () => {
          // Gone, or another user's: start fresh without an error.
          if (current()) writeSession(ACTIVE_CONVERSATION_KEY, null);
        },
      )
      .finally(() => {
        if (current()) setLoadingConv(false);
      });
    return () => {
      cancelled = true;
    };
  }, [restoreId, showConversation]);

  async function openConversation(id: string) {
    if (locked) return;
    const seq = ++navSeq.current;
    setNavigating(true);
    setLoadingConv(true);
    setConvError(null);
    setAskError(null);
    try {
      const d = await api.conversation(id);
      if (seq === navSeq.current) showConversation(d);
    } catch (err) {
      if (seq === navSeq.current) setConvError(errorMessage(err));
    } finally {
      if (seq === navSeq.current) {
        setLoadingConv(false);
        setNavigating(false);
      }
    }
  }

  async function newConversation() {
    if (locked) return;
    const seq = ++navSeq.current;
    setNavigating(true);
    setConvError(null);
    setAskError(null);
    try {
      const { id } = await api.newConversation();
      if (seq !== navSeq.current) return;
      selectConversation(id);
      setEntries([]);
      setPending(null);
      setChips([]);
      setBaseFilters(EMPTY_FILTERS);
      setFilters(EMPTY_FILTERS);
      setContextError(null);
      loadConversations();
    } catch (err) {
      if (seq === navSeq.current) setConvError(errorMessage(err));
    } finally {
      if (seq === navSeq.current) setNavigating(false);
    }
  }

  /** Reloads the active conversation's context after a failed reload; unsent filter edits are kept. */
  async function reloadContext() {
    if (locked || !activeId) return;
    const id = activeId;
    const from = filters;
    const base = baseFilters;
    const seq = ++navSeq.current;
    setNavigating(true);
    try {
      const d = await api.conversation(id);
      if (seq === navSeq.current) applyContext(d, (cur, next) => keepUnsent(cur, from, base, false, next));
    } catch (err) {
      if (seq === navSeq.current && !(err instanceof ApiError && err.status === 401)) setContextError(CONTEXT_STALE);
    } finally {
      if (seq === navSeq.current) setNavigating(false);
    }
  }

  /**
   * One user action is one turn: its `turn_id` is fixed here and reused by Retry, so a turn the server already
   * finished comes back unchanged instead of being applied twice. `replaceKey` replaces that entry's answer in
   * place (refresh); otherwise the answer is appended, or replaces the entry with the same question id.
   */
  async function runTurn(req: AskRequest, display: string, opts: TurnOptions = {}) {
    const { replaceKey, sendsFilters = false } = opts;
    const body: AskRequest = {
      ...req,
      conversation_id: req.conversation_id ?? activeId ?? undefined,
      turn_id: req.turn_id ?? newTurnId(),
    };
    // What the filter panel held when the turn started: edits made after this are the user's, not the turn's.
    const sentFrom = filters;
    const sentBase = baseFilters;
    const seq = navSeq.current;
    const ctrl = new AbortController();
    turnAborts.current.add(ctrl);
    setBusy(true);
    setAskError(null);
    const timer = window.setTimeout(() => setSlow(true), SLOW_TURN_MS);
    if (body.question && !replaceKey) writeSession(UNSENT_QUESTION_KEY, body.question);
    try {
      const res = await api.askTurn(body, ctrl.signal);
      writeSession(UNSENT_QUESTION_KEY, null);
      setRestored(false);
      // Clear the composer only if it still holds the question sent; a draft typed meanwhile is kept.
      const sent = body.question;
      if (sent && !replaceKey) setInput((cur) => (cur.trim() === sent ? "" : cur));
      if (seq !== navSeq.current) return; // another conversation was opened meanwhile; the turn is stored
      selectConversation(res.conversation_id);
      const key = replaceKey ?? res.question_id;
      const entry: Entry = {
        key,
        question_id: res.question_id,
        question: display,
        sent: body.question,
        answer: res.answer,
        stale: false,
        hidden: false,
        created_at: new Date().toISOString(),
      };
      setEntries((prev) => {
        const at = prev.findIndex((e) => (replaceKey ? e.key === replaceKey : e.question_id === res.question_id));
        if (at < 0) return [...prev, entry];
        const next = [...prev];
        next[at] = { ...entry, key: prev[at].key };
        return next;
      });
      if (res.answer.kind === "clarification" && res.answer.clarification) setPending(res.answer.clarification);
      else if (body.clarification) setPending(null);
      setFocusId(key);
      // The server decides what the turn did to the context and to an open clarification. If that cannot be
      // read, the clarification shown may already be resolved: its buttons are disabled until a reload succeeds.
      try {
        const d = await api.conversation(res.conversation_id, ctrl.signal);
        if (seq === navSeq.current) {
          applyContext(d, (cur, next) => keepUnsent(cur, sentFrom, sentBase, sendsFilters, next));
        }
      } catch (err) {
        if (isAbortError(err) || (err instanceof ApiError && err.status === 401)) return;
        if (seq === navSeq.current) setContextError(CONTEXT_STALE);
      }
      loadConversations();
    } catch (err) {
      if (isAbortError(err)) return; // the page was left; nothing to show
      if (err instanceof ApiError && err.status === 401) return; // redirecting to /login; question kept
      const retry = canRetry(err) ? () => void runTurn(body, display, opts) : undefined;
      setAskError({ message: errorMessage(err), retry });
    } finally {
      turnAborts.current.delete(ctrl);
      window.clearTimeout(timer);
      if (!ctrl.signal.aborted) {
        setSlow(false);
        setBusy(false);
      }
    }
  }

  const edits = filterEdits(filters, baseFilters);
  const filtersDirty = !!(edits.filters || edits.remove);
  const hasAnswer = entries.some((e) => e.answer && e.answer.kind !== "clarification");
  const yearsInvalid = !!(yearError(filters.year_from) || yearError(filters.year_to));

  function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const q = input.trim();
    if (!q || locked) return;
    if (yearsInvalid) {
      setFiltersOpen(true);
      return;
    }
    // Typed text while a clarification is open is sent as is: the server tells an answer to it from a new
    // question, and keeps the clarification open for the latter.
    void runTurn({ question: q, ...edits }, q, { sendsFilters: true });
  }

  function onApplyFilters() {
    if (locked || !filtersDirty || yearsInvalid) return;
    void runTurn({ ...edits }, "עדכון הסינון", { sendsFilters: true });
  }

  function onChoose(key: string, value: string, label: string) {
    if (locked || contextStale) return;
    void runTurn({ clarification: { key, value } }, `בחירה: ${label}`);
  }

  function onRemoveChip(chip: ContextChip) {
    if (locked) return;
    void runTurn({ remove: [chip.key] }, `הסרת תנאי: ${chip.label}: ${chip.value}`);
  }

  function onRefresh(m: Entry) {
    if (locked) return;
    void runTurn({ question: m.sent ?? m.question }, m.question, { replaceKey: m.key });
  }

  function onAskAgain(m: Entry) {
    if (locked) return;
    void runTurn({ question: m.sent ?? m.question }, m.question);
  }

  let pendingIdx = -1;
  if (pending) {
    for (let i = entries.length - 1; i >= 0; i--) {
      if (entries[i].answer?.kind === "clarification" && sameClarification(entries[i].answer?.clarification, pending)) {
        pendingIdx = i;
        break;
      }
    }
  }
  const pendingIsLast = pendingIdx >= 0 && pendingIdx === entries.length - 1;
  const clarState = (idx: number): ClarificationState =>
    idx === pendingIdx ? (pendingIsLast ? "active" : "below") : "done";
  const pinned = pending !== null && !pendingIsLast ? pending : null;

  return (
    <div className="chat-layout">
      <aside className="card stack" aria-label="שיחות">
        <button type="button" className="btn btn-primary" onClick={() => void newConversation()} disabled={locked}>
          שיחה חדשה
        </button>
        <ErrorAlert message={listError} onRetry={loadConversations} />
        {conversations.length === 0 && !listError && <p className="muted small">אין עדיין שיחות.</p>}
        <ul className="conv-list">
          {conversations.map((c) => (
            <li key={c.id}>
              <button
                type="button"
                aria-current={c.id === activeId ? "true" : undefined}
                disabled={locked}
                onClick={() => void openConversation(c.id)}
                style={{ overflowWrap: "anywhere" }}
              >
                <div>{c.title || "שיחה ללא כותרת"}</div>
                <div className="small muted">
                  <B>{formatTimestamp(c.updated_at)}</B>
                </div>
              </button>
            </li>
          ))}
        </ul>
      </aside>

      <section className="stack" aria-label="שיחה" style={{ minWidth: 0 }}>
        <h1>שאלות על מאגר המשרד</h1>
        <ErrorAlert message={convError} />

        <div className="thread" aria-live="polite">
          {loadingConv && <p className="muted">טוען שיחה...</p>}
          {!loadingConv && entries.length === 0 && (
            <p className="muted">
              שאלו בעברית כל שאלה על המסמכים, השומות והעסקאות של המשרד. לדוגמה: מה גודל ממ״ד ממוצע ברמת גן? אילו
              שומות מזכירות היתר בנייה? מה מחיר העסקאות למ״ר בחרוזים בשנת <B>2024</B>?
            </p>
          )}
          {entries.map((m, idx) =>
            m.hidden ? (
              <p key={m.key} className="muted">
                התשובה הוסתרה כי אחד המקורות שלה נמחק או שאינו זמין לך עוד.
              </p>
            ) : (
              <div key={m.key} className="stack">
                <div className="bubble-q">{m.question}</div>
                {m.stale && (
                  <div className="alert alert-warn row">
                    <span className="badge badge-warn" role="status" aria-label="תשובה לא עדכנית">
                      לא עדכני
                    </span>
                    <span>הנתונים או ההרשאות השתנו מאז שניתנה התשובה.</span>
                    <button
                      type="button"
                      className="btn"
                      disabled={locked}
                      onClick={() => onAskAgain(m)}
                    >
                      שאל שוב
                    </button>
                  </div>
                )}
                {m.answer ? (
                  <AnswerCard
                    ref={(el) => {
                      if (el) answerRefs.current.set(m.key, el);
                      else answerRefs.current.delete(m.key);
                    }}
                    answer={m.answer}
                    clarificationState={clarState(idx)}
                    busy={locked}
                    choicesDisabled={contextStale}
                    onChoose={onChoose}
                    onRefresh={() => onRefresh(m)}
                  />
                ) : (
                  <p className="muted">לא התקבלה תשובה לשאלה זו.</p>
                )}
              </div>
            ),
          )}
          {busy && (
            <div className="answer-card muted" role="status">
              {slow ? "קוראים את המסמכים הרלוונטיים. זה עשוי להימשך עד דקה..." : "חושב..."}
            </div>
          )}
        </div>

        {askError && <ErrorAlert message={askError.message} onRetry={locked ? undefined : askError.retry} />}
        <ErrorAlert message={contextError} onRetry={locked ? undefined : () => void reloadContext()} />

        {pinned && (
          <section className="answer-card" aria-label="שאלת הבהרה פתוחה">
            <span className="small muted">שאלת הבהרה פתוחה מתשובה קודמת:</span>
            <ClarificationBlock
              clarification={pinned}
              state="active"
              disabled={locked || contextStale}
              onChoose={(value, label) => onChoose(pinned.key, value, label)}
            />
          </section>
        )}

        <ContextStrip chips={chips} disabled={locked} onRemove={onRemoveChip} />

        <form className="card composer" onSubmit={onSubmit} aria-label="שליחת שאלה">
          <label htmlFor="question" className="visually-hidden">
            שאלה
          </label>
          {restored && <p className="small muted">השאלה שלא נשלחה לפני ההתחברות מחדש שוחזרה.</p>}
          {pending && (
            <p className="small muted">
              ממתינה שאלת הבהרה. אפשר לענות בלחיצה על אחת האפשרויות או בכתיבה חופשית, או לשאול שאלה אחרת — ההבהרה
              תישאר פתוחה.
            </p>
          )}
          <textarea
            id="question"
            ref={questionRef}
            className="input"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="הקלידו שאלה..."
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                e.currentTarget.form?.requestSubmit();
              }
            }}
          />
          <div className="row">
            <button type="submit" className="btn btn-primary" disabled={locked || !input.trim()}>
              {busy ? "חושב..." : "שליחה"}
            </button>
            <button
              type="button"
              className="btn"
              aria-expanded={filtersOpen}
              aria-controls="filters"
              onClick={() => setFiltersOpen((v) => !v)}
            >
              סינון (אופציונלי)
            </button>
            {filtersDirty && !filtersOpen && <span className="small muted">שינויי הסינון יישלחו עם השאלה הבאה.</span>}
          </div>
          {filtersOpen && (
            <fieldset id="filters" className="stack" style={{ border: "none", padding: 0, margin: 0, gap: 8 }}>
              <legend className="visually-hidden">סינון</legend>
              <p className="small muted" style={{ margin: 0 }}>
                הסינון משנה את תנאי השיחה. רק מה ששיניתם נשלח, עם השאלה הבאה או בלחיצה על &quot;החלת הסינון&quot;.
              </p>
              <div className="row" style={{ alignItems: "flex-start" }}>
                <div className="field">
                  <label htmlFor="f-city">עיר</label>
                  <input
                    id="f-city"
                    className="input"
                    value={filters.city}
                    onChange={(e) => setFilters({ ...filters, city: e.target.value })}
                  />
                </div>
                <div className="field">
                  <label htmlFor="f-neighborhood">שכונה</label>
                  <input
                    id="f-neighborhood"
                    className="input"
                    value={filters.neighborhood}
                    onChange={(e) => setFilters({ ...filters, neighborhood: e.target.value })}
                  />
                </div>
                <div className="field">
                  <label htmlFor="f-kind">סוג נתון</label>
                  <select
                    id="f-kind"
                    className="input"
                    value={filters.data_kind}
                    onChange={(e) => setFilters({ ...filters, data_kind: e.target.value })}
                  >
                    <option value="">ללא</option>
                    {DATA_KIND_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label htmlFor="f-date">סוג תאריך</label>
                  <select
                    id="f-date"
                    className="input"
                    value={filters.date_field}
                    onChange={(e) => setFilters({ ...filters, date_field: e.target.value })}
                  >
                    <option value="">ללא</option>
                    {DATE_FIELD_OPTIONS.map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </div>
                {(["year_from", "year_to"] as const).map((k) => {
                  const err = yearError(filters[k]);
                  return (
                    <div className="field" key={k}>
                      <label htmlFor={`f-${k}`}>{k === "year_from" ? "משנה" : "עד שנה"}</label>
                      <input
                        id={`f-${k}`}
                        className="input"
                        inputMode="numeric"
                        dir="ltr"
                        size={6}
                        value={filters[k]}
                        aria-invalid={err ? true : undefined}
                        aria-describedby={err ? `f-${k}-err` : undefined}
                        onChange={(e) => setFilters({ ...filters, [k]: e.target.value.trim() })}
                      />
                      {err && (
                        <span id={`f-${k}-err`} className="field-error">
                          {err}
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>
              <div className="row">
                <button
                  type="button"
                  className="btn"
                  disabled={locked || !filtersDirty || yearsInvalid || !hasAnswer}
                  onClick={onApplyFilters}
                >
                  החלת הסינון
                </button>
                <button type="button" className="btn" onClick={() => setFilters(EMPTY_FILTERS)}>
                  ניקוי
                </button>
                <button type="button" className="btn" disabled={!filtersDirty} onClick={() => setFilters(baseFilters)}>
                  ביטול השינויים
                </button>
              </div>
            </fieldset>
          )}
        </form>
      </section>
    </div>
  );
}
