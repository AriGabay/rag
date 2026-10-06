"use client";

import { useEffect, useRef, useState } from "react";
import { AnswerCard, ClarificationBlock } from "@/components/AnswerCard";
import { B, ErrorAlert } from "@/components/ui";
import { api, ApiError, errorMessage, UNSENT_QUESTION_KEY } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { DATA_KIND_OPTIONS, DATE_FIELD_OPTIONS, formatTimestamp } from "@/lib/format";
import type { AskFilters, AskRequest, Clarification, ConversationDetail, ConversationListItem, Message } from "@/lib/types";

interface Entry extends Message {
  note?: string;
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

const DISCARDED_NOTE = "ההבהרה הממתינה בוטלה, והטקסט נשלח כשאלה חדשה.";

function toAskFilters(f: Filters): AskFilters | undefined {
  const out: AskFilters = {};
  if (f.city.trim()) out.city = f.city.trim();
  if (f.neighborhood.trim()) out.neighborhood = f.neighborhood.trim();
  if (f.data_kind) out.data_kind = f.data_kind;
  if (f.date_field) out.date_field = f.date_field;
  if (/^\d{4}$/.test(f.year_from)) out.year_from = Number(f.year_from);
  if (/^\d{4}$/.test(f.year_to)) out.year_to = Number(f.year_to);
  return Object.keys(out).length > 0 ? out : undefined;
}

function yearError(v: string): string | null {
  if (!v) return null;
  return /^\d{4}$/.test(v) ? null : "יש להזין שנה בת 4 ספרות";
}

function conditionsList(c: ConversationDetail["confirmed_conditions"]): { label: string; value: string }[] {
  if (!c) return [];
  if (Array.isArray(c)) {
    return c.filter((x) => x && typeof x.label === "string").map((x) => ({ label: x.label, value: String(x.value) }));
  }
  return Object.entries(c)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .map(([k, v]) => ({ label: k, value: String(v) }));
}

function readUnsent(): string {
  try {
    return window.sessionStorage.getItem(UNSENT_QUESTION_KEY) ?? "";
  } catch {
    return "";
  }
}

function hasUnsent(): boolean {
  return typeof window !== "undefined" && readUnsent() !== "";
}

function writeUnsent(q: string | null) {
  try {
    if (q) window.sessionStorage.setItem(UNSENT_QUESTION_KEY, q);
    else window.sessionStorage.removeItem(UNSENT_QUESTION_KEY);
  } catch {
    // ignore
  }
}

export default function ChatPage() {
  const convList = useApi(api.conversations);
  const conversations: ConversationListItem[] = convList.data?.conversations ?? [];
  const listError = convList.error;
  const loadConversations = convList.reload;
  const [activeId, setActiveId] = useState<string | null>(null);
  const [activeConditions, setActiveConditions] = useState<{ label: string; value: string }[]>([]);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [pending, setPending] = useState<Clarification | null>(null);
  const [loadingConv, setLoadingConv] = useState(false);
  const [convError, setConvError] = useState<string | null>(null);

  // A question left unsent by an expired session (401 → login → back here) is restored.
  // Authenticated pages render only on the client (AppShell waits for /api/auth/me), so reading
  // sessionStorage in the initializer cannot cause a hydration mismatch.
  const [input, setInput] = useState(() => (typeof window === "undefined" ? "" : readUnsent()));
  const [restored, setRestored] = useState(() => hasUnsent());
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);

  const [busy, setBusy] = useState(false);
  const [askError, setAskError] = useState<{ message: string; retry?: () => void } | null>(null);
  const [focusId, setFocusId] = useState<string | null>(null);
  const answerRefs = useRef(new Map<string, HTMLElement>());

  useEffect(() => {
    if (!focusId) return;
    answerRefs.current.get(focusId)?.focus();
  }, [focusId, entries]);

  async function openConversation(id: string) {
    setLoadingConv(true);
    setConvError(null);
    setAskError(null);
    try {
      const d = await api.conversation(id);
      setActiveId(d.id);
      setEntries(d.messages);
      setPending(d.pending_clarification ?? null);
      setActiveConditions(conditionsList(d.confirmed_conditions));
    } catch (err) {
      setConvError(errorMessage(err));
    } finally {
      setLoadingConv(false);
    }
  }

  async function newConversation() {
    setConvError(null);
    setAskError(null);
    try {
      const { id } = await api.newConversation();
      setActiveId(id);
      setEntries([]);
      setPending(null);
      setActiveConditions([]);
      setFilters(EMPTY_FILTERS);
      loadConversations();
    } catch (err) {
      setConvError(errorMessage(err));
    }
  }

  async function send(req: AskRequest, displayQuestion: string, note?: string) {
    setBusy(true);
    setAskError(null);
    if (req.question) writeUnsent(req.question);
    try {
      const res = await api.ask({ ...req, conversation_id: activeId ?? undefined });
      writeUnsent(null);
      setRestored(false);
      if (req.question) setInput("");
      setActiveId(res.conversation_id);
      const entry: Entry = {
        question_id: res.question_id,
        question: displayQuestion,
        answer: res.answer,
        stale: false,
        hidden: false,
        created_at: new Date().toISOString(),
        note,
      };
      setEntries((prev) => [...prev, entry]);
      setPending(res.answer.kind === "clarification" && res.answer.clarification ? res.answer.clarification : null);
      setFocusId(res.question_id);
      loadConversations();
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) return; // redirecting to /login; question kept
      const message = errorMessage(err);
      const retry = err instanceof ApiError && err.isServerError ? () => void send(req, displayQuestion, note) : undefined;
      setAskError({ message, retry });
    } finally {
      setBusy(false);
    }
  }

  function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const q = input.trim();
    if (!q || busy) return;
    if (yearError(filters.year_from) || yearError(filters.year_to)) {
      setFiltersOpen(true);
      return;
    }
    // Typed text while a clarification is pending is a new question; the pending clarification is discarded.
    const note = pending ? DISCARDED_NOTE : undefined;
    void send({ question: q, filters: toAskFilters(filters) }, q, note);
  }

  function onChoose(key: string, value: string, label: string) {
    if (busy) return;
    void send({ clarification: { key, value } }, `בחירה: ${label}`);
  }

  const lastIdx = entries.length - 1;
  const lastClarIdx = lastIdx >= 0 && entries[lastIdx].answer?.kind === "clarification" ? lastIdx : -1;
  const pendingShownInThread = pending !== null && lastClarIdx >= 0;

  return (
    <div className="chat-layout">
      <aside className="card stack" aria-label="שיחות">
        <button type="button" className="btn btn-primary" onClick={newConversation} disabled={busy}>
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
                onClick={() => void openConversation(c.id)}
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

      <section className="stack" aria-label="שיחה">
        <h1>שאלות על מאגר המשרד</h1>
        <ErrorAlert message={convError} />
        {activeConditions.length > 0 && (
          <div className="small">
            <strong>תנאים שאושרו בשיחה: </strong>
            {activeConditions.map((c, i) => (
              <span key={`${c.label}-${i}`}>
                {i > 0 && " · "}
                {c.label}: <B>{c.value}</B>
              </span>
            ))}
          </div>
        )}

        <div className="thread" aria-live="polite">
          {loadingConv && <p className="muted">טוען שיחה...</p>}
          {!loadingConv && entries.length === 0 && (
            <p className="muted">
              שאלו שאלה בעברית על העסקאות, השומות והמסמכים של המשרד. לדוגמה: מה מחיר למ״ר ברמת גן בשכונת חרוזים בשנת{" "}
              <B>2024</B>?
            </p>
          )}
          {entries.map((m, idx) =>
            m.hidden ? (
              <p key={m.question_id} className="muted">
                התשובה הוסתרה כי אחד המקורות שלה נמחק או שאינו זמין לך עוד.
              </p>
            ) : (
              <div key={m.question_id} className="stack">
                <div className="bubble-q">{m.question}</div>
                {m.note && <p className="small muted">{m.note}</p>}
                {m.stale && (
                  <div className="alert alert-warn row">
                    <span className="badge badge-warn" role="status" aria-label="תשובה לא עדכנית">
                      לא עדכני
                    </span>
                    <span>הנתונים או ההרשאות השתנו מאז שניתנה התשובה.</span>
                    <button
                      type="button"
                      className="btn"
                      disabled={busy}
                      onClick={() => void send({ question: m.question }, m.question)}
                    >
                      שאל שוב
                    </button>
                  </div>
                )}
                {m.answer ? (
                  <AnswerCard
                    ref={(el) => {
                      if (el) answerRefs.current.set(m.question_id, el);
                      else answerRefs.current.delete(m.question_id);
                    }}
                    answer={m.answer}
                    clarificationActive={pending !== null && idx === lastClarIdx}
                    busy={busy}
                    onChoose={onChoose}
                  />
                ) : (
                  <p className="muted">לא התקבלה תשובה לשאלה זו.</p>
                )}
              </div>
            ),
          )}
          {pending && !pendingShownInThread && (
            <div className="answer-card">
              <ClarificationBlock
                clarification={pending}
                active
                disabled={busy}
                onChoose={(value, label) => onChoose(pending.key, value, label)}
              />
            </div>
          )}
          {busy && (
            <div className="answer-card muted" role="status">
              חושב...
            </div>
          )}
        </div>

        {askError && <ErrorAlert message={askError.message} onRetry={askError.retry} />}

        <form className="card composer" onSubmit={onSubmit} aria-label="שליחת שאלה">
          <label htmlFor="question" className="visually-hidden">
            שאלה
          </label>
          {restored && <p className="small muted">השאלה שלא נשלחה לפני ההתחברות מחדש שוחזרה.</p>}
          {pending && (
            <p className="small muted">
              ממתינה שאלת הבהרה. בחרו אחת מהאפשרויות, או הקלידו שאלה חדשה (ההבהרה הממתינה תבוטל).
            </p>
          )}
          <textarea
            id="question"
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
            <button type="submit" className="btn btn-primary" disabled={busy || !input.trim()}>
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
          </div>
          {filtersOpen && (
            <fieldset id="filters" className="row" style={{ border: "none", padding: 0, margin: 0, alignItems: "flex-start" }}>
              <legend className="visually-hidden">סינון</legend>
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
              <button type="button" className="btn" style={{ alignSelf: "flex-end" }} onClick={() => setFilters(EMPTY_FILTERS)}>
                ניקוי
              </button>
            </fieldset>
          )}
        </form>
      </section>
    </div>
  );
}
