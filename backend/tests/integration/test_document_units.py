"""The unit store through ingestion, reindexing, the positions backfill and first use (round 9 U7: KTD4, KTD5; R6–R9).

Synthetic files are ingested through the real worker with OCR, vision and the measurement pass off. The units a
reading stores are those of its manifest; publish writes them in its own transaction (a version is never ready
without them), the reindex swap replaces them with the reading, a positions backfill that changes the derivation key
derives them again, a reading published before this round gets them on first use (once per version and reading in
the process, even for four callers at once), and a user outside the document's group never reads, builds or writes
them. The count script prints ids, counts, page ranges, kinds and certainty only. Nothing here is customer data."""

from __future__ import annotations

import importlib.util
import json
import re
import threading
import time
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text

from app import worker
from app.chat import contexts, entities, units
from app.db import tenant_tx
from app.extraction.geometry import POSITIONS_VERSION
from app.platform import pipeline, search
from app.platform.jobs import enqueue_positions
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.helpers import upload
from tests.support.unit_files import READING, unit_file

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANIFEST = json.loads((FIXTURES / "round9" / "manifest.json").read_text(encoding="utf-8"))["documents"]
R7B = FIXTURES / "round7" / "R7b_synthetic_two_appraisals_one_file.pdf"
D1 = FIXTURES / "D1_synthetic_harozim_digital.pdf"
R9A = FIXTURES / MANIFEST["compendium"]["file"]
R9B = FIXTURES / MANIFEST["similar_names"]["file"]

_STRIP = (
    "UPDATE pages SET mediabox = NULL, cropbox = NULL, rotation = NULL, display_width = NULL, display_height = NULL,"
    " printed_label = NULL, geometry_issue = NULL WHERE version_id = :v",
    "UPDATE document_blocks SET spans = NULL WHERE version_id = :v",
    "UPDATE document_versions SET ingestion = ingestion - 'positions' WHERE id = :v",
)


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


def drain() -> None:
    while worker.run_one("test-worker"):
        pass


def ingest(office, path: Path) -> tuple[str, str]:
    """A file through the upload API and the worker; returns (document id, version id)."""
    r = upload(office.client, [(path.name, path.read_bytes())], group_id=office.default_group_id)
    assert r.status_code == 200, r.text
    drain()
    result = r.json()["results"][0]
    return result["document_id"], result["version_id"]


def stored(office, version: str) -> list:
    with tenant_tx(office.system) as conn:
        return conn.execute(text(
            "SELECT unit_no, reading_id, kind, title, aliases, identifiers, page_first, page_last, block_first,"
            " block_last, certainty, evidence, units_version, derivation_key FROM document_units"
            " WHERE version_id = :v ORDER BY reading_id, unit_no"), {"v": version}).all()


def current(office, version: str) -> list:
    """The stored rows of the derivation key a freshly ingested reading has (the rows readers see): the table keeps the
    rows of earlier keys beside them (round 15 U3, KTD6)."""
    key = f"{units.UNITS_VERSION}|{POSITIONS_VERSION}"
    return [r for r in stored(office, version) if r.derivation_key == key]


def ingestion(office, version: str) -> dict:
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT ingestion FROM document_versions WHERE id = :v"), {"v": version}).scalar_one()


# --- publish -----------------------------------------------------------------------------------------------------------


def test_publish_stores_the_units_of_the_reading_that_its_manifest_names(office):
    _, ver = ingest(office, R9A)
    rows = stored(office, ver)
    reading = ingestion(office, ver)
    expected = MANIFEST["compendium"]["units"]
    assert [(r.kind, r.page_first, r.page_last) for r in rows] == [
        (e["kind"], e["first_page"], e["last_page"]) for e in expected]
    for r, e in zip(rows, expected, strict=True):
        assert set(e["aliases"]) <= set(r.aliases)
        if e["kind"] == "appraisal":
            assert r.title == e["title"]
        assert r.reading_id == reading["reading_id"] and r.units_version == units.UNITS_VERSION
        assert r.derivation_key == f"{units.UNITS_VERSION}|{POSITIONS_VERSION}"
    assert [r.certainty for r in rows] == ["certain"] * 4
    assert [r.unit_no for r in rows] == [1, 2, 3, 4] and rows[0].block_first == 0
    assert all(a.block_last + 1 == b.block_first for a, b in zip(rows, rows[1:], strict=False))
    # the comparables appendix names other properties; none of them is a unit or an alias
    every = " ".join(" ".join(r.aliases) + " " + (r.title or "") for r in rows)
    assert "הצבעוני" not in every and "השקד" not in every
    # evidence: block indexes and rule names, never copied text
    assert rows[1].evidence["boundary"]["signals"] == ["label_restart", "title_block_repeat"]
    assert not re.search(r"[א-ת]", json.dumps(rows[1].evidence, ensure_ascii=False))
    assert rows[1].identifiers["parcels"] == [["31140", "22"]]


def test_the_round_7_two_appraisal_file_is_two_units_and_a_single_appraisal_file_is_one(office):
    _, two = ingest(office, R7B)
    _, one = ingest(office, D1)
    _, similar = ingest(office, R9B)
    assert [(r.kind, r.certainty, r.page_first, r.page_last) for r in stored(office, two)] == [
        ("appraisal", "certain", 1, 1), ("appraisal", "certain", 2, 2)]
    (only,) = stored(office, one)
    assert (only.kind, only.certainty, only.unit_no) == ("document", "unsplit", 1)
    first, second = stored(office, similar)
    assert "הגפן א" in first.aliases and "הגפן ב" in second.aliases and "הגפן ב" not in first.aliases
    with tenant_tx(office.system) as conn:  # every ready version has units
        bare = conn.execute(text(
            "SELECT count(*) FROM document_versions v WHERE v.status IN ('ready', 'needs_review') AND NOT EXISTS"
            " (SELECT 1 FROM document_units u WHERE u.version_id = v.id)")).scalar_one()
    assert bare == 0


def test_a_version_is_never_ready_without_units_because_publish_writes_them_in_its_transaction(office, monkeypatch):
    def failing(conn, version_id):
        raise RuntimeError("the units could not be written")

    monkeypatch.setattr(units, "replace", failing)
    r = upload(office.client, [(D1.name, D1.read_bytes())], group_id=office.default_group_id)
    ver = r.json()["results"][0]["version_id"]
    drain()
    with tenant_tx(office.system) as conn:
        status = conn.execute(text("SELECT status FROM document_versions WHERE id = :v"), {"v": ver}).scalar_one()
        facts = conn.execute(text("SELECT count(*) FROM occurrences WHERE version_id = :v"), {"v": ver}).scalar_one()
    assert status not in ("ready", "needs_review") and not stored(office, ver) and facts == 0  # one transaction


def test_a_derivation_that_fails_leaves_one_undetermined_unit_and_does_not_block_publication(office, monkeypatch):
    def failing(*_a, **_k):
        raise ValueError("a block the derivation cannot read")

    monkeypatch.setattr(units, "derive", failing)
    _, ver = ingest(office, D1)
    (only,) = stored(office, ver)
    assert (only.kind, only.certainty, only.title) == ("document", "unsplit", None)
    assert only.evidence["kind"] == {"rule": "derivation_failed"}
    with tenant_tx(office.system) as conn:
        assert conn.execute(text("SELECT status FROM document_versions WHERE id = :v"), {"v": ver}).scalar_one() in (
            "ready", "needs_review")


# --- reindex and the positions backfill ----------------------------------------------------------------------------------


def test_the_reindex_swap_replaces_the_units_of_the_old_reading(office):
    _, ver = ingest(office, R9A)
    old = ingestion(office, ver)["reading_id"]
    with tenant_tx(office.system) as conn:  # a unit of the reading about to be replaced that the new one lacks
        conn.execute(text(
            "INSERT INTO document_units (office_id, document_id, version_id, reading_id, unit_no, kind, block_first,"
            " block_last, certainty, units_version, derivation_key) SELECT office_id, document_id, version_id,"
            " reading_id, 9, 'appraisal', 0, 1, 'certain', units_version, derivation_key FROM document_units"
            " WHERE version_id = :v AND unit_no = 1"), {"v": ver})
    assert len(stored(office, ver)) == 5
    assert pipeline.reindex_version(office.office_id, ver) == "reindexed"
    new = ingestion(office, ver)["reading_id"]
    rows = stored(office, ver)
    assert new != old
    assert {r.reading_id for r in rows} == {new} and [r.unit_no for r in rows] == [1, 2, 3, 4]
    assert [(r.kind, r.page_first, r.page_last) for r in rows] == [
        (e["kind"], e["first_page"], e["last_page"]) for e in MANIFEST["compendium"]["units"]]


def test_a_positions_backfill_derives_the_units_again_because_the_key_changed(office):
    _, ver = ingest(office, R7B)
    with tenant_tx(office.system) as conn:  # the version as a reading before positions left it, with units of then
        for sql in _STRIP:
            conn.execute(text(sql), {"v": ver})
        conn.execute(text("DELETE FROM document_units WHERE version_id = :v"), {"v": ver})
        units.replace(conn, ver)
        conn.execute(text("UPDATE document_units SET title = 'שורה ישנה' WHERE version_id = :v"), {"v": ver})
    before = stored(office, ver)
    assert {r.derivation_key for r in before} == {f"{units.UNITS_VERSION}|-"} and before[0].title == "שורה ישנה"
    assert pipeline.backfill_positions(office.office_id, ver) == "positioned"
    after = current(office, ver)
    assert [r.title for r in after] == ["שומת מקרקעין — רחוב הדמומית 12, כפר הדמה",
                                        "שומת מקרקעין — רחוב הצפצפה 7, כפר הדמה"]
    assert {r.reading_id for r in after} == {before[0].reading_id}  # the reading itself did not change
    # the rows of the earlier key are kept beside the new ones, unchanged (KTD6, KD8)
    assert [r for r in stored(office, ver) if r.derivation_key == f"{units.UNITS_VERSION}|-"] == before
    assert pipeline.backfill_positions(office.office_id, ver) == "present"
    assert current(office, ver) == after and len(stored(office, ver)) == len(before) + len(after)


def test_the_positions_job_through_the_worker_rebuilds_the_units(office):
    _, ver = ingest(office, R7B)
    with tenant_tx(office.system) as conn:
        for sql in _STRIP:
            conn.execute(text(sql), {"v": ver})
        conn.execute(text("UPDATE document_units SET derivation_key = :k, title = 'שורה ישנה' WHERE version_id = :v"),
                     {"v": ver, "k": f"{units.UNITS_VERSION}|-"})
        assert enqueue_positions(conn, ver)
    drain()
    assert len(current(office, ver)) == 2 and all(r.title != "שורה ישנה" for r in current(office, ver))
    # the rows the job replaced in meaning stay where they were (the old key, not read)
    assert {r.title for r in stored(office, ver) if r.derivation_key == f"{units.UNITS_VERSION}|-"} == {"שורה ישנה"}


# --- the lazy build ------------------------------------------------------------------------------------------------------


def forget(office, ver: str) -> None:
    """The version as a reading published before this round: no units."""
    with tenant_tx(office.system) as conn:
        conn.execute(text("DELETE FROM document_units WHERE version_id = :v"), {"v": ver})
    units._MEMO.clear()


def test_a_reading_published_before_this_round_gets_its_units_on_first_use_and_only_then(office, monkeypatch):
    _, ver = ingest(office, R9A)
    want = [(r.unit_no, r.kind, r.title, r.page_first, r.page_last) for r in stored(office, ver)]
    forget(office, ver)
    calls = []
    real = units.derive_version
    monkeypatch.setattr(units, "derive_version", lambda *a, **k: calls.append(1) or real(*a, **k))
    with tenant_tx(office.ctx()) as conn:
        found = units.ensure(conn, ver)
    assert [(u.unit_no, u.kind, u.title, u.page_first, u.page_last) for u in found] == want
    assert [(r.unit_no, r.kind, r.title, r.page_first, r.page_last) for r in stored(office, ver)] == want
    with tenant_tx(office.ctx()) as conn:
        assert [u.unit_no for u in units.ensure(conn, ver)] == [1, 2, 3, 4]
    assert len(calls) == 1


def test_four_callers_at_once_build_the_units_of_a_reading_once(office, monkeypatch):
    _, ver = ingest(office, R9A)
    forget(office, ver)
    calls, results, errors = [], [], []
    real = units.derive_version

    def slow(*a, **k):
        calls.append(1)
        time.sleep(0.3)
        return real(*a, **k)

    monkeypatch.setattr(units, "derive_version", slow)
    barrier = threading.Barrier(4)

    def caller():
        try:
            barrier.wait()
            with tenant_tx(office.ctx()) as conn:
                results.append([u.unit_no for u in units.ensure(conn, ver)])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=caller) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors and results == [[1, 2, 3, 4]] * 4
    assert len(calls) == 1
    assert [r.unit_no for r in stored(office, ver)] == [1, 2, 3, 4]  # one set of rows, not four


def test_units_of_another_derivation_key_are_derived_again_on_first_use_beside_the_old_rows(office):
    """Round 15 U3 (KTD6, R7, KD8): a version whose rows are of another key gets the current key's rows on first use,
    and the old rows are not deleted. Readers see only the current key."""
    _, ver = ingest(office, R7B)
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_units SET derivation_key = 'units-v0|-', title = 'שורה ישנה'"
                          " WHERE version_id = :v"), {"v": ver})
    units._MEMO.clear()
    with tenant_tx(office.ctx()) as conn:
        found = units.ensure(conn, ver)
        assert [u.title for u in units.stored(conn, ver)] == [u.title for u in found]
        assert [u.title for u in units.stored_many(conn, [ver])[UUID(ver)]] == [u.title for u in found]
    assert [u.title for u in found][0] != "שורה ישנה"
    rows = stored(office, ver)
    assert len(rows) == 4
    assert {r.derivation_key for r in rows} == {"units-v0|-", f"{units.UNITS_VERSION}|{POSITIONS_VERSION}"}
    assert {r.title for r in rows if r.derivation_key == "units-v0|-"} == {"שורה ישנה"}
    assert len(current(office, ver)) == 2


def test_replace_rewrites_only_the_rows_of_its_own_key(office):
    _, ver = ingest(office, R7B)
    with tenant_tx(office.system) as conn:   # the old key's rows: a copy of the current ones under another key
        conn.execute(text(
            "INSERT INTO document_units (office_id, document_id, version_id, reading_id, unit_no, kind, title, block_first,"
            " block_last, certainty, units_version, derivation_key) SELECT office_id, document_id, version_id, reading_id,"
            " unit_no, kind, 'שורה ישנה', block_first, block_last, certainty, 'units-v0', 'units-v0|-' FROM document_units"
            " WHERE version_id = :v"), {"v": ver})
        units.replace(conn, ver)   # re-derives the current key only
    rows = stored(office, ver)
    assert len(rows) == 4 and {r.title for r in rows if r.derivation_key == "units-v0|-"} == {"שורה ישנה"}
    assert len(current(office, ver)) == 2 and all(r.title != "שורה ישנה" for r in current(office, ver))


def test_a_user_outside_the_documents_group_neither_reads_builds_nor_writes_units(office, db):
    closed = make_group(office, "קבוצה סגורה")
    outsider = make_user(office, "outside@example.test", [office.default_group_id])
    insider = make_user(office, "inside@example.test", [office.default_group_id, closed])
    other = make_office(db, "משרד ב", "admin-b@example.test")
    _, ver = ingest(office, D1)
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE documents SET group_id = :g"), {"g": closed})
    forget(office, ver)
    with tenant_tx(office.ctx("employee", outsider)) as conn:
        assert units.ensure(conn, ver) == () and units.stored(conn, ver) is None
    with tenant_tx(other.ctx()) as conn:
        assert units.ensure(conn, ver) == ()
    assert stored(office, ver) == []  # nothing was built for either
    with tenant_tx(office.ctx("employee", insider)) as conn:
        assert len(units.ensure(conn, ver)) == 1
    assert len(stored(office, ver)) == 1
    with tenant_tx(office.ctx("employee", outsider)) as conn:
        assert units.stored(conn, ver) is None and units.read_or_derive(conn, ver) == ()


def test_a_transaction_that_cannot_write_still_gets_the_units(office):
    _, ver = ingest(office, R7B)
    forget(office, ver)
    with tenant_tx(office.ctx()) as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        found = units.ensure(conn, ver)
    assert len(found) == 2 and stored(office, ver) == []


# --- the count script ------------------------------------------------------------------------------------------------------


def _count_script():
    path = Path(__file__).resolve().parents[2] / "scripts" / "count_units.py"
    spec = importlib.util.spec_from_file_location("count_units", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_unit_count_script_reads_only_and_prints_no_document_content(office):
    compendium = ingest(office, R9A)[0]
    single = ingest(office, D1)[0]
    two = ingest(office, R7B)[0]
    with tenant_tx(office.system) as conn:
        names = [r.title for r in conn.execute(text("SELECT title FROM document_units")) if r.title]
        names += [a for r in conn.execute(text("SELECT aliases FROM document_units")) for a in r.aliases]
        conn.execute(text("DELETE FROM document_units WHERE document_id = :d"), {"d": single})  # derived in memory
    script = _count_script()
    rows = {r["document_id"]: r for r in script.count_office(office.system)}
    assert [(u["kind"], u["pages"]) for u in rows[compendium]["units"]] == [
        ("appraisal", (1, 2)), ("appraisal", (3, 4)), ("appraisal", (5, 6)), ("appendix", (7, 8))]
    assert [u["certainty"] for u in rows[two]["units"]] == ["certain", "certain"]
    assert [(u["kind"], u["certainty"]) for u in rows[single]["units"]] == [("document", "unsplit")]
    out = script.report(list(rows.values()))
    assert f"{compendium}  pages=8  units=4  [1: pages 1-2, appraisal, certain] [2: pages 3-4," in out, out
    assert "single_unit=1  multi_unit=2  appraisal_units=5" in out
    assert not re.search(r"[א-ת]", out) and "31102" not in out
    assert names and not any(n in out for n in names)
    with tenant_tx(office.system) as conn:  # read-only: the units it derived in memory were not written
        assert conn.execute(text("SELECT count(*) FROM document_units WHERE document_id = :d"),
                            {"d": single}).scalar_one() == 0


# --- derivations side by side: readers see the current key only, older anchors resolve through the kept rows -----------
# (round 15 U3, KTD6, R7, KD8). A later derivation is simulated by raising ``units.UNITS_VERSION`` and storing the rows
# it would derive.


def three_units(office, monkeypatch, titled: bool = True) -> tuple[str, str]:
    """A file of three appraisals of two blocks each (units 1, 2, 3 over blocks 0-1, 2-3, 4-5); the third is untitled
    when ``titled`` is false. Its rows are those of the ``units-v4`` derivation (the one before the later derivation
    ``derive_as`` simulates), whatever ``UNITS_VERSION`` the code has now."""
    names = ["פרויקט הארז", "מתחם האשל", "פרויקט הברוש" if titled else None]
    monkeypatch.setattr(units, "UNITS_VERSION", "units-v4")
    return unit_file(office, office.default_group_id, "קובץ שומות סינתטי", [
        {"title": t, "aliases": [t.split()[-1]] if t else [], "identifiers": [f"רחוב הדוגמה {n}"],
         "blocks": [t or f"שומה {n}", "ב"]} for n, t in enumerate(names, 1)])


def derive_as(office, ver: str, monkeypatch, units_version: str, spec: list[dict], clear_caches: bool = True) -> None:
    """The version's units as a later derivation would store them, beside the rows already there (``spec``: dicts with
    ``blocks`` (first, last), ``pages``, and optionally ``title``, ``aliases``, ``kind``)."""
    monkeypatch.setattr(units, "UNITS_VERSION", units_version)
    with tenant_tx(office.system) as conn:
        reading = units.reading_of(conn, ver)
        rows = [units.Unit(n, u.get("kind", "appraisal"), u.get("title"), tuple(u.get("aliases") or ()),
                           {"shown": list(u.get("identifiers") or ())}, u["pages"][0], u["pages"][1],
                           u["blocks"][0], u["blocks"][1], "certain", {}) for n, u in enumerate(spec, 1)]
        units._insert(conn, ver, reading, rows)
    units._MEMO.clear()
    if clear_caches:
        contexts._CACHE.clear()


def count(office, ver: str) -> int:
    return len(stored(office, ver))


def test_after_a_later_derivation_the_old_rows_stay_and_every_reader_returns_only_the_new_units(office, monkeypatch):
    doc, ver = three_units(office, monkeypatch)
    before = stored(office, ver)
    derive_as(office, ver, monkeypatch, "units-v5", [
        {"title": "מתחם חדש א", "blocks": (0, 3), "pages": (1, 2)},
        {"title": "מתחם חדש ב", "blocks": (4, 5), "pages": (3, 3)}])
    after = stored(office, ver)
    assert len(after) == len(before) + 2 and [r for r in after if r.units_version == "units-v4"] == before
    with tenant_tx(office.ctx()) as conn:
        assert [u.title for u in units.stored(conn, ver)] == ["מתחם חדש א", "מתחם חדש ב"]
        assert [u.title for u in units.stored_many(conn, [ver])[UUID(ver)]] == ["מתחם חדש א", "מתחם חדש ב"]
        assert [u.title for u in units.ensure(conn, ver)] == ["מתחם חדש א", "מתחם חדש ב"]
        assert [u.title for u in units.read_or_derive(conn, ver)] == ["מתחם חדש א", "מתחם חדש ב"]
        names = entities._unit_names(conn)
        assert "מתחם חדש א" in names and "פרויקט הארז" not in names
        # the first-message query (a module constant, of the derivation it was imported with) is keyed too
        assert "derivation_key" in entities._UNIT_NAMES_SQL
    assert count(office, ver) == len(after)   # reading changed nothing


def test_the_one_unit_check_reads_the_current_derivation_only(office, monkeypatch):
    doc, ver = three_units(office, monkeypatch)
    with tenant_tx(office.ctx()) as conn:
        assert search.document_term_holders(conn, [["סינתטי"]]) == {}   # three units: the file is not "one unit"
    derive_as(office, ver, monkeypatch, "units-v5", [{"title": "קובץ אחד", "blocks": (0, 5), "pages": (1, 3)}])
    with tenant_tx(office.ctx()) as conn:
        assert set(search.document_term_holders(conn, [["סינתטי"]])) == {UUID(doc)}   # the v5 derivation has one


def test_contexts_of_a_version_follow_a_derivation_change_in_the_same_process(office, monkeypatch):
    _, ver = three_units(office, monkeypatch)
    with tenant_tx(office.ctx()) as conn:
        first = contexts.of_version(conn, ver, READING)
    assert first.numbers == [1, 2, 3] and first.tag is None and first.key(2) == f"{ver}#{READING}#2"
    derive_as(office, ver, monkeypatch, "units-v5", [
        {"title": "מתחם חדש א", "blocks": (0, 3), "pages": (1, 2)}, {"title": "מתחם חדש ב", "blocks": (4, 5), "pages": (3, 3)}],
        clear_caches=False)   # the contexts cache is keyed by derivation: the old entry is still there and is not read
    assert any(k[:3] == (ver, READING, "units-v4") for k in contexts._CACHE)   # the key ends with the cards' digest
    with tenant_tx(office.ctx()) as conn:
        second = contexts.of_version(conn, ver, READING)
    assert second.numbers == [1, 2] and second.tag == "v5" and second.key(2) == f"{ver}#{READING}#2@v5"


def anchor_on_v4(doc: str, ver: str, no: int, title: str | None = None) -> dict:
    """The anchor a conversation stored on a ``units-v4`` unit: no tag."""
    return entities.unit_anchor({"document_id": doc, "reading_id": READING, "unit_no": no, "title": title,
                                "aliases": [], "identifiers": [], "page_first": no, "page_last": no})


def test_an_anchor_on_an_untitled_v4_unit_resolves_by_range_to_the_unit_that_now_holds_it(office, monkeypatch):
    doc, ver = three_units(office, monkeypatch, titled=False)
    anchor = anchor_on_v4(doc, ver, 3)   # v4 unit 3: blocks 4-5
    # v5 renumbers: a new unit 3 sits where the v4 unit 2 ended, and the range 4-5 is now unit 4
    derive_as(office, ver, monkeypatch, "units-v5", [
        {"blocks": (0, 1), "pages": (1, 1)}, {"blocks": (2, 2), "pages": (2, 2)},
        {"blocks": (3, 3), "pages": (2, 2)}, {"blocks": (4, 5), "pages": (3, 3)}])
    live = entities.live_unit(office.ctx(), anchor)
    assert live.status == "moved" and live.unit["unit_no"] == 4 and live.unit["units_tag"] == "v5"
    assert live.unit["unit_no"] != anchor["unit_no"]   # never the unit that happens to carry the old number
    again = entities.live_unit(office.ctx(), live.unit)   # the new anchor is current, by its number
    assert again.status == "current" and again.unit["unit_no"] == 4


def test_an_anchor_whose_range_spans_two_new_units_is_gone_and_lists_the_candidates(office, monkeypatch):
    doc, ver = three_units(office, monkeypatch, titled=False)
    anchor = anchor_on_v4(doc, ver, 3)
    derive_as(office, ver, monkeypatch, "units-v5", [
        {"blocks": (0, 1), "pages": (1, 1)}, {"blocks": (2, 4), "pages": (2, 3)}, {"blocks": (5, 5), "pages": (3, 3)}])
    live = entities.live_unit(office.ctx(), anchor)
    assert live.status == "gone" and live.unit is None and len(live.candidates) == 3


def test_an_old_anchor_is_found_by_name_and_the_old_rows_are_not_read_as_current(office, monkeypatch):
    doc, ver = three_units(office, monkeypatch)
    anchor = anchor_on_v4(doc, ver, 2, "מתחם האשל")
    entities_anchor = entities.unit_anchor({"document_id": doc, "reading_id": READING, "unit_no": 2, "units_tag": None,
                                            "title": "מתחם האשל", "aliases": []})
    derive_as(office, ver, monkeypatch, "units-v5", [
        {"title": "מתחם האשל", "blocks": (0, 3), "pages": (1, 2)}, {"title": "פרויקט הברוש", "blocks": (4, 5), "pages": (3, 3)}])
    live = entities.live_unit(office.ctx(), anchor)
    assert live.status == "moved" and live.unit["unit_no"] == 1 and live.unit["display"] == "מתחם האשל"
    assert entities.same_unit(entities_anchor, live.unit)


def test_the_kept_row_is_read_only_through_the_older_derivations_tag(office, monkeypatch):
    doc, ver = three_units(office, monkeypatch)
    derive_as(office, ver, monkeypatch, "units-v5", [{"title": "קובץ אחד", "blocks": (0, 5), "pages": (1, 3)}])
    with tenant_tx(office.ctx()) as conn:
        old = units.kept(conn, ver, READING, 3, None)
        assert old is not None and (old.block_first, old.block_last) == (4, 5)
        assert units.kept(conn, ver, READING, 3, "v5") is None   # unit 3 of v5 does not exist
        assert units.kept(conn, ver, "another-reading", 3, None) is None
