import { expect, test, type Locator, type Page, type Route } from "@playwright/test";
import { answerWith, BASE, DOC, message, PNG, READING, source, thread, TITLE, VER, type Json } from "./answer-fixtures";
import { assistantMessages, composer, login, sourceViewer, USERS } from "./helpers";

// Round 11 U10: the time-preference switch above the composer, the state each answer shows and the answer's timeline.
// Payloads are fixed and synthetic (invented values, titles and quotes); the specs check how the screens show them,
// not what the server derives. Only the office B admin logs in. The `answer.time` payload is built by a later backend
// unit, so it is stubbed here from the shared contract; the requests that would run a turn are answered here too.

const SWITCH = "העדף נתונים עדכניים";
const EXPLANATION = "מציג תחילה נתונים עדכניים, לצד נתונים קודמים רלוונטיים";

function theSwitch(page: Page): Locator {
  return page.getByRole("switch", { name: SWITCH });
}

async function newConversation(page: Page): Promise<string> {
  const res = await page.request.post("/api/chat/conversations");
  expect(res.ok()).toBeTruthy();
  return (await res.json()).id as string;
}

function overflowOf(page: Page): Promise<number> {
  return page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
}

/** An assistant message as the server returns it after round 11: `time_preference` is the recorded state. */
function reply(id: string, answer: Json | null, preference: boolean | null, extra: Json = {}): Json {
  return message(id, "assistant", String(answer?.markdown ?? ""), answer, {
    time_preference: preference === null ? null : { version: 1, prefer_recent: preference, anchor_date: "2026-10-14" },
    ...extra,
  });
}

/** Serves `items` as the conversation `id`, with the switch stored as `prefer`, and the pages the source viewer reads. */
async function serveThread(page: Page, id: string, items: Json[], prefer: boolean): Promise<void> {
  await page.route(`**/api/chat/conversations/${id}/messages*`, (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    const t = thread(id, items);
    return route.fulfill({ json: { ...t, conversation: { ...(t.conversation as Json), prefer_recent: prefer } } });
  });
  await page.route("**/api/documents/**/pages/*/image*", (route) =>
    route.fulfill({ status: 200, contentType: "image/png", body: PNG,
      headers: { "X-Display-Width": "600", "X-Display-Height": "900", "X-Render-Scale": "1", "X-Scale-Tier": "normal",
        "X-Reading-State": "current", "Cache-Control": "private, no-store" } }));
  await page.route("**/api/documents/**/blocks*", (route) =>
    route.fulfill({ json: { document_id: DOC, version_id: VER, title: TITLE, is_current: true,
      mime_type: "application/pdf", total: 1, file_url: `${BASE}/file`, reading_id: READING,
      blocks: [{ index: 0, kind: "paragraph", section: "4. שיטת ההיוון", section_path: ["4. שיטת ההיוון"],
        paragraph_no: null, page: 3, media: null, source: "text", status: "read", note: null,
        text: "שיעור ההיוון החודשי הוא 0.6%.", bbox: [60, 100, 540, 160], method: "text_layer",
        reader_version: "pdf-test", content_hash: null, original_text: null, page_url: `${BASE}/pages/3/image`,
        region_url: `${BASE}/regions/0/image?reading_id=${READING}` }] } }));
}

/** Answers the send request of a turn with a finished pair; `onBody` sees the request body. */
async function serveSend(page: Page, opts: { onBody?: (body: Json) => void; gate?: Promise<void> } = {}): Promise<void> {
  await page.route("**/api/chat/conversations/*/messages", async (route: Route) => {
    if (route.request().method() !== "POST") return route.fallback();
    const body = route.request().postDataJSON() as Json;
    opts.onBody?.(body);
    if (opts.gate) await opts.gate;
    const answer = answerWith({ markdown: "התשובה הסינתטית [S1].", status: "answered", gaps: [], components: [] });
    return route.fulfill({
      json: {
        user: message("u-sent", "user", String(body.content), null, {
          client_id: body.client_id, reply_to: null,
          time_preference: { version: 1, prefer_recent: body.prefer_recent ?? false, anchor_date: "2026-10-14" },
        }),
        assistant: reply("a-sent", answer, Boolean(body.prefer_recent), { reply_to: "u-sent" }),
      },
    });
  });
}

/** Presses Tab until the focused element satisfies `predicate` (evaluated in the page). */
async function tabUntil(page: Page, predicate: (el: Element) => boolean, max = 300): Promise<void> {
  for (let i = 0; i < max; i++) {
    await page.keyboard.press("Tab");
    const focused = await page.evaluateHandle(() => document.activeElement ?? document.body);
    const hit = await focused.evaluate(predicate);
    await focused.dispose();
    if (hit) return;
  }
  throw new Error(`focus never reached the target after ${max} Tab presses`);
}

const TIMELINE_TEXT = "ציר זמן — שווי הנכס (סינתטי)\n15/03/2025 — 2,450,000 ₪ [S1]\n20/06/2024 — 2,300,000 ₪ [S2]";

function timelineAnswer(): Json {
  const row = (extra: Json): Json => ({
    date: "2025-03-15", precision: "day", end: null, date_kind: "valuation", value_text: "2,450,000 ₪", unit: "₪", sid: "S1",
    source: { document_id: DOC, title: TITLE, location: "עמוד 3" }, marks: [], ...extra,
  });
  return answerWith({
    markdown: "השווי העדכני הוא 2,450,000 ₪ [S1], ובעבר 2,300,000 ₪ [S2].",
    status: "answered", gaps: [], components: [],
    sources: [source("S1"), source("S2", { text: "שווי הנכס 2,300,000 ₪." }), source("S3", { text: "שווי הנכס 2,100,000 ₪." })],
    time: {
      mode: "recent", prefer_recent: true, requested: null,
      text: TIMELINE_TEXT,
      timeline: [{
        label: "שווי הנכס (סינתטי)",
        // payload order is the display order: it is deliberately not chronological in its tail
        rows: [
          row({}),
          row({ date: "2024-06-20", value_text: "2,300,000 ₪", sid: "S2", marks: ["conflict"] }),
          row({ date: "2023-05", precision: "month", date_kind: "transaction", value_text: "2,200,000 ₪", sid: "S3",
            marks: ["forecast"] }),
          row({ date: null, precision: null, date_kind: null, value_text: "2,100,000 ₪", sid: "S3",
            marks: ["unknown_date", "outside_requested"] }),
          // a table caption's period (2020-2023), its value without the unit in the text: the end and the unit are shown
          row({ date: "2020", end: "2023", precision: "year", date_kind: "range", value_text: "1,900,000", sid: "S2",
            marks: ["uncertain"] }),
        ],
      }],
    },
  });
}

test.describe("the switch", () => {
  test("a new conversation shows it on, and the first send body carries the value", async ({ page }) => {
    await login(page, USERS.adminB);
    const bodies: Json[] = [];
    await serveSend(page, { onBody: (b) => bodies.push(b) });
    await page.goto("/chat");
    const sw = theSwitch(page);
    await expect(sw).toBeVisible();
    await expect(sw).toHaveAttribute("aria-checked", "true");
    await expect(page.getByText(EXPLANATION)).toBeVisible();
    await composer(page).fill("מה השווי של הנכס הסינתטי?");
    await composer(page).press("Enter");
    await expect(assistantMessages(page).last().getByTestId("time-note")).toHaveText("העדפת זמן: עדכני תחילה");
    expect(bodies).toHaveLength(1);
    expect(bodies[0].prefer_recent).toBe(true);
  });

  test("a draft turned off sends off, and a new chat starts on again", async ({ page }) => {
    await login(page, USERS.adminB);
    const bodies: Json[] = [];
    await serveSend(page, { onBody: (b) => bodies.push(b) });
    await page.goto("/chat");
    await theSwitch(page).click();
    await expect(theSwitch(page)).toHaveAttribute("aria-checked", "false");
    await composer(page).fill("שאלה סינתטית בלי העדפת זמן");
    await composer(page).press("Enter");
    await expect(assistantMessages(page).last().getByTestId("time-note")).toHaveText("העדפת זמן: ללא");
    expect(bodies[0].prefer_recent).toBe(false);
    await expect(theSwitch(page)).toHaveAttribute("aria-checked", "false");
    await page.getByRole("button", { name: "שיחה חדשה" }).first().click();
    await expect(theSwitch(page)).toHaveAttribute("aria-checked", "true");
  });

  test("turned off on an existing conversation it stays off after a reload", async ({ page }) => {
    await login(page, USERS.adminB);
    const id = await newConversation(page);
    await page.goto(`/chat?c=${id}`);
    await expect(theSwitch(page)).toHaveAttribute("aria-checked", "true");
    const saved = page.waitForResponse((r) => r.url().endsWith(`/api/chat/conversations/${id}`) && r.request().method() === "PATCH");
    await theSwitch(page).click();
    await expect(theSwitch(page)).toHaveAttribute("aria-checked", "false");
    expect((await saved).ok()).toBeTruthy();
    expect((await saved).request().postDataJSON()).toEqual({ prefer_recent: false });
    await page.reload();
    await expect(theSwitch(page)).toHaveAttribute("aria-checked", "false");
  });

  test("a messages response that was requested before the toggle and lands after the save does not undo it", async ({ page }) => {
    await login(page, USERS.adminB);
    const id = await newConversation(page);
    // the conversation's messages request is held: it left before the toggle and lands after the PATCH has settled,
    // carrying the value the server held before (on)
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    await page.route(`**/api/chat/conversations/${id}/messages*`, async (route) => {
      if (route.request().method() !== "GET") return route.fallback();
      await gate;
      const t = thread(id, []);
      return route.fulfill({ json: { ...t, conversation: { ...(t.conversation as Json), id, prefer_recent: true } } });
    });
    await page.goto(`/chat?c=${id}`);
    await expect(theSwitch(page)).toHaveAttribute("aria-checked", "true"); // from the list, which is not held
    const saved = page.waitForResponse((r) => r.url().endsWith(`/api/chat/conversations/${id}`) && r.request().method() === "PATCH");
    await theSwitch(page).click();
    await expect(theSwitch(page)).toHaveAttribute("aria-checked", "false");
    expect((await saved).ok()).toBeTruthy();
    const landed = page.waitForResponse((r) => r.url().includes(`/api/chat/conversations/${id}/messages`));
    release();
    await landed;
    // let the stale payload be handled, then the switch must still hold the user's choice
    await page.waitForTimeout(300);
    await expect(theSwitch(page)).toHaveAttribute("aria-checked", "false");
  });

  test("a failed save reverts the switch and shows the notice", async ({ page }) => {
    await login(page, USERS.adminB);
    const id = await newConversation(page);
    await page.route(`**/api/chat/conversations/${id}`, (route) =>
      route.request().method() === "PATCH"
        ? route.fulfill({ status: 500, json: { detail: "שמירת ההעדפה נכשלה (בדיקה סינתטית)" } })
        : route.fallback());
    await page.goto(`/chat?c=${id}`);
    await expect(theSwitch(page)).toHaveAttribute("aria-checked", "true");
    await theSwitch(page).click();
    await expect(page.getByRole("alert").filter({ hasText: "שמירת ההעדפה נכשלה" })).toBeVisible();
    await expect(theSwitch(page)).toHaveAttribute("aria-checked", "true");
  });

  test("the keyboard reaches it, and Space and Enter toggle it", async ({ page }) => {
    await login(page, USERS.adminB);
    await page.goto("/chat");
    await expect(theSwitch(page)).toHaveAttribute("aria-checked", "true");
    await tabUntil(page, (el) => el.getAttribute("role") === "switch");
    const sw = theSwitch(page);
    await expect(sw).toBeFocused();
    await page.keyboard.press("Space");
    await expect(sw).toHaveAttribute("aria-checked", "false");
    await page.keyboard.press("Enter");
    await expect(sw).toHaveAttribute("aria-checked", "true");
  });

  test("the explanation is wired to the switch and the touch target is 44px", async ({ page }) => {
    await login(page, USERS.adminB);
    await page.goto("/chat");
    const sw = theSwitch(page);
    await expect(sw).toBeVisible();
    await expect(sw).toHaveAccessibleDescription(EXPLANATION);
    const box = await sw.boundingBox();
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);
    // the control row is above the pill, outside the textarea and send row
    const form = await page.locator("form.chat-composer").boundingBox();
    expect((box?.y ?? 0) + (box?.height ?? 0)).toBeLessThanOrEqual((form?.y ?? 0) + 1);
  });

  test("it is enabled while a turn runs", async ({ page }) => {
    await login(page, USERS.adminB);
    const id = await newConversation(page);
    const running = message("a-run", "assistant", "", null, { status: "running", progress: [{ step: "search", label: "מחפש" }] });
    await serveThread(page, id, [message("u-run", "user", "שאלה סינתטית שעדיין רצה", null), running], true);
    await page.route("**/api/chat/messages/a-run", (route) => route.fulfill({ json: running }));
    await page.goto(`/chat?c=${id}`);
    await expect(assistantMessages(page).last().locator(".status-line")).toBeVisible();
    await expect(theSwitch(page)).toBeEnabled();
    await expect(theSwitch(page)).toHaveAttribute("aria-checked", "true");
  });

  test("it is disabled only while the send request is pending", async ({ page }) => {
    await login(page, USERS.adminB);
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    await serveSend(page, { gate });
    await page.goto("/chat");
    await expect(theSwitch(page)).toBeEnabled();
    await composer(page).fill("שאלה סינתטית בזמן שליחה");
    await composer(page).press("Enter");
    await expect(theSwitch(page)).toBeDisabled();
    release();
    await expect(assistantMessages(page).last().getByTestId("time-note")).toBeVisible();
    await expect(theSwitch(page)).toBeEnabled();
  });

  test("on a phone the switch and its explanation sit above the pill and the textarea keeps its width", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await login(page, USERS.adminB);
    await page.goto("/chat");
    const sw = theSwitch(page);
    const desc = page.getByText(EXPLANATION);
    await expect(sw).toBeVisible();
    await expect(desc).toBeVisible();
    expect(await overflowOf(page)).toBeLessThanOrEqual(0);
    const s = await sw.boundingBox();
    const d = await desc.boundingBox();
    const f = await page.locator("form.chat-composer").boundingBox();
    const t = await composer(page).boundingBox();
    expect(s?.height ?? 0).toBeGreaterThanOrEqual(44);
    // the explanation stacks under the label, and both are above the pill
    expect(d?.y ?? 0).toBeGreaterThanOrEqual((s?.y ?? 0) + (s?.height ?? 0) - 1);
    expect((d?.y ?? 0) + (d?.height ?? 0)).toBeLessThanOrEqual((f?.y ?? 0) + 1);
    expect(t?.width ?? 0).toBeGreaterThanOrEqual(250);
  });

  test("in dark mode it uses the colours of the other composer controls", async ({ page }) => {
    await login(page, USERS.adminB);
    await page.goto("/chat");
    await page.getByRole("combobox", { name: "ערכת צבעים" }).selectOption("dark");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    const colours = await page.evaluate(() => {
      const css = (el: Element | null, p: string) => (el ? getComputedStyle(el).getPropertyValue(p) : "");
      const sw = document.querySelector(".recency-switch");
      return {
        label: css(document.querySelector(".recency-label"), "color"),
        text: css(document.querySelector(".chat-composer textarea"), "color"),
        desc: css(document.querySelector(".recency-desc"), "color"),
        hint: css(document.querySelector(".composer-hint"), "color"),
        trackOn: css(sw?.querySelector(".recency-track") ?? null, "background-color"),
        page: css(document.querySelector(".chat-composer-wrap"), "background-color"),
      };
    });
    expect(colours.label).toBe(colours.text);
    expect(colours.desc).toBe(colours.hint);
    expect(colours.trackOn).not.toBe(colours.page);
    await page.getByRole("combobox", { name: "ערכת צבעים" }).selectOption("system");
  });
});

test.describe("the state an answer shows", () => {
  test("each answer shows the state it was produced with, whatever the switch says now", async ({ page }) => {
    await login(page, USERS.adminB);
    const id = await newConversation(page);
    const plain = answerWith({ markdown: "תשובה סינתטית [S1].", status: "answered", gaps: [], components: [] });
    const explicit = answerWith({ markdown: "תשובה לתקופה מבוקשת [S1].", status: "answered", gaps: [], components: [],
      time: { mode: "explicit", prefer_recent: true, requested: { kind: "point", start: "2022-01-01", end: null,
        precision: "year", text: "ב-2022" }, timeline: [] } });
    const searchOnly = answerWith({ kind: "search_only", markdown: "תוצאות חיפוש סינתטיות [S1].", status: "answered",
      gaps: [], components: [] });
    await serveThread(page, id, [
      message("u1", "user", "שאלה ראשונה", null), reply("a1", plain, false),
      message("u2", "user", "שאלה שנייה", null), reply("a2", plain, true),
      message("u3", "user", "שאלה שלישית", null), reply("a3", explicit, true),
      message("u4", "user", "שאלה רביעית", null), reply("a4", searchOnly, true),
      message("u5", "user", "שאלה מלפני הסבב", null), reply("a5", plain, null),
    ], true);
    await page.goto(`/chat?c=${id}`);
    const replies = assistantMessages(page);
    await expect(replies).toHaveCount(5);
    // the switch is on, while the first answer was produced with it off
    await expect(theSwitch(page)).toHaveAttribute("aria-checked", "true");
    await expect(replies.nth(0).getByTestId("time-note")).toHaveText("העדפת זמן: ללא");
    await expect(replies.nth(1).getByTestId("time-note")).toHaveText("העדפת זמן: עדכני תחילה");
    await expect(replies.nth(2).getByTestId("time-note")).toHaveText("לפי הזמן שביקשת");
    await expect(replies.nth(3).getByTestId("time-note")).toHaveText("העדפת זמן: עדכני תחילה");
    await expect(replies.nth(4).getByTestId("time-note")).toHaveCount(0);
    // the note sits in the actions row
    await expect(replies.nth(0).locator(".msg-actions").getByTestId("time-note")).toBeVisible();
  });
});

test.describe("the timeline", () => {
  async function open(page: Page): Promise<Locator> {
    await login(page, USERS.adminB);
    const id = await newConversation(page);
    const answer = timelineAnswer();
    await serveThread(page, id, [message("u1", "user", "מה השווי של הנכס?", null), reply("a1", answer, true)], true);
    await page.goto(`/chat?c=${id}`);
    const block = assistantMessages(page).last().getByTestId("timeline");
    await expect(block).toBeVisible();
    return block;
  }

  test("rows keep the payload order, with dates in bdi, a kind label and text badges", async ({ page }) => {
    const block = await open(page);
    await expect(block.getByRole("heading", { name: "ציר זמן" })).toBeVisible();
    await expect(block.getByText("שווי הנכס (סינתטי)")).toBeVisible();
    const rows = block.getByTestId("timeline-row");
    await expect(rows).toHaveCount(5);
    expect(await rows.locator(".tl-date bdi").allInnerTexts()).toEqual([
      "15/03/2025", "20/06/2024", "05/2023", "ללא תאריך", "2020–2023"]);
    // the unit is added only where the value text does not carry it
    expect(await rows.locator(".tl-value bdi").allInnerTexts()).toEqual([
      "2,450,000 ₪", "2,300,000 ₪", "2,200,000 ₪", "2,100,000 ₪", "1,900,000 ₪"]);
    await expect(rows.nth(4).locator(".tl-kind")).toHaveText("תקופה");
    await expect(rows.nth(4).getByText("קריאה לא ודאית")).toBeVisible();
    await expect(rows.nth(0).locator(".tl-kind")).toHaveText("מועד קובע");
    await expect(rows.nth(2).locator(".tl-kind")).toHaveText("מועד עסקה");
    await expect(rows.nth(1).getByText("סתירה בין מקורות")).toBeVisible();
    await expect(rows.nth(2).getByText("תחזית")).toBeVisible();
    await expect(rows.nth(3).getByText("מועד לא ידוע")).toBeVisible();
    await expect(rows.nth(3).getByText("מחוץ לזמן שביקשת")).toBeVisible();
    await expect(rows.nth(0).getByText("סתירה בין מקורות")).toHaveCount(0);
    // a table at desktop width
    await expect(block.getByRole("table")).toBeVisible();
    await expect(block.getByRole("columnheader", { name: "תאריך" })).toBeVisible();
  });

  test("at 375px it stacks the rows without horizontal scroll, and a citation opens the source viewer", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    const block = await open(page);
    expect(await overflowOf(page)).toBeLessThanOrEqual(0);
    const first = block.getByTestId("timeline-row").first();
    const date = await first.locator(".tl-date").boundingBox();
    const value = await first.locator(".tl-value").boundingBox();
    const cite = await first.locator(".tl-source").boundingBox();
    // line 1 the date, line 2 the value, then the citation
    expect(value?.y ?? 0).toBeGreaterThanOrEqual((date?.y ?? 0) + (date?.height ?? 0) - 1);
    expect(cite?.y ?? 0).toBeGreaterThanOrEqual((value?.y ?? 0) + (value?.height ?? 0) - 1);
    await first.getByRole("button", { name: /^מקור S1/ }).click();
    await expect(sourceViewer(page)).toBeVisible();
  });

  test("the copy button appends the timeline's plain text to the answer", async ({ page, context }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await open(page);
    await page.evaluate(() => {
      const w = window as unknown as { __copied?: string };
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: { writeText: async (t: string) => { w.__copied = t; } },
      });
    });
    await assistantMessages(page).last().getByRole("button", { name: "העתקת התשובה" }).click();
    await expect.poll(() => page.evaluate(() => (window as unknown as { __copied?: string }).__copied ?? "")).toContain(TIMELINE_TEXT);
    const copied = await page.evaluate(() => (window as unknown as { __copied?: string }).__copied ?? "");
    expect(copied).toContain("השווי העדכני הוא");
    expect(copied.indexOf("השווי העדכני")).toBeLessThan(copied.indexOf(TIMELINE_TEXT));
    // the text is not shown a second time on screen
    await expect(page.getByText("15/03/2025 — 2,450,000 ₪ [S1]")).toHaveCount(0);
  });

  test("an answer without a timeline shows no block", async ({ page }) => {
    await login(page, USERS.adminB);
    const id = await newConversation(page);
    const answer = answerWith({ markdown: "תשובה בלי ציר זמן [S1].", status: "answered", gaps: [], components: [],
      time: { mode: "none", prefer_recent: false, requested: null, timeline: [] } });
    await serveThread(page, id, [message("u1", "user", "שאלה", null), reply("a1", answer, false)], false);
    await page.goto(`/chat?c=${id}`);
    await expect(assistantMessages(page).last().getByTestId("time-note")).toHaveText("העדפת זמן: ללא");
    await expect(page.getByTestId("timeline")).toHaveCount(0);
  });
});
