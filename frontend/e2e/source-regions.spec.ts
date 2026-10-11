import { expect, test, type Page } from "@playwright/test";
import { assistantMessages, login, USERS } from "./helpers";

// The source panel's PDF regions and corrected text (U6). The conversation is real (office B), but its messages, the
// cited document's blocks and the region images are served from fixed synthetic payloads, so the test checks the
// panel's rendering and states, not a model's choices or a processed document.

const NOW = new Date().toISOString();
const DOC = "00000000-0000-0000-0000-0000000000d1";
const VER = "00000000-0000-0000-0000-0000000000a1";
const BASE = `/api/documents/${DOC}/versions/${VER}`;
// a 1×1 PNG
const PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
  "base64",
);

function block(index: number, extra: object) {
  return {
    index, kind: "paragraph", section: "2. תיאור הנכס", section_path: ["2. תיאור הנכס"], paragraph_no: null, page: 3,
    media: null, source: "text", status: "read", note: null, text: "", bbox: [60, 100 + index * 80, 540, 160 + index * 80],
    method: "text_layer", reader_version: "pdf-test", content_hash: null, original_text: null,
    page_url: `${BASE}/pages/3/image`, region_url: `${BASE}/regions/${index}/image`, ...extra,
  };
}

const BLOCKS = [
  block(0, { text: "הנכס נמצא בשכונה שקטה ברחוב הדוגמה 4.", original_text: "הðכס ðמצא בשכוðה שקטה ברחוב הדוגמה 4." }),
  block(1, { kind: "table", text: "אזור | שטח\nצפון | 1,250", table: { headers: ["אזור", "שטח"], caption: null,
    title: [], notes: [], source: "vision", media: null, rows: [["צפון", "1,250"]] } }),
  block(2, { kind: "image", source: "none", status: "unread", method: "none",
    note: "טבלה בתמונה: נדרשת קריאה חזותית, שאינה מופעלת במשרד" }),
];

function messages(conversationId: string) {
  const conversation = { id: conversationId, title: "בדיקת מקור", updated_at: NOW, created_at: NOW, archived: false,
    engine: "rag" };
  const doc = { document_id: DOC, title: "שומה סינתטית" };
  const answer = {
    kind: "rag", status: "ok", markdown: "הנכס נמצא בשכונה שקטה [S1].", claims: [], clarification: null, missing: null,
    sources: [{ id: "S1", document_id: DOC, version_id: VER, title: doc.title, section: "2. תיאור הנכס",
      location: "עמוד 3", kind: "text", text: "הנכס נמצא בשכונה שקטה ברחוב הדוגמה 4.", block_start: 0, block_end: 0,
      table_index: null, page_list: [3], chunk_id: null }],
    measurements: [], computations: [], documents: [doc],
    verification: { judged: true, judge_status: "ok", removed: 0, partial: 0, annotated: 0 }, searches: [], coverage: [],
    ledger: { cited: [doc], scope_kind: "focused", scope_query: "", complete: true }, scope_kind: "focused", focus: null,
  };
  return {
    conversation, has_more: false,
    messages: [
      { id: "m1", role: "user", content: "איפה נמצא הנכס?", status: "done", error: null, progress: [], answer: null,
        reply_to: null, client_id: null, created_at: NOW, stale: false, cancel_requested: false },
      { id: "m2", role: "assistant", content: answer.markdown, status: "done", error: null, progress: [], answer,
        reply_to: "m1", client_id: null, created_at: NOW, stale: false, cancel_requested: false, usage: [] },
    ],
  };
}

/** Opens the source panel of a mocked answer; `recheck` answers the blocks request made after an image failed. */
async function openPanel(page: Page, recheck: "ok" | "gone" = "ok") {
  await login(page, USERS.adminB);
  const { id } = await (await page.request.post("/api/chat/conversations")).json();
  await page.route(`**/api/chat/conversations/${id}/messages*`, (route) => route.fulfill({ json: messages(id) }));
  await page.route("**/api/documents/**/blocks*", (route) => {
    const url = new URL(route.request().url());
    const single = url.searchParams.get("start") === url.searchParams.get("end");
    if (single && recheck === "gone") return route.fulfill({ status: 404, json: { detail: "לא נמצא" } });
    const start = Number(url.searchParams.get("start") ?? 0);
    const end = Number(url.searchParams.get("end") ?? 99);
    return route.fulfill({ json: { document_id: DOC, version_id: VER, title: "שומה סינתטית", is_current: true,
      mime_type: "application/pdf", total: BLOCKS.length, file_url: `${BASE}/file`,
      blocks: BLOCKS.filter((b) => b.index >= start && b.index <= end) } });
  });
  await page.goto(`/chat?c=${id}`);
  await assistantMessages(page).last().locator(".cite").first().click();
  const panel = page.getByRole("complementary", { name: "תצוגת מקור" });
  await expect(panel).toContainText("צפון"); // the blocks around the cited one are shown
  return { panel, id };
}

test("a corrected block shows the corrected text and the original on request", async ({ page }) => {
  const { panel, id } = await openPanel(page);
  const cited = panel.locator('[data-cited="true"]');
  await expect(cited).toContainText("הנכס נמצא בשכונה שקטה");
  await expect(cited).toContainText("טקסט מתוקן");
  const toggle = cited.getByRole("button", { name: "הטקסט המקורי כפי שחולץ" });
  await expect(toggle).toHaveAttribute("aria-pressed", "false");
  await toggle.focus();
  await page.keyboard.press("Enter");
  await expect(toggle).toHaveAttribute("aria-pressed", "true");
  const original = cited.locator('.original-text [dir="auto"]');
  await expect(original).toHaveText("הðכס ðמצא בשכוðה שקטה ברחוב הדוגמה 4.");
  await page.request.delete(`/api/chat/conversations/${id}`);
});

test("region markers name page, kind and status; a region opens as an image, an unread one shows its reason", async ({
  page,
}) => {
  await page.route("**/regions/*/image", (route) => route.fulfill({ status: 200, contentType: "image/png", body: PNG }));
  const { panel, id } = await openPanel(page);
  const table = panel.getByRole("button", { name: "אזור במסמך: עמוד 3, טבלה, נקרא" });
  await expect(table).toHaveAttribute("aria-expanded", "false");
  await table.focus();
  await page.keyboard.press("Enter");
  await expect(table).toHaveAttribute("aria-expanded", "true");
  await expect(panel.getByRole("img", { name: "טבלה בעמוד 3 של המסמך" })).toBeVisible();

  const unread = panel.getByRole("button", { name: "אזור במסמך: עמוד 3, תמונה, לא נקרא" });
  await unread.click();
  await expect(panel).toContainText("האזור לא נקרא: טבלה בתמונה: נדרשת קריאה חזותית, שאינה מופעלת במשרד");
  await expect(panel.getByRole("img", { name: "תמונה בעמוד 3 של המסמך" })).toHaveCount(0);
  await page.request.delete(`/api/chat/conversations/${id}`);
});

test("an image that cannot be shown falls back to the block's extracted text", async ({ page }) => {
  await page.route("**/regions/*/image", (route) => route.fulfill({ status: 500, body: "" }));
  const { panel, id } = await openPanel(page, "ok");
  await panel.getByRole("button", { name: "אזור במסמך: עמוד 3, טבלה, נקרא" }).click();
  await expect(panel).toContainText("התמונה אינה זמינה");
  await expect(panel).toContainText("הטקסט שחולץ מהאזור:");
  await page.request.delete(`/api/chat/conversations/${id}`);
});

test("a document no longer visible shows nothing from it when its region fails", async ({ page }) => {
  await page.route("**/regions/*/image", (route) => route.fulfill({ status: 404, body: "" }));
  const { panel, id } = await openPanel(page, "gone");
  await panel.getByRole("button", { name: "אזור במסמך: עמוד 3, טבלה, נקרא" }).click();
  await expect(panel.getByRole("alert")).toContainText("המקור אינו זמין עוד");
  await expect(panel).not.toContainText("הנכס נמצא בשכונה שקטה");
  await expect(panel).not.toContainText("צפון");
  await page.request.delete(`/api/chat/conversations/${id}`);
});
