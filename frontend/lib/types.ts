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

export interface Coverage {
  text: string;
  docs_pending: number;
  docs_failed: number;
  docs_needs_review: number;
  records_awaiting_verification: number;
}

export type AnswerKind = "numeric" | "clarification" | "content" | "combined" | "abstain";
export type Provider = "template" | "mock" | "cloud" | "extractive";

export interface Answer {
  kind: AnswerKind;
  text: string;
  provider: Provider;
  demo: boolean;
  clarification?: Clarification | null;
  numeric?: NumericResult | null;
  sources: Source[];
  coverage?: Coverage | null;
  limitations: string[];
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

export interface ConversationDetail {
  id: string;
  title: string | null;
  // Shape not pinned by the contract: accepted as a list of {label, value} or a key/value object.
  confirmed_conditions: Condition[] | Record<string, unknown> | null;
  pending_clarification: Clarification | null;
  messages: Message[];
}

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
  question?: string;
  filters?: AskFilters;
  clarification?: { key: string; value: string };
}

export interface AskResponse {
  conversation_id: string;
  question_id: string;
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

export type EffectiveProvider = "cloud" | "enabled_no_key" | "demo_mock" | "extractive";

export interface AdminSettings {
  cloud_llm_enabled: boolean;
  provider_name: string | null;
  model: string | null;
  effective_provider: EffectiveProvider;
  acknowledged_at: string | null;
}

export interface AdminCoverage {
  documents_by_status: Record<string, number>;
  records: { total: number; verified: number; awaiting_verification: number; needs_review: number };
  open_dedup_candidates: number;
  review_queue_count: number;
}
