import { expect, test } from "@playwright/test";
import { expectNumberInVisualOrder, login, USERS } from "./helpers";

// Gate 8 / R37: Hebrew RTL shell. Read-only (office A employee).
test.describe("RTL shell", () => {
  test("html is rtl/he, navigation is Hebrew, numbers stay in visual order", async ({ page }) => {
    await login(page, USERS.dana);
    await expect(page).toHaveURL(/\/chat$/);

    const html = page.locator("html");
    await expect(html).toHaveAttribute("dir", "rtl");
    await expect(html).toHaveAttribute("lang", "he");
    expect(await page.evaluate(() => getComputedStyle(document.body).direction)).toBe("rtl");

    const nav = page.getByRole("navigation", { name: "ניווט ראשי" });
    await expect(nav.getByRole("link")).toHaveText(["שאלות", "מסמכים", "בדיקת נתונים"]);
    // An employee does not see the admin screen.
    await expect(nav.getByRole("link", { name: "ניהול" })).toHaveCount(0);
    await expect(nav.getByRole("link", { name: "שאלות" })).toHaveAttribute("aria-current", "page");
    await expect(page.getByRole("button", { name: "יציאה" })).toBeVisible();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("שאלות על מאגר המשרד");

    // In RTL the navigation starts at the right: "שאלות" is to the right of "מסמכים".
    const first = await nav.getByRole("link", { name: "שאלות" }).boundingBox();
    const second = await nav.getByRole("link", { name: "מסמכים" }).boundingBox();
    expect(first && second && first.x > second.x).toBeTruthy();

    // The example question's year is isolated in <bdi> and reads left to right.
    await expectNumberInVisualOrder(page.locator(".thread"), "2024");

    // Navigation stays in Hebrew on every screen.
    await nav.getByRole("link", { name: "מסמכים" }).click();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("מסמכים");
    await nav.getByRole("link", { name: "בדיקת נתונים" }).click();
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("בדיקת נתונים");
  });

  test("admin sees the admin link", async ({ page }) => {
    await login(page, USERS.adminA);
    const nav = page.getByRole("navigation", { name: "ניווט ראשי" });
    await expect(nav.getByRole("link")).toHaveText(["שאלות", "מסמכים", "בדיקת נתונים", "ניהול"]);
  });
});
