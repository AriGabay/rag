import path from "node:path";
import { expect, type APIRequestContext, type Locator, type Page } from "@playwright/test";

// Synthetic demo users created by backend/scripts/seed_demo.py.
export const PASSWORD = process.env.E2E_PASSWORD ?? "demo1234";
export const USERS = {
  adminA: "admin-a@demo.test",
  dana: "dana@demo.test",
  yossi: "yossi@demo.test",
  adminB: "admin-b@demo.test",
} as const;

export const FIXTURES = path.resolve(__dirname, "../../backend/tests/fixtures");
export const fixture = (name: string) => path.join(FIXTURES, name);

/** Screenshots for the report land in frontend/test-results/screenshots (gitignored, kept across runs). */
export const shot = (name: string) => path.resolve(__dirname, "../test-results/screenshots", name);

export async function login(page: Page, email: string, password = PASSWORD): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("דוא״ל").fill(email);
  await page.getByLabel("סיסמה").fill(password);
  await page.getByRole("button", { name: "כניסה" }).click();
  // The app opens on the chat, which has its own sidebar instead of the header navigation.
  await expect(page.getByRole("button", { name: "שיחה חדשה" }).first()).toBeVisible();
}

/** API-level session for test setup/cleanup only (same-origin through the Next proxy). */
export async function apiLogin(request: APIRequestContext, email: string): Promise<void> {
  const res = await request.post("/api/auth/login", { data: { email, password: PASSWORD } });
  expect(res.ok(), `login ${email}: ${res.status()}`).toBeTruthy();
}

interface DocSummary {
  id: string;
  title: string;
  deleted: boolean;
}

/**
 * Logically deletes office B documents with the given title so an upload test starts from the same state on every
 * run (the backend deduplicates uploads by content hash). Only ever called with an office B session.
 */
export async function deleteOfficeBDocsByTitle(request: APIRequestContext, title: string): Promise<void> {
  await apiLogin(request, USERS.adminB);
  const me = await (await request.get("/api/auth/me")).json();
  expect(me.user.email, "cleanup must run as the office B admin").toBe(USERS.adminB);
  const { documents } = (await (await request.get(`/api/documents?q=${encodeURIComponent(title)}`)).json()) as {
    documents: DocSummary[];
  };
  for (const d of documents.filter((x) => x.title === title && !x.deleted)) {
    const res = await request.delete(`/api/documents/${d.id}`);
    expect(res.ok(), `delete ${d.title}: ${res.status()}`).toBeTruthy();
  }
  await request.post("/api/auth/logout");
}

/** The chat's message box and send button, and the assistant replies in the thread. */
export function composer(page: Page): Locator {
  return page.getByRole("textbox", { name: "הודעה" });
}

export function assistantMessages(page: Page): Locator {
  return page.locator(".msg-assistant");
}

/** Sends a message with Enter and waits until a new assistant reply is no longer working (answer, failure or
 * cancellation). Returns the reply's locator. */
export async function sendAndWait(page: Page, text: string, timeout = 240_000): Promise<Locator> {
  const before = await assistantMessages(page).count();
  await composer(page).fill(text);
  await composer(page).press("Enter");
  const reply = assistantMessages(page).nth(before);
  await expect(reply).toBeVisible();
  await expect(reply.locator(".status-line")).toHaveCount(0, { timeout });
  return reply;
}

/** Turns cloud use on (true) or off for office B, as the office B admin. Only ever called for office B. */
export async function setOfficeBCloud(request: APIRequestContext, enabled: boolean): Promise<void> {
  await apiLogin(request, USERS.adminB);
  const me = await (await request.get("/api/auth/me")).json();
  expect(me.user.email, "settings change must run as the office B admin").toBe(USERS.adminB);
  const res = await request.put("/api/admin/settings", { data: { cloud_llm_enabled: enabled, acknowledge: enabled } });
  expect(res.ok(), `office B cloud ${enabled}: ${res.status()}`).toBeTruthy();
  await request.post("/api/auth/logout");
}

const MIME: Record<string, string> = {
  ".pdf": "application/pdf",
  ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
};

/** The upload's content type, from the fixture's extension (a DOCX unless it is a PDF). */
export function mimeOf(file: string): string {
  return MIME[path.extname(file).toLowerCase()] ?? MIME[".docx"];
}

type ListedDoc = DocSummary & { group: { id: string; name: string }; current_version: { id: string; status: string } | null };

async function listOfficeBDocs(request: APIRequestContext, title: string): Promise<ListedDoc[]> {
  const res = await request.get(`/api/documents?q=${encodeURIComponent(title)}`);
  return ((await res.json()) as { documents: ListedDoc[] }).documents.filter((d) => d.title === title && !d.deleted);
}

/** One office B document as the tests use it: its id and its current version. */
export interface OfficeBDoc {
  id: string;
  versionId: string;
}

/**
 * Uploads each fixture to office B (as its admin) unless a live copy with its title exists, into `groupId` (default:
 * the office's first group), then waits until every one is processed. All uploads are queued before the first wait,
 * so the shared worker reads them back to back. Only ever writes office B.
 */
export async function ensureOfficeBDocuments(
  request: APIRequestContext,
  items: { file: string; title: string }[],
  opts: { groupId?: string; timeout?: number } = {},
): Promise<Record<string, OfficeBDoc>> {
  await apiLogin(request, USERS.adminB);
  const me = await (await request.get("/api/auth/me")).json();
  expect(me.user.email, "uploads must run as the office B admin").toBe(USERS.adminB);
  const fs = await import("node:fs");
  let groupId = opts.groupId;
  for (const { file, title } of items) {
    if ((await listOfficeBDocs(request, title)).length > 0) continue;
    if (!groupId) {
      const groups = (await (await request.get("/api/admin/groups")).json()) as { groups: { id: string }[] };
      groupId = groups.groups[0].id;
    }
    const res = await request.post("/api/documents", {
      multipart: {
        files: { name: path.basename(file), mimeType: mimeOf(file), buffer: fs.readFileSync(file) },
        group_id: groupId,
      },
    });
    expect(res.ok(), `upload ${title}: ${res.status()}`).toBeTruthy();
  }
  const out: Record<string, OfficeBDoc> = {};
  for (const { title } of items) {
    await expect
      .poll(async () => (await listOfficeBDocs(request, title))[0]?.current_version?.status ?? "none", {
        message: `${title} is processed`,
        timeout: opts.timeout ?? 900_000,
        intervals: [3000],
      })
      .toMatch(/^(ready|needs_review)$/);
    const [doc] = await listOfficeBDocs(request, title);
    out[title] = { id: doc.id, versionId: doc.current_version!.id };
  }
  await request.post("/api/auth/logout");
  return out;
}

/** Uploads a fixture to office B (as its admin) unless a live copy exists, and waits until it is processed. */
export async function ensureOfficeBDocument(
  request: APIRequestContext,
  file: string,
  title: string,
  opts: { groupId?: string } = {},
): Promise<string> {
  const docs = await ensureOfficeBDocuments(request, [{ file, title }], opts);
  return docs[title].id;
}

/** Uploads `file` as a new version of an office B document (as its admin) and waits until that version is current
 * and processed. Returns the new version's id. */
export async function uploadOfficeBVersion(
  request: APIRequestContext,
  documentId: string,
  file: string,
  title: string,
): Promise<string> {
  await apiLogin(request, USERS.adminB);
  const fs = await import("node:fs");
  const before = (await listOfficeBDocs(request, title)).find((d) => d.id === documentId)?.current_version?.id;
  const res = await request.post("/api/documents", {
    multipart: {
      files: { name: path.basename(file), mimeType: mimeOf(file), buffer: fs.readFileSync(file) },
      document_id: documentId,
    },
  });
  expect(res.ok(), `new version of ${title}: ${res.status()}`).toBeTruthy();
  let versionId = "";
  await expect
    .poll(
      async () => {
        const doc = (await listOfficeBDocs(request, title)).find((d) => d.id === documentId);
        versionId = doc?.current_version?.id ?? "";
        return versionId && versionId !== before ? doc!.current_version!.status : "waiting";
      },
      { message: `the new version of ${title} is processed`, timeout: 900_000, intervals: [3000] },
    )
    .toMatch(/^(ready|needs_review)$/);
  await request.post("/api/auth/logout");
  return versionId;
}

/** The reading a version's blocks are currently read from (what a new citation is pinned to). */
export async function currentReading(request: APIRequestContext, doc: OfficeBDoc): Promise<string | null> {
  await apiLogin(request, USERS.adminB);
  const res = await request.get(`/api/documents/${doc.id}/versions/${doc.versionId}/blocks?start=0&end=0`);
  expect(res.ok(), `blocks of ${doc.id}: ${res.status()}`).toBeTruthy();
  const body = (await res.json()) as { reading_id: string | null };
  await request.post("/api/auth/logout");
  return body.reading_id;
}

/** Office B's provider settings as its admin sees them (whether a model key is configured, the mode). */
export async function officeBSettings(request: APIRequestContext): Promise<{ key_present: boolean; mode: string }> {
  await apiLogin(request, USERS.adminB);
  const res = await request.get("/api/admin/settings");
  expect(res.ok(), `office B settings: ${res.status()}`).toBeTruthy();
  const body = await res.json();
  await request.post("/api/auth/logout");
  return body;
}

/** An office B document group with this name (created when missing), as its admin. */
export async function ensureOfficeBGroup(request: APIRequestContext, name: string): Promise<string> {
  await apiLogin(request, USERS.adminB);
  const find = async () =>
    ((await (await request.get("/api/admin/groups")).json()) as { groups: { id: string; name: string }[] }).groups.find(
      (g) => g.name === name,
    );
  let group = await find();
  if (!group) {
    const res = await request.post("/api/admin/groups", { data: { name } });
    expect(res.ok() || res.status() === 409, `create group ${name}: ${res.status()}`).toBeTruthy();
    group = await find();
  }
  await request.post("/api/auth/logout");
  return group!.id;
}

/**
 * A synthetic office B employee (no upload rights) in exactly `groupIds`, created when missing; an existing one is
 * reactivated, put back in those groups and given the demo password. Returns the user's id. Office B only.
 */
export async function ensureOfficeBEmployee(
  request: APIRequestContext,
  email: string,
  fullName: string,
  groupIds: string[],
): Promise<string> {
  await apiLogin(request, USERS.adminB);
  const me = await (await request.get("/api/auth/me")).json();
  expect(me.user.email, "user changes must run as the office B admin").toBe(USERS.adminB);
  const find = async () =>
    ((await (await request.get("/api/admin/users")).json()) as { users: { id: string; email: string }[] }).users.find(
      (u) => u.email === email,
    );
  let user = await find();
  if (!user) {
    const res = await request.post("/api/admin/users", {
      data: { email, full_name: fullName, password: PASSWORD, role: "employee", can_upload: false, group_ids: groupIds },
    });
    expect(res.ok(), `create ${email}: ${res.status()}`).toBeTruthy();
    user = await find();
  } else {
    const res = await request.patch(`/api/admin/users/${user.id}`, {
      data: { is_active: true, group_ids: groupIds, password: PASSWORD },
    });
    expect(res.ok(), `reset ${email}: ${res.status()}`).toBeTruthy();
  }
  await request.post("/api/auth/logout");
  return user!.id;
}

/** Sets an office B user's groups (as the office B admin): an empty list takes away every group's documents. */
export async function setOfficeBUserGroups(request: APIRequestContext, userId: string, groupIds: string[]): Promise<void> {
  await apiLogin(request, USERS.adminB);
  const res = await request.patch(`/api/admin/users/${userId}`, { data: { group_ids: groupIds } });
  expect(res.ok(), `groups of ${userId}: ${res.status()}`).toBeTruthy();
  await request.post("/api/auth/logout");
}

/**
 * Reads office B's current documents again through the admin API (every version, `all`) and waits until no reindex
 * job is queued or running. Only ever called for office B.
 */
export async function reprocessOfficeB(request: APIRequestContext, timeout = 1_500_000): Promise<number> {
  await apiLogin(request, USERS.adminB);
  const me = await (await request.get("/api/auth/me")).json();
  expect(me.user.email, "reprocess must run as the office B admin").toBe(USERS.adminB);
  const res = await request.post("/api/admin/reprocess", { data: { all: true } });
  expect(res.ok(), `reprocess office B: ${res.status()}`).toBeTruthy();
  const { queued } = (await res.json()) as { queued: number };
  await expect
    .poll(
      async () => {
        const jobs = ((await (await request.get("/api/admin/jobs")).json()) as {
          jobs: { kind: string; status: string; count: number }[];
        }).jobs;
        return jobs
          .filter((j) => j.kind === "process:reindex" && (j.status === "queued" || j.status === "running"))
          .reduce((n, j) => n + j.count, 0);
      },
      { message: "office B reprocess finished", timeout, intervals: [5000] },
    )
    .toBe(0);
  await request.post("/api/auth/logout");
  return queued;
}

/** The stored answer of the conversation's last reply (the page's own session), with the conversation's id. */
export async function lastAnswer<T = Record<string, unknown>>(page: Page): Promise<{ conversationId: string; answer: T }> {
  await expect(page).toHaveURL(/\/chat\?c=/);
  const conversationId = new URL(page.url()).searchParams.get("c")!;
  const res = await page.request.get(`/api/chat/conversations/${conversationId}/messages`);
  expect(res.ok(), `messages of ${conversationId}: ${res.status()}`).toBeTruthy();
  const { messages } = (await res.json()) as { messages: { role: string; answer: T | null }[] };
  const replies = messages.filter((m) => m.role === "assistant" && m.answer);
  expect(replies.length, "the conversation has an answer").toBeGreaterThan(0);
  return { conversationId, answer: replies[replies.length - 1].answer! };
}

/** Citation ids in order of first appearance in an answer's text: chip n is the n-th (as the chat numbers them). */
export function citationOrder(markdown: string): string[] {
  const out: string[] = [];
  for (const m of markdown.matchAll(/\[((?:[SMCVA]\d+)(?:\s*[,،;]\s*[SMCVA]\d+)*)\]/g)) {
    for (const id of m[1].split(/\s*[,،;]\s*/)) if (!out.includes(id)) out.push(id);
  }
  return out;
}

/** The chip of citation `id` in a reply (its accessible name starts with "מקור n:"). */
export function citationChip(reply: Locator, markdown: string, id: string): Locator {
  const n = citationOrder(markdown).indexOf(id) + 1;
  expect(n, `${id} is cited in the answer text`).toBeGreaterThan(0);
  return reply.locator(".body").getByRole("button", { name: new RegExp(`^מקור ${n}:`) }).first();
}

/**
 * Asserts that `digits` (e.g. "25,336") is rendered inside a <bdi> under `scope` and that its characters are laid out
 * left to right on screen, i.e. the number is not reversed by the RTL context.
 */
export async function expectNumberInVisualOrder(scope: Locator, digits: string): Promise<void> {
  const result = await scope.evaluate((root, needle) => {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const text = n.textContent ?? "";
      const start = text.indexOf(needle);
      if (start < 0) continue;
      const inBdi = !!n.parentElement?.closest("bdi");
      const xs: number[] = [];
      for (let i = 0; i < needle.length; i++) {
        const r = document.createRange();
        r.setStart(n, start + i);
        r.setEnd(n, start + i + 1);
        const rect = r.getBoundingClientRect();
        xs.push(rect.left + rect.width / 2);
      }
      return { found: true, inBdi, xs };
    }
    return { found: false, inBdi: false, xs: [] as number[] };
  }, digits);
  expect(result.found, `"${digits}" is rendered`).toBe(true);
  expect(result.inBdi, `"${digits}" is isolated in <bdi>`).toBe(true);
  for (let i = 1; i < result.xs.length; i++) {
    expect(result.xs[i], `character ${i} of "${digits}" is right of character ${i - 1}`).toBeGreaterThan(result.xs[i - 1]);
  }
}

/** The source viewer beside the chat. */
export function sourceViewer(page: Page): Locator {
  return page.getByRole("complementary", { name: "תצוגת מקור" });
}

/** Opens a reply's details (collapsed by default). */
export async function openDetails(reply: Locator): Promise<void> {
  const details = reply.locator("details.msg-details");
  if ((await details.getAttribute("open")) === null) await details.locator("> summary").click();
  await expect(details).toHaveAttribute("open", "");
}

/** Whether an office B version's reading holds a table the vision model read from a picture (at ingestion). */
export async function officeBHasVisionTable(request: APIRequestContext, doc: OfficeBDoc): Promise<boolean> {
  await apiLogin(request, USERS.adminB);
  const res = await request.get(`/api/documents/${doc.id}/versions/${doc.versionId}/blocks`);
  expect(res.ok(), `blocks of ${doc.id}: ${res.status()}`).toBeTruthy();
  const { blocks } = (await res.json()) as { blocks: { table?: { source?: string | null } }[] };
  await request.post("/api/auth/logout");
  return blocks.some((b) => b.table?.source === "vision");
}
