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
  await expect(page.getByRole("navigation", { name: "ניווט ראשי" })).toBeVisible();
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

export function answerCards(page: Page): Locator {
  return page.getByRole("article", { name: "תשובה" });
}

export async function ask(page: Page, question: string): Promise<void> {
  const before = await answerCards(page).count();
  const box = page.getByRole("textbox", { name: "שאלה" });
  await box.fill(question);
  await page.getByRole("button", { name: "שליחה" }).click();
  await expect(answerCards(page)).toHaveCount(before + 1);
}

/** Chooses an option of the active clarification and waits for the next answer card. */
export async function chooseOption(page: Page, label: string | RegExp): Promise<void> {
  const before = await answerCards(page).count();
  await page.getByRole("group", { name: "אפשרויות הבהרה" }).getByRole("button", { name: label }).click();
  await expect(answerCards(page)).toHaveCount(before + 1);
}

/**
 * Answers pending clarifications by clicking the first option (or a preferred one when offered) until the last
 * answer is not a clarification. Returns the labels of the clarification questions that were answered.
 */
export async function resolveClarifications(page: Page, preferred: RegExp[] = [], max = 6): Promise<string[]> {
  const asked: string[] = [];
  for (let i = 0; i < max; i++) {
    const group = page.getByRole("group", { name: "אפשרויות הבהרה" });
    if ((await group.count()) === 0) return asked;
    const card = answerCards(page).last();
    asked.push((await card.locator("strong").first().innerText()).trim());
    const buttons = group.getByRole("button");
    let target = buttons.first();
    for (const re of preferred) {
      const hit = group.getByRole("button", { name: re });
      if ((await hit.count()) > 0) {
        target = hit.first();
        break;
      }
    }
    const before = await answerCards(page).count();
    await target.click();
    await expect(answerCards(page)).toHaveCount(before + 1);
  }
  throw new Error(`still asking for clarification after ${max} choices: ${asked.join(" | ")}`);
}

/** Reads the "תנאים:" line of a numeric answer card as label → value. */
export async function conditionsOf(card: Locator): Promise<Record<string, string>> {
  const line = card.locator("div", { has: card.page().locator("strong", { hasText: "תנאים:" }) }).last();
  const text = (await line.innerText()).replace(/^\s*תנאים:\s*/, "");
  const out: Record<string, string> = {};
  for (const part of text.split(" · ")) {
    const idx = part.indexOf(":");
    if (idx > 0) out[part.slice(0, idx).trim()] = part.slice(idx + 1).trim();
  }
  return out;
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
