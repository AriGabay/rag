import { expect, test, type Page, type Route } from "@playwright/test";
import { answerWith, message, NOW, type Json } from "./answer-fixtures";
import { assistantMessages, composer } from "./helpers";

// Clarification buttons (round 26 U5, R5, R9, R11, R13, KTD5, AE3, AE4, AE7). Everything is route-mocked, including the session, so
// the test needs no backend: it checks what the page shows of the server's clarification payload and what a click sends, not a
// model's choices. All names are invented.

const CONV = "00000000-0000-0000-0000-00000000c126";
const QUESTION = "לאיזו מהשומות של הכלנית 6 הכוונה?";
const REASON = "יש שתי שומות בשכונות שונות, והשטח שונה ביניהן.";
const OPT_A = { id: "opt-a", label: "נווה אשחר", detail: "שומה מיום 12.03.2023, עמודים 4-9" };
const OPT_B = { id: "opt-b", label: "גבעת הדקל", detail: "שומה מיום 01.07.2021, עמודים 2-7" };
const OPT_C = { id: "opt-c", label: "רמת הברושים", detail: "שומה מיום 20.11.2019, עמודים 1-5" };
const STALE_NOTE = "השאלה כבר לא פתוחה";
const FOLLOW = "שטח המגרש בשומה של נווה אשחר הוא 412 מ״ר [S1].";

interface ClarifyOpts {
  options?: { id: string; label: string; detail: string }[];
  allowAll?: boolean;
  /** The body repeats the question (the usual case: the clarifying answer's text is the question). */
  bodyHasQuestion?: boolean;
  state?: "open" | "answered" | "closed" | "stale";
  chosen?: string | null;
}

interface Mock {
  /** The bodies of the sends the page made. */
  posts: Json[];
  /** How the next send is answered; default: accepted, and the continuation runs until `finish()`. */
  onSend: (body: Json) => { status: number; json: Json; delayMs?: number } | null;
  /** The server's view of the clarification, as every list returns it. */
  server: { state: string; chosen: string | null };
  /** Lets the running continuation finish with its answer. */
  finish: () => void;
}

function clarifyAnswer(opts: ClarifyOpts): { answer: Json; clarify: Json } {
  const options = opts.options ?? [OPT_A, OPT_B];
  const markdown = opts.bodyHasQuestion === false ? "נמצאו שתי שומות לנכס הזה [S1]." : `נמצאו שתי שומות לנכס הזה.\n\n${QUESTION}`;
  const answer = answerWith(
    { status: "clarification", markdown, clarification: QUESTION, missing: null, gaps: [], components: [] },
    { completeness: { status: "full", requirements: 1, missing: [] } },
  );
  const clarify = {
    id: "cl-1", question: QUESTION, reason: REASON, kind: "unit", options, allow_all: opts.allowAll === true,
    state: opts.state ?? "open", chosen: opts.chosen ?? null,
  };
  return { answer, clarify };
}

/** Serves the session, the conversation list, one conversation holding a question and its clarifying answer, and the continuation. */
async function mockStack(page: Page, opts: ClarifyOpts = {}): Promise<Mock> {
  const { answer, clarify } = clarifyAnswer(opts);
  const server = { state: opts.state ?? "open", chosen: opts.chosen ?? null };
  const items: Json[] = [];
  const mock: Mock = {
    posts: [],
    server,
    onSend: () => null,
    finish: () => undefined,
  };
  let finished = false;
  mock.finish = () => {
    finished = true;
  };
  const follow = answerWith({ status: "answered", markdown: FOLLOW, gaps: [], components: [] },
    { completeness: { status: "full", requirements: 1, missing: [] } });
  const listed = (): Json[] => [
    message("m1", "user", "מה שטח המגרש בכלנית 6?", null),
    message("m2", "assistant", String(answer.markdown), answer, { clarify: { ...clarify, state: server.state, chosen: server.chosen } }),
    ...items,
  ];
  await page.route("**/api/**", async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();
    if (path === "/api/auth/me") {
      return route.fulfill({ json: { user: { id: "u1", email: "dana@demo.test", full_name: "דנה לוי", role: "member", can_upload: true },
        office: { id: "o1", name: "משרד לדוגמה" }, groups: [], demo_mode: false } });
    }
    if (path === "/api/chat/conversations") {
      return route.fulfill({ json: { conversations: [{ id: CONV, title: "שטח המגרש", updated_at: NOW, created_at: NOW, archived: false,
        engine: "rag", prefer_recent: true }], next: null } });
    }
    if (path === `/api/chat/conversations/${CONV}/messages`) {
      if (method === "POST") {
        const body = route.request().postDataJSON() as Json;
        mock.posts.push(body);
        const custom = mock.onSend(body);
        if (custom) {
          if (custom.delayMs) await new Promise((r) => setTimeout(r, custom.delayMs));
          return route.fulfill({ status: custom.status, json: custom.json });
        }
        const user = message("m3", "user", String(body.content), null, { client_id: body.client_id });
        const running = message("m4", "assistant", "", null, { status: "running", reply_to: "m3",
          progress: [{ step: "agent", label: "ממשיך את הבקשה המקורית" }] });
        items.push(user, running);
        const clar = body.clarification as { option_id: string } | undefined;
        if (clar) {
          server.state = "answered";
          server.chosen = clar.option_id;
        } else {
          server.state = "closed";
        }
        return route.fulfill({ json: { user, assistant: running } });
      }
      return route.fulfill({ json: { conversation: { id: CONV, title: "שטח המגרש", updated_at: NOW, created_at: NOW, archived: false,
        engine: "rag" }, has_more: false, messages: items.length && finished
          ? listed().map((m) => (m.id === "m4" ? message("m4", "assistant", FOLLOW, follow, { reply_to: "m3" }) : m))
          : listed() } });
    }
    if (path === `/api/chat/conversations/${CONV}/reports`) return route.fulfill({ json: { reports: [] } });
    if (path === "/api/chat/messages/m4") {
      return route.fulfill({ json: finished
        ? message("m4", "assistant", FOLLOW, follow, { reply_to: "m3" })
        : message("m4", "assistant", "", null, { status: "running", reply_to: "m3", progress: [{ step: "agent", label: "ממשיך את הבקשה המקורית" }] }) });
    }
    return route.fulfill({ status: 404, json: { detail: "לא נמצא" } });
  });
  return mock;
}

async function open(page: Page, opts: ClarifyOpts = {}): Promise<Mock> {
  const mock = await mockStack(page, opts);
  await page.goto(`/chat?c=${CONV}`);
  await expect(assistantMessages(page).first()).toBeVisible();
  return mock;
}

const group = (page: Page) => page.getByRole("group", { name: QUESTION });
const optionButtons = (page: Page) => group(page).getByRole("button");

test.describe("clarification buttons", () => {
  test("the question and reason show once, the options are buttons in order, and Tab reaches them", async ({ page }) => {
    await open(page);
    const reply = assistantMessages(page).first();
    const block = reply.getByTestId("clarify");
    await expect(block).toHaveAttribute("data-state", "open");
    await expect(reply.getByTestId("clarification-lead")).toHaveCount(0);
    await expect(optionButtons(page)).toHaveCount(2);
    await expect(optionButtons(page).nth(0)).toContainText("נווה אשחר");
    await expect(optionButtons(page).nth(0)).toContainText("12.03.2023");
    await expect(optionButtons(page).nth(1)).toContainText("גבעת הדקל");
    // the number hints are 1..n
    await expect(block.locator(".clarify-num")).toHaveText(["1", "2"]);
    // the question is on screen once (the body already says it, so the block does not repeat it) and so is the reason
    expect((await reply.innerText()).split(QUESTION).length - 1).toBe(1);
    expect((await reply.innerText()).split(REASON).length - 1).toBe(1);
    await expect(group(page)).toHaveAttribute("aria-label", QUESTION);
    // Tab from the question's own message reaches the first option, then the second
    await page.locator(".msg-user").first().focus();
    await page.keyboard.press("Tab");
    await expect(optionButtons(page).nth(0)).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(optionButtons(page).nth(1)).toBeFocused();
    // RTL layout
    await expect(block).toHaveAttribute("dir", "rtl");
  });

  test("a body that does not say the question gets it in the block, once", async ({ page }) => {
    await open(page, { bodyHasQuestion: false });
    const reply = assistantMessages(page).first();
    await expect(reply.getByTestId("clarify-question")).toHaveText(QUESTION);
    expect((await reply.innerText()).split(QUESTION).length - 1).toBe(1);
  });

  test("Enter on an option sends its label and the ids, marks the clarification answered at once, and focus lands in the composer", async ({ page }) => {
    const mock = await open(page);
    await page.locator(".msg-user").first().focus();
    await page.keyboard.press("Tab");
    await expect(optionButtons(page).nth(0)).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(composer(page)).toBeFocused();
    await expect(composer(page)).toBeEnabled();
    await expect.poll(() => mock.posts.length).toBe(1);
    expect(mock.posts[0]).toMatchObject({ content: "נווה אשחר", clarification: { id: "cl-1", option_id: "opt-a" } });
    // answered: the chosen option is marked, the other dimmed, and neither can be clicked while the continuation runs
    const block = assistantMessages(page).first().getByTestId("clarify");
    await expect(block).toHaveAttribute("data-state", "answered");
    await expect(optionButtons(page).nth(0)).toHaveAttribute("data-chosen", "true");
    await expect(optionButtons(page).nth(1)).toHaveAttribute("data-dimmed", "true");
    await expect(optionButtons(page).nth(0)).toBeDisabled();
    await expect(optionButtons(page).nth(1)).toBeDisabled();
    await optionButtons(page).nth(1).click({ force: true });
    // the choice is in the conversation, and the progress names the original request continuing
    await expect(page.locator(".msg-user .bubble").last()).toHaveText("נווה אשחר");
    await expect(assistantMessages(page).last().locator(".status-line")).toContainText("ממשיך את הבקשה המקורית");
    expect(mock.posts).toHaveLength(1);
    // the continuation lands; the composer stayed available throughout and the group stays answered
    mock.finish();
    await expect(assistantMessages(page).last().locator(".body")).toContainText("412", { timeout: 15_000 });
    await expect(block).toHaveAttribute("data-state", "answered");
    await expect(composer(page)).toBeEnabled();
    expect(mock.posts).toHaveLength(1);
  });

  test("a double click on an option sends once", async ({ page }) => {
    const mock = await open(page);
    mock.onSend = () => null;
    await optionButtons(page).nth(1).dblclick();
    await expect.poll(() => mock.posts.length).toBe(1);
    await page.waitForTimeout(400);
    expect(mock.posts).toHaveLength(1);
    expect(mock.posts[0]).toMatchObject({ content: "גבעת הדקל", clarification: { id: "cl-1", option_id: "opt-b" } });
  });

  test("a slow send cannot be doubled by another click either", async ({ page }) => {
    const mock = await open(page);
    mock.onSend = () => ({ status: 200, delayMs: 600, json: { user: message("m3", "user", "נווה אשחר", null, { client_id: "x" }),
      assistant: message("m4", "assistant", "", null, { status: "running", reply_to: "m3", progress: [] }) } });
    await optionButtons(page).nth(0).click();
    await optionButtons(page).nth(0).click({ force: true });
    await optionButtons(page).nth(1).click({ force: true });
    await page.waitForTimeout(900);
    expect(mock.posts).toHaveLength(1);
  });

  test("the extra \"כולן\" button exists only with allow_all, and sends the reserved id", async ({ page }) => {
    const without = await open(page);
    await expect(optionButtons(page)).toHaveCount(2);
    await expect(group(page).getByRole("button", { name: "כולן" })).toHaveCount(0);
    expect(without.posts).toHaveLength(0);
    await page.unrouteAll({ behavior: "ignoreErrors" });
    const mock = await open(page, { allowAll: true, options: [OPT_A, OPT_B, OPT_C] });
    await expect(optionButtons(page)).toHaveCount(4);
    const all = group(page).getByRole("button", { name: "כולן" });
    await expect(all).toBeVisible();
    await all.click();
    await expect.poll(() => mock.posts.length).toBe(1);
    expect(mock.posts[0]).toMatchObject({ content: "כולן", clarification: { id: "cl-1", option_id: "all" } });
    await expect(all).toHaveAttribute("data-chosen", "true");
  });

  test("a closed or stale clarification shows every option disabled with a short label; an answered one marks the chosen option after a refresh", async ({ page }) => {
    for (const state of ["closed", "stale"] as const) {
      await page.unrouteAll({ behavior: "ignoreErrors" });
      const mock = await open(page, { state });
      await expect(assistantMessages(page).first().getByTestId("clarify")).toHaveAttribute("data-state", state);
      await expect(optionButtons(page)).toHaveCount(2);
      await expect(optionButtons(page).nth(0)).toBeDisabled();
      await expect(optionButtons(page).nth(1)).toBeDisabled();
      await expect(assistantMessages(page).first().getByTestId("clarify-state")).toHaveText("לא פעילה");
      await optionButtons(page).nth(0).click({ force: true });
      await page.waitForTimeout(150);
      expect(mock.posts).toHaveLength(0);
      // the message box stays available
      await expect(composer(page)).toBeEnabled();
    }
    await page.unrouteAll({ behavior: "ignoreErrors" });
    await open(page, { state: "answered", chosen: "opt-b" });
    await expect(optionButtons(page).nth(1)).toHaveAttribute("data-chosen", "true");
    await expect(optionButtons(page).nth(0)).toHaveAttribute("data-dimmed", "true");
    await expect(optionButtons(page).nth(0)).toBeDisabled();
    await expect(assistantMessages(page).first().getByTestId("clarify-state")).toHaveText("נענתה");
  });

  test("a refresh after a click restores the answered state from the server", async ({ page }) => {
    const mock = await open(page);
    await optionButtons(page).nth(0).click();
    await expect.poll(() => mock.posts.length).toBe(1);
    mock.finish();
    await expect(assistantMessages(page).last().locator(".body")).toContainText("412", { timeout: 15_000 });
    await page.reload();
    await expect(assistantMessages(page).first()).toBeVisible();
    await expect(assistantMessages(page).first().getByTestId("clarify")).toHaveAttribute("data-state", "answered");
    await expect(optionButtons(page).nth(0)).toHaveAttribute("data-chosen", "true");
    await expect(optionButtons(page).nth(1)).toHaveAttribute("data-dimmed", "true");
    await expect(optionButtons(page).nth(1)).toBeDisabled();
  });

  test("a typed reply that is not an answer closes the buttons (AE4) and they do nothing", async ({ page }) => {
    const mock = await open(page);
    await composer(page).fill("מהו שיעור ההיוון המקובל?");
    await composer(page).press("Enter");
    await expect.poll(() => mock.posts.length).toBe(1);
    expect(mock.posts[0]).not.toHaveProperty("clarification");
    await expect(assistantMessages(page).first().getByTestId("clarify")).toHaveAttribute("data-state", "closed");
    await expect(optionButtons(page).nth(0)).toBeDisabled();
    mock.finish();
    await expect(assistantMessages(page).last().locator(".body")).toContainText("412", { timeout: 15_000 });
    await expect(assistantMessages(page).first().getByTestId("clarify")).toHaveAttribute("data-state", "closed");
    await optionButtons(page).nth(1).click({ force: true });
    await page.waitForTimeout(150);
    expect(mock.posts).toHaveLength(1);
  });

  test("a 409 clarification_stale removes the bubble, shows the message beside the buttons, disables them and offers no resend", async ({ page }) => {
    const mock = await open(page);
    mock.onSend = () => ({ status: 409, json: { detail: { code: "clarification_stale", message: STALE_NOTE } } });
    await optionButtons(page).nth(0).click();
    await expect.poll(() => mock.posts.length).toBe(1);
    const block = assistantMessages(page).first().getByTestId("clarify");
    await expect(block.getByTestId("clarify-note")).toHaveText(STALE_NOTE);
    await expect(block).toHaveAttribute("data-state", "stale");
    await expect(optionButtons(page).nth(0)).toBeDisabled();
    await expect(optionButtons(page).nth(1)).toBeDisabled();
    // neither the optimistic bubble nor its "resend as text" link remains, and no running reply is left behind
    await expect(page.locator(".msg-user .bubble", { hasText: "נווה אשחר" })).toHaveCount(0);
    await expect(page.locator(".msg-user")).toHaveCount(1);
    await expect(page.getByText("שליחה חוזרת")).toHaveCount(0);
    await expect(assistantMessages(page)).toHaveCount(1);
    await expect(composer(page)).toBeEnabled();
  });

  test("another failure of a click (clarification_unavailable) shows its message with no bubble and no resend, and the buttons are usable again", async ({ page }) => {
    const mock = await open(page);
    const unavailable = "אי אפשר להחיל בחירה כרגע";
    mock.onSend = () => ({ status: 409, json: { detail: { code: "clarification_unavailable", message: unavailable } } });
    await optionButtons(page).nth(0).click();
    await expect.poll(() => mock.posts.length).toBe(1);
    const block = assistantMessages(page).first().getByTestId("clarify");
    await expect(block.getByTestId("clarify-note")).toHaveText(unavailable);
    await expect(block).toHaveAttribute("data-state", "open");
    await expect(optionButtons(page).nth(0)).toBeEnabled();
    await expect(page.locator(".msg-user")).toHaveCount(1);
    await expect(page.getByText("שליחה חוזרת")).toHaveCount(0);
  });

  test("control: a typed message that fails still offers the resend link", async ({ page }) => {
    const mock = await open(page);
    mock.onSend = () => ({ status: 500, json: { detail: "boom" } });
    await composer(page).fill("הודעה שנכשלה");
    await composer(page).press("Enter");
    await expect.poll(() => mock.posts.length).toBe(1);
    await expect(page.getByText("שליחה חוזרת")).toBeVisible();
    // and the clarification is untouched by it
    await expect(assistantMessages(page).first().getByTestId("clarify")).toHaveAttribute("data-state", "open");
  });

  test("a clarification with no options keeps the old lead line and shows no buttons", async ({ page }) => {
    await open(page, { options: [] });
    const reply = assistantMessages(page).first();
    await expect(reply.getByTestId("clarification-lead")).toContainText("נדרשת הבהרה");
    await expect(reply.getByTestId("clarify")).toHaveCount(0);
    await expect(reply.getByRole("button", { name: /נווה אשחר/ })).toHaveCount(0);
    // control: with options the lead line is replaced
    await page.unrouteAll({ behavior: "ignoreErrors" });
    await open(page);
    await expect(assistantMessages(page).first().getByTestId("clarification-lead")).toHaveCount(0);
  });

  test("at 375px wide nothing overflows horizontally, even with long labels and a fourth button", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 800 });
    const long = { id: "opt-long", label: "פרוייקט מגורים בשכונת הרקפות הצפונית ליד גן הילדים הגדול",
      detail: "שומה מיום 30.09.2022, עמודים 12-31, כתובת: רחוב הכלנית 6 קומה 3 דירה 12 ב" };
    await open(page, { options: [OPT_A, OPT_B, long], allowAll: true });
    const block = assistantMessages(page).first().getByTestId("clarify");
    await expect(optionButtons(page)).toHaveCount(4);
    const metrics = await page.evaluate(() => {
      const root = document.documentElement;
      const thread = document.querySelector(".chat-thread") as HTMLElement;
      const clarify = document.querySelector("[data-testid=clarify]") as HTMLElement;
      const rects = Array.from(clarify.querySelectorAll("button")).map((b) => b.getBoundingClientRect());
      return {
        pageOverflow: root.scrollWidth - root.clientWidth,
        threadOverflow: thread.scrollWidth - thread.clientWidth,
        blockOverflow: clarify.scrollWidth - clarify.clientWidth,
        maxRight: Math.max(...rects.map((r) => r.right)),
        minLeft: Math.min(...rects.map((r) => r.left)),
        width: window.innerWidth,
      };
    });
    expect(metrics.pageOverflow).toBeLessThanOrEqual(0);
    expect(metrics.threadOverflow).toBeLessThanOrEqual(0);
    expect(metrics.blockOverflow).toBeLessThanOrEqual(0);
    expect(metrics.minLeft).toBeGreaterThanOrEqual(0);
    expect(metrics.maxRight).toBeLessThanOrEqual(metrics.width);
    await expect(block).toBeVisible();
  });
});
