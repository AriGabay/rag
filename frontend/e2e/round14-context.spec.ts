import { expect, test, type Locator, type Page } from "@playwright/test";
import { sourceViewer } from "./helpers";
import {
  answerWith, anchorAt, contextComponent, derivedComputation, diagnostics, DOC, NOT_VERIFIED, openAnswer, partialAnswer,
  partialDecision, PARTIAL_SENTENCE, PARTIAL_SENTENCE_2, rowState, source, SUPPORTED, TITLE, WHY_CHECK, type Json,
} from "./answer-fixtures";

// Round 14 U8: the components table and the timeline read the row's structured `state` (R11, R12, KTD8), and a "verified in part"
// mark opens the verification detail of its claims (R14, KTD9). Payloads are fixed and synthetic (invented facets, quotes,
// titles and coefficients); the specs check how the screen shows what the server sent — the words of a state, the context line
// and the chips that open the value's and the context's places, the reconciled adjustment, history rows and off-screen
// counterparts, the partial mark as a button for the message's owner and as text for anyone else, the layout at 375px in RTL, and
// an answer stored before round 14 — not what the server derives. Only the office B admin logs in; the API answers are stubbed.

const FACET = "הפחתה בשל מגבלות בנייה";
const NOT_READ = "ההקשר לא נקרא";
const NONE_IN_RANGE = "המקור לא מציין רכיב בקטע שנקרא";
const LEGACY_NONE = "רכיב לא צוין במקור";
const CTX_LOCATION = "משפט קודם באותה פסקה";
const CTX_QUOTE = "ההפחתה שלהלן נובעת ממגבלות הבנייה החלות על המגרש";
const VALUE_LABEL = "עמוד 3, סעיף «4. שיטת ההיוון»";
const CTX_LABEL = "עמוד 2, סעיף «3. הפחתות»";
const CTX_ANCHOR = anchorAt(CTX_LABEL, 2, 5);

function row(sid: string, facet: string, value: string, extra: Json = {}): Json {
  return {
    facet, value_text: value, unit: null, sid, source: { document_id: DOC, title: TITLE, location: "עמוד 3" }, marks: [], ...extra,
  };
}

function block(rows: Json[]): Json {
  return {
    label: `מקדם (סינתטי) — ${TITLE}`,
    as_of: { date: "2025-03-31", precision: "day", end: null, date_kind: "valuation" },
    rows,
  };
}

function timelineRow(sid: string, date: string, value: string, extra: Json = {}): Json {
  return {
    date, precision: "day", end: null, date_kind: "valuation", value_text: value, unit: null, sid,
    source: { document_id: DOC, title: TITLE, location: "עמוד 3" }, marks: [], ...extra,
  };
}

function timeAnswer(opts: { breakdown?: Json[]; timeline?: Json[]; computations?: Json[] }): Json {
  const time: Json = { mode: "recent", prefer_recent: true, requested: null, timeline: opts.timeline ?? [], text: null };
  if (opts.breakdown !== undefined) time.breakdown = opts.breakdown;
  return answerWith({
    markdown: "מקדם ההפחתה הוא 0.9 [S1], ומקדם נוסף הוא 0.8 [S2].",
    status: "answered", gaps: [], components: [],
    sources: [source("S1"), source("S2", { text: "מקדם נוסף 0.8." })],
    computations: opts.computations ?? [],
    time,
  });
}

function overflowOf(page: Page): Promise<number> {
  return page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
}

function contextRow(extra: Json = {}, quote: string | null = CTX_QUOTE): Json {
  return row("S1", FACET, "0.9", {
    state: rowState({ component: contextComponent(CTX_LOCATION, quote, CTX_ANCHOR) }), ...extra,
  });
}

// --- the component cell: the state's words, then the context ---------------------------------------------------------

test.describe("the component cell", () => {
  test("a context that was not read says so in the server's words, and never that the source states no component", async ({ page }) => {
    const state = rowState({ component: { status: "not_read", reason: "picture_cell", written: null, part: null, tier: null,
      context: null } });
    const { reply } = await openAnswer(page, timeAnswer({ breakdown: [block([
      row("S1", NOT_READ, "0.9", { state }), row("S2", NONE_IN_RANGE, "0.8",
        { state: rowState({ component: { status: "none", reason: "not_in_range", written: null, part: null, tier: null,
          context: null } }) })])] }));
    const facets = reply.getByTestId("breakdown-facet");
    await expect(facets.nth(0)).toHaveText(NOT_READ);
    await expect(facets.nth(1)).toHaveText(NONE_IN_RANGE);
    await expect(reply.getByTestId("breakdown")).not.toContainText(LEGACY_NONE);
    // no context was read, so there is no context line and no context chip
    await expect(reply.getByTestId("breakdown-context")).toHaveCount(0);
    await expect(reply.getByTestId("context-chip")).toHaveCount(0);
  });

  test("a context shows its place and its words after the component, and a chip that opens the context's place", async ({ page }) => {
    const { reply } = await openAnswer(page, timeAnswer({ breakdown: [block([contextRow(), row("S2", FACET, "0.8")])] }));
    const first = reply.getByTestId("breakdown-row").first();
    await expect(first.getByTestId("breakdown-facet")).toHaveText(FACET); // the component's words, alone
    const line = first.getByTestId("breakdown-context");
    await expect(line).toContainText(CTX_LOCATION);
    await expect(line).toContainText(CTX_QUOTE);
    // the facet, the context line, then the chip, in that order in the cell
    const order = await first.locator(".tl-facet").evaluate((cell) => {
      const at = (sel: string) => cell.querySelector(sel);
      const follows = (a: Element | null, b: Element | null) =>
        !!a && !!b && !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
      return follows(at("[data-testid=breakdown-facet]"), at("[data-testid=breakdown-context]")) &&
        follows(at("[data-testid=breakdown-context]"), at("[data-testid=context-chip]"));
    });
    expect(order).toBe(true);
    const chip = first.getByTestId("context-chip");
    await expect(chip).toHaveText("הקשר");
    await expect(chip).toHaveAccessibleName(new RegExp(CTX_LOCATION));
    await expect(chip).toHaveAccessibleName(new RegExp(CTX_LABEL));
    // a row without a context has neither
    await expect(reply.getByTestId("breakdown-row").nth(1).getByTestId("context-chip")).toHaveCount(0);
  });

  test("the context chip opens the viewer at the context's place and the value chip at the value's, under different names", async ({ page }) => {
    const { reply } = await openAnswer(page, timeAnswer({ breakdown: [block([contextRow(), row("S2", FACET, "0.8")])] }));
    const first = reply.getByTestId("breakdown-row").first();
    const ctx = first.getByTestId("context-chip");
    const value = first.getByRole("button", { name: `מקור S1: ${TITLE}` });
    expect(await ctx.getAttribute("aria-label")).not.toBe(await value.getAttribute("aria-label"));
    await ctx.click();
    await expect(sourceViewer(page)).toBeVisible();
    await expect(sourceViewer(page).getByTestId("viewer-location")).toContainText(CTX_LABEL);
    await value.click();
    await expect(sourceViewer(page).getByTestId("viewer-location")).toContainText(VALUE_LABEL);
    await expect(sourceViewer(page).getByTestId("viewer-location")).not.toContainText(CTX_LABEL);
    await ctx.click();
    await expect(sourceViewer(page).getByTestId("viewer-location")).toContainText(CTX_LABEL);
  });

  test("a context whose place could not be located shows its line and no chip", async ({ page }) => {
    const unlocated = row("S1", FACET, "0.9", { state: rowState({ component: contextComponent(CTX_LOCATION, CTX_QUOTE, null) }) });
    const { reply } = await openAnswer(page, timeAnswer({ breakdown: [block([unlocated, row("S2", FACET, "0.8")])] }));
    await expect(reply.getByTestId("breakdown-context").first()).toContainText(CTX_LOCATION);
    await expect(reply.getByTestId("context-chip")).toHaveCount(0);
  });

  test("a quote longer than 120 characters is clipped on the screen too", async ({ page }) => {
    const long = "ז".repeat(300);
    const { reply } = await openAnswer(page, timeAnswer({ breakdown: [block([contextRow({}, long), row("S2", FACET, "0.8")])] }));
    const text = (await reply.getByTestId("breakdown-context").first().innerText()).replace(/\s+/g, " ");
    expect((text.match(/ז/g) ?? []).length).toBeLessThanOrEqual(120);
    expect((text.match(/ז/g) ?? []).length).toBeGreaterThan(100);
  });
});

// --- AE6: a coefficient and the reduction derived from it are one adjustment -------------------------------------------

test.describe("a reconciled adjustment", () => {
  const reconciled = (derived: string | null) => row("S1", FACET, "0.9", {
    reduction: { sid: "S2", value_text: "10%", unit: null, source: { document_id: DOC, title: TITLE, location: "עמוד 3" } },
    derived, state: rowState({ comparison: { status: "same", with: ["S2"] } }),
  });

  test("shows both forms, the coefficient first, the formula and a chip to the calculation, and no badge", async ({ page }) => {
    const { reply } = await openAnswer(page, timeAnswer({ breakdown: [block([reconciled("C1"), row("S3", "הפחתה אחרת", "0.7")])],
      computations: [derivedComputation("C1", "מקדם ההפחתה")] }));
    const first = reply.getByTestId("breakdown-row").first();
    const forms = first.getByTestId("reconciled-form");
    await expect(forms).toHaveCount(2);
    await expect(forms.nth(0)).toContainText("0.9");
    await expect(forms.nth(1)).toContainText("10%");
    // each form has its own value chip: the coefficient's S1 and the reduction's S2
    await expect(forms.nth(0).getByRole("button", { name: `מקור S1: ${TITLE}` })).toBeVisible();
    await expect(forms.nth(1).getByRole("button", { name: `מקור S2: ${TITLE}` })).toBeVisible();
    const formula = first.getByTestId("reconciled-formula");
    await expect(formula).toContainText("1 − 0.9");
    expect(await first.locator("[data-mark]").count()).toBe(0);
    await formula.getByRole("button", { name: /C1/ }).click();
    await expect(page.getByTestId("calc-view")).toHaveAttribute("data-id", "C1");
  });

  test("a value chip of either form opens the viewer", async ({ page }) => {
    const { reply } = await openAnswer(page, timeAnswer({ breakdown: [block([reconciled("C1"), row("S3", "הפחתה אחרת", "0.7")])],
      computations: [derivedComputation("C1", "מקדם ההפחתה")] }));
    await reply.getByTestId("reconciled-form").nth(1).getByRole("button", { name: /^מקור S2/ }).click();
    await expect(sourceViewer(page)).toBeVisible();
  });

  test("a formula whose calculation is not registered yet shows the text and no chip", async ({ page }) => {
    const { reply } = await openAnswer(page, timeAnswer({ breakdown: [block([reconciled(null), row("S3", "הפחתה אחרת", "0.7")])] }));
    const formula = reply.getByTestId("reconciled-formula");
    await expect(formula).toContainText("1 − 0.9");
    await expect(formula.getByRole("button")).toHaveCount(0);
  });

  test("a pair that does not reconcile stays two rows with their conflict badges", async ({ page }) => {
    const conflict = [
      row("S1", FACET, "0.9", { marks: ["conflict"], conflict_with: ["S2"],
        state: rowState({ comparison: { status: "conflict", with: ["S2"] } }) }),
      row("S2", FACET, "15%", { marks: ["conflict"], conflict_with: ["S1"],
        state: rowState({ comparison: { status: "conflict", with: ["S1"] } }) }),
    ];
    const { reply } = await openAnswer(page, timeAnswer({ breakdown: [block(conflict)] }));
    await expect(reply.getByTestId("breakdown-row")).toHaveCount(2);
    await expect(reply.locator("[data-mark=conflict]")).toHaveCount(2);
    await expect(reply.getByTestId("reconciled-form")).toHaveCount(0);
    await expect(reply.getByRole("button", { name: "סתירה מול S2" })).toBeVisible();
  });
});

// --- history rows and counterparts that are not in the answer --------------------------------------------------------------

test.describe("history and off-screen counterparts", () => {
  const NOTE = "נתון היסטורי, לא נכלל במסקנת התשובה";

  test("a history row carries the server's note as a small label", async ({ page }) => {
    const timeline = [{ label: "מקדם (סינתטי)", rows: [
      timelineRow("S1", "2025-03-31", "0.9", { state: rowState() }),
      timelineRow("S2", "2023-03-31", "0.8", { note: NOTE, state: rowState({ support: "history", claim: null }) }),
    ] }];
    const { reply } = await openAnswer(page, timeAnswer({ timeline }));
    const rows = reply.getByTestId("timeline-row");
    await expect(rows.nth(1).getByTestId("row-note")).toHaveText(NOTE);
    await expect(rows.nth(0).getByTestId("row-note")).toHaveCount(0);
  });

  test("a counterpart the answer does not use is text with its title and location, not a chip", async ({ page }) => {
    const away = { sid: "S9", title: "שומה סינתטית אחרת", location: "עמוד 7" };
    const rows = [
      row("S1", FACET, "0.9", { marks: ["conflict"], not_in_answer_with: [away],
        state: rowState({ comparison: { status: "conflict", with: [] } }) }),
      row("S2", "הפחתה אחרת", "0.8"),
    ];
    const { reply } = await openAnswer(page, timeAnswer({ breakdown: [block(rows)] }));
    const first = reply.getByTestId("breakdown-row").first();
    await expect(first.getByTestId("not-in-answer")).toHaveText("ערך זה אינו בתשובה: שומה סינתטית אחרת, עמוד 7");
    await expect(first.getByRole("button", { name: /S9/ })).toHaveCount(0);
    await expect(first.locator("[data-mark=conflict]")).toContainText("סתירה בין מקורות");
  });
});

// --- 375px, RTL --------------------------------------------------------------------------------------------------------------

test.describe("at 375px and in RTL", () => {
  test("the context line wraps inside the cell and the page does not scroll sideways", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    const quote = "מילה ".repeat(23).trim();
    const { reply } = await openAnswer(page, timeAnswer({ breakdown: [block([contextRow({}, quote), row("S2", FACET, "0.8")])] }));
    const line = reply.getByTestId("breakdown-context").first();
    await expect(line).toBeVisible();
    expect(await overflowOf(page)).toBeLessThanOrEqual(0);
    const box = await line.boundingBox();
    expect(box?.x ?? -1).toBeGreaterThanOrEqual(0);
    expect((box?.x ?? 0) + (box?.width ?? 0)).toBeLessThanOrEqual(375);
    // it wraps: taller than a single line, and nothing spills out of its box
    expect(box?.height ?? 0).toBeGreaterThan(40);
    expect(await line.evaluate((el) => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
    expect(await line.evaluate((el) => getComputedStyle(el).direction)).toBe("rtl");
    // the chip is reachable and opens the viewer on a phone too
    await reply.getByTestId("context-chip").first().click();
    await expect(sourceViewer(page)).toBeVisible();
  });

  test("a reconciled row stays inside the viewport", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    const reconciled = row("S1", FACET, "0.9", {
      reduction: { sid: "S2", value_text: "10%", unit: null, source: { document_id: DOC, title: TITLE, location: "עמוד 3" } },
      derived: "C1", state: rowState({ comparison: { status: "same", with: ["S2"] } }),
    });
    const { reply } = await openAnswer(page, timeAnswer({ breakdown: [block([reconciled, row("S3", "הפחתה אחרת", "0.7")])],
      computations: [derivedComputation("C1", "מקדם ההפחתה")] }));
    await expect(reply.getByTestId("reconciled-formula")).toBeVisible();
    expect(await overflowOf(page)).toBeLessThanOrEqual(0);
  });
});

// --- the partial marks (R14, AE10) ---------------------------------------------------------------------------------------------

const CLAIM_1 = "k000000000a01";
const CLAIM_2 = "k000000000a02";

async function serveDiagnostics(page: Page, body: Json | number): Promise<void> {
  await page.route("**/api/chat/messages/m2/diagnostics", (route) =>
    typeof body === "number" ? route.fulfill({ status: body, json: { detail: "x" } }) : route.fulfill({ json: body }));
}

function markButtons(reply: Locator): Locator {
  return reply.getByTestId("partial-mark");
}

test.describe("a mark of a claim verified in part", () => {
  test("for the owner it is a button that opens the detail on its claim: supported, not verified, why and what was checked", async ({ page }) => {
    await serveDiagnostics(page, diagnostics([partialDecision(CLAIM_1)]));
    const { reply } = await openAnswer(page, partialAnswer([[CLAIM_1]]), { can_view_diagnostics: true });
    const mark = markButtons(reply);
    await expect(mark).toHaveCount(1);
    await expect(mark).toHaveText("(אומת חלקית)");
    await expect(reply.locator("details.msg-details")).not.toHaveAttribute("open", "");
    await mark.click();
    await expect(reply.locator("details.msg-details")).toHaveAttribute("open", "");
    const entry = reply.locator(`[data-testid=change][data-claim-id=${CLAIM_1}]`);
    await expect(entry).toBeVisible();
    await expect(entry.getByTestId("partial-supported")).toContainText(SUPPORTED);
    await expect(entry.getByTestId("partial-not-verified")).toContainText(NOT_VERIFIED);
    await expect(entry.getByTestId("partial-why")).toContainText("המקורות שנבדקו אינם מציינים זאת");
    await expect(entry.getByTestId("partial-why")).toContainText(WHY_CHECK);
    const checked = entry.getByTestId("partial-checked");
    await expect(checked).toContainText("S1");
    await expect(checked).toContainText("S2");
    await expect(checked).toContainText("לא נמצא"); // the re-read of S2 and how it ended
    // the entry took the focus
    await expect(entry).toBeFocused();
  });

  test("two marks open their own claims, and a second press moves the focus to its entry", async ({ page }) => {
    await serveDiagnostics(page, diagnostics([partialDecision(CLAIM_1), partialDecision(CLAIM_2, { not_verified: "ערך אחר שלא אומת" })]));
    const { reply } = await openAnswer(page, partialAnswer([[CLAIM_1], [CLAIM_2]], {}, [{}, { kind: "context_not_read", text: PARTIAL_SENTENCE_2 }]),
      { can_view_diagnostics: true });
    const marks = markButtons(reply);
    await expect(marks).toHaveCount(2);
    await marks.nth(0).click();
    await expect(reply.locator(`[data-testid=change][data-claim-id=${CLAIM_1}]`)).toBeFocused();
    await marks.nth(1).click();
    await expect(reply.locator(`[data-testid=change][data-claim-id=${CLAIM_2}]`)).toBeFocused();
    await expect(reply.locator(`[data-claim-id=${CLAIM_2}]`).getByTestId("partial-not-verified")).toContainText("ערך אחר שלא אומת");
  });

  test("when the diagnostics fail to load it falls back to the mark's reason sentence, and offers a retry", async ({ page }) => {
    await serveDiagnostics(page, 500);
    const { reply } = await openAnswer(page, partialAnswer([[CLAIM_1]]), { can_view_diagnostics: true });
    await markButtons(reply).click();
    await expect(reply.getByTestId("removal-error")).toBeVisible();
    await expect(reply.getByTestId("partial-reason")).toHaveText(PARTIAL_SENTENCE);
  });

  test("a diagnostics 404 falls back to the reason sentence too", async ({ page }) => {
    await serveDiagnostics(page, 404);
    const { reply } = await openAnswer(page, partialAnswer([[CLAIM_1]]), { can_view_diagnostics: true });
    await markButtons(reply).click();
    await expect(reply.getByTestId("removal-unavailable")).toBeVisible();
    await expect(reply.getByTestId("partial-reason")).toHaveText(PARTIAL_SENTENCE);
  });

  test("for a viewer who may not open diagnostics it is not a button, and the summary lists the reason sentences in mark order", async ({ page }) => {
    let asked = 0;
    await page.route("**/api/chat/messages/m2/diagnostics", (route) => { asked += 1; return route.fulfill({ status: 404, json: { detail: "x" } }); });
    const { reply } = await openAnswer(page, partialAnswer([[CLAIM_1], [CLAIM_2]], {}, [{}, { kind: "context_not_read", text: PARTIAL_SENTENCE_2 }]),
      { can_view_diagnostics: false });
    await expect(reply.locator(".body")).toContainText("(אומת חלקית)");
    await expect(markButtons(reply)).toHaveCount(0);
    await expect(reply.getByRole("button", { name: /אומת חלקית/ })).toHaveCount(0);
    await reply.locator("details.msg-details > summary").click();
    const items = reply.getByTestId("partial-reasons").locator("li");
    await expect(items).toHaveText([PARTIAL_SENTENCE, PARTIAL_SENTENCE_2]);
    // the reasons are the whole of what such a viewer gets: no request for the diagnostics was made
    expect(asked).toBe(0);
  });

  test("a message that says nothing of diagnostics is read as one the viewer may not open", async ({ page }) => {
    const { reply } = await openAnswer(page, partialAnswer([[CLAIM_1]]));
    await expect(markButtons(reply)).toHaveCount(0);
    await reply.locator("details.msg-details > summary").click();
    await expect(reply.getByTestId("partial-reasons").locator("li")).toHaveText([PARTIAL_SENTENCE]);
  });

  test("the owner's summary does not repeat the sentences the detail gives", async ({ page }) => {
    const { reply } = await openAnswer(page, partialAnswer([[CLAIM_1]]), { can_view_diagnostics: true });
    await reply.locator("details.msg-details > summary").click();
    await expect(reply.getByTestId("partial-reasons")).toHaveCount(0);
    await expect(reply.getByTestId("other-changes")).toBeVisible();
  });
});

// --- an answer stored before round 14 -----------------------------------------------------------------------------------------

test.describe("an older answer", () => {
  test("a row with no `state` renders as in round 13: the facet alone, no context, no note, no label", async ({ page }) => {
    const older = [row("S1", LEGACY_NONE, "0.9"), row("S2", FACET, "0.8")];
    const { reply } = await openAnswer(page, timeAnswer({ breakdown: [block(older)] }));
    await expect(reply.getByTestId("breakdown-facet").nth(0)).toHaveText(LEGACY_NONE);
    await expect(reply.getByTestId("breakdown-facet").nth(1)).toHaveText(FACET);
    for (const id of ["breakdown-context", "context-chip", "reconciled-form", "row-note", "not-in-answer"]) {
      await expect(reply.getByTestId(id)).toHaveCount(0);
    }
  });

  test("a mark stored without ordinals stays text even for the owner, and no reasons are listed", async ({ page }) => {
    const answer = partialAnswer([[CLAIM_1]]);
    const spans = (answer.server_spans as Json[]).map(({ start, end, kind }) => ({ start, end, kind }));
    const verification = { ...(answer.verification as Json) };
    delete verification.partials;
    const { reply } = await openAnswer(page, { ...answer, server_spans: spans, verification }, { can_view_diagnostics: true });
    await expect(reply.locator(".body")).toContainText("(אומת חלקית)");
    await expect(markButtons(reply)).toHaveCount(0);
    await reply.locator("details.msg-details > summary").click();
    await expect(reply.getByTestId("partial-reasons")).toHaveCount(0);
    await expect(reply.getByTestId("verification")).toContainText("אומתו חלקית");
  });
});
