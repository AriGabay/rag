// Types of the conversational chat API (/api/chat). Kept apart from the earlier /api/ask types.

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
}

export interface ChatComputation {
  id: string;
  operation: string;
  result: string | null;
  unit: string;
  inputs: string[];
  documents: number;
  note: string;
}

export interface ChatClaim {
  text: string;
  source_ids: string[];
  basis: "explicit" | "inference" | "computed";
}

/** What verification did, in counts; what it removed, and why, is diagnostics (not on this path). */
export interface ChatVerification {
  judged: boolean;
  judge_status: string | null;
  removed: number;
  partial: number;
  annotated: number;
  request_mismatch?: boolean;
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
}
