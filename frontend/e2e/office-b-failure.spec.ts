import { expect, test, type Page } from "@playwright/test";
import { deleteOfficeBDocsByTitle, fixture, login, USERS } from "./helpers";

// Office B only (the office A data set is shared with the API eval/load runs and must not change).
const FILE = "BAD_synthetic_encrypted.pdf";
const TITLE = "BAD synthetic encrypted";

test.describe.configure({ mode: "serial" });

function docRow(page: Page) {
  // Admins also see logically deleted documents (badge "נמחק") from earlier runs; skip those rows.
  return page
    .getByRole("table", { name: "רשימת מסמכים" })
    .getByRole("row")
    .filter({ hasText: TITLE })
    .filter({ hasNotText: "נמחק" });
}

test.describe("failed processing status (office B)", () => {
  test.beforeAll(async ({ request }) => {
    await deleteOfficeBDocsByTitle(request, TITLE);
  });

  test("password-protected PDF is accepted, then shows 'נכשל' with the Hebrew reason", async ({ page }) => {
    test.setTimeout(180_000);
    await login(page, USERS.adminB);
    await page.getByRole("navigation", { name: "ניווט ראשי" }).getByRole("link", { name: "מסמכים" }).click();

    await page.getByLabel("בחירת קבצים להעלאה").setInputFiles(fixture(FILE));
    await expect(page.getByText("נבחרו")).toContainText("1");
    await page.getByRole("button", { name: "העלאה", exact: true }).click();

    const results = page.getByRole("region", { name: "תוצאות ההעלאה" });
    await expect(results.getByRole("status", { name: `${FILE}: התקבל לעיבוד` })).toBeVisible();

    // The list polls every 3 seconds while the version is pending/processing.
    const row = docRow(page);
    await expect(row.getByRole("status", { name: "סטטוס: נכשל" })).toBeVisible({ timeout: 120_000 });
    await expect(row.getByRole("status")).toHaveText("נכשל");
    await expect(row).toContainText("מוגן בסיסמה");

    // The document panel shows the same status and reason for the version.
    await row.getByRole("button", { name: TITLE }).click();
    const panel = page.getByRole("region", { name: "פרטי מסמך" });
    await expect(panel.getByRole("status", { name: "סטטוס: נכשל" })).toBeVisible();
    await expect(panel).toContainText("הקובץ מוגן בסיסמה ולא ניתן לעבד אותו");
  });

  test("status filter 'נכשל' lists only failed documents", async ({ page }) => {
    await login(page, USERS.adminB);
    await page.goto("/documents");
    const table = page.getByRole("table", { name: "רשימת מסמכים" });
    await expect(docRow(page).first()).toBeVisible();
    await page.getByLabel("סינון לפי סטטוס").selectOption({ label: "נכשל" });
    await page.getByRole("button", { name: "חיפוש", exact: true }).click();
    // Office B also holds ready documents (DB1), which must disappear from the filtered list.
    await expect(table.getByRole("status", { name: "סטטוס: מוכן" })).toHaveCount(0, { timeout: 10_000 });
    await expect(table.getByRole("status", { name: "סטטוס: נכשל" }).first()).toBeVisible();
  });
});
