"""Appraisal contexts built from stored units, through ingestion (round 9 U8: KTD6; R6, R10).

The synthetic compendium of round 9 (R9a: three appraisals titled only by a project name, then an appendix of
comparables) is ingested through the real worker with OCR, vision and the measurement pass off. Its contexts are its
stored units: ``named`` finds the second appraisal by its project alias although no address or parcel of it is
written in the question, a name written with a Hebrew prefix letter included; the appendix is labelled as such and is
never the asked property; a context key is of its reading; the same file read again after a reindex is a new reading
with new keys. Nothing here is customer data."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from app import worker
from app.chat import contexts, units
from app.chat import tools as T
from app.db import tenant_tx
from app.platform import pipeline
from tests.conftest import login
from tests.factories import make_office
from tests.helpers import upload

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANIFEST = json.loads((FIXTURES / "round9" / "manifest.json").read_text(encoding="utf-8"))["documents"]
R9A = FIXTURES / MANIFEST["compendium"]["file"]
EXPECTED = MANIFEST["compendium"]["units"]


@pytest.fixture(autouse=True)
def no_models(monkeypatch):
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    monkeypatch.setattr(pipeline, "vision_reader", lambda ctx: None)
    monkeypatch.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")


@pytest.fixture
def office(db, client):
    a = make_office(db, "משרד א", "admin-a@example.test")
    login(client, "admin-a@example.test")
    a.client = client
    return a


def ingest(office, path: Path) -> str:
    r = upload(office.client, [(path.name, path.read_bytes())], group_id=office.default_group_id)
    assert r.status_code == 200, r.text
    while worker.run_one("test-worker"):
        pass
    return r.json()["results"][0]["version_id"]


def reading_of(office, version: str) -> str:
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT ingestion->>'reading_id' FROM document_versions WHERE id = :v"),
                            {"v": version}).scalar_one()


def contexts_of(office, version: str) -> contexts.Contexts:
    with tenant_tx(office.system) as conn:
        return contexts.of_version(conn, version, reading_of(office, version))


def test_named_finds_the_second_appraisal_by_its_project_alias_and_the_appendix_is_never_a_subject(office):
    ver = ingest(office, R9A)
    cx = contexts_of(office, ver)
    assert cx.multi and cx.numbers == [1, 2, 3, 4]
    for alias in EXPECTED[1]["aliases"]:  # "גבעת הרקפות", "הרקפות": no address or parcel is in the question
        assert cx.named(f"מה שווי הנכס ב{alias}?") == [2], alias
    assert cx.named(f"מה שווי {EXPECTED[1]['title']}") == [2]
    assert cx.named(f"מה שווי {EXPECTED[2]['title']}") == [3]
    # the appendix has a title of its own, and it is a unit, but it is not a property: nothing names it
    assert [cx.kind(n) for n in cx.numbers] == [e["kind"] for e in EXPECTED]
    assert cx.subjects == [1, 2, 3]
    assert cx.named(EXPECTED[3]["title"]) == [] and cx.named("עסקאות השוואה נספח") == []
    assert cx.describe(4).startswith("הקשר 4: ") and "נספח" in cx.describe(4)
    # every block, wherever it is, lies in exactly the unit that holds it
    with tenant_tx(office.system) as conn:
        rows = conn.execute(text("SELECT unit_no, block_first, block_last FROM document_units WHERE version_id = :v"
                                 " ORDER BY unit_no"), {"v": ver}).all()
    assert [(s.number, s.first, s.last) for s in cx.segments] == [tuple(r) for r in rows]
    assert [cx.at_block(r.block_first) for r in rows] == [1, 2, 3, 4]


def test_the_tools_use_the_units_and_the_asked_property_is_never_the_appendix(office):
    ver = ingest(office, R9A)
    ws = T.Workspace(ctx=office.ctx())
    outline = T.tool_outline(ws, _document_of(office, ver))
    for n in range(1, len(EXPECTED) + 1):
        assert f"\nהקשר {n}: " in outline
    assert "\nהקשר 4: נספח" in outline
    ws.user_messages = [{"text": f"מה השווי של {EXPECTED[1]['aliases'][0]}?", "current": True}]
    with tenant_tx(office.ctx()) as conn:
        cx = T.contexts_of(ws, conn, ver)
    assert T.asked_contexts(ws, cx) == {2}
    ws.user_messages = [{"text": f"מה המחיר ב{EXPECTED[3]['title']}?", "current": True}]
    assert T.asked_contexts(ws, cx) == set()  # the appendix is not the asked property, however it is named


def test_a_context_key_is_of_its_reading_and_a_reindexed_file_gets_new_keys(office):
    ver = ingest(office, R9A)
    old = contexts_of(office, ver)
    first_reading = reading_of(office, ver)
    assert old.reading_id == first_reading and first_reading in old.key(2)
    assert contexts.split_key(old.key(2)) == (ver, 2)
    assert pipeline.reindex_version(office.office_id, ver) == "reindexed"
    new = contexts_of(office, ver)
    assert new.reading_id == reading_of(office, ver) != first_reading
    assert {old.key(n) for n in old.numbers}.isdisjoint({new.key(n) for n in new.numbers})
    assert [s.kind for s in new.segments] == [s.kind for s in old.segments]


def test_a_version_without_a_reading_falls_back_to_the_contexts_of_its_blocks(office, monkeypatch):
    ver = ingest(office, R9A)
    monkeypatch.setattr(units, "ensure", lambda conn, version_id: ())
    contexts._CACHE.clear()
    with tenant_tx(office.system) as conn:
        cx = contexts.of_version(conn, ver, "no-such-reading")
    assert cx.reading_id is None and {cx.kind(n) for n in cx.numbers} == {"appraisal"}  # only appraisals are found


def _document_of(office, version: str) -> str:
    with tenant_tx(office.system) as conn:
        return str(conn.execute(text("SELECT document_id FROM document_versions WHERE id = :v"),
                                {"v": version}).scalar_one())
