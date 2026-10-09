import { deflateSync } from "node:zlib";
import { expect, test, type Locator, type Page, type Route } from "@playwright/test";
import { assistantMessages, login, USERS } from "./helpers";

// The source viewer (U6) and the answer's completeness and value statuses (U9, U12). The conversation is real
// (office B), but its messages, the cited document's blocks and the page images are fixed synthetic payloads, so the
// test checks how the viewer draws a stored anchor, not a model's choices or a processed document. The page image is
// a real PNG of known size, so a highlight's position can be compared with the anchor's fractions of the page.
// The real end-to-end check against rendered documents is `citations.spec.ts` (U14).

const NOW = new Date().toISOString();
const DOC = "00000000-0000-0000-0000-0000000000d1";
const VER = "00000000-0000-0000-0000-0000000000a1";
const BASE = `/api/documents/${DOC}/versions/${VER}`;
const TITLE = "שומה סינתטית לבדיקה";
const READING = "reading-synthetic-1";

// --- a PNG of known size -------------------------------------------------------------------------------------------

const PAGE_W = 200;
const PAGE_H = 300;

function crc32(buf: Buffer): number {
  let crc = 0xffffffff;
  for (const b of buf) {
    crc ^= b;
    for (let k = 0; k < 8; k++) crc = crc & 1 ? (crc >>> 1) ^ 0xedb88320 : crc >>> 1;
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function chunk(type: string, data: Buffer): Buffer {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(body));
  return Buffer.concat([len, body, crc]);
}

/** A white RGB page with a dark bar over `dark` (fractions of the page), like a line of text. */
function pagePng(dark: [number, number, number, number]): Buffer {
  const row = PAGE_W * 3 + 1;
  const raw = Buffer.alloc(row * PAGE_H, 0xff);
  for (let y = 0; y < PAGE_H; y++) {
    raw[y * row] = 0; // filter: none
    for (let x = 0; x < PAGE_W; x++) {
      const inside = x >= dark[0] * PAGE_W && x < dark[2] * PAGE_W && y >= dark[1] * PAGE_H && y < dark[3] * PAGE_H;
      if (inside) raw.fill(0x22, y * row + 1 + x * 3, y * row + 1 + x * 3 + 3);
    }
  }
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(PAGE_W, 0);
  ihdr.writeUInt32BE(PAGE_H, 4);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 2; // truecolour
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(raw)),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

const SPAN: [number, number, number, number] = [0.1, 0.2, 0.6, 0.25];
const PNG = pagePng(SPAN);

// --- synthetic payloads --------------------------------------------------------------------------------------------

type Json = Record<string, unknown>;

function anchor(extra: Json = {}): Json {
  return {
    v: 1, precision: "span", precision_label: "המקום המצוטט מסומן בעמוד", region: null, degraded: null,
    document_id: DOC, version_id: VER, reading_id: READING, block_start: 0, block_end: 0,
    pages: [{ page: 3, printed_label: "5", width: 600, height: 900, rects: [SPAN], focus: [[0.4, 0.2, 0.5, 0.25]] }],
    location: { title: TITLE, label: "עמוד 3 (בדפוס 5), סעיף «2. תיאור הנכס»", page: 3, page_end: null,
      printed_page: "5", section: "2. תיאור הנכס" },
    table: null, structured: null, truncated: false, ...extra,
  };
}

function source(id: string, a: Json | null, extra: Json = {}): Json {
  return {
    id, document_id: DOC, version_id: VER, title: TITLE, section: "2. תיאור הנכס", location: "עמוד 3", kind: "text",
    text: "הנכס נמצא בשכונה שקטה ברחוב הדוגמה 4.", block_start: 0, block_end: 0, table_index: null, page_list: [3],
    chunk_id: null, reading_id: READING, status: "complete", anchor: a, ...extra,
  };
}

function answerWith(markdown: string, extra: Json = {}): Json {
  const doc = { document_id: DOC, title: TITLE };
  return {
    kind: "rag", status: "answered", markdown, claims: [], clarification: null, missing: null,
    sources: [source("S1", anchor())], measurements: [], computations: [], values: [], assumptions: [],
    documents: [doc],
    verification: { judged: true, judge_status: "ok", removed: 0, partial: 0, annotated: 0, correctness: "verified" },
    searches: [], coverage: [], ledger: { cited: [doc], scope_kind: "focused", scope_query: "", complete: true },
    ...extra,
  };
}

function messages(conversationId: string, answer: Json) {
  const conversation = { id: conversationId, title: "בדיקת מציג המקור", updated_at: NOW, created_at: NOW,
    archived: false, engine: "rag" };
  return {
    conversation, has_more: false,
    messages: [
      { id: "m1", role: "user", content: "איפה נמצא הנכס?", status: "done", error: null, progress: [], answer: null,
        reply_to: null, client_id: null, created_at: NOW, stale: false, cancel_requested: false },
      { id: "m2", role: "assistant", content: String(answer.markdown), status: "done", error: null, progress: [],
        answer, reply_to: "m1", client_id: null, created_at: NOW, stale: false, cancel_requested: false, usage: [] },
    ],
  };
}

function pdfBlock(index: number, extra: Json = {}): Json {
  return {
    index, kind: "paragraph", section: "2. תיאור הנכס", section_path: ["2. תיאור הנכס"], paragraph_no: null, page: 3,
    media: null, source: "text", status: "read", note: null, text: `קטע סינתטי ${index}`, bbox: [60, 100, 540, 160],
    method: "text_layer", reader_version: "pdf-test", content_hash: null, original_text: null,
    page_url: `${BASE}/pages/3/image`, region_url: `${BASE}/regions/${index}/image?reading_id=${READING}`, ...extra,
  };
}

const PDF_BLOCKS = [pdfBlock(0, { text: "הנכס נמצא בשכונה שקטה ברחוב הדוגמה 4." }), pdfBlock(1), pdfBlock(2)];

interface Opened {
  viewer: Locator;
  id: string;
  imageRequests: string[];
}

type ImageHandler = (route: Route, url: URL) => Promise<void> | void;

const servePage: ImageHandler = (route) =>
  route.fulfill({
    status: 200, contentType: "image/png", body: PNG,
    headers: { "X-Display-Width": "600", "X-Display-Height": "900", "X-Render-Scale": "1", "X-Scale-Tier": "normal",
      "X-Reading-State": "current", "Cache-Control": "private, no-store" },
  });

/** Logs in, serves `answer` as the conversation's reply, and opens the citation chip `chip` (0-based). */
async function openViewer(
  page: Page,
  answer: Json,
  opts: { chip?: number; image?: ImageHandler; blocks?: Json[]; mime?: string } = {},
): Promise<Opened> {
  await login(page, USERS.adminB);
  const { id } = await (await page.request.post("/api/chat/conversations")).json();
  const imageRequests: string[] = [];
  await page.route(`**/api/chat/conversations/${id}/messages*`, (route) => route.fulfill({ json: messages(id, answer) }));
  await page.route("**/api/documents/**/pages/*/image*", (route) => {
    const url = new URL(route.request().url());
    imageRequests.push(url.pathname + url.search);
    return (opts.image ?? servePage)(route, url);
  });
  const blocks = opts.blocks ?? PDF_BLOCKS;
  await page.route("**/api/documents/**/blocks*", (route) => {
    const url = new URL(route.request().url());
    const start = Number(url.searchParams.get("start") ?? 0);
    const end = Number(url.searchParams.get("end") ?? 99);
    return route.fulfill({ json: { document_id: DOC, version_id: VER, title: TITLE, is_current: true,
      mime_type: opts.mime ?? "application/pdf", total: blocks.length, file_url: `${BASE}/file`, reading_id: READING,
      blocks: blocks.filter((b) => (b.index as number) >= start && (b.index as number) <= end) } });
  });
  await page.goto(`/chat?c=${id}`);
  await assistantMessages(page).last().locator(".cite").nth(opts.chip ?? 0).click();
  const viewer = page.getByRole("complementary", { name: "תצוגת מקור" });
  await expect(viewer).toBeVisible();
  return { viewer, id, imageRequests };
}

/** The element's box relative to the page image's box, as fractions of the image. */
async function relativeBox(image: Locator, el: Locator) {
  const a = await image.boundingBox();
  const b = await el.boundingBox();
  expect(a && b, "image and highlight are laid out").toBeTruthy();
  return { x0: (b!.x - a!.x) / a!.width, y0: (b!.y - a!.y) / a!.height,
    x1: (b!.x + b!.width - a!.x) / a!.width, y1: (b!.y + b!.height - a!.y) / a!.height };
}

function expectNear(actual: number, expected: number, what: string) {
  expect(Math.abs(actual - expected), `${what}: ${actual} vs ${expected}`).toBeLessThan(0.01);
}

async function tabUntil(page: Page, predicate: (el: Element) => boolean, max = 60): Promise<void> {
  for (let i = 0; i < max; i++) {
    await page.keyboard.press("Tab");
    const focused = await page.evaluateHandle(() => document.activeElement ?? document.body);
    const hit = await focused.evaluate(predicate);
    await focused.dispose();
    if (hit) return;
  }
  throw new Error(`focus never reached the target after ${max} Tab presses`);
}

// --- the viewer ----------------------------------------------------------------------------------------------------

test.describe("source viewer (desktop)", () => {
  test("a span anchor opens its page at its version, the highlight at the anchor's fractions of the image", async ({
    page,
  }) => {
    const { viewer, id, imageRequests } = await openViewer(page, answerWith("הנכס נמצא בשכונה שקטה [S1]."));
    const image = viewer.getByTestId("viewer-page-image");
    await expect(image).toBeVisible();
    expect(imageRequests[0]).toBe(`${BASE}/pages/3/image?scale=normal&reading_id=${READING}`);
    expect(await image.evaluate((el: HTMLImageElement) => [el.naturalWidth, el.naturalHeight])).toEqual([PAGE_W, PAGE_H]);

    // the header: readable title, file page beside the printed page, section and precision
    await expect(viewer.getByTestId("viewer-title")).toHaveText(TITLE);
    await expect(viewer.getByTestId("viewer-page")).toContainText("עמוד 3 בקובץ");
    await expect(viewer.getByTestId("viewer-printed-page")).toContainText("5");
    await expect(viewer.getByTestId("viewer-section")).toContainText("2. תיאור הנכס");
    await expect(viewer.getByTestId("viewer-precision")).toHaveText("המקום המצוטט מסומן בעמוד");

    const box = await relativeBox(image, viewer.getByTestId("viewer-highlight"));
    expectNear(box.x0, SPAN[0], "left");
    expectNear(box.y0, SPAN[1], "top");
    expectNear(box.x1, SPAN[2], "right");
    expectNear(box.y1, SPAN[3], "bottom");
    await expect(viewer.getByTestId("viewer-focus")).toHaveCount(1);

    // the extracted text is the other tab, with the cited block marked
    await viewer.getByRole("tab", { name: "הטקסט שחולץ" }).click();
    await expect(viewer.getByRole("tab", { name: "הטקסט שחולץ" })).toHaveAttribute("aria-selected", "true");
    await expect(viewer.locator('[data-cited="true"]')).toContainText("הנכס נמצא בשכונה שקטה");
    await expect(viewer.getByTestId("viewer-page-image")).toHaveCount(0);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a cell anchor marks the cell and shows the table's title, row, column and unit", async ({ page }) => {
    const cell = anchor({
      precision: "cell", precision_label: "התא מסומן בטבלה",
      pages: [{ page: 3, printed_label: null, width: 600, height: 900, rects: [[0.55, 0.4, 0.7, 0.43]], focus: [] }],
      location: { title: TITLE, label: "עמוד 3, טבלה «טבלת שטחים», שורה «צפון», עמודה «שטח (מ״ר)»", page: 3,
        page_end: null, printed_page: null, section: null },
      table: { table_index: 0, title: "טבלת שטחים", row_label: "צפון", row_number: 1, column_header: "שטח (מ״ר)",
        column_number: 2, unit_note: "השטחים במ״ר", source: null,
        header: { page: 3, rects: [[0.55, 0.35, 0.7, 0.38]] }, notes: [{ text: "השטחים לפי מדידה", page: 3 }] },
    });
    const value = { id: "V1", value: "1250", value_text: "1,250", label: "שטח צפון", source_id: "S1",
      document_id: DOC, version_id: VER, reading_id: READING, title: TITLE, location: "עמוד 3, טבלה", kind: "area",
      unit: "sqm", unit_label: "מ״ר", period: "none", vat: "not_applicable", area_basis: "", subject: "נכס סינתטי",
      role: "area", provenance: {}, certainty: "verified",
      locator: { row: "צפון", column: "שטח (מ״ר)", row_number: 1, column_number: 2 }, quote: "צפון | 1,250",
      total: false, approx: false, anchor: cell };
    const { viewer, id } = await openViewer(page, answerWith("שטח הצפון 1,250 מ״ר [V1].", { values: [value] }));
    await expect(viewer.getByTestId("viewer-precision")).toHaveText("התא מסומן בטבלה");
    await expect(viewer.getByTestId("viewer-table-title")).toHaveText("טבלת שטחים");
    await expect(viewer.getByTestId("viewer-table-row")).toHaveText("צפון");
    await expect(viewer.getByTestId("viewer-table-column")).toHaveText("שטח (מ״ר)");
    await expect(viewer.getByTestId("viewer-table-unit")).toHaveText("השטחים במ״ר");
    await expect(viewer.getByTestId("viewer-value-status")).toContainText("נבדק אוטומטית");
    const image = viewer.getByTestId("viewer-page-image");
    await expect(image).toBeVisible();
    const box = await relativeBox(image, viewer.getByTestId("viewer-highlight"));
    expectNear(box.x0, 0.55, "cell left");
    expectNear(box.y0, 0.4, "cell top");
    // the column header is anchored too, and reachable from the citation (R10)
    await expect(viewer.getByTestId("viewer-header-highlight")).toHaveCount(1);
    await viewer.getByTestId("viewer-header-link").click();
    await expect(viewer.getByTestId("viewer-header-highlight")).toBeInViewport();
    await expect(viewer.getByTestId("viewer-table-notes")).toContainText("השטחים לפי מדידה");
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a region anchor says the mark is at table level", async ({ page }) => {
    const region = anchor({
      precision: "region", region: "table", degraded: "no_cell_box",
      precision_label: "הסימון ברמת הטבלה: לא נשמר מיקום לתא עצמו",
      pages: [{ page: 3, printed_label: null, width: 600, height: 900, rects: [[0.05, 0.3, 0.95, 0.6]], focus: [] }],
      table: { table_index: 1, title: "טבלה מתמונה", row_label: "דרום", row_number: 2, column_header: "שווי",
        column_number: 3, unit_note: null, source: "vision", header: null, notes: [] },
    });
    const { viewer, id } = await openViewer(page, answerWith("השווי בדרום [S1].", { sources: [source("S1", region)] }));
    await expect(viewer.getByTestId("viewer-precision")).toHaveText("הסימון ברמת הטבלה: לא נשמר מיקום לתא עצמו");
    await expect(viewer.getByTestId("viewer-degraded")).toBeVisible();
    await expect(viewer.getByTestId("viewer-highlight")).toHaveCount(1);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a page-precision anchor draws no rectangle and shows the page", async ({ page }) => {
    const pageOnly = anchor({
      precision: "page", precision_label: "הסימון ברמת העמוד: לא נשמר מיקום מדויק יותר", degraded: "no_geometry",
      pages: [{ page: 7, printed_label: null, width: 600, height: 900, rects: [], focus: [] }],
      location: { title: TITLE, label: "עמוד 7", page: 7, page_end: null, printed_page: null, section: null },
    });
    const { viewer, id, imageRequests } = await openViewer(page, answerWith("נאמר בעמוד [S1].", {
      sources: [source("S1", pageOnly)] }));
    await expect(viewer.getByTestId("viewer-page-image")).toBeVisible();
    await expect(viewer.getByTestId("viewer-highlight")).toHaveCount(0);
    await expect(viewer.getByTestId("viewer-page")).toContainText("עמוד 7 בקובץ");
    await expect(viewer.getByTestId("viewer-precision")).toContainText("ברמת העמוד");
    expect(imageRequests[0]).toContain("/pages/7/image");
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a DOCX anchor opens the structured view with the quoted words marked and no page", async ({ page }) => {
    const text = "שטח המגרש הוא 1,250 מ״ר לפי התשריט.";
    const at = text.indexOf("1,250");
    const docx = anchor({
      precision: "structured", precision_label: "מבנה המסמך (ללא עמודים)", pages: [], block_start: 5, block_end: 5,
      location: { title: TITLE, label: "סעיף «3.2 שטחים», פסקה 4", page: null, page_end: null, printed_page: null,
        section: "3.2 שטחים" },
      structured: { section_path: ["3. ממצאים", "3.2 שטחים"], paragraph_no: 4, paragraph_end: null, label: null, text,
        highlight: [at, at + 5], cell: null },
    });
    const blocks = [4, 5, 6].map((i) => ({ ...pdfBlock(i), page: null, page_url: undefined, region_url: undefined,
      section_path: ["3. ממצאים", "3.2 שטחים"], paragraph_no: i - 1,
      text: i === 5 ? text : i === 4 ? "המגרש נמדד בשנה האחרונה." : "אין חריגות בנייה ידועות." }));
    const { viewer, id, imageRequests } = await openViewer(page, answerWith("שטח המגרש 1,250 מ״ר [S1].", {
      sources: [source("S1", docx, { block_start: 5, block_end: 5, page_list: null })] }), {
      blocks, mime: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" });
    const view = viewer.getByTestId("structured-view");
    await expect(view).toBeVisible();
    await expect(view.getByTestId("structured-path")).toContainText("3.2 שטחים");
    await expect(view.getByTestId("structured-highlight")).toHaveText("1,250");
    await expect(view).toContainText("המגרש נמדד בשנה האחרונה."); // context before
    await expect(view).toContainText("אין חריגות בנייה ידועות."); // context after
    await expect(viewer.getByTestId("viewer-precision")).toHaveText("מבנה המסמך (ללא עמודים)");
    await expect(viewer.getByRole("tablist")).toHaveCount(0);
    await expect(viewer).not.toContainText(/עמוד\s*\d/);
    expect(imageRequests).toHaveLength(0);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a block range over pages 3–4 stacks both pages, centres the first rectangle and moves between pages", async ({
    page,
  }) => {
    const range = anchor({
      precision: "block", precision_label: "הקטע מסומן בעמוד", block_start: 0, block_end: 2,
      pages: [
        { page: 3, printed_label: null, width: 600, height: 900, rects: [[0.1, 0.82, 0.9, 0.95]], focus: [] },
        { page: 4, printed_label: null, width: 600, height: 900, rects: [[0.1, 0.05, 0.9, 0.2]], focus: [] },
      ],
      location: { title: TITLE, label: "עמודים 3–4", page: 3, page_end: 4, printed_page: null, section: null },
    });
    const { viewer, id, imageRequests } = await openViewer(page, answerWith("הקטע נמשך לעמוד הבא [S1].", {
      sources: [source("S1", range, { block_end: 2 })] }));
    await expect(viewer.getByTestId("viewer-page-image")).toHaveCount(2);
    await expect(viewer.getByTestId("viewer-page")).toContainText("עמודים 3–4");
    expect(imageRequests.map((u) => u.split("?")[0]).sort()).toEqual([`${BASE}/pages/3/image`, `${BASE}/pages/4/image`]);
    const scroller = await viewer.getByTestId("viewer-pages").boundingBox();
    const first = await viewer.getByTestId("viewer-highlight").first().boundingBox();
    const centre = first!.y + first!.height / 2;
    expect(centre).toBeGreaterThan(scroller!.y);
    expect(centre).toBeLessThan(scroller!.y + scroller!.height);
    await viewer.getByRole("button", { name: "העמוד הבא" }).click();
    await expect(viewer.locator('[data-testid="viewer-page-frame"][data-page="4"]')).toBeInViewport();
    await expect(viewer.getByRole("button", { name: "העמוד הקודם" })).toBeEnabled();
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a stale anchor shows the original page with the snapshot highlight and a note", async ({ page }) => {
    const { viewer, id, imageRequests } = await openViewer(page, answerWith("הנכס נמצא בשכונה שקטה [S1]."), {
      image: (route) => route.fulfill({ status: 200, contentType: "image/png", body: PNG,
        headers: { "X-Display-Width": "600", "X-Display-Height": "900", "X-Reading-State": "stale" } }),
    });
    await expect(viewer.getByTestId("viewer-page-image")).toBeVisible();
    await expect(viewer.getByTestId("viewer-stale")).toContainText("עובד מחדש");
    await expect(viewer.getByTestId("viewer-highlight")).toHaveCount(1);
    expect(imageRequests[0]).toContain(`reading_id=${READING}`);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("revoked access shows the unavailable message and requests no image again", async ({ page }) => {
    const { viewer, id, imageRequests } = await openViewer(page, answerWith("הנכס נמצא בשכונה שקטה [S1]."), {
      image: (route) => route.fulfill({ status: 404, json: { detail: "לא נמצא" } }),
    });
    await expect(viewer.getByRole("alert")).toContainText("המקור אינו זמין עוד");
    await expect(viewer).not.toContainText("הנכס נמצא בשכונה שקטה");
    await page.waitForTimeout(1500);
    expect(imageRequests).toHaveLength(1);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a render failure falls back to the text view", async ({ page }) => {
    const { viewer, id } = await openViewer(page, answerWith("הנכס נמצא בשכונה שקטה [S1]."), {
      image: (route) => route.fulfill({ status: 422, headers: { "X-Source-State": "render_failed" },
        json: { detail: "לא ניתן להציג את העמוד מהקובץ המקורי", state: "render_failed" } }),
    });
    await expect(viewer.getByRole("tab", { name: "הטקסט שחולץ" })).toHaveAttribute("aria-selected", "true");
    await expect(viewer.getByTestId("source-text-view")).toContainText("לא ניתן להציג את העמוד מהקובץ המקורי");
    await expect(viewer.locator('[data-cited="true"]')).toContainText("הנכס נמצא בשכונה שקטה");
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("keyboard: Tab reaches next, next moves through the answer's citations, Esc closes and returns focus", async ({
    page,
  }) => {
    const second = anchor({ pages: [{ page: 4, printed_label: null, width: 600, height: 900, rects: [SPAN], focus: [] }],
      location: { title: TITLE, label: "עמוד 4", page: 4, page_end: null, printed_page: null, section: null } });
    const { viewer, id } = await openViewer(page, answerWith("הנכס נמצא בשכונה שקטה [S1]. הוא בן שתי קומות [S2].", {
      sources: [source("S1", anchor()), source("S2", second, { block_start: 1, block_end: 1 })] }));
    await expect(viewer.getByTestId("viewer-position")).toContainText("1");
    await expect(viewer.getByRole("button", { name: "סגירת תצוגת המקור" })).toBeFocused();
    await tabUntil(page, (el) => el.getAttribute("aria-label") === "המקור הבא");
    await page.keyboard.press("Enter");
    await expect(viewer.getByTestId("viewer-position")).toContainText("2");
    await expect(viewer.getByTestId("viewer-page")).toContainText("עמוד 4 בקובץ");
    await expect(viewer.getByRole("button", { name: "המקור הקודם" })).toBeEnabled();
    await page.keyboard.press("Escape");
    await expect(viewer).toHaveCount(0);
    // focus is back on the chip that opened it, and the conversation is still shown
    const chip = assistantMessages(page).last().locator(".cite").first();
    await expect(chip).toBeFocused();
    await page.request.delete(`/api/chat/conversations/${id}`);
  });
});

test.describe("source viewer (mobile)", () => {
  test.use({ viewport: { width: 375, height: 812 }, hasTouch: true });

  test("full screen, zoom uses the larger scale, and browser Back closes it with the conversation shown", async ({
    page,
  }) => {
    const { viewer, id, imageRequests } = await openViewer(page, answerWith("הנכס נמצא בשכונה שקטה [S1]."));
    const box = await viewer.boundingBox();
    expect(box!.width).toBeGreaterThanOrEqual(370);
    expect(box!.height).toBeGreaterThanOrEqual(800);
    await expect(viewer.getByTestId("viewer-page-image")).toBeVisible();
    await viewer.getByRole("button", { name: "הגדלת העמוד" }).click();
    await expect(viewer.getByTestId("viewer-pages")).toHaveAttribute("data-scale", "zoom");
    await expect.poll(() => imageRequests.some((u) => u.includes("scale=zoom"))).toBe(true);
    const image = viewer.getByTestId("viewer-page-image");
    await expect(image).toBeVisible();
    // the zoomed page is wider than the screen, and the highlight still sits at the anchor's fractions
    expect((await image.boundingBox())!.width).toBeGreaterThan(375);
    const rel = await relativeBox(image, viewer.getByTestId("viewer-highlight"));
    expectNear(rel.x0, SPAN[0], "zoomed left");
    expectNear(rel.y0, SPAN[1], "zoomed top");
    await page.goBack();
    await expect(viewer).toHaveCount(0);
    await expect(assistantMessages(page).last()).toBeVisible();
    await expect(page).toHaveURL(new RegExp(`/chat\\?c=${id}`));
    await page.request.delete(`/api/chat/conversations/${id}`);
  });
});

// --- completeness and value statuses beside the answer (U9, U12) ---------------------------------------------------

test.describe("answer completeness and value statuses", () => {
  test("an incomplete answer lists each missing requirement with its reason; details keep the requirements", async ({
    page,
  }) => {
    const answer = answerWith("השווי למ״ר 9,500 ₪ [S1].", {
      status: "partial",
      verification: { judged: true, judge_status: "ok", removed: 0, partial: 0, annotated: 0, correctness: "verified",
        completeness: { status: "partial", requirements: 2, missing: [{ id: "Q2", text: "השוואה לסף", status: "missing",
          reason: "calculation_incomplete", reason_text: "החישוב לא הושלם" }] } },
      ledger: { cited: [{ document_id: DOC, title: TITLE }], scope_kind: "focused", scope_query: "", complete: true,
        requirements: [
          { id: "Q1", text: "השווי למ״ר", calculation: false, status: "full", stated: false, units: [0], related: ["S1"],
            reason: "", limitation: null, limitation_text: null },
          { id: "Q2", text: "השוואה לסף", calculation: true, status: "missing", stated: false, units: [], related: [],
            reason: "", limitation: "calculation_incomplete", limitation_text: "החישוב לא הושלם" },
        ] },
    });
    const { viewer, id } = await openViewer(page, answer);
    await page.keyboard.press("Escape");
    await expect(viewer).toHaveCount(0);
    const reply = assistantMessages(page).last();
    await expect(reply.getByTestId("completeness")).toContainText("השוואה לסף — החישוב לא הושלם");
    await expect(reply.getByTestId("correctness")).toHaveCount(0);
    await reply.getByText(/מקורות ופרטים/).click();
    await expect(reply.getByTestId("requirements")).toContainText("השוואה לסף (חישוב) — חסר · החישוב לא הושלם");
    await expect(reply.getByTestId("verification")).not.toContainText("כל הטענות נבדקו");
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a complete verified answer shows no indicator; values and measurements show their statuses", async ({
    page,
  }) => {
    const measurement = { id: "M1", measurement_id: "00000000-0000-0000-0000-0000000000e1", document_id: DOC,
      version_id: VER, title: TITLE, metric: "שווי למ״ר", metric_kind: "value_per_sqm", value_text: "9,500",
      unit: "ILS", period: "none", vat: "excluded", area_basis: null, subject: null, value_role: "conclusion",
      status: "verified", quote: "השווי למ״ר 9,500 ₪", section: "2. תיאור הנכס", block_index: 0, table_index: null,
      reading_id: READING, anchor: anchor() };
    const answer = answerWith("השווי למ״ר 9,500 ₪ [M1].", {
      measurements: [measurement],
      verification: { judged: true, judge_status: "ok", removed: 0, partial: 0, annotated: 0, correctness: "verified",
        completeness: { status: "full", requirements: 1, missing: [] } },
    });
    const { viewer, id } = await openViewer(page, answer);
    await expect(viewer.getByTestId("viewer-value-status")).toContainText("אומת או תוקן על ידי אדם");
    await page.keyboard.press("Escape");
    const reply = assistantMessages(page).last();
    await expect(reply.getByTestId("completeness")).toHaveCount(0);
    await expect(reply.getByTestId("correctness")).toHaveCount(0);
    await reply.getByText(/מקורות ופרטים/).click();
    await expect(reply.getByTestId("value-status").first()).toContainText("אומת או תוקן על ידי אדם");
    await expect(reply.getByTestId("verification")).toContainText("כל הטענות נבדקו מול המקורות המצוטטים");
    await page.request.delete(`/api/chat/conversations/${id}`);
  });
});

test.describe("a hidden answer", () => {
  test("an answer hidden after its source was revoked shows the hidden note and the page keeps working", async ({
    page,
  }) => {
    await login(page, USERS.adminB);
    const { id } = await (await page.request.post("/api/chat/conversations")).json();
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    // the server keeps only the answer's kind once a document behind it is no longer visible
    const body = messages(id, { kind: "rag", hidden: true });
    body.messages[1].content = "";
    await page.route(`**/api/chat/conversations/${id}/messages*`, (route) => route.fulfill({ json: body }));
    await page.goto(`/chat?c=${id}`);
    await expect(page.locator(".msg-assistant").last()).toContainText("התשובה הוסתרה");
    await expect(page.getByRole("button", { name: "שיחה חדשה" }).first()).toBeVisible();
    expect(errors).toEqual([]);
  });
});
