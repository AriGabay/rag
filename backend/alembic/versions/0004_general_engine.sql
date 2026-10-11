-- General question engine (KTD6): attribute registry, facts with provenance, extraction ledger,
-- conversation state, turn reservation, provider test results, extract_facts jobs.
-- Runs as rag_owner. Every new table gets ENABLE + FORCE RLS and explicit grants (no default privileges).

-- ---------------------------------------------------------------------------
-- Attribute registry (KTD7). Structured entries map to a whitelisted record column; extracted
-- entries are read from documents on demand. facts_version is bumped per attribute (KTD9).
-- ---------------------------------------------------------------------------
CREATE TABLE attribute_definitions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  office_id uuid NOT NULL REFERENCES offices(id),
  key text NOT NULL,
  label_he text NOT NULL,
  aliases text[] NOT NULL DEFAULT '{}',
  value_type text NOT NULL CHECK (value_type IN ('numeric', 'text', 'boolean', 'date')),
  unit_dimension text,
  canonical_unit text,
  source text NOT NULL CHECK (source IN ('structured', 'extracted')),
  structured_column text CHECK (structured_column IN (
    'transactions.price_per_sqm', 'transactions.price', 'transactions.area', 'occurrences.rooms')),
  extraction_prompt_version text,
  status text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'active')),
  facts_version bigint NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (office_id, key),
  CHECK ((source = 'structured') = (structured_column IS NOT NULL))
);

-- ---------------------------------------------------------------------------
-- Facts: one extracted value per mention, with verbatim quote and source path (KTD8).
-- ---------------------------------------------------------------------------
CREATE TABLE facts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  office_id uuid NOT NULL REFERENCES offices(id),
  document_id uuid NOT NULL REFERENCES documents(id),
  version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  attribute_id uuid NOT NULL REFERENCES attribute_definitions(id) ON DELETE CASCADE,
  entity_role text NOT NULL DEFAULT 'subject',
  entity_key text,
  entity_descriptor text,
  value_numeric numeric,
  value_text text,
  unit text,
  canonical_value numeric,
  quote text NOT NULL,
  source_path jsonb NOT NULL,   -- {page, chunk_id, table_index, row_index, col}
  extraction_version text NOT NULL,
  model text,
  status text NOT NULL DEFAULT 'needs_review'
    CHECK (status IN ('auto_validated', 'needs_review', 'verified', 'corrected', 'rejected')),
  reviewed_by uuid,
  reviewed_at timestamptz,
  review_note text,
  previous jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX facts_attribute_idx ON facts (office_id, attribute_id, status);
CREATE INDEX facts_version_idx ON facts (version_id, attribute_id);

-- One state per (version, attribute, extraction version) (KTD8 step 7).
CREATE TABLE fact_extraction_ledger (
  office_id uuid NOT NULL REFERENCES offices(id),
  document_id uuid NOT NULL REFERENCES documents(id),
  version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  attribute_id uuid NOT NULL REFERENCES attribute_definitions(id) ON DELETE CASCADE,
  extraction_version text NOT NULL,
  state text NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending', 'found', 'not_stated', 'partial_scan', 'failed')),
  char_budget integer,
  detail jsonb NOT NULL DEFAULT '{}'::jsonb,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (version_id, attribute_id, extraction_version)
);
CREATE INDEX fact_extraction_ledger_attribute_idx ON fact_extraction_ledger (office_id, attribute_id, extraction_version, state);

-- ---------------------------------------------------------------------------
-- Column changes on existing tables.
-- ---------------------------------------------------------------------------
ALTER TABLE conversations
  ADD COLUMN state jsonb NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN state_version integer NOT NULL DEFAULT 0;

ALTER TABLE questions
  ADD COLUMN turn_id uuid,
  ADD COLUMN status text NOT NULL DEFAULT 'done' CHECK (status IN ('pending', 'done', 'failed')),
  ADD COLUMN plan jsonb,
  ADD COLUMN steps jsonb,
  ADD COLUMN facts_versions jsonb;   -- {attribute_id: facts_version used}
CREATE UNIQUE INDEX questions_turn_uq ON questions (conversation_id, turn_id) WHERE turn_id IS NOT NULL;

ALTER TABLE office_settings
  ADD COLUMN provider_test_provider text,
  ADD COLUMN provider_test_model text,
  ADD COLUMN provider_test_ok boolean,
  ADD COLUMN provider_test_status text,
  ADD COLUMN provider_tested_at timestamptz;

ALTER TABLE chunks
  ADD COLUMN table_index integer,
  ADD COLUMN row_index integer;

ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN ('process', 'extract_facts'));
ALTER TABLE jobs ADD COLUMN payload jsonb;

-- ---------------------------------------------------------------------------
-- Row level security for the new tables, and per-user conversations/questions.
-- ---------------------------------------------------------------------------
ALTER TABLE attribute_definitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE attribute_definitions FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON attribute_definitions
  USING (office_id = app_office()) WITH CHECK (office_id = app_office());

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['facts', 'fact_extraction_ledger'] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY document_access ON %I USING (office_id = app_office() '
      'AND EXISTS (SELECT 1 FROM documents d WHERE d.id = document_id)) '
      'WITH CHECK (office_id = app_office())', t);
  END LOOP;
END $$;

-- Restrictive: ANDed with tenant_isolation, so a user (admins included) sees and writes only own rows.
-- The server-only system context (no user; worker, maintenance) keeps office-wide access, as in
-- the documents and transactions policies.
CREATE POLICY user_isolation ON conversations AS RESTRICTIVE
  USING (user_id = app_user() OR app_role() = 'system') WITH CHECK (user_id = app_user() OR app_role() = 'system');
CREATE POLICY user_isolation ON questions AS RESTRICTIVE
  USING (user_id = app_user() OR app_role() = 'system') WITH CHECK (user_id = app_user() OR app_role() = 'system');

GRANT SELECT, INSERT, UPDATE, DELETE ON attribute_definitions, facts, fact_extraction_ledger TO rag_app;

-- ---------------------------------------------------------------------------
-- jobs_claim: also returns kind and payload; process jobs before extract_facts jobs so ingestion
-- is never starved. Keeps the 0003 max-attempts logic; an exhausted extract_facts job never
-- fails its document version. The return type changes, so the function is recreated.
-- ---------------------------------------------------------------------------
DROP FUNCTION jobs_claim(text, integer);
CREATE FUNCTION jobs_claim(p_worker text, p_lease_seconds integer)
RETURNS TABLE (job_id uuid, office_id uuid, version_id uuid, kind text, payload jsonb, attempts integer,
               max_attempts integer)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp AS
$fn$
BEGIN
  -- Exhausted jobs whose worker died: terminal, and so is the version of a process job.
  WITH dead AS (
    UPDATE jobs j SET status = 'failed', locked_by = NULL, lease_until = NULL, updated_at = now(),
           last_error = 'lease expired after max attempts'
     WHERE j.status = 'running' AND j.lease_until < now() AND j.attempts >= j.max_attempts
    RETURNING j.version_id, j.kind)
  UPDATE document_versions v SET status = 'failed', status_reason = 'העיבוד נכשל שוב ושוב (ייתכן שהקובץ גורם לקריסה). ניתן להעלות אותו מחדש', processed_at = now()
    FROM dead WHERE v.id = dead.version_id AND dead.kind = 'process' AND v.status IN ('pending', 'processing');

  RETURN QUERY
  UPDATE jobs j
     SET status = 'running', locked_by = p_worker, attempts = j.attempts + 1,
         lease_until = now() + make_interval(secs => p_lease_seconds), updated_at = now()
   WHERE j.id = (
         SELECT c.id FROM jobs c
          WHERE (c.status = 'queued' AND c.run_after <= now())
             OR (c.status = 'running' AND c.lease_until < now() AND c.attempts < c.max_attempts)
          ORDER BY (c.kind <> 'process'), c.created_at
          FOR UPDATE SKIP LOCKED
          LIMIT 1)
  RETURNING j.id, j.office_id, j.version_id, j.kind, j.payload, j.attempts, j.max_attempts;
END
$fn$;
ALTER FUNCTION jobs_claim(text, integer) OWNER TO rag_lookup;
REVOKE ALL ON FUNCTION jobs_claim(text, integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION jobs_claim(text, integer) TO rag_app;
