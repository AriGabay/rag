import { expect, test, type Locator, type Page } from "@playwright/test";
import { ANSWER_MODE_LABEL } from "../lib/format";
import type { AskResponse, ConversationContext, ConversationDetail, ProviderMode } from "../lib/types";
import { answerCards, apiLogin, login, shot, USERS } from "./helpers";

// U10: general conversations in office A (dana; the held-out group of the synthetic corpus). Read-only: only
// questions are asked. The office may run in cloud or limited mode, so each test reads the mode from the answer
// (or from the admin settings) and asserts what that mode promises.

const SAFE_ROOM = "מה גודל ממ״ד ממוצע ברמת גן?";
const PLACE_FOLLOW_UP = "ומה לגבי גבעתיים?";
const WHY = "למה?";
const TOPIC_SWITCH = "עכשיו בנושא אחר: אילו שומות מזכירות היתר בנייה?";
const PERMITS = "אילו שומות מזכירות היתר בנייה?";
const PRICE = "מה מחיר למ״ר ברמת גן בשכונת חרוזים בשנת 2024?";
const DATA_KIND_QUESTION = "לאיזה נתון הכוונה?";
const UNKNOWN_PLACE = "מה מחיר למ״ר ברמת גן בשכונת נווה צדק בשנת 2024?";
// A turn may read documents for up to TURN_DEADLINE_SECONDS (45 s) before it answers.
const TURN_TIMEOUT = 150_000;

function isAsk(url: string): boolean {
  return new URL(url).pathname === "/api/ask";
}

/** Waits until no turn is running (the submit button shows "שליחה" again), so the context strip is synced. */
async function idle(page: Page): Promise<void> {
  await expect(page.getByRole("button", { name: "שליחה", exact: true })).toBeVisible({ timeout: TURN_TIMEOUT });
}

/** Asks through the composer and returns the server's response for the turn. */
async function askTurn(page: Page, question: string): Promise<AskResponse> {
  const before = await answerCards(page).count();
  const response = page.waitForResponse(
    (r) => isAsk(r.url()) && r.request().method() === "POST" && r.status() === 200,
    { timeout: TURN_TIMEOUT },
  );
  await page.getByRole("textbox", { name: "שאלה" }).fill(question);
  await page.getByRole("button", { name: "שליחה", exact: true }).click();
  const res = (await (await response).json()) as AskResponse;
  await expect(answerCards(page)).toHaveCount(before + 1, { timeout: TURN_TIMEOUT });
  await idle(page);
  return res;
}

/** Clicks an option of the one active clarification group and returns the response. */
async function choose(page: Page, label: string | RegExp): Promise<AskResponse> {
  const before = await answerCards(page).count();
  const response = page.waitForResponse((r) => isAsk(r.url()) && r.status() === 200, { timeout: TURN_TIMEOUT });
  await page.getByRole("group", { name: "אפשרויות הבהרה" }).getByRole("button", { name: label }).click();
  const res = (await (await response).json()) as AskResponse;
  await expect(answerCards(page)).toHaveCount(before + 1, { timeout: TURN_TIMEOUT });
  await idle(page);
  return res;
}

async function conversation(page: Page, id: string): Promise<ConversationDetail> {
  const res = await page.request.get(`/api/conversations/${id}`);
  expect(res.ok(), `GET conversation: ${res.status()}`).toBeTruthy();
  return (await res.json()) as ConversationDetail;
}

async function contextOf(page: Page, id: string): Promise<ConversationContext> {
  const ctx = (await conversation(page, id)).context;
  expect(ctx, "the conversation carries its context").toBeTruthy();
  return ctx!;
}

function strip(page: Page): Locator {
  return page.getByRole("region", { name: "הקשר השיחה" });
}

/** The strip shows exactly the server's chips (or is absent when there are none). */
async function expectStripMatches(page: Page, ctx: ConversationContext): Promise<void> {
  if (ctx.chips.length === 0) {
    await expect(strip(page)).toHaveCount(0);
    return;
  }
  await expect(strip(page).getByRole("listitem")).toHaveCount(ctx.chips.length);
  for (const c of ctx.chips) await expect(strip(page)).toContainText(`${c.label}: ${c.value}`);
}

async function expectModeBadge(card: Locator, mode: ProviderMode | undefined): Promise<void> {
  expect(mode, "every answer states its mode").toBeTruthy();
  await expect(card.locator(`[data-mode="${mode}"]`)).toHaveText(ANSWER_MODE_LABEL[mode!]);
}

async function expectNoHorizontalScroll(page: Page): Promise<void> {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow, "no horizontal scroll").toBeLessThanOrEqual(0);
}

test.describe("general conversations (office A, read-only)", () => {
  test.beforeEach(async ({ page }) => {
    await login(page, USERS.dana);
    await page.getByRole("button", { name: "שיחה חדשה", exact: true }).click();
    await expect(answerCards(page)).toHaveCount(0);
  });

  test("full conversation: ממ״ד → another place → למה? → new topic clears → reload keeps the state", async ({ page }) => {
    test.setTimeout(600_000);

    // 1. A new attribute nobody anticipated: never a price clarification.
    const first = await askTurn(page, SAFE_ROOM);
    const id = first.conversation_id;
    const card1 = answerCards(page).last();
    const mode = first.answer.mode;
    expect(first.answer.kind).not.toBe("clarification");
    await expect(card1).not.toContainText(DATA_KIND_QUESTION);
    await expect(page.getByRole("group", { name: "אפשרויות הבהרה" })).toHaveCount(0);
    await expectModeBadge(card1, mode);
    if (mode === "cloud") {
      // A computation over the documents in scope, with its coverage and method in the details.
      expect(["numeric", "abstain"]).toContain(first.answer.kind);
      expect(first.answer.coverage?.facts?.in_scope ?? 0).toBeGreaterThan(0);
      if (first.answer.kind === "abstain") expect(first.answer.abstention_kind).toBe("not_extracted_or_verified");
      else await expect(card1.getByText(/מבוסס על \d+ תצפיות/).first()).toBeVisible();
      const details = card1.locator("details");
      await expect(details).not.toHaveAttribute("open", /.*/);
      await details.getByText("פרטי השיטה והכיסוי").click();
      await expect(details).toContainText("מסמכים בתחום");
      await expect(details).toContainText("שיטה");
    } else if (mode === "limited" || mode === "error") {
      // Passages and the limitation, no number.
      expect(first.answer.numeric ?? null).toBeNull();
      expect(first.answer.sources.length).toBeGreaterThan(0);
      await expect(card1.getByRole("region", { name: "מקורות" })).toBeVisible();
      await expect(card1.locator(".figures")).toHaveCount(0);
    }
    const ctx1 = await contextOf(page, id);
    expect(ctx1.city).toBe("רמת גן");
    await expectStripMatches(page, ctx1);

    // 2. A follow-up changes only the place.
    const second = await askTurn(page, PLACE_FOLLOW_UP);
    expect(second.conversation_id).toBe(id);
    expect(second.answer.kind).not.toBe("clarification");
    const ctx2 = await contextOf(page, id);
    expect(ctx2.city).toBe("גבעתיים");
    const withoutPlace = (c: ConversationContext) => ({ ...c, city: null, neighborhood: null, chips: null });
    expect(withoutPlace(ctx2)).toEqual(withoutPlace(ctx1));
    await expectStripMatches(page, ctx2);
    await expect(strip(page)).not.toContainText("רמת גן");

    // 3. "למה?" answers from the previous answer's method and sources.
    const why = await askTurn(page, WHY);
    expect(why.answer.meta).toBe("explain_previous");
    const card3 = answerCards(page).last();
    await expect(card3).toContainText("כך התקבלה התשובה");
    if (why.answer.sources.length > 0) await expect(card3.getByRole("region", { name: "מקורות" })).toBeVisible();
    expect(await contextOf(page, id)).toEqual(ctx2);

    // 4. A new topic clears the conditions that no longer apply, and the answer says what it cleared.
    const topic = await askTurn(page, TOPIC_SWITCH);
    const card4 = answerCards(page).last();
    expect(topic.answer.sources.length).toBeGreaterThan(0);
    await expect(card4.getByRole("region", { name: "מקורות" })).toBeVisible();
    const ctx4 = await contextOf(page, id);
    expect(ctx4.city).toBeNull();
    if (ctx2.attribute) expect(ctx4.attribute).not.toBe(ctx2.attribute);
    expect((topic.answer.cleared ?? []).map((c) => c.key)).toContain("city");
    await expect(card4.getByText(/^נוקו מההקשר:/)).toContainText("גבעתיים");
    await expectStripMatches(page, ctx4);
    await page.screenshot({ path: shot("general-conversation.png"), fullPage: true });

    // 5. Reload: the same conversation, messages and context come back from the server.
    const cards = await answerCards(page).count();
    await page.reload();
    await expect(answerCards(page)).toHaveCount(cards);
    await expect(
      page.getByRole("complementary", { name: "שיחות" }).locator('button[aria-current="true"]'),
    ).toBeVisible();
    const ctx5 = await contextOf(page, id);
    expect(ctx5).toEqual(ctx4);
    await expectStripMatches(page, ctx5);
  });

  test("a clarification answered in free text resolves", async ({ page }) => {
    test.setTimeout(300_000);
    const asked = await askTurn(page, PRICE);
    test.skip(asked.answer.kind !== "clarification", "the price question was answered without a clarification");
    await expect(answerCards(page).last()).toContainText(DATA_KIND_QUESTION);
    await expect(page.getByText("ממתינה שאלת הבהרה.")).toBeVisible();

    const reply = await askTurn(page, "התכוונתי לעסקאות");
    expect(reply.answer.interpretation_note ?? "").toContain("כמענה לשאלת ההבהרה");
    const card = answerCards(page).last();
    await expect(card.getByText(/כמענה לשאלת ההבהרה/)).toContainText("מחירי עסקאות");
    await expect(card).not.toContainText(DATA_KIND_QUESTION);
    await expect(answerCards(page).nth(0).getByText("שאלת ההבהרה כבר טופלה.")).toBeVisible();
    expect((await contextOf(page, asked.conversation_id)).data_kind).toBe("transaction_price");
  });

  test("an unrelated question keeps the clarification pinned; its button still answers it", async ({ page }) => {
    test.setTimeout(300_000);
    const asked = await askTurn(page, PRICE);
    test.skip(asked.answer.kind !== "clarification", "the price question was answered without a clarification");
    const question = asked.answer.clarification!.question;

    const other = await askTurn(page, PERMITS);
    expect(other.answer.kind).not.toBe("clarification");
    const pinned = page.getByRole("region", { name: "שאלת הבהרה פתוחה" });
    await expect(pinned).toBeVisible();
    await expect(pinned).toContainText(question);
    await expect(pinned.getByRole("group", { name: "אפשרויות הבהרה" })).toBeVisible();
    await expect(page.getByRole("group", { name: "אפשרויות הבהרה" })).toHaveCount(1);
    await expect(answerCards(page).nth(0)).toContainText("שאלת ההבהרה עדיין פתוחה");
    await expect(page.getByText("ממתינה שאלת הבהרה.")).toBeVisible();
    expect((await conversation(page, asked.conversation_id)).pending_clarification?.question).toBe(question);

    // Mobile width: the pinned clarification, the context strip and the answers fit without horizontal scroll.
    await page.setViewportSize({ width: 375, height: 812 });
    await expectNoHorizontalScroll(page);
    await page.screenshot({ path: shot("general-pinned-clarification-mobile.png"), fullPage: true });
    await page.setViewportSize({ width: 1280, height: 800 });

    // The button of the pinned clarification still works (MVP AE1).
    const chosen = await choose(page, "מחירי עסקאות");
    expect(chosen.answer.clarification?.question ?? "").not.toBe(question);
    await expect(answerCards(page).last()).not.toContainText(DATA_KIND_QUESTION);
    await expect(pinned).toHaveCount(0);
    expect((await contextOf(page, asked.conversation_id)).data_kind).toBe("transaction_price");
  });

  test("Retry after a network error reuses the turn id and does not duplicate the message", async ({ page }) => {
    const turnIds: string[] = [];
    let conversationId = "";
    await page.route(
      (url) => isAsk(url.toString()),
      async (route) => {
        const body = route.request().postDataJSON() as { turn_id: string; conversation_id: string };
        turnIds.push(body.turn_id);
        conversationId = body.conversation_id;
        if (turnIds.length === 1) {
          await route.fetch(); // the server finishes the turn; the browser never sees the answer
          await route.abort("failed");
          return;
        }
        await route.continue();
      },
    );
    await page.getByRole("textbox", { name: "שאלה" }).fill(UNKNOWN_PLACE);
    await page.getByRole("button", { name: "שליחה", exact: true }).click();
    const alert = page.locator(".alert-error");
    await expect(alert).toContainText("אין חיבור לשרת");
    await expect(answerCards(page)).toHaveCount(0);

    await alert.getByRole("button", { name: "נסו שוב" }).click();
    await expect(answerCards(page)).toHaveCount(1);
    await expect(page.locator(".bubble-q", { hasText: UNKNOWN_PLACE })).toHaveCount(1);
    expect(turnIds).toHaveLength(2);
    expect(turnIds[1]).toBe(turnIds[0]);
    const stored = (await conversation(page, conversationId)).messages.filter((m) => m.question === UNKNOWN_PLACE);
    expect(stored).toHaveLength(1);
  });

  test("a turn still in progress (409) is polled until its stored result arrives", async ({ page }) => {
    const turnIds: string[] = [];
    let conversationId = "";
    await page.route(
      (url) => isAsk(url.toString()),
      async (route) => {
        const body = route.request().postDataJSON() as { turn_id: string; conversation_id: string };
        turnIds.push(body.turn_id);
        conversationId = body.conversation_id;
        if (turnIds.length === 1) {
          await route.fetch();
          await route.fulfill({ status: 409, json: { detail: "השאלה עדיין בעיבוד" } });
          return;
        }
        await route.continue();
      },
    );
    await page.getByRole("textbox", { name: "שאלה" }).fill(UNKNOWN_PLACE);
    await page.getByRole("button", { name: "שליחה", exact: true }).click();
    await expect(answerCards(page)).toHaveCount(1);
    await expect(page.locator(".alert-error")).toHaveCount(0);
    expect(turnIds.length).toBeGreaterThanOrEqual(2);
    expect(new Set(turnIds).size).toBe(1);
    const stored = (await conversation(page, conversationId)).messages.filter((m) => m.question === UNKNOWN_PLACE);
    expect(stored).toHaveLength(1);
  });

  test("a partial answer shows its ledger counts, and refresh replaces it in place with a new turn", async ({ page }) => {
    test.setTimeout(300_000);
    const turnIds: string[] = [];
    // The first answer is marked partial on its way to the browser (a real deadline cannot be forced here).
    await page.route(
      (url) => isAsk(url.toString()),
      async (route) => {
        turnIds.push((route.request().postDataJSON() as { turn_id: string }).turn_id);
        const res = await route.fetch();
        if (turnIds.length > 1 || res.status() !== 200) {
          await route.fulfill({ response: res });
          return;
        }
        const json = (await res.json()) as AskResponse;
        const coverage = json.answer.coverage ?? {
          text: "",
          docs_pending: 0,
          docs_failed: 0,
          docs_needs_review: 0,
          records_awaiting_verification: 0,
        };
        json.answer = {
          ...json.answer,
          partial: true,
          pending_extraction: 2,
          coverage: {
            ...coverage,
            facts: {
              in_scope: 9,
              found: 5,
              not_stated: 2,
              partial_scan: 1,
              pending: 0,
              failed: 0,
              not_yet_extracted: 2,
              awaiting_review: 1,
              conflicts: 0,
              unknown_metadata: 0,
            },
          },
        };
        await route.fulfill({ response: res, json });
      },
    );
    const first = await askTurn(page, PERMITS);
    test.skip(first.answer.kind === "clarification", "the question was answered with a clarification");
    const card = answerCards(page).last();
    const notice = card.getByRole("note", { name: "תשובה חלקית" });
    await expect(notice.getByText("חלקי", { exact: true })).toBeVisible();
    await expect(notice).toContainText("9 מסמכים בתחום");
    await expect(notice).toContainText("1 נקרא חלקית");
    await expect(notice).toContainText("2 טרם חולצו");

    const response = page.waitForResponse((r) => isAsk(r.url()) && r.status() === 200, { timeout: TURN_TIMEOUT });
    await notice.getByRole("button", { name: "רענון תשובה" }).click();
    await response;
    await idle(page);
    await expect(answerCards(page)).toHaveCount(1);
    await expect(page.locator(".bubble-q", { hasText: PERMITS })).toHaveCount(1);
    await expect(answerCards(page).last().getByRole("note", { name: "תשובה חלקית" })).toHaveCount(0);
    expect(turnIds).toHaveLength(2);
    expect(turnIds[1]).not.toBe(turnIds[0]);
  });

  test("limited mode: the ממ״ד question shows passages and the limitation, with no price clarification", async ({
    page,
    request,
  }) => {
    await apiLogin(request, USERS.adminA);
    const settings = (await (await request.get("/api/admin/settings")).json()) as { mode: ProviderMode };
    await request.post("/api/auth/logout");
    test.skip(settings.mode !== "limited", `office A runs in ${settings.mode} mode; this scenario needs cloud use off`);

    const res = await askTurn(page, SAFE_ROOM);
    const card = answerCards(page).last();
    expect(res.answer.mode).toBe("limited");
    await expectModeBadge(card, "limited");
    expect(res.answer.kind).not.toBe("clarification");
    await expect(page.getByRole("group", { name: "אפשרויות הבהרה" })).toHaveCount(0);
    await expect(card).not.toContainText(DATA_KIND_QUESTION);
    expect(res.answer.numeric ?? null).toBeNull();
    await expect(card.locator(".figures")).toHaveCount(0);
    expect(res.answer.sources.length).toBeGreaterThan(0);
    await expect(card.getByRole("region", { name: "מקורות" })).toBeVisible();
    await expect(card.getByRole("region", { name: "סתירות ומגבלות" })).toContainText("מודל הענן");
    await page.screenshot({ path: shot("general-limited-mode.png"), fullPage: true });
  });
});
