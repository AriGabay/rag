import { expect, test } from "@playwright/test";
import {
  answerCards,
  ask,
  conditionsOf,
  deleteOfficeBDocsByTitle,
  fixture,
  login,
  resolveClarifications,
  shot,
  USERS,
} from "./helpers";

// Gate 8 full flow, office B admin: upload → per-file result → status → review approve → ask → clarification →
// numeric answer → sources → source PDF. Office B only; office A data is never modified.
const FILE = "D5_synthetic_harozim_mixed_formats.pdf";
const TITLE = "D5 synthetic harozim mixed formats";

test.describe("full flow (office B)", () => {
  test.beforeAll(async ({ request }) => {
    // Upload dedup is by content hash, so a leftover copy from an earlier run is removed first.
    await deleteOfficeBDocsByTitle(request, TITLE);
  });

  test("upload, review, approve, ask, clarify, answer, open source", async ({ page }) => {
    test.setTimeout(300_000);
    await login(page, USERS.adminB);
    await page.getByRole("navigation", { name: "ניווט ראשי" }).getByRole("link", { name: "מסמכים" }).click();
    await expect(page).toHaveURL(/\/documents$/);

    // 1. Upload through the file input; per-file result.
    await page.getByLabel("בחירת קבצים להעלאה").setInputFiles(fixture(FILE));
    await page.getByRole("button", { name: "העלאה", exact: true }).click();
    const results = page.getByRole("region", { name: "תוצאות ההעלאה" });
    await expect(results.getByRole("status", { name: `${FILE}: התקבל לעיבוד` })).toBeVisible();

    // 2. Status table polls until the version is processed (ready or needs review).
    // Admins also see logically deleted copies from earlier runs (badge "נמחק"); only the live row counts.
    const row = page
      .getByRole("table", { name: "רשימת מסמכים" })
      .getByRole("row")
      .filter({ hasText: TITLE })
      .filter({ hasNotText: "נמחק" });
    await expect(row).toHaveCount(1);
    await expect(row.getByRole("status", { name: /^סטטוס: (מוכן|דורש בדיקה)$/ })).toBeVisible({ timeout: 180_000 });

    // 3. Open the document's review queue from the per-file result.
    await results.getByRole("button", { name: "פרטים" }).click();
    const panel = page.getByRole("region", { name: "פרטי מסמך" });
    await expect(panel.getByRole("heading", { name: TITLE })).toBeVisible();
    await expect(panel.getByRole("status", { name: /^סטטוס: (מוכן|דורש בדיקה)$/ })).toBeVisible();
    await panel.getByRole("link", { name: "לבדיקת הנתונים של המסמך" }).click();
    await expect(page).toHaveURL(/\/review\?document_id=/);
    await expect(page.getByText("מסונן למסמך אחד")).toBeVisible();

    const queue = page.getByRole("complementary", { name: "תור הבדיקה" });
    const firstRecord = queue.getByRole("button").filter({ hasText: TITLE }).first();
    await expect(firstRecord).toBeVisible();
    await firstRecord.click();

    // 4. The record is shown beside the PDF at the cited page.
    const detail = page.getByRole("region", { name: "פרטי הפריט" });
    await expect(detail.getByRole("heading", { level: 2, name: TITLE })).toBeVisible();
    await expect(detail.getByRole("table", { name: "שדות הרשומה" })).toBeVisible();
    const frame = detail.locator("iframe.source-frame");
    await expect(frame).toHaveAttribute("src", /\/api\/documents\/[^/]+\/versions\/[^/]+\/file#page=\d+$/);
    await expect(frame).toHaveAttribute("title", /^קובץ המקור, עמוד \d+$/);
    const fieldsBox = await detail.getByRole("table", { name: "שדות הרשומה" }).boundingBox();
    const frameBox = await frame.boundingBox();
    expect(fieldsBox && frameBox, "record and source are both laid out").toBeTruthy();
    // Side by side; in RTL the record (first column) is on the right of the PDF.
    expect(fieldsBox!.x).toBeGreaterThan(frameBox!.x);
    await page.screenshot({ path: shot("review-record-beside-pdf.png"), fullPage: true });

    // 5. Approve with a note.
    await detail.getByLabel("הערה (לא חובה)").fill("נבדק מול המקור בבדיקת דפדפן");
    await detail.getByRole("button", { name: "אישור הרשומה" }).click();
    await expect(detail.getByText("הרשומה אושרה.")).toBeVisible();
    await expect(detail.getByText("הערת בדיקה: נבדק מול המקור בבדיקת דפדפן")).toBeVisible();

    // 6. Ask in chat; the clarification options are the primary path.
    await page.getByRole("navigation", { name: "ניווט ראשי" }).getByRole("link", { name: "שאלות" }).click();
    await page.getByRole("button", { name: "שיחה חדשה", exact: true }).click();
    await ask(page, "מה מחיר למ״ר ברמת גן בשכונת חרוזים בשנת 2024?");
    const clar = page.getByRole("group", { name: "אפשרויות הבהרה" });
    await expect(clar.getByRole("button", { name: "מחירי עסקאות" })).toBeVisible();
    await expect(answerCards(page).last()).toContainText("לאיזה נתון הכוונה?");
    const asked = await resolveClarifications(page, [/^מחירי עסקאות$/, /^תאריך העסקה$/]);
    expect(asked.length).toBeGreaterThanOrEqual(2);

    // 7. Numeric answer card with labeled mean and weighted figures.
    const card = answerCards(page).last();
    await expect(card.getByText("ממוצע מחירי המ״ר", { exact: true })).toBeVisible();
    await expect(card.getByText("מחיר משוקלל = סך מחירים חלקי סך שטחים", { exact: true })).toBeVisible();
    const figures = card.locator(".figure-value bdi");
    await expect(figures).toHaveCount(2);
    for (const v of await figures.allInnerTexts()) expect(v).toMatch(/^\d{1,3}(,\d{3})*(\.\d+)? ₪ למ״ר$/);
    await expect(card.getByText(/מבוסס על \d+ רשומות ייחודיות/)).toBeVisible();
    const conds = await conditionsOf(card);
    expect(conds["עיר"]).toBe("רמת גן");
    expect(conds["שכונה"]).toBe("חרוזים");
    expect(conds["סוג הנתון"]).toBe("מחירי עסקאות");

    // 8. Sources list; each source links to the authenticated file endpoint at a page.
    const sources = card.getByRole("region", { name: "מקורות" });
    await expect(sources.getByRole("heading", { name: "מקורות" })).toBeVisible();
    const links = sources.getByRole("link");
    expect(await links.count()).toBeGreaterThan(0);
    const href = await links.first().getAttribute("href");
    expect(href).toMatch(/^\/api\/documents\/[^/]+\/versions\/[^/]+\/file#page=\d+$/);
    await expect(links.first()).toHaveAttribute("target", "_blank");
    await card.scrollIntoViewIfNeeded();
    await page.screenshot({ path: shot("chat-numeric-answer.png"), fullPage: true });

    // 9. Opening the source returns the PDF with the page's own session cookie.
    const fetched = await page.evaluate(async (url) => {
      const res = await fetch(url, { credentials: "same-origin" });
      const buf = new Uint8Array(await res.arrayBuffer());
      return { status: res.status, type: res.headers.get("content-type"), magic: String.fromCharCode(...buf.slice(0, 5)) };
    }, href!);
    expect(fetched.status).toBe(200);
    expect(fetched.type).toContain("application/pdf");
    expect(fetched.magic).toBe("%PDF-");

    // Clicking the link opens the source in a new tab (noopener) that loads the PDF with the session cookie.
    const path = href!.split("#")[0];
    const [popup, request] = await Promise.all([
      page.waitForEvent("popup"),
      page.context().waitForEvent("request", (r) => new URL(r.url()).pathname === path),
      links.first().click(),
    ]);
    const response = await request.response();
    expect(response?.status()).toBe(200);
    expect(response?.headers()["content-type"]).toContain("application/pdf");
    await popup.close();
  });
});
