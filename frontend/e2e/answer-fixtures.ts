import { createHash } from "node:crypto";
import { expect, type Locator, type Page } from "@playwright/test";
import { assistantMessages, login, USERS } from "./helpers";

// Synthetic answers for the browser tests of what verification did to an answer (round 7 U8, round 8 U8): the payload
// builders (an answer, its summary rows, the log entries the diagnostics route returns), one conversation served with
// them in office B, and the fixed cited document. Every name and value is invented. Not a test file (no `.spec`), so
// the specs that share it do not register each other's tests.

export const NOW = new Date().toISOString();
export const DOC = "00000000-0000-0000-0000-0000000000d7";
export const VER = "00000000-0000-0000-0000-0000000000a7";
export const BASE = `/api/documents/${DOC}/versions/${VER}`;
export const TITLE = "שומה סינתטית לבדיקת פערים";
export const READING = "reading-synthetic-7";
export const GAP_LINE = "לא אותר בחיפושים שבוצעו: שיעור ההיוון.";
export const DRAFT = "שיעור ההיוון בשומה הוא 7% לשנה.";
export const REASON = "המקור מציין שיעור חודשי, לא שנתי.";

// a 1×1 white PNG: the viewer only needs an image that decodes
export const PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC",
  "base64",
);

export type Json = Record<string, unknown>;

export function anchor(): Json {
  return {
    v: 1, precision: "span", precision_label: "המקום המצוטט מסומן בעמוד", region: null, degraded: null,
    document_id: DOC, version_id: VER, reading_id: READING, block_start: 0, block_end: 0,
    pages: [{ page: 3, printed_label: null, width: 600, height: 900, rects: [[0.1, 0.2, 0.6, 0.25]], focus: [] }],
    location: { title: TITLE, label: "עמוד 3, סעיף «4. שיטת ההיוון»", page: 3, page_end: null, printed_page: null,
      section: "4. שיטת ההיוון" },
    table: null, structured: null, truncated: false,
  };
}

/** A document anchor at another place of the same fixed document (round 14 U8: a value's place and its context's place
 * differ): `label` is the readable location the viewer's header shows. */
export function anchorAt(label: string, page: number, block: number): Json {
  const a = anchor() as { pages: Json[]; location: Json } & Json;
  return { ...a, block_start: block, block_end: block, pages: [{ ...a.pages[0], page }],
    location: { ...a.location, label, page, section: null } };
}

export function source(id: string, extra: Json = {}): Json {
  return {
    id, document_id: DOC, version_id: VER, title: TITLE, section: "4. שיטת ההיוון", location: "עמוד 3", kind: "text",
    text: "שיעור ההיוון החודשי הוא 0.6%.", block_start: 0, block_end: 0, table_index: null, page_list: [3],
    chunk_id: null, reading_id: READING, status: "complete", anchor: anchor(), ...extra,
  };
}

export function component(id: string, text: string, status: string, extra: Json = {}): Json {
  return {
    id, text, kind: "information", aspect: "", parent: "", conditional: false, subject: "", status,
    evidence_state: null, limitation: null, limitation_text: null, stated: false, gap: null, units: [],
    removed_units: [], absence_units: [], related: [], place: null, document: null, document_id: null,
    document_title: null, searched: null, claimed: null, detail: "", removal_kinds: [], ...extra,
  };
}

export const REMOVAL_SENTENCE: Record<string, string> = {
  wrong_unit: "הוסרה טענה שהציגה נתון ביחידה, בתקופה, בבסיס שטח או במע\"מ שאינם כבמקור.",
  absent_from_source: "הוסרה טענה שהמקורות שנבדקו אינם מציינים.",
  not_checked: "הוסרה טענה שלא ניתן היה לבדוק מול המקורות; היא לא נמצאה שגויה.",
};

/** A claim id as the engine writes it: "k" and 12 hex digits (round 8 U3). */
export function cid(n: number): string {
  return `k${n.toString(16).padStart(12, "0")}`;
}

/** A summary row on the normal path (round 8 U7): the kind's fixed sentence, the claim's id and its action; a cut's
 * row also carries its marker's ordinal. `legacy` builds a row stored before round 8 (no id, no action). */
export function removal(kind: string, componentId: string | null = null, extra: Json = {}): Json {
  return { failure_kind: kind, component: componentId, text: REMOVAL_SENTENCE[kind], claim_id: cid(1), action: "removed",
    ...extra };
}

export function legacyRemoval(kind: string, componentId: string | null = null): Json {
  return { failure_kind: kind, component: componentId, text: REMOVAL_SENTENCE[kind] };
}

/** How many claims each action touched, from the rows (and `more` for the actions that have no row). */
export function actionsFor(rows: Json[], more: Json = {}): Json {
  const n = (a: string) => rows.filter((r) => r.action === a).length;
  return { removed: n("removed"), cut: n("cut"), rewritten: 0, withheld: n("withheld"), qualified: 0, cited: 0, ...more };
}

/** A round-7 answer: the server's gap paragraph ends the markdown, with its groups, components and removals. The
 * payload is round 8's: the digest of the markdown and the counts of what each action touched (from the rows, unless
 * `verification` gives them or leaves them out with `actions: undefined`). */
export function answerWith(extra: Json = {}, verification: Json = {}): Json {
  const doc = { document_id: DOC, title: TITLE };
  const markdown = String(extra.markdown ?? `השווי למ״ר בשומה הוא 9,500 ₪ [S1].\n\n${GAP_LINE}`);
  const rows = (verification.removals as Json[] | undefined) ?? [];
  return {
    kind: "rag", status: "partial", markdown, claims: [], clarification: null, missing: null,
    sources: [source("S1")], measurements: [], computations: [], values: [], assumptions: [], documents: [doc],
    verification: { judged: true, judge_status: "ok", removed: 0, partial: 0, annotated: 0, correctness: "verified",
      request_mismatch: false, removals: [], final_digest: createHash("sha256").update(markdown).digest("hex"),
      actions: actionsFor(rows),
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

export function message(id: string, role: "user" | "assistant", content: string, answer: Json | null, extra: Json = {}): Json {
  return { id, role, content, status: "done", error: null, progress: [], answer, reply_to: role === "assistant" ? "m1" : null,
    client_id: null, created_at: NOW, stale: false, cancel_requested: false, usage: [], ...extra };
}

export function thread(conversationId: string, items: Json[]) {
  return {
    conversation: { id: conversationId, title: "בדיקת פערים והסרות", updated_at: NOW, created_at: NOW, archived: false,
      engine: "rag" },
    has_more: false, messages: items,
  };
}

/** The diagnostics of m2: each removal's draft, factual reason and the sources it was checked against. */
export function diagnostics(removed: Json[]): Json {
  return { message_id: "m2", rounds: [], removed, resolution: null, limits_hit: [] };
}

/** A log entry (round 8 U7): the claim's id and action, its draft (`text`), what remains of it, the verdict, the
 * reason, the sources checked and the final check's result. */
export function decision(kind: string, extra: Json = {}): Json {
  return {
    text: DRAFT, reason: REASON, severity: "error", kind: "unit", failure_kind: kind, check: "judge", component: "N2",
    checked_ids: ["S9"], repair_attempted: true, claim_id: cid(1), action: "removed", remaining: "", verdict: "unsupported",
    cause: "", sources_checked: ["S9"], rereads: [], post_check: "n/a",
    sources: [{ ...source("S9"), type: "sources" }], ...extra,
  };
}

/** Logs in, serves `answer` as the reply to one question, and opens the conversation. `messageExtra` sets keys of the
 * assistant message itself (round 14: `can_view_diagnostics`). */
export async function openAnswer(page: Page, answer: Json, messageExtra: Json = {}): Promise<{ id: string; reply: Locator }> {
  await login(page, USERS.adminB);
  const { id } = await (await page.request.post("/api/chat/conversations")).json();
  await page.route(`**/api/chat/conversations/${id}/messages*`, (route) =>
    route.fulfill({ json: thread(id, [message("m1", "user", "מה השווי למ״ר ומה שיעור ההיוון?", null),
      message("m2", "assistant", String(answer.markdown), answer, messageExtra)]) }));
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

export function count(text: string, part: string): number {
  return text.split(part).length - 1;
}

export async function openDetails(reply: Locator): Promise<void> {
  await reply.locator("details.msg-details > summary").click();
  await expect(reply.locator("details.msg-details")).toHaveAttribute("open", "");
}


/** The removal note the server appends (round 13 U12): `body` followed by the note's blockquote lines, with the
 * registry span that marks the note (offsets in code points, as the server writes them). */
export function withRemovalNote(body: string, lines: string[]): { markdown: string; server_spans: Json[] } {
  const note = "\n\n" + lines.map((l) => `> ${l}`).join("\n>\n");
  const start = Array.from(body).length;
  return {
    markdown: body + note,
    server_spans: [{ start, end: start + Array.from(note).length, kind: "removal_note" }],
  };
}


// --- round 14 U8: row states, context, the reconciled adjustment and the partial marks ------------------------------

/** The server's literal "verified in part" mark, with its leading space (compose.PARTIAL_MARK). */
export const PARTIAL_MARK = " *(אומת חלקית)*";

/** A row's `state` (round 14 U6, KTD8). `component` overrides the component part (default: stated, no context). */
export function rowState(opts: { component?: Json; support?: "cited" | "history"; comparison?: Json; reason?: string;
  claim?: Json | null } = {}): Json {
  const component = { status: "stated", reason: null, written: "מקדם", part: "sentence", tier: "own", context: null,
    ...opts.component };
  return {
    reading: "verified", component, claim: opts.claim === undefined ? { result: "supported", reason: null } : opts.claim,
    support: opts.support ?? "cited", comparison: opts.comparison ?? { status: "single", with: [] },
    reason: opts.reason ?? (component.reason as string | null) ?? "single",
  };
}

/** The component part of a state whose component was read from elsewhere than the value's own sentence. */
export function contextComponent(location: string, quote: string | null, at: Json | null): Json {
  return { status: "stated", reason: null, written: "הפחתה בשל מגבלות בנייה", part: "paragraph", tier: "context",
    context: { location, quote, anchor: at } };
}

/** The markdown of `body` plus the server's partial marks, and the `partial_mark` spans that locate them (offsets in code
 * points, as Python counts them): `marks` is each mark's claim ids, in the text's order. `parts` are the text pieces; a mark
 * follows each piece but the last. */
export function withPartialMarks(parts: string[], marks: string[][]): { markdown: string; server_spans: Json[] } {
  let markdown = "";
  const server_spans: Json[] = [];
  parts.forEach((part, i) => {
    markdown += part;
    if (i < marks.length) {
      const start = Array.from(markdown).length;
      markdown += PARTIAL_MARK;
      server_spans.push({ start, end: start + Array.from(PARTIAL_MARK).length, kind: "partial_mark", ordinal: i + 1,
        claim_ids: marks[i] });
    }
  });
  return { markdown, server_spans };
}

export const PARTIAL_SENTENCE = "אומת חלקית: חלק מהטענה אינו מצוין במקורות שנבדקו.";
export const PARTIAL_SENTENCE_2 = "אומת חלקית: ההקשר שמגדיר חלק מהטענה לא נקרא.";

/** `verification.partials` rows (round 14 U7): one per mark, in the text's order. */
export function partialRows(marks: { claims: string[]; kind?: string; text?: string }[]): Json[] {
  return marks.map((m, i) => ({ ordinal: i + 1, claim_id: m.claims[0], claim_ids: m.claims,
    failure_kind: m.kind ?? "absent_from_source", check: "judge", text: m.text ?? PARTIAL_SENTENCE }));
}

export const SUPPORTED = "שווי הנכס בשומה הוא 9,500 ₪ למ״ר";
export const NOT_VERIFIED = "ושיעור ההיוון שלו הוא 7%";
export const WHY_CHECK = "judge";

/** A diagnostics entry a partial mark opens (round 14 U7): action `qualified`, with the structured detail. */
export function partialDecision(claimId: string, extra: Json = {}): Json {
  return decision("absent_from_source", {
    claim_id: claimId, action: "qualified", text: `${SUPPORTED} ${NOT_VERIFIED}`, remaining: SUPPORTED, verdict: "partial",
    reason: "אומת חלקית: חלק מהטענה אינו מצוין במקורות שנבדקו.", repair_attempted: false, check: WHY_CHECK,
    supported: SUPPORTED, not_verified: NOT_VERIFIED, why: { failure_kind: "absent_from_source", check: WHY_CHECK },
    checked: { sources: ["S1", "S2"], rereads: [{ source: "S2", scope: "next_page", outcome: "not_found" }] },
    sources_checked: ["S1", "S2"], sources: [{ ...source("S1"), type: "sources" }],
    ...extra,
  });
}

/** An answer with partial marks: `marks` lists the claims of each mark; `verification` has the matching `partials` rows and
 * the count of qualified claims. The first part of the text carries a citation so that the details have a source. */
export function partialAnswer(marks: string[][], extra: Json = {}, kinds: { kind?: string; text?: string }[] = []): Json {
  const parts = [`${SUPPORTED} [S1]`, `. ושיעור ההיוון הוא 7% [S1]`, "."].slice(0, marks.length + 1);
  const { markdown, server_spans } = withPartialMarks(parts, marks);
  const rows = marks.map((claims, i) => ({ claims, ...kinds[i] }));
  return answerWith({ markdown, server_spans, status: "answered", gaps: [], components: [], ...extra },
    { partial: marks.length, correctness: "partial", partials: partialRows(rows),
      actions: { removed: 0, cut: 0, rewritten: 0, withheld: 0, qualified: marks.length, cited: 0 },
      completeness: { status: "full", requirements: 1, missing: [] } });
}

/** A stored computation (C#) the derived reduction points at: `1 − V1`. */
export function derivedComputation(id: string, label: string): Json {
  return {
    id, label, expression: "1 - V1", formula: `1 − ${label}`, value: "0.1", display: { value: "0.1", percent: "10%" },
    unit: "", kind: "ratio", result_kind: "derived", result_kind_label: "נגזר מערכים מצוטטים",
    inputs: [{ id: "V1", label, kind: "value", value: "0.9", display: "0.9", value_text: "0.9", source_id: "S1",
      certainty: "verified" }],
    assumptions: [], sources: ["S1"], documents: 1, conditional: false, conditions: [], vat: null, justification: null,
    note: "", reproduces: null, n: null, operation: "1 - V1", result: "0.1",
    anchor: { v: 1, precision: "computed", precision_label: "תוצאה מחושבת", inputs: ["V1"] },
  };
}
