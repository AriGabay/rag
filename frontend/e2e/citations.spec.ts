import fs from "node:fs";
import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import {
  apiLogin,
  citationChip,
  citationOrder,
  currentReading,
  deleteOfficeBDocsByTitle,
  ensureOfficeBDocuments,
  ensureOfficeBEmployee,
  ensureOfficeBGroup,
  fixture,
  lastAnswer,
  login,
  officeBSettings,
  reprocessOfficeB,
  sendAndWait,
  setOfficeBCloud,
  setOfficeBUserGroups,
  shot,
  uploadOfficeBVersion,
  USERS,
  type OfficeBDoc,
} from "./helpers";

// End-to-end citations (U14, R33, AE1–AE4) against the real stack, office B only: synthetic documents with known
// positions (backend/tests/fixtures/citations/, see its manifest.json) are uploaded and processed, a question is asked
// in limited mode (office B's cloud use off: the reply is search results with citations, no model involved), and a
// citation is opened. For each, the test checks:
//   - the page image the viewer requested: its document, version, file page and the reading the citation was made from;
//   - the highlight rectangle, measured on screen relative to the page image, against the manifest's box, within the
//     manifest's tolerance (0.02 of the page's width or height on each edge);
//   - that the page image is text-coloured (dark) at the rectangle's centre: a pixel darker than mid grey within ±1% of
//     the page width and ±30% of the rectangle's height around it (a centre can fall between two words).
// The manifest's boxes come from the generator's own layout, never from the application's readers.
//
// Precision expected per document (manifest `expect`): a search passage is cited as its blocks (heading and sentence),
// a table row as the row's cells (region "row"), a scanned page at page level (OCR lines carry no positions, so no
// rectangle is drawn), a row of a table read from a picture at table level on the picture's region (needs the vision
// model when the document is processed), and a DOCX citation in the structured view (no page).
//
// Two scenarios need office B's model (gpt-6-luna) and are tagged @model: the picture table (read by the vision model
// at upload) and a computation from two DOCX table cells. They are skipped when office B has no model key, or with
// E2E_SKIP_MODEL=1. Office B's cloud use is switched off again after each.
//
// The last scenario reprocesses every office B document through the admin API (an old conversation must keep its
// original highlight), so it runs last and takes several minutes.

type Box = [number, number, number, number];

interface Place {
  page: number;
  text: string;
  box: Box;
}

interface Expectation {
  precision: string;
  region?: string;
  target: string;
  also?: string[];
  contains?: string;
  avoid?: string;
  header?: string;
  page?: number;
  pick: { kind: string; contains: string };
}

interface PdfEntry {
  file: string;
  title: string;
  kind: string;
  pages: number;
  places: Record<string, Place>;
  query: string;
  expect: Expectation;
  replacement?: PdfEntry;
}

interface Manifest {
  tolerance: number;
  documents: {
    digital: PdfEntry;
    scanned: PdfEntry;
    mixed: PdfEntry;
    rotated: PdfEntry;
    repeated: Omit<PdfEntry, "expect"> & { expect: { row: Expectation; paragraph: Expectation }; number: string };
    cross_page: PdfEntry;
    docx: {
      file: string;
      title: string;
      query: string;
      section: string;
      expect: { precision: string; pick: { kind: string; contains: string } };
      computation: { row: string; inputs: string[]; result: string };
    };
    replaced: PdfEntry & { replacement: PdfEntry };
  };
}

interface Anchor {
  precision: string;
  region: string | null;
  document_id: string;
  version_id: string;
  reading_id: string | null;
  pages: { page: number; rects: Box[] }[];
  structured: { cell: unknown } | null;
}

interface Source {
  id: string;
  document_id: string | null;
  kind: string;
  title: string;
  text: string | null;
  anchor: Anchor | null;
}

interface Answer {
  kind: string;
  markdown: string;
  sources: Source[];
  computations: { id: string }[];
}

const MANIFEST = JSON.parse(fs.readFileSync(fixture("citations/manifest.json"), "utf-8")) as Manifest;
const DOCS = MANIFEST.documents;
const TOL = MANIFEST.tolerance;
const GROUP = "ציטוטים לבדיקה (סינתטי)";
const VIEWER = { email: "viewer-b@demo.test", name: "צופה בדיקות ציטוטים (סינתטי)" };
const LABEL: Record<string, string> = {
  block: "הקטע מסומן בעמוד",
  region_row: "השורה מסומנת בטבלה",
  region_table: "הסימון ברמת הטבלה",
  page: "ברמת העמוד",
  structured: "מבנה המסמך (ללא עמודים)",
};

// --- setup -----------------------------------------------------------------------------------------------------------

interface Setup {
  groupId: string;
  viewerId: string;
  docs: Record<string, OfficeBDoc>;
  modelReady: boolean;
}

let setup: Setup | null = null;

const LIMITED = [DOCS.digital, DOCS.scanned, DOCS.rotated, DOCS.repeated, DOCS.cross_page, DOCS.docx];

async function prepare(request: APIRequestContext): Promise<Setup> {
  if (setup) return setup;
  await setOfficeBCloud(request, false);
  const groupId = await ensureOfficeBGroup(request, GROUP);
  const viewerId = await ensureOfficeBEmployee(request, VIEWER.email, VIEWER.name, [groupId]);
  const docs = await ensureOfficeBDocuments(
    request,
    LIMITED.map((d) => ({ file: fixture(d.file), title: d.title })),
    { groupId, timeout: 1_500_000 },
  );
  const settings = await officeBSettings(request);
  setup = { groupId, viewerId, docs, modelReady: settings.key_present && process.env.E2E_SKIP_MODEL !== "1" };
  return setup;
}

/** Whether the version's reading holds a table the vision model read from a picture. */
async function hasVisionTable(request: APIRequestContext, doc: OfficeBDoc): Promise<boolean> {
  await apiLogin(request, USERS.adminB);
  const res = await request.get(`/api/documents/${doc.id}/versions/${doc.versionId}/blocks`);
  expect(res.ok(), `blocks of ${doc.id}: ${res.status()}`).toBeTruthy();
  const { blocks } = (await res.json()) as { blocks: { table?: { source?: string | null } }[] };
  await request.post("/api/auth/logout");
  return blocks.some((b) => b.table?.source === "vision");
}

// --- asking and opening -------------------------------------------------------------------------------------------

function viewerOf(page: Page): Locator {
  return page.getByRole("complementary", { name: "תצוגת מקור" });
}

/** A new conversation with one question; the reply must be the plainly labelled search results (limited mode). */
async function askLimited(page: Page, question: string) {
  await page.getByRole("button", { name: "שיחה חדשה" }).first().click();
  const reply = await sendAndWait(page, question);
  await expect(reply).toContainText("תוצאות חיפוש בלבד");
  const { conversationId, answer } = await lastAnswer<Answer>(page);
  expect(answer.kind).toBe("search_only");
  return { reply, answer, conversationId };
}

/** The answer's source of document `docId` that the manifest names: its kind and a phrase of its text. */
function pickSource(answer: Answer, docId: string, pick: { kind: string; contains: string }): Source {
  const found = answer.sources.find(
    (s) => s.document_id === docId && s.kind === pick.kind && (s.text ?? "").includes(pick.contains),
  );
  const listed = answer.sources.map((s) => `${s.id} ${s.kind} «${s.title}» ${(s.text ?? "").slice(0, 50)}`).join(" / ");
  expect(found, `a ${pick.kind} source with «${pick.contains}» among: ${listed}`).toBeTruthy();
  expect(found!.anchor, `${found!.id} carries an anchor`).toBeTruthy();
  return found!;
}

async function openCitation(page: Page, reply: Locator, answer: Answer, id: string): Promise<Locator> {
  await citationChip(reply, answer.markdown, id).click();
  const viewer = viewerOf(page);
  await expect(viewer).toBeVisible();
  return viewer;
}

// --- measuring what is shown --------------------------------------------------------------------------------------

interface Shown {
  frame: Locator;
  image: Locator;
  src: URL;
  rects: Box[];
  headerRects: Box[];
}

async function relative(image: Locator, el: Locator): Promise<Box> {
  const a = await image.boundingBox();
  const b = await el.boundingBox();
  expect(a && b, "the page image and the rectangle are laid out").toBeTruthy();
  return [(b!.x - a!.x) / a!.width, (b!.y - a!.y) / a!.height, (b!.x + b!.width - a!.x) / a!.width,
    (b!.y + b!.height - a!.y) / a!.height];
}

/** File page `pageNo` as the viewer shows it: its loaded image, the image URL it requested and its rectangles. */
async function shownPage(viewer: Locator, pageNo: number): Promise<Shown> {
  const frame = viewer.locator(`[data-testid="viewer-page-frame"][data-page="${pageNo}"]`);
  const image = frame.getByTestId("viewer-page-image");
  await expect(image).toBeVisible();
  await expect.poll(() => image.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth > 0)).toBe(true);
  const src = new URL((await image.getAttribute("data-src"))!, "http://stack.test");
  const rects: Box[] = [];
  for (const hl of await frame.getByTestId("viewer-highlight").all()) rects.push(await relative(image, hl));
  const headerRects: Box[] = [];
  for (const hl of await frame.getByTestId("viewer-header-highlight").all()) headerRects.push(await relative(image, hl));
  return { frame, image, src, rects, headerRects };
}

function expectImageUrl(src: URL, doc: OfficeBDoc, pageNo: number, reading: string | null, scale = "normal") {
  expect(src.pathname, "the page image of the cited version and page").toBe(
    `/api/documents/${doc.id}/versions/${doc.versionId}/pages/${pageNo}/image`,
  );
  expect(src.searchParams.get("scale")).toBe(scale);
  expect(src.searchParams.get("reading_id"), "the reading the citation was made from").toBe(reading ?? "none");
}

const fmt = (b: Box) => `[${b.map((v) => v.toFixed(3)).join(", ")}]`;

function near(a: Box, b: Box): boolean {
  return a.every((v, i) => Math.abs(v - b[i]) <= TOL);
}

function covers(outer: Box, inner: Box): boolean {
  return outer[0] <= inner[0] + TOL && outer[1] <= inner[1] + TOL && outer[2] >= inner[2] - TOL && outer[3] >= inner[3] - TOL;
}

function overlaps(a: Box, b: Box): boolean {
  return Math.min(a[2], b[2]) - Math.max(a[0], b[0]) > 0.002 && Math.min(a[3], b[3]) - Math.max(a[1], b[1]) > 0.002;
}

/** The page image's pixels around the centre of `box` (fractions of the image), read through a canvas: the darkest
 * luminance and how many pixels are darker than mid grey. */
async function inkAt(image: Locator, box: Box): Promise<{ darkest: number; dark: number }> {
  return image.evaluate((img: HTMLImageElement, r) => {
    const w = img.naturalWidth;
    const h = img.naturalHeight;
    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d")!;
    ctx.drawImage(img, 0, 0);
    const cx = ((r[0] + r[2]) / 2) * w;
    const cy = ((r[1] + r[3]) / 2) * h;
    const dx = Math.max(2, 0.01 * w);
    const dy = Math.max(2, 0.3 * (r[3] - r[1]) * h);
    const x0 = Math.max(0, Math.round(cx - dx));
    const y0 = Math.max(0, Math.round(cy - dy));
    const data = ctx.getImageData(x0, y0, Math.max(1, Math.round(2 * dx)), Math.max(1, Math.round(2 * dy))).data;
    let darkest = 255;
    let dark = 0;
    for (let i = 0; i < data.length; i += 4) {
      const l = 0.299 * data[i] + 0.587 * data[i + 1] + 0.114 * data[i + 2];
      darkest = Math.min(darkest, l);
      if (l < 128) dark += 1;
    }
    return { darkest, dark };
  }, box);
}

async function expectInk(image: Locator, box: Box, what: string) {
  const ink = await inkAt(image, box);
  expect(ink.dark, `${what}: text-coloured pixels at the rectangle's centre (darkest ${ink.darkest})`).toBeGreaterThan(0);
}

function labelOf(e: { precision: string; region?: string }): string {
  return LABEL[e.region ? `${e.precision}_${e.region}` : e.precision];
}

/**
 * Checks an opened PDF citation against its manifest expectation: the anchor's precision, the location shown, the
 * requested page image, every rectangle against the manifest boxes, and ink at the cited place. Returns what is shown.
 */
async function checkPdfCitation(
  viewer: Locator,
  entry: { places: Record<string, Place> },
  e: Expectation,
  source: Source,
  doc: OfficeBDoc,
  reading: string | null,
  scale = "normal",
): Promise<Shown> {
  const anchor = source.anchor!;
  const target = entry.places[e.target];
  const pageNo = e.page ?? target.page;
  expect(anchor.precision, `${source.id}: precision`).toBe(e.precision);
  if (e.region) expect(anchor.region).toBe(e.region);
  expect(anchor.version_id).toBe(doc.versionId);
  expect(anchor.reading_id).toBe(reading);
  await expect(viewer.getByTestId("viewer-precision")).toContainText(labelOf(e));
  await expect(viewer.getByTestId("viewer-page")).toContainText(`עמוד ${pageNo}`);
  const shown = await shownPage(viewer, pageNo);
  expectImageUrl(shown.src, doc, pageNo, reading, scale);
  if (e.precision === "page") {
    expect(shown.rects, "a page-level citation draws no rectangle").toHaveLength(0);
    return shown;
  }
  const rects = shown.rects.map(fmt).join(" ");
  expect(shown.rects.some((r) => near(r, target.box)), `a rectangle at ${fmt(target.box)} (shown: ${rects})`).toBe(true);
  const allowed = [e.target, ...(e.also ?? [])].map((k) => entry.places[k].box);
  for (const r of shown.rects) {
    expect(allowed.some((b) => near(r, b)), `rectangle ${fmt(r)} is one of the cited blocks ${allowed.map(fmt)}`).toBe(true);
  }
  const hit = shown.rects.find((r) => near(r, target.box))!;
  if (e.contains) expect(covers(hit, entry.places[e.contains].box), `${fmt(hit)} covers ${e.contains}`).toBe(true);
  if (e.avoid) {
    const avoid = entry.places[e.avoid].box;
    for (const r of shown.rects) expect(overlaps(r, avoid), `${fmt(r)} stays off ${e.avoid} ${fmt(avoid)}`).toBe(false);
  }
  // ink at the rectangle's centre; for a table row, at the cited cell inside it
  const inkBox: Box = e.contains ? entry.places[e.contains].box : hit;
  await expectInk(shown.image, inkBox, `${source.id} on page ${pageNo}`);
  if (e.header) {
    const header = entry.places[e.header];
    const headerShown = header.page === pageNo ? shown : await shownPage(viewer, header.page);
    expect(headerShown.headerRects.some((r) => near(r, header.box)), `the column headers at ${fmt(header.box)}`).toBe(true);
  }
  return shown;
}

/** Asks `entry.query` in limited mode (as the page's user), opens the picked citation and checks it. The reading the
 * citation should be pinned to is read first, through the admin's separate API session. */
async function askAndCheck(
  page: Page,
  request: APIRequestContext,
  entry: { query: string; places: Record<string, Place> },
  doc: OfficeBDoc,
  e: Expectation,
) {
  const reading = await currentReading(request, doc);
  const asked = await askLimited(page, entry.query);
  const source = pickSource(asked.answer, doc.id, e.pick);
  const viewer = await openCitation(page, asked.reply, asked.answer, source.id);
  const shown = await checkPdfCitation(viewer, entry, e, source, doc, reading);
  return { ...asked, source, viewer, shown, reading };
}

// --- the scenarios ------------------------------------------------------------------------------------------------

test.describe("citations open the cited place in the original page (office B)", () => {
  test.beforeAll(async ({ request }) => {
    // the uploads wait behind any processing already queued on the shared worker
    test.setTimeout(1_800_000);
    await prepare(request);
  });

  test.afterAll(async ({ request }) => {
    await setOfficeBCloud(request, false);
  });

  test("digital PDF: the cited sentence on page 2 is highlighted, also when zoomed", async ({ page, request }) => {
    test.setTimeout(300_000);
    const { docs } = await prepare(request);
    const doc = docs[DOCS.digital.title];
    await login(page, USERS.adminB);
    const { viewer, source, reading } = await askAndCheck(page, request, DOCS.digital, doc, DOCS.digital.expect);
    await expect(viewer.getByTestId("viewer-title")).toHaveText(DOCS.digital.title);
    await page.screenshot({ path: shot("citations-digital.png"), fullPage: true });

    // zoom: the larger rendering is requested, and the rectangle stays on the sentence
    await viewer.getByRole("button", { name: "הגדלת העמוד" }).click();
    await expect(viewer.getByTestId("viewer-pages")).toHaveAttribute("data-scale", "zoom");
    await expect(viewer.locator('[data-testid="viewer-page-frame"][data-page="2"]')).toHaveAttribute("data-zoom", "true");
    const zoomedImage = viewer.locator('[data-testid="viewer-page-frame"][data-page="2"] [data-testid="viewer-page-image"]');
    await expect(zoomedImage).toHaveAttribute("data-src", /[?&]scale=zoom(&|$)/);
    await checkPdfCitation(viewer, DOCS.digital, DOCS.digital.expect, source, doc, reading, "zoom");
    await viewer.getByRole("button", { name: "הקטנת העמוד" }).click();
    await expect(viewer.getByTestId("viewer-pages")).toHaveAttribute("data-scale", "normal");
    await viewer.getByRole("button", { name: "סגירת תצוגת המקור" }).click();
    await expect(viewer).toHaveCount(0);
  });

  test("AE2: on a page turned 90° with an offset CropBox the highlight lies on the sentence", async ({ page, request }) => {
    test.setTimeout(300_000);
    const { docs } = await prepare(request);
    await login(page, USERS.adminB);
    await askAndCheck(page, request, DOCS.rotated, docs[DOCS.rotated.title], DOCS.rotated.expect);
    await page.screenshot({ path: shot("citations-rotated.png"), fullPage: true });
  });

  test("AE1: a number in a sentence and in a table cell: each citation marks its own occurrence", async ({
    page,
    request,
  }) => {
    test.setTimeout(300_000);
    const { docs } = await prepare(request);
    const entry = DOCS.repeated;
    const doc = docs[entry.title];
    await login(page, USERS.adminB);
    // the table row: its rectangle covers the cell holding the number and stays off the sentence
    const asked = await askAndCheck(page, request, entry, doc, entry.expect.row);
    await expect(asked.viewer.getByTestId("viewer-table-row")).toContainText("מחסן הדולפין");
    // the sentence, from the same answer: its rectangle stays off the cell
    await asked.viewer.getByRole("button", { name: "סגירת תצוגת המקור" }).click();
    const sentence = pickSource(asked.answer, doc.id, entry.expect.paragraph.pick);
    const viewer = await openCitation(page, asked.reply, asked.answer, sentence.id);
    await checkPdfCitation(viewer, entry, entry.expect.paragraph, sentence, doc, asked.reading);
  });

  test("a table that continues on page 2: the row is marked on page 2, its column headers on page 1", async ({
    page,
    request,
  }) => {
    test.setTimeout(300_000);
    const { docs } = await prepare(request);
    await login(page, USERS.adminB);
    const { viewer } = await askAndCheck(page, request, DOCS.cross_page, docs[DOCS.cross_page.title],
      DOCS.cross_page.expect);
    await expect(viewer.getByTestId("viewer-table-row")).toContainText("יחידת הנחליאלי");
    await expect(viewer.getByTestId("viewer-page-image")).toHaveCount(2);
  });

  test("scanned PDF: the cited page opens at page level, without a rectangle", async ({ page, request }) => {
    test.setTimeout(300_000);
    const { docs } = await prepare(request);
    await login(page, USERS.adminB);
    const { viewer } = await askAndCheck(page, request, DOCS.scanned, docs[DOCS.scanned.title], DOCS.scanned.expect);
    await expect(viewer.getByTestId("viewer-highlight")).toHaveCount(0);
  });

  test("DOCX: the structured view shows the cited table without a page", async ({ page, request }) => {
    test.setTimeout(300_000);
    const { docs } = await prepare(request);
    const entry = DOCS.docx;
    const doc = docs[entry.title];
    await login(page, USERS.adminB);
    const asked = await askLimited(page, entry.query);
    const source = pickSource(asked.answer, doc.id, entry.expect.pick);
    expect(source.anchor!.precision).toBe("structured");
    const viewer = await openCitation(page, asked.reply, asked.answer, source.id);
    const view = viewer.getByTestId("structured-view");
    await expect(view).toBeVisible();
    await expect(view).toContainText("חנות העפרוני");
    await expect(viewer.getByTestId("viewer-precision")).toHaveText(LABEL.structured);
    await expect(viewer.getByTestId("viewer-page-image")).toHaveCount(0);
    await expect(viewer).not.toContainText(/עמוד\s*\d/);
    // a search hit names the row, not one cell: the cell is marked when a value is taken from it (the @model scenario)
    if (source.anchor!.structured?.cell) await expect(view.getByTestId("structured-cell")).toBeVisible();
  });

  test("mobile: the viewer is full screen and the highlight stays on the sentence when zoomed", async ({
    page,
    request,
  }) => {
    test.setTimeout(300_000);
    const { docs } = await prepare(request);
    const doc = docs[DOCS.digital.title];
    await page.setViewportSize({ width: 375, height: 812 });
    await login(page, USERS.adminB);
    const { viewer, source, reading } = await askAndCheck(page, request, DOCS.digital, doc, DOCS.digital.expect);
    const box = await viewer.boundingBox();
    expect(box!.width).toBeGreaterThanOrEqual(370);
    expect(box!.height).toBeGreaterThanOrEqual(800);
    await viewer.getByRole("button", { name: "הגדלת העמוד" }).click();
    await expect(viewer.getByTestId("viewer-pages")).toHaveAttribute("data-scale", "zoom");
    const zoomed = await checkPdfCitation(viewer, DOCS.digital, DOCS.digital.expect, source, doc, reading, "zoom");
    expect((await zoomed.image.boundingBox())!.width, "the zoomed page is wider than the screen").toBeGreaterThan(375);
    await page.screenshot({ path: shot("citations-mobile-zoom.png") });
    await viewer.getByRole("button", { name: "סגירת תצוגת המקור" }).click();
    await expect(viewer).toHaveCount(0);
  });

  test("AE4: revoked access — a user who lost the document's group sees the source as unavailable", async ({
    page,
    request,
  }) => {
    test.setTimeout(300_000);
    const { docs, groupId, viewerId } = await prepare(request);
    const doc = docs[DOCS.digital.title];
    await login(page, VIEWER.email);
    try {
      const asked = await askLimited(page, DOCS.digital.query);
      const source = pickSource(asked.answer, doc.id, DOCS.digital.expect.pick);
      // while the user is in the group the page opens
      const viewer = await openCitation(page, asked.reply, asked.answer, source.id);
      await shownPage(viewer, DOCS.digital.places.target.page);
      await viewer.getByRole("button", { name: "סגירת תצוגת המקור" }).click();
      // the document's group is taken from the user (the same as moving the document to a group the user lacks)
      await setOfficeBUserGroups(request, viewerId, []);
      await citationChip(asked.reply, asked.answer.markdown, source.id).click();
      await expect(viewerOf(page).getByRole("alert")).toContainText("המקור אינו זמין עוד");
      await expect(viewerOf(page).getByTestId("viewer-page-image")).toHaveCount(0);
      // reopened, the answer that rests on it is hidden
      await page.reload();
      await expect(page.locator(".msg-assistant").last()).toContainText("התשובה הוסתרה");
    } finally {
      await setOfficeBUserGroups(request, viewerId, [groupId]);
    }
  });

  test("AE4: after the document is replaced by an upload, the old conversation still opens version 1 with its highlight", async ({
    page,
    request,
  }) => {
    test.setTimeout(1_200_000);
    const { groupId } = await prepare(request);
    const entry = DOCS.replaced;
    // a fresh document every run (uploads are deduplicated by content), version 1 first
    await deleteOfficeBDocsByTitle(request, entry.title);
    const docs = await ensureOfficeBDocuments(request, [{ file: fixture(entry.file), title: entry.title }], { groupId });
    const v1 = docs[entry.title];
    await login(page, USERS.adminB);
    const first = await askAndCheck(page, request, entry, v1, entry.expect);
    const original = await first.shown.frame.getByTestId("viewer-highlight").evaluateAll((els) =>
      els.map((el) => el.getAttribute("data-rect")),
    );
    await first.viewer.getByRole("button", { name: "סגירת תצוגת המקור" }).click();

    const v2 = await uploadOfficeBVersion(request, v1.id, fixture(entry.replacement.file), entry.title);
    expect(v2).not.toBe(v1.versionId);
    await page.goto(`/chat?c=${first.conversationId}`);
    const reply = page.locator(".msg-assistant").last();
    const viewer = await openCitation(page, reply, first.answer, first.source.id);
    // version 1's page with the original rectangle, not the replacement's sentence
    const shown = await checkPdfCitation(viewer, entry, entry.expect, first.source, v1, first.reading);
    const again = await shown.frame.getByTestId("viewer-highlight").evaluateAll((els) =>
      els.map((el) => el.getAttribute("data-rect")),
    );
    expect(again).toEqual(original);
    const moved = entry.replacement.places.target.box;
    expect(shown.rects.some((r) => near(r, moved)), "the highlight is not moved to the replacement's sentence").toBe(false);
  });

  test("AE3 @model: a row of a table read from a picture is marked at table level on the picture", async ({
    page,
    request,
  }) => {
    test.setTimeout(1_200_000);
    const { groupId, modelReady } = await prepare(request);
    test.skip(!modelReady, "office B has no model key configured (or E2E_SKIP_MODEL=1)");
    const entry = DOCS.mixed;
    let doc: OfficeBDoc;
    try {
      // the picture's table is read by the vision model while the document is processed: cloud use on for the upload
      // (a copy processed earlier without it, while cloud use was off, is replaced)
      await setOfficeBCloud(request, true);
      const upload = [{ file: fixture(entry.file), title: entry.title }];
      doc = (await ensureOfficeBDocuments(request, upload, { groupId }))[entry.title];
      if (!(await hasVisionTable(request, doc))) {
        await deleteOfficeBDocsByTitle(request, entry.title);
        doc = (await ensureOfficeBDocuments(request, upload, { groupId }))[entry.title];
      }
      expect(await hasVisionTable(request, doc), "the vision model read the picture's table").toBe(true);
    } finally {
      await setOfficeBCloud(request, false);
    }
    await login(page, USERS.adminB);
    const { viewer } = await askAndCheck(page, request, entry, doc, entry.expect);
    await expect(viewer.getByTestId("viewer-degraded")).toBeVisible();
    await expect(viewer.getByTestId("viewer-table")).toBeVisible();
    await page.screenshot({ path: shot("citations-picture-table.png"), fullPage: true });
  });

  test("@model: a computation from two DOCX table cells opens its breakdown, and an input opens its cell", async ({
    page,
    request,
  }) => {
    test.setTimeout(600_000);
    const { modelReady } = await prepare(request);
    test.skip(!modelReady, "office B has no model key configured (or E2E_SKIP_MODEL=1)");
    const entry = DOCS.docx;
    const comp = entry.computation;
    await setOfficeBCloud(request, true);
    try {
      await login(page, USERS.adminB);
      await page.getByRole("button", { name: "שיחה חדשה" }).first().click();
      const reply = await sendAndWait(
        page,
        `בטבלת היחידות במסמך «${entry.title}»: מה השווי של ${comp.row} לפי השטח כפול המחיר למ״ר? חשב והצג את החישוב.`,
        400_000,
      );
      const { answer } = await lastAnswer<Answer>(page);
      // structure, not wording: a computed result cited in the text, its breakdown, and an input's place
      expect.soft(answer.markdown).toContain(comp.result);
      const order = citationOrder(answer.markdown);
      const computed = answer.computations.map((c) => c.id).find((id) => order.includes(id));
      expect(computed, `a computed result is cited (computations: ${answer.computations.map((c) => c.id)})`).toBeTruthy();
      await citationChip(reply, answer.markdown, computed!).click();
      const calc = page.getByRole("complementary", { name: "פירוט החישוב" });
      await expect(calc).toBeVisible();
      await expect(calc.getByTestId("calc-formula")).toBeVisible();
      await expect(calc.getByTestId("calc-result")).toBeVisible();
      const inputs = calc.getByTestId("calc-input");
      expect(await inputs.count(), "the computation has two inputs").toBeGreaterThanOrEqual(2);
      const fromDocument = calc.locator('[data-testid="calc-input"][data-kind="value"], [data-testid="calc-input"][data-kind="measurement"]');
      await expect(fromDocument.first()).toBeVisible();
      await page.screenshot({ path: shot("citations-calculation.png"), fullPage: true });
      await fromDocument.first().getByRole("button").first().click();
      const viewer = viewerOf(page);
      await expect(viewer).toBeVisible();
      const view = viewer.getByTestId("structured-view");
      await expect(view).toBeVisible();
      // the input's place is marked: its cell, or its quoted words
      await expect(view.getByTestId("structured-cell").or(view.getByTestId("structured-highlight")).first()).toBeVisible();
      const cell = view.getByTestId("structured-cell");
      if ((await cell.count()) > 0) expect.soft(comp.inputs).toContain((await cell.first().innerText()).trim());
      await page.screenshot({ path: shot("citations-calculation-input.png"), fullPage: true });
    } finally {
      await setOfficeBCloud(request, false);
    }
  });

  test("AE4: after a reprocess, an old conversation opens the original page and reading with the original highlight", async ({
    page,
    request,
  }) => {
    // reprocesses every office B document: kept last in this file
    test.setTimeout(1_800_000);
    const { docs } = await prepare(request);
    const doc = docs[DOCS.digital.title];
    await login(page, USERS.adminB);
    const first = await askAndCheck(page, request, DOCS.digital, doc, DOCS.digital.expect);
    const original = await first.shown.frame.getByTestId("viewer-highlight").evaluateAll((els) =>
      els.map((el) => el.getAttribute("data-rect")),
    );
    await first.viewer.getByRole("button", { name: "סגירת תצוגת המקור" }).click();

    const queued = await reprocessOfficeB(request);
    expect(queued, "the reprocess queued office B's documents").toBeGreaterThan(0);
    const now = await currentReading(request, doc);

    await page.goto(`/chat?c=${first.conversationId}`);
    const reply = page.locator(".msg-assistant").last();
    const viewer = await openCitation(page, reply, first.answer, first.source.id);
    // the same version, page and reading as when the answer was given, with the same rectangles
    const shown = await checkPdfCitation(viewer, DOCS.digital, DOCS.digital.expect, first.source, doc, first.reading);
    const again = await shown.frame.getByTestId("viewer-highlight").evaluateAll((els) =>
      els.map((el) => el.getAttribute("data-rect")),
    );
    expect(again).toEqual(original);
    // a reading that was replaced is said so; one kept as it was is current
    if (now !== first.reading) await expect(viewer.getByTestId("viewer-stale")).toBeVisible();
    else await expect(viewer.getByTestId("viewer-stale")).toHaveCount(0);
    await page.screenshot({ path: shot("citations-after-reprocess.png"), fullPage: true });
  });
});
