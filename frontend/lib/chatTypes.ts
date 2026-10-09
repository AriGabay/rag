// Types of the conversational chat API (/api/chat). Kept apart from the earlier /api/ask types.

import type { ValueStatus } from "./format";

export interface ChatConversation {
  id: string;
  title: string;
  updated_at: string;
  created_at: string;
  archived: boolean;
  engine: "legacy" | "rag";
}

export interface ChatProgressStep {
  step: string;
  label: string;
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
  certainty?: "verified" | "model_asserted";
  /** For an assumption: the user's words. */
  quote?: string;
}

/** What a calculation is: a scenario the user asked for (it rests on a user assumption), a value the report itself
 * writes reproduced, or a computation. */
export type ChatResultKind = "scenario" | "reproduces_report_value" | "computed";

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
  n?: number | null;
  /** Its inputs, each opened through its own anchor (absent on older answers). */
  anchor?: ChatComputedAnchor | null;
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
  certainty: "verified" | "model_asserted";
  locator: { row?: string; column?: string; row_number?: number; column_number?: number; quote?: string };
  quote: string;
  total: boolean;
  approx: boolean;
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
}

export interface ChatClaim {
  text: string;
  source_ids: string[];
  basis: "explicit" | "inference" | "computed";
}

/** Whether the answer's claims held against their sources: every claim supported, some removed or only partly
 * supported, or none survived. Reported apart from completeness (R19). */
export type ChatCorrectness = "verified" | "partial" | "unverified";

/** How a requirement of the question was given in the verified answer. */
export type ChatRequirementStatus = "full" | "partial" | "missing" | "undeterminable";

/** A requirement not given in full, with the reason computed from what the turn found and did (R21). */
export interface ChatCompletenessGap {
  id: string;
  text: string;
  status: Exclude<ChatRequirementStatus, "full">;
  /** "not_found" | "uncertain" | "tool_failure" | "calculation_incomplete" | "insufficient" | "not_searched" */
  reason: string | null;
  reason_text: string | null;
}

/** The answer's completeness against the requirements derived from the question (R18, R19). */
export interface ChatCompleteness {
  status: ChatRequirementStatus;
  requirements: number;
  missing: ChatCompletenessGap[];
}

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
  steps?: number;
  hidden?: boolean;
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
