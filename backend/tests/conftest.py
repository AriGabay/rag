"""Shared test setup.

Integration tests run against the ``rag_test`` database of the Compose Postgres. Migrations run
once as ``rag_owner``; tests then talk to the database as ``rag_app`` (no RLS bypass), exactly
like the running app. Between tests every table is truncated by the owner.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://rag_app:change-me-app@localhost:5433/rag_test")
os.environ.setdefault("OWNER_DATABASE_URL", "postgresql+psycopg://rag_owner:change-me-owner@localhost:5433/rag_test")
os.environ.setdefault("EMBEDDING_PROVIDER", "hash")
os.environ.setdefault("DEMO_MODE", "true")
os.environ.setdefault("SESSION_SECRET", "test-secret")

import tempfile  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

os.environ.setdefault("FILE_STORAGE_ROOT", tempfile.mkdtemp(prefix="rag-files-"))

from app.config import get_settings  # noqa: E402
from app.db import reset_engine  # noqa: E402

_TABLES = (
    "answer_cache, answer_sources, questions, conversations, dedup_candidates, fact_values, occurrences, "
    "transactions, provider_usage, audit_events, jobs, chunks, extracted_tables, pages, document_versions, "
    "documents, sessions, user_groups, document_groups, users, office_data_versions, office_settings, offices, "
    "attribute_definitions, facts, fact_extraction_ledger"
)


def _db_available() -> bool:
    try:
        engine = create_engine(get_settings().owner_database_url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:  # noqa: BLE001
        return False


def alembic_config():
    """Alembic config bound to the test database (owner role)."""
    from alembic.config import Config

    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    cfg.attributes["url"] = get_settings().owner_database_url
    return cfg


@pytest.fixture(scope="session")
def owner_engine():
    if not _db_available():
        pytest.skip("Postgres test database not reachable (start: docker compose up -d db)")
    from alembic import command

    command.upgrade(alembic_config(), "head")
    engine = create_engine(get_settings().owner_database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def db(owner_engine):
    """Clean database for one test. Yields the owner engine (for bootstrap/inspection only)."""
    with owner_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {_TABLES} CASCADE"))
    reset_engine()
    yield owner_engine
    reset_engine()


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


def login(client, email: str, password: str = "secret-pass"):
    """Log ``client`` in and return it (the cookie jar now holds the session)."""
    r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return client
