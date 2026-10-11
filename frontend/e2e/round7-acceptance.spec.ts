import fs from "node:fs";
import { expect, test, type APIRequestContext, type Locator, type Page } from "@playwright/test";
import {
  citationChip,
  citationOrder,
  deleteOfficeBDocsByTitle,
  ensureOfficeBDocuments,
  ensureOfficeBGroup,
  fixture,
  lastAnswer,
  login,
  officeBHasVisionTable,
  officeBSettings,
  openDetails,
  sendAndWait,
  setOfficeBCloud,
  shot,
  sourceViewer,
  USERS,
  type OfficeBDoc,
} from "./helpers";

// Round 7 acceptance in the browser (U9, R26, R29) against the running stack with the real model, office B only. The
// synthetic round-7 fixtures (backend/tests/fixtures/round7/, see its manifest.json; every name, address and amount
// is invented) are uploaded while office B's cloud use is off — so R7c's cost table, a picture, stays an unread region
// that the turn has to inspect — and processed. Office B's cloud use is then switched on for the questions and off
// again after each.
//
// What is checked is structure, not wording: the gap summary appears once and each gap line once; the components
// are listed in the details; a removal, when the model's answer had one, expands with its kind and its checked
// source (when it had none, the removals section is absent — none is faked); a cited source opens in the viewer at
// the page the manifest gives; a computation opens its breakdown, and an input opens its place in the document.
//
// Needs office B's model key (skipped without it, or with E2E_SKIP_MODEL=1).

interface Fact {
  page: number;
  value?: string;
  text?: string;
  box?: [number, number, number, number];
}

interface Doc {
  file: string;
  title: string;
  facts: Record<string, Fact>;
  question_total?: { inputs: string[]; result: string };
  gap_to_threshold?: { right: string; from_rounded_rate: string };
}

const MANIFEST = JSON.parse(fs.readFileSync(fixture("round7/manifest.json"), "utf-8")) as {
  documents: Record<"plan_status" | "two_appraisals" | "cost_table_image" | "residual", Doc>;
};
const DOCS = MANIFEST.documents;
const GROUP = "סבב 7 לבדיקה (סינתטי)";

interface Item {
  id: string;
  document_id?: string | null;
  anchor?: { pages: { page: number }[] } | null;
  inputs?: { id: string; kind: string }[];
  value?: string;
}

interface Answer {
  kind: string;
  status: string;
  markdown: string;
  sources: Item[];
  values: Item[];
  computations: Item[];
  components?: { id: string; status: string }[];
  gaps?: { reason: string; text: string; components: string[] }[];
  verification: { removed: number; removals?: { failure_kind: string }[] } | null;
}

let docs: Record<string, OfficeBDoc> | null = null;
let modelReady = false;

async function prepare(request: APIRequestContext): Promise<Record<string, OfficeBDoc>> {
  if (docs) return docs;
  const settings = await officeBSettings(request);
  modelReady = settings.key_present && process.env.E2E_SKIP_MODEL !== "1";
  await setOfficeBCloud(request, false);
  const groupId = await ensureOfficeBGroup(request, GROUP);
  const items = [DOCS.plan_status, DOCS.two_appraisals, DOCS.cost_table_image, DOCS.residual].map((d) => ({
    file: fixture(d.file),
    title: d.title,
  }));
  docs = await ensureOfficeBDocuments(request, items, { groupId, timeout: 1_500_000 });
  // R7c's picture must be unread at ingestion (the turn inspects it): a copy read by the vision model is replaced
  if (await officeBHasVisionTable(request, docs[DOCS.cost_table_image.title])) {
    await deleteOfficeBDocsByTitle(request, DOCS.cost_table_image.title);
    const again = await ensureOfficeBDocuments(request, [items[2]], { groupId, timeout: 1_500_000 });
    docs[DOCS.cost_table_image.title] = again[DOCS.cost_table_image.title];
  }
  return docs;
}

/** A new conversation with one question to the real model; the reply and its stored answer. */
async function ask(page: Page, question: string): Promise<{ reply: Locator; answer: Answer }> {
  await page.getByRole("button", { name: "שיחה חדשה" }).first().click();
  const reply = await sendAndWait(page, question, 400_000);
  const { answer } = await lastAnswer<Answer>(page);
  expect(answer.kind, `a model answer (markdown: ${answer.markdown?.slice(0, 200)})`).toBe("rag");
  return { reply, answer };
}

function count(text: string, part: string): number {
  return text.split(part).length - 1;
}

/** A gap line as the reply renders it: without the markdown bold, cut before its first citation (a chip). */
function shownGap(markdown: string): string {
  return markdown.replace(/\*\*/g, "").split(/\s*\[(?:[SMCVA]\d+)/)[0].trim();
}

/** The gap summary: the completeness line once (for a partial answer), each gap line once, and the last gap line is
 * the last line of the answer's body. */
async function checkGapSummary(reply: Locator, answer: Answer): Promise<void> {
  const gaps = answer.gaps ?? [];
  if (answer.status === "partial") {
    await expect(reply.getByTestId("completeness")).toHaveCount(1);
    await expect(reply.getByTestId("completeness")).toHaveAttribute("data-status", "partial");
  }
  const text = await reply.innerText();
  for (const g of gaps) expect(count(text, shownGap(g.text)), `gap line once: ${g.text}`).toBe(1);
  if (gaps.length) {
    const body = (await reply.locator(".body").innerText()).trim().split("\n").filter((l) => l.trim());
    expect(body[body.length - 1].trim().startsWith(shownGap(gaps[gaps.length - 1].text))).toBe(true);
  }
}

/** The components in the details, and the removals: expanded when the answer had one, absent when it had none. */
async function checkDetails(page: Page, reply: Locator, answer: Answer, name: string): Promise<void> {
  await openDetails(reply);
  const comps = answer.components ?? [];
  if (comps.length) await expect(reply.getByTestId("components")).toBeVisible();
  const removals = answer.verification?.removals ?? [];
  if (!removals.length) {
    test.info().annotations.push({ type: "removals", description: `${name}: the model's answer had no removal` });
    await expect(reply.getByTestId("removals")).toHaveCount(0);
    await expect(reply.getByTestId("removal-notice")).toHaveCount(0);
    return;
  }
  test.info().annotations.push({ type: "removals", description: `${name}: ${removals.map((r) => r.failure_kind)}` });
  await expect(reply.getByTestId("removal-notice")).toHaveCount(1);
  const item = reply.getByTestId("removal").first();
  await expect(item).toHaveAttribute("data-kind", removals[0].failure_kind);
  await expect(item.getByTestId("removal-kind")).toBeVisible();
  await item.getByRole("button", { name: /פירוט/ }).click();
  await expect(item.getByTestId("removal-loading")).toHaveCount(0, { timeout: 30_000 });
  await expect(item.getByTestId("removal-detail").or(item.getByTestId("removal-unavailable"))).toBeVisible();
  const source = item.getByTestId("removal-source");
  if ((await source.count()) > 0) {
    await source.first().click();
    await expect(sourceViewer(page)).toBeVisible();
    await sourceViewer(page).getByRole("button", { name: "סגירת תצוגת המקור" }).click();
  }
}

/** Opens cited item `id` (a source or a value) and checks the viewer shows file page `pageNo`. */
async function openCited(page: Page, reply: Locator, answer: Answer, id: string, pageNo: number): Promise<Locator> {
  await citationChip(reply, answer.markdown, id).click();
  const viewer = sourceViewer(page);
  await expect(viewer).toBeVisible();
  await expect(viewer.getByTestId("viewer-page")).toContainText(`עמוד ${pageNo}`);
  return viewer;
}

/** Opens computation `id`'s breakdown and the first input taken from the document; the viewer shows `pageNo`. */
async function openComputationInput(page: Page, reply: Locator, answer: Answer, id: string, pageNo: number) {
  await citationChip(reply, answer.markdown, id).click();
  const calc = page.getByRole("complementary", { name: "פירוט החישוב" });
  await expect(calc).toBeVisible();
  await expect(calc.getByTestId("calc-formula")).toBeVisible();
  await expect(calc.getByTestId("calc-result")).toBeVisible();
  expect(await calc.getByTestId("calc-input").count(), "the computation has two inputs").toBeGreaterThanOrEqual(2);
  const fromDocument = calc.locator(
    '[data-testid="calc-input"][data-kind="value"], [data-testid="calc-input"][data-kind="measurement"]',
  );
  await expect(fromDocument.first()).toBeVisible();
  await fromDocument.first().getByRole("button").first().click();
  const viewer = sourceViewer(page);
  await expect(viewer).toBeVisible();
  await expect(viewer.getByTestId("viewer-page")).toContainText(`עמוד ${pageNo}`);
  return viewer;
}

const cited = (answer: Answer, prefix: string) => citationOrder(answer.markdown).filter((i) => i.startsWith(prefix));

function itemOf(answer: Answer, id: string): Item | undefined {
  return [...answer.sources, ...answer.values, ...answer.computations].find((x) => x.id === id);
}

test.describe("round 7 acceptance with the real model (office B)", () => {
  test.beforeAll(async ({ request }) => {
    test.setTimeout(1_800_000);
    await prepare(request);
  });

  test.beforeEach(async ({ request }) => {
    await prepare(request);
    test.skip(!modelReady, "office B has no model key configured (or E2E_SKIP_MODEL=1)");
    await setOfficeBCloud(request, true);
  });

  test.afterEach(async ({ request }) => {
    await setOfficeBCloud(request, false);
  });

  test("a partly found category: the gap summary once, the components, removals, and a source at its page", async ({
    page,
  }) => {
    test.setTimeout(600_000);
    const doc = DOCS.plan_status;
    await login(page, USERS.adminB);
    const { reply, answer } = await ask(
      page,
      `במסמך «${doc.title}»: פרט את נתוני התכנית — שימושים, יח״ד, שטחים, גובה, קומות וקווי בניין — ככל שהם מופיעים בפרק המצב התכנוני.`,
    );
    await expect(reply.locator(".body")).toContainText("84");
    expect(answer.gaps?.length ?? 0, "the missing items are stated").toBeGreaterThan(0);
    await checkGapSummary(reply, answer);
    await checkDetails(page, reply, answer, "partly found");
    await page.screenshot({ path: shot("round7-gaps.png"), fullPage: true });
    const [first] = [...cited(answer, "S"), ...cited(answer, "V")];
    expect(first, "the answer cites the chapter").toBeTruthy();
    await openCited(page, reply, answer, first, doc.facts.approved_units.page);
  });

  test("two appraisals in one file: the asked property's figure opens on its own page", async ({ page }) => {
    test.setTimeout(600_000);
    const doc = DOCS.two_appraisals;
    const right = doc.facts.second_per_sqm;
    await login(page, USERS.adminB);
    const { reply, answer } = await ask(page, `במסמך «${doc.title}»: מה השווי למ״ר שנקבע לנכס ברחוב הצפצפה 7?`);
    await expect(reply.locator(".body")).toContainText(right.value!);
    await expect(reply.locator(".body")).not.toContainText(doc.facts.first_per_sqm.value!);
    await checkGapSummary(reply, answer);
    await checkDetails(page, reply, answer, "two appraisals");
    const line = answer.markdown.split("\n").find((l) => l.includes(right.value!))!;
    const id = citationOrder(line).find((i) => /^[SV]/.test(i));
    expect(id, `the figure is cited: ${line}`).toBeTruthy();
    expect(itemOf(answer, id!)?.anchor?.pages.map((p) => p.page)).toEqual([right.page]);
    await openCited(page, reply, answer, id!, right.page);
    await page.screenshot({ path: shot("round7-two-appraisals.png"), fullPage: true });
  });

  test("the explicit profit amount: the gap computation opens its breakdown and an input opens its page", async ({
    page,
  }) => {
    test.setTimeout(600_000);
    const doc = DOCS.residual;
    const gap = doc.gap_to_threshold!;
    await login(page, USERS.adminB);
    const { reply, answer } = await ask(
      page,
      `במסמך «${doc.title}»: מה הפער בין הרווח היזמי לבין הרווח המינימלי הנדרש לכדאיות הפרויקט?`,
    );
    await expect(reply.locator(".body")).toContainText(gap.right);
    await expect(reply.locator(".body")).not.toContainText(gap.from_rounded_rate);
    await checkGapSummary(reply, answer);
    await checkDetails(page, reply, answer, "explicit amount");
    const computed = cited(answer, "C");
    expect(computed, `a computed result is cited (${answer.computations.map((c) => c.id)})`).not.toHaveLength(0);
    await openComputationInput(page, reply, answer, computed[computed.length - 1], doc.facts.profit_amount.page);
    await page.screenshot({ path: shot("round7-explicit-amount.png"), fullPage: true });
  });

  test("a table read during the turn: the computation's input opens the table on its page", async ({ page }) => {
    test.setTimeout(600_000);
    const doc = DOCS.cost_table_image;
    const total = doc.question_total!;
    await login(page, USERS.adminB);
    const { reply, answer } = await ask(
      page,
      `במסמך «${doc.title}»: מה עלות הבנייה העילית והחניון התת-קרקעי יחד, לפי טבלת עלויות הבנייה?`,
    );
    await expect(reply.locator(".body")).toContainText(total.result);
    await checkGapSummary(reply, answer);
    await checkDetails(page, reply, answer, "inspected table");
    const computed = cited(answer, "C").find((id) => itemOf(answer, id)?.inputs?.length);
    expect(computed, `a computed result is cited (${answer.computations.map((c) => c.id)})`).toBeTruthy();
    const viewer = await openComputationInput(page, reply, answer, computed!, doc.facts.picture.page);
    await expect(viewer.getByTestId("viewer-highlight").first()).toBeVisible();
    await page.screenshot({ path: shot("round7-inspected-table.png"), fullPage: true });
  });
});
