import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  actionsFor,
  anchor,
  BASE,
  cid,
  component,
  cutMarker,
  DOC,
  message,
  openDetails,
  PARTIAL_MARK,
  partialRows,
  PNG,
  READING,
  removal,
  source,
  thread,
  TITLE,
  VER,
  withServerPieces,
  answerWith,
  type Json,
} from "./answer-fixtures";
import { assistantMessages, login, sourceViewer, USERS } from "./helpers";

// Round 22 U15: what the display keeps of the meaning the server stored. The conversation is real (office B, synthetic demo
// user) but every message is a fixed synthetic payload in the shape the server serialises (`app/chat/anchors.py` v2 table
// anchors, `compose.ServerSpan.as_dict`, `ChatValue` provenance and stated words). No chat request ever reaches the backend:
// the messages route fulfils GET and POST itself, and a guard fails the test if a send leaves for any other conversation.

// --- the stored table (served in the order it is drawn, though it was stored reversed) ------------------------------------

const HEADERS = ["נכס", "שווי למ״ר", "שיעור היוון", "שטח במ״ר", "תאריך הערכה"];
const CUT_CLAIM = cid(0x71);
const PARTIAL_CLAIM = cid(0x72);
const COND_CLAIM = cid(0x73);
const CONDITION = " (בכפוף לאישור התכנית)";
const NOTE_TEXT = "הוסרה מהתשובה טענה אחת שלא נמצאה לה תמיכה במקורות.";
const TABLE_NOTES = ["השווי נקוב לפני מע״מ", "הנתונים נכונים למועד ההערכה"];
const UNIT_NOTE = "הסכומים בשקלים חדשים";

/** The anchor of the cell V1 cites: the server's v2 table anchor of a table stored reversed and served as it is drawn
 * (`direction` as `stored_tables.Direction._asdict()` writes it), with the labels the agent was shown. */
function cellAnchor(): Json {
  const base = anchor() as { pages: Json[]; location: Json } & Json;
  return {
    ...base, v: 2, precision: "cell", precision_label: "התא מסומן בטבלה",
    pages: [{ page: 3, printed_label: null, width: 600, height: 900, rects: [[0.55, 0.4, 0.7, 0.43]], focus: [] }],
    location: { title: TITLE, label: "עמוד 3, טבלה «טבלת שוויים», שורה «נכס א׳», עמודה «שווי למ״ר»", page: 3,
      page_end: null, printed_page: null, section: null },
    table: {
      table_index: 0, title: "טבלת שוויים", row_label: "נכס א׳", row_number: 1, column_header: "שווי למ״ר", column_number: 2,
      unit_note: UNIT_NOTE, source: null, direction: { reversed: true, basis: "boxes", inferred: true },
      header: { page: 3, rects: [[0.55, 0.35, 0.7, 0.38]] }, notes: TABLE_NOTES.map((text) => ({ text, page: 3 })),
    },
  };
}

function cellValue(): Json {
  return {
    id: "V1", value: "9500", value_text: "9,500 ₪", label: "שווי למ״ר של נכס א׳", source_id: "S2", document_id: DOC,
    version_id: VER, reading_id: READING, title: TITLE, location: "עמוד 3, טבלה", kind: "amount", unit: "ILS",
    unit_label: "₪", period: "none", vat: "excluded", area_basis: "", subject: "נכס א׳", role: "unknown",
    provenance: { unit: "source", period: "not_stated", vat: "source" }, certainty: "verified",
    locator: { row: "נכס א׳", column: "שווי למ״ר", row_number: 1, column_number: 2 }, quote: "נכס א׳ | 9,500 ₪",
    total: false, approx: false, anchor: cellAnchor(),
  };
}

/** A stored answer with a GFM table, as `compose` writes it: a cut marker in a cell (row 1), a partial mark and a condition
 * written INSIDE the cells of row 2, the cited cell V1 in row 1, and the removal note after the text. */
function tableAnswer(): Json {
  const head = `| ${HEADERS.join(" | ")} |\n| ${HEADERS.map(() => "---").join(" | ")} |\n`;
  const { markdown, server_spans } = withServerPieces([
    `להלן נתוני שני הנכסים שנבדקו:\n\n${head}| נכס א׳ | 9,500 ₪ [V1] | `,
    { text: cutMarker(1), span: { kind: "marker", claim_ids: [CUT_CLAIM], datum: "V2" } },
    " | 120 | 1.1.2025 |\n| נכס ב׳ | 8,200 ₪ [S1]",
    { text: PARTIAL_MARK, span: { kind: "partial_mark", ordinal: 1, claim_ids: [PARTIAL_CLAIM], datum: "V3" } },
    " | 7%",
    { text: CONDITION, span: { kind: "qualifier", claim_ids: [COND_CLAIM], datum: "V4", addition: "condition" } },
    " [S1] | 95 | 1.1.2025 |\n\nהנתונים לפי השומה הסינתטית.",
    { text: `\n\n> ${NOTE_TEXT}`, span: { kind: "removal_note" } },
  ]);
  const removals = [removal("absent_from_source", null, { claim_id: CUT_CLAIM, action: "cut", ordinal: 1 })];
  return answerWith(
    {
      markdown, server_spans, status: "answered", gaps: [],
      sources: [source("S1"), source("S2", { anchor: cellAnchor(), table_index: 0, kind: "table" })],
      values: [cellValue()], components: [component("N1", "השווי למ״ר", "full", { units: [0], related: ["S1"] })],
    },
    {
      removed: 0, partial: 1, correctness: "partial", removals, partials: partialRows([{ claims: [PARTIAL_CLAIM] }]),
      actions: actionsFor(removals, { qualified: 1 }), completeness: { status: "full", requirements: 1, missing: [] },
    },
  );
}

// --- serving one conversation --------------------------------------------------------------------------------------------

interface Served {
  id: string;
  reply: Locator;
  state: { items: Json[]; gets: number; posts: Json[]; leaked: string[] };
}

const QUESTION = "מה השווי למ״ר ומה שיעור ההיוון של שני הנכסים?";

/** Logs in as the synthetic office B admin, creates an empty conversation (no model call), and serves `items` as its messages.
 * A POST is answered by `onSend` (it may extend `state.items`) and never reaches the backend; without `onSend` it is refused. */
async function serve(
  page: Page,
  items: Json[],
  opts: { onSend?: (body: Json, state: Served["state"]) => Json } = {},
): Promise<Served> {
  await login(page, USERS.adminB);
  const { id } = await (await page.request.post("/api/chat/conversations")).json();
  const state: Served["state"] = { items, gets: 0, posts: [], leaked: [] };
  // the guard goes first (so it is consulted last): a send or retry that no mock below takes is recorded and refused
  await page.route(/\/api\/chat\/(conversations\/[^/]+\/messages|messages\/[^/]+\/(retry|cancel))(\?.*)?$/, (route) => {
    if (route.request().method() === "GET") return route.fallback();
    state.leaked.push(`${route.request().method()} ${route.request().url()}`);
    return route.fulfill({ status: 500, json: { detail: "blocked by the test" } });
  });
  await page.route(`**/api/chat/conversations/${id}/messages*`, (route) => {
    const method = route.request().method();
    if (method === "GET") {
      state.gets += 1;
      return route.fulfill({ json: thread(id, state.items) });
    }
    if (method === "POST" && opts.onSend) {
      const body = route.request().postDataJSON() as Json;
      state.posts.push(body);
      return route.fulfill({ json: opts.onSend(body, state) });
    }
    state.leaked.push(`${method} ${route.request().url()}`);
    return route.fulfill({ status: 500, json: { detail: "blocked by the test" } });
  });
  await page.route("**/api/documents/**/pages/*/image*", (route) =>
    route.fulfill({ status: 200, contentType: "image/png", body: PNG,
      headers: { "X-Display-Width": "600", "X-Display-Height": "900", "X-Render-Scale": "1", "X-Scale-Tier": "normal",
        "X-Reading-State": "current", "Cache-Control": "private, no-store" } }));
  await page.route("**/api/documents/**/blocks*", (route) =>
    route.fulfill({ json: { document_id: DOC, version_id: VER, title: TITLE, is_current: true,
      mime_type: "application/pdf", total: 1, file_url: `${BASE}/file`, reading_id: READING,
      blocks: [{ index: 0, kind: "paragraph", section: null, section_path: [], paragraph_no: null, page: 3, media: null,
        source: "text", status: "read", note: null, text: "נכס א׳ | 9,500 ₪", bbox: [60, 100, 540, 160],
        method: "text_layer", reader_version: "pdf-test", content_hash: null, original_text: null,
        page_url: `${BASE}/pages/3/image`, region_url: `${BASE}/regions/0/image?reading_id=${READING}` }] } }));
  await page.goto(`/chat?c=${id}`);
  return { id, reply: assistantMessages(page).last(), state };
}

function pair(answer: Json, extra: Json = {}): Json[] {
  return [message("m1", "user", QUESTION, null), message("m2", "assistant", String(answer.markdown), answer, extra)];
}

async function finish(page: Page, served: Served): Promise<void> {
  expect(served.state.leaked, "no chat request left the mocks").toEqual([]);
  await page.request.delete(`/api/chat/conversations/${served.id}`);
}

const cellOf = (el: Locator) => el.locator("xpath=ancestor::td[1]");
const cellIndex = (td: Locator) => td.evaluate((c) => (c as HTMLTableCellElement).cellIndex);
const rowIndex = (td: Locator) => td.evaluate((c) => (c.parentElement as HTMLTableRowElement).rowIndex);

// --- 1. a citation to a cell of a table stored reversed -------------------------------------------------------------------

test.describe("a citation to a cell of a table stored reversed", () => {
  test("the chip sits in its own cell of the table drawn right to left, and opens the viewer at that cell with its labels and notes", async ({ page }) => {
    const served = await serve(page, pair(tableAnswer(), { can_view_diagnostics: true }));
    const reply = served.reply;
    await expect(reply).toBeVisible();
    const table = reply.locator(".body .table-wrap table");
    // the columns are in the order they are drawn: the first is the right-most
    await expect(table.locator("thead th")).toHaveText(HEADERS);
    expect(await table.evaluate((t) => getComputedStyle(t).direction)).toBe("rtl");
    const xs: number[] = [];
    for (let i = 0; i < HEADERS.length; i++) xs.push((await table.locator("thead th").nth(i).boundingBox())!.x);
    for (let i = 1; i < xs.length; i++) expect(xs[i]).toBeLessThan(xs[i - 1]);
    // the chip of V1 is in the cell the anchor names: row 1, second column, under «שווי למ״ר»
    const chip = table.locator(".cite").first();
    const td = cellOf(chip);
    expect(await rowIndex(td)).toBe(1);
    expect(await cellIndex(td)).toBe(1);
    await expect(td).toContainText("9,500 ₪");
    await expect(table.locator("thead th").nth(1)).toHaveText("שווי למ״ר");
    await expect(table.locator("tbody tr").nth(0).locator("td").nth(0)).toHaveText("נכס א׳");

    await chip.click();
    const viewer = sourceViewer(page);
    await expect(viewer).toBeVisible();
    await expect(viewer.getByTestId("viewer-precision")).toHaveText("התא מסומן בטבלה");
    // the labels are the ones of the cell in the drawn order: the row «נכס א׳», the column «שווי למ״ר»
    await expect(viewer.getByTestId("viewer-table-title")).toHaveText("טבלת שוויים");
    await expect(viewer.getByTestId("viewer-table-row")).toHaveText("נכס א׳");
    await expect(viewer.getByTestId("viewer-table-column")).toHaveText("שווי למ״ר");
    await expect(viewer.getByTestId("viewer-table-unit")).toHaveText(UNIT_NOTE);
    // the table's own notes are shown with the cell
    const notes = viewer.getByTestId("viewer-table-notes");
    for (const n of TABLE_NOTES) await expect(notes).toContainText(n);
    await expect(notes.locator("li")).toHaveCount(TABLE_NOTES.length);
    // the cell is marked on the page, and so is its column header
    await expect(viewer.getByTestId("viewer-page-image")).toBeVisible();
    await expect(viewer.getByTestId("viewer-highlight")).toHaveCount(1);
    await expect(viewer.getByTestId("viewer-header-highlight")).toHaveCount(1);
    await expect(viewer).toContainText("שורה «נכס א׳», עמודה «שווי למ״ר»");
    // the viewer is right to left
    expect(await viewer.evaluate((v) => getComputedStyle(v).direction)).toBe("rtl");
    await finish(page, served);
  });
});

// --- 2. reload ----------------------------------------------------------------------------------------------------------

test.describe("reload of the conversation page", () => {
  test("keeps the same answer text, the removal note and the marks in their cells, and the cell citation still opens", async ({ page }) => {
    const served = await serve(page, pair(tableAnswer(), { can_view_diagnostics: true }));
    const snapshot = async () => {
      const reply = assistantMessages(page).last();
      await expect(reply.locator(".body table")).toHaveCount(1);
      const marks = async (id: string) => reply.getByTestId(id).evaluateAll((els) =>
        els.map((e) => ({ text: (e.textContent ?? "").trim(), row: (e.closest("tr") as HTMLTableRowElement | null)?.rowIndex ?? -1,
          col: (e.closest("td") as HTMLTableCellElement | null)?.cellIndex ?? -1 })));
      return {
        body: ((await reply.locator(".body").innerText()) ?? "").replace(/\s+/g, " ").trim(),
        cut: await marks("cut-marker"),
        partial: await marks("partial-mark"),
        note: ((await reply.getByTestId("removal-note").innerText()) ?? "").replace(/\s+/g, " ").trim(),
        cells: await reply.locator(".body td").allInnerTexts(),
      };
    };
    const before = await snapshot();
    expect(before.cut).toEqual([{ text: cutMarker(1), row: 1, col: 2 }]);
    expect(before.partial).toHaveLength(1);
    expect(before.partial[0]).toMatchObject({ text: "(אומת חלקית)", row: 2, col: 1 });
    expect(before.note).toContain(NOTE_TEXT);
    expect(before.body).toContain(CONDITION.trim());
    const getsBefore = served.state.gets;
    await page.reload();
    await expect(assistantMessages(page)).toHaveCount(1);
    const after = await snapshot();
    expect(served.state.gets, "the page read the stored thread again").toBeGreaterThan(getsBefore);
    expect(after).toEqual(before);
    // the stored citation still opens its cell with the same notes
    await assistantMessages(page).last().locator(".body table .cite").first().click();
    const viewer = sourceViewer(page);
    await expect(viewer.getByTestId("viewer-table-row")).toHaveText("נכס א׳");
    await expect(viewer.getByTestId("viewer-table-column")).toHaveText("שווי למ״ר");
    for (const n of TABLE_NOTES) await expect(viewer.getByTestId("viewer-table-notes")).toContainText(n);
    await finish(page, served);
  });
});

// --- 3. marks inside cells ----------------------------------------------------------------------------------------------

test.describe("marks inside table cells (right to left)", () => {
  test("the cut marker, the partly verified mark and the condition each stay in their own cell, inside its box", async ({ page }) => {
    const served = await serve(page, pair(tableAnswer(), { can_view_diagnostics: true }));
    const reply = served.reply;
    const table = reply.locator(".body .table-wrap table");
    await expect(table).toHaveCount(1);

    const cut = reply.getByTestId("cut-marker");
    const partial = reply.getByTestId("partial-mark");
    await expect(cut).toHaveCount(1);
    await expect(partial).toHaveCount(1);
    // the cut marker is the whole content of row 1, third column («שיעור היוון»)
    const cutCell = cellOf(cut);
    await expect(cutCell).toHaveText(cutMarker(1));
    expect([await rowIndex(cutCell), await cellIndex(cutCell)]).toEqual([1, 2]);
    // the partial mark follows the value it qualifies in row 2, second column («שווי למ״ר»)
    const partialCell = cellOf(partial);
    await expect(partialCell).toContainText("8,200 ₪");
    await expect(partial).toHaveText("(אומת חלקית)");
    expect([await rowIndex(partialCell), await cellIndex(partialCell)]).toEqual([2, 1]);
    const partialText = (await partialCell.innerText()).replace(/\s+/g, " ");
    expect(partialText.indexOf("8,200")).toBeLessThan(partialText.indexOf("(אומת חלקית)"));
    // the condition stays inside row 2, third column, next to the 7% it conditions
    const condCell = table.locator("tbody tr").nth(1).locator("td").nth(2);
    await expect(condCell).toContainText("7%");
    await expect(condCell).toContainText(CONDITION.trim());
    // and nowhere else
    for (const [row, col] of [[0, 0], [0, 1], [0, 2], [0, 3], [0, 4], [1, 0], [1, 1], [1, 3], [1, 4]]) {
      await expect(table.locator("tbody tr").nth(row).locator("td").nth(col)).not.toContainText("בכפוף לאישור");
    }
    await expect(table.locator("tbody tr").nth(0)).not.toContainText("אומת חלקית");
    await expect(table.locator("tbody tr").nth(1).locator("td").nth(2)).not.toContainText("אומת חלקית");
    // each mark's box lies inside its cell's box, and the cell reads right to left
    for (const [mark, cell] of [[cut, cutCell], [partial, partialCell]] as const) {
      const m = (await mark.boundingBox())!;
      const c = (await cell.boundingBox())!;
      expect(m.x).toBeGreaterThanOrEqual(c.x - 1);
      expect(m.x + m.width).toBeLessThanOrEqual(c.x + c.width + 1);
      expect(m.y).toBeGreaterThanOrEqual(c.y - 1);
      expect(m.y + m.height).toBeLessThanOrEqual(c.y + c.height + 1);
      expect(await cell.evaluate((td) => getComputedStyle(td).direction)).toBe("rtl");
    }
    // right to left: the third column lies left of the second
    const xs = [await cutCell.boundingBox(), await partialCell.boundingBox()].map((b) => b!.x);
    expect(xs[0], "the third column lies left of the second one").toBeLessThan(xs[1]);
    await finish(page, served);
  });
});

// --- 4. the calculation view's honest note ------------------------------------------------------------------------------

const OLD_TEXT = "נקבע ולא נמצא במקור";
const STATED_KIND_WORDS = "שווי שוק מוערך";

function calcValue(id: string, label: string, v: string, text: string, extra: Json): Json {
  return {
    id, value: v, value_text: text, label, source_id: `S${id.slice(1)}`, document_id: DOC, version_id: VER, reading_id: READING,
    title: TITLE, location: "עמוד 3", kind: "amount", unit: "ILS", unit_label: "₪", period: "none", vat: "excluded",
    area_basis: "", subject: "", role: "unknown", provenance: { kind: "source", unit: "source", period: "not_stated", vat: "source" },
    certainty: "verified", locator: { quote: `${label}: ${text}` }, quote: `${label}: ${text}`, total: false, approx: false,
    anchor: anchor(), ...extra,
  };
}

function calcAnswer(): Json {
  const values = [
    calcValue("V1", "שווי הקרקע", "100000", "100,000", { certainty: "model_asserted",
      provenance: { kind: "stated_different", unit: "source", period: "not_stated", vat: "source" },
      stated: { kind: STATED_KIND_WORDS } }),
    calcValue("V2", "עלות הבנייה", "50000", "50,000", { certainty: "model_asserted",
      provenance: { kind: "source", unit: "model_asserted", period: "not_stated", vat: "source" } }),
    calcValue("V3", "דמי ניהול", "20000", "20,000", { certainty: "verified" }),
    // stored before provenance carried the attributes: only the certainty is known
    calcValue("V4", "היטל השבחה", "10000", "10,000", { certainty: "model_asserted", provenance: {} }),
  ];
  const input = (v: Json) => ({ id: v.id, label: v.label, kind: "value", value: v.value, display: v.value_text,
    value_text: v.value_text, source_id: v.source_id, certainty: v.certainty });
  const computation = {
    id: "C1", label: "סך העלויות והשווי", expression: "V1 + V2 + V3 + V4", formula: "שווי הקרקע + עלות הבנייה + דמי ניהול + היטל השבחה",
    value: "180000", display: { value: "180,000" }, unit: "₪", kind: "amount", result_kind: "derived",
    result_kind_label: "נגזר מערכים מצוטטים", inputs: values.map(input), assumptions: [], sources: ["S1", "S2", "S3", "S4"],
    documents: 1, conditional: false, conditions: [], vat: "excluded", justification: null, note: "", reproduces: null, n: null,
    operation: "V1 + V2 + V3 + V4", result: "180000",
    anchor: { v: 2, precision: "computed", precision_label: "תוצאה מחושבת", inputs: ["V1", "V2", "V3", "V4"] },
  };
  return answerWith(
    {
      markdown: "הסכום הכולל הוא 180,000 ₪ [C1].", status: "answered", gaps: [], components: [], server_spans: [],
      sources: [1, 2, 3, 4].map((n) => source(`S${n}`)), values, computations: [computation],
    },
    { completeness: { status: "full", requirements: 1, missing: [] } },
  );
}

test.describe("the calculation view's honest note", () => {
  test("says which defining attribute the source does not confirm, and nothing for a verified value or in the old words", async ({ page }) => {
    const served = await serve(page, pair(calcAnswer()));
    await served.reply.locator(".cite").first().click();
    const calc = page.getByRole("complementary", { name: "פירוט החישוב" });
    await expect(calc).toBeVisible();
    const rows = calc.locator('[data-testid="calc-input"][data-kind="value"]');
    await expect(rows).toHaveCount(4);
    const noted = (n: number) => rows.nth(n).getByTestId("calc-model-asserted");
    // V1: the source states another kind, in its own words
    await expect(noted(0)).toHaveText(new RegExp(`סוג הערך במקור: «${STATED_KIND_WORDS}»`));
    await expect(noted(0)).not.toContainText("יחידת הערך");
    // V2: the unit was not confirmed in the source, the kind was
    await expect(noted(1)).toContainText("יחידת הערך לא אושרה במקור");
    await expect(noted(1)).not.toContainText("סוג הערך");
    // V3: verified, no note at all
    await expect(noted(2)).toHaveCount(0);
    await expect(rows.nth(2)).not.toContainText("לא אושר");
    // V4: an old answer with only the certainty gets the general note, never the period or the VAT
    await expect(noted(3)).toHaveText(/משמעות הערך לא אושרה במלואה במקור/);
    for (const n of [0, 1, 3]) {
      await expect(noted(n)).not.toContainText("תקופה");
      await expect(noted(n)).not.toContainText("מע״מ");
    }
    await expect(page.locator("body")).not.toContainText(OLD_TEXT);
    // the same notes in the details' document data, and the old words nowhere
    await page.getByRole("button", { name: "סגירת פירוט החישוב" }).click();
    await expect(calc).toHaveCount(0);
    await openDetails(served.reply);
    const data = served.reply.getByTestId("calc-document-data");
    const lines = data.locator("li");
    await expect(lines).toHaveCount(4);
    await expect(lines.nth(0)).toContainText(`סוג הערך במקור: «${STATED_KIND_WORDS}»`);
    await expect(lines.nth(1)).toContainText("יחידת הערך לא אושרה במקור");
    await expect(lines.nth(2)).not.toContainText("לא אושר");
    await expect(lines.nth(2)).not.toContainText("במקור: «");
    await expect(lines.nth(3)).toContainText("משמעות הערך לא אושרה במלואה במקור");
    await expect(page.locator("body")).not.toContainText(OLD_TEXT);
    await finish(page, served);
  });

  test("a value whose kind and unit are both unconfirmed shows both notes in one line", async ({ page }) => {
    const answer = calcAnswer();
    const values = answer.values as Json[];
    values[1] = { ...values[1], provenance: { kind: "model_asserted", unit: "stated_different", period: "not_stated", vat: "source" },
      stated: { unit: "אלפי ₪" } };
    const served = await serve(page, pair(answer));
    await served.reply.locator(".cite").first().click();
    const note = page.getByRole("complementary", { name: "פירוט החישוב" })
      .locator('[data-testid="calc-input"][data-kind="value"]').nth(1).getByTestId("calc-model-asserted");
    await expect(note).toContainText("סוג הערך לא אושר במקור");
    await expect(note).toContainText("יחידת הערך במקור: «אלפי ₪»");
    await expect(page.locator("body")).not.toContainText(OLD_TEXT);
    await finish(page, served);
  });
});

// --- 5. mobile ----------------------------------------------------------------------------------------------------------

test.describe("a phone (375px)", () => {
  test.use({ viewport: { width: 375, height: 812 }, hasTouch: true });

  const overflow = (page: Page) =>
    page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth,
      dir: getComputedStyle(document.documentElement).direction, attr: document.documentElement.dir }));

  test("the answer with the table adds no horizontal page scroll, keeps right to left, and the viewer fits too", async ({ page }) => {
    const served = await serve(page, pair(tableAnswer(), { can_view_diagnostics: true }));
    const reply = served.reply;
    const table = reply.locator(".body .table-wrap table");
    await expect(table).toHaveCount(1);
    let o = await overflow(page);
    expect(o.scroll, `page scrollWidth ${o.scroll} vs clientWidth ${o.client}`).toBeLessThanOrEqual(o.client);
    expect(o.dir).toBe("rtl");
    expect(o.attr).toBe("rtl");
    // the wide table scrolls inside its own container, within the screen
    const wrap = reply.locator(".body .table-wrap");
    expect(await wrap.evaluate((el) => getComputedStyle(el).overflowX)).toBe("auto");
    const sizes = await wrap.evaluate((el) => ({ client: el.clientWidth, view: window.innerWidth }));
    expect(sizes.client).toBeLessThanOrEqual(sizes.view);
    expect(await table.evaluate((t) => getComputedStyle(t).direction)).toBe("rtl");
    await expect(table.locator("thead th")).toHaveText(HEADERS);
    // the marks are still in their cells at this width
    await expect(cellOf(reply.getByTestId("cut-marker"))).toHaveText(cutMarker(1));
    await expect(cellOf(reply.getByTestId("partial-mark"))).toContainText("8,200 ₪");
    // the citation opens the viewer over the page, which does not widen it either, and its labels read right to left
    await table.locator(".cite").first().click();
    const viewer = sourceViewer(page);
    await expect(viewer).toBeVisible();
    await expect(viewer.getByTestId("viewer-table-row")).toHaveText("נכס א׳");
    await expect(viewer.getByTestId("viewer-table-notes")).toContainText(TABLE_NOTES[0]);
    o = await overflow(page);
    expect(o.scroll, `page scrollWidth ${o.scroll} vs clientWidth ${o.client} with the viewer open`).toBeLessThanOrEqual(o.client);
    expect(o.dir).toBe("rtl");
    const box = (await viewer.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(-1);
    expect(box.x + box.width).toBeLessThanOrEqual(o.client + 1);
    await finish(page, served);
  });
});

// --- 6. a two-list answer and its follow-up -----------------------------------------------------------------------------

const LISTS_MD = [
  "נמצאו שתי רשימות במסמך [S1]:",
  "**רשימה א׳ - נכסים בבעלות החברה**",
  "1. נכס א׳\n2. נכס ב׳\n3. נכס ג׳",
  "**רשימה ב׳ - נכסים מושכרים**",
  "1. נכס ד׳\n2. נכס ה׳",
].join("\n\n");
const FOLLOW_UP = "ומה השווי למ״ר של הפריט השני ברשימה ב׳?";
const FOLLOW_UP_MD = "הפריט השני ברשימה ב׳ הוא נכס ה׳; שוויו למ״ר הוא 7,300 ₪ [S1].";

test.describe("a two-list answer and a follow-up turn", () => {
  test("both turns are shown, the first unchanged and the second with the answer the server returned", async ({ page }) => {
    const first = answerWith({ markdown: LISTS_MD, status: "answered", gaps: [], components: [], server_spans: [] },
      { completeness: { status: "full", requirements: 1, missing: [] } });
    const items = [message("m1", "user", "אילו רשימות של נכסים יש במסמך?", null), message("m2", "assistant", LISTS_MD, first)];
    const served = await serve(page, items, {
      onSend: (body, state) => {
        const answer = answerWith({ markdown: FOLLOW_UP_MD, status: "answered", gaps: [], components: [], server_spans: [] },
          { completeness: { status: "full", requirements: 1, missing: [] } });
        const user = message("m3", "user", String(body.content), null, { client_id: body.client_id, reply_to: null });
        const assistant = message("m4", "assistant", FOLLOW_UP_MD, answer, { reply_to: "m3" });
        state.items = [...state.items, user, assistant];
        return { user, assistant };
      },
    });
    const replies = assistantMessages(page);
    await expect(replies).toHaveCount(1);
    await expect(replies.first().locator(".body ol")).toHaveCount(2);
    await expect(replies.first().locator(".body ol").nth(1).locator("li")).toHaveText(["נכס ד׳", "נכס ה׳"]);

    await page.getByRole("textbox", { name: "הודעה" }).fill(FOLLOW_UP);
    await page.getByRole("textbox", { name: "הודעה" }).press("Enter");
    await expect(replies).toHaveCount(2);
    expect(served.state.posts).toHaveLength(1);
    expect(served.state.posts[0].content).toBe(FOLLOW_UP);
    // both turns: the user's question, then the answer, twice, in order
    await expect(page.locator(".msg-user")).toHaveCount(2);
    await expect(page.locator(".msg-user").last()).toContainText(FOLLOW_UP);
    // the first turn is unchanged
    await expect(replies.first().locator(".body ol")).toHaveCount(2);
    await expect(replies.first().locator(".body ol").first().locator("li")).toHaveText(["נכס א׳", "נכס ב׳", "נכס ג׳"]);
    // the follow-up shows what the mock returned, and none of the first turn's lists
    const second = replies.last().locator(".body");
    await expect(second).toContainText("הפריט השני ברשימה ב׳ הוא נכס ה׳");
    await expect(second).toContainText("7,300 ₪");
    await expect(second.locator("ol")).toHaveCount(0);
    await expect(second).not.toContainText("נכס א׳");
    // the order in the page: the first reply comes before the second question, which comes before the second reply
    const order = await page.evaluate(() => [...document.querySelectorAll(".msg-user, .msg-assistant")].map((e) => e.className.includes("msg-user") ? "u" : "a").join(""));
    expect(order).toBe("uaua");
    // a reload serves the stored thread: both turns are still there
    await page.reload();
    await expect(assistantMessages(page)).toHaveCount(2);
    await expect(assistantMessages(page).last().locator(".body")).toContainText("7,300 ₪");
    await finish(page, served);
  });
});
