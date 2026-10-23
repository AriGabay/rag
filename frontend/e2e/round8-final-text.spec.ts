import { createHash } from "node:crypto";
import path from "node:path";
import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  actionsFor,
  answerWith,
  cid,
  component,
  decision,
  diagnostics,
  openAnswer,
  openDetails,
  REASON,
  removal,
  TITLE as FIXTURE_TITLE,
  type Json,
} from "./answer-fixtures";
import {
  assistantMessages,
  ensureOfficeBDocument,
  FIXTURES,
  lastAnswer,
  login,
  officeBSettings,
  sendAndWait,
  setOfficeBCloud,
  sourceViewer,
  USERS,
} from "./helpers";

// Round 8 U8 (R17, R22, R23, R25, AE8): what verification did to an answer, linked to its claim by id, and the answer
// that is shown, saved and shown again after a refresh being one version.
//
// Part 1 serves fixed synthetic payloads (the conversation is real, office B; its messages and the diagnostics route
// are mocked, as in chat-gaps-removals.spec.ts): the test checks how entries are paired, worded and opened, not a
// model's choices. Part 2 runs one question against the real stack and compares the displayed text, the API's text,
// the text after a reload and the digest. Office B only; synthetic data only.

const MARKER = (n: number) => `«[${n}: חלק שלא אומת הושמט]»`;

const CUT_1 = "שאושר ביום 1.1.2020";
const CUT_2 = "ושיעורה קבוע בחוק";
const DRAFT_A = "שיעור התשואה בשומה הוא 7% לשנה.";
const DRAFT_B = "דמי השכירות לכלל הנכס הם 90,000 ₪ לחודש.";
const WITHHELD_DRAFT = "מספר החניות בנכס הוא 12.";

/** A full-completeness round-8 answer around `markdown`: its summary rows and the counts of the actions that have
 * none (`more`). */
function r8Answer(markdown: string, rows: Json[], more: Json = {}, verification: Json = {}): Json {
  return answerWith(
    {
      status: "answered", markdown, gaps: [],
      components: [component("N1", "השווי למ״ר", "full", { units: [0], related: ["S1"] })],
    },
    {
      correctness: "partial", removed: rows.filter((r) => r.action === "removed").length,
      completeness: { status: "full", requirements: 1, missing: [] }, removals: rows,
      actions: actionsFor(rows, more), ...verification,
    },
  );
}

/** Serves the log of m2 (the diagnostics route) and counts its calls. */
async function serveLog(page: Page, entries: Json[]): Promise<{ calls: () => number }> {
  let calls = 0;
  await page.route("**/api/chat/messages/m2/diagnostics", (route) => {
    calls += 1;
    return route.fulfill({ json: diagnostics(entries) });
  });
  return { calls: () => calls };
}

function entry(reply: Locator, claim: string): Locator {
  return reply.locator(`[data-testid="removal"][data-claim-id="${claim}"]`);
}

async function expectNoOverflow(page: Page): Promise<void> {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(0);
}

// --- claim-linked details ----------------------------------------------------------------------------------------

test.describe("claim-linked details (mocked messages)", () => {
  test("claims in an order unlike the log's: each detail opens under its own claim", async ({ page }) => {
    // the rows are in the text's order; the log holds a qualified claim first, then the later claim, then the earlier
    const rows = [
      removal("wrong_unit", "N2", { claim_id: cid(0x70) }),
      removal("absent_from_source", null, { claim_id: cid(0x30) }),
    ];
    const answer = r8Answer("השווי למ״ר בשומה הוא 9,500 ₪ [S1].", rows,
      { qualified: 1 });
    const { id, reply } = await openAnswer(page, answer);
    await serveLog(page, [
      decision("absent_from_source", { claim_id: cid(0x10), action: "qualified", text: "טענה שנשארה עם סייג.",
        remaining: "טענה שנשארה עם סייג.", reason: "המקור מציין זאת רק בחלקו." }),
      decision("absent_from_source", { claim_id: cid(0x30), text: DRAFT_B, reason: "אין במקור נתון כזה." }),
      decision("wrong_unit", { claim_id: cid(0x70), text: DRAFT_A, reason: REASON }),
    ]);
    await openDetails(reply);
    const items = reply.getByTestId("removal");
    await expect(items).toHaveCount(2);
    // keyed and ordered by the text, not by the log
    await expect(items.nth(0)).toHaveAttribute("data-claim-id", cid(0x70));
    await expect(items.nth(1)).toHaveAttribute("data-claim-id", cid(0x30));
    // open the second row first, then the first
    await items.nth(1).getByRole("button", { name: /פירוט/ }).click();
    await expect(entry(reply, cid(0x30)).getByTestId("removal-draft")).toContainText(DRAFT_B);
    await expect(entry(reply, cid(0x30))).not.toContainText(DRAFT_A);
    await items.nth(0).getByRole("button", { name: /פירוט/ }).click();
    await expect(entry(reply, cid(0x70)).getByTestId("removal-draft")).toContainText(DRAFT_A);
    await expect(entry(reply, cid(0x70))).not.toContainText(DRAFT_B);
    await expect(entry(reply, cid(0x70)).getByTestId("removal-reason")).toContainText(REASON);
    // the qualified claim is in the log but not under either row
    await expect(reply.getByTestId("removals")).not.toContainText("טענה שנשארה עם סייג.");
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a row whose claim id is not in the log shows no detail, never another claim's", async ({ page }) => {
    const rows = [removal("wrong_unit", "N2", { claim_id: cid(0x70) })];
    const { id, reply } = await openAnswer(page, r8Answer("השווי למ״ר בשומה הוא 9,500 ₪ [S1].", rows));
    await serveLog(page, [decision("wrong_unit", { claim_id: cid(0x99), text: DRAFT_B })]);
    await openDetails(reply);
    const item = reply.getByTestId("removal").first();
    await item.getByRole("button", { name: /פירוט/ }).click();
    await expect(item.getByTestId("removal-unavailable")).toBeVisible();
    await expect(reply).not.toContainText(DRAFT_B);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("two cuts: each marker's ordinal opens its own entry", async ({ page }) => {
    const markdown =
      `השטח העיקרי בתא שטח 1 הוא 1,200 מ״ר ${MARKER(1)} [S1].\n\n` +
      `תכסית הקרקע המותרת היא 40% ${MARKER(2)} [S1].`;
    const rows = [
      removal("absent_from_source", null, { claim_id: cid(0x41), action: "cut", ordinal: 1 }),
      removal("contradicts_source", null, { claim_id: cid(0x42), action: "cut", ordinal: 2 }),
    ];
    const { id, reply } = await openAnswer(page, r8Answer(markdown, rows));
    await serveLog(page, [
      decision("absent_from_source", { claim_id: cid(0x41), action: "cut", ordinal: 1, text: CUT_1,
        remaining: MARKER(1), reason: "המקור אינו מציין תאריך אישור." }),
      decision("contradicts_source", { claim_id: cid(0x42), action: "cut", ordinal: 2, text: CUT_2,
        remaining: MARKER(2), reason: "המקור מציין שיעור אחר." }),
    ]);
    // the markers are in the text as saved, set apart from the model's words, and linked
    const body = reply.locator(".body");
    await expect(body).toContainText(MARKER(1));
    await expect(body).toContainText(MARKER(2));
    const markers = body.getByTestId("cut-marker");
    await expect(markers).toHaveCount(2);
    await expect(markers.nth(0)).toHaveAttribute("data-ordinal", "1");
    await expect(markers.nth(1)).toHaveAttribute("data-ordinal", "2");
    const look = await markers.nth(0).evaluate((el) => {
      const own = getComputedStyle(el);
      const model = getComputedStyle(el.closest("p")!);
      return { markerBorder: own.borderTopStyle, markerBg: own.backgroundColor, modelBg: model.backgroundColor };
    });
    expect(look.markerBorder).toBe("dashed");
    expect(look.markerBg).not.toBe(look.modelBg);
    // the notice names the cuts without the verb "removed"
    const notice = reply.getByTestId("removal-notice");
    await expect(notice).toContainText("הושמטו חלקים שלא אומתו מ-2 משפטים");
    await expect(notice).not.toContainText("הסיר");
    await expect(notice).not.toContainText("הוסר");
    await expect(reply.getByTestId("correctness")).toHaveCount(0);
    // the details are closed until a marker is used
    await expect(reply.locator("details.msg-details")).not.toHaveAttribute("open", "");
    await markers.nth(1).click();
    await expect(reply.locator("details.msg-details")).toHaveAttribute("open", "");
    const second = entry(reply, cid(0x42));
    await expect(second).toHaveAttribute("data-focused", "true");
    await expect(second.getByRole("button", { name: /הסתרת/ })).toHaveAttribute("aria-expanded", "true");
    await expect(second.getByTestId("removal-draft")).toContainText(CUT_2);
    await expect(second.getByTestId("removal-draft")).not.toContainText(CUT_1);
    await expect(second.getByTestId("change-unsupported")).toContainText(CUT_2);
    await expect(second.getByTestId("change-kept")).toContainText(MARKER(2));
    await expect(entry(reply, cid(0x41)).getByRole("button", { name: /פרטי השינוי/ })).toHaveAttribute("aria-expanded", "false");
    await expect(entry(reply, cid(0x41)).getByTestId("removal-draft")).toHaveCount(0);
    // the other marker opens the other entry
    await markers.nth(0).click();
    const first = entry(reply, cid(0x41));
    await expect(first).toHaveAttribute("data-focused", "true");
    await expect(first.getByTestId("removal-draft")).toContainText(CUT_1);
    await expect(first.getByTestId("removal-draft")).not.toContainText(CUT_2);
    await expect(first.getByTestId("change-action")).toContainText("סימון 1");
    // the draft text is only in the labelled blocks, never in the answer's body
    await expect(body).not.toContainText(CUT_1);
    await expect(body).not.toContainText(CUT_2);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a withheld claim says the check could not complete, never that it was rejected or contradicts", async ({ page }) => {
    const rows = [
      removal("not_checked", null, { claim_id: cid(0x51), action: "withheld", cause: "check_failed",
        text: "הוסרה טענה שבדיקתה לא הושלמה בגלל תקלה בבדיקה; היא לא נמצאה שגויה ולא נמצאה חסרה." }),
    ];
    const { id, reply } = await openAnswer(page, r8Answer("השווי למ״ר בשומה הוא 9,500 ₪ [S1].", rows));
    await serveLog(page, [
      // even a log entry that carries a verdict word and a judge's reason shows only the server's wording
      decision("not_checked", { claim_id: cid(0x51), action: "withheld", cause: "check_failed", text: WITHHELD_DRAFT,
        reason: "הטענה סותרת את המקור לכאורה", verdict: "unchecked", repair_attempted: false }),
    ]);
    const notice = reply.getByTestId("removal-notice");
    await expect(notice).toContainText("בדיקתה לא הושלמה");
    await expect(notice).not.toContainText("סותר");
    await expect(notice).not.toContainText("נפסל");
    await openDetails(reply);
    const item = entry(reply, cid(0x51));
    await expect(item.getByTestId("removal-kind")).not.toContainText("סותר");
    await expect(item.getByTestId("change-action")).toHaveAttribute("data-action", "withheld");
    await item.getByRole("button", { name: /פירוט/ }).click();
    const detail = item.getByTestId("removal-detail");
    await expect(detail.getByTestId("removal-reason")).toContainText("הבדיקה לא הושלמה");
    await expect(detail).not.toContainText("סותר");
    await expect(detail).not.toContainText("נפסל");
    await expect(detail).not.toContainText("סיבת ההסרה");
    const draft = detail.getByTestId("removal-draft");
    await expect(draft).toHaveAttribute("data-label", "unverified");
    await expect(draft).toContainText("טיוטה שלא אומתה");
    await expect(draft).toContainText(WITHHELD_DRAFT);
    await expect(reply.locator(".body")).not.toContainText(WITHHELD_DRAFT);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a rejected draft is labelled, sits only in its block of an expanded entry, and is not in the answer", async ({
    page,
  }) => {
    const rows = [removal("contradicts_source", "N2", { claim_id: cid(0x61) })];
    const { id, reply } = await openAnswer(page, r8Answer("השווי למ״ר בשומה הוא 9,500 ₪ [S1].", rows));
    await serveLog(page, [decision("contradicts_source", { claim_id: cid(0x61), text: DRAFT_A, verdict: "contradicts" })]);
    // closed: no draft anywhere, in the notice, the details or the body
    await expect(reply).not.toContainText(DRAFT_A);
    await openDetails(reply);
    await expect(reply).not.toContainText(DRAFT_A);
    const item = entry(reply, cid(0x61));
    await item.getByRole("button", { name: /פירוט/ }).click();
    const draft = item.getByTestId("removal-draft");
    await expect(draft).toHaveAttribute("data-label", "rejected");
    await expect(draft).toContainText("טיוטה שנפסלה");
    await expect(draft).toContainText(DRAFT_A);
    await expect(item.getByTestId("removal-reason")).toContainText(REASON);
    await expect(reply.locator(".body")).not.toContainText(DRAFT_A);
    // exactly once in the whole reply: inside the block
    expect((await reply.innerText()).split(DRAFT_A).length - 1).toBe(1);
    expect(await draft.evaluate((el, text) => el.textContent?.includes(text as string), DRAFT_A)).toBe(true);
    // collapsing drops it again
    await item.getByRole("button", { name: /הסתרת/ }).click();
    await expect(reply).not.toContainText(DRAFT_A);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a cited-only claim is not in the default notice; qualified and cited show only in the collapsed details", async ({
    page,
  }) => {
    // one removal, one qualifier, one citation repaired: the notice counts the removal alone
    const rows = [removal("wrong_unit", "N2", { claim_id: cid(0x71) })];
    const answer = r8Answer("השווי למ״ר בשומה הוא 9,500 ₪ [S1]. שיעור ההיוון 6% [S1].", rows,
      { qualified: 1, cited: 1 });
    const { id, reply } = await openAnswer(page, answer);
    await serveLog(page, [
      decision("absent_from_source", { claim_id: cid(0x72), action: "qualified", severity: "partial", text: "שיעור ההיוון 6%",
        remaining: "שיעור ההיוון 6% (לפי המקור, כפי שנכתב בו)", reason: "המקור מציין את השיעור בלי שנה." }),
      decision("absent_from_source", { claim_id: cid(0x73), action: "cited", severity: "partial", text: "השווי למ״ר 9,500 ₪",
        remaining: "השווי למ״ר 9,500 ₪ [S1]", reason: "", checked_ids: ["S9"] }),
      decision("wrong_unit", { claim_id: cid(0x71), text: DRAFT_A }),
    ]);
    const notice = reply.getByTestId("removal-notice");
    await expect(notice).toContainText("האימות הסיר טענה אחת");
    await expect(notice).not.toContainText("סייג");
    await expect(notice).not.toContainText("ציטוט");
    await expect(notice).not.toContainText("2");
    await openDetails(reply);
    const verification = reply.getByTestId("verification");
    await expect(verification).toContainText("נשארה בתשובה עם סייג");
    await expect(verification).toContainText("צורף ציטוט");
    // their entries load from the log, each under its own claim id
    await reply.getByTestId("other-changes").getByRole("button", { name: /פרטי השינויים/ }).click();
    const changes = reply.getByTestId("change");
    await expect(changes).toHaveCount(2);
    const qualified = reply.locator(`[data-testid="change"][data-claim-id="${cid(0x72)}"]`);
    await expect(qualified.getByTestId("change-action")).toHaveAttribute("data-action", "qualified");
    await expect(qualified.getByTestId("change-kept")).toContainText("כפי שנכתב בו");
    await expect(qualified.getByTestId("removal-reason")).toContainText("בלי שנה");
    await expect(qualified.getByTestId("removal-draft")).toHaveCount(0);
    const cited = reply.locator(`[data-testid="change"][data-claim-id="${cid(0x73)}"]`);
    await expect(cited.getByTestId("change-action")).toHaveAttribute("data-action", "cited");
    await expect(cited.getByTestId("removal-draft")).toHaveCount(0);
    await expect(cited.getByTestId("change-kept")).toHaveCount(0);
    await expect(cited).not.toContainText("השווי למ״ר 9,500 ₪");
    // the removal's draft is still not among them
    await expect(reply.getByTestId("changes")).not.toContainText(DRAFT_A);
    // the attached source opens the viewer at its place
    await cited.getByTestId("removal-source").first().click();
    const viewer = sourceViewer(page);
    await expect(viewer).toBeVisible();
    await expect(viewer.getByTestId("viewer-title")).toContainText(FIXTURE_TITLE);
    await page.keyboard.press("Escape");
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("an answer whose only action is a citation shows no notice and no quality line", async ({ page }) => {
    const answer = r8Answer("השווי למ״ר בשומה הוא 9,500 ₪ [S1].", [], { cited: 1 }, { correctness: "verified" });
    const { id, reply } = await openAnswer(page, answer);
    await expect(reply.locator(".body")).toContainText("9,500");
    await expect(reply.getByTestId("removal-notice")).toHaveCount(0);
    await expect(reply.getByTestId("correctness")).toHaveCount(0);
    await expect(reply.locator(".answer-quality")).toHaveCount(0);
    await openDetails(reply);
    await expect(reply.getByTestId("verification")).toContainText("צורף ציטוט");
    await expect(reply.getByTestId("removal")).toHaveCount(0);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a rewritten claim: the original and the wording that stays, counted in the notice without the verb removed", async ({
    page,
  }) => {
    const answer = r8Answer("השווי למ״ר בשומה הוא 9,500 ₪ [S1].", [], { rewritten: 1 });
    const { id, reply } = await openAnswer(page, answer);
    await serveLog(page, [
      decision("absent_from_source", { claim_id: cid(0x81), action: "rewritten", severity: "partial", text: DRAFT_B,
        remaining: "השווי למ״ר בשומה הוא 9,500 ₪ [S1].", reason: "הניסוח המקורי כלל חלק שהמקור אינו מציין." }),
    ]);
    const notice = reply.getByTestId("removal-notice");
    await expect(notice).toContainText("נוסחה מחדש");
    await expect(notice).not.toContainText("הסיר");
    await openDetails(reply);
    await reply.getByTestId("other-changes").getByRole("button", { name: /פרטי השינויים/ }).click();
    const change = reply.getByTestId("change");
    await expect(change.getByTestId("removal-draft")).toHaveAttribute("data-label", "original");
    await expect(change.getByTestId("removal-draft")).toContainText(DRAFT_B);
    await expect(change.getByTestId("change-kept")).toContainText("9,500");
    await expect(reply.locator(".body")).not.toContainText(DRAFT_B);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });
});

// --- mobile ------------------------------------------------------------------------------------------------------

test.describe("claim-linked details (mobile)", () => {
  test.use({ viewport: { width: 375, height: 812 }, hasTouch: true });

  test("markers, the notice and the expanded entries fit the screen without a horizontal scroll", async ({ page }) => {
    const markdown =
      `השטח העיקרי בתא שטח 1 הוא 1,200 מ״ר ${MARKER(1)} [S1].\n\nתכסית הקרקע המותרת היא 40% ${MARKER(2)} [S1].`;
    const rows = [
      removal("absent_from_source", null, { claim_id: cid(0x41), action: "cut", ordinal: 1 }),
      removal("contradicts_source", null, { claim_id: cid(0x42), action: "cut", ordinal: 2 }),
      removal("not_checked", null, { claim_id: cid(0x43), action: "withheld", cause: "omitted" }),
    ];
    const { id, reply } = await openAnswer(page, r8Answer(markdown, rows));
    await serveLog(page, [
      decision("absent_from_source", { claim_id: cid(0x41), action: "cut", ordinal: 1, text: CUT_1, remaining: MARKER(1) }),
      decision("contradicts_source", { claim_id: cid(0x42), action: "cut", ordinal: 2, text: CUT_2, remaining: MARKER(2) }),
      decision("not_checked", { claim_id: cid(0x43), action: "withheld", cause: "omitted", text: WITHHELD_DRAFT }),
    ]);
    await expect(reply.getByTestId("removal-notice")).toBeVisible();
    await expectNoOverflow(page);
    await reply.getByTestId("cut-marker").nth(1).click();
    await expect(entry(reply, cid(0x42)).getByTestId("removal-draft")).toContainText(CUT_2);
    await entry(reply, cid(0x43)).getByRole("button", { name: /פירוט/ }).click();
    await expect(entry(reply, cid(0x43)).getByTestId("removal-draft")).toContainText(WITHHELD_DRAFT);
    await entry(reply, cid(0x41)).getByRole("button", { name: /פרטי השינוי/ }).click();
    await expect(entry(reply, cid(0x41)).getByTestId("removal-draft")).toContainText(CUT_1);
    await expectNoOverflow(page);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });
});

// --- the same version: displayed, returned by the API, after a reload (office B, real stack) ---------------------

// The conversational question runs against the real model (office B only, a synthetic report: invented names and
// values), as in chat.spec.ts; it is skipped when office B has no model key. What is compared is structure: the
// words the page shows are the words of the API's Markdown (citation chips aside), before and after a reload, and the
// payload's digest is the SHA-256 of that Markdown.
const FILE = path.join(FIXTURES, "chat", "CHAT_synthetic_mixed_report.docx");
const TITLE = "CHAT synthetic mixed report";

/** Only the letters and digits of a text, in order: markdown syntax, list numbers' dots, spaces and chip buttons do not
 * count; the words and numbers do. */
function words(text: string): string {
  return Array.from(text.matchAll(/[\p{L}\p{N}]+/gu), (m) => m[0]).join("");
}

/** The words of the API's Markdown as the page shows them: no citation ids (they are chips), no list or heading
 * markers. */
function markdownWords(markdown: string): string {
  return words(
    markdown
      .replace(/\[((?:[SMCVA]\d+)(?:\s*[,،;]\s*[SMCVA]\d+)*)\]/g, "")
      .replace(/^\s*(?:#{1,6}|>|[-*+]|\d+[.)])\s+/gm, ""),
  );
}

/** The words the reply's body shows, without the citation chips' numbers. */
async function shownWords(reply: Locator): Promise<string> {
  const text = await reply.locator(".body").evaluate((el) => {
    const copy = el.cloneNode(true) as HTMLElement;
    copy.querySelectorAll(".cite").forEach((c) => c.remove());
    return copy.textContent ?? "";
  });
  return words(text);
}

interface Message {
  id: string;
  role: string;
  content: string;
  answer: { kind: string; markdown: string; verification: { final_digest?: string } | null } | null;
}

async function lastReply(page: Page, conversationId: string): Promise<Message> {
  const res = await page.request.get(`/api/chat/conversations/${conversationId}/messages`);
  expect(res.ok(), `messages of ${conversationId}: ${res.status()}`).toBeTruthy();
  const { messages } = (await res.json()) as { messages: Message[] };
  const replies = messages.filter((m) => m.role === "assistant" && m.answer);
  expect(replies.length, "the conversation has an answer").toBeGreaterThan(0);
  return replies[replies.length - 1];
}

test.describe("the same version (office B, real model)", () => {
  test.describe.configure({ mode: "serial" });
  let modelReady = false;

  test.beforeAll(async ({ request }) => {
    test.setTimeout(960_000);
    const settings = await officeBSettings(request);
    modelReady = settings.key_present && process.env.E2E_SKIP_MODEL !== "1";
    if (!modelReady) return;
    await setOfficeBCloud(request, true);
    await ensureOfficeBDocument(request, FILE, TITLE);
  });

  test.afterAll(async ({ request }) => {
    if (modelReady) await setOfficeBCloud(request, false);
  });

  test("the displayed answer, the API's answer and the answer after a reload are one version with one digest", async ({
    page,
  }) => {
    test.skip(!modelReady, "office B has no model key configured (or E2E_SKIP_MODEL=1)");
    test.setTimeout(420_000);
    await login(page, USERS.adminB);
    await page.getByRole("button", { name: "שיחה חדשה" }).first().click();
    const reply = await sendAndWait(page, "מה דמי השכירות הראויים למ״ר בשומה של הגפן 12?", 400_000);
    const { conversationId } = await lastAnswer(page);

    // what the API returns: the message's text is the answer's text, and the digest is that text's SHA-256
    const first = await lastReply(page, conversationId);
    expect(first.answer?.kind, `a model answer (${first.content.slice(0, 120)})`).toBe("rag");
    const markdown = first.answer!.markdown;
    expect(markdown.length).toBeGreaterThan(0);
    expect(first.content).toBe(markdown);
    const digest = first.answer!.verification?.final_digest;
    expect(digest, "the payload carries the digest of the saved text").toMatch(/^[0-9a-f]{64}$/);
    expect(digest).toBe(createHash("sha256").update(markdown, "utf8").digest("hex"));

    // what the page shows is that text
    const shown = await shownWords(reply);
    expect(shown.length).toBeGreaterThan(0);
    expect(shown).toBe(markdownWords(markdown));

    // after a reload: the same message, the same text, the same digest, the same words on the page
    await page.reload();
    await expect(page).toHaveURL(new RegExp(`c=${conversationId}`));
    const again = assistantMessages(page).last();
    await expect(again.locator(".body")).toBeVisible();
    const second = await lastReply(page, conversationId);
    expect(second.id).toBe(first.id);
    expect(second.content).toBe(first.content);
    expect(second.answer!.markdown).toBe(markdown);
    expect(second.answer!.verification?.final_digest).toBe(digest);
    expect(await shownWords(again)).toBe(shown);
    // a cut marker, when the answer has any, is in the text as saved and opens its entry
    for (const m of markdown.matchAll(/«\[(\d+): חלק שלא אומת הושמט\]»/g)) {
      await expect(again.locator(`[data-testid="cut-marker"][data-ordinal="${m[1]}"]`)).toBeVisible();
    }
  });
});
