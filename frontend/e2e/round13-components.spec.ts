import { expect, test, type Page } from "@playwright/test";
import { assistantMessages, sourceViewer } from "./helpers";
import { answerWith, openAnswer, source, DOC, TITLE, type Json } from "./answer-fixtures";

// Round 13 U6: the components table (`answer.time.breakdown`) beside the timeline, and the undecided mark with its
// counterpart. Payloads are fixed and synthetic (invented coefficients, facets and titles); the specs check how the screen
// shows what the server sent — the heading, the facet column, the values and source chips, the undecided badge and the
// chip that names its counterpart, the layout at 375px in RTL, and an older message without `breakdown` — not what the
// server derives. Only the office B admin logs in; the API answers are stubbed, so the stack's backend image does not matter.

const FACET_A = "מקדם הפחתה בשל מגבלות בנייה";
const FACET_B = "מקדם התאמה בשל שיתוף במקרקעין ובשל מגבלות זכויות מעבר לרכיב רגיל בלשון ארוכה במיוחד כדי לבדוק גלישת שורות";
const NO_FACET = "רכיב לא צוין במקור";
const COPY_TEXT = `רכיבי השומה\nמקדם (סינתטי) — ${TITLE} (31/03/2025)\n- ${FACET_A} · 0.8 · ${TITLE}`;

function row(sid: string, facet: string, value: string, extra: Json = {}): Json {
  return {
    facet, value_text: value, unit: null, sid, source: { document_id: DOC, title: TITLE, location: "עמוד 3" }, marks: [], ...extra,
  };
}

function block(rows: Json[], extra: Json = {}): Json {
  return {
    label: `מקדם (סינתטי) — ${TITLE}`,
    as_of: { date: "2025-03-31", precision: "day", end: null, date_kind: "valuation" },
    rows, ...extra,
  };
}

function timelineRow(sid: string, date: string, value: string, extra: Json = {}): Json {
  return {
    date, precision: "day", end: null, date_kind: "valuation", value_text: value, unit: null, sid,
    source: { document_id: DOC, title: TITLE, location: "עמוד 3" }, marks: [], ...extra,
  };
}

/** A time answer. `breakdown`: the blocks (undefined: an older message with no such key); `timeline`: groups. */
function timeAnswer(opts: { breakdown?: Json[]; timeline?: Json[]; text?: string | null }): Json {
  const time: Json = {
    mode: "recent", prefer_recent: true, requested: null, timeline: opts.timeline ?? [],
    text: opts.text === undefined ? null : opts.text,
  };
  if (opts.breakdown !== undefined) time.breakdown = opts.breakdown;
  return answerWith({
    markdown: "מקדם ההפחתה הוא 0.8 [S1], ומקדם נוסף הוא 0.9 [S2].",
    status: "answered", gaps: [], components: [],
    sources: [source("S1"), source("S2", { text: "מקדם נוסף 0.9." })],
    time,
  });
}

const TWO = [row("S1", FACET_A, "0.8"), row("S2", FACET_B, "0.9")];

function overflowOf(page: Page): Promise<number> {
  return page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
}

test.describe("the components table", () => {
  test("it shows the facet in the source's words, the value and a source chip under its heading", async ({ page }) => {
    const { reply } = await openAnswer(page, timeAnswer({ breakdown: [block(TWO)] }));
    const table = reply.getByTestId("breakdown");
    await expect(table).toBeVisible();
    await expect(table.getByRole("heading", { name: "רכיבי השומה" })).toBeVisible();
    await expect(table.getByRole("columnheader", { name: "רכיב או שיקול (כלשון המקור)" })).toBeVisible();
    const rows = table.getByTestId("breakdown-row");
    await expect(rows).toHaveCount(2);
    await expect(rows.nth(0).getByTestId("breakdown-facet")).toHaveText(FACET_A); // server order, the whole text
    await expect(rows.nth(1).getByTestId("breakdown-facet")).toHaveText(FACET_B);
    await expect(rows.nth(0)).toContainText("0.8");
    await expect(rows.nth(1)).toContainText("0.9");
    await expect(rows.nth(0).getByRole("button", { name: `מקור S1: ${TITLE}` })).toBeVisible();
    await expect(table.getByTestId("breakdown-block")).toContainText("31/03/2025");
    // it is not the request's component list
    await expect(table.getByText("רכיבי הבקשה")).toHaveCount(0);
    await rows.nth(1).getByRole("button", { name: /^מקור S2/ }).click();
    await expect(sourceViewer(page)).toBeVisible();
  });

  test("a row whose source states no facet says so", async ({ page }) => {
    const { reply } = await openAnswer(page, timeAnswer({ breakdown: [block([row("S1", FACET_A, "0.8"), row("S2", NO_FACET, "0.9")])] }));
    await expect(reply.getByTestId("breakdown-facet").nth(1)).toHaveText(NO_FACET);
  });

  test("it follows the timeline in reading order, and both can be on screen at once", async ({ page }) => {
    const timeline = [{ label: "מקדם (סינתטי)", rows: [timelineRow("S1", "2023-03-31", "0.8")] }];
    const { reply } = await openAnswer(page, timeAnswer({ breakdown: [block(TWO)], timeline }));
    await expect(reply.getByTestId("timeline")).toBeVisible();
    await expect(reply.getByTestId("breakdown")).toBeVisible();
    const order = await reply.evaluate((root) => {
      const at = (sel: string) => root.querySelector(sel);
      const follows = (a: Element | null, b: Element | null) =>
        !!a && !!b && !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
      return {
        bodyThenTimeline: follows(at(".body"), at("[data-testid=timeline]")),
        timelineThenBreakdown: follows(at("[data-testid=timeline]"), at("[data-testid=breakdown]")),
        breakdownThenActions: follows(at("[data-testid=breakdown]"), at(".msg-actions")),
      };
    });
    expect(order).toEqual({ bodyThenTimeline: true, timelineThenBreakdown: true, breakdownThenActions: true });
  });

  test("the copy button appends the server's copy, and the table is not shown a second time as text", async ({ page, context }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    const { reply } = await openAnswer(page, timeAnswer({ breakdown: [block(TWO)], text: COPY_TEXT }));
    await page.evaluate(() => {
      const w = window as unknown as { __copied?: string };
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: { writeText: async (t: string) => { w.__copied = t; } },
      });
    });
    await reply.getByRole("button", { name: "העתקת התשובה" }).click();
    await expect.poll(() => page.evaluate(() => (window as unknown as { __copied?: string }).__copied ?? "")).toContain(COPY_TEXT);
    await expect(page.getByText(COPY_TEXT)).toHaveCount(0);
  });
});

test.describe("the undecided mark", () => {
  const undecided = [
    row("S1", FACET_A, "0.8", { marks: ["undecided"], undecided_with: ["S2"] }),
    row("S2", NO_FACET, "0.9", { marks: ["undecided"], undecided_with: ["S1"] }),
  ];

  test("its badge is followed by the counterpart as a source chip that opens the viewer", async ({ page }) => {
    const { reply } = await openAnswer(page, timeAnswer({ breakdown: [block(undecided)] }));
    const first = reply.getByTestId("breakdown-row").first();
    await expect(first.locator("[data-mark=undecided]")).toContainText("השוואה לא הוכרעה");
    const chip = first.getByRole("button", { name: "השוואה לא הוכרעה מול S2" });
    await expect(chip).toBeVisible();
    await expect(chip).toHaveText("S2");
    // the badge comes first, then the chip; the badge text is not read a second time
    expect(await first.locator(".tl-undecided").evaluate((el) => el.children[0].textContent)).toBe("השוואה לא הוכרעה");
    await expect(first.locator(".tl-undecided .badge")).toHaveAttribute("aria-hidden", "true");
    await chip.click();
    await expect(sourceViewer(page)).toBeVisible();
    // the second row names the first
    await expect(reply.getByTestId("breakdown-row").nth(1).getByRole("button", { name: "השוואה לא הוכרעה מול S1" })).toBeVisible();
  });

  test("a timeline row carries the same badge and chip", async ({ page }) => {
    const timeline = [{
      label: "מקדם (סינתטי)",
      rows: [timelineRow("S1", "2025-03-31", "0.8", { marks: ["undecided"], undecided_with: ["S2"] }),
        timelineRow("S2", "2023-03-31", "0.8")],
    }];
    const { reply } = await openAnswer(page, timeAnswer({ timeline }));
    const first = reply.getByTestId("timeline-row").first();
    await expect(first.getByRole("button", { name: "השוואה לא הוכרעה מול S2" })).toBeVisible();
    await expect(reply.getByTestId("breakdown")).toHaveCount(0);
  });

  test("an undecided mark without a counterpart is still a badge", async ({ page }) => {
    const { reply } = await openAnswer(page, timeAnswer({ breakdown: [block([row("S1", FACET_A, "0.8", { marks: ["undecided"] }), row("S2", FACET_B, "0.9")])] }));
    await expect(reply.locator("[data-mark=undecided]")).toHaveText("השוואה לא הוכרעה");
  });
});

test.describe("the conflict mark", () => {
  // two appraisals' components blocks: each conflicting row names the other, which sits in another block
  const blocks = [
    block([row("S1", FACET_A, "0.8", { marks: ["conflict"], conflict_with: ["S2"] })]),
    block([row("S2", FACET_A, "0.85", { marks: ["conflict"], conflict_with: ["S1"] })]),
  ];

  test("its badge is followed by the counterpart as a source chip that opens the viewer", async ({ page }) => {
    const { reply } = await openAnswer(page, timeAnswer({ breakdown: blocks }));
    const first = reply.getByTestId("breakdown-row").first();
    await expect(first.locator("[data-mark=conflict]")).toContainText("סתירה בין מקורות");
    const chip = first.getByRole("button", { name: "סתירה מול S2" });
    await expect(chip).toBeVisible();
    await expect(chip).toHaveText("S2");
    expect(await first.locator("[data-mark=conflict]").evaluate((el) => el.children[0].textContent)).toBe("סתירה בין מקורות");
    await expect(first.locator("[data-mark=conflict] .badge")).toHaveAttribute("aria-hidden", "true");
    await chip.click();
    await expect(sourceViewer(page)).toBeVisible();
    await expect(reply.getByTestId("breakdown-row").nth(1).getByRole("button", { name: "סתירה מול S1" })).toBeVisible();
  });

  test("a timeline row carries it too, and at 375px the chips stay inside the viewport", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    const timeline = [{
      label: "מקדם (סינתטי)",
      rows: [timelineRow("S1", "2025-03-31", "0.8", { marks: ["conflict"], conflict_with: ["S2"] }),
        timelineRow("S2", "2025-03-31", "0.85", { marks: ["conflict"], conflict_with: ["S1"] })],
    }];
    const { reply } = await openAnswer(page, timeAnswer({ timeline }));
    const first = reply.getByTestId("timeline-row").first();
    const chip = first.getByRole("button", { name: "סתירה מול S2" });
    await expect(chip).toBeVisible();
    expect(await overflowOf(page)).toBeLessThanOrEqual(0);
    const box = await chip.boundingBox();
    expect((box?.x ?? -1)).toBeGreaterThanOrEqual(0);
    expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(375);
  });

  test("a conflict mark without a counterpart (an older message) is still a badge", async ({ page }) => {
    const timeline = [{
      label: "מקדם (סינתטי)",
      rows: [timelineRow("S1", "2025-03-31", "0.8", { marks: ["conflict"] }), timelineRow("S2", "2025-03-31", "0.85", { marks: ["conflict"] })],
    }];
    const { reply } = await openAnswer(page, timeAnswer({ timeline }));
    await expect(reply.locator("[data-mark=conflict]")).toHaveCount(2);
    await expect(reply.getByRole("button", { name: /^סתירה מול/ })).toHaveCount(0);
  });
});

test.describe("at 375px and in RTL", () => {
  test("the table stacks inside the viewport, a long facet wraps whole, and the text runs right to left", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    const rows = [row("S1", FACET_A, "0.8", { marks: ["undecided"], undecided_with: ["S2"] }), row("S2", FACET_B, "0.9")];
    const { reply } = await openAnswer(page, timeAnswer({ breakdown: [block(rows)] }));
    const table = reply.getByTestId("breakdown");
    await expect(table).toBeVisible();
    expect(await overflowOf(page)).toBeLessThanOrEqual(0);
    const long = reply.getByTestId("breakdown-facet").nth(1);
    await expect(long).toHaveText(FACET_B); // not truncated
    const box = await long.boundingBox();
    expect(box?.x ?? -1).toBeGreaterThanOrEqual(0);
    expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(375);
    expect(await long.evaluate((el) => getComputedStyle(el).overflowWrap)).toBe("anywhere");
    expect(await long.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
    expect(await long.evaluate((el) => getComputedStyle(el).direction)).toBe("rtl");
    // the facet, then the value, then the citation and the marks, one under the other
    const first = reply.getByTestId("breakdown-row").first();
    const facet = await first.locator(".tl-facet").boundingBox();
    const value = await first.locator(".tl-value").boundingBox();
    const marks = await first.locator(".tl-marks").boundingBox();
    expect(value?.y ?? 0).toBeGreaterThanOrEqual((facet?.y ?? 0) + (facet?.height ?? 0) - 1);
    expect(marks?.y ?? 0).toBeGreaterThanOrEqual((value?.y ?? 0) + (value?.height ?? 0) - 1);
    // the undecided chip is reachable and opens the viewer on a phone too
    await first.getByRole("button", { name: "השוואה לא הוכרעה מול S2" }).click();
    await expect(sourceViewer(page)).toBeVisible();
  });

  test("at desktop width it is a table whose first column is the facet", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    const { reply } = await openAnswer(page, timeAnswer({ breakdown: [block(TWO)] }));
    const table = reply.getByTestId("breakdown").getByRole("table");
    await expect(table).toBeVisible();
    const head = await table.getByRole("columnheader").first().boundingBox();
    const second = await table.getByRole("columnheader").nth(1).boundingBox();
    // RTL: the first column is the rightmost
    expect(head?.x ?? 0).toBeGreaterThan(second?.x ?? 0);
    expect(await table.evaluate((el) => getComputedStyle(el).direction)).toBe("rtl");
    expect(await overflowOf(page)).toBeLessThanOrEqual(0);
  });
});

test.describe("an older message", () => {
  test("without `breakdown` it renders as before: the timeline alone, no components table", async ({ page }) => {
    const timeline = [{ label: "מקדם (סינתטי)", rows: [timelineRow("S1", "2025-03-31", "0.8"), timelineRow("S2", "2023-03-31", "0.7")] }];
    const { reply } = await openAnswer(page, timeAnswer({ timeline }));
    await expect(reply.getByTestId("timeline")).toBeVisible();
    await expect(reply.getByTestId("timeline-row")).toHaveCount(2);
    await expect(reply.getByTestId("breakdown")).toHaveCount(0);
    await expect(reply.getByText("רכיבי השומה")).toHaveCount(0);
    await expect(reply.locator("[data-mark=undecided]")).toHaveCount(0);
  });

  test("an empty `breakdown`, or no `time` at all, shows no table", async ({ page }) => {
    const { reply } = await openAnswer(page, timeAnswer({ breakdown: [] }));
    await expect(reply.getByTestId("breakdown")).toHaveCount(0);
    const bare = answerWith({ markdown: "תשובה ישנה [S1].", status: "answered", gaps: [], components: [] });
    const { reply: old } = await openAnswer(page, bare);
    await expect(old.getByTestId("breakdown")).toHaveCount(0);
    await expect(assistantMessages(page).last()).toBeVisible();
  });
});
