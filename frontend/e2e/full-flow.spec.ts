import { expect, test } from "@playwright/test";
import { deleteOfficeBDocsByTitle, fixture, login, sendAndWait, shot, USERS } from "./helpers";

// Full flow, office B admin: upload → per-file result → status and reading summary → review approve → ask in the
// chat → sources → source panel → source PDF. Office B only; office A data is never modified.
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
    await page.getByRole("link", { name: "מסמכים" }).first().click();
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
    // What was read is shown apart from structured records: passages and tables, not only "records".
    await expect(row).toContainText(/\d+ קטעים/);
    await expect(row).toContainText("רשומות עסקאות");

    // 3. Open the document's review queue from the per-file result.
    await results.getByRole("button", { name: "פרטים" }).click();
    const panel = page.getByRole("region", { name: "פרטי מסמך" });
    await expect(panel.getByRole("heading", { name: TITLE })).toBeVisible();
    await expect(panel.getByRole("status", { name: /^סטטוס: (מוכן|דורש בדיקה)$/ })).toBeVisible();
    await panel.getByRole("link", { name: "לבדיקת הנתונים של המסמך" }).click();
    await expect(page).toHaveURL(/\/review\?document_id=/);
    await page.getByRole("tab", { name: "רשומות עסקה" }).click();
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

    // 6. Ask in the chat. Office B has cloud use off here, so the reply is plainly labelled as search results with
    // their sources, never presented as an analyzed answer.
    await page.goto("/chat");
    await page.getByRole("button", { name: "שיחה חדשה" }).first().click();
    const reply = await sendAndWait(page, "מחיר למ״ר ברמת גן בשכונת חרוזים");
    await expect(reply).toContainText("תוצאות חיפוש בלבד");
    await expect(reply.locator(".cite").first()).toBeVisible();
    await page.screenshot({ path: shot("chat-limited-mode.png"), fullPage: true });

    // 7. A source opens beside the thread at its place in the document, with the original file one click away.
    await reply.locator(".cite").first().click();
    const sourcePanel = page.getByRole("complementary", { name: "תצוגת מקור" });
    await expect(sourcePanel).toContainText(TITLE);
    const href = await sourcePanel.getByRole("link", { name: "הורדת הקובץ המקורי" }).getAttribute("href");
    expect(href).toMatch(/^\/api\/documents\/[^/]+\/versions\/[^/]+\/file$/);

    // 8. Opening the source returns the PDF with the page's own session cookie.
    const fetched = await page.evaluate(async (url) => {
      const res = await fetch(url, { credentials: "same-origin" });
      const buf = new Uint8Array(await res.arrayBuffer());
      return { status: res.status, type: res.headers.get("content-type"), magic: String.fromCharCode(...buf.slice(0, 5)) };
    }, href!);
    expect(fetched.status).toBe(200);
    expect(fetched.type).toContain("application/pdf");
    expect(fetched.magic).toBe("%PDF-");
  });
});
