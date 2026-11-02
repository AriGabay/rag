"""Migration 0016: page geometry, word spans, value stance, the ``verified_values`` cache, the ``kept_previous`` and
``positions`` job values and OCR languages in reading-cache keys come and go cleanly; row security stays forced; a
verified value is reachable only through a document the user may see; and the readings of every office keep being
found under their rewritten keys. Synthetic data only."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.db import reset_engine, tenant_tx
from tests.conftest import alembic_config
from tests.factories import make_document, make_group, make_office, make_user

pytestmark = pytest.mark.db

PAGE_COLUMNS = {"mediabox", "cropbox", "rotation", "display_width", "display_height", "printed_label",
                "geometry_issue"}


def _columns(db, table: str) -> set[str]:
    with db.connect() as conn:
        return {r.column_name for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = :t"), {"t": table})}


def _forced(db, table: str) -> bool:
    with db.connect() as conn:
        return bool(conn.execute(text("SELECT relforcerowsecurity AND relrowsecurity FROM pg_class"
                                      " WHERE relname = :t"), {"t": table}).scalar())


def _exists(db, table: str) -> bool:
    with db.connect() as conn:
        return conn.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": table}).scalar_one()


def _image_reading(conn, digest: str, config: str) -> None:
    conn.execute(text(
        "INSERT INTO image_readings (office_id, content_hash, reader_version, model_config, crop_scale, status,"
        " reading) VALUES (app_office(), :h, 'regions-v1', :m, '1.0', 'read', CAST('{\"text\": \"לוגו\"}' AS jsonb))"),
        {"h": digest, "m": config})


def _region_reading(conn, doc, ver, config: str) -> None:
    conn.execute(text(
        "INSERT INTO region_readings (office_id, document_id, version_id, reading_id, region, reader_version,"
        " model_config, page, status, reading) VALUES (app_office(), :d, :v, 'reading-1', 'block:3', 'inspect-v1',"
        " :m, 2, 'read', CAST('{\"text\": \"דמי שכירות 41,300\"}' AS jsonb))"), {"d": doc, "v": ver, "m": config})


def _configs(conn, table: str) -> list[str]:
    return sorted(r[0] for r in conn.execute(text(f"SELECT model_config FROM {table}")))


def _job(conn, ver, kind: str, status: str, key: str) -> None:
    conn.execute(text("INSERT INTO jobs (office_id, version_id, kind, status, idempotency_key)"
                      " VALUES (app_office(), :v, :k, :s, :i)"), {"v": ver, "k": kind, "s": status, "i": key})


def _verified(conn, doc, ver, locator: str = '{"block": 4, "span": [12, 21]}') -> None:
    conn.execute(text(
        "INSERT INTO verified_values (office_id, document_id, version_id, reading_id, locator, value)"
        " VALUES (app_office(), :d, :v, 'reading-1', CAST(:l AS jsonb),"
        " CAST('{\"value\": \"1,760,000\", \"unit\": \"₪\", \"quote\": \"ושוויה 1,760,000 ₪\"}' AS jsonb))"),
        {"d": doc, "v": ver, "l": locator})


def test_the_revision_comes_and_goes_and_rewrites_reading_keys_in_every_office(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")
    doc_a, ver_a = make_document(a, a.default_group_id, title="שומה סינתטית א")
    doc_b, ver_b = make_document(b, b.default_group_id, title="שומה סינתטית ב")
    assert PAGE_COLUMNS <= _columns(db, "pages") and "spans" in _columns(db, "document_blocks")
    assert {"stated_by", "stance"} <= _columns(db, "measurements")
    assert _exists(db, "verified_values") and _forced(db, "verified_values")
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0015")
        reset_engine()
        assert not PAGE_COLUMNS & _columns(db, "pages") and "spans" not in _columns(db, "document_blocks")
        assert not {"stated_by", "stance"} & _columns(db, "measurements") and not _exists(db, "verified_values")
        assert all(_forced(db, t) for t in ("pages", "document_blocks", "measurements", "jobs", "image_readings",
                                             "region_readings"))
        with tenant_tx(a.system) as conn:  # readings cached before the revision, in two offices
            _image_reading(conn, "logo-a", "none")
            _image_reading(conn, "chart-a", "scripted:low")
            _region_reading(conn, doc_a, ver_a, "scripted:low")
        with tenant_tx(b.system) as conn:
            _image_reading(conn, "logo-b", "none")
        with pytest.raises(DBAPIError), tenant_tx(a.system) as conn:
            _job(conn, ver_a, "positions", "queued", "positions-early")
    finally:
        command.upgrade(cfg, "head")
        reset_engine()

    assert PAGE_COLUMNS <= _columns(db, "pages") and _forced(db, "verified_values")
    with tenant_tx(a.system) as conn:
        assert _configs(conn, "image_readings") == ["none|ocr=heb+eng", "scripted:low|ocr=heb+eng"]
        assert _configs(conn, "region_readings") == ["scripted:low|ocr=heb+eng"]
        # the reading is found under the key that names its OCR languages, and only there
        assert conn.execute(text("SELECT count(*) FROM image_readings WHERE content_hash = 'logo-a'"
                                 " AND model_config = 'none|ocr=heb+eng'")).scalar_one() == 1
        _job(conn, ver_a, "positions", "queued", "positions-1")
        _job(conn, ver_a, "process", "kept_previous", "process-kept")
        with pytest.raises(DBAPIError), conn.begin_nested():
            _job(conn, ver_a, "process", "abandoned", "process-bad")
    with tenant_tx(b.system) as conn:
        assert _configs(conn, "image_readings") == ["none|ocr=heb+eng"]

    try:
        command.downgrade(cfg, "0015")
        reset_engine()
        with tenant_tx(a.system) as conn:  # back to the keys the older code reads
            assert _configs(conn, "image_readings") == ["none", "scripted:low"]
            assert _configs(conn, "region_readings") == ["scripted:low"]
            jobs = {(r.kind, r.status) for r in conn.execute(text("SELECT kind, status FROM jobs"))}
            assert jobs == {("process", "failed")}
        with tenant_tx(b.system) as conn:
            assert _configs(conn, "image_readings") == ["none"]
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    with tenant_tx(a.system) as conn:
        assert _configs(conn, "image_readings") == ["none|ocr=heb+eng", "scripted:low|ocr=heb+eng"]
    assert _forced(db, "verified_values") and _forced(db, "image_readings") and _forced(db, "region_readings")


def test_positions_and_stance_columns_hold_what_extraction_writes(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = make_document(a, a.default_group_id, title="שומה סינתטית")
    with tenant_tx(a.system) as conn:
        conn.execute(text(
            "INSERT INTO pages (office_id, document_id, version_id, page_no, text, method, ok, mediabox, cropbox,"
            " rotation, display_width, display_height, printed_label) VALUES (app_office(), :d, :v, 1, 'טקסט',"
            " 'text_layer', true, CAST('[36, 24, 877.89, 619.28]' AS jsonb), CAST('[46, 32, 865.89, 613.28]' AS jsonb),"
            " 90, 581.28, 819.89, '5')"), {"d": doc, "v": ver})
        conn.execute(text(
            "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, text, spans)"
            " VALUES (app_office(), :d, :v, 0, 'paragraph', 'שטח הדירה',"
            " CAST('[[0, 0, 0, 3, 520.8, 58.8, 542.0, 69.3], [0, 1, 4, 9, 489.8, 58.8, 517.5, 69.3]]' AS jsonb))"),
            {"d": doc, "v": ver})
        row = conn.execute(text("SELECT rotation, display_width, printed_label, geometry_issue, cropbox FROM pages")
                           ).one()
        assert row == (90, 581.28, "5", None, [46, 32, 865.89, 613.28])
        spans = conn.execute(text("SELECT spans FROM document_blocks")).scalar_one()
        assert spans[1][:4] == [0, 1, 4, 9]
        with pytest.raises(DBAPIError), conn.begin_nested():
            conn.execute(text("UPDATE pages SET rotation = 360"))
        conn.execute(text(
            "INSERT INTO measurements (office_id, document_id, version_id, statement_key, metric, metric_kind,"
            " value_text, quote, extraction_version, stated_by, stance) VALUES (app_office(), :d, :v, 'k1', 'שווי',"
            " 'value', '1,760,000', 'שוויה 1,760,000 ₪', 'test', 'השמאי', 'proposal')"), {"d": doc, "v": ver})
        with pytest.raises(DBAPIError), conn.begin_nested():
            conn.execute(text("UPDATE measurements SET stance = 'decided'"))
        conn.execute(text("UPDATE measurements SET stance = NULL, stated_by = NULL"))


def test_verified_values_follow_the_document_permissions(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")
    closed = make_group(a, "קבוצה סגורה")
    inside = make_user(a, "inside@example.test", [a.default_group_id, closed])
    outside = make_user(a, "outside@example.test", [a.default_group_id])
    doc, ver = make_document(a, closed, title="שומה סינתטית סגורה")
    with tenant_tx(a.ctx("employee", inside)) as conn:  # a user who may see the document stores and reads it
        _verified(conn, doc, ver)
        assert conn.execute(text("SELECT value->>'value' FROM verified_values")).scalar_one() == "1,760,000"
    with tenant_tx(a.ctx("employee", outside)) as conn:  # outside the group it does not exist
        assert conn.execute(text("SELECT count(*) FROM verified_values")).scalar_one() == 0
    with pytest.raises(DBAPIError), tenant_tx(a.ctx("employee", outside)) as conn:
        _verified(conn, doc, ver, '{"block": 5}')
    with tenant_tx(b.ctx()) as conn:  # another office sees nothing
        assert conn.execute(text("SELECT count(*) FROM verified_values")).scalar_one() == 0
    with pytest.raises(DBAPIError), tenant_tx(a.ctx()) as conn:  # one value per version, reading and locator
        _verified(conn, doc, ver)
    with tenant_tx(a.ctx()) as conn:
        _verified(conn, doc, ver, '{"table": 0, "row": 3, "col": 2}')
        assert conn.execute(text("SELECT count(*) FROM verified_values")).scalar_one() == 2
