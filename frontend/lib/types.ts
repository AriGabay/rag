// Types mirroring docs/api-contract.md. Money, areas and price per sqm are exact decimal strings.

export type DecimalString = string;
export type IsoDate = string;

export type Role = "admin" | "employee";

export interface Group {
  id: string;
  name: string;
}

export interface MeResponse {
  user: { id: string; email: string; full_name: string; role: Role; can_upload: boolean };
  office: { id: string; name: string };
  groups: Group[];
  demo_mode: boolean;
}

// ---- Documents ----

export type VersionStatus = "pending" | "processing" | "ready" | "needs_review" | "failed" | "superseded";

export interface Version {
  id: string;
  version_no: number;
  filename: string;
  status: VersionStatus;
  status_reason: string | null;
  page_count: number | null;
  pages_incomplete: number[] | number | null;
  records_total: number | null;
  records_needing_review: number | null;
  is_current: boolean;
  created_at: string;
  processed_at: string | null;
  mime_type: string;
  reading?: VersionReading;
}

/** What was read of a version, kept apart (searchable passages are not structured records). */
export interface VersionReading {
  passages: number;
  tables: number;
  measurements: number;
  measurements_state: "pending" | "done" | "partial" | "failed" | null;
  images_total: number;
  images: Partial<Record<"read" | "read_uncertain" | "no_text" | "decorative" | "unread", number>>;
  unread: { media: string | null; section: string | null; reason: string | null }[];
  partial: boolean;
  ingestion_version: string | null;
}

export interface DocumentSummary {
  id: string;
  title: string;
  group: Group;
  deleted: boolean;
  created_at: string;
  current_version: Version | null;
  versions_count: number;
}

export interface DocumentDetail extends DocumentSummary {
  versions: Version[];
}

export interface UploadResult {
  filename: string;
  status: "accepted" | "duplicate" | "rejected";
  reason?: string;
  document_id?: string;
  version_id?: string;
}

export interface SearchResult {
  chunk_id: string;
  document_id: string;
  version_id: string;
  title: string;
  page_list: number[];
  section: string | null;
  snippet: string;
  score: number;
}

// ---- Review ----

export type FieldName =
  | "data_kind"
  | "city"
  | "neighborhood"
  | "address"
  | "block"
  | "parcel"
  | "sub_parcel"
  | "property_type"
  | "rooms"
  | "transaction_date"
  | "valuation_date"
  | "report_date"
  | "area"
  | "area_type"
  | "price"
  | "currency"
  | "vat_basis"
  | "price_per_sqm_stated";

export interface Source {
  evidence_id: string;
  document_id: string;
  version_id: string;
  title: string;
  page_list: number[];
  section: string | null;
  row: number | null;
  snippet: string;
  url: string;
  /** Fact sources: the value in the attribute's canonical unit and whether a person reviewed it. */
  value?: string | null;
  tier?: "verified" | "preliminary";
  /** Compare sources: the side / version label (for example "גרסה 2"). */
  label?: string | null;
}

export interface RecordSummary {
  data_kind: string | null;
  city: string | null;
  neighborhood: string | null;
  address: string | null;
  price: DecimalString | null;
  area: DecimalString | null;
  area_type: string | null;
  transaction_date: IsoDate | null;
  valuation_date: IsoDate | null;
}

export interface RecordReviewItem {
  kind: "record";
  id: string;
  document: { id: string; title: string };
  version_id: string;
  page_no: number | null;
  verification_status: string;
  summary: RecordSummary;
  flags: { conflict: boolean; missing_critical: string[]; ocr: boolean };
}

export interface TransactionSummary {
  transaction_id: string;
  data_kind: string | null;
  address: string | null;
  price: DecimalString | null;
  area: DecimalString | null;
  area_type: string | null;
  transaction_date: IsoDate | null;
  sources: Source[];
}

export interface DedupReviewItem {
  kind: "dedup";
  id: string;
  reason: string;
  a: TransactionSummary;
  b: TransactionSummary;
}

export type ReviewItem = RecordReviewItem | DedupReviewItem;

export interface RecordField {
  field: FieldName | string;
  label: string;
  original_text: string | null;
  normalized_value: string | null;
  status: string;
  source_path: string | null;
  previous: unknown[];
}

export interface RecordDetail {
  id: string;
  document: { id: string; title: string };
  version_id: string;
  page_no: number | null;
  table_index: number | null;
  row_index: number | null;
  text_span: string | null;
  is_docx: boolean;
  verification_status: string;
  conflict_flag: boolean;
  missing_critical: string[];
  ocr: boolean;
  review_note: string | null;
  fields: RecordField[];
  computed_price_per_sqm: DecimalString | null;
  stated_price_per_sqm: DecimalString | null;
  calc_definition: string | null;
  file_url: string;
}

// ---- Chat ----

export interface Condition {
  label: string;
  value: string;
}

export interface ClarificationOption {
  value: string;
  label: string;
}

export interface Clarification {
  key: string;
  question: string;
  options: ClarificationOption[];
}

/** The price-per-sqm block of a price question. */
export interface NumericResult {
  conditions: Condition[];
  record_count: number;
  mean_price_per_sqm: DecimalString | null;
  weighted_price_per_sqm: DecimalString | null;
  median_price_per_sqm: DecimalString | null;
  min_price_per_sqm: DecimalString | null;
  max_price_per_sqm: DecimalString | null;
  currency: string;
  uncertain_duplicates: number;
  conflicts: number;
}

/** Any other computed attribute; for an extracted attribute the figure covers reviewed values only. */
export interface AttributeNumeric {
  conditions: Condition[];
  attribute: string;
  operation: string;
  value: DecimalString | null;
  unit: string | null;
  record_count: number;
  minimum?: DecimalString | null;
  maximum?: DecimalString | null;
  /** Listed when there are five or fewer observations. */
  values?: DecimalString[] | null;
}

/** Values the server validated mechanically but no person reviewed, shown as a separately labeled figure. */
export interface Preliminary {
  value: DecimalString | null;
  record_count: number;
  values: DecimalString[] | null;
}

/** Extraction coverage of an attribute over the documents in scope (R14). */
export interface FactCoverage {
  in_scope: number;
  found: number;
  not_stated: number;
  partial_scan: number;
  pending: number;
  failed: number;
  not_yet_extracted: number;
  awaiting_review: number;
  conflicts: number;
  unknown_metadata: number;
}

export interface Coverage {
  text: string;
  docs_pending: number;
  docs_failed: number;
  docs_needs_review: number;
  records_awaiting_verification: number;
  facts?: FactCoverage | null;
}

export type ClaimKind = "explicit" | "inferred" | "computed";

export interface Claim {
  text: string;
  kind: ClaimKind;
  evidence_ids: string[];
}

export type AbstentionKind = "not_found" | "not_stated" | "not_extracted_or_verified" | "insufficient_permission_scope";

export interface CompareConflict {
  datum: string;
  sides: { label: string; text: string; evidence_ids: string[] }[];
}

export interface CompareInfo {
  sides: { label: string; document_id: string; version_id: string; evidence_count: number }[];
  incomplete: boolean;
  missing_sides: string[];
  conflicts: CompareConflict[];
}

/** One context item of the conversation state; `key` is what `remove` takes. */
export interface ContextChip {
  key: string;
  label: string;
  value: string;
}

export type AnswerKind = "numeric" | "clarification" | "content" | "combined" | "abstain";
export type Provider = "template" | "mock" | "cloud" | "extractive";

/** Keys after `limitations` are absent from answers stored before the general question engine. */
export interface Answer {
  kind: AnswerKind;
  text: string;
  provider: Provider;
  demo: boolean;
  clarification?: Clarification | null;
  numeric?: NumericResult | AttributeNumeric | null;
  sources: Source[];
  coverage?: Coverage | null;
  limitations: string[];
  mode?: ProviderMode;
  claims?: Claim[];
  abstention_kind?: AbstentionKind | null;
  dropped_claims?: number;
  preliminary?: Preliminary | null;
  method?: string | null;
  conditions?: Condition[];
  partial?: boolean;
  pending_extraction?: number;
  interpretation_note?: string | null;
  cleared?: ContextChip[];
  meta?: "explain_previous" | "show_sources" | null;
  compare?: CompareInfo | null;
  cached?: boolean;
}

export interface Message {
  question_id: string;
  question: string;
  answer: Answer | null;
  stale: boolean;
  hidden: boolean;
  created_at: string;
}

export interface ConversationListItem {
  id: string;
  title: string | null;
  updated_at: string;
}

/** The server-held conversation context (R17); `chips` drive the context strip. */
export interface ConversationContext {
  attribute: string | null;
  metric: string | null;
  city: string | null;
  neighborhood: string | null;
  years: { from: number | null; to: number | null } | null;
  data_kind: string | null;
  date_field: string | null;
  chips: ContextChip[];
}

export interface ConversationDetail {
  id: string;
  title: string | null;
  confirmed_conditions: Condition[] | null;
  pending_clarification: Clarification | null;
  context?: ConversationContext | null;
  messages: Message[];
}

/** Explicit condition edits, applied by the server as a delta to the conversation context. */
export interface AskFilters {
  city?: string;
  neighborhood?: string;
  data_kind?: string;
  date_field?: string;
  year_from?: number;
  year_to?: number;
}

export interface AskRequest {
  conversation_id?: string;
  /** One per user action, reused on retries so a turn never applies twice. */
  turn_id?: string;
  question?: string;
  filters?: AskFilters;
  /** Context chip keys to clear (`ContextChip.key`). */
  remove?: string[];
  clarification?: { key: string; value: string };
}

export interface AskResponse {
  conversation_id: string;
  question_id: string;
  turn_id?: string;
  answer: Answer;
}

// ---- Admin ----

export interface AdminUser {
  id: string;
  email: string;
  full_name: string;
  role: Role;
  can_upload: boolean;
  is_active: boolean;
  group_ids: string[];
}

export interface AdminGroup {
  id: string;
  name: string;
  document_count: number;
}

/** Derived per office by the backend (providers/status.py): which provider actually answers. */
export type ProviderMode = "cloud" | "error" | "demo" | "limited";

/** Connection-test and call statuses; `missing_key` means no request was sent. */
export type ProviderStatus =
  | "ok"
  | "missing_key"
  | "auth"
  | "model_unavailable"
  | "timeout"
  | "rate_limited"
  | "quota"
  | "refusal"
  | "incomplete"
  | "invalid"
  | "error"
  /** Cloud use was acknowledged for another provider than the selected one: nothing is sent until re-acknowledged. */
  | "reacknowledge_required";

export interface ProviderTest {
  provider: string | null;
  model: string | null;
  ok: boolean;
  status: ProviderStatus;
  tested_at: string | null;
}

export interface PurposeModel {
  purpose: "agent" | "resolve" | "verify" | "measure" | "vision" | "summary";
  model: string;
  effort: string | null;
}

export interface AdminSettings {
  cloud_llm_enabled: boolean;
  provider: "openai" | "anthropic";
  provider_name: string;
  model: string;
  /** Every model purpose with its configured model and reasoning effort (null: the provider takes none). */
  purposes: PurposeModel[];
  /** Whether the server holds a key for the selected provider. The key itself never leaves the server. */
  key_present: boolean;
  mode: ProviderMode;
  /** The failure behind `error` mode, else null. */
  mode_status: ProviderStatus | null;
  /** Cloud mode without a passing connection test for the current provider and model. */
  untested: boolean;
  last_test: ProviderTest | null;
  retention_note: string | null;
  acknowledged_at: string | null;
}

export interface AdminCoverage {
  documents_by_status: Record<string, number>;
  records: { total: number; verified: number; awaiting_verification: number; needs_review: number };
  open_dedup_candidates: number;
  review_queue_count: number;
}

// ---------- Facts review (U11) ----------

export type FactStatus = "auto_validated" | "needs_review" | "verified" | "corrected" | "rejected";

export interface FactUnitOption {
  /** Unit code sent back on correction; "" means a plain number without a unit. */
  code: string;
  label: string;
}

export interface FactAttribute {
  id: string;
  label: string;
  value_type: string;
  unit_dimension: string | null;
  canonical_unit: string | null;
  canonical_unit_label: string | null;
  /** Units of the attribute's dimension only (empty for non-numeric attributes). */
  unit_options: FactUnitOption[];
  facts_version: number;
}

/** One fact as a row: the value in the attribute's canonical unit, the verbatim quote and the page link. */
export interface FactBrief {
  id: string;
  status: FactStatus;
  value: string | null;
  unit: string | null;
  unit_label: string | null;
  original: { value_text: string | null; unit: string | null; unit_label: string | null };
  quote: string;
  page: number | null;
  url: string;
  document: { id: string; title: string };
  version_id: string;
}

export interface FactHistoryEntry {
  action: "approve" | "reject" | "correct";
  status: FactStatus;
  at: string;
  by: string;
  value_numeric?: string | null;
  canonical_value?: string | null;
  unit?: string | null;
}

export interface ReviewFact extends FactBrief {
  entity_role: string;
  entity_descriptor: string | null;
  review_note: string | null;
  reviewed_at: string | null;
  previous: FactHistoryEntry[];
  /** Differing values for the same entity in documents the reviewer can see (computed at read time). */
  conflicts: FactBrief[];
}

export interface FactDetail extends ReviewFact {
  attribute: FactAttribute;
  facts_version: number;
}

export interface FactReviewGroup {
  attribute: FactAttribute;
  documents: { document: { id: string; title: string }; version_id: string; facts: ReviewFact[] }[];
}

// ---------- end Facts review ----------
