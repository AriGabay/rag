import { expect, test } from "@playwright/test";
import { login, PASSWORD, USERS } from "./helpers";

test.describe("authentication", () => {
  test("unauthenticated visit redirects to /login and returns to the page after login", async ({ page }) => {
    await page.goto("/documents");
    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByRole("heading", { name: "כניסה למאגר הידע" })).toBeVisible();

    await page.getByLabel("דוא״ל").fill(USERS.dana);
    await page.getByLabel("סיסמה").fill(PASSWORD);
    await page.getByRole("button", { name: "כניסה" }).click();
    await expect(page).toHaveURL(/\/documents$/);
    await expect(page.getByRole("heading", { level: 1 })).toHaveText("מסמכים");
  });

  test("wrong password shows a Hebrew error and stays on /login", async ({ page }) => {
    await page.goto("/login");
    // Office B admin: one failed attempt per run, far below the lockout threshold, and no impact on office A users.
    await page.getByLabel("דוא״ל").fill(USERS.adminB);
    await page.getByLabel("סיסמה").fill("not-the-password");
    await page.getByRole("button", { name: "כניסה" }).click();
    // Next.js also mounts an empty role="alert" route announcer, so scope to the login form.
    const form = page.locator("form", { has: page.getByRole("heading", { name: "כניסה למאגר הידע" }) });
    const alert = form.getByRole("alert");
    await expect(alert).toBeVisible();
    await expect(alert).toHaveText(/שגוי/);
    await expect(alert).toHaveText(/^[\u0590-\u05FF\s.,"'-]+$/);
    await expect(page).toHaveURL(/\/login$/);

    // Empty fields are validated in Hebrew too.
    await page.getByLabel("סיסמה").fill("");
    await page.getByRole("button", { name: "כניסה" }).click();
    await expect(form.getByRole("alert")).toHaveText("יש להזין דוא״ל וסיסמה.");
  });

  test("login then logout ends the session", async ({ page }) => {
    await login(page, USERS.dana);
    await expect(page.getByText("דנה (עובדת, קבוצה 1)")).toBeVisible();
    await page.getByRole("button", { name: "יציאה" }).click();
    await expect(page).toHaveURL(/\/login$/);
    // The session cookie no longer works: protected pages bounce back to /login.
    await page.goto("/chat");
    await expect(page).toHaveURL(/\/login$/);
    const me = await page.request.get("/api/auth/me");
    expect(me.status()).toBe(401);
  });
});
