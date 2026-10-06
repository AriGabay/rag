import { expect, test } from "@playwright/test";
import {
  answerCards,
  ask,
  chooseOption,
  conditionsOf,
  expectNumberInVisualOrder,
  login,
  resolveClarifications,
  shot,
  USERS,
} from "./helpers";

// Office A, read-only: only questions are asked (as dana, group G1). No documents or approvals change.
const AE1 = "מה מחיר למ״ר ברמת גן בשכונת חרוזים בשנת 2024?";
const FULLY_SPECIFIED = "מה מחיר למ״ר של עסקאות ברמת גן בשכונת חרוזים לפי תאריך עסקה בשנת 2024?";
const FOLLOW_UP = "ומה לגבי 2023?";

test.describe("chat messages (office A, read-only)", () => {
  test.beforeEach(async ({ page }) => {
    await login(page, USERS.dana);
    await page.getByRole("button", { name: "שיחה חדשה", exact: true }).click();
    await expect(answerCards(page)).toHaveCount(0);
  });

  test("AE1: an ambiguous price question asks for the data kind, then the date type", async ({ page }) => {
    await ask(page, AE1);
    const card = answerCards(page).nth(0); // a fixed handle: later answers are appended after it
    await expect(card).toContainText("לאיזה נתון הכוונה?");
    const options = page.getByRole("group", { name: "אפשרויות הבהרה" }).getByRole("button");
    await expect(options).toHaveText(["מחירי עסקאות", "שווי שנקבע בשומות"]);
    // No number is computed before the clarification.
    await expect(card.getByText("ממוצע מחירי המ״ר")).toHaveCount(0);
    await expect(page.getByText("ממתינה שאלת הבהרה.")).toBeVisible();

    await chooseOption(page, "מחירי עסקאות");
    await expect(page.getByText("בחירה: מחירי עסקאות")).toBeVisible();
    const next = answerCards(page).last();
    await expect(next).toContainText("לפי איזה תאריך לסנן את השנה?");
    await expect(page.getByRole("group", { name: "אפשרויות הבהרה" }).getByRole("button", { name: "תאריך העסקה" })).toBeVisible();
    await expect(page.getByRole("group", { name: "אפשרויות הבהרה" }).getByRole("button", { name: /המועד הקובע/ })).toBeVisible();
    // The answered clarification is no longer actionable.
    await expect(card.getByText("שאלת ההבהרה כבר טופלה.")).toBeVisible();
  });

  test("unknown neighborhood: abstains with a message and no number", async ({ page }) => {
    await ask(page, "מה מחיר למ״ר ברמת גן בשכונת נווה צדק בשנת 2024?");
    const card = answerCards(page).last();
    await expect(card.getByText("אין מספיק מידע", { exact: true })).toBeVisible();
    await expect(card).toContainText("אין במאגר המשרד רשומות עבור");
    await expect(card).toContainText("נווה צדק");
    await expect(card).toContainText("לא חושב מספר");
    await expect(card.locator(".figures")).toHaveCount(0);
    await expect(card.getByText("ממוצע מחירי המ״ר")).toHaveCount(0);
    await expect(card.getByText(/₪/)).toHaveCount(0);
    await expect(page.getByRole("group", { name: "אפשרויות הבהרה" })).toHaveCount(0);
  });

  test("AE4: follow-up 'ומה לגבי 2023?' changes only the year; new conversation resets conditions", async ({ page }) => {
    test.setTimeout(180_000);
    await ask(page, FULLY_SPECIFIED);
    // The question states data kind and date type, so only remaining ambiguities (area basis, property type, VAT)
    // are asked; none of them is the data kind.
    const asked = await resolveClarifications(page, [/^נטו/, /^דירה /, /^כולל מע״מ/]);
    expect(asked.join(" | ")).not.toContain("לאיזה נתון הכוונה?");

    const first = answerCards(page).last();
    await expect(first.getByText("ממוצע מחירי המ״ר", { exact: true })).toBeVisible();
    const c2024 = await conditionsOf(first);
    expect(c2024["סוג הנתון"]).toBe("מחירי עסקאות");
    expect(c2024["שכונה"]).toBe("חרוזים");
    expect(c2024["תאריך העסקה"]).toMatch(/^2024 /);

    // R37: the money figure is isolated and not reversed.
    const mean = (await first.locator(".figure-value bdi").first().innerText()).trim();
    const digits = /^[\d,.]+/.exec(mean)?.[0] ?? "";
    expect(digits).toMatch(/^\d{1,3}(,\d{3})+/);
    await expectNumberInVisualOrder(first.locator(".figure-value").first(), digits);

    // Follow-up typed and sent with Enter (keyboard path).
    const box = page.getByRole("textbox", { name: "שאלה" });
    const before = await answerCards(page).count();
    await box.fill(FOLLOW_UP);
    await box.press("Enter");
    await expect(answerCards(page)).toHaveCount(before + 1);
    await expect(box).toHaveValue("");
    const second = answerCards(page).last();
    await expect(second.getByText("ממוצע מחירי המ״ר", { exact: true })).toBeVisible();
    await expect(page.getByRole("group", { name: "אפשרויות הבהרה" })).toHaveCount(0);
    const c2023 = await conditionsOf(second);
    expect(c2023["תאריך העסקה"]).toMatch(/^2023 /);
    const withoutYear = (c: Record<string, string>) =>
      Object.fromEntries(Object.entries(c).filter(([k]) => k !== "תאריך העסקה"));
    expect(withoutYear(c2023)).toEqual(withoutYear(c2024));
    expect(Object.keys(withoutYear(c2023)).length).toBeGreaterThanOrEqual(3);
    await second.scrollIntoViewIfNeeded();
    await page.screenshot({ path: shot("chat-followup-2023.png"), fullPage: true });

    // Reopening the conversation shows its confirmed conditions.
    const convList = page.getByRole("complementary", { name: "שיחות" });
    await convList.locator('button[aria-current="true"]').click();
    const confirmed = page.getByText("תנאים שאושרו בשיחה:");
    await expect(confirmed).toBeVisible();
    const confirmedLine = page.locator("div.small", { has: confirmed });
    await expect(confirmedLine).toContainText("שכונה: חרוזים");
    await expect(confirmedLine).toContainText("2023");

    // New conversation: no confirmed conditions, and the same follow-up is no longer resolved against them.
    await page.getByRole("button", { name: "שיחה חדשה", exact: true }).click();
    await expect(confirmed).toHaveCount(0);
    await expect(answerCards(page)).toHaveCount(0);
    await ask(page, FOLLOW_UP);
    const fresh = answerCards(page).last();
    await expect(fresh.getByText("ממוצע מחירי המ״ר")).toHaveCount(0);
    // Without context the follow-up is ambiguous: a short clarification, never a figure. The rules ask for the
    // data kind (limited mode); in cloud mode the server asks what the follow-up refers to.
    await expect(fresh).toContainText(/לאיזה נתון הכוונה\?|שאלת ההמשך/);
  });

  test("a conversation started with 'שיחה חדשה' is titled by its first question in the list", async ({ page }) => {
    const question = "מה מחיר למ״ר ברמת גן בשכונת נווה צדק בשנת 2024?";
    await ask(page, question);
    const current = page.getByRole("complementary", { name: "שיחות" }).locator('button[aria-current="true"]');
    await expect(current).toContainText(question.slice(0, 30), { timeout: 10_000 });
  });
});
