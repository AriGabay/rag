-- Initial schema for the appraisal knowledge engine MVP.
-- Runs as rag_owner. See docs/architecture.md for the RLS model (KTD3-KTD6).

-- ---------------------------------------------------------------------------
-- Tenant context helpers. Missing or empty GUC -> NULL -> policies match nothing.
-- ---------------------------------------------------------------------------
CREATE FUNCTION app_office() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT NULLIF(current_setting('app.office_id', true), '')::uuid $$;
CREATE FUNCTION app_user() RETURNS uuid LANGUAGE sql STABLE AS
$$ SELECT NULLIF(current_setting('app.user_id', true), '')::uuid $$;
CREATE FUNCTION app_role() RETURNS text LANGUAGE sql STABLE AS
$$ SELECT NULLIF(current_setting('app.role', true), '') $$;

-- ---------------------------------------------------------------------------
-- Platform (domain-agnostic): offices, users, groups, sessions, documents.
-- ---------------------------------------------------------------------------
CREATE TABLE offices (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE office_settings (
  office_id uuid PRIMARY KEY REFERENCES offices(id),
  cloud_llm_enabled boolean NOT NULL DEFAULT false,
  cloud_provider text,
  acknowledged_by uuid,
  acknowledged_at timestamptz,
  settings_version integer NOT NULL DEFAULT 1
);

CREATE TABLE office_data_versions (
  office_id uuid PRIMARY KEY REFERENCES offices(id),
  version bigint NOT NULL DEFAULT 1
);

CREATE TABLE users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  office_id uuid NOT NULL REFERENCES offices(id),
  email text NOT NULL,
  full_name text NOT NULL,
  password_hash text NOT NULL,
  role text NOT NULL CHECK (role IN ('admin', 'employee')),
  can_upload boolean NOT NULL DEFAULT false,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX users_email_uq ON users (lower(email));

CREATE TABLE document_groups (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  office_id uuid NOT NULL REFERENCES offices(id),
  name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (office_id, name)
);

CREATE TABLE user_groups (
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  group_id uuid NOT NULL REFERENCES document_groups(id) ON DELETE CASCADE,
  office_id uuid NOT NULL REFERENCES offices(id),
  PRIMARY KEY (user_id, group_id)
);

CREATE TABLE sessions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  token_hash text NOT NULL UNIQUE,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  office_id uuid NOT NULL REFERENCES offices(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  expires_at timestamptz NOT NULL
);

CREATE TABLE documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  office_id uuid NOT NULL REFERENCES offices(id),
  group_id uuid NOT NULL REFERENCES document_groups(id),
  title text NOT NULL,
  created_by uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  deleted_at timestamptz,
  deleted_by uuid REFERENCES users(id)
);
CREATE INDEX documents_office_group_idx ON documents (office_id, group_id);

CREATE TABLE document_versions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  office_id uuid NOT NULL REFERENCES offices(id),
  document_id uuid NOT NULL REFERENCES documents(id),
  version_no integer NOT NULL,
  sha256 text NOT NULL,
  filename text NOT NULL,
  mime_type text NOT NULL,
  size_bytes bigint NOT NULL,
  storage_key text NOT NULL,
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'processing', 'ready', 'needs_review', 'failed', 'superseded')),
  status_reason text,
  page_count integer,
  pages_incomplete integer NOT NULL DEFAULT 0,
  records_total integer NOT NULL DEFAULT 0,
  records_needing_review integer NOT NULL DEFAULT 0,
  is_current boolean NOT NULL DEFAULT false,
  extraction_version text,
  cloned_from_version_id uuid,
  uploaded_by uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz,
  UNIQUE (document_id, version_no)
);
CREATE INDEX document_versions_hash_idx ON document_versions (office_id, sha256);

CREATE TABLE pages (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  office_id uuid NOT NULL REFERENCES offices(id),
  document_id uuid NOT NULL REFERENCES documents(id),
  version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  page_no integer NOT NULL,
  text text NOT NULL DEFAULT '',
  method text NOT NULL CHECK (method IN ('text_layer', 'ocr', 'docx', 'failed')),
  quality numeric(5, 4) NOT NULL DEFAULT 0,
  ok boolean NOT NULL DEFAULT false,
  UNIQUE (version_id, page_no)
);

CREATE TABLE extracted_tables (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  office_id uuid NOT NULL REFERENCES offices(id),
  document_id uuid NOT NULL REFERENCES documents(id),
  version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  table_index integer NOT NULL,
  page_start integer,
  page_end integer,
  structure jsonb NOT NULL,   -- {headers:[...], units:[...], rows:[{page, cells:[...]}], ocr}
  UNIQUE (version_id, table_index)
);

CREATE TABLE chunks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  office_id uuid NOT NULL REFERENCES offices(id),
  document_id uuid NOT NULL REFERENCES documents(id),
  version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  chunk_index integer NOT NULL,
  kind text NOT NULL CHECK (kind IN ('text', 'table_row')),
  page_list integer[],        -- physical PDF pages; NULL for DOCX
  section text,
  text text NOT NULL,
  normalized_text text NOT NULL,
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', normalized_text)) STORED,
  embedding vector(384),
  embedding_model text,
  UNIQUE (version_id, chunk_index)
);
CREATE INDEX chunks_tsv_idx ON chunks USING gin (tsv);
CREATE INDEX chunks_trgm_idx ON chunks USING gin (normalized_text gin_trgm_ops);
CREATE INDEX chunks_embedding_idx ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX chunks_office_idx ON chunks (office_id, version_id);

CREATE TABLE jobs (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  office_id uuid NOT NULL REFERENCES offices(id),
  version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  kind text NOT NULL CHECK (kind IN ('process')),
  status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed')),
  attempts integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 3,
  run_after timestamptz NOT NULL DEFAULT now(),
  locked_by text,
  lease_until timestamptz,
  last_error text,
  idempotency_key text NOT NULL UNIQUE,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX jobs_claim_idx ON jobs (status, run_after);

CREATE TABLE audit_events (
  id bigserial PRIMARY KEY,
  office_id uuid NOT NULL REFERENCES offices(id),
  user_id uuid,
  action text NOT NULL,
  target_type text,
  target_id text,
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE provider_usage (
  id bigserial PRIMARY KEY,
  office_id uuid NOT NULL REFERENCES offices(id),
  provider text NOT NULL,
  model text,
  purpose text NOT NULL,
  input_tokens integer,
  output_tokens integer,
  latency_ms integer,
  ok boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Appraisal domain: transactions/valuations (unique facts), their occurrences
-- in documents, per-occurrence field values with provenance, dedup review.
-- ---------------------------------------------------------------------------
CREATE TABLE transactions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  office_id uuid NOT NULL REFERENCES offices(id),
  data_kind text NOT NULL
    CHECK (data_kind IN ('transaction_price', 'appraised_value', 'asking_price', 'adjusted_comparable')),
  match_key text,           -- set only when a certain-match key could be built
  price numeric(16, 2),
  currency text,
  area numeric(12, 2),
  area_type text CHECK (area_type IN ('net', 'gross', 'registered', 'equivalent', 'other')),
  transaction_date date,
  valuation_date date,
  report_date date,
  price_per_sqm numeric(14, 2),   -- computed = price / area, lineage in calc_definition
  calc_definition text,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX transactions_match_key_uq ON transactions (office_id, match_key) WHERE match_key IS NOT NULL;

CREATE TABLE occurrences (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  office_id uuid NOT NULL REFERENCES offices(id),
  transaction_id uuid NOT NULL REFERENCES transactions(id),
  document_id uuid NOT NULL REFERENCES documents(id),
  version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  record_index integer NOT NULL,
  page_no integer,
  table_index integer,
  row_index integer,
  text_span text,
  extraction_version text NOT NULL,
  -- normalized per-occurrence values (filters/display read these, KTD2)
  data_kind text NOT NULL,
  city text,
  neighborhood text,
  address text,
  block text,
  parcel text,
  sub_parcel text,
  property_type text,
  rooms numeric(4, 1),
  transaction_date date,
  valuation_date date,
  report_date date,
  area numeric(12, 2),
  area_type text,
  price numeric(16, 2),
  currency text,
  vat_basis text,
  price_per_sqm_stated numeric(14, 2),
  price_per_sqm_computed numeric(14, 2),
  conflict_flag boolean NOT NULL DEFAULT false,
  missing_critical text[] NOT NULL DEFAULT '{}',
  ocr boolean NOT NULL DEFAULT false,
  verification_status text NOT NULL DEFAULT 'auto_extracted'
    CHECK (verification_status IN ('auto_extracted', 'needs_review', 'human_verified', 'corrected', 'rejected')),
  verified_by uuid,
  verified_at timestamptz,
  review_note text,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (version_id, record_index)
);
CREATE INDEX occurrences_tx_idx ON occurrences (transaction_id);
CREATE INDEX occurrences_filter_idx ON occurrences (office_id, city, neighborhood);

CREATE TABLE fact_values (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  office_id uuid NOT NULL REFERENCES offices(id),
  document_id uuid NOT NULL REFERENCES documents(id),
  occurrence_id uuid NOT NULL REFERENCES occurrences(id) ON DELETE CASCADE,
  field text NOT NULL,
  original_text text,
  normalized_value text,
  source_path jsonb NOT NULL,   -- {version_id, page, table_index, row_index, col, span}
  extraction_version text NOT NULL,
  status text NOT NULL DEFAULT 'extracted' CHECK (status IN ('extracted', 'computed', 'corrected')),
  corrected_by uuid,
  corrected_at timestamptz,
  note text,
  previous jsonb NOT NULL DEFAULT '[]'::jsonb,
  UNIQUE (occurrence_id, field)
);

CREATE TABLE dedup_candidates (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  office_id uuid NOT NULL REFERENCES offices(id),
  transaction_a uuid NOT NULL REFERENCES transactions(id),
  transaction_b uuid NOT NULL REFERENCES transactions(id),
  reason text NOT NULL,
  status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'merged', 'kept_separate')),
  resolved_by uuid,
  resolved_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (transaction_a, transaction_b)
);

-- ---------------------------------------------------------------------------
-- Answering: conversations, questions, sources, cache.
-- ---------------------------------------------------------------------------
CREATE TABLE conversations (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  office_id uuid NOT NULL REFERENCES offices(id),
  user_id uuid NOT NULL REFERENCES users(id),
  title text,
  confirmed_conditions jsonb,
  pending_clarification jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE questions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  office_id uuid NOT NULL REFERENCES offices(id),
  conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  user_id uuid NOT NULL REFERENCES users(id),
  question_text text NOT NULL,
  intent text,
  parse_route text,
  conditions jsonb,
  answer jsonb,
  answer_kind text,
  data_version bigint,
  scope_hash text,
  latency_ms integer,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE answer_sources (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  office_id uuid NOT NULL REFERENCES offices(id),
  question_id uuid NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
  document_id uuid NOT NULL REFERENCES documents(id),
  version_id uuid NOT NULL,
  occurrence_id uuid,
  chunk_id uuid,
  page_list integer[]
);

CREATE TABLE answer_cache (
  office_id uuid NOT NULL REFERENCES offices(id),
  cache_key text NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (office_id, cache_key)
);

-- ---------------------------------------------------------------------------
-- Row level security. FORCE so even the owner is bound; rag_app never bypasses.
-- ---------------------------------------------------------------------------
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'office_settings', 'office_data_versions', 'users', 'document_groups', 'user_groups', 'sessions',
    'jobs', 'audit_events', 'provider_usage', 'dedup_candidates', 'conversations', 'questions',
    'answer_cache'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY tenant_isolation ON %I USING (office_id = app_office()) WITH CHECK (office_id = app_office())', t);
  END LOOP;
END $$;

ALTER TABLE offices ENABLE ROW LEVEL SECURITY;
ALTER TABLE offices FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON offices USING (id = app_office()) WITH CHECK (id = app_office());

-- Documents: office + document-group permission (KTD5).
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;
CREATE POLICY document_access ON documents
  USING (
    office_id = app_office()
    AND (
      app_role() IN ('admin', 'system')
      OR (app_role() = 'employee'
          AND group_id IN (SELECT ug.group_id FROM user_groups ug WHERE ug.user_id = app_user()))
    )
  )
  WITH CHECK (office_id = app_office());

-- Content tables reach documents through the RLS-filtered documents table.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'document_versions', 'pages', 'extracted_tables', 'chunks', 'occurrences', 'fact_values', 'answer_sources'
  ] LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format(
      'CREATE POLICY document_access ON %I USING (office_id = app_office() '
      'AND EXISTS (SELECT 1 FROM documents d WHERE d.id = document_id)) '
      'WITH CHECK (office_id = app_office())', t);
  END LOOP;
END $$;

-- A transaction is visible only through an occurrence the user may see.
ALTER TABLE transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE transactions FORCE ROW LEVEL SECURITY;
CREATE POLICY transaction_access ON transactions
  USING (
    office_id = app_office()
    AND (app_role() = 'system'
         OR EXISTS (SELECT 1 FROM occurrences o WHERE o.transaction_id = transactions.id))
  )
  WITH CHECK (office_id = app_office());

-- ---------------------------------------------------------------------------
-- Privileges for the runtime role.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO rag_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO rag_app;
GRANT EXECUTE ON FUNCTION app_office(), app_user(), app_role() TO rag_app, rag_lookup;

-- ---------------------------------------------------------------------------
-- Narrow cross-tenant lookups (KTD6). Owned by rag_lookup (NOLOGIN BYPASSRLS).
-- ---------------------------------------------------------------------------
GRANT SELECT (id, office_id, email, role, password_hash, is_active) ON users TO rag_lookup;
GRANT SELECT ON sessions TO rag_lookup;
GRANT SELECT, UPDATE ON jobs TO rag_lookup;
GRANT SELECT (id, office_id, document_id, sha256, status, created_at) ON document_versions TO rag_lookup;
GRANT SELECT (id, deleted_at, group_id) ON documents TO rag_lookup;
GRANT SELECT, INSERT ON offices, office_settings, office_data_versions, document_groups, users TO rag_lookup;

CREATE FUNCTION auth_login_lookup(p_email text)
RETURNS TABLE (user_id uuid, office_id uuid, role text, password_hash text, is_active boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS
$$ SELECT u.id, u.office_id, u.role, u.password_hash, u.is_active FROM users u WHERE lower(u.email) = lower(p_email) $$;

CREATE FUNCTION auth_resolve_session(p_token_hash text)
RETURNS TABLE (session_id uuid, user_id uuid, office_id uuid, role text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS
$$ SELECT s.id, s.user_id, s.office_id, u.role
     FROM sessions s JOIN users u ON u.id = s.user_id
    WHERE s.token_hash = p_token_hash AND s.expires_at > now() AND u.is_active $$;

CREATE FUNCTION jobs_claim(p_worker text, p_lease_seconds integer)
RETURNS TABLE (job_id uuid, office_id uuid, version_id uuid, kind text, attempts integer, max_attempts integer)
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp AS
$$
  UPDATE jobs j
     SET status = 'running', locked_by = p_worker, attempts = j.attempts + 1,
         lease_until = now() + make_interval(secs => p_lease_seconds), updated_at = now()
   WHERE j.id = (
         SELECT c.id FROM jobs c
          WHERE (c.status = 'queued' AND c.run_after <= now())
             OR (c.status = 'running' AND c.lease_until < now())
          ORDER BY c.created_at
          FOR UPDATE SKIP LOCKED
          LIMIT 1)
  RETURNING j.id, j.office_id, j.version_id, j.kind, j.attempts, j.max_attempts
$$;

-- In-office hash lookup over live (not deleted, not failed) versions. Office comes from the
-- caller's GUC, never a parameter, so it cannot probe another office.
CREATE FUNCTION docs_hash_lookup(p_sha256 text)
RETURNS TABLE (version_id uuid, document_id uuid, group_id uuid, status text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS
$$ SELECT v.id, v.document_id, d.group_id, v.status
     FROM document_versions v JOIN documents d ON d.id = v.document_id
    WHERE v.office_id = app_office() AND v.sha256 = p_sha256
      AND d.deleted_at IS NULL AND v.status NOT IN ('failed', 'superseded')
    ORDER BY v.created_at $$;

-- Bootstrap a new office with its first admin. Executable by rag_owner only (seed/ops), not rag_app.
CREATE FUNCTION bootstrap_office(p_name text, p_admin_email text, p_admin_name text, p_password_hash text)
RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp AS
$$
DECLARE v_office uuid;
BEGIN
  INSERT INTO offices (name) VALUES (p_name) RETURNING id INTO v_office;
  INSERT INTO office_settings (office_id) VALUES (v_office);
  INSERT INTO office_data_versions (office_id) VALUES (v_office);
  INSERT INTO users (office_id, email, full_name, password_hash, role, can_upload)
       VALUES (v_office, p_admin_email, p_admin_name, p_password_hash, 'admin', true);
  INSERT INTO document_groups (office_id, name) VALUES (v_office, 'כללי');
  RETURN v_office;
END
$$;

ALTER FUNCTION auth_login_lookup(text) OWNER TO rag_lookup;
ALTER FUNCTION auth_resolve_session(text) OWNER TO rag_lookup;
ALTER FUNCTION jobs_claim(text, integer) OWNER TO rag_lookup;
ALTER FUNCTION docs_hash_lookup(text) OWNER TO rag_lookup;
ALTER FUNCTION bootstrap_office(text, text, text, text) OWNER TO rag_lookup;

REVOKE ALL ON FUNCTION auth_login_lookup(text), auth_resolve_session(text), jobs_claim(text, integer),
  docs_hash_lookup(text), bootstrap_office(text, text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_login_lookup(text), auth_resolve_session(text), jobs_claim(text, integer),
  docs_hash_lookup(text) TO rag_app;
GRANT EXECUTE ON FUNCTION bootstrap_office(text, text, text, text) TO rag_owner;
