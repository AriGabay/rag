import { expect, test, type Page, type Route } from "@playwright/test";
import { answerCards, login, USERS } from "./helpers";
import type { Answer, AskResponse, Clarification, ConversationDetail, ConversationListItem } from "../lib/types";

// Async races of the chat page, against a mocked conversation API (only the login is real), so each race is
// forced deterministically: a turn or a conversation load is held open until the test releases it.

const CONV_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
const CONV_B = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const TURN_IN_PROGRESS = "השאלה עדיין בעיבוד";

const CLARIFICATION: Clarification = {
  key: "data_kind",
  question: "לאיזה סוג נתון התכוונתם?",
  options: [
    { value: "transaction", label: "עסקאות" },
    { value: "appraisal", label: "שומות" },
  ],
};

function contentAnswer(text: string): Answer {
  return { kind: "content", text, provider: "extractive", demo: false, sources: [], limitations: [], claims: [] };
}

function clarificationAnswer(): Answer {
  return { ...contentAnswer(CLARIFICATION.question), kind: "clarification", clarification: CLARIFICATION };
}

function detail(id: string, over: Partial<ConversationDetail> = {}): ConversationDetail {
  return {
    id,
    title: id === CONV_A ? "שיחה א" : "שיחה ב",
    confirmed_conditions: null,
    pending_clarification: null,
    context: null,
    messages: [],
    ...over,
  };
}

const LIST: ConversationListItem[] = [
  { id: CONV_A, title: "שיחה א", updated_at: "2026-10-01T10:00:00+03:00" },
  { id: CONV_B, title: "שיחה ב", updated_at: "2026-10-01T09:00:00+03:00" },
];

/** A promise the test resolves to let a held request through. */
function gate(): { wait: Promise<void>; open: () => void } {
  let open = () => {};
  const wait = new Promise<void>((resolve) => {
    open = resolve;
  });
  return { wait, open };
}

const path = (url: string) => new URL(url).pathname;

/** Mocks the conversation list and `/api/conversations/{id}` with `onDetail` (default: an empty conversation). */
async function mockConversations(page: Page, onDetail?: (route: Route, id: string) => Promise<void>): Promise<void> {
  await page.route(
    (url) => path(url.toString()) === "/api/conversations",
    async (route) => {
      if (route.request().method() === "POST") await route.fulfill({ json: { id: CONV_B } });
      else await route.fulfill({ json: { conversations: LIST } });
    },
  );
  await page.route(
    (url) => path(url.toString()).startsWith("/api/conversations/"),
    async (route) => {
      const id = path(route.request().url()).split("/").pop() ?? "";
      if (onDetail) await onDetail(route, id);
      else await route.fulfill({ json: detail(id) });
    },
  );
}

async function openChat(page: Page): Promise<void> {
  await login(page, USERS.adminA);
  await page.evaluate(() => window.sessionStorage.clear());
  await page.goto("/chat");
  await expect(page.getByRole("heading", { name: "שאלות על מאגר המשרד" })).toBeVisible();
}

test.describe("Chat async races", () => {
  test("a draft typed while a turn runs is kept and keeps the focus", async ({ page }) => {
    const held = gate();
    await mockConversations(page);
    await page.route(
      (url) => path(url.toString()) === "/api/ask",
      async (route) => {
        await held.wait;
        const res: AskResponse = { conversation_id: CONV_A, question_id: "q-1", answer: contentAnswer("תשובה ראשונה") };
        await route.fulfill({ json: res });
      },
    );
    await openChat(page);
    const box = page.getByRole("textbox", { name: "שאלה" });
    await box.fill("שאלה ראשונה");
    await page.getByRole("button", { name: "שליחה", exact: true }).click();
    await expect(page.getByRole("button", { name: "חושב..." })).toBeDisabled();

    await box.fill("טיוטה לשאלה הבאה");
    held.open();
    await expect(answerCards(page)).toHaveCount(1);
    await expect(box).toHaveValue("טיוטה לשאלה הבאה");
    await expect(box).toBeFocused();
  });

  test("the sidebar and the composer are locked while a turn runs and while a conversation opens", async ({ page }) => {
    const turn = gate();
    const load = gate();
    await mockConversations(page, async (route, id) => {
      if (id === CONV_A) await load.wait;
      const messages =
        id === CONV_A
          ? [{ question_id: "a-1", question: "שאלה בשיחה א", answer: contentAnswer("תשובה בשיחה א"), stale: false, hidden: false, created_at: "2026-10-01T10:00:00+03:00" }]
          : [];
      await route.fulfill({ json: detail(id, { messages }) });
    });
    await page.route(
      (url) => path(url.toString()) === "/api/ask",
      async (route) => {
        await turn.wait;
        await route.fulfill({ json: { conversation_id: CONV_B, question_id: "b-1", answer: contentAnswer("תשובה") } });
      },
    );
    await openChat(page);
    const sidebar = page.getByRole("complementary", { name: "שיחות" });

    await page.getByRole("textbox", { name: "שאלה" }).fill("שאלה");
    await page.getByRole("button", { name: "שליחה", exact: true }).click();
    await expect(sidebar.getByRole("button", { name: "שיחה חדשה" })).toBeDisabled();
    await expect(sidebar.getByRole("button", { name: /שיחה א/ })).toBeDisabled();
    turn.open();
    await expect(answerCards(page)).toHaveCount(1);

    await sidebar.getByRole("button", { name: /שיחה א/ }).click();
    await expect(page.getByText("טוען שיחה...")).toBeVisible();
    await expect(sidebar.getByRole("button", { name: /שיחה ב/ })).toBeDisabled();
    await expect(sidebar.getByRole("button", { name: "שיחה חדשה" })).toBeDisabled();
    await page.getByRole("textbox", { name: "שאלה" }).fill("שאלה בזמן טעינה");
    await expect(page.getByRole("button", { name: "שליחה", exact: true })).toBeDisabled();
    load.open();
    await expect(page.locator(".bubble-q", { hasText: "שאלה בשיחה א" })).toBeVisible();
    await expect(sidebar.getByRole("button", { name: /שיחה ב/ })).toBeEnabled();
  });

  test("when the context cannot be reloaded after a turn, the stale clarification is disabled until it reloads", async ({
    page,
  }) => {
    let loads = 0;
    await mockConversations(page, async (route, id) => {
      loads += 1;
      if (loads === 2) {
        await route.fulfill({ status: 500, json: { detail: "boom" } });
        return;
      }
      const first = {
        question_id: "a-1",
        question: "מה המחיר?",
        answer: clarificationAnswer(),
        stale: false,
        hidden: false,
        created_at: "2026-10-01T10:00:00+03:00",
      };
      await route.fulfill({
        json: detail(id, { messages: [first], pending_clarification: loads === 1 ? CLARIFICATION : null }),
      });
    });
    await page.route(
      (url) => path(url.toString()) === "/api/ask",
      async (route) => {
        const res: AskResponse = { conversation_id: CONV_A, question_id: "a-2", answer: contentAnswer("תשובה לעסקאות") };
        await route.fulfill({ json: res });
      },
    );
    await openChat(page);
    await page.getByRole("complementary", { name: "שיחות" }).getByRole("button", { name: /שיחה א/ }).click();
    await expect(page.getByRole("group", { name: "אפשרויות הבהרה" }).getByRole("button", { name: "עסקאות" })).toBeEnabled();

    // Free text resolves the clarification on the server, but the context reload after the turn fails.
    await page.getByRole("textbox", { name: "שאלה" }).fill("התכוונתי לעסקאות");
    await page.getByRole("button", { name: "שליחה", exact: true }).click();
    await expect(answerCards(page)).toHaveCount(2);
    const alert = page.getByRole("alert").filter({ hasText: "מצב השיחה לא נטען מחדש" });
    await expect(alert).toBeVisible();
    const pinned = page.getByRole("region", { name: "שאלת הבהרה פתוחה" });
    await expect(pinned.getByRole("button", { name: "עסקאות" })).toBeDisabled();
    await expect(pinned.getByRole("button", { name: "שומות" })).toBeDisabled();

    await alert.getByRole("button", { name: "נסו שוב" }).click();
    await expect(alert).toHaveCount(0);
    await expect(pinned).toHaveCount(0);
  });

  test("leaving the chat page stops polling a turn still in progress", async ({ page }) => {
    let posts = 0;
    await mockConversations(page);
    await page.route(
      (url) => path(url.toString()) === "/api/ask",
      async (route) => {
        posts += 1;
        await route.fulfill({ status: 409, json: { detail: TURN_IN_PROGRESS } });
      },
    );
    await openChat(page);
    await page.getByRole("textbox", { name: "שאלה" }).fill("שאלה ארוכה");
    await page.getByRole("button", { name: "שליחה", exact: true }).click();
    await expect.poll(() => posts).toBeGreaterThanOrEqual(2);

    await page.getByRole("navigation", { name: "ניווט ראשי" }).getByRole("link", { name: "מסמכים" }).click();
    await expect(page).toHaveURL(/\/documents/);
    const after = posts;
    await page.waitForTimeout(5_000); // more than two poll intervals
    expect(posts).toBe(after);
  });
});
