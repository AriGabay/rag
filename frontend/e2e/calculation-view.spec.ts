import { deflateSync } from "node:zlib";
import { expect, test, type Locator, type Page } from "@playwright/test";
import { assistantMessages, login, USERS } from "./helpers";

// The calculation breakdown (U7, R14, R16, AE5). The conversation is real (office B), but its messages, the cited
// document's blocks and its page images are fixed synthetic payloads, so the test checks how a stored computation is
// broken down and navigated, not a model's choices. Numbers: income 200,000 and costs 100,000 from the document, a
// 5% cost increase from the user's question: profit 200,000 − 100,000 × 1.05 = 95,000, and its rate 95,000 / 200,000.

const NOW = new Date().toISOString();
const DOC = "00000000-0000-0000-0000-0000000000d7";
const VER = "00000000-0000-0000-0000-0000000000a7";
const BASE = `/api/documents/${DOC}/versions/${VER}`;
const TITLE = "שומה סינתטית לבדיקת חישוב";
const READING = "reading-synthetic-calc";
const QUESTION = "מה יהיה הרווח אם העלויות יעלו ב-5% לעומת השומה?";
const QUOTE = "אם העלויות יעלו ב-5%";

// --- a blank page image ---------------------------------------------------------------------------------------------

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

function whitePng(w = 200, h = 300): Buffer {
  const row = w * 3 + 1;
  const raw = Buffer.alloc(row * h, 0xff);
  for (let y = 0; y < h; y++) raw[y * row] = 0;
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0);
  ihdr.writeUInt32BE(h, 4);
  ihdr[8] = 8;
  ihdr[9] = 2;
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", ihdr),
    chunk("IDAT", deflateSync(raw)),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

const PNG = whitePng();

// --- synthetic payloads ---------------------------------------------------------------------------------------------

type Json = Record<string, unknown>;

function docAnchor(page: number, block: number, label: string): Json {
  return {
    v: 1, precision: "span", precision_label: "המקום המצוטט מסומן בעמוד", region: null, degraded: null,
    document_id: DOC, version_id: VER, reading_id: READING, block_start: block, block_end: block,
    pages: [{ page, printed_label: null, width: 600, height: 900, rects: [[0.1, 0.2, 0.6, 0.25]], focus: [] }],
    location: { title: TITLE, label, page, page_end: null, printed_page: null, section: null },
    table: null, structured: null, truncated: false,
  };
}

const BLOCK_TEXT = ["ההכנסות השנתיות מהנכס: 200,000 ₪.", "העלויות השנתיות: 100,000 ₪.", "המחיר כפי שנכתב: 95,000 ₪."];

function source(id: string, block: number, page: number, anchored: boolean): Json {
  return {
    id, document_id: DOC, version_id: VER, title: TITLE, section: null, location: `עמוד ${page}`, kind: "text",
    text: BLOCK_TEXT[block], block_start: block, block_end: block, table_index: null, page_list: [page],
    chunk_id: null, reading_id: READING, status: "complete",
    anchor: anchored ? docAnchor(page, block, `עמוד ${page}`) : null,
  };
}

function value(id: string, sourceId: string, label: string, v: string, written: string, page: number, block: number,
  anchored: boolean): Json {
  return {
    id, value: v, value_text: written, label, source_id: sourceId, document_id: DOC, version_id: VER,
    reading_id: READING, title: TITLE, location: `עמוד ${page}`, kind: "amount", unit: "ILS", unit_label: "₪",
    period: "year", vat: "excluded", area_basis: "", subject: "", role: "unknown",
    provenance: { unit: "source", period: "source", vat: "source" }, certainty: "verified",
    locator: { quote: BLOCK_TEXT[block] }, quote: BLOCK_TEXT[block], total: false, approx: false,
    ...(anchored ? { anchor: docAnchor(page, block, `עמוד ${page}`) } : {}),
  };
}

const ASSUMPTION = {
  id: "A1", value: "5", value_text: "5%", unit: "percent", label: "עליית העלויות", quote: QUOTE, turn: 1,
  current: true, message_id: "m1",
};

const INPUT_V1 = { id: "V1", label: "הכנסות שנתיות", kind: "value", value: "200000", display: "200,000",
  value_text: "200,000", source_id: "S1", certainty: "verified" };
const INPUT_V2 = { id: "V2", label: "עלויות שנתיות", kind: "value", value: "100000", display: "100,000",
  value_text: "100,000", source_id: "S2", certainty: "verified" };
const INPUT_A1 = { id: "A1", label: "עליית העלויות", kind: "assumption", value: "5", display: "5", value_text: "5%",
  quote: QUOTE };

function profit(extra: Json = {}): Json {
  return {
    id: "C1", label: "רווח לאחר עליית עלויות", expression: "V1 - V2 * (1 + A1%)",
    formula: "הכנסות שנתיות − עלויות שנתיות × (1 + עליית העלויות)", value: "95000.00", display: { value: "95,000" },
    unit: "₪ לשנה", kind: "amount", result_kind: "scenario", result_kind_label: "תרחיש לפי בקשה",
    inputs: [INPUT_V1, INPUT_V2, INPUT_A1], assumptions: ["A1"], sources: ["S1", "S2"], documents: 1,
    conditional: false, conditions: [], vat: "excluded", justification: null, note: "", reproduces: null, n: null,
    operation: "V1 - V2 * (1 + A1%)", result: "95000.00",
    anchor: { v: 1, precision: "computed", precision_label: "תוצאה מחושבת", inputs: ["V1", "V2", "A1"] },
    ...extra,
  };
}

function rate(): Json {
  return {
    id: "C2", label: "שיעור הרווח", expression: "C1 / V1", formula: "רווח לאחר עליית עלויות / הכנסות שנתיות",
    value: "0.475", display: { value: "0.475", percent: "47.5%" }, unit: "", kind: "ratio", result_kind: "scenario",
    result_kind_label: "תרחיש לפי בקשה",
    inputs: [{ id: "C1", label: "רווח לאחר עליית עלויות", kind: "computation", value: "95000.00", display: "95,000" },
      INPUT_V1],
    assumptions: ["A1"], sources: ["S1", "S2"], documents: 1, conditional: false, conditions: [], vat: null,
    justification: null, note: "", reproduces: null, n: null, operation: "C1 / V1", result: "0.475",
    anchor: { v: 1, precision: "computed", precision_label: "תוצאה מחושבת", inputs: ["C1", "V1"] },
  };
}

function answerWith(markdown: string, extra: Json = {}, anchored = true): Json {
  const doc = { document_id: DOC, title: TITLE };
  return {
    kind: "rag", status: "answered", markdown, claims: [], clarification: null, missing: null,
    sources: [source("S1", 0, 3, anchored), source("S2", 1, 4, anchored)], measurements: [],
    computations: [profit(anchored ? {} : { anchor: undefined })],
    values: [value("V1", "S1", "הכנסות שנתיות", "200000", "200,000", 3, 0, anchored),
      value("V2", "S2", "עלויות שנתיות", "100000", "100,000", 4, 1, anchored)],
    assumptions: [ASSUMPTION], documents: [doc],
    verification: { judged: true, judge_status: "ok", removed: 0, partial: 0, annotated: 0, correctness: "verified" },
    searches: [], coverage: [], ledger: { cited: [doc], scope_kind: "focused", scope_query: "", complete: true },
    ...extra,
  };
}

function messages(conversationId: string, answer: Json) {
  const conversation = { id: conversationId, title: "בדיקת פירוט חישוב", updated_at: NOW, created_at: NOW,
    archived: false, engine: "rag" };
  return {
    conversation, has_more: false,
    messages: [
      { id: "m1", role: "user", content: QUESTION, status: "done", error: null, progress: [], answer: null,
        reply_to: null, client_id: null, created_at: NOW, stale: false, cancel_requested: false },
      { id: "m2", role: "assistant", content: String(answer.markdown), status: "done", error: null, progress: [],
        answer, reply_to: "m1", client_id: null, created_at: NOW, stale: false, cancel_requested: false, usage: [] },
    ],
  };
}

function blocks(): Json[] {
  return BLOCK_TEXT.map((text, index) => ({
    index, kind: "paragraph", section: null, section_path: [], paragraph_no: null, page: 3 + index, media: null,
    source: "text", status: "read", note: null, text, bbox: [60, 100, 540, 160], method: "text_layer",
    reader_version: "pdf-test", content_hash: null, original_text: null, page_url: `${BASE}/pages/${3 + index}/image`,
    region_url: `${BASE}/regions/${index}/image?reading_id=${READING}`,
  }));
}

/** Logs in, serves `answer` as the conversation's reply, and opens the citation chip `chip` (0-based). */
async function openChip(page: Page, answer: Json, chip = 0): Promise<{ id: string }> {
  await login(page, USERS.adminB);
  const { id } = await (await page.request.post("/api/chat/conversations")).json();
  await page.route(`**/api/chat/conversations/${id}/messages*`, (route) => route.fulfill({ json: messages(id, answer) }));
  await page.route("**/api/documents/**/pages/*/image*", (route) =>
    route.fulfill({
      status: 200, contentType: "image/png", body: PNG,
      headers: { "X-Display-Width": "600", "X-Display-Height": "900", "X-Render-Scale": "1",
        "X-Scale-Tier": "normal", "X-Reading-State": "current", "Cache-Control": "private, no-store" },
    }));
  await page.route("**/api/documents/**/blocks*", (route) => {
    const url = new URL(route.request().url());
    const start = Number(url.searchParams.get("start") ?? 0);
    const end = Number(url.searchParams.get("end") ?? 99);
    const all = blocks();
    return route.fulfill({ json: { document_id: DOC, version_id: VER, title: TITLE, is_current: true,
      mime_type: "application/pdf", total: all.length, file_url: `${BASE}/file`, reading_id: READING,
      blocks: all.filter((b) => (b.index as number) >= start && (b.index as number) <= end) } });
  });
  await page.goto(`/chat?c=${id}`);
  await assistantMessages(page).last().locator(".cite").nth(chip).click();
  return { id };
}

/** The breakdown on top (a level below it is hidden, so it is not in the accessibility tree). */
function breakdown(page: Page): Locator {
  return page.getByRole("complementary", { name: "פירוט החישוב" });
}

/** Every breakdown level, shown or kept below another level. */
function breakdownLevels(page: Page): Locator {
  return page.getByTestId("calc-view");
}

function viewer(page: Page): Locator {
  return page.getByRole("complementary", { name: "תצוגת מקור" });
}

const PROFIT_TEXT = "הרווח הצפוי לאחר עליית העלויות הוא 95,000 ₪ לשנה [C1], לפי ההנחה שלך [A1].";

// --- desktop --------------------------------------------------------------------------------------------------------

test.describe("calculation breakdown (desktop)", () => {
  test("AE5: formula, two document inputs that open the viewer at their anchors, and the user's assumption", async ({
    page,
  }) => {
    const { id } = await openChip(page, answerWith(PROFIT_TEXT));
    const calc = breakdown(page);
    await expect(calc).toBeVisible();
    await expect(calc.getByRole("button", { name: "סגירת פירוט החישוב" })).toBeFocused();
    await expect(calc.getByTestId("calc-formula")).toContainText("הכנסות שנתיות − עלויות שנתיות");
    await expect(calc.getByTestId("calc-result")).toContainText("95,000");
    // the full value, how it is rounded for display, and that it is computed now, not written in the document
    await expect(calc.getByTestId("calc-unrounded")).toContainText("95,000.00");
    await expect(calc.getByTestId("calc-rounding")).toBeVisible();
    await expect(calc.getByTestId("calc-result")).toHaveAttribute("data-result-kind", "scenario");
    await expect(calc.getByTestId("calc-kind")).toHaveText("חושב עכשיו לפי בקשתך");
    await expect(calc.getByTestId("calc-result")).toContainText("אינו כתוב במסמך");
    await expect(calc.getByTestId("calc-vat")).toContainText("אינם כוללים מע״מ");
    await expect(calc.getByTestId("calc-conditional")).toHaveAttribute("data-conditional", "false");

    const docs = calc.locator('[data-testid="calc-input"][data-kind="value"]');
    await expect(docs).toHaveCount(2);
    const assumption = calc.getByTestId("calc-assumption");
    await expect(assumption).toContainText("5%");
    await expect(assumption.getByTestId("calc-assumption-quote")).toHaveText(QUOTE);

    // the first input opens the viewer at its own place, above the breakdown
    const first = docs.first().getByRole("button");
    await first.click();
    const v = viewer(page);
    await expect(v).toBeVisible();
    await expect(calc).toBeHidden();
    await expect(v.getByTestId("viewer-page")).toContainText("עמוד 3 בקובץ");
    // previous/next moves through the breakdown's document inputs
    await expect(v.getByTestId("viewer-position")).toContainText("1");
    await expect(v.getByTestId("viewer-position")).toContainText("2");
    await v.getByRole("button", { name: "המקור הבא" }).click();
    await expect(v.getByTestId("viewer-page")).toContainText("עמוד 4 בקובץ");
    // Esc closes the viewer only: the breakdown is back, with focus on the input that opened it
    await page.keyboard.press("Escape");
    await expect(v).toHaveCount(0);
    await expect(calc).toBeVisible();
    await expect(first).toBeFocused();

    // the second input opens the viewer at its own anchor
    await docs.nth(1).getByRole("button").click();
    await expect(viewer(page).getByTestId("viewer-page")).toContainText("עמוד 4 בקובץ");
    await page.keyboard.press("Escape");
    await expect(calc).toBeVisible();

    // the assumption leads back to the user's message, which is marked
    await assumption.getByTestId("calc-assumption-goto").click();
    const asked = page.locator('.msg-user[data-message-id="m1"]');
    await expect(asked).toBeFocused();
    await expect(asked).toHaveAttribute("data-flash", "true");
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a chained calculation opens its own breakdown, and back returns to the outer one", async ({ page }) => {
    const answer = answerWith("שיעור הרווח הצפוי הוא 47.5% [C2], לפי ההנחה שלך [A1].", {
      computations: [profit(), rate()],
    });
    const { id } = await openChip(page, answer);
    const calc = breakdown(page);
    await expect(calc).toHaveAttribute("data-id", "C2");
    await expect(calc.getByTestId("calc-result")).toContainText("47.5%");
    await expect(calc.getByTestId("calc-rounding")).toContainText("כאחוז");
    await calc.getByTestId("calc-nested").click();
    // the inner breakdown is on top, the outer one kept below it
    await expect(breakdownLevels(page)).toHaveCount(2);
    const inner = breakdownLevels(page).last();
    await expect(inner).toBeVisible();
    await expect(inner).toHaveAttribute("data-id", "C1");
    await expect(inner.getByTestId("calc-formula")).toContainText("עלויות שנתיות");
    await expect(breakdownLevels(page).first()).toBeHidden();
    // the back step returns to the outer breakdown, with focus on the input that opened the inner one
    await inner.getByTestId("calc-back").click();
    await expect(breakdownLevels(page)).toHaveCount(1);
    await expect(breakdown(page)).toHaveAttribute("data-id", "C2");
    await expect(breakdown(page).getByTestId("calc-nested")).toBeFocused();
    // and so does the browser's Back
    await breakdown(page).getByTestId("calc-nested").click();
    await expect(breakdownLevels(page)).toHaveCount(2);
    await page.goBack();
    await expect(breakdownLevels(page)).toHaveCount(1);
    await expect(breakdown(page)).toHaveAttribute("data-id", "C2");
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a result that reproduces a report value is labelled as written in the report", async ({ page }) => {
    const answer = answerWith("הסכום כפי שנכתב בשומה הוא 95,000 ₪ [C1].", {
      computations: [profit({ result_kind: "reproduces_report_value", result_kind_label: "משחזר ערך מהשומה",
        reproduces: { source: "S1", as_written: "95,000" }, inputs: [INPUT_V1, INPUT_V2], assumptions: [] })],
      assumptions: [],
    });
    const { id } = await openChip(page, answer);
    const calc = breakdown(page);
    await expect(calc.getByTestId("calc-result")).toHaveAttribute("data-result-kind", "reproduces_report_value");
    await expect(calc.getByTestId("calc-kind")).toHaveText("כתוב בשומה");
    await expect(calc.getByTestId("calc-reproduces")).toContainText("95,000");
    await expect(calc.getByTestId("calc-result")).not.toContainText("אינו כתוב במסמך");
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a conditional result says what it depends on", async ({ page }) => {
    const answer = answerWith(PROFIT_TEXT, {
      computations: [profit({ conditional: true, conditions: ["הכנסות ועלויות בבסיס מע״מ שונה"],
        justification: "הסכומים הושוו כפי שנכתבו" })],
    });
    const { id } = await openChip(page, answer);
    const conditional = breakdown(page).getByTestId("calc-conditional");
    await expect(conditional).toHaveAttribute("data-conditional", "true");
    await expect(conditional).toContainText("הכנסות ועלויות בבסיס מע״מ שונה");
    await expect(conditional).toContainText("הסכומים הושוו כפי שנכתבו");
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("an old answer without anchors shows the formula, and its inputs open the source window", async ({ page }) => {
    const { id } = await openChip(page, answerWith(PROFIT_TEXT, {}, false));
    const calc = breakdown(page);
    await expect(calc.getByTestId("calc-formula")).toContainText("הכנסות שנתיות");
    await calc.locator('[data-testid="calc-input"][data-kind="value"]').first().getByRole("button").click();
    const v = viewer(page);
    await expect(v).toBeVisible();
    await expect(v.getByTestId("viewer-tab-page")).toHaveCount(0);
    await expect(v.getByTestId("source-text-view")).toBeVisible();
    await expect(v.locator('[data-cited="true"]')).toContainText("ההכנסות השנתיות מהנכס");
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("an A# chip opens the assumption with the user's words and the message they come from", async ({ page }) => {
    const { id } = await openChip(page, answerWith(PROFIT_TEXT), 1);
    const view = page.getByRole("complementary", { name: "פירוט ההנחה" });
    await expect(view).toBeVisible();
    await expect(view.getByTestId("assumption-quote")).toContainText(QUOTE);
    await expect(view).toContainText("לא נתון מהמסמכים");
    // the calculation that uses it opens from here, with a way back
    await view.getByTestId("calc-nested").click();
    await expect(breakdown(page)).toHaveAttribute("data-id", "C1");
    await breakdown(page).getByTestId("calc-back").click();
    await expect(view).toBeVisible();
    await view.getByTestId("assumption-goto").click();
    await expect(page.locator('.msg-user[data-message-id="m1"]')).toBeFocused();
    await page.request.delete(`/api/chat/conversations/${id}`);
  });
});

// --- mobile ---------------------------------------------------------------------------------------------------------

test.describe("calculation breakdown (mobile)", () => {
  test.use({ viewport: { width: 375, height: 812 }, hasTouch: true });

  test("full screen; an input opens the viewer and Back returns to the breakdown, then to the conversation", async ({
    page,
  }) => {
    const { id } = await openChip(page, answerWith(PROFIT_TEXT));
    const calc = breakdown(page);
    await expect(calc).toBeVisible();
    const box = await calc.boundingBox();
    expect(box!.width).toBeGreaterThanOrEqual(370);
    expect(box!.height).toBeGreaterThanOrEqual(800);
    await calc.locator('[data-testid="calc-input"][data-kind="value"]').first().getByRole("button").click();
    await expect(viewer(page)).toBeVisible();
    await page.goBack();
    await expect(viewer(page)).toHaveCount(0);
    await expect(calc).toBeVisible();
    // the way back to the user's message closes the panels, which cover the conversation here
    await calc.getByTestId("calc-assumption-goto").click();
    await expect(breakdownLevels(page)).toHaveCount(0);
    await expect(page.locator('.msg-user[data-message-id="m1"]')).toBeFocused();
    await expect(page).toHaveURL(new RegExp(`/chat\\?c=${id}`));
    await page.request.delete(`/api/chat/conversations/${id}`);
  });
});
