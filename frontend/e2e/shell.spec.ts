import { expect, test } from "@playwright/test";
import { login, USERS } from "./helpers";

// Hebrew RTL shell (R37). Read-only: no message is sent (office A employee and admin).
test.describe("RTL shell", () => {
  test("html is rtl/he; the chat's sidebar holds the navigation; other screens keep the header", async ({ page }) => {
    await login(page, USERS.dana);
    await expect(page).toHaveURL(/\/chat/);

    const html = page.locator("html");
    await expect(html).toHaveAttribute("dir", "rtl");
    await expect(html).toHaveAttribute("lang", "he");
    expect(await page.evaluate(() => getComputedStyle(document.body).direction)).toBe("rtl");

    const sidebar = page.getByRole("complementary", { name: "היסטוריית שיחות" });
    await expect(sidebar.getByRole("link", { name: "מסמכים" })).toBeVisible();
    await expect(sidebar.getByRole("link", { name: "בדיקת נתונים" })).toBeVisible();
    // An employee does not see the admin screen.
    await expect(sidebar.getByRole("link", { name: "ניהול" })).toHaveCount(0);
    await expect(sidebar.getByRole("button", { name: "יציאה" })).toBeVisible();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("במה אפשר לעזור?");

    // Navigation stays in Hebrew on every screen.
    await sidebar.getByRole("link", { name: "מסמכים" }).click();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("מסמכים");
    const nav = page.getByRole("navigation", { name: "ניווט ראשי" });
    await nav.getByRole("link", { name: "בדיקת נתונים" }).click();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("בדיקת נתונים");
    await nav.getByRole("link", { name: "שאלות" }).click();
    await expect(page).toHaveURL(/\/chat/);
  });

  test("admin sees the admin link", async ({ page }) => {
    await login(page, USERS.adminA);
    const sidebar = page.getByRole("complementary", { name: "היסטוריית שיחות" });
    await expect(sidebar.getByRole("link", { name: "ניהול" })).toBeVisible();
  });
});
