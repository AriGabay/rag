import { expect, test } from "@playwright/test";
import { anchor, answerWith, DOC, openAnswer, openDetails, source, TITLE, type Json } from "./answer-fixtures";
import { citationChip, sourceViewer } from "./helpers";

// A source from a file that holds several appraisals shows its unit (title and pages) beside the file name; a source
// from a file with one unit shows none (round 9 U9, R10, KTD7). The conversation is real (office B); its messages and
// the cited document are fixed synthetic payloads, as in the gap and removal specs: the test checks how the unit is
// shown, not a model's choice. The unit is the second appraisal of the synthetic compendium R9a
// (backend/tests/fixtures/round9/manifest.json); every name here is invented.

const UNIT = {
  id: "00000000-0000-0000-0000-0000000000a7#reading-synthetic-7#2",
  no: 2,
  title: "מתחם גבעת הרקפות",
  label: "מתחם גבעת הרקפות · רחוב הרקפת 8",
  kind: "appraisal",
  pages: [3, 4],
};
const UNIT_TEXT = "מתחם גבעת הרקפות · עמודים 3–4";
const MARKDOWN = "שווי הנכס הוא 11,776,000 ₪ [S1].\n\nשווי הנכס בשומה האחרת הוא 9,500 ₪ [S2].";

function withUnit(): Json {
  const a = anchor();
  return source("S1", { unit: UNIT, anchor: { ...a, unit: UNIT } });
}

test.describe("unit beside the file name", () => {
  test("a source of a file with several units shows the unit and its pages; a source of a one-unit file shows none", async ({
    page,
  }) => {
    const { id, reply } = await openAnswer(
      page,
      answerWith({
        markdown: MARKDOWN,
        status: "answered",
        gaps: [],
        components: [],
        sources: [withUnit(), source("S2", { text: "שווי הנכס הוא 9,500 ₪." })],
        documents: [{ document_id: DOC, title: TITLE }],
      }),
    );
    // the sources list of the details names the unit beside the file; the one-unit file's entry has none
    await openDetails(reply);
    const list = reply.locator("details.msg-details ol li");
    await expect(list).toHaveCount(2);
    await expect(list.nth(0)).toContainText(UNIT_TEXT);
    await expect(list.nth(1)).not.toContainText("עמודים 3–4");
    await expect(list.nth(1)).not.toContainText("ללא כותרת");

    // the viewer of the first citation shows the unit under the file title
    await citationChip(reply, MARKDOWN, "S1").click();
    const viewer = sourceViewer(page);
    await expect(viewer).toBeVisible();
    await expect(viewer.getByTestId("viewer-title")).toContainText(TITLE);
    await expect(viewer.getByTestId("viewer-unit")).toContainText(UNIT_TEXT);
    await page.keyboard.press("Escape");

    // and the second citation, of a file with one unit, shows no unit
    await citationChip(reply, MARKDOWN, "S2").click();
    await expect(sourceViewer(page)).toBeVisible();
    await expect(sourceViewer(page).getByTestId("viewer-title")).toContainText(TITLE);
    await expect(sourceViewer(page).getByTestId("viewer-unit")).toHaveCount(0);
    await page.keyboard.press("Escape");
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a unit that states no title is shown as untitled, with its pages", async ({ page }) => {
    const untitled = { ...UNIT, title: null, label: "ללא שם מזהה" };
    const a = anchor();
    const { id, reply } = await openAnswer(
      page,
      answerWith({
        markdown: "שווי הנכס הוא 11,776,000 ₪ [S1].",
        status: "answered",
        gaps: [],
        components: [],
        sources: [source("S1", { unit: untitled, anchor: { ...a, unit: untitled } })],
      }),
    );
    await openDetails(reply);
    await expect(reply.locator("details.msg-details ol li").first()).toContainText("ללא כותרת · עמודים 3–4");
    await page.request.delete(`/api/chat/conversations/${id}`);
  });
});
