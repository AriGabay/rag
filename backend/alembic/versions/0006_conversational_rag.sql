-- Conversational RAG: document structure (blocks, ingestion report), meaning-preserving measurements, and
-- chat messages with server-side progress and cancellation.
-- Runs as rag_owner. Every new table gets ENABLE + FORCE RLS and explicit grants (no default privileges).

-- ---------------------------------------------------------------------------
-- Document structure: every block of a document in reading order (DOCX has sections and paragraphs, not
-- pages), chunks that point at their block range, and what was read of each version.
-- ---------------------------------------------------------------------------
CREATE TABLE document_blocks (
  office_id uuid NOT NULL REFERENCES offices(id),
  document_id uuid NOT NULL REFERENCES documents(id),
  version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  block_index integer NOT NULL,
  kind text NOT NULL CHECK (kind IN ('heading', 'paragraph', 'textbox', 'table', 'image')),
  section text,
  section_path text[] NOT NULL DEFAULT '{}',
  label text,
  paragraph_no integer,
  page integer,
  media text,
  source text NOT NULL DEFAULT 'text',
  status text NOT NULL DEFAULT 'read'
    CHECK (status IN ('read', 'read_uncertain', 'no_text', 'decorative', 'unread')),
  note text,
  table_index integer,
  text text NOT NULL DEFAULT '',
  PRIMARY KEY (version_id, block_index)
);
CREATE INDEX document_blocks_office_idx ON document_blocks (office_id, version_id);

ALTER TABLE chunks DROP CONSTRAINT chunks_kind_check;
ALTER TABLE chunks ADD CONSTRAINT chunks_kind_check CHECK (kind IN ('text', 'table_row', 'table', 'image'));
ALTER TABLE chunks ADD COLUMN block_start integer, ADD COLUMN block_end integer;

-- {blocks: {kind: n}, images: {status: n}, image_methods: {...}, unread: [...], tables: n, partial: bool}
ALTER TABLE document_versions ADD COLUMN ingestion jsonb;

-- ---------------------------------------------------------------------------
-- Measurements: one stated quantity with its meaning. A value is never just "a price per m²": it keeps the
-- metric as written, its kind, unit, period, area basis, VAT status, form (exact/approximate/range), the
-- subject it describes and the role of the statement (the appraiser's determination, a comparable, an asking
-- price, a survey figure...), with its quote and location. Values from one sentence or table row share a
-- statement_key, so they stay apart without losing their relation.
-- ---------------------------------------------------------------------------
CREATE TABLE measurements (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  office_id uuid NOT NULL REFERENCES offices(id),
  document_id uuid NOT NULL REFERENCES documents(id),
  version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  block_index integer,
  table_index integer,
  row_index integer,
  statement_key text NOT NULL,
  metric text NOT NULL,
  metric_kind text NOT NULL,
  value numeric,
  value_low numeric,
  value_high numeric,
  value_form text NOT NULL DEFAULT 'exact'
    CHECK (value_form IN ('exact', 'approximate', 'range', 'minimum', 'maximum')),
  value_text text NOT NULL,
  unit text,
  period text NOT NULL DEFAULT 'unknown' CHECK (period IN ('month', 'year', 'one_time', 'none', 'unknown')),
  area_basis text,
  vat text NOT NULL DEFAULT 'unknown' CHECK (vat IN ('included', 'excluded', 'unknown', 'not_applicable')),
  subject text,
  subject_role text NOT NULL DEFAULT 'other'
    CHECK (subject_role IN ('appraised_property', 'comparable', 'survey', 'asking', 'contract', 'general', 'other')),
  value_role text NOT NULL DEFAULT 'other'
    CHECK (value_role IN ('appraiser_determination', 'actual_contract', 'comparable_transaction', 'asking_price',
                          'survey_statistic', 'calculation', 'planning_legal', 'other')),
  effective_date text,
  quote text NOT NULL,
  section text,
  extraction_version text NOT NULL,
  model text,
  status text NOT NULL DEFAULT 'needs_review'
    CHECK (status IN ('auto_validated', 'needs_review', 'verified', 'corrected', 'rejected')),
  issues jsonb NOT NULL DEFAULT '[]'::jsonb,
  conflict jsonb,   -- a newer extraction that disagrees with this reviewed measurement
  reviewed_by uuid,
  reviewed_at timestamptz,
  review_note text,
  previous jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX measurements_kind_idx ON measurements (office_id, metric_kind, status);
CREATE INDEX measurements_version_idx ON measurements (version_id, extraction_version);

-- One run per (version, extraction version): which documents were read for measurements, and how far.
CREATE TABLE measurement_runs (
  office_id uuid NOT NULL REFERENCES offices(id),
  document_id uuid NOT NULL REFERENCES documents(id),
  version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  extraction_version text NOT NULL,
  state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'done', 'partial', 'failed')),
  passages_total integer NOT NULL DEFAULT 0,
  passages_done integer NOT NULL DEFAULT 0,
  found integer NOT NULL DEFAULT 0,
  detail jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (version_id, extraction_version)
);

ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN ('process', 'extract_facts', 'extract_measurements'));

-- ---------------------------------------------------------------------------
-- Chat: messages of a conversation (the user's and the assistant's), the assistant's progress while it works,
-- a cancellation the worker thread honours between steps, and per-conversation archive and summary.
-- ---------------------------------------------------------------------------
ALTER TABLE conversations
  ADD COLUMN archived_at timestamptz,
  ADD COLUMN summary text,
  ADD COLUMN summary_message_count integer NOT NULL DEFAULT 0,
  ADD COLUMN engine text NOT NULL DEFAULT 'legacy' CHECK (engine IN ('legacy', 'rag'));

CREATE TABLE messages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  office_id uuid NOT NULL REFERENCES offices(id),
  conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES users(id),
  role text NOT NULL CHECK (role IN ('user', 'assistant')),
  content text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'done'
    CHECK (status IN ('running', 'cancelling', 'cancelled', 'done', 'failed')),
  client_id uuid,
  reply_to uuid REFERENCES messages(id) ON DELETE CASCADE,
  progress jsonb NOT NULL DEFAULT '[]'::jsonb,
  answer jsonb,
  error text,
  cancel_requested boolean NOT NULL DEFAULT false,
  data_version bigint,
  scope_hash text,
  model text,
  usage jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);
CREATE INDEX messages_conversation_idx ON messages (conversation_id, created_at, id);
CREATE UNIQUE INDEX messages_client_uq ON messages (conversation_id, client_id) WHERE client_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Row level security and grants.
-- ---------------------------------------------------------------------------
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['document_blocks', 'measurements', 'measurement_runs'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY document_access ON %I USING (office_id = app_office() '
      'AND EXISTS (SELECT 1 FROM documents d WHERE d.id = document_id)) '
      'WITH CHECK (office_id = app_office())', t);
  END LOOP;
END $$;

ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
ALTER TABLE messages FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON messages USING (office_id = app_office()) WITH CHECK (office_id = app_office());
CREATE POLICY user_isolation ON messages AS RESTRICTIVE
  USING (user_id = app_user() OR app_role() = 'system') WITH CHECK (user_id = app_user() OR app_role() = 'system');

GRANT SELECT, INSERT, UPDATE, DELETE ON document_blocks, measurements, measurement_runs, messages TO rag_app;
