import { expect, test, type Page } from "@playwright/test";
import { answerWith, message, thread, type Json } from "./answer-fixtures";
import { assistantMessages, login, USERS } from "./helpers";

// Report a problematic answer (round 25 U5, R11, KTD9). The conversation is real (office B); its messages and the report routes
// are fixed synthetic payloads, so the test checks the user's dialog and the admin's views, not the server's snapshot (the backend
// tests do: test_answer_reports.py).

const REPORT_ID = "11111111-2222-3333-4444-555555555555";
const FAILED = "אירעה תקלה בעיבוד התשובה. אפשר לנסות שוב.";

/** A real conversation whose thread is `items`; the user's reports of it are `reports`. Returns the conversation id. */
async function open(page: Page, items: Json[], reports: Json[] = []): Promise<string> {
  await login(page, USERS.adminB);
  const { id } = await (await page.request.post("/api/chat/conversations")).json();
  await page.route(`**/api/chat/conversations/${id}/messages*`, (route) => route.fulfill({ json: thread(id, items) }));
  await page.route(`**/api/chat/conversations/${id}/reports`, (route) => route.fulfill({ json: { reports } }));
  await page.goto(`/chat?c=${id}`);
  await expect(assistantMessages(page).last()).toBeVisible();
  return id;
}

const asked = (id: string) => message(id, "user", "מה השווי למ״ר?", null);
const done = (id: string, replyTo: string) =>
  message(id, "assistant", "השווי הוא 9,500 ₪ [S1].", answerWith({ status: "answered", markdown: "השווי למ״ר בשומה הוא 9,500 ₪ [S1]." }, { completeness: { status: "full", requirements: 1, missing: [] } }), { reply_to: replyTo });
const failed = (id: string, replyTo: string) =>
  message(id, "assistant", "", null, { reply_to: replyTo, status: "failed", error: FAILED });

test.describe("the user's report dialog", () => {
  test("the action opens the dialog; submit waits for a category; the report id is shown, copyable and announced; the marker reopens it prefilled", async ({ page, context }) => {
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    const id = await open(page, [asked("m1"), done("m2", "m1")]);
    const bodies: Json[] = [];
    await page.route("**/api/chat/messages/m2/report", async (route) => {
      bodies.push(route.request().postDataJSON() as Json);
      await route.fulfill({ json: { id: REPORT_ID, category: bodies.at(-1)?.category, note: bodies.at(-1)?.note, message_id: "m2",
        updated: bodies.length > 1, created_at: new Date().toISOString() } });
    });
    const reply = assistantMessages(page).last();
    await reply.getByTestId("report-action").click();
    const dialog = page.getByRole("dialog", { name: "דיווח על תשובה" });
    await expect(dialog).toBeVisible();
    // no category is preselected and submit is disabled until one is chosen
    await expect(dialog.getByRole("radio", { checked: true })).toHaveCount(0);
    await expect(dialog.getByRole("radio")).toHaveCount(5);
    await expect(dialog.getByTestId("report-submit")).toBeDisabled();
    // the limit on the note is visible
    await expect(dialog.getByTestId("report-note-count")).toContainText("1,000");
    await dialog.getByRole("radio", { name: "המקור שצוטט שגוי" }).check();
    await expect(dialog.getByTestId("report-submit")).toBeEnabled();
    await dialog.getByLabel("הערה (לא חובה)").fill("הציטוט אינו מהשומה");
    await expect(dialog.getByTestId("report-note-count")).toContainText("18");
    await dialog.getByTestId("report-submit").click();
    expect(bodies).toEqual([{ category: "wrong_source", note: "הציטוט אינו מהשומה" }]);
    // the id is shown in a live region and can be copied
    await expect(dialog.getByTestId("report-status")).toHaveAttribute("aria-live", "polite");
    await expect(dialog.getByTestId("report-status")).toContainText("הדיווח נשלח");
    await expect(dialog.getByTestId("report-id")).toHaveText(REPORT_ID);
    await dialog.getByRole("button", { name: "העתקת מספר הדיווח" }).click();
    await expect(dialog.getByRole("button", { name: "המספר הועתק" })).toBeVisible();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(REPORT_ID);
    await dialog.getByRole("button", { name: "סגירה" }).click();
    await expect(dialog).toHaveCount(0);
    // the answer now shows the marker, and the action reopens the report prefilled, so a resubmit reads as an update
    await expect(reply.getByTestId("report-marker")).toBeVisible();
    await expect(reply.getByTestId("report-action")).toHaveAttribute("data-reported", "true");
    await reply.getByTestId("report-action").click();
    const again = page.getByRole("dialog", { name: "עדכון דיווח על תשובה" });
    await expect(again.getByRole("radio", { name: "המקור שצוטט שגוי" })).toBeChecked();
    await expect(again.getByLabel("הערה (לא חובה)")).toHaveValue("הציטוט אינו מהשומה");
    await again.getByRole("radio", { name: "אחר", exact: true }).check();
    await again.getByRole("button", { name: "עדכון הדיווח" }).click();
    await expect(again.getByTestId("report-status")).toContainText("הדיווח עודכן");
    expect(bodies[1]).toEqual({ category: "other", note: "הציטוט אינו מהשומה" });
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("an earlier report of the user is loaded with the conversation: the marker shows and the dialog opens prefilled", async ({ page }) => {
    const id = await open(page, [asked("m1"), done("m2", "m1")],
      [{ id: REPORT_ID, message_id: "m2", category: "missing", note: "חסר שיעור ההיוון" }]);
    const reply = assistantMessages(page).last();
    await expect(reply.getByTestId("report-marker")).toBeVisible();
    await reply.getByTestId("report-action").click();
    const dialog = page.getByRole("dialog", { name: "עדכון דיווח על תשובה" });
    await expect(dialog.getByRole("radio", { name: "חסר מידע בתשובה" })).toBeChecked();
    await expect(dialog.getByLabel("הערה (לא חובה)")).toHaveValue("חסר שיעור ההיוון");
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("409, 404 and 422 each show their own inline text, and the dialog stays open for another try", async ({ page }) => {
    const id = await open(page, [asked("m1"), done("m2", "m1")]);
    const texts: Record<number, string> = {
      409: "עדיין בעיבוד", 404: "כבר לא קיימת", 422: "הדיווח אינו תקין",
    };
    let status = 409;
    await page.route("**/api/chat/messages/m2/report", (route) => route.fulfill({ status, json: { detail: "x" } }));
    await assistantMessages(page).last().getByTestId("report-action").click();
    const dialog = page.getByRole("dialog", { name: "דיווח על תשובה" });
    await dialog.getByRole("radio", { name: "התשובה שגויה" }).check();
    for (const code of [409, 404, 422]) {
      status = code;
      await dialog.getByTestId("report-submit").click();
      await expect(dialog.getByTestId("report-error")).toContainText(texts[code]);
      await expect(dialog.getByTestId("report-submit")).toBeEnabled();   // sending ended; the form is still filled
    }
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("submit is disabled with progress text while the report is sent", async ({ page }) => {
    const id = await open(page, [asked("m1"), done("m2", "m1")]);
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    await page.route("**/api/chat/messages/m2/report", async (route) => {
      await gate;
      await route.fulfill({ json: { id: REPORT_ID, category: "wrong", note: "", message_id: "m2", updated: false, created_at: "" } });
    });
    await assistantMessages(page).last().getByTestId("report-action").click();
    const dialog = page.getByRole("dialog", { name: "דיווח על תשובה" });
    await dialog.getByRole("radio", { name: "התשובה שגויה" }).check();
    await dialog.getByTestId("report-submit").click();
    await expect(dialog.getByTestId("report-submit")).toBeDisabled();
    await expect(dialog.getByTestId("report-submit")).toContainText("שולח");
    release();
    await expect(dialog.getByTestId("report-id")).toHaveText(REPORT_ID);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });
});

test.describe("where the action is", () => {
  test("it is on a failed message that is not the last, and the regenerate button of the last answer still works", async ({ page }) => {
    const id = await open(page, [asked("m1"), failed("m2", "m1"), asked("m3"), done("m4", "m3")]);
    const [first, last] = [assistantMessages(page).nth(0), assistantMessages(page).nth(1)];
    await expect(first.locator(".msg-error")).toContainText(FAILED);
    await expect(first.getByRole("button", { name: "ניסיון חוזר" })).toHaveCount(0);   // only the last can be retried
    await expect(first.getByTestId("report-action")).toBeVisible();                     // but any failed one can be reported
    await first.getByTestId("report-action").click();
    await expect(page.getByRole("dialog", { name: "דיווח על תשובה" })).toBeVisible();
    await page.keyboard.press("Escape");
    // the regenerate button of the last answer is untouched
    let retried = false;
    await page.route("**/api/chat/messages/m4/retry", (route) => {
      retried = true;
      return route.fulfill({ json: done("m5", "m3") });
    });
    await last.getByRole("button", { name: "יצירת תשובה מחדש" }).click();
    await expect.poll(() => retried).toBe(true);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("it is inside a cancelled block regardless of position, and on a done answer that is not the last", async ({ page }) => {
    const cancelled = message("m2", "assistant", "", null, { reply_to: "m1", status: "cancelled", error: "העיבוד נעצר לבקשתך." });
    const id = await open(page, [asked("m1"), cancelled, asked("m3"), done("m4", "m3"), asked("m5"), done("m6", "m5")]);
    await expect(assistantMessages(page).nth(0).locator(".msg-cancelled").getByTestId("report-action")).toBeVisible();
    await expect(assistantMessages(page).nth(1).getByTestId("report-action")).toBeVisible();
    await expect(assistantMessages(page).nth(2).getByTestId("report-action")).toBeVisible();
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("it is never on a running answer", async ({ page }) => {
    const running = message("m2", "assistant", "", null, { reply_to: "m1", status: "running", progress: [{ step: "queued", label: "ממתין לעיבוד" }] });
    await page.route("**/api/chat/messages/m2", (route) => route.fulfill({ json: running }));
    const id = await open(page, [asked("m1"), running]);
    await expect(assistantMessages(page).last().getByTestId("report-action")).toHaveCount(0);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });
});

test.describe("the admin's reports", () => {
  const rows = [
    { id: "aaaaaaaa-0000-0000-0000-000000000002", category: "wrong", message_status: "done", message_exists: true, created_at: "2026-10-29T10:00:00Z" },
    { id: "aaaaaaaa-0000-0000-0000-000000000001", category: "missing", message_status: "failed", message_exists: false, created_at: "2026-10-28T10:00:00Z" },
  ];
  const detail = (id: string, exists: boolean) => ({
    ...rows.find((r) => r.id === id), reporter_id: "u1", note: "הערה פרטית של המדווח", message_id: exists ? "m2" : null, conversation_id: null,
    build_sha: "abc1234", model_ids: { agent: "model-x" }, updated_at: "2026-10-29T10:00:00Z",
    snapshot: { version: 1, counts: { rounds: 1 }, resolution: { timing: { total_seconds: 3.2 } } },
  });

  test("an empty office shows the empty-state line", async ({ page }) => {
    await login(page, USERS.adminB);
    await page.route("**/api/admin/answer-reports", (route) => route.fulfill({ json: { reports: [] } }));
    await page.goto("/admin");
    const panel = page.getByTestId("answer-reports");
    await expect(panel.getByText("עדיין לא דווחו תשובות בעייתיות.")).toBeVisible();
    await expect(panel.locator("table")).toHaveCount(0);
  });

  test("the list keeps the server's newest-first order, says when the message is gone, opens a detail and links the export", async ({ page }) => {
    await login(page, USERS.adminB);
    await page.route("**/api/admin/answer-reports", (route) => route.fulfill({ json: { reports: rows } }));
    await page.route("**/api/admin/answer-reports/*", (route) => {
      const id = route.request().url().split("/").pop() ?? "";
      return route.fulfill({ json: detail(id, rows[0].id === id) });
    });
    await page.goto("/admin");
    const panel = page.getByTestId("answer-reports");
    const ids = panel.getByTestId("report-row-id");
    await expect(ids).toHaveText([rows[0].id, rows[1].id]);
    const body = panel.locator("tbody tr");
    await expect(body.nth(0)).not.toContainText("ההודעה אינה קיימת עוד");
    await expect(body.nth(1)).toContainText("ההודעה אינה קיימת עוד");
    await expect(body.nth(1)).toContainText("חסר מידע בתשובה");
    await expect(body.nth(1).getByRole("link", { name: "הורדת ייצוא" })).toHaveAttribute(
      "href", `/api/admin/answer-reports/${rows[1].id}/export`);
    await body.nth(1).getByRole("button", { name: "פרטים" }).click();
    const open = panel.getByTestId("report-detail");
    await expect(open.getByTestId("report-detail-note")).toHaveText("הערה פרטית של המדווח");
    await expect(open).toContainText("ההודעה אינה קיימת עוד");
    await expect(open.getByTestId("report-snapshot")).toContainText("total_seconds");
  });
});
