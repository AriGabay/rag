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
// Facts review (U11)
import type { FactDetail, FactReviewGroup } from "./types";

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
  if (opts.body instanceof FormData) {
    init.body = opts.body;
  } else if (opts.body !== undefined) {
    init.body = JSON.stringify(opts.body);
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
  }

  let res: Response;
  try {
    res = await fetch(path, init);
  } catch {
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

export const api = {
  // Auth
  login: (email: string, password: string) =>
    request<{ ok: true }>("/api/auth/login", { method: "POST", body: { email, password }, skipAuthRedirect: true }),
  logout: () => request<{ ok: true }>("/api/auth/logout", { method: "POST", skipAuthRedirect: true }),
  me: () => request<MeResponse>("/api/auth/me"),

  // Documents
  upload: (files: File[], groupId: string, documentId?: string) => {
    const fd = new FormData();
    for (const f of files) fd.append("files", f, f.name);
    fd.append("group_id", groupId);
    if (documentId) fd.append("document_id", documentId);
    return request<{ results: UploadResult[] }>("/api/documents", { method: "POST", body: fd });
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
  conversation: (id: string) => request<ConversationDetail>(`/api/conversations/${encodeURIComponent(id)}`),
  ask: (body: AskRequest) => request<AskResponse>("/api/ask", { method: "POST", body }),
  /** Posts a turn; while the server still processes the same `turn_id` (409) it polls until the result is stored. */
  askTurn: (body: AskRequest) => askTurn(body),

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
async function askTurn(body: AskRequest): Promise<AskResponse> {
  const started = Date.now();
  for (;;) {
    try {
      return await request<AskResponse>("/api/ask", { method: "POST", body });
    } catch (err) {
      if (!isTurnInProgress(err)) throw err;
      if (Date.now() - started > POLL_LIMIT_MS) throw new ApiError(0, TURN_TIMEOUT);
      await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
    }
  }
}

// ---------- Facts review (U11) ----------

export const factsApi = {
  list: () => request<{ attributes: FactReviewGroup[] }>("/api/review/facts"),
  get: (id: string) => request<FactDetail>(`/api/review/facts/${encodeURIComponent(id)}`),
  approve: (id: string, note?: string) =>
    request<FactDetail>(`/api/review/facts/${encodeURIComponent(id)}/approve`, {
      method: "POST",
      body: note ? { note } : {},
    }),
  reject: (id: string, note: string) =>
    request<FactDetail>(`/api/review/facts/${encodeURIComponent(id)}/reject`, { method: "POST", body: { note } }),
  correct: (id: string, value: string, unit: string, note?: string) =>
    request<FactDetail>(`/api/review/facts/${encodeURIComponent(id)}/correct`, {
      method: "POST",
      body: note ? { value, unit, note } : { value, unit },
    }),
};

// ---------- end Facts review ----------
