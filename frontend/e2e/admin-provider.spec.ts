import { expect, test } from "@playwright/test";
import { apiLogin, login, shot, USERS } from "./helpers";

// U2 / R8: honest provider status. Runs as the office B admin so office A's settings stay untouched. The
// connection test sends only a fixed synthetic prompt (no office content); with a key on the stack it is a real
// call, so the result may be any status, and the test checks that it is shown, not that it passed.
const MODE_LABELS = /מודל ענן פעיל|תקלה בספק המודל|מצב דמו: מודל מדומה בלבד|מצב מוגבל/;
const STATUS_LABELS =
  /החיבור תקין|לא נמצא בשרת מפתח|הספק דחה את המפתח|המודל שנבחר אינו זמין|הספק לא הגיב בזמן|חריגה ממגבלת קצב|מכסת השימוש|הספק סירב|נקטעה לפני סיומה|לא תאמה את המבנה|תקלה כללית בפנייה/;

interface Settings {
  provider_name: string;
  model: string;
  key_present: boolean;
}

test.describe("Admin provider status", () => {
  test("shows provider, model, key presence and the connection-test result", async ({ page, request }) => {
    await apiLogin(request, USERS.adminB);
    const settings = (await (await request.get("/api/admin/settings")).json()) as Settings;
    await request.post("/api/auth/logout");

    await login(page, USERS.adminB);
    await page.getByRole("navigation", { name: "ניווט ראשי" }).getByRole("link", { name: "ניהול" }).click();
    const panel = page.getByRole("region", { name: "מודל שפה בענן" });
    await expect(panel).toBeVisible();

    await expect(panel.getByText(settings.provider_name, { exact: true })).toBeVisible();
    await expect(panel.getByText(settings.model, { exact: true }).first()).toBeVisible();
    await expect(panel.getByText(settings.key_present ? "מפתח נמצא" : "לא נמצא מפתח", { exact: true })).toBeVisible();
    await expect(panel.getByRole("status", { name: /^מצב ספק:/ })).toHaveText(MODE_LABELS);
    await expect(panel.getByText(/ללא תוכן ממסמכי המשרד/)).toBeVisible();

    const result = panel.getByTestId("provider-last-test");
    await panel.getByRole("button", { name: "בדיקת חיבור" }).click();
    await expect(panel.getByRole("button", { name: "בדיקת חיבור" })).toBeEnabled({ timeout: 60_000 });
    await expect(result).toContainText(/הצליחה|נכשלה/);
    await expect(result).toContainText(STATUS_LABELS);
    await expect(result).not.toContainText("טרם בוצעה");
    if (!settings.key_present) await expect(result).toContainText("לא נמצא בשרת מפתח");
    // The key never reaches the browser.
    await expect(page.locator("body")).not.toContainText(/sk-[A-Za-z0-9_-]{8,}/);
    await page.screenshot({ path: shot("admin-provider-status.png"), fullPage: true });
  });

  test("an employee cannot run the connection test", async ({ request }) => {
    await apiLogin(request, USERS.dana);
    const res = await request.post("/api/admin/provider/test");
    expect(res.status()).toBe(403);
    await request.post("/api/auth/logout");
  });
});
