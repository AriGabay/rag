import { expect, test, type Locator, type Page } from "@playwright/test";
import { answerWith, openAnswer, source, DOC, TITLE, type Json } from "./answer-fixtures";

// Round 12 U8: the server-written coverage note under a time answer. Payloads are fixed and synthetic (invented values,
// titles and notes); the specs check how the screen shows `answer.time.coverage.note` — its own element in reading order
// after the body and the timeline slot, with a timeline or without one, no extra tab stop, nothing for an answer that
// carries no coverage — not what the server derives. Only the office B admin logs in; the API answers are stubbed, so the
// stack's backend image does not matter.

const NOTE_PARTIAL = [
  "הנתון המאוחר ביותר במקורות שנבדקו.",
  "ללא מועד קובע ברור: שומה סינתטית ללא מועד.",
  "שומות שלא קושרו לנכס (אין הוכחה שהן שלו): שומה סינתטית 2024 (15/06/2024) ועוד 2.",
].join("\n");
const NOTE_COMPLETE = "נבדקו כל השומות של הנכס הזמינות לך (3).";
const NOTE_EXPLICIT = "הנתונים לתקופה שביקשת נמצאו רק במקורות שנבדקו.\nלא נבדקו (מגבלת מספר השומות בחיפוש): שומת 2024 סינתטית (01/05/2024).";
const TIMELINE_TEXT = "ציר זמן — שווי הנכס (סינתטי)\n15/03/2025 — 2,450,000 ₪ [S1]\n20/06/2024 — 2,300,000 ₪ [S2]";

function coverage(note: string | null, extra: Json = {}): Json {
  return {
    v: 1, mode: "recent", requested: null, scope: "property", via: "focus", identified: true,
    property: { label: "הצאלון 7 (סינתטי)", reasons: ["parcel 7101/15"], units: 3 },
    units: [], candidates: { named: [], count: 0 }, undetermined: 0, complete: false, blocking: ["undated"], note, ...extra,
  };
}

function rowOf(extra: Json): Json {
  return {
    date: "2025-03-15", precision: "day", end: null, date_kind: "valuation", value_text: "2,450,000 ₪", unit: "₪", sid: "S1",
    source: { document_id: DOC, title: TITLE, location: "עמוד 3" }, marks: [], ...extra,
  };
}

/** A time answer: `timeline` rows (or none), the server's copy text, and the coverage (or none, as on an older message). */
function timeAnswer(opts: { rows?: boolean; coverage?: Json | null; text?: string | null; mode?: string }): Json {
  const rows = opts.rows ?? true;
  const time: Json = {
    mode: opts.mode ?? "recent", prefer_recent: true, requested: null,
    timeline: rows ? [{ label: "שווי הנכס (סינתטי)", rows: [rowOf({}), rowOf({ date: "2024-06-20", value_text: "2,300,000 ₪", sid: "S2" })] }] : [],
    text: opts.text === undefined ? (rows ? TIMELINE_TEXT : null) : opts.text,
  };
  if (opts.coverage) time.coverage = opts.coverage;
  return answerWith({
    markdown: "הנתון המאוחר ביותר במקורות שנבדקו הוא 2,450,000 ₪ [S1], ובעבר 2,300,000 ₪ [S2].",
    status: "answered", gaps: [], components: [],
    sources: [source("S1"), source("S2", { text: "שווי הנכס 2,300,000 ₪." })],
    time,
  });
}

function overflowOf(page: Page): Promise<number> {
  return page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
}

async function copiedText(page: Page, reply: Locator): Promise<string> {
  await page.evaluate(() => {
    const w = window as unknown as { __copied?: string };
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: async (t: string) => { w.__copied = t; } },
    });
  });
  await reply.getByRole("button", { name: "העתקת התשובה" }).click();
  await expect.poll(() => page.evaluate(() => (window as unknown as { __copied?: string }).__copied ?? "")).not.toBe("");
  return page.evaluate(() => (window as unknown as { __copied?: string }).__copied ?? "");
}

test.describe("the coverage note", () => {
  test("it follows the timeline in reading order, is exposed as a note and adds no tab stop", async ({ page }) => {
    const { reply } = await openAnswer(page, timeAnswer({ coverage: coverage(NOTE_PARTIAL), text: `${TIMELINE_TEXT}\n\n${NOTE_PARTIAL}` }));
    const note = reply.getByRole("note");
    await expect(note).toHaveCount(1);
    await expect(note).toHaveAttribute("data-testid", "coverage-note");
    await expect(note).toBeVisible();
    // the words are the server's, line by line
    expect(await note.evaluate((el) => el.textContent)).toBe(NOTE_PARTIAL);
    await expect(note).toContainText("הנתון המאוחר ביותר במקורות שנבדקו");
    // reading order: body, the timeline, then the note, then the action row
    const order = await reply.evaluate((root) => {
      const at = (sel: string) => root.querySelector(sel);
      const follows = (a: Element | null, b: Element | null) =>
        !!a && !!b && !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
      return {
        bodyThenTimeline: follows(at(".body"), at("[data-testid=timeline]")),
        timelineThenNote: follows(at("[data-testid=timeline]"), at("[data-testid=coverage-note]")),
        noteThenActions: follows(at("[data-testid=coverage-note]"), at(".msg-actions")),
      };
    });
    expect(order).toEqual({ bodyThenTimeline: true, timelineThenNote: true, noteThenActions: true });
    // not a tab stop: no tabindex and nothing focusable inside; Tab from the last timeline citation lands on the copy button
    await expect(note).not.toHaveAttribute("tabindex", /.*/);
    await expect(note.locator("a, button, input, select, textarea, [tabindex]")).toHaveCount(0);
    await reply.getByTestId("timeline").getByRole("button", { name: /^מקור S2/ }).focus();
    await page.keyboard.press("Tab");
    await expect(reply.getByRole("button", { name: "העתקת התשובה" })).toBeFocused();
  });

  test("at 375px in RTL it sits under the timeline, inside the viewport, without horizontal scroll", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    const { reply } = await openAnswer(page, timeAnswer({ coverage: coverage(NOTE_PARTIAL), text: `${TIMELINE_TEXT}\n\n${NOTE_PARTIAL}` }));
    const note = reply.getByTestId("coverage-note");
    await expect(note).toBeVisible();
    expect(await overflowOf(page)).toBeLessThanOrEqual(0);
    const t = await reply.getByTestId("timeline").boundingBox();
    const n = await note.boundingBox();
    expect(n?.y ?? 0).toBeGreaterThanOrEqual((t?.y ?? 0) + (t?.height ?? 0) - 1);
    expect((n?.x ?? -1)).toBeGreaterThanOrEqual(0);
    expect((n?.x ?? 0) + (n?.width ?? 0)).toBeLessThanOrEqual(375);
    expect(await note.evaluate((el) => getComputedStyle(el).direction)).toBe("rtl");
    // the lines are separate lines, not one run-on paragraph
    expect(await note.evaluate((el) => getComputedStyle(el).whiteSpace)).toBe("pre-line");
  });

  test("it appears with an empty timeline too (an answer that cites a passage only)", async ({ page }) => {
    const { reply } = await openAnswer(page, timeAnswer({ rows: false, coverage: coverage(NOTE_PARTIAL), text: NOTE_PARTIAL }));
    await expect(reply.getByTestId("timeline")).toHaveCount(0);
    await expect(reply.getByTestId("coverage-note")).toHaveText(NOTE_PARTIAL.replace(/\n/g, " "));
    await expect(reply.getByRole("note")).toBeVisible();
  });

  test("the explicit, complete and not-identified notes are shown as the server wrote them", async ({ page }) => {
    const { reply } = await openAnswer(page, timeAnswer({ rows: false, mode: "explicit", coverage: coverage(NOTE_EXPLICIT, { mode: "explicit" }), text: NOTE_EXPLICIT }));
    await expect(reply.getByTestId("coverage-note")).toContainText("הנתונים לתקופה שביקשת נמצאו רק במקורות שנבדקו");
    await expect(reply.getByTestId("coverage-note")).toContainText("שומת 2024 סינתטית");
  });

  test("a complete note says only what was checked, never the latest in the repository", async ({ page }) => {
    const { reply } = await openAnswer(page, timeAnswer({ coverage: coverage(NOTE_COMPLETE, { complete: true, blocking: [] }) }));
    const note = reply.getByTestId("coverage-note");
    await expect(note).toHaveText(NOTE_COMPLETE);
    expect(await note.textContent()).not.toContain("במאגר");
  });

  test("an older message without coverage renders as before: no note, no complete label", async ({ page }) => {
    const { reply } = await openAnswer(page, timeAnswer({}));
    await expect(reply.getByTestId("timeline")).toBeVisible();
    await expect(reply.getByTestId("coverage-note")).toHaveCount(0);
    await expect(reply.getByRole("note")).toHaveCount(0);
    expect(await reply.textContent()).not.toContain("נבדקו כל השומות");
    // and one without any `time` at all
    const bare = answerWith({ markdown: "תשובה ישנה [S1].", status: "answered", gaps: [], components: [] });
    const { reply: old } = await openAnswer(page, bare);
    await expect(old.getByTestId("coverage-note")).toHaveCount(0);
  });

  test("a coverage record without a note shows nothing", async ({ page }) => {
    const { reply } = await openAnswer(page, timeAnswer({ coverage: coverage(null) }));
    await expect(reply.getByTestId("coverage-note")).toHaveCount(0);
  });

  test("the copy button appends the note, with a timeline and with none", async ({ page, context }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    const { reply } = await openAnswer(page, timeAnswer({ coverage: coverage(NOTE_PARTIAL), text: `${TIMELINE_TEXT}\n\n${NOTE_PARTIAL}` }));
    const copied = await copiedText(page, reply);
    expect(copied).toContain(TIMELINE_TEXT);
    expect(copied.endsWith(NOTE_PARTIAL)).toBe(true);
    expect(copied.split(NOTE_PARTIAL)).toHaveLength(2); // once
    expect(copied.indexOf("הנתון המאוחר ביותר במקורות שנבדקו הוא")).toBeLessThan(copied.indexOf(TIMELINE_TEXT));
    // the note is not shown a second time on screen as plain text of the timeline copy
    await expect(page.getByText(TIMELINE_TEXT)).toHaveCount(0);
  });

  test("the copy button appends the note even when there is no timeline text", async ({ page, context }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    const { reply } = await openAnswer(page, timeAnswer({ rows: false, coverage: coverage(NOTE_PARTIAL), text: null }));
    const copied = await copiedText(page, reply);
    expect(copied.endsWith(NOTE_PARTIAL)).toBe(true);
    expect(copied.split(NOTE_PARTIAL)).toHaveLength(2);
  });

  test("a message without coverage copies the answer alone", async ({ page, context }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    const { reply } = await openAnswer(page, timeAnswer({ rows: false, text: null }));
    const copied = await copiedText(page, reply);
    expect(copied).toBe("הנתון המאוחר ביותר במקורות שנבדקו הוא 2,450,000 ₪, ובעבר 2,300,000 ₪."); // the answer alone
  });
});
