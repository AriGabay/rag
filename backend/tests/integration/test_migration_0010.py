"""Migration 0010: ``provider_usage`` gains the cache buckets and the estimated cost, added and dropped cleanly;
rows logged before it keep an unknown (null) cost, and row security stays forced. Synthetic data only."""

import pytest
from sqlalchemy import text

from alembic import command
from app.db import reset_engine, tenant_tx
from tests.conftest import alembic_config
from tests.factories import make_office

pytestmark = pytest.mark.db

NEW = {"cached_input_tokens", "cache_write_tokens", "cost_usd"}


def _columns(db) -> set[str]:
    with db.connect() as conn:
        return {r.column_name for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'provider_usage'"))}


def _forced(db) -> bool:
    with db.connect() as conn:
        return conn.execute(text("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'provider_usage'")
                            ).scalar_one()


def test_the_cost_columns_come_and_go_and_old_rows_keep_an_unknown_cost(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")
    assert NEW <= _columns(db) and _forced(db)
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0009")
        reset_engine()
        assert not NEW & _columns(db) and _forced(db)
        with tenant_tx(a.system) as conn:  # a row logged before the revision
            conn.execute(text("INSERT INTO provider_usage (office_id, provider, model, purpose, input_tokens,"
                              " output_tokens, ok, status) VALUES (app_office(), 'openai', 'gpt-5.4-mini', 'agent',"
                              " 1000, 50, true, 'ok')"))
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    assert NEW <= _columns(db) and _forced(db)
    with tenant_tx(a.system) as conn:
        row = conn.execute(text("SELECT input_tokens, cached_input_tokens, cache_write_tokens, cost_usd"
                                " FROM provider_usage")).one()
        assert row.input_tokens == 1000 and row.cached_input_tokens is None and row.cache_write_tokens is None
        assert row.cost_usd is None  # never back-filled as zero
    with tenant_tx(b.system) as conn:  # the tenant policy still isolates the table
        assert conn.execute(text("SELECT count(*) FROM provider_usage")).scalar() == 0
