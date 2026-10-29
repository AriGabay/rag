"""People's decisions on measurements survive reprocessing (U12, R26): a reprocess that keeps the previous reading
leaves every measurement as it was, and one an admin applies afterwards keeps a verification and a rejection (a
rejected value does not come back when the version is measured again). A value is shown as checked by a person only
when a person reviewed it (R25). Synthetic readings from a scripted reader only; no model is called."""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import text

from app import worker
from app.db import tenant_tx
from app.measurements.extract import Row
from app.measurements.store import REVIEWED, store_rows
from tests.integration.test_reindex import _job, _reprocess, make_gated
from tests.integration.test_reprocess_gate import (
    AREA,
    CORRECTIONS,
    LOST_120,
    RENT_OLD,
    VALUE_OLD,
    add_measurement,
    block_of,
    measurement,
    reading,
)

pytestmark = pytest.mark.db


@pytest.fixture
def gated(db, client, monkeypatch):
    return make_gated(db, client, monkeypatch)


def _rent_row(block: int) -> Row:
    """What a fresh extraction finds again for the rent a person rejected: the same statement and number."""
    return Row(metric="דמי שכירות", metric_kind="value", value=Decimal("55"), low=None, high=None, form="exact",
               value_text="55 ₪", unit="ILS", period="month", area_basis=None, vat="unknown", subject=None,
               subject_role="appraised_property", value_role="other", effective_date=None, quote=RENT_OLD, section=None,
               block_index=block, table_index=None, row_index=None, statement_key=f"b{block}:x")


def _state(office, mid: str) -> tuple:
    m = measurement(office, mid)[0]
    return m.status, m.block_index, m.anchor_lost, m.reviewed_by


def test_a_kept_reprocess_leaves_every_decision_and_an_applied_one_keeps_them(gated, client):
    with tenant_tx(gated.system) as conn:
        reviewer = conn.execute(text("SELECT id FROM users WHERE role = 'admin'")).scalar_one()
    value = add_measurement(gated, "verified", VALUE_OLD, "1,250,000 ₪", 1250000, block_of(gated, "1,250,000"))
    rent_block = block_of(gated, "דמי שכירות")
    rent = add_measurement(gated, "rejected", RENT_OLD, "55 ₪", 55, rent_block)
    area = add_measurement(gated, "auto_validated", AREA, "120 מ\"ר", 120, block_of(gated, "השטח הבנוי"))
    with tenant_tx(gated.system) as conn:
        conn.execute(text("UPDATE measurements SET reviewed_by = :u, reviewed_at = now() WHERE id = ANY(:m)"),
                     {"u": reviewer, "m": [value, rent]})
    before = {m: _state(gated, m) for m in (value, rent, area)}

    gated.reader["next"] = reading(LOST_120, CORRECTIONS)
    _reprocess(gated)
    assert _job(gated).status == "kept_previous"
    assert {m: _state(gated, m) for m in (value, rent, area)} == before  # nothing touched while the reading is kept

    assert client.post("/api/admin/reprocess", json={"accept_regression": True}).json()["queued"] == 1
    while worker.run_one("test-worker"):
        pass
    assert _job(gated).status == "done"
    assert measurement(gated, value)[0].status == "verified"
    assert measurement(gated, rent)[0].status == "rejected"

    with tenant_tx(gated.system) as conn:  # measured again: the rejected value finds the person's decision
        store_rows(conn, gated.document, gated.version, [_rent_row(block_of(gated, "דמי שכירות"))], "m2", None)
        rows = conn.execute(text("SELECT status FROM measurements WHERE version_id = :v AND value = 55"),
                            {"v": gated.version}).scalars().all()
    assert rows == ["rejected"]


def test_only_a_reviewed_measurement_carries_a_person_s_decision(gated):
    """The human-verified label rests on a review record: every verified, corrected or rejected measurement was set
    by a person (``reviewed_by``); extraction stores only ``auto_validated`` or ``needs_review``."""
    with tenant_tx(gated.system) as conn:
        store_rows(conn, gated.document, gated.version, [_rent_row(block_of(gated, "דמי שכירות"))], "m1", None)
        statuses = conn.execute(text("SELECT status, reviewed_by FROM measurements WHERE version_id = :v"),
                                {"v": gated.version}).all()
    assert statuses and all(s.status not in REVIEWED and s.reviewed_by is None for s in statuses)
