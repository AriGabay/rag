import path from "node:path";
import { expect, test } from "@playwright/test";
import {
  assistantMessages,
  composer,
  ensureOfficeBDocument,
  FIXTURES,
  login,
  sendAndWait,
  setOfficeBCloud,
  shot,
  USERS,
} from "./helpers";

// The conversational chat against the real model (office B only, a synthetic report: invented names and values).
// Office B's cloud use is switched on for these tests and off again afterwards; office A is never touched.
const FILE = path.join(FIXTURES, "chat", "CHAT_synthetic_mixed_report.docx");
const TITLE = "CHAT synthetic mixed report";

test.describe.configure({ mode: "serial" });

test.describe("chat (office B, real model)", () => {
  test.beforeAll(async ({ request }) => {
    // the upload waits behind any processing already queued on the shared worker
    test.setTimeout(960_000);
    await setOfficeBCloud(request, true);
    await ensureOfficeBDocument(request, FILE, TITLE);
  });

  test.afterAll(async ({ request }) => {
    await setOfficeBCloud(request, false);
  });

  test("a question appears at once, shows progress, and is answered with a source that opens in place", async ({ page }) => {
    test.setTimeout(300_000);
    await login(page, USERS.adminB);
    await page.getByRole("button", { name: "שיחה חדשה" }).first().click();
    await composer(page).fill("מה דמי השכירות הראויים למ״ר בשומה של הגפן 12?");
    await composer(page).press("Enter");
    // the user's message is in the thread immediately, and the reply shows that work is going on
    await expect(page.locator(".msg-user").last()).toContainText("דמי השכירות הראויים");
    const reply = assistantMessages(page).last();
    await expect(reply.locator(".status-line")).toBeVisible();
    await expect(page.getByRole("button", { name: "עצירת התשובה" })).toBeVisible();
    await expect(reply.locator(".status-line")).toHaveCount(0, { timeout: 240_000 });
    await expect(reply.locator(".body")).toContainText("55");
    await expect(reply.locator(".body")).toContainText(/חודש/);
    // the conversation was created and the URL points at it
    await expect(page).toHaveURL(/\/chat\?c=/);
    // a citation opens the source panel at the cited place
    await reply.locator(".cite").first().click();
    const panel = page.getByRole("complementary", { name: "תצוגת מקור" });
    await expect(panel).toBeVisible();
    await expect(panel).toContainText(TITLE);
    await expect(panel.locator('[data-cited="true"]').first()).toBeVisible();
    await page.screenshot({ path: shot("chat-answer-source.png"), fullPage: true });
    await panel.getByRole("button", { name: "סגירת תצוגת המקור" }).click();
    await expect(panel).toHaveCount(0);
  });

  test("history survives a reload, and a follow-up keeps the conversation's document", async ({ page }) => {
    test.setTimeout(300_000);
    await login(page, USERS.adminB);
    await page.getByRole("button", { name: "שיחה חדשה" }).first().click();
    await sendAndWait(page, "מה השווי למ״ר בנוי שנקבע בשומה של הגפן 12?");
    const url = page.url();
    await page.reload();
    await expect(page).toHaveURL(url);
    await expect(page.locator(".msg-user")).toContainText("מה השווי למ״ר בנוי");
    await expect(assistantMessages(page).first().locator(".body")).toContainText("9,500");
    const followUp = await sendAndWait(page, "האם זה כולל מע״מ?");
    await expect(followUp.locator(".body")).toContainText(/ללא מע/);
  });

  test("switching conversations while one is working keeps each answer in its own conversation", async ({ page }) => {
    test.setTimeout(400_000);
    await login(page, USERS.adminB);
    await page.getByRole("button", { name: "שיחה חדשה" }).first().click();
    await composer(page).fill("מה השווי הכולל שנקבע לנכס בשומה של הגפן 12?");
    await composer(page).press("Enter");
    await expect(page).toHaveURL(/\/chat\?c=/);
    const first = new URL(page.url()).searchParams.get("c");
    // while it works, open a new conversation and ask something else there
    await page.getByRole("button", { name: "שיחה חדשה" }).first().click();
    await expect(page.locator(".msg")).toHaveCount(0);
    const other = await sendAndWait(page, "האם צפוי היטל השבחה בנכס בשומה של הגפן 12?");
    await expect(other.locator(".body")).toContainText(/לא צפוי/);
    await expect(other.locator(".body")).not.toContainText("11,300,000");
    // back in the first conversation: only its own question and its own answer
    await page.goto(`/chat?c=${first}`);
    const answers = assistantMessages(page);
    await expect(answers).toHaveCount(1);
    await expect(answers.first().locator(".status-line")).toHaveCount(0, { timeout: 240_000 });
    await expect(answers.first().locator(".body")).toContainText("11,300,000");
    await expect(page.locator(".msg-user")).toHaveCount(1);
  });

  test("stop cancels on the server and says so only once the server confirms", async ({ page }) => {
    test.setTimeout(240_000);
    await login(page, USERS.adminB);
    await page.getByRole("button", { name: "שיחה חדשה" }).first().click();
    await composer(page).fill("תסביר בפירוט את כל השיקולים של השמאי בשומה של הגפן 12");
    await composer(page).press("Enter");
    const stop = page.getByRole("button", { name: "עצירת התשובה" });
    await expect(stop).toBeVisible();
    const reply = assistantMessages(page).last();
    // stop only once the server is working on it (a step past "queued"); an answer that finished before the click
    // is a different, valid case (the stop leaves it done), covered by the API tests
    await expect(reply.locator(".status-line")).toContainText(/מבין את הבקשה|מחפש|קורא|מאתר|בודק|מאמת/, {
      timeout: 60_000,
    });
    await stop.click();
    // either still finishing the call already in flight ("עוצר…"), or confirmed as stopped
    await expect(reply).toContainText(/עוצר|נעצר/);
    await expect(reply).toContainText("העיבוד נעצר", { timeout: 180_000 });
    await expect(reply.locator(".body")).toHaveCount(0);
  });

  test("conversations can be searched, renamed, archived and deleted", async ({ page }) => {
    test.setTimeout(300_000);
    await login(page, USERS.adminB);
    await page.getByRole("button", { name: "שיחה חדשה" }).first().click();
    await sendAndWait(page, "כמה מקומות חניה יש במבנה בשומה של הגפן 12?");
    const sidebar = page.getByRole("complementary", { name: "היסטוריית שיחות" });
    await sidebar.getByRole("searchbox", { name: "חיפוש בשיחות" }).fill("מקומות חניה");
    const item = sidebar.getByRole("navigation", { name: "שיחות" }).getByRole("link", { name: /מקומות חניה/ }).first();
    await expect(item).toBeVisible();
    await sidebar.getByRole("button", { name: /פעולות לשיחה/ }).first().click();
    await page.getByRole("menuitem", { name: "שינוי שם" }).click();
    const input = sidebar.getByRole("textbox", { name: "שם השיחה" });
    await input.fill("חניות בגפן");
    await input.press("Enter");
    await sidebar.getByRole("searchbox", { name: "חיפוש בשיחות" }).fill("חניות בגפן");
    await expect(sidebar.getByRole("link", { name: "חניות בגפן" })).toBeVisible();
    await sidebar.getByRole("button", { name: /פעולות לשיחה חניות בגפן/ }).click();
    await page.getByRole("menuitem", { name: "העברה לארכיון" }).click();
    await expect(sidebar.getByRole("link", { name: "חניות בגפן" })).toHaveCount(0);
    await sidebar.getByRole("button", { name: "ארכיון שיחות" }).click();
    await expect(sidebar.getByRole("link", { name: "חניות בגפן" })).toBeVisible();
    page.once("dialog", (d) => void d.accept());
    await sidebar.getByRole("button", { name: /פעולות לשיחה חניות בגפן/ }).click();
    await page.getByRole("menuitem", { name: "מחיקה" }).click();
    await expect(sidebar.getByRole("link", { name: "חניות בגפן" })).toHaveCount(0);
  });

  test("Shift+Enter adds a line, Enter sends; RTL layout with the sidebar on the left; dark theme", async ({ page }) => {
    await login(page, USERS.adminB);
    await page.getByRole("button", { name: "שיחה חדשה" }).first().click();
    const box = composer(page);
    await box.fill("שורה ראשונה");
    await box.press("Shift+Enter");
    await box.pressSequentially("שורה שנייה");
    await expect(box).toHaveValue("שורה ראשונה\nשורה שנייה");
    await expect(page.locator(".msg")).toHaveCount(0);
    await expect(page.locator("html")).toHaveAttribute("dir", "rtl");
    const sidebar = await page.getByRole("complementary", { name: "היסטוריית שיחות" }).boundingBox();
    const main = await page.locator(".chat-main").boundingBox();
    expect(sidebar && main && sidebar.x < main.x).toBeTruthy();
    await page.getByRole("combobox", { name: "ערכת צבעים" }).selectOption("dark");
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    await page.getByRole("combobox", { name: "ערכת צבעים" }).selectOption("system");
  });

  test("on a phone the sidebar is a drawer and the composer stays usable", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await login(page, USERS.adminB);
    const sidebar = page.getByRole("complementary", { name: "היסטוריית שיחות", includeHidden: true });
    await expect(sidebar).toHaveAttribute("data-open", "false");
    await expect(composer(page)).toBeVisible();
    await page.getByRole("button", { name: "פתיחת סרגל הצד" }).click();
    await expect(sidebar).toHaveAttribute("data-open", "true");
    await page.screenshot({ path: shot("chat-mobile-drawer.png") });
    await page.getByRole("button", { name: "סגירת סרגל הצד" }).click();
    await expect(sidebar).toHaveAttribute("data-open", "false");
    const scroll = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(scroll).toBeLessThanOrEqual(0);
  });
});
