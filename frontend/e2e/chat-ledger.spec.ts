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
    verification: { judged: true, judge_status: "ok", removed: 0, partial: 0, annotated: 0 }, searches: ["שווי"], coverage: [],
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
    scope_kind: "set", scope_query: "עיר הבדיקה", matching: [doc(1), doc(2), doc(3)], read: [doc(1)],
    retrieved_only: [doc(2)], with_data: [doc(1)], not_checked: [doc(3)], unused: [doc(2)], partially_read: [],
    omitted: [], complete: false,
    levels: { [doc(1).document_id]: "verified", [doc(2).document_id]: "retrieved", [doc(3).document_id]: "located" },
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
  await expect(block).toContainText("התשובה אינה מכסה את כל מסמכי התחום");
  await expect(block.getByTestId("ledger-data")).toContainText("נקראו (סעיף/טבלה) 1 · נשלפו קטעים בלבד מ-1");
  await expect(block).toContainText("נשלפו מהם קטעים בלבד (הסעיף או הטבלה לא נקראו): שומה סינתטית 2");
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

test("document coverage is shown apart from data coverage, with the rows of a cited table", async ({ page }) => {
  await login(page, USERS.adminB);
  const { id } = await (await page.request.post("/api/chat/conversations")).json();
  const doc = (n: number) => ({ document_id: `00000000-0000-0000-0000-00000000000${n}`, title: `שומה סינתטית ${n}` });
  // every document gave a verified datum, but from retrieved passages only: document coverage 3/3, reading 0/3
  const ledger = {
    scope_kind: "set", scope_query: "עיר הבדיקה", matching: [doc(1), doc(2), doc(3)], read: [],
    retrieved_only: [doc(1), doc(2), doc(3)], with_data: [doc(1), doc(2), doc(3)], not_checked: [], unused: [],
    partially_read: [], omitted: [], complete: true,
    tables: [{ title: "שומה סינתטית 1", location: "טבלה", rows: 9, presented: 2 }],
    levels: { [doc(1).document_id]: "verified", [doc(2).document_id]: "verified", [doc(3).document_id]: "verified" },
  };
  await page.route(`**/api/chat/conversations/${id}/messages*`, (route) =>
    route.fulfill({ json: messages(id, ledger, "השווי למ\"ר הוא 9,500 ₪ [S1].") }));
  await page.goto(`/chat?c=${id}`);
  const reply = assistantMessages(page).last();
  await reply.getByText(/מקורות ופרטים/).click();
  const block = reply.getByTestId("ledger");
  await expect(block.getByTestId("ledger-documents")).toContainText("נתון מאומת מ-3");
  await expect(block.getByTestId("ledger-documents")).toContainText("התשובה מכסה את כל מסמכי התחום");
  await expect(block.getByTestId("ledger-data")).toContainText("נקראו (סעיף/טבלה) 0 · נשלפו קטעים בלבד מ-3");
  await expect(block.getByTestId("ledger-table")).toContainText("הוצגו ערכים מ-2 מתוך 9 שורות");
  await page.request.delete(`/api/chat/conversations/${id}`);
});

test("an answer stored before reading levels shows no data-coverage figures", async ({ page }) => {
  await login(page, USERS.adminB);
  const { id } = await (await page.request.post("/api/chat/conversations")).json();
  const doc = (n: number) => ({ document_id: `00000000-0000-0000-0000-00000000000${n}`, title: `שומה סינתטית ${n}` });
  // the older shape: "checked", no levels, no read / retrieved_only
  const ledger = { scope_kind: "set", scope_query: "עיר הבדיקה", matching: [doc(1)], checked: [doc(1)],
    with_data: [doc(1)], not_checked: [], unused: [], partially_read: [], omitted: [], complete: true };
  await page.route(`**/api/chat/conversations/${id}/messages*`, (route) =>
    route.fulfill({ json: messages(id, ledger, "השווי למ\"ר הוא 9,500 ₪ [S1].") }));
  await page.goto(`/chat?c=${id}`);
  const reply = assistantMessages(page).last();
  await reply.getByText(/מקורות ופרטים/).click();
  await expect(reply.getByTestId("ledger-documents")).toContainText("נתון מאומת מ-1");
  await expect(reply.getByTestId("ledger-data")).toHaveCount(0);
  await page.request.delete(`/api/chat/conversations/${id}`);
});

test("a count from a listing shows the listing inline and says the documents were not read", async ({ page }) => {
  await login(page, USERS.adminB);
  const { id } = await (await page.request.post("/api/chat/conversations")).json();
  const docs = [1, 2].map((n) => ({ document_id: `00000000-0000-0000-0000-00000000000${n}`, title: `שומה סינתטית ${n}` }));
  const md = "יש לנו 2 שומות בעיר הבדיקה [S1].\n\n> **כיסוי:** רשימת 2 המסמכים לתחום \"עיר הבדיקה\" נבדקה; תוכן המסמכים לא נקרא.";
  const body = messages(id, { scope_kind: "set", scope_query: "עיר הבדיקה", membership: true, matching: docs, pages: 1,
    pages_read: 1, complete: true, tables: [] }, md);
  const answer = body.messages[1].answer as Record<string, unknown>;
  answer.sources = [{ id: "S1", document_id: null, version_id: null, title: "מסמכים בתחום \"עיר הבדיקה\"",
    section: null, location: "רשימת מסמכים", kind: "listing",
    text: "תחום: מסמכים שמכילים את כל המונחים \"עיר הבדיקה\"\nעמוד 1 מתוך 1; סה\"כ 2 מסמכים מתאימים\n- \"שומה סינתטית 1\"\n- \"שומה סינתטית 2\"",
    block_start: null, block_end: null, table_index: null, page_list: null, chunk_id: null,
    listed_document_ids: docs.map((d) => d.document_id) }];
  answer.documents = [];
  answer.ledger = { ...(answer.ledger as object), cited: [] };
  await page.route(`**/api/chat/conversations/${id}/messages*`, (route) => route.fulfill({ json: body }));
  let blocksRequested = false;
  await page.route("**/api/documents/**/blocks*", (route) => {
    blocksRequested = true;
    return route.abort();
  });
  await page.goto(`/chat?c=${id}`);
  const reply = assistantMessages(page).last();
  await reply.getByText(/מקורות ופרטים/).click();
  await expect(reply.getByTestId("ledger")).toContainText("2 מסמכים ברשימה · נבדקו לפי התאמת המונחים; תוכן המסמכים לא נקרא");
  await reply.locator(".cite").first().click();
  const panel = page.getByRole("complementary", { name: "תצוגת מקור" });
  await expect(panel).toContainText("סה\"כ 2 מסמכים מתאימים");
  await expect(panel).toContainText("רשימת המסמכים שהוחזרה בחיפוש בתור הזה");
  expect(blocksRequested).toBe(false);
  await page.request.delete(`/api/chat/conversations/${id}`);
});
