import { readFileSync } from "node:fs";
import path from "node:path";
import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  actionsFor,
  answerWith,
  anchor,
  cid,
  component,
  count,
  decision,
  diagnostics,
  DOC,
  openAnswer,
  PARTIAL_MARK,
  READING,
  source,
  TITLE,
  VER,
  withServerPieces,
  type Json,
} from "./answer-fixtures";
import { sourceViewer } from "./helpers";

// Round 23 display check: the server's new wordings (the confirming reading, the rescue by a source the turn read, the column that
// could not be placed, the attribute that could not be confirmed from the source, the neutral "removed in verification" lines and the
// qualifier that was not confirmed) as they are shown. The conversation is real (office B, the synthetic demo user) but its messages,
// the diagnostics route and the cited document are fixed synthetic payloads in the shape the server writes (`compose.ServerSpan`):
// no model is called and every property, name and number is invented. The Hebrew sentences are the backend's own (see WORDINGS and
// the guard test that finds each of them in the backend source).

// --- the backend's wordings -----------------------------------------------------------------------------------------------

const BACKEND = path.resolve(__dirname, "../../backend/app/chat");
const fill = (template: string, values: Record<string, string | number>) =>
  template.replace(/\{(\w+)\}/g, (_all, k: string) => String(values[k]));

/** Each wording with the backend file it lives in and the fixed pieces the guard looks for in that file's source. */
const WORDINGS = {
  // verify.py PARTIAL_SENTENCES (the fixed sentence of a mark's reason class)
  sentenceConfirm: { file: "verify.py", text: "אומת חלקית: שתי בדיקות בלתי תלויות של הטענה מול המקורות לא הסכימו, והשנייה מצאה אותה נתמכת." },
  sentenceRescuedRead: { file: "verify.py", text: "אומת חלקית: הנתון נמצא במקור אחר מאותו מסמך שנקרא בתור, ולא במקור שהמשפט מצטט." },
  sentenceColumns: { file: "verify.py", text: "אומת חלקית: שיוך הנתון לעמודה או לתא בשורת הטקסט במקור לא נקבע." },
  // verify.py: the reason of each mark's log entry (MSG_CONFIRM_SPLIT, MSG_RESCUED_READ, MSG_COLUMN_UNKNOWN; a source string in two halves)
  reasonConfirm: { file: "verify.py", text: "שתי קריאות בלתי תלויות של הטענה מול המקורות לא הסכימו, והשנייה מצאה אותה נתמכת; הנתון נשאר מסומן כמאומת חלקית",
    pieces: ["שתי קריאות בלתי תלויות של הטענה מול המקורות לא הסכימו, והשנייה מצאה אותה נתמכת; ", "הנתון נשאר מסומן כמאומת חלקית"] },
  reasonRescuedRead: { file: "verify.py", text: "נתמך במקור {sid}, שהתור קרא מאותו מסמך ומאותה יחידה והתשובה לא ציטטה, ולא במקור שהיחידה עצמה מצטטת; הנתון נשאר מסומן כמאומת חלקית",
    pieces: ["נתמך במקור {sid}, שהתור קרא מאותו מסמך ומאותה יחידה והתשובה לא ציטטה, ולא במקור שהיחידה עצמה מצטטת; ", "הנתון נשאר מסומן כמאומת חלקית"] },
  reasonColumns: { file: "verify.py", text: "המספר כתוב בשורת טקסט שהשרת לא הצליח לשייך לעמודות שלה (אין מיקומי מילים שמורים), ולכן ההתאמה של הטענה לעמודה או לתא לא נבדקה; הנתון נשאר מסומן כמאומת חלקית",
    pieces: ["המספר כתוב בשורת טקסט שהשרת לא הצליח לשייך לעמודות שלה (אין מיקומי מילים שמורים), ולכן ההתאמה של הטענה לעמודה או לתא ", "לא נבדקה; הנתון נשאר מסומן כמאומת חלקית"] },
  // verify.py MSG_QUALIFIER_UNCONFIRMED (the qualifier edited out when the cited source was not read in full or clearly)
  qualifier: { file: "verify.py", text: "> הוסר ניסוח של {label} שלא אומת מהמקור לגבי {n} נתונים; הנתונים עצמם נשארו בתשובה." },
  // verify.py conditional note and tools.py attribute clauses ("could not be confirmed from the source")
  noteOne: { file: "verify.py", text: "התוצאה {ids} מותנית: {reason}" },
  stands: { file: "verify.py", text: "הערך {name} כתוב במקור; {what}" },
  unitUnconfirmed: { file: "tools.py", text: "היחידה: לא ניתן היה לאשר אותה מהמקור, והחישוב מניח {assumed}" },
  kindUnconfirmed: { file: "tools.py", text: "סוג הנתון: לא ניתן היה לאשר אותו מהמקור, והחישוב מניח שהוא «{assumed}»" },
  // coverage.py: the neutral removal (no kind named), in the gap paragraph and in the item line
  // (f-strings in the source: the guard looks for their fixed parts)
  gapRemoved: { file: "coverage.py", text: "{who}: מה שנכתב על כך בתשובה הוסר באימות.",
    pieces: [": מה שנכתב על כך בתשובה הוסר באימות"] },
  itemRemoved: { file: "coverage.py", text: "{name}: נקרא {place}, אך מה שנכתב עליו בתשובה הוסר באימות.",
    pieces: [": נקרא {place}, אך מה שנכתב עליו בתשובה הוסר באימות"] },
} as const;

type WordingKey = keyof typeof WORDINGS;
const W = (key: WordingKey, values: Record<string, string | number> = {}) => fill(WORDINGS[key].text, values);

// --- the synthetic answer -------------------------------------------------------------------------------------------------

const PROPERTY = "בניין הרימון";
const UNIT_ROW = "דירה 4";
const HEADERS = ["דירה", "שווי למ״ר", "שטח במ״ר"];
const CLAIM_CONFIRM = cid(0x81);
const CLAIM_RESCUED = cid(0x82);
const CLAIM_COLUMNS = cid(0x83);
const SENT_CONFIRM = "שיעור ההיוון בבניין הוא 6.3% לשנה";
const SENT_RESCUED = "דמי הניהול החודשיים בבניין הם 410 ₪";
const SENT_COLUMNS = "שטח הדירה הוא 82 מ״ר";
const ITEM_NAME = "«מקדם התאמה לקומה»";
const ITEM_LOCATION = "עמוד 3";
const GAP_COMPONENT = "שיעור הריבית הממוצע";
const COND_RESULT = "1,030,000";
const COND_NAME = "«שווי למ״ר של דירה 4» (11,300 ₪)";

const NOTE_QUALIFIER = W("qualifier", { label: "התקופה", n: 2 });
const NOTE_CONDITIONAL = W("noteOne", {
  ids: COND_RESULT,
  reason: W("stands", {
    name: COND_NAME,
    what: [W("unitUnconfirmed", { assumed: "₪ למ״ר" }), W("kindUnconfirmed", { assumed: "שווי ליחידת שטח" })].join("; "),
  }) + ".",
});
const LINE_GAP = W("gapRemoved", { who: `**${GAP_COMPONENT}**` });
const LINE_ITEM = W("itemRemoved", { name: ITEM_NAME, place: `ב"${TITLE}" (${ITEM_LOCATION}) [S3]` });

/** The anchor of the cell V1 cites: the server's v2 table anchor (`anchors.py`), row «דירה 4», column «שווי למ״ר». */
function cellAnchor(): Json {
  const base = anchor() as { pages: Json[]; location: Json } & Json;
  return {
    ...base, v: 2, precision: "cell", precision_label: "התא מסומן בטבלה",
    pages: [{ page: 3, printed_label: null, width: 600, height: 900, rects: [[0.5, 0.4, 0.7, 0.43]], focus: [] }],
    location: { title: TITLE, label: `עמוד 3, טבלה «טבלת דירות», שורה «${UNIT_ROW}», עמודה «שווי למ״ר»`, page: 3,
      page_end: null, printed_page: null, section: null },
    table: {
      table_index: 0, title: "טבלת דירות", row_label: UNIT_ROW, row_number: 1, column_header: "שווי למ״ר", column_number: 2,
      unit_note: "הסכומים בשקלים חדשים", source: null, direction: { reversed: true, basis: "boxes", inferred: true },
      header: { page: 3, rects: [[0.5, 0.35, 0.7, 0.38]] }, notes: [{ text: "השווי נקוב לפני מע״מ", page: 3 }],
    },
  };
}

function cellValue(): Json {
  return {
    id: "V1", value: "11300", value_text: "11,300 ₪", label: "שווי למ״ר של דירה 4", source_id: "S2", document_id: DOC,
    version_id: VER, reading_id: READING, title: TITLE, location: "עמוד 3, טבלה", kind: "amount", unit: "ILS",
    unit_label: "₪", period: "none", vat: "excluded", area_basis: "", subject: UNIT_ROW, role: "unknown",
    provenance: { unit: "source", period: "not_stated", vat: "source" }, certainty: "verified",
    locator: { row: UNIT_ROW, column: "שווי למ״ר", row_number: 1, column_number: 2 }, quote: `${UNIT_ROW} | 11,300 ₪`,
    total: false, approx: false, anchor: cellAnchor(),
  };
}

const SENTENCE_BY_CHECK: Record<string, string> = {
  confirm_split: W("sentenceConfirm"),
  rescue_read: W("sentenceRescuedRead"),
  column_unknown: W("sentenceColumns"),
};

function answer(): Json {
  const { markdown, server_spans } = withServerPieces([
    `ב${PROPERTY} נמצא שווי של 11,300 ₪ למ״ר לדירה 4 [S1].\n\n`,
    `סך העלות המחושב לדירה 4 הוא ${COND_RESULT} ₪ (תוצאה מותנית) [S1].\n\n`,
    `${SENT_CONFIRM} [S1]`,
    { text: PARTIAL_MARK, span: { kind: "partial_mark", ordinal: 1, claim_ids: [CLAIM_CONFIRM] } },
    `.\n\n${SENT_RESCUED} [S1]`,
    { text: PARTIAL_MARK, span: { kind: "partial_mark", ordinal: 2, claim_ids: [CLAIM_RESCUED] } },
    `.\n\n| ${HEADERS.join(" | ")} |\n| ${HEADERS.map(() => "---").join(" | ")} |\n| ${UNIT_ROW} | 11,300 ₪ [V1] | 82`,
    { text: PARTIAL_MARK, span: { kind: "partial_mark", ordinal: 3, claim_ids: [CLAIM_COLUMNS], datum: "V2" } },
    ` |\n\nהנתונים לפי השומה הסינתטית.`,
    { text: `\n\n${NOTE_QUALIFIER}`, span: { kind: "removal_note" } },
    { text: `\n\n${NOTE_CONDITIONAL}`, span: { kind: "conditional_note" } },
    { text: `\n\n${LINE_GAP}`, span: { kind: "gap_paragraph" } },
    { text: `\n\n${LINE_ITEM}`, span: { kind: "item_gap" } },
  ]);
  const partials = [["confirm_split", CLAIM_CONFIRM], ["rescue_read", CLAIM_RESCUED], ["column_unknown", CLAIM_COLUMNS]]
    .map(([check, claim], i) => ({ ordinal: i + 1, claim_id: claim, claim_ids: [claim], failure_kind: null, check,
      text: SENTENCE_BY_CHECK[check] }));
  const gapRow = { reason: "removed", reason_text: "הוסר באימות", components: ["N2"], texts: [GAP_COMPONENT], text: LINE_GAP };
  return answerWith({
    markdown, server_spans, status: "partial",
    sources: [source("S1"), source("S2", { anchor: cellAnchor(), table_index: 0, kind: "table" }), source("S3")],
    values: [cellValue()],
    components: [
      component("N1", "השווי למ״ר", "full", { units: [0], related: ["S1"] }),
      component("N2", GAP_COMPONENT, "not_answered", { limitation: "removed", limitation_text: "הוסר באימות", stated: true,
        gap: LINE_GAP }),
    ],
    gaps: [gapRow],
  }, {
    removed: 0, partial: 3, conditional: 1, correctness: "partial", partials, removals: [],
    actions: actionsFor([], { qualified: 3 }),
    completeness: { status: "partial", requirements: 2, missing: [
      { id: "N2", text: GAP_COMPONENT, status: "not_answered", reason: "removed", parent: "", conditional: false }] },
  });
}

/** The log entry a partial mark opens (`decisions._partial_detail`): `reason` carries the lead "נתמך חלקית: " the server writes,
 * `not_verified` is the reason without it (the claim is a whole unit), `supported` the sentence as it stays. */
function markEntry(claim: string, check: string, why: string, text: string, checked: string[], rereads: Json[] = []): Json {
  return decision("absent_from_source", {
    claim_id: claim, action: "qualified", failure_kind: null, check, severity: "partial", verdict: "partial", text, remaining: text,
    reason: `נתמך חלקית: ${why}`, repair_attempted: false, supported: text, not_verified: why,
    why: { failure_kind: null, check }, checked: { sources: checked, rereads }, sources_checked: checked,
    sources: checked.map((id) => ({ ...source(id), type: "sources" })),
  });
}

const REASON_CONFIRM = W("reasonConfirm");
const REASON_RESCUED = W("reasonRescuedRead", { sid: "S3" });
const REASON_COLUMNS = W("reasonColumns");

function answerDiagnostics(): Json {
  return diagnostics([
    markEntry(CLAIM_CONFIRM, "confirm_split", REASON_CONFIRM, SENT_CONFIRM, ["S1"]),
    markEntry(CLAIM_RESCUED, "rescue_read", REASON_RESCUED, SENT_RESCUED, ["S1", "S3"]),
    markEntry(CLAIM_COLUMNS, "column_unknown", REASON_COLUMNS, `${UNIT_ROW} | 11,300 ₪ | 82`, ["S2"],
      [{ source: "S2", scope: "next_page", outcome: "not_found" }]),
  ]);
}

// --- helpers --------------------------------------------------------------------------------------------------------------

const norm = (s: string) => s.replace(/\s+/g, " ").trim();
const bodyText = async (reply: Locator) => norm(await reply.locator(".body").innerText());
const replyText = async (reply: Locator) => norm(await reply.innerText());
const pageOverflow = (page: Page) => page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
const cellOf = (el: Locator) => el.locator("xpath=ancestor::td[1]");

async function open(page: Page, canViewDiagnostics: boolean) {
  const served = await openAnswer(page, answer(), { can_view_diagnostics: canViewDiagnostics });
  await page.route("**/api/chat/messages/m2/diagnostics", (route) => route.fulfill({ json: answerDiagnostics() }));
  return served;
}

/** The pieces of the server's lines whose text a citation chip does not split. */
const ITEM_HEAD = norm(`${ITEM_NAME}: נקרא ב"${TITLE}" (${ITEM_LOCATION})`);
const ITEM_TAIL = "אך מה שנכתב עליו בתשובה הוסר באימות.";
const BODY_WORDINGS: [string, string][] = [
  ["qualifier note", norm(NOTE_QUALIFIER.replace(/^>\s*/, ""))],
  ["conditional note", norm(NOTE_CONDITIONAL)],
  ["gap line (neutral removal)", norm(LINE_GAP.replace(/\*\*/g, ""))],
  ["item line head", ITEM_HEAD],
  ["item line tail (neutral removal)", ITEM_TAIL],
];

// --- tests ----------------------------------------------------------------------------------------------------------------

test.describe("round 23 wordings", () => {
  test("guard: every Hebrew wording of this test is in the backend source it names", () => {
    const sources = new Map<string, string>();
    for (const [key, w] of Object.entries(WORDINGS)) {
      if (!sources.has(w.file)) sources.set(w.file, readFileSync(path.join(BACKEND, w.file), "utf8"));
      const src = sources.get(w.file)!;
      const pieces = "pieces" in w ? w.pieces : [w.text];
      for (const piece of pieces) expect(src.includes(piece), `${key}: «${piece}» not found in backend/app/chat/${w.file}`).toBe(true);
    }
  });

  test("each server line is visible exactly once in the answer body, in the server's order and after the text", async ({ page }) => {
    const { id, reply } = await open(page, true);
    const body = await bodyText(reply);
    for (const [label, text] of BODY_WORDINGS) expect(count(body, text), `${label} once in the body`).toBe(1);
    // the fixed reason sentences and the log reasons live in the details, never in the body
    for (const s of Object.values(SENTENCE_BY_CHECK)) expect(count(body, norm(s)), "a mark's sentence is not in the body").toBe(0);
    for (const r of [REASON_CONFIRM, REASON_RESCUED, REASON_COLUMNS]) expect(count(body, norm(r))).toBe(0);
    // the order compose writes: the text, the table, the removal note, the conditional note, the gap paragraph, the item line
    const at = (t: string) => body.indexOf(t);
    expect(at("הנתונים לפי השומה הסינתטית.")).toBeLessThan(at(BODY_WORDINGS[0][1]));
    expect(at(BODY_WORDINGS[0][1])).toBeLessThan(at(BODY_WORDINGS[1][1]));
    expect(at(BODY_WORDINGS[1][1])).toBeLessThan(at(BODY_WORDINGS[2][1]));
    expect(at(BODY_WORDINGS[2][1])).toBeLessThan(at(ITEM_HEAD));
    // the right element: the qualifier note is a blockquote, the others are paragraphs of their own (the item line holds head and tail)
    const quote = reply.locator(".body blockquote");
    await expect(quote).toHaveCount(1);
    await expect(quote).toContainText(BODY_WORDINGS[0][1]);
    for (const text of [BODY_WORDINGS[1][1], BODY_WORDINGS[2][1]]) {
      const p = reply.locator(".body p").filter({ hasText: text });
      await expect(p).toHaveCount(1);
      expect(norm(await p.innerText())).toBe(text);
    }
    const item = reply.locator(".body p").filter({ hasText: ITEM_TAIL });
    await expect(item).toHaveCount(1);
    await expect(item).toContainText(ITEM_HEAD);
    // the server's lines are plain text: no button, no mark element
    for (const el of [quote, item, reply.locator(".body p").filter({ hasText: BODY_WORDINGS[1][1] })]) {
      await expect(el.locator("button:not(.cite), [data-testid]")).toHaveCount(0);
    }
    // no raw markdown reaches the screen
    expect(body).not.toMatch(/\*\*|\[S\d\]|\[V\d\]|^>|\| ---/);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("the three partial marks are marks: the confirming reading and the rescue in their sentences, the unplaced column inside its table cell", async ({ page }) => {
    const { id, reply } = await open(page, true);
    const marks = reply.getByTestId("partial-mark");
    await expect(marks).toHaveCount(3);
    for (let i = 0; i < 3; i++) {
      await expect(marks.nth(i)).toHaveText("(אומת חלקית)");
      await expect(marks.nth(i)).toBeEnabled();
      await expect(marks.nth(i)).toHaveAttribute("data-ordinal", String(i + 1));
    }
    // 1 and 2 follow their sentence in a paragraph, outside any table; 3 is the last cell of the row, after its value
    for (const [i, sentence] of [[0, SENT_CONFIRM], [1, SENT_RESCUED]] as const) {
      await expect(marks.nth(i).locator("xpath=ancestor::td")).toHaveCount(0);
      const p = marks.nth(i).locator("xpath=ancestor::p[1]");
      await expect(p).toContainText(sentence);
      const text = norm(await p.innerText());
      expect(text.indexOf(sentence)).toBeLessThan(text.indexOf("(אומת חלקית)"));
    }
    const cell = cellOf(marks.nth(2));
    expect(await cell.evaluate((td) => (td as HTMLTableCellElement).cellIndex)).toBe(2);
    expect(await cell.evaluate((td) => (td.parentElement as HTMLTableRowElement).rowIndex)).toBe(1);
    await expect(cell).toContainText("82");
    expect(norm(await cell.innerText()).indexOf("82")).toBeLessThan(norm(await cell.innerText()).indexOf("(אומת חלקית)"));
    await expect(reply.locator(".body table thead th").nth(2)).toHaveText("שטח במ״ר");
    // the mark's box lies inside its cell
    const [m, c] = [(await marks.nth(2).boundingBox())!, (await cell.boundingBox())!];
    expect(m.x).toBeGreaterThanOrEqual(c.x - 1);
    expect(m.x + m.width).toBeLessThanOrEqual(c.x + c.width + 1);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a mark opens its own entry: the log reason of each reading shows once, with the check that made the mark", async ({ page }) => {
    const { id, reply } = await open(page, true);
    const expected: [number, string, string, string][] = [
      [1, CLAIM_CONFIRM, REASON_CONFIRM, "confirm_split"],
      [2, CLAIM_RESCUED, REASON_RESCUED, "rescue_read"],
      [3, CLAIM_COLUMNS, REASON_COLUMNS, "column_unknown"],
    ];
    await expect(reply.locator("details.msg-details")).not.toHaveAttribute("open", "");
    for (const [ordinal, claim, reason, check] of expected) {
      await reply.locator(`[data-testid="partial-mark"][data-ordinal="${ordinal}"]`).click();
      await expect(reply.locator("details.msg-details")).toHaveAttribute("open", "");
      const entry = reply.locator(`[data-testid="change"][data-claim-id="${claim}"]`);
      await expect(entry).toBeVisible();
      await expect(entry.getByTestId("partial-not-verified")).toContainText(norm(reason));
      await expect(entry.getByTestId("partial-why")).toContainText(check);
      await expect(entry.getByTestId("partial-checked")).toBeVisible();
      // the reason is on the screen once, and no other mark's reason is
      const all = await replyText(reply);
      expect(count(all, norm(reason)), `${check}: the log reason once`).toBe(1);
      for (const [, , other] of expected.filter((e) => e[1] !== claim)) {
        expect(count(await replyText(entry), norm(other)), `${check}: not another mark's reason`).toBe(0);
      }
    }
    // the rescue entry lists the source the turn read and the column entry its re-read
    await expect(reply.locator(`[data-testid="change"][data-claim-id="${CLAIM_RESCUED}"] [data-testid="partial-checked"]`)).toContainText("S3");
    await expect(reply.locator(`[data-testid="change"][data-claim-id="${CLAIM_COLUMNS}"] [data-testid="partial-checked"]`))
      .toContainText("קריאה חוזרת של S2");
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a viewer who may not open the diagnostics sees the marks as text and each fixed reason sentence once, in the details", async ({ page }) => {
    const { id, reply } = await open(page, false);
    await expect(reply.getByTestId("partial-mark")).toHaveCount(0);
    expect(count(await bodyText(reply), "(אומת חלקית)")).toBe(3);
    // closed details hide the sentences; opened, the list holds each once, in the order of the marks
    expect(count(await replyText(reply), norm(W("sentenceConfirm")))).toBe(0);
    await reply.locator("details.msg-details > summary").click();
    const list = reply.getByTestId("partial-reasons");
    await expect(list).toBeVisible();
    await expect(list.locator("li")).toHaveText([W("sentenceConfirm"), W("sentenceRescuedRead"), W("sentenceColumns")]);
    for (const s of Object.values(SENTENCE_BY_CHECK)) expect(count(await replyText(reply), norm(s)), s).toBe(1);
    // the log reasons need the diagnostics route: they are not shown to this viewer
    for (const r of [REASON_CONFIRM, REASON_RESCUED, REASON_COLUMNS]) expect(count(await replyText(reply), norm(r))).toBe(0);
    // the neutral removal in the components list: the limitation label, and the gap line is still only in the body
    expect(count(await bodyText(reply), norm(LINE_GAP.replace(/\*\*/g, "")))).toBe(1);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("the citations open the mocked source: a sentence at its block and page, a table cell at its row and column", async ({ page }) => {
    const { id, reply } = await open(page, true);
    const viewer = sourceViewer(page);
    // [S1] in the first sentence: the document, the page and the cited block
    const s1 = reply.locator(".body p").first().locator(".cite");
    await expect(s1).toHaveCount(1);
    await s1.click();
    await expect(viewer).toBeVisible();
    await expect(viewer.getByTestId("viewer-title")).toContainText(TITLE);
    await expect(viewer.getByTestId("viewer-page")).toContainText("עמוד 3 בקובץ");
    await expect(viewer.getByTestId("viewer-precision")).toHaveText("המקום המצוטט מסומן בעמוד");
    await expect(viewer.getByTestId("viewer-highlight")).toHaveCount(1);
    await expect(viewer).toContainText("סעיף «4. שיטת ההיוון»");
    await page.keyboard.press("Escape");
    await expect(viewer).toHaveCount(0);
    // [V1] in the table: the chip sits in row 1, second column, and opens that cell with its labels and notes
    const chip = reply.locator(".body table .cite");
    await expect(chip).toHaveCount(1);
    const td = cellOf(chip);
    expect(await td.evaluate((c) => (c as HTMLTableCellElement).cellIndex)).toBe(1);
    expect(await td.evaluate((c) => (c.parentElement as HTMLTableRowElement).rowIndex)).toBe(1);
    await expect(td).toContainText("11,300 ₪");
    await chip.click();
    await expect(viewer).toBeVisible();
    await expect(viewer.getByTestId("viewer-precision")).toHaveText("התא מסומן בטבלה");
    await expect(viewer.getByTestId("viewer-table-row")).toHaveText(UNIT_ROW);
    await expect(viewer.getByTestId("viewer-table-column")).toHaveText("שווי למ״ר");
    await expect(viewer.getByTestId("viewer-table-notes")).toContainText("השווי נקוב לפני מע״מ");
    await expect(viewer.getByTestId("viewer-highlight")).toHaveCount(1);
    await expect(viewer.getByTestId("viewer-header-highlight")).toHaveCount(1);
    expect(await viewer.evaluate((v) => getComputedStyle(v).direction)).toBe("rtl");
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("right to left: the body, the table and every server line read right to left and are aligned to the right", async ({ page }) => {
    const { id, reply } = await open(page, true);
    const dir = (loc: Locator) => loc.evaluate((el) => ({ direction: getComputedStyle(el).direction, align: getComputedStyle(el).textAlign }));
    expect((await dir(reply.locator(".body"))).direction).toBe("rtl");
    expect(await reply.locator(".body table").evaluate((t) => getComputedStyle(t).direction)).toBe("rtl");
    const headers = reply.locator(".body table thead th");
    await expect(headers).toHaveText(HEADERS);
    const xs: number[] = [];
    for (let i = 0; i < HEADERS.length; i++) xs.push((await headers.nth(i).boundingBox())!.x);
    for (let i = 1; i < xs.length; i++) expect(xs[i]).toBeLessThan(xs[i - 1]);
    const bodyBox = (await reply.locator(".body").boundingBox())!;
    for (const [label, text] of BODY_WORDINGS) {
      const block = reply.locator(".body p, .body blockquote").filter({ hasText: text.slice(0, 30) }).first();
      const d = await dir(block);
      expect(d.direction, `${label}: direction`).toBe("rtl");
      expect(["start", "right"], `${label}: alignment`).toContain(d.align);
      const box = (await block.boundingBox())!;
      expect(box.x + box.width, `${label}: starts at the right edge of the body`).toBeGreaterThan(bodyBox.x + bodyBox.width - 40);
    }
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("after a reload the conversation is kept and the same text, marks and notes are rendered", async ({ page }) => {
    const { id } = await open(page, true);
    const snapshot = async () => {
      const reply = page.locator(".msg-assistant").last();
      await expect(reply.locator(".body table")).toHaveCount(1);
      return {
        body: await bodyText(reply),
        cells: await reply.locator(".body td").allInnerTexts(),
        marks: await reply.getByTestId("partial-mark").evaluateAll((els) => els.map((e) => ({
          ordinal: e.getAttribute("data-ordinal"), text: (e.textContent ?? "").trim(), inCell: !!e.closest("td"),
          row: (e.closest("tr") as HTMLTableRowElement | null)?.rowIndex ?? -1 }))),
        quote: await reply.locator(".body blockquote").allInnerTexts(),
        cites: await reply.locator(".body .cite").count(),
      };
    };
    const before = await snapshot();
    expect(before.marks).toHaveLength(3);
    const messages = page.locator(".msg-user");
    const userBefore = await messages.count();
    await page.reload();
    await expect(page.locator(".msg-assistant")).toHaveCount(1);
    expect(await messages.count(), "the question is kept").toBe(userBefore);
    expect(page.url()).toContain(`c=${id}`);
    const after = await snapshot();
    expect(after).toEqual(before);
    for (const [label, text] of BODY_WORDINGS) expect(count(after.body, text), `${label} once after the reload`).toBe(1);
    // a mark still opens its entry
    await page.locator(".msg-assistant").last().locator('[data-testid="partial-mark"][data-ordinal="1"]').click();
    await expect(page.locator(".msg-assistant").last().locator(`[data-testid="change"][data-claim-id="${CLAIM_CONFIRM}"]`)
      .getByTestId("partial-not-verified")).toContainText(norm(REASON_CONFIRM));
    await page.request.delete(`/api/chat/conversations/${id}`);
  });
});

test.describe("round 23 wordings (mobile)", () => {
  test.use({ viewport: { width: 375, height: 812 }, hasTouch: true });

  test("at 375px nothing widens the page: the server lines wrap, the table scrolls in its own container, the entries open", async ({ page }) => {
    const { id, reply } = await open(page, true);
    expect(await pageOverflow(page)).toBeLessThanOrEqual(0);
    const body = await bodyText(reply);
    for (const [label, text] of BODY_WORDINGS) expect(count(body, text), `${label} once`).toBe(1);
    // every server line lies inside the screen
    for (const [label, text] of BODY_WORDINGS) {
      const block = reply.locator(".body p, .body blockquote").filter({ hasText: text.slice(0, 30) }).first();
      const box = (await block.boundingBox())!;
      expect(box.x, `${label}: left edge`).toBeGreaterThanOrEqual(-1);
      expect(box.x + box.width, `${label}: right edge`).toBeLessThanOrEqual(376);
    }
    const wrap = reply.locator(".body .table-wrap");
    const sizes = await wrap.evaluate((el) => ({ client: el.clientWidth, view: window.innerWidth, overflow: getComputedStyle(el).overflowX }));
    expect(sizes.client).toBeLessThanOrEqual(sizes.view);
    expect(sizes.overflow).toBe("auto");
    // the three entries open without widening the page
    for (const ordinal of [1, 2, 3]) {
      await reply.locator(`[data-testid="partial-mark"][data-ordinal="${ordinal}"]`).click();
      await expect(reply.getByTestId("partial-not-verified").last()).toBeVisible();
      expect(await pageOverflow(page)).toBeLessThanOrEqual(0);
    }
    for (const r of [REASON_CONFIRM, REASON_RESCUED, REASON_COLUMNS]) expect(count(await replyText(reply), norm(r))).toBe(1);
    // and the source viewer opens over the answer
    await reply.locator(".body table .cite").click();
    await expect(sourceViewer(page)).toBeVisible();
    expect(await pageOverflow(page)).toBeLessThanOrEqual(0);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });
});
