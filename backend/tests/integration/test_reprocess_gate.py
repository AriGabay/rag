"""Safe reprocessing (U7, KTD7): a reading is replaced only by one that is complete and not worse, atomically with its
embeddings; a regression keeps the old reading and is recorded until an admin accepts it; reviewed measurements are
re-anchored in the new reading (after the font-map repair) or kept with ``anchor_lost``; references to an earlier
reading report ``stale``. Synthetic readings from a scripted reader only: no PDF is parsed and no model is called."""

from __future__ import annotations

import contextlib
import hashlib
import json

import pytest
from sqlalchemy import text

from app import worker
from app.chat import tools as T
from app.db import tenant_tx
from app.extraction.base import Block, ExtractionError, ExtractionResult, PageResult, TableResult, TableRow
from app.extraction.chunking import chunk_blocks
from app.platform import pipeline
from app.platform.jobs import enqueue_reindex
from app.platform.storage import get_storage, storage_key
from tests.conftest import login
from tests.factories import make_office

pytestmark = pytest.mark.db

ETH = "ð"
SECTION = "1. כללי"
COMPARABLES = ("להלן נתוני ההשוואה:", ["כתובת", "שטח במ\"ר", "מחיר למ\"ר"],
               [["רחוב הדקל 4", "95", "21,500"], ["רחוב האלון 7", "110", "19,800"]])
LEVIES = ("טבלת היטלים:", ["רכיב", "סכום"], [["היטל השבחה", "40,000"]])
# a font whose map is broken reads נ as ð; a bold one draws every glyph twice
VALUE_OLD = f"שווי ה{ETH}כס {ETH}קבע בסך 1,250,000 ₪ ליום הקובע."
VALUE_NEW = "שווי הנכס נקבע בסך 1,250,000 ₪ ליום הקובע."
RENT_OLD = "דמי שכירות 55 ₪ למ\"ר לחודש."
RENT_NEW = "דמי שכירות של 55 ₪ למ\"ר לחודש."
AREA = "השטח הבנוי 120 מ\"ר בקומה 3."
PRICE_NEW = "מחיר ממוצע: 20,500 ₪"
CORRECTIONS = {"fonts": [], "unresolved": [], "corrections": [
    {"font": "SYNTH+Book", "from": ETH, "to": "נ", "to_final": "ן", "samples": 6, "agreement": 1.0, "occurrences": 9},
    {"font": "SYNTH+Bold", "from": ETH, "to": "ר", "to_final": None, "samples": 4, "agreement": 1.0, "occurrences": 3},
]}


def doubled(s: str) -> str:
    """How a bold font whose glyphs are drawn twice reads without char dedupe."""
    return "".join(c if c == " " else c * 2 for c in s)


def reading(pages: dict[int, list], fontmap: dict | None = None, failed: tuple[int, ...] = ()) -> ExtractionResult:
    """A scripted PDF reading. Each page holds paragraphs (str), unread pictures (``("img", reason)``) and tables
    (``("table", caption, headers, rows)``); a page in ``failed`` was not read."""
    blocks: list[Block] = []
    tables: list[TableResult] = []
    page_results = []
    for page, items in pages.items():
        lines = []
        for item in items:
            bbox = [40.0, 100.0 + 20 * len(blocks), 500.0, 115.0 + 20 * len(blocks)]
            if isinstance(item, str):
                blocks.append(Block(index=len(blocks), kind="paragraph", text=item, section=SECTION,
                                    section_path=[SECTION], page=page, bbox=bbox, method="text_layer",
                                    reader_version=pipeline.PDF_INGESTION_VERSION))
                lines.append(item)
            elif item[0] == "img":
                blocks.append(Block(index=len(blocks), kind="image", text="", section=SECTION, section_path=[SECTION],
                                    page=page, status="unread", note=item[1], bbox=bbox, method="none",
                                    reader_version=pipeline.PDF_INGESTION_VERSION, content_hash=f"h{len(blocks)}"))
            else:
                _, caption, headers, rows = item
                t = TableResult(index=len(tables), headers=headers, units=[None] * len(headers),
                                rows=[TableRow(page, r) for r in rows], page_start=page, page_end=page,
                                section=SECTION, caption=caption, block_index=len(blocks))
                tables.append(t)
                body = "\n".join(" | ".join(r) for r in [headers, *rows])
                blocks.append(Block(index=len(blocks), kind="table", text=body, section=SECTION,
                                    section_path=[SECTION], page=page, table_index=t.index, bbox=bbox,
                                    method="text_layer", reader_version=pipeline.PDF_INGESTION_VERSION))
                lines += [caption, body]
        ok = page not in failed
        page_results.append(PageResult(page_no=page, text="\n".join(lines) if ok else "",
                                       method="text_layer" if ok else "failed", quality=1.0 if ok else 0.0, ok=ok))
    return ExtractionResult(page_count=len(pages), pages=page_results, tables=tables,
                            chunks=chunk_blocks(blocks, tables), blocks=blocks, fontmap=fontmap)


OLD = {1: [VALUE_OLD, AREA, RENT_OLD, doubled(PRICE_NEW)], 2: [("table", *COMPARABLES)]}
NEW = {1: ["הדוח נערך לבקשת הבעלים.", VALUE_NEW, AREA, RENT_NEW, PRICE_NEW],
       2: [("table", *LEVIES),
           ("table", COMPARABLES[0], COMPARABLES[1], [["רחוב התמר 2", "80", "22,000"], *COMPARABLES[2]])]}


@pytest.fixture
def office(db, client, monkeypatch):
    """An office with one PDF version read by the scripted reader (``office.reader["next"]`` is what the next
    reading returns, or raises)."""
    a = make_office(db, "משרד א", "admin-a@example.test")
    login(client, "admin-a@example.test")
    a.reader = {"next": reading(OLD)}

    def scripted(data, mime_type, deadline, vision, readings=None):
        r = a.reader["next"]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(pipeline, "_extract", scripted)
    monkeypatch.setattr(pipeline, "vision_reader", lambda ctx: None)
    data = b"%PDF-1.4 synthetic"
    sha = hashlib.sha256(data).hexdigest()
    key = storage_key(a.office_id, sha)
    get_storage().put(key, data)
    with tenant_tx(a.ctx()) as conn:
        a.document = conn.execute(text("INSERT INTO documents (office_id, group_id, title) VALUES (app_office(), :g,"
                                       " 'שומת נכס סינתטית') RETURNING id"), {"g": a.default_group_id}).scalar_one()
        a.version = conn.execute(text(
            "INSERT INTO document_versions (office_id, document_id, version_no, sha256, filename, mime_type,"
            " size_bytes, storage_key, uploaded_by) VALUES (app_office(), :d, 1, :s, 'synthetic.pdf',"
            " 'application/pdf', :b, :k, :u) RETURNING id"),
            {"d": a.document, "s": sha, "b": len(data), "k": key, "u": a.admin_id}).scalar_one()
    pipeline.process_version(a.office_id, a.version)
    return a


def q(office, sql, **p):
    with tenant_tx(office.system) as conn:
        return conn.execute(text(sql), {"v": office.version} | p).all()


def snapshot(office) -> tuple:
    return (q(office, "SELECT block_index, kind, text, page FROM document_blocks WHERE version_id = :v"
                      " ORDER BY block_index"),
            q(office, "SELECT chunk_index, text, embedding::text, embedding_model FROM chunks WHERE version_id = :v"
                      " ORDER BY chunk_index"),
            q(office, "SELECT page_no, text, ok FROM pages WHERE version_id = :v ORDER BY page_no"),
            q(office, "SELECT ingestion FROM document_versions WHERE id = :v"))


def reading_id(office) -> str | None:
    return q(office, "SELECT ingestion->>'reading_id' AS r FROM document_versions WHERE id = :v")[0].r


def reprocess(office, accept: bool = False) -> None:
    with tenant_tx(office.system) as conn:
        enqueue_reindex(conn, office.version, pipeline.PDF_INGESTION_VERSION, accept_regression=accept)
    while worker.run_one("test-worker"):
        pass


def job(office):
    return q(office, "SELECT status, last_error, payload FROM jobs WHERE payload->>'mode' = 'reindex'")[0]


# --- the swap ------------------------------------------------------------------------------------------------

def test_a_reading_carries_a_reading_id_and_embeddings_for_every_chunk(office):
    assert reading_id(office)
    assert q(office, "SELECT count(*) AS n FROM chunks WHERE version_id = :v AND embedding IS NULL")[0].n == 0


def test_an_extraction_that_raises_leaves_the_reading_identical(office):
    before = snapshot(office)
    office.reader["next"] = ExtractionError("קריאה נכשלה זמנית", permanent=False)
    reprocess(office)
    assert job(office).status in ("queued", "failed") and "קריאה נכשלה" in job(office).last_error
    assert snapshot(office) == before


def test_a_successful_reindex_never_commits_a_chunk_without_its_embedding(office, monkeypatch):
    seen: list[int] = []
    real = pipeline.tenant_tx

    @contextlib.contextmanager
    def watched(ctx):
        with real(ctx) as conn:
            yield conn
        with real(office.system) as conn:  # what any other session sees after each commit
            seen.append(conn.execute(text("SELECT count(*) FROM chunks WHERE version_id = :v AND (embedding IS NULL"
                                          " OR embedding_model IS NULL)"), {"v": office.version}).scalar_one())

    monkeypatch.setattr(pipeline, "tenant_tx", watched)
    old = reading_id(office)
    office.reader["next"] = reading(NEW, CORRECTIONS)
    assert pipeline.reindex_version(office.office_id, office.version) == "reindexed"
    assert seen and set(seen) == {0}
    assert reading_id(office) not in (None, old)
    assert q(office, "SELECT count(*) AS n FROM chunks WHERE version_id = :v")[0].n == len(reading(NEW).chunks)


def test_a_page_that_loses_a_number_keeps_the_old_reading_and_records_the_regression(office, client):
    before = snapshot(office)
    lost = {1: [VALUE_NEW, AREA.replace("120", "12O"), RENT_NEW, PRICE_NEW], 2: [("table", *COMPARABLES)]}
    office.reader["next"] = reading(lost, CORRECTIONS)
    reprocess(office)
    j = job(office)
    assert j.status == "failed" and "120" in j.last_error  # permanent: re-running reads the same
    assert snapshot(office)[:3] == before[:3]
    record = q(office, "SELECT ingestion->'reprocess_regression' AS r FROM document_versions WHERE id = :v")[0].r
    assert record["pages"] == [{"page": 1, "missing_numbers": ["120"]}]
    assert record["ingestion_version"] == pipeline.PDF_INGESTION_VERSION and not record.get("failed_pages")
    jobs = client.get("/api/admin/jobs").json()
    assert [r["version_id"] for r in jobs["regressions"]] == [str(office.version)]
    # a plain reprocess does not read it again: the regression waits for an admin's decision
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET ingestion = jsonb_set(ingestion, '{ingestion_version}',"
                          " '\"pdf-blocks-old\"') WHERE id = :v"), {"v": office.version})
    assert client.post("/api/admin/reprocess", json={}).json()["queued"] == 0

    # the admin accepts the recorded regression: the re-run swaps and the acceptance stays on the report
    r = client.post("/api/admin/reprocess", json={"accept_regression": True}).json()
    assert r["queued"] == 1 and r["versions"] == [str(office.version)]
    while worker.run_one("test-worker"):
        pass
    assert job(office).status == "done"
    ing = q(office, "SELECT ingestion FROM document_versions WHERE id = :v")[0].ingestion
    assert "reprocess_regression" not in ing
    assert ing["accepted_regression"]["pages"] == [{"page": 1, "missing_numbers": ["120"]}]
    assert any("12O" in b.text for b in q(office, "SELECT text FROM document_blocks WHERE version_id = :v"))


LOST_120 = {1: [VALUE_NEW, AREA.replace("120", "12O"), RENT_NEW, PRICE_NEW], 2: [("table", *COMPARABLES)]}


def recorded_regression(office) -> dict | None:
    return q(office, "SELECT ingestion->'reprocess_regression' AS r FROM document_versions WHERE id = :v")[0].r


@pytest.mark.parametrize("change", ["more_pages_lost", "fewer_tables", "other_reader"])
def test_an_accepted_regression_does_not_cover_a_different_one(office, change):
    """The admin accepted what was recorded (page 1 lost "120"). A re-run that reads worse in another way, or under
    another reader, is a new regression: it is recorded and the current reading stays."""
    office.reader["next"] = reading(LOST_120, CORRECTIONS)
    reprocess(office)
    before = snapshot(office)
    if change == "more_pages_lost":
        office.reader["next"] = reading(LOST_120, CORRECTIONS, failed=(2,))
    elif change == "fewer_tables":
        office.reader["next"] = reading({1: LOST_120[1], 2: [COMPARABLES[0] + " " + " ".join(
            c for row in [COMPARABLES[1], *COMPARABLES[2]] for c in row)]}, CORRECTIONS)
    else:
        with tenant_tx(office.system) as conn:
            conn.execute(text("UPDATE document_versions SET ingestion = jsonb_set(ingestion,"
                              " '{reprocess_regression,ingestion_version}', '\"pdf-blocks-old\"') WHERE id = :v"),
                         {"v": office.version})
    with tenant_tx(office.system) as conn:
        conn.execute(text("DELETE FROM jobs"))
    reprocess(office, accept=True)
    assert job(office).status == "failed"
    assert snapshot(office)[:3] == before[:3]
    record = recorded_regression(office)
    assert record["pages"] == [{"page": 1, "missing_numbers": ["120"]}]
    assert record["ingestion_version"] == pipeline.PDF_INGESTION_VERSION
    if change == "more_pages_lost":
        assert record["failed_pages"] == [2]
    elif change == "fewer_tables":
        assert record["tables"] == {"before": 1, "after": 0}
    ing = q(office, "SELECT ingestion FROM document_versions WHERE id = :v")[0].ingestion
    assert "accepted_regression" not in ing


def test_an_accepted_regression_covers_a_re_run_that_loses_less(office):
    office.reader["next"] = reading(LOST_120, CORRECTIONS, failed=(2,))
    reprocess(office)
    assert recorded_regression(office)["failed_pages"] == [2]
    office.reader["next"] = reading(LOST_120, CORRECTIONS)  # page 2 reads again; page 1 still lost "120"
    with tenant_tx(office.system) as conn:
        conn.execute(text("DELETE FROM jobs"))
    reprocess(office, accept=True)
    assert job(office).status == "done"
    ing = q(office, "SELECT ingestion FROM document_versions WHERE id = :v")[0].ingestion
    assert "reprocess_regression" not in ing
    assert ing["accepted_regression"]["pages"] == [{"page": 1, "missing_numbers": ["120"]}]


def test_a_reindex_reuses_an_unchanged_chunks_vector_and_embeds_only_changed_text(office, monkeypatch):
    from app.providers.embeddings import get_embedding_provider

    provider = get_embedding_provider()
    embedded: list[str] = []
    real = provider.embed_passages

    def recorded(texts):
        embedded.extend(texts)
        return real(texts)

    monkeypatch.setattr(type(provider), "embed_passages", lambda self, texts: recorded(texts))
    old = {r.text: r.e for r in q(office, "SELECT text, embedding::text AS e FROM chunks WHERE version_id = :v")}
    changed = {1: [*OLD[1], "תוספת סינתטית: חניה אחת בקומת המרתף."], 2: OLD[2]}
    office.reader["next"] = reading(changed)
    assert pipeline.reindex_version(office.office_id, office.version) == "reindexed"
    new = {r.text: r.e for r in q(office, "SELECT text, embedding::text AS e FROM chunks WHERE version_id = :v")}
    kept = set(old) & set(new)
    assert kept and all(new[t] == old[t] for t in kept)  # the unchanged chunk's vector, without a call
    assert embedded and set(embedded) == set(new) - set(old)


@pytest.mark.parametrize("worse", ["failed_page", "fewer_tables"])
def test_more_failed_pages_or_fewer_tables_keep_the_old_reading(office, worse):
    before = snapshot(office)
    if worse == "failed_page":
        office.reader["next"] = reading(NEW, CORRECTIONS, failed=(2,))
    else:
        office.reader["next"] = reading({1: NEW[1], 2: [COMPARABLES[0] + " " + " ".join(
            c for row in [COMPARABLES[1], *COMPARABLES[2]] for c in row)]}, CORRECTIONS)
    reprocess(office)
    assert job(office).status == "failed"
    record = q(office, "SELECT ingestion->'reprocess_regression' AS r FROM document_versions WHERE id = :v")[0].r
    if worse == "failed_page":
        assert record["failed_pages"] == [2]
    else:
        assert record["tables"] == {"before": 1, "after": 0}
    assert snapshot(office)[:3] == before[:3]


def test_a_reading_that_only_reports_unread_regions_swaps(office):
    old = reading_id(office)
    office.reader["next"] = reading({1: [*OLD[1], ("img", "הקריאה החזותית אינה זמינה")],
                                     2: [*OLD[2], ("img", "הקריאה החזותית אינה זמינה")]})
    reprocess(office)
    assert job(office).status == "done"
    assert reading_id(office) != old
    ing = q(office, "SELECT ingestion FROM document_versions WHERE id = :v")[0].ingestion
    assert ing["partial"] is True and len(ing["unread"]) == 2 and "reprocess_regression" not in ing


# --- measurements -----------------------------------------------------------------------------------------------

def add_measurement(office, status: str, quote: str, value_text: str, value: float, block: int | None,
                    table: int | None = None, row: int | None = None, key: str | None = None) -> str:
    with tenant_tx(office.system) as conn:
        return str(conn.execute(text(
            "INSERT INTO measurements (office_id, document_id, version_id, block_index, table_index, row_index,"
            " statement_key, metric, metric_kind, value, value_text, quote, extraction_version, status)"
            " VALUES (app_office(), :d, :v, :b, :t, :r, :k, 'נתון', 'value', :val, :vt, :q, 'm1', :s) RETURNING id"),
            {"d": office.document, "v": office.version, "b": block, "t": table, "r": row,
             "k": key or f"b{block}:x", "val": value, "vt": value_text, "q": quote, "s": status}).scalar_one())


def measurement(office, mid: str):
    return q(office, "SELECT * FROM measurements WHERE id = :m", m=mid)


def block_of(office, needle: str) -> int:
    return q(office, "SELECT block_index FROM document_blocks WHERE version_id = :v AND text LIKE :t",
             t=f"%{needle}%")[0].block_index


def chunk_start(office, needle: str) -> int:
    """Where an unreviewed measurement is anchored: the first block of its passage, as extraction anchors it."""
    return q(office, "SELECT block_start FROM chunks WHERE version_id = :v AND kind = 'text' AND text LIKE :t",
             t=f"%{needle}%")[0].block_start


def test_reviewed_measurements_are_re_anchored_after_the_font_map_repair_or_kept_with_anchor_lost(office):
    value = add_measurement(office, "verified", "שווי ה" + ETH + "כס " + ETH + "קבע בסך 1,250,000 ₪",
                            "1,250,000 ₪", 1250000, block_of(office, "1,250,000"))
    price = add_measurement(office, "corrected", doubled(PRICE_NEW), doubled("20,500 ₪"), 20500,
                            block_of(office, doubled("20,500")))
    cell = add_measurement(office, "verified", "כתובת: רחוב האלון 7 | שטח במ\"ר: 110 | מחיר למ\"ר: 19,800",
                           "19,800", 19800, block_of(office, "רחוב האלון"), table=0, row=1, key="t0:r1:מחירלמר")
    rent_block = block_of(office, "דמי שכירות")
    rent = add_measurement(office, "rejected", RENT_OLD, "55 ₪", 55, rent_block)
    guess = add_measurement(office, "auto_validated", RENT_OLD, "55 ₪", 55, block_of(office, "דמי שכירות"))
    kept = add_measurement(office, "auto_validated", AREA, "120 מ\"ר", 120, chunk_start(office, AREA))
    with tenant_tx(office.system) as conn:  # the reviewer had corrected the value: the extracted one is history
        conn.execute(text("UPDATE measurements SET previous = CAST(:p AS jsonb) WHERE id = :m"),
                     {"m": price, "p": json.dumps([{"status": "auto_validated", "value_text": doubled("20,500 ₪")}])})
    old = reading_id(office)

    office.reader["next"] = reading(NEW, CORRECTIONS)
    reprocess(office)
    assert job(office).status == "done"

    m = measurement(office, value)[0]
    assert m.status == "verified" and m.anchor_lost is None
    assert m.block_index == block_of(office, VALUE_NEW) and m.block_index != 0
    assert m.quote == "שווי הנכס נקבע בסך 1,250,000 ₪"  # the stored quote now reads as the text of record
    assert m.statement_key.startswith(f"b{m.block_index}:")
    m = measurement(office, price)[0]
    assert m.status == "corrected" and m.anchor_lost is None and m.block_index == block_of(office, PRICE_NEW)
    m = measurement(office, cell)[0]  # matched by caption, headers and row label, not by its index
    assert (m.table_index, m.row_index, m.statement_key, m.anchor_lost) == (1, 2, "t1:r2:מחירלמר", None)
    assert m.block_index == block_of(office, "רחוב התמר") and "19,800" in m.quote
    m = measurement(office, rent)[0]  # its words are not in the new reading: the decision stays, the place is lost
    assert m.status == "rejected" and m.block_index is None and m.table_index is None
    assert m.anchor_lost["reading_id"] == old and m.anchor_lost["block_index"] == rent_block
    assert not measurement(office, guess)  # an unreviewed value whose words are gone is not kept
    m = measurement(office, kept)[0]
    assert m.status == "auto_validated" and m.block_index == chunk_start(office, AREA) and m.anchor_lost is None


def test_a_value_whose_place_was_lost_is_not_offered_as_verified_against_a_cell(office):
    lost = add_measurement(office, "verified", RENT_OLD, "55 ₪", 55, block_of(office, "דמי שכירות"))
    office.reader["next"] = reading(NEW, CORRECTIONS)
    reprocess(office)
    assert measurement(office, lost)[0].anchor_lost is not None
    ws = T.Workspace(ctx=office.ctx())
    listing = T.tool_find_measurements(ws, "דמי שכירות")
    assert "המיקום במסמך אבד" in listing
    public = next(iter(ws.measurements.values())).public()
    assert public["anchor_lost"] is True and public["block_index"] is None
    assert public["reading_id"] == reading_id(office)


# --- references to an earlier reading ---------------------------------------------------------------------------

def test_a_reference_to_an_earlier_reading_is_stale_and_opens_nothing(office, client):
    old = reading_id(office)
    ws = T.Workspace(ctx=office.ctx())
    T.tool_search(ws, "שווי")
    src = next(s for s in ws.sources.values() if s.block_start is not None)
    assert src.public()["reading_id"] == old  # an answer's sources keep the reading they were read from
    prior = {"version_id": str(office.version), "block_start": 0, "block_end": 0, "table_index": None,
             "chunk_id": None, "reading_id": old}
    assert "שווי" in T.tool_read(T.Workspace(ctx=office.ctx(), prior={"P1": prior}), {"source": "P1"})
    blocks = client.get(f"/api/documents/{office.document}/versions/{office.version}/blocks",
                        params={"start": 0, "end": 2, "reading_id": old}).json()
    assert blocks["stale"] is False and blocks["reading_id"] == old and blocks["blocks"]

    office.reader["next"] = reading(NEW, CORRECTIONS)
    reprocess(office)

    with pytest.raises(T.ToolError, match="קריאה קודמת"):
        T.tool_read(T.Workspace(ctx=office.ctx(), prior={"P1": prior}), {"source": "P1"})
    with pytest.raises(T.ToolError, match="קריאה קודמת"):  # an answer stored before reading ids: stale as well
        T.tool_read(T.Workspace(ctx=office.ctx(), prior={"P1": prior | {"reading_id": None}}), {"source": "P1"})
    with pytest.raises(T.ToolError, match="קריאה קודמת"):  # a source of this turn read before the swap
        T.tool_read(ws, {"source": src.sid})
    for stale in (old, "none"):
        body = client.get(f"/api/documents/{office.document}/versions/{office.version}/blocks",
                          params={"start": 0, "end": 2, "reading_id": stale}).json()
        assert body["stale"] is True and body["blocks"] == [] and body["reading_id"] == reading_id(office)
    fresh = client.get(f"/api/documents/{office.document}/versions/{office.version}/blocks",
                       params={"start": 0, "end": 2}).json()
    assert fresh["stale"] is False and fresh["blocks"]
