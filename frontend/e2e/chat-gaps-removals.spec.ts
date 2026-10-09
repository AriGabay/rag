import { expect, test, type Locator, type Page, type Route } from "@playwright/test";
import { assistantMessages, composer, login, USERS } from "./helpers";

// Gaps and removals in the answer (round 7 U8, R11, R13, R29). The conversation is real (office B), but its messages,
// the diagnostics route and the cited document are fixed synthetic payloads: the test checks how the server's gap
// paragraph, component outcomes and removal decisions are shown, not a model's choices.

const NOW = new Date().toISOString();
const DOC = "00000000-0000-0000-0000-0000000000d7";
const VER = "00000000-0000-0000-0000-0000000000a7";
const BASE = `/api/documents/${DOC}/versions/${VER}`;
const TITLE = "שומה סינתטית לבדיקת פערים";
const READING = "reading-synthetic-7";
const GAP_LINE = "לא אותר בחיפושים שבוצעו: שיעור ההיוון.";
const DRAFT = "שיעור ההיוון בשומה הוא 7% לשנה.";
const REASON = "המקור מציין שיעור חודשי, לא שנתי.";

// a 1×1 white PNG: the viewer only needs an image that decodes
const PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC",
  "base64",
);

type Json = Record<string, unknown>;

function anchor(): Json {
  return {
    v: 1, precision: "span", precision_label: "המקום המצוטט מסומן בעמוד", region: null, degraded: null,
    document_id: DOC, version_id: VER, reading_id: READING, block_start: 0, block_end: 0,
    pages: [{ page: 3, printed_label: null, width: 600, height: 900, rects: [[0.1, 0.2, 0.6, 0.25]], focus: [] }],
    location: { title: TITLE, label: "עמוד 3, סעיף «4. שיטת ההיוון»", page: 3, page_end: null, printed_page: null,
      section: "4. שיטת ההיוון" },
    table: null, structured: null, truncated: false,
  };
}

function source(id: string, extra: Json = {}): Json {
  return {
    id, document_id: DOC, version_id: VER, title: TITLE, section: "4. שיטת ההיוון", location: "עמוד 3", kind: "text",
    text: "שיעור ההיוון החודשי הוא 0.6%.", block_start: 0, block_end: 0, table_index: null, page_list: [3],
    chunk_id: null, reading_id: READING, status: "complete", anchor: anchor(), ...extra,
  };
}

function component(id: string, text: string, status: string, extra: Json = {}): Json {
  return {
    id, text, kind: "information", aspect: "", parent: "", conditional: false, subject: "", status,
    evidence_state: null, limitation: null, limitation_text: null, stated: false, gap: null, units: [],
    removed_units: [], absence_units: [], related: [], place: null, document: null, document_id: null,
    document_title: null, searched: null, claimed: null, detail: "", removal_kinds: [], ...extra,
  };
}

const REMOVAL_SENTENCE: Record<string, string> = {
  wrong_unit: "הוסרה טענה שהציגה נתון ביחידה, בתקופה, בבסיס שטח או במע\"מ שאינם כבמקור.",
  absent_from_source: "הוסרה טענה שהמקורות שנבדקו אינם מציינים.",
  not_checked: "הוסרה טענה שלא ניתן היה לבדוק מול המקורות; היא לא נמצאה שגויה.",
};

function removal(kind: string, componentId: string | null = null): Json {
  return { failure_kind: kind, component: componentId, text: REMOVAL_SENTENCE[kind] };
}

/** A round-7 answer: the server's gap paragraph ends the markdown, with its groups, components and removals. */
function answerWith(extra: Json = {}, verification: Json = {}): Json {
  const doc = { document_id: DOC, title: TITLE };
  const markdown = `השווי למ״ר בשומה הוא 9,500 ₪ [S1].\n\n${GAP_LINE}`;
  return {
    kind: "rag", status: "partial", markdown, claims: [], clarification: null, missing: null,
    sources: [source("S1")], measurements: [], computations: [], values: [], assumptions: [], documents: [doc],
    verification: { judged: true, judge_status: "ok", removed: 0, partial: 0, annotated: 0, correctness: "verified",
      request_mismatch: false, removals: [],
      completeness: { status: "partial", requirements: 2, missing: [
        { id: "N2", text: "שיעור ההיוון", status: "not_answered", reason: "not_located", parent: "", conditional: false },
      ] }, ...verification },
    searches: ["שיעור היוון"], coverage: [],
    ledger: { cited: [doc], scope_kind: "focused", scope_query: "", complete: true },
    components: [
      component("N1", "השווי למ״ר", "full", { units: [0], related: ["S1"] }),
      component("N2", "שיעור ההיוון", "not_answered", { limitation: "not_located",
        limitation_text: "לא אותר בחיפושים שבוצעו", stated: true, gap: GAP_LINE, searched: true }),
    ],
    gaps: [{ reason: "not_located", reason_text: "לא אותר בחיפושים שבוצעו", components: ["N2"], texts: ["שיעור ההיוון"],
      text: GAP_LINE }],
    ...extra,
  };
}

function message(id: string, role: "user" | "assistant", content: string, answer: Json | null, extra: Json = {}): Json {
  return { id, role, content, status: "done", error: null, progress: [], answer, reply_to: role === "assistant" ? "m1" : null,
    client_id: null, created_at: NOW, stale: false, cancel_requested: false, usage: [], ...extra };
}

function thread(conversationId: string, items: Json[]) {
  return {
    conversation: { id: conversationId, title: "בדיקת פערים והסרות", updated_at: NOW, created_at: NOW, archived: false,
      engine: "rag" },
    has_more: false, messages: items,
  };
}

/** The diagnostics of m2: each removal's draft, factual reason and the sources it was checked against. */
function diagnostics(removed: Json[]): Json {
  return { message_id: "m2", rounds: [], removed, resolution: null, limits_hit: [] };
}

function decision(kind: string, extra: Json = {}): Json {
  return {
    text: DRAFT, reason: REASON, severity: "error", kind: "unit", failure_kind: kind, check: "judge", component: "N2",
    checked_ids: ["S9"], repair_attempted: true,
    sources: [{ ...source("S9"), type: "sources" }], ...extra,
  };
}

/** Logs in, serves `answer` as the reply to one question, and opens the conversation. */
async function openAnswer(page: Page, answer: Json): Promise<{ id: string; reply: Locator }> {
  await login(page, USERS.adminB);
  const { id } = await (await page.request.post("/api/chat/conversations")).json();
  await page.route(`**/api/chat/conversations/${id}/messages*`, (route) =>
    route.fulfill({ json: thread(id, [message("m1", "user", "מה השווי למ״ר ומה שיעור ההיוון?", null),
      message("m2", "assistant", String(answer.markdown), answer)]) }));
  await page.route("**/api/documents/**/pages/*/image*", (route) =>
    route.fulfill({ status: 200, contentType: "image/png", body: PNG,
      headers: { "X-Display-Width": "600", "X-Display-Height": "900", "X-Render-Scale": "1", "X-Scale-Tier": "normal",
        "X-Reading-State": "current", "Cache-Control": "private, no-store" } }));
  await page.route("**/api/documents/**/blocks*", (route) =>
    route.fulfill({ json: { document_id: DOC, version_id: VER, title: TITLE, is_current: true,
      mime_type: "application/pdf", total: 1, file_url: `${BASE}/file`, reading_id: READING,
      blocks: [{ index: 0, kind: "paragraph", section: "4. שיטת ההיוון", section_path: ["4. שיטת ההיוון"],
        paragraph_no: null, page: 3, media: null, source: "text", status: "read", note: null,
        text: "שיעור ההיוון החודשי הוא 0.6%.", bbox: [60, 100, 540, 160], method: "text_layer",
        reader_version: "pdf-test", content_hash: null, original_text: null, page_url: `${BASE}/pages/3/image`,
        region_url: `${BASE}/regions/0/image?reading_id=${READING}` }] } }));
  await page.goto(`/chat?c=${id}`);
  const reply = assistantMessages(page).last();
  await expect(reply).toBeVisible();
  return { id, reply };
}

function count(text: string, part: string): number {
  return text.split(part).length - 1;
}

async function openDetails(reply: Locator): Promise<void> {
  await reply.locator("details.msg-details > summary").click();
  await expect(reply.locator("details.msg-details")).toHaveAttribute("open", "");
}

// --- the gap summary above the details --------------------------------------------------------------------------

test.describe("gap summary", () => {
  test("a partial answer shows the gap paragraph once; the quality line shows the status without the reasons", async ({
    page,
  }) => {
    const { id, reply } = await openAnswer(page, answerWith());
    await expect(reply.locator(".body")).toContainText(GAP_LINE);
    const line = reply.getByTestId("completeness");
    await expect(line).toHaveAttribute("data-status", "partial");
    await expect(line).toContainText("התשובה חלקית");
    await expect(line).not.toContainText("שיעור ההיוון");
    await expect(line).not.toContainText("לא אותר");
    await expect(reply.getByTestId("completeness-gap")).toHaveCount(0);
    expect(count(await reply.innerText(), GAP_LINE)).toBe(1);
    // the details stay collapsed by default; opened, they do not repeat the gap paragraph either
    await expect(reply.locator("details.msg-details")).not.toHaveAttribute("open", "");
    await openDetails(reply);
    expect(count(await reply.innerText(), GAP_LINE)).toBe(1);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a complete answer shows neither a gap paragraph nor a completeness line", async ({ page }) => {
    const answer = answerWith({
      status: "answered", markdown: "השווי למ״ר בשומה הוא 9,500 ₪ [S1].", gaps: [],
      components: [component("N1", "השווי למ״ר", "full", { units: [0], related: ["S1"] })],
    }, { completeness: { status: "full", requirements: 1, missing: [] } });
    const { id, reply } = await openAnswer(page, answer);
    await expect(reply.locator(".body")).toContainText("9,500");
    await expect(reply.getByTestId("completeness")).toHaveCount(0);
    await expect(reply.getByTestId("removal-notice")).toHaveCount(0);
    expect(count(await reply.innerText(), GAP_LINE)).toBe(0);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a result kept with the server's conditional qualifier gets the conditional line, not the generic one", async ({
    page,
  }) => {
    const answer = answerWith({
      status: "answered", markdown: "סך העלויות הוא 20,220,000 ₪ (תוצאה מותנית: הערכים V1, V2 אינם ודאיים) [C1].",
      gaps: [], components: [component("N1", "סך העלויות", "full", { units: [0], related: ["C1"] })],
    }, { correctness: "partial", conditional: 1, completeness: { status: "full", requirements: 1, missing: [] } });
    const { id, reply } = await openAnswer(page, answer);
    const line = reply.getByTestId("correctness");
    await expect(line).toContainText("מותנות");
    await expect(line).not.toContainText("הוסרו");
    await expect(reply.getByTestId("removal-notice")).toHaveCount(0);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });
});

// --- removals -----------------------------------------------------------------------------------------------------

test.describe("removals", () => {
  test("two removals show one removal notice outside the collapsed details, with no draft text", async ({ page }) => {
    const answer = answerWith({}, { removed: 2, correctness: "partial",
      removals: [removal("wrong_unit", "N2"), removal("wrong_unit")] });
    const { id, reply } = await openAnswer(page, answer);
    const notice = reply.getByTestId("removal-notice");
    await expect(notice).toBeVisible();
    await expect(notice).toContainText("2");
    await expect(notice).toContainText("יחידה");
    await expect(notice).not.toContainText(DRAFT);
    // claims only removed: the notice replaces the generic correctness line
    await expect(reply.getByTestId("correctness")).toHaveCount(0);
    await expect(reply.locator("details.msg-details")).not.toHaveAttribute("open", "");
    // the notice is above the details, not inside them
    await expect(reply.locator("details.msg-details").getByTestId("removal-notice")).toHaveCount(0);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("expanding a removal shows its kind, the reason and the draft under an unverified label; its source opens the viewer", async ({
    page,
  }) => {
    const answer = answerWith({}, { removed: 1, correctness: "partial", removals: [removal("wrong_unit", "N2")] });
    const { id, reply } = await openAnswer(page, answer);
    let release: () => void = () => {};
    const held = new Promise<void>((r) => {
      release = r;
    });
    let calls = 0;
    await page.route("**/api/chat/messages/m2/diagnostics", async (route) => {
      calls += 1;
      await held;
      await route.fulfill({ json: diagnostics([decision("wrong_unit")]) });
    });
    await openDetails(reply);
    const item = reply.getByTestId("removal").first();
    await expect(item).toHaveAttribute("data-kind", "wrong_unit");
    await expect(item.getByTestId("removal-kind")).toContainText("יחידה");
    await expect(item).toContainText(REMOVAL_SENTENCE.wrong_unit);
    await expect(item).not.toContainText(DRAFT);
    // keyboard: the disclosure is a button
    const toggle = item.getByRole("button", { name: /פירוט/ });
    await expect(toggle).toHaveAttribute("aria-expanded", "false");
    await toggle.focus();
    await page.keyboard.press("Enter");
    await expect(toggle).toHaveAttribute("aria-expanded", "true");
    // while loading: busy, and the kind stays visible
    await expect(item.getByTestId("removal-loading")).toBeVisible();
    await expect(item.getByTestId("removal-kind")).toBeVisible();
    release();
    const draft = item.getByTestId("removal-draft");
    await expect(draft).toContainText("טיוטה שלא אומתה");
    await expect(draft).toContainText(DRAFT);
    await expect(item.getByTestId("removal-reason")).toContainText(REASON);
    await expect(item.getByTestId("removal-loading")).toHaveCount(0);
    expect(calls).toBe(1);
    // never inline with the answer
    await expect(reply.locator(".body")).not.toContainText(DRAFT);
    // the source the route returned opens the viewer at the cited place
    await item.getByTestId("removal-source").first().click();
    const viewer = page.getByRole("complementary", { name: "תצוגת מקור" });
    await expect(viewer).toBeVisible();
    await expect(viewer.getByTestId("viewer-title")).toContainText(TITLE);
    await expect(viewer.getByTestId("viewer-page")).toContainText("עמוד 3 בקובץ");
    await page.keyboard.press("Escape");
    await expect(viewer).toHaveCount(0);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a diagnostics request that fails shows the retry message and the kind, and no draft text", async ({ page }) => {
    const answer = answerWith({}, { removed: 1, correctness: "partial", removals: [removal("wrong_unit", "N2")] });
    const { id, reply } = await openAnswer(page, answer);
    let fail = true;
    await page.route("**/api/chat/messages/m2/diagnostics", (route) =>
      fail ? route.fulfill({ status: 500, json: { detail: "boom" } })
        : route.fulfill({ json: diagnostics([decision("wrong_unit")]) }));
    await openDetails(reply);
    const item = reply.getByTestId("removal").first();
    await item.getByRole("button", { name: /פירוט/ }).click();
    const error = item.getByTestId("removal-error");
    await expect(error).toBeVisible();
    await expect(item.getByTestId("removal-kind")).toContainText("יחידה");
    await expect(item.getByTestId("removal-draft")).toHaveCount(0);
    await expect(reply).not.toContainText(DRAFT);
    // a retry that succeeds shows the detail
    fail = false;
    await error.getByRole("button", { name: "ניסיון חוזר" }).click();
    await expect(item.getByTestId("removal-draft")).toContainText(DRAFT);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a diagnostics 404 shows the kind only, and a collapse and reopen never shows an earlier draft", async ({ page }) => {
    const answer = answerWith({}, { removed: 1, correctness: "partial", removals: [removal("wrong_unit", "N2")] });
    const { id, reply } = await openAnswer(page, answer);
    let gone = false;
    await page.route("**/api/chat/messages/m2/diagnostics", (route) =>
      gone ? route.fulfill({ status: 404, json: { detail: "לא נמצא" } })
        : route.fulfill({ json: diagnostics([decision("wrong_unit")]) }));
    await openDetails(reply);
    const item = reply.getByTestId("removal").first();
    const toggle = item.getByRole("button", { name: /פירוט/ });
    await toggle.click();
    await expect(item.getByTestId("removal-draft")).toContainText(DRAFT);
    await toggle.click(); // collapse
    await expect(item.getByTestId("removal-draft")).toHaveCount(0);
    gone = true; // a document behind the answer is no longer visible
    await toggle.click();
    await expect(item.getByTestId("removal-unavailable")).toBeVisible();
    await expect(item.getByTestId("removal-kind")).toContainText("יחידה");
    await expect(item.getByTestId("removal-draft")).toHaveCount(0);
    await expect(item.getByTestId("removal-error")).toHaveCount(0);
    await expect(item.getByTestId("removal-source")).toHaveCount(0);
    await expect(reply).not.toContainText(DRAFT);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });

  test("a not-checked removal reads as could not be checked; a not-relevant component is not listed", async ({ page }) => {
    const answer = answerWith({
      components: [
        component("N1", "השווי למ״ר", "full", { units: [0], related: ["S1"] }),
        component("N2", "שיעור ההיוון", "not_answered", { limitation: "not_located",
          limitation_text: "לא אותר בחיפושים שבוצעו", stated: true, gap: GAP_LINE }),
        component("N3", "שטח הנכס", "partial", { limitation: "region_not_read",
          limitation_text: "האזור הרלוונטי לא נקרא במלואו", removed_units: [2], removal_kinds: ["not_checked"] }),
        component("N4", "הנחת תשואה של המשתמש", "not_relevant", { kind: "assumption" }),
      ],
    }, { removed: 1, correctness: "partial", removals: [removal("not_checked", "N3")],
      completeness: { status: "partial", requirements: 3, missing: [
        { id: "N2", text: "שיעור ההיוון", status: "not_answered", reason: "not_located", parent: "", conditional: false },
        { id: "N3", text: "שטח הנכס", status: "partial", reason: "region_not_read", parent: "", conditional: false },
      ] } });
    const { id, reply } = await openAnswer(page, answer);
    const notice = reply.getByTestId("removal-notice");
    await expect(notice).toContainText("לא ניתן היה לבדוק");
    await expect(notice).not.toContainText("שגוי");
    await openDetails(reply);
    const item = reply.getByTestId("removal").first();
    await expect(item.getByTestId("removal-kind")).toContainText("לא ניתן היה לבדוק");
    await expect(item.getByTestId("removal-kind")).not.toContainText("שגוי");
    const list = reply.getByTestId("components");
    await expect(list.getByTestId("component")).toHaveCount(2);
    const missing = list.locator('[data-testid="component"][data-id="N2"]');
    await expect(missing).toContainText("שיעור ההיוון");
    await expect(missing).toContainText("לא ניתן");
    await expect(missing).toContainText("לא אותר בחיפושים שבוצעו");
    const partial = list.locator('[data-testid="component"][data-id="N3"]');
    await expect(partial).toContainText("ניתן בחלקו");
    await expect(partial).toContainText("האזור הרלוונטי לא נקרא במלואו");
    await expect(partial).toContainText("לא ניתן היה לבדוק");
    await expect(list.getByTestId("components-full")).toContainText("1");
    await expect(list).not.toContainText("הנחת תשואה של המשתמש");
    await page.request.delete(`/api/chat/conversations/${id}`);
  });
});

// --- clarification ------------------------------------------------------------------------------------------------

test.describe("clarification", () => {
  test("a clarification answer shows the clarification lead, and a reply in the normal input continues the task", async ({
    page,
  }) => {
    const question = "לפי איזה שיעור היוון לחשב את השווי?";
    const answer = answerWith({
      status: "clarification", markdown: `נמצא שווי למ״ר של 9,500 ₪ [S1].\n\n${question}`, clarification: question,
      missing: "שיעור ההיוון לחישוב", gaps: [],
      components: [component("N1", "השווי למ״ר", "full", { units: [0], related: ["S1"] }),
        component("N2", "שיעור ההיוון לחישוב", "needs_clarification", { kind: "clarification",
          limitation: "detail_missing", limitation_text: "חסר פרט בבקשה" })],
    }, { completeness: { status: "partial", requirements: 2, missing: [
      { id: "N2", text: "שיעור ההיוון לחישוב", status: "needs_clarification", reason: "detail_missing", parent: "",
        conditional: false }] } });
    await login(page, USERS.adminB);
    const { id } = await (await page.request.post("/api/chat/conversations")).json();
    const items = [message("m1", "user", "חשב את השווי המהוון", null), message("m2", "assistant", String(answer.markdown), answer)];
    const posted: Json[] = [];
    const follow = answerWith({ status: "answered", markdown: "לפי שיעור היוון של 6% השווי המהוון הוא 8,962 ₪ [S1].",
      gaps: [], components: [] }, { completeness: { status: "full", requirements: 1, missing: [] } });
    await page.route(`**/api/chat/conversations/${id}/messages*`, async (route: Route) => {
      if (route.request().method() === "POST") {
        const body = route.request().postDataJSON() as Json;
        posted.push(body);
        const user = message("m3", "user", String(body.content), null, { client_id: body.client_id });
        const pending = message("m4", "assistant", "", null, { status: "running", reply_to: "m3",
          progress: [{ step: "agent", label: "מחשב" }] });
        items.push(user, pending);
        return route.fulfill({ json: { user, assistant: pending } });
      }
      return route.fulfill({ json: thread(id, items) });
    });
    await page.route("**/api/chat/messages/m4", (route) =>
      route.fulfill({ json: message("m4", "assistant", String(follow.markdown), follow, { reply_to: "m3" }) }));
    await page.goto(`/chat?c=${id}`);
    const reply = assistantMessages(page).last();
    const lead = reply.getByTestId("clarification-lead");
    await expect(lead).toBeVisible();
    await expect(lead).toContainText("שיעור ההיוון לחישוב");
    await expect(reply.getByTestId("completeness")).toHaveCount(0);
    await expect(reply).not.toContainText("תשובה חלקית");
    await expect(reply).not.toContainText("התשובה חלקית");
    await composer(page).fill("6% לשנה");
    await composer(page).press("Enter");
    await expect(assistantMessages(page)).toHaveCount(2);
    await expect(assistantMessages(page).last().locator(".body")).toContainText("8,962", { timeout: 15_000 });
    expect(posted).toHaveLength(1);
    expect(posted[0].content).toBe("6% לשנה");
    await page.request.delete(`/api/chat/conversations/${id}`);
  });
});

// --- older answers ------------------------------------------------------------------------------------------------

test.describe("older answers", () => {
  test("an answer without component outcomes renders as before", async ({ page }) => {
    const answer = answerWith({
      markdown: "השווי למ״ר בשומה הוא 9,500 ₪ [S1].", components: undefined, gaps: undefined,
      ledger: { cited: [{ document_id: DOC, title: TITLE }], scope_kind: "focused", scope_query: "", complete: true,
        requirements: [
          { id: "Q1", text: "השווי למ״ר", calculation: false, status: "full", stated: false, units: [0], related: ["S1"],
            reason: "", limitation: null, limitation_text: null },
          { id: "Q2", text: "שיעור ההיוון", calculation: false, status: "missing", stated: false, units: [], related: [],
            reason: "", limitation: "not_found", limitation_text: "לא נמצא בחיפוש" },
        ] },
    }, {
      removed: 1, correctness: "partial", removals: undefined,
      completeness: { status: "partial", requirements: 2, missing: [{ id: "Q2", text: "שיעור ההיוון", status: "missing",
        reason: "not_found", reason_text: "לא נמצא בחיפוש" }] },
    });
    const { id, reply } = await openAnswer(page, answer);
    await expect(reply.getByTestId("completeness")).toContainText("שיעור ההיוון — לא נמצא בחיפוש");
    await expect(reply.getByTestId("removal-notice")).toHaveCount(0);
    await expect(reply.getByTestId("clarification-lead")).toHaveCount(0);
    await expect(reply.getByTestId("correctness")).toHaveAttribute("data-status", "partial");
    await openDetails(reply);
    await expect(reply.getByTestId("requirements")).toContainText("שיעור ההיוון — חסר · לא נמצא בחיפוש");
    await expect(reply.getByTestId("verification")).toContainText("הוסרו 1 טענות");
    await expect(reply.getByTestId("components")).toHaveCount(0);
    await expect(reply.getByTestId("removal")).toHaveCount(0);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });
});

// --- mobile -------------------------------------------------------------------------------------------------------

test.describe("gaps and removals (mobile)", () => {
  test.use({ viewport: { width: 375, height: 812 }, hasTouch: true });

  test("the removal detail opens inside the details without a horizontal scroll", async ({ page }) => {
    const answer = answerWith({}, { removed: 1, correctness: "partial", removals: [removal("absent_from_source", "N2")] });
    const { id, reply } = await openAnswer(page, answer);
    await page.route("**/api/chat/messages/m2/diagnostics", (route) =>
      route.fulfill({ json: diagnostics([decision("absent_from_source")]) }));
    await expect(reply.getByTestId("removal-notice")).toBeVisible();
    await openDetails(reply);
    const item = reply.getByTestId("removal").first();
    await item.getByRole("button", { name: /פירוט/ }).click();
    await expect(item.getByTestId("removal-draft")).toContainText(DRAFT);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    expect(overflow).toBeLessThanOrEqual(0);
    await page.request.delete(`/api/chat/conversations/${id}`);
  });
});
