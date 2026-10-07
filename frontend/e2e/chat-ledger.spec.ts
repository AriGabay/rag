import { expect, test } from "@playwright/test";
import { assistantMessages, login, USERS } from "./helpers";

// How an answer's coverage is shown. The conversation is real (office B), but its messages are served from a fixed
// synthetic payload, so the test checks the rendering of the server's coverage ledger, not a model's choices.

const NOW = new Date().toISOString();

function messages(conversationId: string, ledger: object, markdown: string) {
  const conversation = { id: conversationId, title: "בדיקת כיסוי", updated_at: NOW, created_at: NOW, archived: false,
    engine: "rag" };
  const doc = (n: number) => ({ document_id: `00000000-0000-0000-0000-00000000000${n}`, title: `שומה סינתטית ${n}` });
  const answer = {
    kind: "rag", status: "partial", markdown, claims: [], clarification: null, missing: null,
    sources: [{ id: "S1", document_id: doc(1).document_id, version_id: "00000000-0000-0000-0000-0000000000a1",
      title: doc(1).title, section: null, location: "סעיף \"השומה\"", kind: "text", text: "השווי למ\"ר הוא 9,500 ₪.",
      block_start: null, block_end: null, table_index: null, page_list: null, chunk_id: null }],
    measurements: [], computations: [], documents: [doc(1)],
    verification: { judged: true, judge_status: "ok", problems: [] }, searches: ["שווי"], coverage: [],
    ledger: { cited: [doc(1)], ...ledger }, scope_kind: (ledger as { scope_kind: string }).scope_kind, focus: null,
  };
  return {
    conversation, has_more: false,
    messages: [
      { id: "m1", role: "user", content: "מה השווי למ״ר בעיר הבדיקה?", status: "done", error: null, progress: [],
        answer: null, reply_to: null, client_id: null, created_at: NOW, stale: false, cancel_requested: false },
      { id: "m2", role: "assistant", content: markdown, status: "done", error: null, progress: [], answer,
        reply_to: "m1", client_id: null, created_at: NOW, stale: false, cancel_requested: false, usage: [] },
    ],
  };
}

test("a set answer that did not cover its whole scope shows the coverage note and the ledger", async ({ page }) => {
  await login(page, USERS.adminB);
  const created = await page.request.post("/api/chat/conversations");
  const { id } = await created.json();
  const doc = (n: number) => ({ document_id: `00000000-0000-0000-0000-00000000000${n}`, title: `שומה סינתטית ${n}` });
  const ledger = {
    scope_kind: "set", scope_query: "עיר הבדיקה", matching: [doc(1), doc(2), doc(3)], checked: [doc(1), doc(2)],
    with_data: [doc(1)], not_checked: [doc(3)], unused: [doc(2)], partially_read: [], omitted: [], complete: false,
  };
  const md = "השווי למ\"ר הוא 9,500 ₪ [S1].\n\n> **כיסוי:** 3 מסמכים מתאימים לתחום \"עיר הבדיקה\"; התשובה מבוססת על 1 מהם.";
  await page.route(`**/api/chat/conversations/${id}/messages*`, (route) =>
    route.fulfill({ json: messages(id, ledger, md) }));
  await page.goto(`/chat?c=${id}`);
  const reply = assistantMessages(page).last();
  await expect(reply.locator(".body")).toContainText("כיסוי:");
  await reply.getByText(/מקורות ופרטים/).click();
  const block = reply.getByTestId("ledger");
  await expect(block).toContainText("3 מסמכים מתאימים");
  await expect(block).toContainText("התשובה אינה מכסה את כל התחום");
  await expect(block).toContainText("לא נבדקו: שומה סינתטית 3");
  await expect(block).toContainText("נבדקו, לא נמצא בהם נתון שנכלל בתשובה: שומה סינתטית 2");
  await page.request.delete(`/api/chat/conversations/${id}`);
});

test("a focused answer lists other documents whose titles match the question", async ({ page }) => {
  await login(page, USERS.adminB);
  const { id } = await (await page.request.post("/api/chat/conversations")).json();
  const ledger = { scope_kind: "focused", scope_query: "", complete: false,
    also_matching: [{ document_id: "00000000-0000-0000-0000-000000000002", title: "שומה סינתטית 2" }] };
  await page.route(`**/api/chat/conversations/${id}/messages*`, (route) =>
    route.fulfill({ json: messages(id, ledger, "השווי למ\"ר הוא 9,500 ₪ [S1].") }));
  await page.goto(`/chat?c=${id}`);
  const reply = assistantMessages(page).last();
  await reply.getByText(/מקורות ופרטים/).click();
  await expect(reply.getByTestId("ledger")).toContainText("מסמכים נוספים שכותרתם מתאימה לשאלה ולא נבדקו: שומה סינתטית 2");
  await page.request.delete(`/api/chat/conversations/${id}`);
});
