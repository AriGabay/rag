// Types of the conversational chat API (/api/chat). Kept apart from the earlier /api/ask types.

import type { ChangeAction, ValueStatus } from "./format";
import type { SourceKind } from "./types";

export interface ChatConversation {
  id: string;
  title: string;
  updated_at: string;
  created_at: string;
  archived: boolean;
  engine: "legacy" | "rag";
  /** The time-preference switch (round 11 U10); absent only on a payload from before round 11, which reads as off. */
  prefer_recent?: boolean;
}

export interface ChatProgressStep {
  step: string;
  label: string;
  /** When the event happened (epoch seconds); absent on steps stored before round 9. Not shown. */
  at?: number;
  /** A finished tool call: its label says what it found. Absent on a start event. */
  done?: boolean;
}

/** Where a citation points (backend `app/chat/anchors.py`), snapshotted into the answer when it was stored, so an old
 * conversation keeps its original place after a reprocess. Absent on answers stored before anchors: they open as
 * before. Rectangles are fractions of the rendered page (origin top-left), never model-supplied positions. */
export type ChatAnchorPrecision = "span" | "cell" | "block" | "region" | "page" | "structured";

/** Why an anchor is less precise than its source asked for. */
export type ChatAnchorDegraded =
  | "reading_changed"
  | "no_geometry"
  | "no_cell_box"
  | "not_located"
  | "unavailable"
  | "anchor_lost";

/** [x0, y0, x1, y1] as fractions of the page's width and height. */
export type ChatAnchorRect = [number, number, number, number];

export interface ChatAnchorPage {
  /** File page, 1-based. */
  page: number;
  /** The page number printed on the page, when detected. */
  printed_label: string | null;
  /** Size of the rendered page in points (null when not stored). */
  width: number | null;
  height: number | null;
  rects: ChatAnchorRect[];
  /** The cited number inside the rects (span precision). */
  focus: ChatAnchorRect[];
}

export interface ChatAnchorTable {
  table_index: number;
  title: string | null;
  row_label: string | null;
  row_number: number | null;
  column_header: string | null;
  column_number: number | null;
  unit_note: string | null;
  /** How the table was read when not from the text layer: "vision", "ocr", "emf". */
  source: string | null;
  /** The column header cell (or the header row) on the page, when its box is stored. */
  header: { page: number; rects: ChatAnchorRect[] } | null;
  notes: { text: string; page?: number }[];
}

export interface ChatAnchorStructured {
  section_path: string[];
  paragraph_no: number | null;
  paragraph_end: number | null;
  label: string | null;
  /** The cited paragraph or cell text (bounded). */
  text: string;
  /** [start, end) of the quoted words in `text`. */
  highlight: [number, number] | null;
  cell: {
    table_index: number;
    row_number: number;
    column_number: number;
    row_label: string | null;
    column_header: string | null;
    text: string;
  } | null;
}

/** The appraisal unit a source, value or citation is in, in a file that holds more than one (absent for a file with
 * one unit). `id` is stable across turns: version, reading and unit number. */
export interface ChatUnit {
  id: string;
  no: number;
  /** The title the document states for the unit; null when it states none. */
  title: string | null;
  label: string;
  kind: string;
  /** First and last page of the unit in the file; empty when unknown. */
  pages: number[];
}

/** What identifies the standard (or other professional document) a citation is from, each part null where the document
 * states nothing (backend `Source.standard`, `anchors.standard_of`; snapshot version 2). */
export interface ChatStandard {
  number: string | null;
  title: string | null;
  edition: string | null;
  effective: string | null;
  /** The section the cited place is in: its number ("2.1"), an appendix ("נספח א'") or its heading. */
  section: string | null;
  section_title: string | null;
}

export interface ChatAnchor {
  v: number;
  precision: ChatAnchorPrecision;
  /** Hebrew label for the viewer's header. */
  precision_label: string;
  /** What a "region" covers. */
  region: "table" | "row" | null;
  degraded: ChatAnchorDegraded | null;
  document_id: string;
  version_id: string;
  /** The reading the turn pinned: the region route must be asked for this one. */
  reading_id: string | null;
  block_start: number | null;
  block_end: number | null;
  /** Empty for DOCX (structured) and when nothing more than the document is known. */
  pages: ChatAnchorPage[];
  location: {
    /** A readable title (null when the title and file name are garbled or ids). */
    title: string | null;
    /** The readable location: page (printed page), section, table, row, column or paragraph. Never ids. */
    label: string;
    page: number | null;
    page_end: number | null;
    printed_page: string | null;
    section: string | null;
  };
  table: ChatAnchorTable | null;
  structured: ChatAnchorStructured | null;
  /** Pages or rectangles were cut to keep the snapshot bounded. */
  truncated: boolean;
  /** The unit the cited place is in, in a file of several appraisals. */
  unit?: ChatUnit | null;
  /** The standard it cites; null for an appraisal and for a snapshot from before round 10 (version 1). */
  standard?: ChatStandard | null;
}

/** A computed result (C#) anchors to its inputs, never to a place in a document. */
export interface ChatComputedAnchor {
  v: number;
  precision: "computed";
  precision_label: string;
  inputs: string[];
}

export interface ChatSource {
  id: string;
  /** null for a listing of documents (kind "listing"): it names documents, it is not one */
  document_id: string | null;
  version_id: string | null;
  title: string;
  section: string | null;
  location: string;
  kind: string;
  text: string;
  block_start: number | null;
  block_end: number | null;
  table_index: number | null;
  page_list: number[] | null;
  chunk_id: string | null;
  listed_document_ids?: string[];
  /** The reading the cited blocks belong to (null: an answer from before readings had ids). Absent on a source
   * that does not come from an answer (the review screen opens the current reading). */
  reading_id?: string | null;
  /** What a reading tool returned: read in full, clipped (more remains), with unread regions, or with uncertain
   * reading. Absent on a search passage. */
  status?: "complete" | "clipped" | "has_unread_regions" | "uncertain_reading" | null;
  clipped?: boolean;
  /** How many regions inside it were not read. */
  unread_regions?: number;
  /** Where a clipped source continues (server-side position; a later turn reopens it from there). */
  more?: { target: (string | number | string[])[]; pos: number | number[] } | null;
  /** A table-row search hit: its body row in the table (0-based). */
  row_index?: number | null;
  /** Where the citation points (absent on older answers). */
  anchor?: ChatAnchor | null;
  /** The unit it is in, when its file holds more than one (the file name alone does not say which appraisal). */
  unit?: ChatUnit | null;
  /** The kind of document it is from (absent on answers from before round 10: an appraisal). */
  source_kind?: SourceKind;
  /** For a standard or another professional document: its identity and the section the passage is in. */
  standard?: ChatStandard | null;
  /** On a source of an appraisal gathered into the conversation's property (round 12): the server's link wording, e.g.
   * "שומה של אותו נכס: חלקה 8401/40 משותפת". Absent on the focus appraisal and in off mode. */
  link?: string | null;
}

export interface ChatMeasurement {
  id: string;
  measurement_id: string;
  document_id: string;
  version_id: string;
  title: string;
  metric: string;
  metric_kind: string;
  value_text: string;
  unit: string | null;
  period: string;
  vat: string;
  area_basis: string | null;
  subject: string | null;
  value_role: string;
  status: string;
  quote: string;
  section: string | null;
  block_index: number | null;
  table_index: number | null;
  /** The reading its place belongs to. */
  reading_id?: string | null;
  /** Its place was not found again when the document was reprocessed: not verified against the current reading. */
  anchor_lost?: boolean;
  /** The component its source states for it (absent on older answers). */
  facet?: ChatFacet | null;
  anchor?: ChatAnchor | null;
}

/** An input of a calculation: a value verified in a source (V#), a user assumption (A#), a stored measurement (M#)
 * or an earlier calculation (C#), with its full value. */
export interface ChatComputationInput {
  id: string;
  label: string;
  kind: "value" | "assumption" | "measurement" | "computation";
  value: string | null;
  display: string | null;
  value_text?: string;
  /** For a value: the passage it was verified in. */
  source_id?: string;
  certainty?: "verified" | "model_asserted" | "uncertain_reading";
  /** For an assumption: the user's words. */
  quote?: string;
}

/** What a calculation is: a scenario the user asked for (it rests on a user assumption), a value the report itself
 * writes reproduced, or a computation. */
export type ChatResultKind = "scenario" | "reproduces_report_value" | "computed" | "derived";

/** An intermediate result of a calculation: its formula in ids and its full value (when the server provides steps). */
export interface ChatComputationStep {
  expression: string;
  value: string;
}

export interface ChatComputation {
  id: string;
  /** The expression in ids (an answer stored before the calculator holds the aggregate's name). */
  operation: string;
  /** The full value (never rounded). */
  result: string | null;
  unit: string;
  /** Ids in an answer stored before the calculator; inputs with their values after. */
  inputs: (string | ChatComputationInput)[];
  documents: number;
  note: string;
  label?: string;
  expression?: string;
  /** The formula with the inputs' Hebrew labels. */
  formula?: string;
  value?: string;
  /** The result as shown: rounded for reading, and as a percentage for a ratio. */
  display?: { value: string; percent?: string };
  kind?: string | null;
  result_kind?: ChatResultKind;
  result_kind_label?: string;
  assumptions?: string[];
  sources?: string[];
  /** It mixes what does not combine (VAT, area basis, subject) on the stated justification. */
  conditional?: boolean;
  conditions?: string[];
  justification?: string | null;
  reproduces?: { source: string; as_written: string } | null;
  /** The VAT basis every money input shares; null for a result that is not money, or whose inputs differ. */
  vat?: "included" | "excluded" | null;
  /** Intermediate results, in order (absent until the server stores them). */
  steps?: ChatComputationStep[];
  n?: number | null;
  /** Its inputs, each opened through its own anchor (absent on older answers). */
  anchor?: ChatComputedAnchor | null;
}

/** The component, consideration or cash flow a value's or measurement's source states for it ("רכיב"): the source's own
 * words — its row label, column header, caption, the purpose phrase of its sentence, a list lead-in or a heading — never
 * a label or subject the model wrote. `own`: the value's own row, column or sentence; `context`: the caption, lead-in or
 * heading. `none`: read, and the source states none; `not_read`: not read. Absent on older answers. */
export interface ChatFacet {
  status: "stated" | "none" | "not_read";
  written: string | null;
  part: "row" | "column" | "caption" | "sentence" | "lead_in" | "heading" | null;
  tier: "own" | "context" | null;
}

/** A value the server verified in a source the answer's turn read (V#): the cell at a row and column, or a number
 * inside an exact quote, with its meaning and which parts of it the source attests. */
export interface ChatValue {
  id: string;
  value: string;
  value_text: string;
  label: string;
  source_id: string;
  document_id: string;
  version_id: string;
  reading_id?: string | null;
  title: string;
  location: string;
  kind: string;
  unit: string;
  unit_label: string;
  period: string;
  vat: string;
  area_basis: string;
  subject: string;
  role: string;
  provenance: Record<string, "source" | "model_asserted" | "not_stated">;
  /** "uncertain_reading": its own region was read uncertainly and no re-read confirmed it. */
  certainty: "verified" | "model_asserted" | "uncertain_reading";
  /** Who stated it and its standing, as the cited text says (absent on older answers). */
  section?: string | null;
  stated_by?: string | null;
  stance?: "adopted" | "claim" | "proposal" | "estimate" | "other" | "unknown" | null;
  scenario?: string | null;
  attribution?: string | null;
  /** Where its unit or meaning was read, per field (`{ unit: "header" }`): the cell, row, column header, table or quote. */
  meaning_from?: Record<string, string> | null;
  /** How its own region was read: "clear" (also after a confirmed re-read) or "uncertain". */
  reading?: "clear" | "uncertain" | null;
  reading_note?: string | null;
  locator: { row?: string; column?: string; row_number?: number; column_number?: number; quote?: string };
  quote: string;
  total: boolean;
  approx: boolean;
  /** The component its source states for it (absent on older answers). */
  facet?: ChatFacet | null;
  /** The cell or quoted words it was taken from (absent on older answers). */
  anchor?: ChatAnchor | null;
}

/** A number the user gave for a scenario (A#), quoted from the user's own message. */
export interface ChatAssumption {
  id: string;
  value: string;
  value_text: string;
  unit: string;
  label: string;
  quote: string;
  /** Which user message it quotes (1 = the first visible one), and whether it is the message this answer replied to. */
  turn: number;
  current: boolean;
  /** The id of the user message it quotes (absent on answers stored before it was recorded; null when not known). */
  message_id?: string | null;
}

export interface ChatClaim {
  text: string;
  source_ids: string[];
  basis: "explicit" | "inference" | "computed";
}

/** Whether the answer's claims held against their sources: every claim supported, some removed or only partly
 * supported, or none survived. Reported apart from completeness (R19). */
export type ChatCorrectness = "verified" | "partial" | "unverified";

/** How a component of the request was given in the shown answer (round 7): given in full or in part, not given,
 * waiting on the user's clarification, or not relevant (an assumption nothing used). `missing` and `undeterminable`
 * are the statuses of answers stored before round 7. */
export type ChatComponentStatus = "full" | "partial" | "not_answered" | "needs_clarification" | "not_relevant";
export type ChatRequirementStatus = ChatComponentStatus | "missing" | "undeterminable";

/** Why a component was not given (the server's one reason vocabulary, `coverage.REASONS`; KTD4). */
export type ChatGapReason =
  | "not_located"
  | "not_in_part_read"
  | "document_not_read"
  | "region_not_read"
  | "limit_reached"
  | "unit_not_located"
  | "standard_absent"
  | "not_verifiable"
  | "sources_conflict"
  | "detail_missing"
  | "calculation_incomplete"
  | "tool_failure"
  | "instruction_not_met"
  | "removed";

/** Why verification removed a claim (KTD5, R12). `not_checked`: it could not be checked, never that it was wrong. */
export type ChatFailureKind =
  | "absent_from_source"
  | "wrong_subject"
  | "wrong_unit"
  | "uncertain_reading"
  | "contradicts_source"
  | "context_not_read"
  | "wrong_calculation"
  | "invalid_citation"
  | "not_checked";

/** A requirement not given in full. Round 7 answers carry no reason text here: the reason is said once, in the
 * answer's gap paragraph (KTD4). */
export interface ChatCompletenessGap {
  id: string;
  text: string;
  status: Exclude<ChatRequirementStatus, "full">;
  /** A gap reason key (older answers: "not_found", "uncertain", …). */
  reason: string | null;
  /** Answers stored before round 7 only. */
  reason_text?: string | null;
  parent?: string;
  conditional?: boolean;
}

/** The answer's completeness against the requirements derived from the question (R18, R19). */
export interface ChatCompleteness {
  status: "full" | "partial" | "missing" | "undeterminable";
  requirements: number;
  missing: ChatCompletenessGap[];
}

/** One claim verification removed, on the normal path: its kind, its component and the server's fixed sentence for
 * the kind — never the claim's text (that is diagnostics). */
export interface ChatRemoval {
  failure_kind: ChatFailureKind | string;
  component: string | null;
  text: string;
  /** For `not_checked` only (round 8 R14): why — `check_failed`, `omitted`, `not_classified`, or (round 9 R19)
   * `time_limit`: the final check ran out of time; `text` says it. */
  cause?: "check_failed" | "omitted" | "not_classified" | "time_limit";
  /** Round 8 U7 (KTD8): the claim this row belongs to, the same id as its `ChatRemovalDecision`. Absent on older answers. */
  claim_id?: string;
  /** What was done to the claim: a unit removed, a unit withheld (its check did not complete), or a span cut. */
  action?: "removed" | "withheld" | "cut";
  /** For a cut: the ordinal N of the marker «[N: …]» that replaced the claim in the answer's text. */
  ordinal?: number;
}

/** How many claims each action touched (round 8 U7); `removed`, `cut`, `rewritten` and `withheld` change the text,
 * `qualified` and `cited` add a qualifier or a citation. */
export type ChatVerificationActions = Record<ChangeAction, number>;

/** What verification did, in counts; what it removed, and why, is diagnostics (not on this path). */
export interface ChatVerification {
  judged: boolean;
  judge_status: string | null;
  removed: number;
  partial: number;
  annotated: number;
  request_mismatch?: boolean;
  /** Absent on answers stored before claim correctness and completeness were reported apart. */
  correctness?: ChatCorrectness;
  /** Absent when no requirements were judged (and on older answers). */
  completeness?: ChatCompleteness;
  /** One summary per claim taken out of the text, in the answer's order (absent on answers stored before round 7). */
  removals?: ChatRemoval[];
  /** sha256 (hex) of the saved Markdown — `Message.content` (round 8 U7, R17). Absent on older answers. */
  final_digest?: string;
  /** Absent on older answers. */
  actions?: ChatVerificationActions;
  /** Results kept with the server's conditional qualifier (absent on older answers). */
  conditional?: number;
  /** Claims whose unsupported qualifier the server edited out of a number the sources back: an edit, not a removal
   * (round 13 U12; absent on older answers). */
  edited?: number;
  /** One row per "אומת חלקית" mark of the text, in the text's order (round 14 U7, KTD9); every viewer gets them, so a
   * viewer who may not open the detail reads the reason sentences here. Absent on answers stored before round 14. */
  partials?: ChatPartialRow[];
}

/** The reason class of one "אומת חלקית" mark (round 14 U7): `ordinal` is the mark's number among the marks, the `ordinal` of its
 * `partial_mark` span; `claim_ids` are the claims it covers (each is one entry of the diagnostics route); `text` is the server's
 * fixed sentence for the class, never the claim's text. */
export interface ChatPartialRow {
  ordinal: number;
  claim_id?: string;
  claim_ids?: string[];
  failure_kind: string | null;
  check?: string | null;
  text: string;
}

/** A run of the saved Markdown the server wrote: offsets in code points and a kind, never its text. */
export interface ChatServerSpan {
  start: number;
  end: number;
  kind: string;
  /** A `partial_mark` span only (round 14 U7): the mark's number among the marks of the text, from 1, and the claims of the
   * unit it marks. Absent on a mark stored before round 14. */
  ordinal?: number;
  claim_ids?: string[];
}

/** A component of the request with its outcome in the shown answer (round 7 U3; `coverage.public_components`). */
export interface ChatComponent {
  id: string;
  text: string;
  kind: "information" | "calculation" | "instruction" | "assumption" | "clarification" | string;
  aspect?: string;
  /** The component it belongs to ("" for a top-level one). */
  parent?: string;
  conditional?: boolean;
  subject?: string;
  status: ChatRequirementStatus;
  /** "undeterminable": the sources were found but do not settle it. */
  evidence_state?: "undeterminable" | null;
  /** Why it was not given: a gap reason key, null when it was given. */
  limitation?: ChatGapReason | string | null;
  limitation_text?: string | null;
  /** The server states it in the answer's gap paragraph. */
  stated?: boolean;
  /** Its sentence in the gap paragraph (already in the answer's text: never shown again). */
  gap?: string | null;
  units?: number[];
  removed_units?: number[];
  /** The failure kinds of its removed claims. */
  removal_kinds?: string[];
  absence_units?: number[];
  /** The workspace ids (S#, V#, M#, C#) the checks tied to it. */
  related?: string[];
  /** The section, table or pages read for it. */
  place?: { sid?: string | null; scope?: string | null; name?: string | null } | null;
  document?: string | null;
  document_id?: string | null;
  document_title?: string | null;
  searched?: boolean | null;
  claimed?: string | null;
  /** The parameters missing, or the first claim without a citation. */
  detail?: string | null;
  /** For an instruction: checked by its citations or by the judge. */
  check?: "citation" | "judge" | string;
  /** For a citation instruction: the claims left without a source. */
  uncited?: string[];
}

/** One group of the server's gap paragraph: a reason and the components it covers. `text` is the line already in
 * the answer's markdown. */
export interface ChatGap {
  reason: ChatGapReason | string;
  reason_text: string;
  components: string[];
  texts: string[];
  text: string;
}

/** A source a removed claim was checked against, as the diagnostics route serves it: an answer-shaped record of its
 * `type` (a passage, a value, a measurement, a calculation or an assumption) with its anchor. */
export type ChatCheckedSource =
  | (ChatSource & { type: "sources" })
  | (ChatValue & { type: "values" })
  | (ChatMeasurement & { type: "measurements" })
  | (ChatComputation & { type: "computations" })
  | (ChatAssumption & { type: "assumptions" });

/** One re-read of a claim's source (diagnostics only; round 8 U5): the source read, what was read, how it ended and,
 * when it was registered as a source of the turn, which. */
export interface ChatReread {
  source: string;
  scope?: string;
  outcome?: string;
  reason?: string;
  expanded_source?: string;
}

/** A handled claim's log entry (diagnostics only): the draft text (never a fact), the factual reason and the sources
 * it was checked against. Round 8 U7 (R22): one per claim removed, cut, rewritten, withheld, qualified or cited, in the
 * text's order and linked to its summary by `claim_id`; the fields after `repair_attempted` are absent on older logs. */
export interface ChatRemovalDecision {
  text: string;
  reason: string;
  severity: string;
  kind: string;
  failure_kind: string | null;
  check: string | null;
  component: string | null;
  checked_ids: string[];
  repair_attempted: boolean;
  sources?: ChatCheckedSource[];
  claim_id?: string;
  action?: ChangeAction;
  /** For a cut: the ordinal of the marker in the answer's text. */
  ordinal?: number;
  /** What remains of the claim in the answer: nothing for a removal, the marker for a cut, the claim otherwise. */
  remaining?: string;
  verdict?: string;
  /** For `not_checked`: why the claim could not be checked. */
  cause?: string;
  sources_checked?: string[];
  rereads?: ChatReread[];
  /** The final check's result on the claim as saved (`n/a` when it was not judged again). */
  post_check?: string;
  /** What a "verified in part" mark opens (round 14 U7, KTD9; on an entry whose action is `qualified` or `cut`): the sentence's
   * text as it stays, the claim's span (else the judge's reason) that was not verified, why (failure kind and check), and what
   * was checked (the sources and the re-reads, each with its outcome). Absent on other entries and on older logs. */
  supported?: string;
  not_verified?: string;
  why?: { failure_kind: string | null; check: string | null };
  checked?: { sources: string[]; rereads: ChatReread[] };
}

/** GET /api/chat/messages/{id}/diagnostics: the owner's or an office admin's view of what verification removed. */
export interface ChatDiagnostics {
  message_id: string;
  rounds: unknown;
  removed: ChatRemovalDecision[];
  resolution: unknown;
  limits_hit: string[];
}

/** A requirement of the question with its status in the verified answer (the ledger's per-requirement detail). */
export interface ChatRequirement {
  id: string;
  text: string;
  /** It asks for a calculation. */
  calculation: boolean;
  status: ChatRequirementStatus;
  /** The answer itself says it is missing or undeterminable. */
  stated: boolean;
  units: number[];
  related: string[];
  /** The judge's reason. */
  reason: string;
  limitation: string | null;
  limitation_text: string | null;
}

export interface ChatCoverage {
  documents: number;
  extracted: number;
  partial_extraction: string[];
  not_extracted: string[];
  partially_read: string[];
}

export interface LedgerDocument {
  document_id: string | null;
  title: string | null;
}

/** How deep the turn reached into a document: listed, a passage retrieved, a section or table read, its datum
 * verified in the answer. */
export type LedgerLevel = "not_reached" | "located" | "retrieved" | "read" | "verified";

/** A table the answer cites: its rows, and how many of them the answer presents a value of. */
export interface LedgerTable {
  title: string;
  location: string;
  rows: number;
  presented: number;
}

/** What the answer covered, computed by the server from what the turn's tools did. */
export interface ChatLedger {
  scope_kind: "focused" | "set";
  scope_query: string;
  cited: LedgerDocument[];
  matching?: LedgerDocument[];
  levels?: Record<string, LedgerLevel>;
  read?: LedgerDocument[];
  retrieved_only?: LedgerDocument[];
  with_data?: LedgerDocument[];
  not_checked?: LedgerDocument[];
  unused?: LedgerDocument[];
  partially_read?: LedgerDocument[];
  omitted?: (LedgerDocument & { what: string; why: string })[];
  also_matching?: LedgerDocument[];
  tables?: LedgerTable[];
  /** an answer about which documents are in the set (a count, a list), resting on a listing only */
  membership?: boolean;
  pages?: number;
  pages_read?: number;
  complete: boolean;
  note?: string;
  /** Each requirement of the question and how the answer gave it (absent on older answers). */
  requirements?: ChatRequirement[];
}

/** The scope line of an answer that stayed in the appraisal unit of the turn before (a metric or computation change is no
 * property change): the unit by its title and pages, in its file. Absent when the unit was not kept from the previous turn. */
export interface ChatUnitScope {
  title: string | null;
  /** First and last page of the unit in the file; empty when unknown. */
  pages: number[];
  kept: true;
  file_title: string;
}

/** What marks a timeline row (round 11 U9); each is shown as a text badge, never by colour alone. `undecided` (round 13 U4):
 * a datum of the same kind whose facet could not be compared with this one's has a different amount in an overlapping
 * period; neither a conflict nor agreement. The row's `undecided_with` names it. */
export type ChatTimelineMark = "forecast" | "conflict" | "undecided" | "outside_requested" | "uncertain" | "unknown_date";

export interface ChatTimelineRow {
  /** ISO date at the row's own precision, null when the sources give none. */
  date: string | null;
  precision: "day" | "month" | "year" | null;
  /** The end of a period (a table caption's range) at the same precision; null for a single date. */
  end: string | null;
  date_kind: "valuation" | "transaction" | "stated" | "range" | null;
  value_text: string;
  /** The unit as display text ("₪", "מ״ר"), not a code; null when none. */
  unit: string | null;
  /** The citation id as shown in the answer (S#, V# or M#). */
  sid: string;
  source: { document_id: string; title: string; location: string | null };
  marks: ChatTimelineMark[];
  /** With the `undecided` mark: the citation ids (V#/M#) of the values this one could not be compared with. */
  undecided_with?: string[];
  /** With the `conflict` mark (round 13 U6): the citation ids of every value this one conflicts with, even one in another
   * block. Absent when the row is in no conflict. Round 14 U6: only rows that are on screen (a counterpart without a row is in
   * `not_in_answer_with`). */
  conflict_with?: string[];
  /** Round 14 U6 (KTD7, R13): counterparts of an `undecided` or `conflict` mark that have no row because the answer does not use
   * them, shown as the text "ערך זה אינו בתשובה: <title>, <location>". Absent when there are none. */
  not_in_answer_with?: ChatNotInAnswer[];
  /** Round 14 U6: on a row of a value the answer does not use (one datum's history), the server's label "נתון היסטורי, לא נכלל
   * במסקנת התשובה". Absent on a row the answer uses. */
  note?: string;
  /** Round 14 U6 (KTD8): the structured state of the row. Absent on an answer stored before round 14: read such a row as before. */
  state?: ChatRowState;
}

/** A value a mark names that is not in the answer (round 14 U6): no row, so no citation chip. */
export interface ChatNotInAnswer {
  sid: string;
  title: string;
  location: string;
}

/** The state of a component (round 14 KTD4): `stated`; `none` (read, nothing in the range checked names one); `unparsed` (read,
 * ambiguous); `not_read`. A proven conflict is a comparison result, never a component state. */
export type ChatRowComponentStatus = "stated" | "none" | "unparsed" | "not_read";

/** Why a component is not stated (`reason` is null for `stated`): `not_in_range`, `intervening_value`, `two_components`,
 * `picture_cell`, `not_needed` (the family needs none), `cap` (the read reached its character cap), or `other` (no more precise
 * reason is recorded). */
export type ChatComponentReason =
  | "not_in_range"
  | "intervening_value"
  | "two_components"
  | "picture_cell"
  | "not_needed"
  | "cap"
  | "other";

/** Where a component was read from, when that is not the value's own sentence, row or cell (round 14 U6, KTD8). `anchor` is the
 * resolved snapshot of that place (null when it cannot be located); the raw stub is never sent. */
export interface ChatComponentContext {
  /** The server's label of the place ("משפט קודם באותה פסקה", "פסקה קודמת בסעיף", "פתיח הרשימה", "כותרת הסעיף", "הערת הטבלה", "כותרת הטבלה"). */
  location: string;
  /** The interpreting text, clipped to 120 characters (null when the read stated no phrase). */
  quote: string | null;
  anchor: ChatAnchor | null;
}

export interface ChatRowComponent {
  status: ChatRowComponentStatus;
  reason: ChatComponentReason | null;
  /** The source's own words; null unless `status` is `stated`. */
  written: string | null;
  /** Where they are: `row`, `column`, `caption`, `sentence`, `lead_in`, `heading`, `preceding_sentence`, `paragraph`, `table_note`. */
  part: string | null;
  /** `own` (the value's own row label, column header or purpose phrase) or `context` (anything read from around it). */
  tier: "own" | "context" | null;
  context: ChatComponentContext | null;
}

/** The verification result of the claim that cites the value, with its reason class (a failure kind, `recency`, `final`,
 * `unspecified`; never the claim's text). */
export interface ChatRowClaim {
  result: "supported" | "partial" | "unsupported" | "not_checked";
  reason: string | null;
}

export type ChatComparisonStatus = "same" | "different" | "undecided" | "conflict" | "single";

/** The four questions of a row apart, plus its support (round 14 U6, KTD8, R9): an undecided comparison never makes a supported
 * value unverified. */
export interface ChatRowState {
  /** `verified`: the server matched the number in the source; `uncertain`: the reading cannot be presented as certain. */
  reading: "verified" | "uncertain";
  component: ChatRowComponent;
  /** Null on a history row (no claim of the answer cites it) and when no verification report was given. */
  claim: ChatRowClaim | null;
  /** `cited`: the answer uses the value; `history`: one datum's other rows, not part of the conclusion. */
  support: "cited" | "history";
  /** `with`: the citation ids of rows on screen. */
  comparison: { status: ChatComparisonStatus; with: string[] };
  /** The one row-level reason key: the component's `reason` when it is not stated, else the comparison status. */
  reason: string;
}

/** One comparable group, its rows already in display order (the server orders them; the screen never re-sorts). */
export interface ChatTimelineGroup {
  label: string;
  rows: ChatTimelineRow[];
}

/** One row of the components table (round 13 U6, KTD7): a component of one appraisal at one date. `facet` is the
 * consideration or cash flow the source gives the value, in its own words, or the server's sentence for the state of the
 * component (round 14 U6: "המקור לא מציין רכיב בקטע שנקרא", "ההקשר לא נקרא", ...; "רכיב לא צוין במקור" only for a family that needs
 * none). `reduction` and `derived` (round 14 U4): a coefficient and the percentage reduction that is one minus it are one row. */
export type ChatBreakdownRow = Pick<
  ChatTimelineRow,
  | "value_text"
  | "unit"
  | "sid"
  | "source"
  | "marks"
  | "undecided_with"
  | "conflict_with"
  | "not_in_answer_with"
  | "note"
  | "state"
> & {
  facet: string;
  reduction?: { sid: string; value_text: string; unit: string | null; source: ChatTimelineRow["source"] };
  /** The C# of `1 − Vn` the server derived for the reduction; null while none is registered. */
  derived?: string | null;
};

/** The components of one appraisal unit at one date (parallel components, never competing values), rows in server order.
 * Its rows are in no timeline group. */
export interface ChatBreakdownBlock {
  label: string;
  as_of: Pick<ChatTimelineRow, "date" | "precision" | "end" | "date_kind">;
  rows: ChatBreakdownRow[];
}

/** One appraisal unit of a property's inventory in the coverage record (round 12 U7, U8). */
export interface ChatCoverageUnit {
  document_id: string;
  version_id: string;
  unit_no: number;
  title: string | null;
  /** ISO date at `precision`; null when the unit states no (single) valuation date. */
  date: string | null;
  precision: "day" | "month" | "year" | null;
  date_status: string;
  undated: boolean;
  /** `located`: the search returned a passage of the unit (round 13: a record of v1 says `hit` for the same thing);
   * `no_match`: searched without one; `not_searched_cap`: not searched (the cap); `read`: the datum was read from the
   * unit (a value or measurement of the cited datum's kind was registered from it, or its section or table was opened)
   * or, on a read-only record (`read_only`), the answer cites its source; `not_searched`: not checked by a read-only
   * turn. */
  status: "located" | "hit" | "no_match" | "not_searched_cap" | "read" | "not_searched";
  link: string | null;
  focus: boolean;
  /** Whether this unit keeps the coverage from being complete. */
  blocks: boolean;
}

/** An unlinked or possible candidate of the property (round 12 U7, round 13 U8): the fields of the candidates the record
 * names. `elsewhere` and `read` are on the items only (`items` of a v2 record). */
export interface ChatCoverageCandidate {
  document_id: string;
  version_id: string;
  unit_no: number;
  title: string | null;
  date: string | null;
  precision: "day" | "month" | "year" | null;
  date_status: string;
  reason: string;
  /** Every mention of the property in the unit stands under another subject (a comparison, a transaction, an appendix). */
  elsewhere?: boolean;
  /** The answer's turn read the unit; read and `elsewhere` together take it out of the candidate class. */
  read?: boolean;
}

/** A class of candidates in a v2 coverage record: at most three `named` (newest first), `count` the ones that still block,
 * `read` the ones that left the class (read under another subject), `items` every one. A v1 record has `named` and `count`. */
export interface ChatCoverageCandidates {
  named: ChatCoverageCandidate[];
  count: number;
  read?: number;
  items?: ChatCoverageCandidate[];
}

/** The newer datum of a class that could not be compared with the cited one (round 13 U4, U8): the answer keeps the
 * qualified wording and the note names it. */
export interface ChatLaterUndecided {
  older: string;
  newer: string;
  title: string | null;
  when: string;
  document_id: string;
  version_id: string;
  unit_no: number | null;
}

/** What a time answer checked (round 12 U7, U8; R12): the appraisals searched, searched without a match, not searched,
 * undated, and unlinked candidates, with the server-written `note` the screen shows. Absent on an answer from before
 * round 12 and on a turn with no time mode: "not recorded", never "complete". Round 13 U8 (`v: 2`) splits the rest of the
 * office into `possible` candidates (block completeness), `unlinkable` units (counted) and `excluded` ones (proven another
 * property, counted); a v1 record has `undetermined` instead. Every field added in v2 is optional here. */
export interface ChatTimeCoverage {
  v: number;
  mode: "recent" | "explicit";
  requested: ChatAnswerTime["requested"];
  scope: "property" | "unit" | "window";
  via: string | null;
  identified: boolean;
  property: { label: string; reasons: string[]; units: number } | null;
  units: ChatCoverageUnit[];
  candidates: ChatCoverageCandidates;
  /** v2: unidentified units that mention the property, or were read in part. */
  possible?: ChatCoverageCandidates;
  /** v2: unidentified units with no mention of the property; counted, they block nothing. */
  unlinkable?: number;
  /** v2: units proven to be another property. */
  excluded?: number;
  /** v1 only: every unit whose identity could not exclude the property. */
  undetermined?: number;
  /** v2: newer measured data of a class that could not be compared with the cited one. */
  later_undecided?: ChatLaterUndecided[];
  complete: boolean;
  blocking: string[];
  /** True on a record built for a timed turn that answered from read sources with no time-ranked search (round 12):
   * such a record is partial unless every unit was read. Absent or false otherwise. */
  read_only?: boolean;
  /** The visible note, lines separated by newlines; rendered as its own element, not part of `markdown`. */
  note?: string | null;
}

/** The time the answer was produced under, and its timeline (round 11 U9); absent on older answers. */
export interface ChatAnswerTime {
  mode: "none" | "recent" | "explicit";
  prefer_recent: boolean;
  requested: { kind: string; start: string | null; end: string | null; precision: string | null; text: string } | null;
  timeline: ChatTimelineGroup[];
  /** The components table (round 13 U6, `answer.time.breakdown`); absent on an older answer. */
  breakdown?: ChatBreakdownBlock[];
  /** A plain-text copy of the whole timeline and the components table, ending with the coverage note when there is one; not in `markdown`, so it
   * is not shown twice. The copy button appends it. */
  text?: string | null;
  /** The coverage record with its note (round 12 U8); absent on older answers and with no time mode. */
  coverage?: ChatTimeCoverage | null;
}

/** The switch state a message was produced with (round 11 KTD1); null on a message from before round 11. */
export interface ChatTimePreference {
  version: number;
  prefer_recent: boolean;
  anchor_date: string;
}

export interface ChatAnswer {
  kind: "rag" | "search_only";
  status: "answered" | "partial" | "not_found" | "clarification";
  markdown: string;
  claims: ChatClaim[];
  clarification: string | null;
  missing: string | null;
  sources: ChatSource[];
  measurements: ChatMeasurement[];
  computations: ChatComputation[];
  /** Values and user assumptions the answer's calculations and citations use (absent on older answers). */
  values?: ChatValue[];
  assumptions?: ChatAssumption[];
  documents: { document_id: string; title: string }[];
  verification: ChatVerification | null;
  searches: string[];
  coverage: ChatCoverage[];
  ledger?: ChatLedger | null;
  /** Each datum the question asked for, with the status the turn's actions support. */
  requested?: {
    label: string;
    status: "found" | "not_found_search" | "source_partial" | "section_checked_absent";
    section: string | null;
    /** Why a section claimed absent counts only as read in part: clipped, or with a region not read. */
    partial_reason?: "clipped" | "unread" | null;
  }[];
  /** Each component of the request with its outcome (absent on answers stored before round 7). */
  components?: ChatComponent[];
  /** The groups of the gap paragraph the markdown ends with (absent on answers stored before round 7). */
  gaps?: ChatGap[];
  steps?: number;
  hidden?: boolean;
  /** The answer's scope line when the unit was kept from the previous turn (absent otherwise, and on older answers). */
  unit_scope?: ChatUnitScope | null;
  /** The time mode and the server-built timeline (round 11; absent on older answers). */
  time?: ChatAnswerTime | null;
  /** Where the server wrote in `markdown` (the removal note among them); absent on older answers. */
  server_spans?: ChatServerSpan[];
}

export type ChatMessageStatus = "running" | "cancelling" | "cancelled" | "done" | "failed";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  status: ChatMessageStatus;
  error: string | null;
  progress: ChatProgressStep[];
  answer: ChatAnswer | null;
  reply_to: string | null;
  client_id: string | null;
  created_at: string;
  stale: boolean;
  cancel_requested: boolean;
  /** The switch state this message was sent or answered with (null: from before round 11). */
  time_preference?: ChatTimePreference | null;
  /** Round 14 U7 (KTD9): the viewer may open this message's diagnostics (its owner, or an office admin). Only then is a "verified
   * in part" mark a button; absent or false, it stays text. A diagnostics 404 can still happen. */
  can_view_diagnostics?: boolean;
}

export interface SourceBlock {
  index: number;
  kind: "heading" | "paragraph" | "textbox" | "table" | "image";
  section: string | null;
  section_path: string[];
  paragraph_no: number | null;
  page: number | null;
  media: string | null;
  source: string;
  status: string;
  note: string | null;
  text: string;
  /** PDF: the block's box on its page (x0, top, x1, bottom in points), how it was read, and the text as
   * extracted when `text` is a verified correction of it. */
  bbox?: [number, number, number, number] | null;
  method?: string | null;
  reader_version?: string | null;
  content_hash?: string | null;
  original_text?: string | null;
  media_url?: string;
  /** PDF: the block's page, and its region when it has a box, rendered as an image. */
  page_url?: string;
  region_url?: string;
  table?: {
    headers: string[] | null;
    caption: string | null;
    title: string[] | null;
    notes: string[] | null;
    source: string | null;
    media: string | null;
    rows: string[][];
  };
}

export interface SourceBlocks {
  document_id: string;
  version_id: string;
  title: string;
  is_current: boolean;
  mime_type: string;
  total: number;
  blocks: SourceBlock[];
  file_url: string;
  /** The version's current reading. */
  reading_id?: string | null;
  /** The document was reprocessed after the citation: its block numbers mean other text now, so no blocks. */
  stale?: boolean;
}

/** What the source viewer opens: a citation's place in its document. The text view always has `source` (the passage,
 * or the passage behind a value or measurement); the page or structured view needs a document anchor. A breakdown's
 * input (U7) builds the same target. */
export interface ViewerTarget {
  /** The citation id (S#, V#, M#). */
  id: string;
  source: ChatSource;
  /** null: an answer stored before anchors, or a source with no place in a document (a listing); the text view only. */
  anchor: ChatAnchor | null;
  /** A value's or measurement's status (V#, M#). */
  valueStatus?: ValueStatus | null;
}

/** The targets a viewer moves through with previous/next: the answer's citations in answer order (a chip), or a
 * breakdown's document inputs (U7). */
export interface ViewerNav {
  items: ViewerTarget[];
  index: number;
}
