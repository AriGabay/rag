#!/bin/bash
# Runs once, on first start of an empty data volume, as the postgres superuser.
# Creates the three roles of KTD3/KTD6 and the application + test databases.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<-SQL
  CREATE ROLE rag_owner LOGIN PASSWORD '${RAG_OWNER_PASSWORD}' NOBYPASSRLS;
  CREATE ROLE rag_app LOGIN PASSWORD '${RAG_APP_PASSWORD}' NOBYPASSRLS NOSUPERUSER NOCREATEDB NOCREATEROLE;
  -- Owns only the narrow SECURITY DEFINER lookup functions; never logs in.
  CREATE ROLE rag_lookup NOLOGIN BYPASSRLS;
  GRANT rag_lookup TO rag_owner;
SQL

for db in rag rag_test; do
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres <<-SQL
    CREATE DATABASE ${db} OWNER rag_owner ENCODING 'UTF8' LC_COLLATE 'en_US.utf8' LC_CTYPE 'en_US.utf8' TEMPLATE template0;
SQL
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" <<-SQL
    CREATE EXTENSION IF NOT EXISTS vector;
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
    REVOKE ALL ON SCHEMA public FROM PUBLIC;
    ALTER SCHEMA public OWNER TO rag_owner;
    GRANT USAGE ON SCHEMA public TO rag_app, rag_lookup;
    GRANT CREATE ON SCHEMA public TO rag_lookup;
    GRANT CONNECT ON DATABASE ${db} TO rag_app;
SQL
done
