"use client";

import Link from "next/link";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useSession } from "@/components/AppShell";
import { api, ApiError, chatApi, errorMessage, isAbortError, newTurnId } from "@/lib/api";
import type { ChatAnswer, ChatConversation, ChatMessage, ChatSource } from "@/lib/chatTypes";
import { AssistantMessage, UserMessage, citedSource } from "./Message";
import { SourcePanel } from "./SourcePanel";
import "./chat.css";

const POLL_MS = 900;
const LAST_KEY = "rag.chat.last";
const THEME_KEY = "rag.theme";
const NEAR_BOTTOM = 120;
const RUNNING = new Set(["running", "cancelling"]);

interface Thread {
  messages: ChatMessage[];
  hasMore: boolean;
  loading: boolean;
  error: string | null;
  /** Errors of messages this tab failed to send, by client id. */
  sendErrors: Record<string, string>;
}

const EMPTY_THREAD: Thread = { messages: [], hasMore: false, loading: false, error: null, sendErrors: {} };

function readLocal(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeLocal(key: string, value: string | null) {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    // storage unavailable: nothing is remembered locally, the server keeps the history
  }
}

function urlConversation(): string | null {
  if (typeof window === "undefined") return null;
  return new URLSearchParams(window.location.search).get("c");
}

function setUrlConversation(id: string | null) {
  const url = id ? `/chat?c=${encodeURIComponent(id)}` : "/chat";
  if (window.location.pathname + window.location.search !== url) window.history.replaceState(null, "", url);
}

type Theme = "system" | "light" | "dark";

function applyTheme(theme: Theme) {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
}

function dayGroup(iso: string): string {
  const d = new Date(iso);
  const now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const t = d.getTime();
  if (t >= start) return "היום";
  if (t >= start - 86_400_000) return "אתמול";
  if (t >= start - 7 * 86_400_000) return "7 הימים האחרונים";
  if (t >= start - 30 * 86_400_000) return "30 הימים האחרונים";
  return "ישן יותר";
}

export default function ChatApp() {
  const me = useSession();
  const [conversations, setConversations] = useState<ChatConversation[]>([]);
  const [listNext, setListNext] = useState<string | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [archived, setArchived] = useState(false);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [threads, setThreads] = useState<Record<string, Thread>>({});
  const [draftThread, setDraftThread] = useState<ChatMessage[]>([]);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [panel, setPanel] = useState<ChatSource | null>(null);
  const [theme, setTheme] = useState<Theme>("system");
  const [sending, setSending] = useState(false);
  const [composerText, setComposerText] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const activeRef = useRef<string | null>(null);
  activeRef.current = activeId;
  // A conversation this tab just created holds only the message being sent: no history to load for it.
  const createdHere = useRef<string | null>(null);

  // --- initial state ---------------------------------------------------------------------------------------
  useEffect(() => {
    const t = (readLocal(THEME_KEY) as Theme | null) ?? "system";
    setTheme(t);
    applyTheme(t);
    if (window.matchMedia("(max-width: 768px)").matches) setSidebarOpen(false);
    const initial = urlConversation() ?? readLocal(LAST_KEY);
    if (initial) setActiveId(initial);
  }, []);

  // --- conversation list -----------------------------------------------------------------------------------
  const loadList = useCallback(
    async (append: boolean, signal?: AbortSignal) => {
      try {
        const res = await chatApi.conversations(
          { q: query.trim() || undefined, archived, before: append ? listNext : null },
          signal,
        );
        setConversations((cur) => (append ? [...cur, ...res.conversations] : res.conversations));
        setListNext(res.next);
        setListError(null);
      } catch (err) {
        if (!isAbortError(err)) setListError(errorMessage(err));
      }
    },
    [query, archived, listNext],
  );

  useEffect(() => {
    const ctrl = new AbortController();
    const h = window.setTimeout(() => void loadList(false, ctrl.signal), query ? 250 : 0);
    return () => {
      window.clearTimeout(h);
      ctrl.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, archived]);

  const touchConversation = useCallback((c: ChatConversation) => {
    setConversations((cur) => [c, ...cur.filter((x) => x.id !== c.id)]);
  }, []);

  // --- active conversation's messages ------------------------------------------------------------------------
  const updateThread = useCallback((id: string, f: (t: Thread) => Thread) => {
    setThreads((cur) => ({ ...cur, [id]: f(cur[id] ?? EMPTY_THREAD) }));
  }, []);

  const loadMessages = useCallback(
    async (id: string, older: boolean, signal?: AbortSignal) => {
      const thread = threads[id];
      const before = older && thread?.messages.length ? thread.messages[0].id : null;
      updateThread(id, (t) => ({ ...t, loading: true, error: null }));
      try {
        const res = await chatApi.messages(id, before, signal);
        updateThread(id, (t) => ({
          ...t,
          loading: false,
          hasMore: res.has_more,
          messages: older ? [...res.messages, ...t.messages.filter((m) => !res.messages.some((r) => r.id === m.id))] : res.messages,
        }));
        if (!older) touchConversation(res.conversation);
      } catch (err) {
        if (isAbortError(err)) return;
        if (err instanceof ApiError && err.status === 404) {
          writeLocal(LAST_KEY, null);
          if (activeRef.current === id) {
            setActiveId(null);
            setNotice("השיחה לא נמצאה (ייתכן שנמחקה).");
          }
          return;
        }
        updateThread(id, (t) => ({ ...t, loading: false, error: errorMessage(err) }));
      }
    },
    [threads, updateThread, touchConversation],
  );

  useEffect(() => {
    setPanel(null);
    if (activeId) {
      setUrlConversation(activeId);
      writeLocal(LAST_KEY, activeId);
      if (createdHere.current === activeId) return undefined;
      const ctrl = new AbortController();
      void loadMessages(activeId, false, ctrl.signal);
      return () => ctrl.abort();
    }
    setUrlConversation(null);
    return undefined;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId]);

  // --- polling running answers of the active conversation ---------------------------------------------------
  const active = activeId ? threads[activeId] ?? EMPTY_THREAD : null;
  const runningIds = useMemo(
    () => (active ? active.messages.filter((m) => m.role === "assistant" && RUNNING.has(m.status)).map((m) => m.id) : []),
    [active],
  );
  const runningKey = runningIds.join(",");

  useEffect(() => {
    if (!activeId || runningIds.length === 0) return;
    const conversation = activeId;
    const ctrl = new AbortController();
    let timer = 0;
    const tick = async () => {
      for (const id of runningIds) {
        try {
          const m = await chatApi.message(id, ctrl.signal);
          if (ctrl.signal.aborted) return;
          // only into the conversation the message belongs to, even if the user switched meanwhile
          updateThread(conversation, (t) => ({ ...t, messages: t.messages.map((x) => (x.id === m.id ? m : x)) }));
          if (!RUNNING.has(m.status)) {
            setConversations((cur) => cur.map((c) => (c.id === conversation ? { ...c, updated_at: new Date().toISOString() } : c)));
          }
        } catch (err) {
          if (isAbortError(err)) return;
        }
      }
      if (!ctrl.signal.aborted) timer = window.setTimeout(tick, POLL_MS);
    };
    timer = window.setTimeout(tick, POLL_MS);
    return () => {
      ctrl.abort();
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeId, runningKey]);

  // --- sending ---------------------------------------------------------------------------------------------
  const busy = runningIds.length > 0 || sending;

  const send = useCallback(
    async (text: string, clientId: string = newTurnId()) => {
      const content = text.trim();
      if (!content || sending) return;
      setSending(true);
      setNotice(null);
      const optimistic: ChatMessage = {
        id: `local-${clientId}`,
        role: "user",
        content,
        status: "done",
        error: null,
        progress: [],
        answer: null,
        reply_to: null,
        client_id: clientId,
        created_at: new Date().toISOString(),
        stale: false,
        cancel_requested: false,
      };
      const pending: ChatMessage = {
        ...optimistic,
        id: `local-a-${clientId}`,
        role: "assistant",
        content: "",
        status: "running",
        progress: [{ step: "send", label: "שולח" }],
        client_id: null,
      };
      let conversationId = activeRef.current;
      try {
        if (!conversationId) {
          setDraftThread([optimistic, pending]);
          const conv = await chatApi.create();
          conversationId = conv.id;
          createdHere.current = conv.id;
          touchConversation(conv);
          setThreads((cur) => ({ ...cur, [conv.id]: { ...EMPTY_THREAD, messages: [optimistic, pending] } }));
          setActiveId(conv.id);
          setDraftThread([]);
        } else {
          const cid = conversationId;
          updateThread(cid, (t) => ({
            ...t,
            messages: [...t.messages.filter((m) => m.client_id !== clientId && m.id !== `local-a-${clientId}`), optimistic, pending],
            sendErrors: Object.fromEntries(Object.entries(t.sendErrors).filter(([k]) => k !== clientId)),
          }));
        }
        const res = await chatApi.send(conversationId, content, clientId);
        const cid = conversationId;
        updateThread(cid, (t) => ({
          ...t,
          messages: [
            ...t.messages.filter((m) => m.id !== optimistic.id && m.id !== pending.id && m.id !== res.user.id),
            res.user,
            ...(res.assistant ? [res.assistant] : []),
          ],
        }));
        setConversations((cur) =>
          cur.map((c) =>
            c.id === cid && c.title === "שיחה חדשה" ? { ...c, title: content.slice(0, 80), updated_at: new Date().toISOString() } : c,
          ),
        );
      } catch (err) {
        const msg = errorMessage(err);
        if (conversationId) {
          const cid = conversationId;
          updateThread(cid, (t) => ({
            ...t,
            messages: t.messages.filter((m) => m.id !== pending.id),
            sendErrors: { ...t.sendErrors, [clientId]: msg },
          }));
        } else {
          setDraftThread([]);
          setNotice(msg);
          setComposerText(content);
        }
      } finally {
        setSending(false);
        createdHere.current = null; // from now on, coming back to it loads it from the server like any other
      }
    },
    [sending, touchConversation, updateThread],
  );

  const stop = useCallback(
    async (m: ChatMessage) => {
      const cid = activeRef.current;
      if (!cid || m.id.startsWith("local-")) return;
      try {
        const res = await chatApi.cancel(m.id);
        updateThread(cid, (t) => ({ ...t, messages: t.messages.map((x) => (x.id === res.id ? res : x)) }));
      } catch (err) {
        setNotice(errorMessage(err));
      }
    },
    [updateThread],
  );

  const retry = useCallback(
    async (m: ChatMessage) => {
      const cid = activeRef.current;
      if (!cid || busy) return;
      try {
        const res = await chatApi.retry(m.id);
        updateThread(cid, (t) => ({ ...t, messages: t.messages.map((x) => (x.id === m.id ? res : x)) }));
      } catch (err) {
        setNotice(errorMessage(err));
      }
    },
    [busy, updateThread],
  );

  const openSource = useCallback((answer: ChatAnswer, id: string) => {
    const s = citedSource(answer, id);
    if (s) setPanel(s);
  }, []);

  // --- conversation actions --------------------------------------------------------------------------------
  const newChat = useCallback(() => {
    setActiveId(null);
    setDraftThread([]);
    setComposerText("");
    setNotice(null);
    writeLocal(LAST_KEY, null);
    if (window.matchMedia("(max-width: 768px)").matches) setSidebarOpen(false);
  }, []);

  const select = useCallback((id: string) => {
    setActiveId(id);
    setNotice(null);
    if (window.matchMedia("(max-width: 768px)").matches) setSidebarOpen(false);
  }, []);

  const rename = useCallback(async (id: string, title: string) => {
    try {
      const c = await chatApi.patch(id, { title });
      setConversations((cur) => cur.map((x) => (x.id === id ? c : x)));
    } catch (err) {
      setNotice(errorMessage(err));
    }
  }, []);

  const archive = useCallback(
    async (id: string, value: boolean) => {
      try {
        await chatApi.patch(id, { archived: value });
        setConversations((cur) => cur.filter((x) => x.id !== id));
        if (activeRef.current === id && value) newChat();
      } catch (err) {
        setNotice(errorMessage(err));
      }
    },
    [newChat],
  );

  const remove = useCallback(
    async (id: string) => {
      if (!window.confirm("למחוק את השיחה לצמיתות? לא ניתן לשחזר אותה.")) return;
      try {
        await chatApi.remove(id);
        setConversations((cur) => cur.filter((x) => x.id !== id));
        setThreads((cur) => {
          const next = { ...cur };
          delete next[id];
          return next;
        });
        if (activeRef.current === id) newChat();
      } catch (err) {
        setNotice(errorMessage(err));
      }
    },
    [newChat],
  );

  const changeTheme = (t: Theme) => {
    setTheme(t);
    applyTheme(t);
    writeLocal(THEME_KEY, t === "system" ? null : t);
  };

  const logout = async () => {
    try {
      await api.logout();
    } catch {
      // leaving anyway
    }
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination
    window.location.href = "/login";
  };

  const messages = activeId ? active?.messages ?? [] : draftThread;
  const title = activeId ? conversations.find((c) => c.id === activeId)?.title ?? "" : "";
  const isAdmin = me?.user.role === "admin";

  return (
    <div className="chat-root">
      <aside className="chat-sidebar" data-open={sidebarOpen ? "true" : "false"} aria-label="היסטוריית שיחות" aria-hidden={!sidebarOpen}>
        <div className="chat-sidebar-top">
          <button type="button" className="new-chat-btn" onClick={newChat}>
            <span aria-hidden="true">✎</span> שיחה חדשה
          </button>
          <button type="button" className="icon-btn" onClick={() => setSidebarOpen(false)} aria-label="סגירת סרגל הצד" title="סגירת סרגל הצד">
            ⟨
          </button>
        </div>
        <input
          className="chat-search"
          type="search"
          placeholder={archived ? "חיפוש בשיחות שבארכיון" : "חיפוש בשיחות"}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          aria-label="חיפוש בשיחות"
        />
        <ConversationList
          items={conversations}
          activeId={activeId}
          archived={archived}
          onSelect={select}
          onRename={rename}
          onArchive={archive}
          onDelete={remove}
        />
        {listError && <div className="alert alert-error" style={{ margin: 8 }}>{listError}</div>}
        {listNext && (
          <button type="button" className="btn btn-small" style={{ margin: "0 10px 8px" }} onClick={() => void loadList(true)}>
            עוד שיחות
          </button>
        )}
        <div className="chat-sidebar-foot">
          <button type="button" className="link" onClick={() => setArchived((a) => !a)}>
            {archived ? "← חזרה לשיחות" : "ארכיון שיחות"}
          </button>
          <Link href="/documents">מסמכים</Link>
          <Link href="/review">בדיקת נתונים</Link>
          {isAdmin && <Link href="/admin">ניהול</Link>}
          <label className="chat-user">
            <span>ערכת צבעים</span>
            <select value={theme} onChange={(e) => changeTheme(e.target.value as Theme)} aria-label="ערכת צבעים">
              <option value="system">לפי המערכת</option>
              <option value="light">בהיר</option>
              <option value="dark">כהה</option>
            </select>
          </label>
          <div className="chat-user">
            <span>
              {me?.user.full_name} · {me?.office.name}
            </span>
            <button type="button" className="link" onClick={logout}>
              יציאה
            </button>
          </div>
        </div>
      </aside>
      <div className="chat-scrim" data-open={sidebarOpen ? "true" : "false"} onClick={() => setSidebarOpen(false)} aria-hidden="true" />
      <main className="chat-main">
        <div className="chat-topbar">
          {!sidebarOpen && (
            <button type="button" className="icon-btn" onClick={() => setSidebarOpen(true)} aria-label="פתיחת סרגל הצד" title="היסטוריית שיחות">
              ☰
            </button>
          )}
          {!sidebarOpen && (
            <button type="button" className="icon-btn" onClick={newChat} aria-label="שיחה חדשה" title="שיחה חדשה">
              ✎
            </button>
          )}
          <span className="title">{title || "עוזר המסמכים של המשרד"}</span>
        </div>
        <ThreadView
          key={activeId ?? "draft"}
          messages={messages}
          thread={active}
          sendErrors={active?.sendErrors ?? {}}
          onLoadOlder={() => activeId && void loadMessages(activeId, true)}
          onCite={openSource}
          onRetry={retry}
          onStop={stop}
          onResend={(m) => void send(m.content, m.client_id ?? undefined)}
        />
        {notice && (
          <div className="composer-hint" role="alert" style={{ color: "var(--danger)" }}>
            {notice}
          </div>
        )}
        <Composer
          value={composerText}
          onChange={setComposerText}
          busy={busy}
          running={runningIds.length > 0}
          onSend={(t) => {
            setComposerText("");
            void send(t);
          }}
          onStop={() => {
            const m = active?.messages.find((x) => x.id === runningIds[0]);
            if (m) void stop(m);
          }}
        />
      </main>
      {panel && <SourcePanel key={`${panel.version_id}:${panel.id}`} source={panel} onClose={() => setPanel(null)} />}
    </div>
  );
}

function ConversationList({
  items,
  activeId,
  archived,
  onSelect,
  onRename,
  onArchive,
  onDelete,
}: {
  items: ChatConversation[];
  activeId: string | null;
  archived: boolean;
  onSelect: (id: string) => void;
  onRename: (id: string, title: string) => void;
  onArchive: (id: string, value: boolean) => void;
  onDelete: (id: string) => void;
}) {
  const [menu, setMenu] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    window.addEventListener("click", close);
    return () => window.removeEventListener("click", close);
  }, [menu]);

  if (items.length === 0) {
    return <div className="chat-list"><p className="muted" style={{ padding: 10 }}>{archived ? "אין שיחות בארכיון." : "עדיין אין שיחות."}</p></div>;
  }
  const groups: { name: string; items: ChatConversation[] }[] = [];
  for (const c of items) {
    const g = dayGroup(c.updated_at);
    if (!groups.length || groups[groups.length - 1].name !== g) groups.push({ name: g, items: [] });
    groups[groups.length - 1].items.push(c);
  }
  return (
    <nav className="chat-list" aria-label="שיחות">
      {groups.map((g) => (
        <div key={g.name}>
          <h3>{g.name}</h3>
          {g.items.map((c) => (
            <div key={c.id} className="chat-item" data-active={c.id === activeId ? "true" : "false"}>
              {editing === c.id ? (
                <input
                  autoFocus
                  value={draft}
                  aria-label="שם השיחה"
                  onChange={(e) => setDraft(e.target.value)}
                  onBlur={() => {
                    if (draft.trim() && draft.trim() !== c.title) onRename(c.id, draft.trim());
                    setEditing(null);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") (e.target as HTMLInputElement).blur();
                    if (e.key === "Escape") setEditing(null);
                  }}
                />
              ) : (
                <a
                  href={`/chat?c=${encodeURIComponent(c.id)}`}
                  aria-current={c.id === activeId ? "page" : undefined}
                  onClick={(e) => {
                    e.preventDefault();
                    onSelect(c.id);
                  }}
                  title={c.title}
                >
                  {c.title}
                </a>
              )}
              <button
                type="button"
                className="icon-btn"
                aria-label={`פעולות לשיחה ${c.title}`}
                aria-expanded={menu === c.id}
                onClick={(e) => {
                  e.stopPropagation();
                  setMenu(menu === c.id ? null : c.id);
                }}
              >
                ⋯
              </button>
              {menu === c.id && (
                <div className="chat-menu" role="menu" onClick={(e) => e.stopPropagation()}>
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      setDraft(c.title);
                      setEditing(c.id);
                      setMenu(null);
                    }}
                  >
                    שינוי שם
                  </button>
                  <button
                    type="button"
                    role="menuitem"
                    onClick={() => {
                      setMenu(null);
                      onArchive(c.id, !archived);
                    }}
                  >
                    {archived ? "הוצאה מהארכיון" : "העברה לארכיון"}
                  </button>
                  <button
                    type="button"
                    role="menuitem"
                    className="danger"
                    onClick={() => {
                      setMenu(null);
                      onDelete(c.id);
                    }}
                  >
                    מחיקה
                  </button>
                </div>
              )}
            </div>
          ))}
        </div>
      ))}
    </nav>
  );
}

function ThreadView({
  messages,
  thread,
  sendErrors,
  onLoadOlder,
  onCite,
  onRetry,
  onStop,
  onResend,
}: {
  messages: ChatMessage[];
  thread: Thread | null;
  sendErrors: Record<string, string>;
  onLoadOlder: () => void;
  onCite: (answer: ChatAnswer, id: string) => void;
  onRetry: (m: ChatMessage) => void;
  onStop: (m: ChatMessage) => void;
  onResend: (m: ChatMessage) => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [atBottom, setAtBottom] = useState(true);
  const atBottomRef = useRef(true);
  const prevHeight = useRef(0);
  const prevFirst = useRef<string | null>(null);
  const firstLoad = useRef(true);

  const onScroll = () => {
    const el = ref.current;
    if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < NEAR_BOTTOM;
    atBottomRef.current = near;
    setAtBottom(near);
    if (el.scrollTop < 80 && thread?.hasMore && !thread.loading) onLoadOlder();
  };

  // keep the reader's place: older messages prepended keep the viewport where it was; new content follows the
  // bottom only when the reader is already there
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const first = messages[0]?.id ?? null;
    if (firstLoad.current && messages.length) {
      el.scrollTop = el.scrollHeight;
      firstLoad.current = false;
    } else if (prevFirst.current && first !== prevFirst.current && prevHeight.current) {
      el.scrollTop += el.scrollHeight - prevHeight.current;
    } else if (atBottomRef.current) {
      el.scrollTop = el.scrollHeight;
    }
    prevHeight.current = el.scrollHeight;
    prevFirst.current = first;
  }, [messages]);

  const jump = () => {
    const el = ref.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  };

  return (
    <>
      <div className="chat-thread" ref={ref} onScroll={onScroll} role="log" aria-live="polite" aria-relevant="additions">
        <div className="chat-thread-inner">
          {messages.length === 0 && !thread?.loading && (
            <div className="chat-empty">
              <h1>במה אפשר לעזור?</h1>
              <p>אפשר לשאול על כל מה שכתוב בשומות ובמסמכי המשרד: נתונים, הסברים, השוואות, טבלאות וחישובים.</p>
            </div>
          )}
          {thread?.hasMore && (
            <button type="button" className="btn btn-small load-older" onClick={onLoadOlder} disabled={thread.loading}>
              {thread.loading ? "טוען…" : "הודעות קודמות"}
            </button>
          )}
          {thread?.error && <div className="alert alert-error">{thread.error}</div>}
          {messages.map((m, i) =>
            m.role === "user" ? (
              <UserMessage
                key={m.id}
                message={m}
                error={m.client_id ? sendErrors[m.client_id] : null}
                onResend={() => onResend(m)}
              />
            ) : (
              <AssistantMessage
                key={m.id}
                message={m}
                isLast={i === messages.length - 1}
                onCite={onCite}
                onRetry={onRetry}
                onStop={onStop}
              />
            ),
          )}
        </div>
      </div>
      {!atBottom && (
        <button type="button" className="jump-latest" onClick={jump}>
          ↓ להודעה האחרונה
        </button>
      )}
    </>
  );
}

function Composer({
  value,
  onChange,
  busy,
  running,
  onSend,
  onStop,
}: {
  value: string;
  onChange: (v: string) => void;
  busy: boolean;
  running: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const composing = useRef(false);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  }, [value]);

  useEffect(() => {
    ref.current?.focus();
  }, []);

  const submit = () => {
    if (busy || !value.trim()) return;
    onSend(value);
  };

  return (
    <div className="chat-composer-wrap">
      <form
        className="chat-composer"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <textarea
          ref={ref}
          rows={1}
          dir="auto"
          value={value}
          maxLength={4000}
          placeholder="שאלו על המסמכים של המשרד…"
          aria-label="הודעה"
          onChange={(e) => onChange(e.target.value)}
          onCompositionStart={() => (composing.current = true)}
          onCompositionEnd={() => (composing.current = false)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !composing.current && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
        />
        {running ? (
          <button type="button" className="send-btn" onClick={onStop} aria-label="עצירת התשובה" title="עצירה">
            ■
          </button>
        ) : (
          <button type="submit" className="send-btn" disabled={busy || !value.trim()} aria-label="שליחה" title="שליחה (Enter)">
            ↑
          </button>
        )}
      </form>
      <p className="composer-hint">התשובות מבוססות על מסמכי המשרד בלבד ומצוטטות. Enter לשליחה, Shift+Enter לשורה חדשה.</p>
    </div>
  );
}
