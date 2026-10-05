import { expect, test, type Page } from "@playwright/test";
import { answerCards, deleteOfficeBDocsByTitle, fixture, PASSWORD, USERS } from "./helpers";

// Keyboard-only paths (R37): upload through the file input (office B) and asking with Enter (office A, read-only).
const FILE = "BAD_synthetic_encrypted.pdf";
const TITLE = "BAD synthetic encrypted";

/** Presses Tab until the focused element satisfies `predicate` (evaluated in the page). */
async function tabUntil(page: Page, predicate: (el: Element) => boolean, max = 300): Promise<void> {
  for (let i = 0; i < max; i++) {
    await page.keyboard.press("Tab");
    const focused = await page.evaluateHandle(() => document.activeElement ?? document.body);
    const hit = await focused.evaluate(predicate);
    await focused.dispose();
    if (hit) return;
  }
  throw new Error(`focus never reached the target after ${max} Tab presses`);
}

async function keyboardLogin(page: Page, email: string): Promise<void> {
  await page.goto("/login");
  await tabUntil(page, (el) => el.id === "email");
  await page.keyboard.type(email);
  await page.keyboard.press("Tab");
  await page.keyboard.type(PASSWORD);
  await page.keyboard.press("Enter");
  await expect(page.getByRole("navigation", { name: "ניווט ראשי" })).toBeVisible();
}

test.describe("keyboard only", () => {
  test("choose a file with the file input and upload with Enter", async ({ page }) => {
    await deleteOfficeBDocsByTitle(page.request, TITLE);
    await keyboardLogin(page, USERS.adminB);
    await page.goto("/documents");
    await expect(page.getByRole("heading", { name: "העלאת מסמכים" })).toBeVisible();

    // Tab until the file input has focus (no mouse, no programmatic focus).
    await tabUntil(page, (el) => el.matches('input[type="file"]'));

    const chooserPromise = page.waitForEvent("filechooser");
    await page.keyboard.press("Space");
    const chooser = await chooserPromise;
    await chooser.setFiles(fixture(FILE));
    await expect(page.getByText("נבחרו")).toContainText("1");

    // Tab forward to the upload button and press Enter.
    await tabUntil(page, (el) => el.tagName === "BUTTON" && el.textContent?.trim() === "העלאה", 10);
    await page.keyboard.press("Enter");

    // A failed version does not count as "already uploaded" (docs_hash_lookup skips failed versions), so the same
    // bytes are accepted again as a retry.
    const results = page.getByRole("region", { name: "תוצאות ההעלאה" });
    await expect(results.getByRole("status", { name: `${FILE}: התקבל לעיבוד` })).toBeVisible();
    await tabUntil(page, (el) => el.tagName === "BUTTON" && el.textContent?.trim() === "פרטים", 10);
    await page.keyboard.press("Enter");
    const panel = page.getByRole("region", { name: "פרטי מסמך" });
    await expect(panel.getByRole("status", { name: "סטטוס: נכשל" })).toBeVisible({ timeout: 120_000 });
  });

  test("ask with Enter and answer the clarification with the keyboard (office A, read-only)", async ({ page }) => {
    await keyboardLogin(page, USERS.dana);
    await expect(page).toHaveURL(/\/chat$/);
    await tabUntil(page, (el) => el.textContent?.trim() === "שיחה חדשה" && el.tagName === "BUTTON");
    await page.keyboard.press("Enter");
    await expect(answerCards(page)).toHaveCount(0);

    await tabUntil(page, (el) => el.id === "question");
    await page.keyboard.type("מה מחיר למ״ר ברמת גן בשכונת חרוזים בשנת 2024?");
    await page.keyboard.press("Enter");
    await expect(answerCards(page)).toHaveCount(1);
    // Focus moves to the new answer so screen-reader and keyboard users land on it.
    await expect(answerCards(page).nth(0)).toBeFocused();
    await expect(answerCards(page).nth(0)).toContainText("לאיזה נתון הכוונה?");

    await tabUntil(page, (el) => el.textContent?.trim() === "מחירי עסקאות" && el.tagName === "BUTTON");
    await page.keyboard.press("Enter");
    await expect(answerCards(page)).toHaveCount(2);
    await expect(answerCards(page).nth(1)).toBeFocused();
    await expect(answerCards(page).nth(1)).toContainText("לפי איזה תאריך לסנן את השנה?");

    // Shift+Enter inserts a newline instead of sending.
    await tabUntil(page, (el) => el.id === "question");
    await page.keyboard.type("שורה ראשונה");
    await page.keyboard.press("Shift+Enter");
    await page.keyboard.type("שורה שנייה");
    await expect(page.getByRole("textbox", { name: "שאלה" })).toHaveValue("שורה ראשונה\nשורה שנייה");
    await expect(answerCards(page)).toHaveCount(2);
  });
});
