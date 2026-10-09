// Same-origin fetch helpers. The Next.js server rewrites /api/* to the backend (KTD14),
// so the HttpOnly session cookie travels with every request.

import type {
  AdminCoverage,
  AdminGroup,
  AdminSettings,
  AdminUser,
  AskRequest,
  AskResponse,
  ConversationDetail,
  ConversationListItem,
  DocumentDetail,
  DocumentSummary,
  MeResponse,
  RecordDetail,
  ReviewItem,
  SearchResult,
  UploadResult,
} from "./types";
import type { ChatConversation, ChatMessage, SourceBlocks } from "./chatTypes";
// Facts review (U11)
import type { FactDetail, FactReviewGroup, FactStatus } from "./types";

export const UNSENT_QUESTION_KEY = "rag.unsentQuestion";
export const ACTIVE_CONVERSATION_KEY = "rag.activeConversation";
export const LOGIN_NEXT_KEY = "rag.loginNext";

export const GENERIC_ERROR = "אירעה שגיאה. נסו שוב.";
export const SERVER_ERROR = "אירעה תקלה בשרת. נסו שוב בעוד רגע.";
export const NETWORK_ERROR = "אין חיבור לשרת. בדקו את החיבור ונסו שוב.";

export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
  get isServerError(): boolean {
    return this.status >= 500 || this.status === 0;
  }
}

interface RequestOptions {
  method?: string;
  body?: unknown;
  /** When true a 401 is thrown to the caller instead of redirecting to /login. */
  skipAuthRedirect?: boolean;
  /** Aborts the request; the caller then sees the signal's reason (an `AbortError`), never an ApiError. */
  signal?: AbortSignal;
}

/** True for the error an aborted request or poll throws (see `isAbortError` callers: ignore it silently). */
export function isAbortError(err: unknown): boolean {
  return (err instanceof DOMException || err instanceof Error) && err.name === "AbortError";
}

export function redirectToLogin(): void {
  if (typeof window === "undefined") return;
  if (window.location.pathname.startsWith("/login")) return;
  try {
    window.sessionStorage.setItem(LOGIN_NEXT_KEY, window.location.pathname + window.location.search);
  } catch {
    // sessionStorage may be unavailable; the user simply lands on the default page after login.
  }
  // A full page load is intentional: it drops all client state of the ended session.
  // eslint-disable-next-line @next/next/no-location-assign-relative-destination
  window.location.href = "/login";
}

async function readDetail(res: Response): Promise<string | null> {
  try {
    const data: unknown = await res.json();
    if (data && typeof data === "object" && "detail" in data) {
      const detail = (data as { detail: unknown }).detail;
      if (typeof detail === "string") return detail;
      // FastAPI validation errors arrive as a list; show the first message when present.
      if (Array.isArray(detail) && detail.length > 0) {
        const first: unknown = detail[0];
        if (first && typeof first === "object" && "msg" in first) {
          const msg = (first as { msg: unknown }).msg;
          if (typeof msg === "string") return msg;
        }
      }
    }
  } catch {
    // Non-JSON body.
  }
  return null;
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const init: RequestInit = {
    method: opts.method ?? "GET",
    credentials: "same-origin",
    headers: { Accept: "application/json" },
    cache: "no-store",
  };
  if (opts.signal) init.signal = opts.signal;
  if (opts.body instanceof FormData) {
    init.body = opts.body;
  } else if (opts.body !== undefined) {
    init.body = JSON.stringify(opts.body);
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
  }

  let res: Response;
  try {
    res = await fetch(path, init);
  } catch (err) {
    if (opts.signal?.aborted) throw opts.signal.reason ?? err;
    throw new ApiError(0, NETWORK_ERROR);
  }

  if (res.status === 401 && !opts.skipAuthRedirect) {
    redirectToLogin();
    throw new ApiError(401, "נדרשת התחברות מחדש.");
  }
  if (!res.ok) {
    const detail = await readDetail(res);
    if (res.status >= 500) throw new ApiError(res.status, SERVER_ERROR);
    throw new ApiError(res.status, detail ?? GENERIC_ERROR);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  return GENERIC_ERROR;
}

function qs(params: Record<string, string | number | undefined | null>): string {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

/** Only server-relative API links are rendered as hrefs; anything else is dropped. */
export function safeApiUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  return url.startsWith("/api/") ? url : null;
}

export function fileUrl(documentId: string, versionId: string, page?: number | null): string {
  const base = `/api/documents/${encodeURIComponent(documentId)}/versions/${encodeURIComponent(versionId)}/file`;
  return page ? `${base}#page=${page}` : base;
}

// ---------- Source images (page views) ----------

/** The typed states of a visible source that cannot be shown (KTD5, R12): its reading changed (stale), its file is
 * gone from storage, or the page cannot be drawn. Lost access is never one of them: it is the uniform 404. */
export type SourceFailureState = "stale" | "file_missing" | "render_failed";
const SOURCE_STATES: readonly string[] = ["stale", "file_missing", "render_failed"];

/** A source request that failed. Branch on `state` (and `revoked`), not on the status code: 422 is also FastAPI's
 * validation error, which carries no state. */
export class SourceError extends ApiError {
  readonly state: SourceFailureState | null;
  constructor(status: number, message: string, state: SourceFailureState | null) {
    super(status, message);
    this.state = state;
  }
  /** The document is no longer visible to this user (deleted, moved to another group, access revoked). */
  get revoked(): boolean {
    return this.status === 404 && this.state === null;
  }
}

/** A rendered page, held as an object URL (the caller revokes it): the server sends sources `no-store`, so the
 * image is fetched once per view and never cached by the browser. */
export interface SourceImage {
  url: string;
  /** With a reading id: whether the anchor's reading is still the stored one (null when not asked). */
  readingState: "current" | "stale" | null;
  /** The page's display frame in points (the frame an anchor's rectangles are fractions of). */
  displayWidth: number | null;
  displayHeight: number | null;
}

export type PageScale = "normal" | "zoom";

function versionBase(documentId: string, versionId: string): string {
  return `/api/documents/${encodeURIComponent(documentId)}/versions/${encodeURIComponent(versionId)}`;
}

/** The page image route; `readingId` is the anchor's reading ("none" for one from before readings had ids). */
export function pageImageUrl(
  documentId: string,
  versionId: string,
  page: number,
  scale: PageScale,
  readingId?: string,
): string {
  return `${versionBase(documentId, versionId)}/pages/${page}/image${qs({ scale, reading_id: readingId })}`;
}

function headerNumber(res: Response, name: string): number | null {
  const v = Number(res.headers.get(name));
  return Number.isFinite(v) && v > 0 ? v : null;
}

/** Fetches a source image once. A failure is a `SourceError`: `revoked` for lost access, `state` for a typed
 * failure of a visible source; the caller decides, and nothing here retries. */
export async function fetchSourceImage(url: string, signal?: AbortSignal): Promise<SourceImage> {
  let res: Response;
  try {
    res = await fetch(url, { credentials: "same-origin", cache: "no-store", headers: { Accept: "image/png" }, signal });
  } catch (err) {
    if (signal?.aborted) throw signal.reason ?? err;
    throw new SourceError(0, NETWORK_ERROR, null);
  }
  if (res.status === 401) {
    redirectToLogin();
    throw new SourceError(401, "נדרשת התחברות מחדש.", null);
  }
  if (!res.ok) {
    let state: string | null = res.headers.get("X-Source-State");
    let detail: string | null = null;
    try {
      const data: unknown = await res.json();
      if (data && typeof data === "object") {
        const d = data as { detail?: unknown; state?: unknown };
        if (typeof d.detail === "string") detail = d.detail;
        if (typeof d.state === "string") state = d.state;
      }
    } catch {
      // not JSON: an untyped failure
    }
    const typed = state && SOURCE_STATES.includes(state) ? (state as SourceFailureState) : null;
    throw new SourceError(res.status, detail ?? (res.status >= 500 ? SERVER_ERROR : GENERIC_ERROR), typed);
  }
  const blob = await res.blob();
  const reading = res.headers.get("X-Reading-State");
  return {
    url: URL.createObjectURL(blob),
    readingState: reading === "current" || reading === "stale" ? reading : null,
    displayWidth: headerNumber(res, "X-Display-Width"),
    displayHeight: headerNumber(res, "X-Display-Height"),
  };
}

export const chatApi = {
  conversations: (opts: { q?: string; archived?: boolean; before?: string | null; limit?: number } = {}, signal?: AbortSignal) =>
    request<{ conversations: ChatConversation[]; next: string | null }>(
      `/api/chat/conversations${qs({ q: opts.q, archived: opts.archived ? "true" : undefined, before: opts.before, limit: opts.limit })}`,
      { signal },
    ),
  create: () => request<ChatConversation>("/api/chat/conversations", { method: "POST" }),
  patch: (id: string, body: { title?: string; archived?: boolean }) =>
    request<ChatConversation>(`/api/chat/conversations/${encodeURIComponent(id)}`, { method: "PATCH", body }),
  remove: (id: string) => request<{ ok: true }>(`/api/chat/conversations/${encodeURIComponent(id)}`, { method: "DELETE" }),
  messages: (id: string, before?: string | null, signal?: AbortSignal) =>
    request<{ conversation: ChatConversation; messages: ChatMessage[]; has_more: boolean }>(
      `/api/chat/conversations/${encodeURIComponent(id)}/messages${qs({ before })}`,
      { signal },
    ),
  send: (id: string, content: string, clientId: string) =>
    request<{ user: ChatMessage; assistant: ChatMessage | null }>(
      `/api/chat/conversations/${encodeURIComponent(id)}/messages`,
      { method: "POST", body: { content, client_id: clientId } },
    ),
  message: (id: string, signal?: AbortSignal) =>
    request<ChatMessage>(`/api/chat/messages/${encodeURIComponent(id)}`, { signal }),
  cancel: (id: string) => request<ChatMessage>(`/api/chat/messages/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  retry: (id: string) => request<ChatMessage>(`/api/chat/messages/${encodeURIComponent(id)}/retry`, { method: "POST" }),
  /** `readingId`: the reading a citation was made from ("none" for one from before readings had ids); a
   * reprocessed document then answers `stale` with no blocks. */
  blocks: (
    documentId: string,
    versionId: string,
    start?: number,
    end?: number,
    signal?: AbortSignal,
    readingId?: string,
  ) =>
    request<SourceBlocks>(
      `/api/documents/${encodeURIComponent(documentId)}/versions/${encodeURIComponent(versionId)}/blocks${qs({ start, end, reading_id: readingId })}`,
      { signal },
    ),
};

export const api = {
  // Auth
  login: (email: string, password: string) =>
    request<{ ok: true }>("/api/auth/login", { method: "POST", body: { email, password }, skipAuthRedirect: true }),
  logout: () => request<{ ok: true }>("/api/auth/logout", { method: "POST", skipAuthRedirect: true }),
  me: () => request<MeResponse>("/api/auth/me"),

  // Documents
  // One request per file: a request never carries more than one file (the /api proxy buffers each request
  // body up to `proxyClientMaxBodySize`), and one file's failure never loses the others' results.
  upload: async (files: File[], groupId: string, documentId?: string) => {
    const results: UploadResult[] = [];
    for (const f of files) {
      const fd = new FormData();
      fd.append("files", f, f.name);
      fd.append("group_id", groupId);
      if (documentId) fd.append("document_id", documentId);
      try {
        const res = await request<{ results: UploadResult[] }>("/api/documents", { method: "POST", body: fd });
        results.push(...res.results);
      } catch (err) {
        if (files.length === 1) throw err;
        const reason = err instanceof Error ? err.message : "שגיאה בהעלאה";
        results.push({ filename: f.name, status: "rejected", reason });
      }
    }
    return { results };
  },
  documents: (q?: string, status?: string) =>
    request<{ documents: DocumentSummary[] }>(`/api/documents${qs({ q, status })}`),
  document: (id: string) => request<DocumentDetail>(`/api/documents/${encodeURIComponent(id)}`),
  deleteDocument: (id: string) =>
    request<{ ok: true }>(`/api/documents/${encodeURIComponent(id)}`, { method: "DELETE" }),
  search: (q: string, limit = 20) => request<{ results: SearchResult[] }>(`/api/search${qs({ q, limit })}`),

  // Review
  reviewQueue: (documentId?: string) =>
    request<{ items: ReviewItem[] }>(`/api/review/queue${qs({ document_id: documentId })}`),
  record: (id: string) => request<RecordDetail>(`/api/review/records/${encodeURIComponent(id)}`),
  approve: (id: string, note?: string) =>
    request<RecordDetail>(`/api/review/records/${encodeURIComponent(id)}/approve`, {
      method: "POST",
      body: note ? { note } : {},
    }),
  correct: (id: string, field: string, value: string, note: string) =>
    request<RecordDetail>(`/api/review/records/${encodeURIComponent(id)}/correct`, {
      method: "POST",
      body: { field, value, note },
    }),
  reject: (id: string, note: string) =>
    request<RecordDetail>(`/api/review/records/${encodeURIComponent(id)}/reject`, { method: "POST", body: { note } }),
  merge: (id: string) =>
    request<{ ok: true }>(`/api/review/dedup/${encodeURIComponent(id)}/merge`, { method: "POST" }),
  keepSeparate: (id: string) =>
    request<{ ok: true }>(`/api/review/dedup/${encodeURIComponent(id)}/keep-separate`, { method: "POST" }),

  // Chat
  conversations: () => request<{ conversations: ConversationListItem[] }>("/api/conversations"),
  newConversation: () => request<{ id: string }>("/api/conversations", { method: "POST" }),
  conversation: (id: string, signal?: AbortSignal) =>
    request<ConversationDetail>(`/api/conversations/${encodeURIComponent(id)}`, { signal }),
  ask: (body: AskRequest) => request<AskResponse>("/api/ask", { method: "POST", body }),
  /**
   * Posts a turn; while the server still processes the same `turn_id` (409) it polls until the result is stored.
   * Aborting `signal` stops the request and the polling (the promise rejects with an `AbortError`).
   */
  askTurn: (body: AskRequest, signal?: AbortSignal) => askTurn(body, signal),

  // Admin
  users: () => request<{ users: AdminUser[] }>("/api/admin/users"),
  createUser: (body: {
    email: string;
    full_name: string;
    password: string;
    role: AdminUser["role"];
    can_upload: boolean;
    group_ids: string[];
  }) => request<unknown>("/api/admin/users", { method: "POST", body }),
  updateUser: (
    id: string,
    body: Partial<Pick<AdminUser, "role" | "can_upload" | "is_active" | "group_ids">> & { password?: string },
  ) => request<unknown>(`/api/admin/users/${encodeURIComponent(id)}`, { method: "PATCH", body }),
  groups: () => request<{ groups: AdminGroup[] }>("/api/admin/groups"),
  createGroup: (name: string) => request<unknown>("/api/admin/groups", { method: "POST", body: { name } }),
  settings: () => request<AdminSettings>("/api/admin/settings"),
  saveSettings: (cloudEnabled: boolean, acknowledge: boolean) =>
    request<unknown>("/api/admin/settings", {
      method: "PUT",
      body: { cloud_llm_enabled: cloudEnabled, acknowledge },
    }),
  testProvider: () => request<AdminSettings>("/api/admin/provider/test", { method: "POST" }),
  coverage: () => request<AdminCoverage>("/api/admin/coverage"),
};

// ---------- Chat turns ----------

/** The server's 409 detail for a turn that is still running (KTD13). */
export const TURN_IN_PROGRESS = "השאלה עדיין בעיבוד";
const POLL_INTERVAL_MS = 2_000;
const POLL_LIMIT_MS = 150_000;
const TURN_TIMEOUT = "המענה לשאלה מתעכב. נסו שוב בעוד רגע; השאלה לא תישלח פעמיים.";

/** A v4 UUID; `crypto.randomUUID` exists only in secure contexts, so plain HTTP falls back to getRandomValues. */
export function newTurnId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const b = crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

export function isTurnInProgress(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409 && err.message === TURN_IN_PROGRESS;
}

/**
 * Re-posting the same body (same `turn_id`) is the poll the contract defines: a finished turn returns its stored
 * result unchanged, a running one answers 409 again, and a failed one runs again on its reservation.
 */
async function askTurn(body: AskRequest, signal?: AbortSignal): Promise<AskResponse> {
  const started = Date.now();
  for (;;) {
    signal?.throwIfAborted();
    try {
      return await request<AskResponse>("/api/ask", { method: "POST", body, signal });
    } catch (err) {
      if (!isTurnInProgress(err)) throw err;
      if (Date.now() - started > POLL_LIMIT_MS) throw new ApiError(0, TURN_TIMEOUT);
      await sleep(POLL_INTERVAL_MS, signal);
    }
  }
}

/** Waits `ms`; rejects with the signal's reason as soon as it aborts. */
function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason);
      return;
    }
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(signal?.reason);
    };
    const timer = window.setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

// ---------- Facts review (U11) ----------

/**
 * What the reviewer saw when acting. The server applies the change only if the row still matches; otherwise it
 * answers 409 (`FACT_CHANGED`) and changes nothing, so an action on a stale list never overwrites another review.
 */
export interface FactPrecondition {
  expected_status: FactStatus;
  expected_value?: string | null;
}

/** The server's 409 detail when a fact changed since the list was loaded. */
export const FACT_CHANGED = "הערך השתנה בינתיים; טענו את הרשימה מחדש";

export const factsApi = {
  list: () => request<{ attributes: FactReviewGroup[] }>("/api/review/facts"),
  get: (id: string) => request<FactDetail>(`/api/review/facts/${encodeURIComponent(id)}`),
  approve: (id: string, pre: FactPrecondition, note?: string) =>
    request<FactDetail>(`/api/review/facts/${encodeURIComponent(id)}/approve`, {
      method: "POST",
      body: note ? { note, ...pre } : { ...pre },
    }),
  reject: (id: string, note: string, pre: FactPrecondition) =>
    request<FactDetail>(`/api/review/facts/${encodeURIComponent(id)}/reject`, {
      method: "POST",
      body: { note, ...pre },
    }),
  correct: (id: string, value: string, unit: string, pre: FactPrecondition, note?: string) =>
    request<FactDetail>(`/api/review/facts/${encodeURIComponent(id)}/correct`, {
      method: "POST",
      body: note ? { value, unit, note, ...pre } : { value, unit, ...pre },
    }),
};

// ---------- end Facts review ----------
