import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  cutMarker,
  openAnswer,
  TABLE_CUT_CLAIM,
  TABLE_CUT_ORIGINAL,
  TABLE_CUT_REASON,
  TABLE_HEADERS,
  TABLE_NOT_VERIFIED,
  TABLE_PARTIAL_CLAIM,
  TABLE_SUPPORTED,
  tableAnswer,
  tableDiagnostics,
} from "./answer-fixtures";

// Server additions inside the cells of a GFM table (round 21 U10, R14). The conversation is real (office B); its messages and
// the diagnostics route are fixed synthetic payloads in the shape the server writes (`compose.ServerSpan`): one table cell is
// cut to the cut marker «[1: חלק שלא אומת הושמט]» and another carries the partial mark " *(אומת חלקית)*" inside it. The test
// checks that the table keeps its columns and direction, that each mark stays in its own <td> and opens its own detail, and
// that on a phone the table scrolls inside its own container instead of widening the page.

/** Opens the answer with the diagnostics route served and returns the pieces the checks use. */
async function openTable(page: Page) {
  const { id, reply } = await openAnswer(page, tableAnswer(), { can_view_diagnostics: true });
  await page.route("**/api/chat/messages/m2/diagnostics", (route) => route.fulfill({ json: tableDiagnostics() }));
  return { id, reply, table: reply.locator(".body .table-wrap table") };
}

const cutMark = (reply: Locator) => reply.getByTestId("cut-marker");
const partialMark = (reply: Locator) => reply.getByTestId("partial-mark");
const cellOf = (mark: Locator) => mark.locator("xpath=ancestor::td[1]");
const cellIndex = (cell: Locator) => cell.evaluate((td) => (td as HTMLTableCellElement).cellIndex);
const rowIndex = (cell: Locator) => cell.evaluate((td) => (td.parentElement as HTMLTableRowElement).rowIndex);

async function pageOverflow(page: Page): Promise<number> {
  return page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
}

test.describe("server additions in table cells", () => {
  test("the table renders with its columns in order and right to left, each mark inside its own cell", async ({ page }) => {
    const { id, reply, table } = await openTable(page);
    await expect(table).toHaveCount(1);
    // the columns, in the order the server wrote them
    const headers = table.locator("thead th");
    await expect(headers).toHaveText(TABLE_HEADERS);
    await expect(table.locator("tbody tr")).toHaveCount(2);
    await expect(table.locator("tbody tr").first().locator("td")).toHaveCount(TABLE_HEADERS.length);
    // right to left: the first column is the right-most, and each next column lies to the left of the one before
    expect(await table.evaluate((t) => getComputedStyle(t).direction)).toBe("rtl");
    const xs: number[] = [];
    for (let i = 0; i < TABLE_HEADERS.length; i++) xs.push((await headers.nth(i).boundingBox())!.x);
    for (let i = 1; i < xs.length; i++) expect(xs[i]).toBeLessThan(xs[i - 1]);
    // the cells keep their values under their own column
    const first = table.locator("tbody tr").nth(0).locator("td");
    await expect(first.nth(0)).toHaveText("נכס א׳");
    await expect(first.nth(1)).toContainText("9,500 ₪");
    await expect(first.nth(3)).toHaveText("120");
    const second = table.locator("tbody tr").nth(1).locator("td");
    await expect(second.nth(0)).toHaveText("נכס ב׳");
    await expect(second.nth(2)).toContainText("7%");
    await expect(second.nth(3)).toHaveText("95");
    // the server's two additions are inside the table, not beside it; the text around the table is intact
    await expect(table.getByTestId("cut-marker")).toHaveCount(1);
    await expect(table.getByTestId("partial-mark")).toHaveCount(1);
    await expect(cutMark(reply)).toHaveCount(1);
    await expect(partialMark(reply)).toHaveCount(1);
    await expect(reply.locator(".body")).toContainText("להלן נתוני שני הנכסים שנבדקו:");
    await expect(reply.locator(".body")).toContainText("הנתונים לפי השומה הסינתטית.");
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("the cut cell shows the marker, and opening it shows the removal detail", async ({ page }) => {
    const { id, reply, table } = await openTable(page);
    const mark = cutMark(reply);
    // the marker is the whole content of its cell: row 1, third column, under «שיעור היוון»
    const cell = cellOf(mark);
    await expect(cell).toHaveText(cutMarker(1));
    expect(await cellIndex(cell)).toBe(2);
    expect(await rowIndex(cell)).toBe(1);
    await expect(table.locator("thead th").nth(2)).toHaveText("שיעור היוון");
    await expect(mark).toHaveAttribute("data-ordinal", "1");
    await expect(mark).toBeEnabled();
    // the model's draft is not in the table; the details are closed until the marker is used
    await expect(reply.locator(".body")).not.toContainText(TABLE_CUT_ORIGINAL);
    await expect(reply.locator("details.msg-details")).not.toHaveAttribute("open", "");
    await mark.click();
    await expect(reply.locator("details.msg-details")).toHaveAttribute("open", "");
    const entry = reply.locator(`[data-testid="removal"][data-claim-id="${TABLE_CUT_CLAIM}"]`);
    await expect(entry).toHaveAttribute("data-focused", "true");
    await expect(entry.getByTestId("removal-draft")).toContainText(TABLE_CUT_ORIGINAL);
    await expect(entry.getByTestId("change-kept")).toContainText(cutMarker(1));
    await expect(entry.getByTestId("removal-reason")).toContainText(TABLE_CUT_REASON);
    // the partly verified cell's entry is not the one that opened
    await expect(reply.locator(`[data-claim-id="${TABLE_PARTIAL_CLAIM}"][data-focused="true"]`)).toHaveCount(0);
    // the detail is only in the details, never in the table
    await expect(table).not.toContainText(TABLE_CUT_ORIGINAL);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("the partial mark is inside its cell, and opening it shows the partly verified sentence", async ({ page }) => {
    const { id, reply, table } = await openTable(page);
    const mark = partialMark(reply);
    await expect(mark).toHaveText("(אומת חלקית)");
    // the same <td> holds the value it qualifies and the mark: row 2, second column, under «שווי למ״ר»
    const cell = cellOf(mark);
    await expect(cell).toContainText("8,200 ₪");
    await expect(cell).toContainText("(אומת חלקית)");
    expect(await cellIndex(cell)).toBe(1);
    expect(await rowIndex(cell)).toBe(2);
    await expect(table.locator("thead th").nth(1)).toHaveText("שווי למ״ר");
    // the mark follows the value in the cell's reading order, and the next cell is not touched by it
    const text = (await cell.innerText()).replace(/\s+/g, " ").trim();
    expect(text.indexOf("8,200")).toBeLessThan(text.indexOf("(אומת חלקית)"));
    await expect(table.locator("tbody tr").nth(1).locator("td").nth(2)).not.toContainText("אומת חלקית");
    await expect(mark).toHaveAttribute("data-ordinal", "1");
    await expect(reply.locator("details.msg-details")).not.toHaveAttribute("open", "");
    await mark.click();
    await expect(reply.locator("details.msg-details")).toHaveAttribute("open", "");
    const entry = reply.locator(`[data-testid="change"][data-claim-id="${TABLE_PARTIAL_CLAIM}"]`);
    await expect(entry).toBeVisible();
    await expect(entry.getByTestId("partial-supported")).toContainText(TABLE_SUPPORTED);
    await expect(entry.getByTestId("partial-not-verified")).toContainText(TABLE_NOT_VERIFIED);
    await expect(entry).toBeFocused();
    // the cut cell's entry did not open
    await expect(reply.locator(`[data-testid="removal"][data-claim-id="${TABLE_CUT_CLAIM}"] [data-testid="removal-draft"]`))
      .toHaveCount(0);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a viewer who may not open the diagnostics sees the partial mark as text in its cell", async ({ page }) => {
    const { id, reply } = await openAnswer(page, tableAnswer(), { can_view_diagnostics: false });
    await expect(partialMark(reply)).toHaveCount(0);
    const cell = reply.locator(".body td").filter({ hasText: "8,200 ₪" });
    await expect(cell).toHaveCount(1);
    await expect(cell).toContainText("(אומת חלקית)");
    await expect(cutMark(reply)).toHaveCount(1);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });
});

// --- mobile -------------------------------------------------------------------------------------------------------

test.describe("server additions in table cells (mobile)", () => {
  test.use({ viewport: { width: 375, height: 812 }, hasTouch: true });

  test("the five-column table stays inside its own scroll container, not the page, and both details still open", async ({ page }) => {
    const { id, reply, table } = await openTable(page);
    expect(await pageOverflow(page)).toBeLessThanOrEqual(0);
    // the existing design: `.table-wrap` scrolls sideways when the table is wider than it (cells wrap first, so five columns may
    // still fit at 375px); either way the container stays within the screen and the page does not widen
    const wrap = reply.locator(".body .table-wrap");
    expect(await wrap.evaluate((el) => getComputedStyle(el).overflowX)).toBe("auto");
    const sizes = await wrap.evaluate((el) => ({ client: el.clientWidth, scroll: el.scrollWidth, view: window.innerWidth }));
    expect(sizes.client).toBeLessThanOrEqual(sizes.view);
    expect(sizes.scroll).toBeGreaterThanOrEqual(sizes.client);
    // the columns keep their order in the scrolled container (right to left)
    await expect(table.locator("thead th")).toHaveText(TABLE_HEADERS);
    // the cut marker opens its removal detail
    await cutMark(reply).click();
    const cut = reply.locator(`[data-testid="removal"][data-claim-id="${TABLE_CUT_CLAIM}"]`);
    await expect(cut.getByTestId("removal-draft")).toContainText(TABLE_CUT_ORIGINAL);
    expect(await pageOverflow(page)).toBeLessThanOrEqual(0);
    // the partial mark opens its own
    await partialMark(reply).click();
    const part = reply.locator(`[data-testid="change"][data-claim-id="${TABLE_PARTIAL_CLAIM}"]`);
    await expect(part.getByTestId("partial-supported")).toContainText(TABLE_SUPPORTED);
    await expect(part.getByTestId("partial-not-verified")).toContainText(TABLE_NOT_VERIFIED);
    expect(await pageOverflow(page)).toBeLessThanOrEqual(0);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });
});
