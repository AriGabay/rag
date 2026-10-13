import fs from "node:fs";
import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  assistantMessages,
  citationChip,
  composer,
  deleteOfficeBDocsByTitle,
  ensureOfficeBDocuments,
  ensureOfficeBGroup,
  fixture,
  lastAnswer,
  login,
  officeBSettings,
  sendAndWait,
  setOfficeBCloud,
  sourceViewer,
  USERS,
} from "./helpers";

// Round 10 in the browser (U11, R39, AE1, AE2) against the running stack with the real model, office B only. The synthetic
// round-10 fixtures (backend/tests/fixtures/round10/, see its manifest.json; every text says it is a test and invents
// every name, address and amount) are the only documents: the synthetic standard 98.1 is uploaded here through the screen
// as a "תקן שמאות", and a synthetic appraisal titled with "שומה" is what the property question is about. Both are removed
// by title afterwards, and office B's cloud use is switched off again.
//
// The conversation is the reported one in miniature: a question about a property, then a general professional question
// whose words the appraisal's title shares ("השומה"), then — after the page is reloaded — "הסבר את השנייה". What is
// checked is structure, not wording: the professional question gets an answer and no clarification, the ordinal after the
// reload is bound to the second item of the list the answer printed (the slots live on the server, not in the page), and
// a citation of the standard opens in the viewer with the standard's number, edition and section.
//
// Needs office B's model key (skipped without it, or with E2E_SKIP_MODEL=1). Not for office A.

interface Doc {
  file: string;
  title: string;
  source_kind: string;
}

const MANIFEST = JSON.parse(fs.readFileSync(fixture("round10/manifest.json"), "utf-8")) as {
  documents: Record<"standard_ed1" | "appraisal_shape_1", Doc>;
};
const STANDARD = MANIFEST.documents.standard_ed1;
const APPRAISAL = MANIFEST.documents.appraisal_shape_1;
const GROUP = "סבב 10 לבדיקה (סינתטי)";
const PROPERTY_QUESTION = "מה דמי השכירות הראויים לנכס ברחוב התמר 9?";
const PROFESSIONAL_QUESTION = "מה הן שלושת גישות השומה הבסיסיות?";
const ORDINAL_QUESTION = "הסבר את השנייה";

interface StandardIdentity {
  number: string | null;
  edition: string | null;
  section: string | null;
}

interface Answer {
  kind: string;
  status: string;
  markdown: string;
  request?: { candidates?: unknown[]; document_ids?: string[]; intent?: { value: string; item: number | null; item_label: string } } | null;
  sources: { id: string; source_kind?: string; standard?: StandardIdentity | null; document_id?: string | null }[];
}

let groupId = "";
let modelReady = false;

test.describe.configure({ mode: "serial" });

test.describe("professional sources in the chat (office B)", () => {
  test.beforeAll(async ({ request }) => {
    const settings = await officeBSettings(request);
    modelReady = settings.key_present && process.env.E2E_SKIP_MODEL !== "1";
    // The documents are read with cloud use off: the standard's metadata then comes from its patterns alone.
    await setOfficeBCloud(request, false);
    groupId = await ensureOfficeBGroup(request, GROUP);
    // Upload dedup is by content hash, so leftovers of an earlier run are removed first.
    await deleteOfficeBDocsByTitle(request, STANDARD.title);
    await deleteOfficeBDocsByTitle(request, APPRAISAL.title);
    await ensureOfficeBDocuments(request, [{ file: fixture(APPRAISAL.file), title: APPRAISAL.title }], { groupId });
  });

  test.afterAll(async ({ request }) => {
    await setOfficeBCloud(request, false);
    await deleteOfficeBDocsByTitle(request, STANDARD.title);
    await deleteOfficeBDocsByTitle(request, APPRAISAL.title);
  });

  test("the synthetic standard is uploaded as a standard through the screen and shows what it states", async ({
    page,
    request,
  }) => {
    test.setTimeout(600_000);
    await login(page, USERS.adminB);
    await page.goto("/documents");

    const panel = page.getByRole("region", { name: "העלאת מסמכים" });
    await page.getByLabel("קבוצת הרשאה").selectOption(groupId);
    await panel.getByRole("radiogroup").getByRole("radio", { name: "תקן שמאות" }).check();
    await page.getByLabel("בחירת קבצים להעלאה").setInputFiles(fixture(STANDARD.file));
    await expect(panel.getByTestId("upload-kind-summary")).toContainText("תקן שמאות");
    await panel.getByRole("button", { name: "העלאה", exact: true }).click();
    const results = page.getByRole("region", { name: "תוצאות ההעלאה" });
    await expect(results.getByRole("status", { name: /\.docx: התקבל לעיבוד/ })).toBeVisible();

    // a live copy exists now, so this only waits until the worker has read it
    await ensureOfficeBDocuments(request, [{ file: fixture(STANDARD.file), title: STANDARD.title }], { groupId });
    await page.reload();
    // the row whose own title is the standard's: another live edition lists this title among its editions
    const row = page
      .getByRole("table", { name: "רשימת מסמכים" })
      .getByRole("row")
      .filter({ has: page.getByRole("button", { name: STANDARD.title, exact: true }) })
      .filter({ hasNotText: "נמחק" });
    await expect(row).toHaveCount(1);
    await expect(row.getByTestId("kind-badge")).toHaveText("תקן שמאות");
    await expect(row.getByTestId("professional-number")).toContainText("98.1");
    await expect(row.getByTestId("professional-edition")).toContainText("גרסה 1.0");
    await expect(row.getByTestId("professional-effective")).toContainText("05.10.2025");
    // what the document does not state is unknown, not filled in
    await expect(row.getByTestId("professional-published")).not.toContainText("לא ידוע");
  });

  test("a property question, the professional question after it, and the ordinal after a reload keep their slots", async ({
    page,
    request,
  }) => {
    test.skip(!modelReady, "office B has no model key configured (or E2E_SKIP_MODEL=1)");
    test.setTimeout(900_000);
    await setOfficeBCloud(request, true);
    await login(page, USERS.adminB);
    await page.getByRole("button", { name: "שיחה חדשה" }).first().click();

    // a property question on the synthetic appraisal
    await sendAndWait(page, PROPERTY_QUESTION, 400_000);
    const property = (await lastAnswer<Answer>(page)).answer;
    expect(property.status, "the property question is answered, not asked about").not.toBe("clarification");

    // the professional question: its words are in the appraisal's title, and it must not turn into a choice of properties
    await sendAndWait(page, PROFESSIONAL_QUESTION, 400_000);
    const { conversationId, answer: professional } = await lastAnswer<Answer>(page);
    expect(professional.kind, `a model answer (markdown: ${professional.markdown?.slice(0, 200)})`).toBe("rag");
    expect(professional.status, "a professional question gets an answer, not a clarification").not.toBe("clarification");
    expect(professional.request?.candidates ?? [], "no property is offered as a choice").toHaveLength(0);
    expect(professional.request?.intent?.value).toBe("professional");
    expect(professional.markdown.trim().length).toBeGreaterThan(0);
    const standardSource = professional.sources.find((s) => s.source_kind === "standard");
    expect(
      standardSource,
      `the standard is cited: ${professional.sources.map((s) => `${s.id} ${s.source_kind}`).join(", ")}`,
    ).toBeTruthy();

    // reload: the conversation comes back from the server with its slots
    await page.reload();
    await expect(composer(page)).toBeVisible();
    await expect(assistantMessages(page)).toHaveCount(2);

    const listed = (md: string, mark: RegExp) => md.split("\n").filter((l) => mark.test(l));
    const numbered = listed(professional.markdown, /^\s*\d{1,2}[.)]\s+\S/);
    const bullets = listed(professional.markdown, /^\s*[-*•]\s+\S/);
    const hasList = numbered.length >= 2 || (numbered.length === 0 && bullets.length >= 2);

    await sendAndWait(page, ORDINAL_QUESTION, 400_000);
    const { answer: second } = await lastAnswer<Answer>(page);
    expect(second.status, "the ordinal after the reload is not asked about").not.toBe("clarification");
    expect(second.request?.candidates ?? []).toHaveLength(0);
    expect(second.request?.intent?.value).toBe("professional");
    if (hasList) {
      expect(second.request?.intent?.item, "the second item of the list the earlier answer printed").toBe(2);
      expect((second.request?.intent?.item_label ?? "").length).toBeGreaterThan(0);
    } else {
      test.info().annotations.push({ type: "list", description: "the professional answer printed no list: no ordinal to bind" });
    }

    // a citation of the standard opens in the viewer with the standard's own label
    const first = assistantMessages(page).nth(1);
    await openStandardCitation(page, first, professional);

    await page.request.delete(`/api/chat/conversations/${conversationId}`);
    await setOfficeBCloud(request, false);
  });
});

/** Opens the first standard source of `answer` from its chip in `reply` and checks the viewer's standard label. */
async function openStandardCitation(page: Page, reply: Locator, answer: Answer): Promise<void> {
  const cited = new Set(answer.markdown.match(/\[(S\d+)\]/g)?.map((m) => m.slice(1, -1)) ?? []);
  const source = answer.sources.find((s) => s.source_kind === "standard" && cited.has(s.id));
  expect(source, "a standard source cited in the text").toBeTruthy();
  const std = source!.standard;
  expect(std?.number).toBe("98.1");

  await citationChip(reply, answer.markdown, source!.id).click();
  const viewer = sourceViewer(page);
  await expect(viewer).toBeVisible();
  const label = viewer.getByTestId("viewer-standard");
  await expect(label).toContainText("תקן 98.1");
  if (std?.edition) await expect(label).toContainText(std.edition);
  if (std?.section) await expect(label).toContainText(`סעיף ${std.section}`);
  await page.keyboard.press("Escape");
}
