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

/** Uploads a fixture to office B (as its admin) unless a live copy exists, and waits until it is processed. */
export async function ensureOfficeBDocument(request: APIRequestContext, file: string, title: string): Promise<string> {
  await apiLogin(request, USERS.adminB);
  const list = async () =>
    ((await (await request.get(`/api/documents?q=${encodeURIComponent(title)}`)).json()) as {
      documents: (DocSummary & { current_version: { status: string } | null })[];
    }).documents.filter((d) => d.title === title && !d.deleted);
  let docs = await list();
  if (docs.length === 0) {
    const fs = await import("node:fs");
    const groups = (await (await request.get("/api/admin/groups")).json()) as { groups: { id: string }[] };
    const res = await request.post("/api/documents", {
      multipart: {
        files: { name: path.basename(file), mimeType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document", buffer: fs.readFileSync(file) },
        group_id: groups.groups[0].id,
      },
    });
    expect(res.ok(), `upload ${title}: ${res.status()}`).toBeTruthy();
  }
  await expect
    .poll(async () => (await list())[0]?.current_version?.status ?? "none", { timeout: 900_000, intervals: [3000] })
    .toMatch(/^(ready|needs_review)$/);
  docs = await list();
  await request.post("/api/auth/logout");
  return docs[0].id;
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
