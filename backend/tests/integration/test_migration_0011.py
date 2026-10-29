"""Migration 0011: ``document_blocks`` gains its provenance columns and ``pages.method`` accepts ``mixed``; both come
and go cleanly, blocks written before it keep empty provenance, and row security stays forced. Synthetic data
only."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.db import reset_engine, tenant_tx
from tests.conftest import alembic_config
from tests.factories import make_document, make_office

pytestmark = pytest.mark.db

NEW = {"bbox", "method", "reader_version", "content_hash", "original_text"}


def _columns(db) -> set[str]:
    with db.connect() as conn:
        return {r.column_name for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'document_blocks'"))}


def _forced(db, table: str) -> bool:
    with db.connect() as conn:
        return conn.execute(text("SELECT relforcerowsecurity FROM pg_class WHERE relname = :t"), {"t": table}
                            ).scalar_one()


def _page(conn, doc, ver, page_no: int, method: str) -> None:
    conn.execute(text("INSERT INTO pages (office_id, document_id, version_id, page_no, text, method, ok)"
                      " VALUES (app_office(), :d, :v, :n, 'טקסט', :m, true)"),
                 {"d": doc, "v": ver, "n": page_no, "m": method})


def test_block_provenance_and_mixed_pages_come_and_go(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")
    assert NEW <= _columns(db) and _forced(db, "document_blocks") and _forced(db, "pages")
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0010")
        reset_engine()
        assert not NEW & _columns(db) and _forced(db, "document_blocks")
        doc, ver = make_document(a, a.default_group_id, title="דוח סינתטי")
        with tenant_tx(a.system) as conn:  # a block and a page written before the revision
            conn.execute(text("INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind,"
                              " text) VALUES (app_office(), :d, :v, 0, 'paragraph', 'פסקה')"), {"d": doc, "v": ver})
            _page(conn, doc, ver, 1, "text_layer")
        with pytest.raises(IntegrityError), tenant_tx(a.system) as conn:
            _page(conn, doc, ver, 2, "mixed")
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    assert NEW <= _columns(db) and _forced(db, "document_blocks")
    with tenant_tx(a.system) as conn:
        row = conn.execute(text("SELECT bbox, method, reader_version, content_hash, original_text FROM document_blocks"
                                )).one()
        assert row == (None, None, None, None, None)
        _page(conn, doc, ver, 2, "mixed")
        conn.execute(text("INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, page,"
                          " status, bbox, method, reader_version, content_hash) VALUES (app_office(), :d, :v, 1,"
                          " 'image', 2, 'unread', CAST(:bb AS jsonb), 'none', 'pdf-blocks-v1', 'abc')"),
                     {"d": doc, "v": ver, "bb": "[10.5, 20, 110, 90.25]"})
        assert conn.execute(text("SELECT bbox FROM document_blocks WHERE block_index = 1")).scalar_one() == [
            10.5, 20, 110, 90.25]
    with tenant_tx(b.system) as conn:  # the tenant policy still isolates both tables
        assert conn.execute(text("SELECT count(*) FROM document_blocks")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM pages")).scalar() == 0
