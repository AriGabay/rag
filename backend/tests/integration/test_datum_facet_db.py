"""Round 13 U3 (R1, R2; KTD2) against stored blocks and tables: the bounded context read of a quote value, and the
facet of a listed measurement read through its table's structure. Synthetic documents only; the rest of the facet
reading is database-free (`tests/unit/test_datum_facet.py`)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from sqlalchemy import text

from app.chat import tools as T
from app.db import tenant_tx
from app.measurements.extract import EXTRACTION_VERSION
from tests.factories import make_document, make_office
from tests.integration.test_chat_calculation import quote, run, workspace

pytestmark = pytest.mark.db

SECTION_A, SECTION_B, SECTION_C = "3. היוון התזרימים", "4. סיכום והערות", "5. נספח נתונים"
LEAD_IN = "תזרים הכנסות משטחי פרסום על הגג:"
TWO_RATES = "הובאה הפחתה של 10% בגין סחירות והפחתה של 20% בגין עוצמת ניצול."
BLOCKS = [  # (kind, section, text, table_index)
    ("heading", SECTION_A, SECTION_A, None),
    ("paragraph", SECTION_A, "שיעור היוון של 6.5% לתזרים דמי שכירות ממשרדים.", None),
    ("paragraph", SECTION_A, LEAD_IN, None),
    ("paragraph", SECTION_A, "שיעור היוון 9%.", None),
    ("heading", SECTION_B, SECTION_B, None),
    ("paragraph", SECTION_B, "השמאי הביא בחשבון את מכלול הנתונים שפורטו.", None),
    ("paragraph", SECTION_B, "שיעור היוון 10%.", None),
    ("table", SECTION_B, "טבלת חישוב שווי הקרקע", 0),
    ("paragraph", SECTION_B, TWO_RATES, None),  # round 14 U3: one sentence holding two numbers (two cache keys)
]
TABLE = {"headers": ["רכיב", "ערך"], "caption": "טבלת חישוב שווי הקרקע", "title": [], "notes": [],
         "section": SECTION_B, "block_index": 7,
         "rows": [{"cells": ["שטח קרקע", "620"]}, {"cells": ["מקדם הפחתה בשל שיתוף במקרקעין", "0.9"]}]}


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = make_document(a, a.default_group_id, "שומה סינתטית — רחוב הכלנית 8", sha="d" * 64)
    with tenant_tx(a.system) as conn:
        conn.execute(text("UPDATE document_versions SET page_count = 1, ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": ver, "i": json.dumps({"reading_id": "reading-1"})})
        for i, (kind, section, body, table) in enumerate(BLOCKS):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, page, text, status, table_index) VALUES (app_office(), :d, :v, :b, :k, :s, :sp, 1,"
                " :t, 'read', :ti)"),
                {"d": doc, "v": ver, "b": i, "k": kind, "s": section, "sp": [section], "t": body, "ti": table})
        conn.execute(text("INSERT INTO extracted_tables (office_id, document_id, version_id, table_index, page_start,"
                          " page_end, structure) VALUES (app_office(), :d, :v, 0, 1, 1, CAST(:s AS jsonb))"),
                     {"d": doc, "v": ver, "s": json.dumps(TABLE, ensure_ascii=False)})
        for statement_key, metric, kind, value, value_text, quote_text, table, row in (
                ("t0:r1:מקדם", "מקדם הפחתה", "coefficient", 0.9, "0.9", "מקדם הפחתה בשל שיתוף במקרקעין | 0.9", 0, 1),
                ("b1:x", "שיעור היוון", "rate", 6.5, "6.5%", BLOCKS[1][2], None, None)):
            conn.execute(text(
                "INSERT INTO measurements (office_id, document_id, version_id, block_index, table_index, row_index,"
                " statement_key, metric, metric_kind, value, value_form, value_text, unit, period, vat, subject_role,"
                " value_role, quote, extraction_version, model, status, issues) VALUES (app_office(), :d, :v, :bi,"
                " :ti, :ri, :sk, :m, :mk, :val, 'exact', :vt, 'ratio', 'none', 'unknown', 'general', 'other', :q, :e,"
                " 'test', 'auto_validated', '[]'::jsonb)"),
                {"d": doc, "v": ver, "bi": 7 if table is not None else 1, "ti": table, "ri": row, "sk": statement_key,
                 "m": metric, "mk": kind, "val": value, "vt": value_text, "q": quote_text, "e": EXTRACTION_VERSION})
    a.doc = str(doc)
    return a


def _source_of(ws, section: str) -> str:
    handle = re.search(r"(§\d+) «" + re.escape(section) + "»", T.tool_outline(ws, ws_doc(ws))).group(1)
    return re.search(r'<source id="(S\d+)"', T.tool_read(ws, {"section": handle})).group(1)


def ws_doc(ws) -> str:
    return ws.doc


def _meaning() -> dict:
    return {"kind": "rate", "unit": "percent", "period": "none", "vat": "unknown", "area_basis": "",
            "subject": "הנכס", "role": "other"}


def _take(office, section: str, sentence: str, number: str):
    ws = workspace(office)
    ws.doc = office.doc
    s = _source_of(ws, section)
    out = run(ws, "take_value", source=s, locator=quote(sentence.rstrip("."), number), meaning=_meaning(),
              label="שיעור")
    assert out.startswith("V1 נרשם"), out
    return ws, out


def test_a_quote_value_with_no_component_in_its_sentence_reads_the_lead_in_above_it(office):
    ws, out = _take(office, SECTION_A, BLOCKS[3][2], "9")
    f = ws.values["V1"].facet  # round 14 U2: a context facet also says where it was read
    assert {k: f[k] for k in ("status", "written", "part", "tier")} == {
        "status": "stated", "written": "תזרים הכנסות משטחי פרסום על הגג", "part": "lead_in", "tier": "context"}
    assert (f["block"], f["sentence"]) == (2, 0) and f["at"]["segments"][0][0] == 2
    assert "תזרים הכנסות משטחי פרסום על הגג" in out  # the model is told


def test_a_quote_value_whose_sentence_names_the_cash_flow_has_an_own_facet(office):
    ws, _ = _take(office, SECTION_A, BLOCKS[1][2], "6.5")
    assert ws.values["V1"].facet == {"status": "stated", "written": "תזרים דמי שכירות ממשרדים", "part": "sentence",
                                     "tier": "own"}


def test_a_heading_that_names_no_cash_flow_leaves_the_value_with_status_none(office):
    ws, out = _take(office, SECTION_B, BLOCKS[6][2], "10")
    assert ws.values["V1"].facet["status"] == "none" and ws.values["V1"].facet["reason"] == "not_in_range"
    assert "רכיב: לא צוין במקור" in out


def test_listed_measurements_carry_their_facets_in_the_listing_and_in_public(office):
    ws = workspace(office)
    out = T.tool_find_measurements(ws, "מקדם הפחתה")
    table_row = next(m for m in ws.measurements.values() if m.row.metric_kind == "coefficient")
    assert table_row.public()["facet"] == {"status": "stated", "written": "מקדם הפחתה בשל שיתוף במקרקעין",
                                           "part": "row", "tier": "own"}
    assert "מקדם הפחתה בשל שיתוף במקרקעין" in out.split("ציטוט")[0]
    text_ws = workspace(office)
    T.tool_find_measurements(text_ws, "שיעור היוון")
    text_row = next(m for m in text_ws.measurements.values() if m.row.metric_kind == "rate")
    assert text_row.public()["facet"]["written"] == "תזרים דמי שכירות ממשרדים"


# --- a page of table measurements: a bounded number of statements (review finding: one read per distinct table) -----------

EXTRA_TABLES = 6
COMPONENTS = ["גובה", "קומה", "מיקום", "שימוש", "גודל", "מצב"]


def _more_tables(office) -> list[str]:
    """``EXTRA_TABLES`` more tables (indexes 1..), each with two body rows and a measurement of the second row. The first
    holds a generic row label and a title line that names a component, the second a matrix (three cells per row)."""
    with tenant_tx(office.system) as conn:
        ver = conn.execute(text("SELECT id FROM document_versions WHERE document_id = :d"), {"d": office.doc}).scalar_one()
        for i in range(1, EXTRA_TABLES + 1):
            structure = {"headers": ["רכיב", "ערך"] if i != 2 else ["רכיב", "משרדים", "ערך"],
                         "caption": f"טבלה {i}", "title": ["מקדם גובה בנייה"] if i == 1 else [], "notes": [],
                         "section": SECTION_B, "block_index": 7,
                         "rows": [{"cells": ["שטח", "10"]},
                                  {"cells": ["הערך שנקבע" if i == 1 else f"מקדם {COMPONENTS[i - 1]}", "0.8"] if i != 2
                                           else [f"מקדם {COMPONENTS[1]}", "0.7", "0.8"]}]}
            conn.execute(text("INSERT INTO extracted_tables (office_id, document_id, version_id, table_index, page_start,"
                              " page_end, structure) VALUES (app_office(), :d, :v, :t, 1, 1, CAST(:s AS jsonb))"),
                         {"d": office.doc, "v": ver, "t": i, "s": json.dumps(structure, ensure_ascii=False)})
            conn.execute(text(
                "INSERT INTO measurements (office_id, document_id, version_id, block_index, table_index, row_index,"
                " statement_key, metric, metric_kind, value, value_form, value_text, unit, period, vat, subject_role,"
                " value_role, quote, extraction_version, model, status, issues) VALUES (app_office(), :d, :v, 7, :t,"
                " 1, :sk, 'מקדם התאמה', 'coefficient', 0.8, 'exact', '0.8', 'ratio', 'none', 'unknown', 'general',"
                " 'other', :q, :e, 'test', 'auto_validated', '[]'::jsonb)"),
                {"d": office.doc, "v": ver, "t": i, "sk": f"t{i}:r1:מקדם", "q": f"מקדם {COMPONENTS[i - 1]} | 0.8",
                 "e": EXTRACTION_VERSION})
    return [str(i) for i in range(1, EXTRA_TABLES + 1)]


def _listing_statements(office, query: str):
    from sqlalchemy import event

    from app.db import get_engine

    seen: list[str] = []

    def record(conn, cursor, statement, *args):  # noqa: ARG001
        if "set_config" not in statement:
            seen.append(statement)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", record)
    try:
        ws = workspace(office)
        T.tool_find_measurements(ws, query, metric_kinds=["coefficient"])
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return ws, seen


def test_a_page_of_table_measurements_reads_no_table_apart_and_its_facets_equal_the_structure_reads(office):
    from app.chat import reader

    _listing_statements(office, "מקדם")  # once to warm what the version caches, apart from the listing itself
    _, one = _listing_statements(office, "מקדם")  # the single table of the fixture
    _more_tables(office)
    ws, many = _listing_statements(office, "מקדם")
    assert len(ws.measurements) == 1 + EXTRA_TABLES
    # the listing's own query carries what the facets read: no table structure or block is fetched per table
    assert not [s for s in many if "FROM extracted_tables" in s and "JOIN" not in s]
    assert not [s for s in many if "FROM document_blocks" in s]
    assert len(many) == len(one)
    with tenant_tx(office.ctx()) as conn:
        for m in ws.measurements.values():
            structure = reader.table_structure(conn, m.row.version_id, m.row.table_index)
            assert m.facet == T._measurement_facet(m.row, structure), m.row.quote
    assert {m.facet["status"] for m in ws.measurements.values()} == {"stated"}
    assert {m.facet["part"] for m in ws.measurements.values()} == {"row", "caption"}  # a title line read as well


# --- a measurement the verifier adopts has its facet read (review finding: a phantom components row) ---------------------

def _adopt_beside_value(office, listed: bool):
    """The coefficient cell taken as V1, and the same cell's stored measurement adopted as M1 by the verifier's fetcher:
    ``listed`` as the fetcher's own query returns it (the table text columns), else as a plain ``m.*`` row."""
    import uuid

    from app.chat import meaning

    with tenant_tx(office.system) as conn:  # the stored row is the subject property's own determination
        conn.execute(text("UPDATE measurements SET subject_role = 'appraised_property',"
                          " value_role = 'appraiser_determination' WHERE metric_kind = 'coefficient'"))
    ws = workspace(office)
    ws.doc = office.doc
    outline = T.tool_outline(ws, ws_doc(ws))
    handle = re.search(r"(T\d+) «טבלת חישוב שווי הקרקע»", outline).group(1)
    s = re.search(r'<source id="(S\d+)"', T.tool_read(ws, {"table": handle})).group(1)
    taken = run(ws, "take_value", source=s, locator={"table": None, "row": "מקדם הפחתה בשל שיתוף במקרקעין",
                                                    "row_number": None, "column": "ערך", "column_number": None,
                                                    "quote": None, "number": None},
                meaning={"kind": "coefficient", "unit": "ratio", "period": "none", "vat": "unknown", "area_basis": "",
                         "subject": "הנכס", "role": "other"}, label="מקדם")
    assert taken.startswith("V1 נרשם"), taken
    fetcher = meaning.Fetcher(ws)
    if listed:
        row = fetcher.subject_measurements({uuid.UUID(office.doc)})[0]
    else:
        with tenant_tx(office.ctx()) as conn:
            row = conn.execute(text("SELECT m.*, d.title FROM measurements m JOIN documents d ON d.id = m.document_id"
                                    " WHERE m.metric_kind = 'coefficient'")).first()
    return ws, fetcher.adopt(row)


@pytest.mark.parametrize("listed", [True, False])
def test_a_table_measurement_adopted_beside_a_value_of_the_same_cell_is_one_component(office, listed):
    from app.chat import timeline

    ws, mid = _adopt_beside_value(office, listed)
    assert ws.measurements[mid].facet == ws.values["V1"].facet
    assert ws.measurements[mid].facet["status"] == "stated"
    timeline_blocks, breakdown = timeline.compose(ws, {"V1", mid})
    assert breakdown == []  # one class of the unit and its date: no second component row


def test_the_blocks_above_a_quote_are_read_for_the_family_of_the_settled_kind_not_the_given_one(office, monkeypatch):
    """The family that decides whether a quote value reads its context (`datum.needs_facet`) is the settled kind's, as the
    facet itself is read (`_value_facet`): a kind the settling changes is the one used for both."""
    real_settle, real_preceding, seen = T._settle_meaning, T._preceding_blocks, []

    def settle(taken, given):
        fields, prov, anchors = real_settle(taken, given)
        return fields | {"kind": "rate"}, prov, anchors

    def preceding(conn, cx, version_id, region, taken, kind):
        seen.append(kind)
        return real_preceding(conn, cx, version_id, region, taken, kind)

    monkeypatch.setattr(T, "_settle_meaning", settle)
    monkeypatch.setattr(T, "_preceding_blocks", preceding)
    ws = workspace(office)
    ws.doc = office.doc
    s = _source_of(ws, SECTION_A)
    run(ws, "take_value", source=s, locator=quote(BLOCKS[3][2].rstrip("."), "9"),
        meaning=_meaning() | {"kind": "income"}, label="שיעור")
    assert seen == ["rate"], seen


# =======================================================================================================================
# round 14 U2: the bounded context read against a stored DOCX (R1, R2; AE1-AE3; KTD3): two plots in two sections
# =======================================================================================================================

MANIFEST14 = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "round14" / "manifest.json").read_text(encoding="utf-8"))
DOC14 = MANIFEST14["documents"]["context_reader"]
DATA14 = {d["id"]: d for d in DOC14["data"]}
SHOWN = ("status", "written", "part", "tier", "reason")


@pytest.fixture
def office14(db, client, monkeypatch):
    """The round-14 context-reader DOCX, uploaded and read by the worker (text only), in a synthetic office."""
    from tests.conftest import login
    from tests.integration.test_property_scope import FIXTURES, upload

    o = make_office(db, "משרד סינתטי יד", "admin-r14f@example.test")
    login(client, "admin-r14f@example.test")
    o.client = client
    o.doc = upload(o, FIXTURES / DOC14["file"], DOC14["source_kind"], monkeypatch)
    return o


def _take14(office, ws, datum_id: str) -> str:
    """The value ``datum_id`` of the manifest, taken from the section that holds it, with the kind the manifest gives."""
    d = DATA14[datum_id]
    s = _source_of(ws, d["where"]["section"])
    number = re.sub(r"[^\d.,]", "", d["shown"])
    return run(ws, "take_value", source=s, locator=quote(d["where"]["quote"].rstrip("."), number),
               meaning=_meaning() | {"kind": "coefficient"}, label=datum_id)


def _workspace14(office):
    ws = workspace(office)
    ws.doc = office.doc
    return ws


@pytest.mark.parametrize("datum_id", list(DATA14))
def test_every_round14_datum_taken_from_the_stored_document_has_the_facet_the_manifest_states(office14, datum_id):
    ws = _workspace14(office14)
    out = _take14(office14, ws, datum_id)
    assert out.startswith("V1 נרשם"), out
    found = ws.values["V1"].facet
    assert {k: found.get(k) for k in SHOWN} == {k: DATA14[datum_id]["facet"].get(k) for k in SHOWN}


def test_the_second_plots_value_does_not_take_the_first_plots_consideration(office14):
    from app.chat import datum, timeline

    ws = _workspace14(office14)
    assert _take14(office14, ws, "r14.same_block").startswith("V1 נרשם")
    second_out = _take14(office14, ws, "r14.second_plot")
    assert second_out.startswith("V2 נרשם") and "רכיב: לא צוין במקור" in second_out
    first, second = ws.values["V1"].facet, ws.values["V2"].facet
    assert (first["status"], first["written"], first["part"]) == ("stated", "עוצמת ניצול", "preceding_sentence")
    assert (second["status"], second["written"], second["reason"]) == ("none", None, "not_in_range")
    d1, d2 = timeline.datums(ws)
    assert datum.relation(d1, d2) == datum.UNDECIDED  # two 10% values of two plots are not one datum


def test_a_context_sentence_is_located_in_its_stored_block_and_the_value_keeps_its_own_anchor(office14):
    ws = _workspace14(office14)
    _take14(office14, ws, "r14.same_block")
    f = ws.values["V1"].facet
    with tenant_tx(office14.ctx()) as conn:
        rows = conn.execute(text("SELECT block_index, text FROM document_blocks WHERE document_id = :d ORDER BY block_index"),
                            {"d": office14.doc}).all()
    by = dict(rows)
    block, start, end = f["at"]["segments"][0]
    assert block == f["block"] and by[block][start:end] == "השמאי לא נתן הפחתה בגין עוצמת ניצול."
    number = ws.anchors["V1"]["number"]  # the value's own locator is its own sentence, apart from the context's
    assert number[0] == block and number[1] >= end


def test_the_read_below_the_context_lookup_asks_the_database_once_for_the_blocks_above(office14):
    from sqlalchemy import event

    from app.db import get_engine

    seen: list[str] = []

    def record(conn, cursor, statement, *args):  # noqa: ARG001
        if "FROM document_blocks" in statement:
            seen.append(statement)

    ws = _workspace14(office14)
    s = _source_of(ws, DATA14["r14.prev_paragraph"]["where"]["section"])
    engine = get_engine()
    event.listen(engine, "before_cursor_execute", record)
    try:
        number = re.sub(r"[^\d.,]", "", DATA14["r14.prev_paragraph"]["shown"])
        run(ws, "take_value", source=s, locator=quote(DATA14["r14.prev_paragraph"]["where"]["quote"].rstrip("."), number),
            meaning=_meaning() | {"kind": "coefficient"}, label="x")
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert sum("ORDER BY block_index DESC LIMIT" in q for q in seen) == 1, seen


# =======================================================================================================================
# round 14 U3: every extraction path and the caches, against stored blocks (R4, R5, R17; AE7, AE8; KTD4, KTD5)
# =======================================================================================================================

def _statements(fn):
    """The SQL statements ``fn`` ran, without the session set-up."""
    from sqlalchemy import event

    from app.db import get_engine

    seen: list[str] = []

    def record(conn, cursor, statement, *args):  # noqa: ARG001
        if "set_config" not in statement:
            seen.append(statement)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", record)
    try:
        out = fn()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return out, seen


def _block_reads(statements: list[str]) -> list[str]:
    """The statements that read the stored blocks of a value's place or its context (``reader.blocks_between``,
    ``reader.blocks_above``), not the version's cached appraisal contexts."""
    return [s for s in statements if "FROM document_blocks WHERE version_id" in s]


def _add_text_measurement(office, block: int, quote_text: str, value_text: str, value: float, key: str) -> None:
    with tenant_tx(office.system) as conn:
        ver = conn.execute(text("SELECT id FROM document_versions WHERE document_id = :d"), {"d": office.doc}).scalar_one()
        conn.execute(text(
            "INSERT INTO measurements (office_id, document_id, version_id, block_index, statement_key, metric,"
            " metric_kind, value, value_form, value_text, unit, period, vat, subject_role, value_role, quote,"
            " extraction_version, model, status, issues) VALUES (app_office(), :d, :v, :bi, :sk, 'שיעור הפחתה', 'rate',"
            " :val, 'exact', :vt, 'ratio', 'none', 'unknown', 'general', 'other', :q, :e, 'test', 'auto_validated',"
            " '[]'::jsonb)"),
            {"d": office.doc, "v": ver, "bi": block, "sk": key, "val": value, "vt": value_text, "q": quote_text,
             "e": EXTRACTION_VERSION})


def test_a_text_measurement_gets_its_component_and_its_span_from_one_batched_block_read(office):
    _add_text_measurement(office, 3, BLOCKS[3][2], "9%", 9, "b3:x")
    _add_text_measurement(office, 3, "ציטוט שאינו בבלוק 9%.", "9%", 9, "b3:y")  # its quote is not in its block
    ws = workspace(office)
    _, seen = _statements(lambda: T.tool_find_measurements(ws, "שיעור", metric_kinds=["rate"]))
    by = {m.row.statement_key: m for m in ws.measurements.values()}
    lead = by["b3:x"].facet
    assert (lead["status"], lead["part"], lead["written"]) == ("stated", "lead_in", "תזרים הכנסות משטחי פרסום על הגג")
    assert by["b1:x"].facet["part"] == "sentence"  # an own phrase needs no context
    ver = str(by["b3:x"].row.version_id)
    assert ws.spans[by["b3:x"].mid] == ("quote", ver, "reading-1", 3, BLOCKS[3][2].index("9"))
    assert by["b3:y"].mid not in ws.spans  # a quote not found in its block has no span
    assert len([s for s in seen if "FROM document_blocks WHERE EXISTS" in s]) == 1  # one block query for the listing
    assert not _block_reads(seen)


def test_a_listing_beyond_the_context_cap_marks_the_extra_rows_not_read_with_the_reason(office, monkeypatch):
    _add_text_measurement(office, 3, BLOCKS[3][2], "9%", 9, "b3:x")
    _add_text_measurement(office, 6, BLOCKS[6][2], "10%", 10, "b6:x")
    monkeypatch.setattr(T, "MEASUREMENT_FACET_ROWS", 1)
    ws = workspace(office)
    _, seen = _statements(lambda: T.tool_find_measurements(ws, "שיעור", metric_kinds=["rate"]))
    by = {m.row.statement_key: m for m in ws.measurements.values()}
    read, capped = by["b3:x"].facet, by["b6:x"].facet  # the cap takes the listing's order: block 3 before block 6
    assert read["status"] == "stated" and (capped["status"], capped["reason"]) == ("not_read", "cap")
    assert len([s for s in seen if "FROM document_blocks WHERE EXISTS" in s]) == 1
    assert by["b6:x"].mid in ws.spans  # the span is read from the same query, whatever the facet cap


def _cached_rows(office) -> list:
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT locator, value FROM verified_values ORDER BY created_at, locator::text")).all()


def _q_of(ws, query: str) -> str:
    return re.search(r"(Q\d+)", T.tool_find_measurements(ws, query)).group(1)


def test_ae7_a_cached_none_is_read_again_and_only_the_facet_of_its_row_changes(office):
    ws, _ = _take(office, SECTION_A, BLOCKS[3][2], "9")
    fresh = ws.values["V1"].facet
    assert fresh["status"] == "stated" and fresh["part"] == "lead_in"
    (row,) = _cached_rows(office)
    assert row.value["record"]["facet_reader"] == T.FACET_READER_VERSION and row.locator["at"] == [3, BLOCKS[3][2].index("9")]
    # an earlier turn read it without the context: the record says none and names no reader (the round-13 shape)
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE verified_values SET value = jsonb_set(value #- '{record,facet_reader}', '{record,facet}',"
                          " CAST(:f AS jsonb))"), {"f": json.dumps(datum_none())})
    (stale,) = _cached_rows(office)
    assert "facet_reader" not in stale.value["record"] and stale.value["record"]["facet"]["status"] == "none"

    later = workspace(office)
    q = _q_of(later, "שיעור היוון")
    out = run(later, "take_value", source=q, locator={}, meaning=_meaning(), label="שיעור")
    assert out.startswith("V1 נרשם"), out
    shown = later.values["V1"].facet
    assert shown["status"] == "stated" and shown["written"] == fresh["written"] and shown["part"] == "lead_in"
    (after,) = _cached_rows(office)  # the row is completed in place: same key, same row, only the facet and its reader
    assert after.locator == stale.locator
    changed = {k for k in set(after.value["record"]) | set(stale.value["record"])
               if after.value["record"].get(k) != stale.value["record"].get(k)}
    assert changed == {"facet", "facet_reader"}
    assert {k: v for k, v in after.value.items() if k != "record"} == {k: v for k, v in stale.value.items() if k != "record"}
    assert after.value["record"]["facet"] == shown and after.value["record"]["facet_reader"] == T.FACET_READER_VERSION

    # and it is stated now: a later take reuses it with no read of the blocks, and the verifier version is the same
    again = workspace(office)
    q = _q_of(again, "שיעור היוון")
    _, seen = _statements(lambda: run(again, "take_value", source=q, locator={}, meaning=_meaning(), label="שיעור"))
    assert not _block_reads(seen) and again.values["V1"].facet == shown
    assert len(_cached_rows(office)) == 1 and T.VALUE_VERIFIER_VERSION == "value-v5"


def test_review7_a_stale_stated_context_facet_is_replaced_in_place_by_the_careful_read_and_a_current_one_is_not(office):
    """A round-13 row holds a stated lead-in; the stored text above the value no longer names a component, so the current
    reader finds none: the row's facet is corrected (facet and reader only). The same row, once written by the current reader,
    is served as it is, whatever the blocks say."""
    ws, _ = _take(office, SECTION_A, BLOCKS[3][2], "9")
    stated = ws.values["V1"].facet
    assert stated["status"] == "stated" and stated["tier"] == "context"
    with tenant_tx(office.system) as conn:  # the round-13 record: stated context facet, no reader named
        conn.execute(text("UPDATE verified_values SET value = value #- '{record,facet_reader}'"))
        conn.execute(text("UPDATE document_blocks SET text = 'פירוט נוסף להלן:' WHERE block_index IN (1, 2)"))
    (before,) = _cached_rows(office)
    assert "facet_reader" not in before.value["record"] and before.value["record"]["facet"] == stated

    later = workspace(office)
    out = run(later, "take_value", source=_q_of(later, "שיעור היוון"), locator={}, meaning=_meaning(), label="שיעור")
    assert out.startswith("V1 נרשם"), out
    assert later.values["V1"].facet["status"] == "none", "the old reader's borrowed lead-in is not presented as the source's"
    (after,) = _cached_rows(office)  # same row, same key, only the facet and its reader changed; nothing deleted
    assert after.locator == before.locator
    changed = {k for k in set(after.value["record"]) | set(before.value["record"])
               if after.value["record"].get(k) != before.value["record"].get(k)}
    assert changed == {"facet", "facet_reader"}
    assert after.value["record"]["facet"] == later.values["V1"].facet
    assert after.value["record"]["facet_reader"] == T.FACET_READER_VERSION and len(_cached_rows(office)) == 1

    # the other direction: a stated context facet the current reader wrote is never lowered by a later read
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE verified_values SET value = jsonb_set(jsonb_set(value, '{record,facet}', CAST(:f AS jsonb)),"
                          " '{record,facet_reader}', CAST(:r AS jsonb))"),
                     {"f": json.dumps(stated, ensure_ascii=False), "r": json.dumps(T.FACET_READER_VERSION)})
    (kept_before,) = _cached_rows(office)
    again = workspace(office)
    run(again, "take_value", source=_q_of(again, "שיעור היוון"), locator={}, meaning=_meaning(), label="שיעור")
    assert again.values["V1"].facet == stated
    (kept_after,) = _cached_rows(office)
    assert kept_after.value == kept_before.value
    # and the write guard itself, at the SQL: a nothing-facet never overwrites a stated facet of the current reader
    with tenant_tx(office.system) as conn:
        T._write_facet(conn, *_row_key(conn), datum_none())
    (guarded,) = _cached_rows(office)
    assert guarded.value == kept_before.value


def _row_key(conn) -> tuple:
    row = conn.execute(text("SELECT version_id, reading_id, locator FROM verified_values")).one()
    return row.version_id, row.reading_id, row.locator


def datum_none() -> dict:
    from app.chat import datum

    return datum.none()


def test_two_numbers_of_one_quote_are_cached_under_two_keys_and_an_older_quote_only_record_of_one_number_is_found(office):
    ws = workspace(office)
    ws.doc = office.doc
    s = _source_of(ws, SECTION_B)
    for number in ("10", "20"):
        out = run(ws, "take_value", source=s, locator=quote(TWO_RATES.rstrip("."), number), meaning=_meaning(), label=number)
        assert out.startswith("V"), out
    rows = _cached_rows(office)
    assert len(rows) == 2 and len({json.dumps(r.locator, sort_keys=True) for r in rows}) == 2
    assert {r.locator["at"][1] for r in rows} == {TWO_RATES.index("10"), TWO_RATES.index("20")}
    assert {r.value["record"]["value"] for r in rows} == {"10", "20"}

    # a record written before this round is keyed by its quote alone: found while that quote holds a single number
    one, _ = _take(office, SECTION_A, BLOCKS[3][2], "9")
    legacy_quote = BLOCKS[3][2].rstrip(".")
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE verified_values SET locator = locator - 'at' WHERE locator->>'quote' = :q"),
                     {"q": legacy_quote})
    span = one.spans["V1"]
    with tenant_tx(office.ctx()) as conn:
        ver = conn.execute(text("SELECT id FROM document_versions WHERE document_id = :d"), {"d": office.doc}).scalar_one()
        found = T._cached_value_at(conn, ver, "reading-1", {"quote": legacy_quote}, span)
        assert found is not None and found["record"]["value"] == "9"
        # a quote that holds two numbers is never answered by a quote-only record
        assert T._cached_value_at(conn, ver, "reading-1", {"quote": TWO_RATES.rstrip(".")}, ("quote", str(ver), "reading-1", 8, 99)) is None
    assert len(_cached_rows(office)) == 3  # nothing deleted along the way


def test_a_fresh_take_that_reads_a_component_completes_a_cached_record_that_had_none(office):
    _take(office, SECTION_B, BLOCKS[6][2], "10")  # nothing names a component for it: none
    (row,) = _cached_rows(office)
    assert row.value["record"]["facet"]["status"] == "none"
    # the paragraph above it now names one (the stored text is edited for the test); a fresh take reads it and completes the row
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_blocks SET text = 'שיעור היוון נקבע לתזרים הכנסות משטחי חניה.'"
                          " WHERE block_index = 5"))
    later = workspace(office)
    later.doc = office.doc
    s = _source_of(later, SECTION_B)
    out = run(later, "take_value", source=s, locator=quote(BLOCKS[6][2].rstrip("."), "10"), meaning=_meaning(), label="שיעור")
    assert out.startswith("V1 נרשם"), out
    assert later.values["V1"].facet["status"] == "stated"
    (after,) = _cached_rows(office)
    assert after.value["record"]["facet"] == later.values["V1"].facet and after.locator == row.locator


def test_a_text_measurement_the_verifier_adopts_has_its_context_read_and_its_span_set(office):
    import uuid

    from app.chat import meaning

    _add_text_measurement(office, 3, BLOCKS[3][2], "9%", 9, "b3:x")
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE measurements SET subject_role = 'appraised_property',"
                          " value_role = 'appraiser_determination' WHERE statement_key = 'b3:x'"))
    ws = workspace(office)
    fetcher = meaning.Fetcher(ws)
    row = next(r for r in fetcher.subject_measurements({uuid.UUID(office.doc)}) if r.statement_key == "b3:x")
    mid = fetcher.adopt(row)
    facet = ws.measurements[mid].facet
    assert (facet["status"], facet["part"], facet["written"]) == ("stated", "lead_in", "תזרים הכנסות משטחי פרסום על הגג")
    assert ws.spans[mid] == ("quote", str(row.version_id), "reading-1", 3, BLOCKS[3][2].index("9"))


def test_ae8_a_measurement_read_from_the_text_of_a_picture_says_its_context_was_not_read(office):
    _add_text_measurement(office, 3, BLOCKS[3][2], "9%", 9, "b3:x")
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_blocks SET kind = 'image' WHERE block_index = 3"))
    ws = workspace(office)
    T.tool_find_measurements(ws, "שיעור", metric_kinds=["rate"])
    facet = next(m.facet for m in ws.measurements.values() if m.row.statement_key == "b3:x")
    assert (facet["status"], facet["reason"]) == ("not_read", "picture_cell")


# --- review #5: the tool layer's context reads that no other test executed (table notes, the unit floor, the block cap) ----------

NOTE = "הערך שנקבע הוא מקדם הפחתה בגין עוצמת ניצול."
GENERIC_ROW = "הערך שנקבע"


def _noted_tables(office, indexes: tuple[int, ...], notes: list[str]) -> None:
    """Tables (``indexes``) whose body row has a generic label and no caption or title naming a component, with a coefficient
    measurement on that row: only a note of the table can name its component."""
    with tenant_tx(office.system) as conn:
        ver = conn.execute(text("SELECT id FROM document_versions WHERE document_id = :d"), {"d": office.doc}).scalar_one()
        for i in indexes:
            structure = {"headers": ["רכיב", "ערך"], "caption": f"טבלה {i}", "title": [], "notes": notes,
                         "section": SECTION_B, "block_index": 7,
                         "rows": [{"cells": ["שטח", "10"]}, {"cells": [GENERIC_ROW, "0.8"]}]}
            conn.execute(text("INSERT INTO extracted_tables (office_id, document_id, version_id, table_index, page_start,"
                              " page_end, structure) VALUES (app_office(), :d, :v, :t, 1, 1, CAST(:s AS jsonb))"),
                         {"d": office.doc, "v": ver, "t": i, "s": json.dumps(structure, ensure_ascii=False)})
            conn.execute(text(
                "INSERT INTO measurements (office_id, document_id, version_id, block_index, table_index, row_index,"
                " statement_key, metric, metric_kind, value, value_form, value_text, unit, period, vat, subject_role,"
                " value_role, quote, extraction_version, model, status, issues) VALUES (app_office(), :d, :v, 7, :t,"
                " 1, :sk, 'מקדם התאמה', 'coefficient', 0.8, 'exact', '0.8', 'ratio', 'none', 'unknown', 'general',"
                " 'other', :q, :e, 'test', 'auto_validated', '[]'::jsonb)"),
                {"d": office.doc, "v": ver, "t": i, "sk": f"t{i}:r1:הערך", "q": f"{GENERIC_ROW} | 0.8",
                 "e": EXTRACTION_VERSION})


def _notes_queries(statements: list[str]) -> list[str]:
    return [s for s in statements if "structure->'notes'" in s]


def test_a_bare_table_row_reads_its_facet_from_the_note_of_its_table_in_one_query_per_page_and_no_query_without_bare_rows(office):
    # the fixture's one coefficient row names its component itself: nothing is bare, so the notes are not asked for
    ws = workspace(office)
    _, seen = _statements(lambda: T.tool_find_measurements(ws, "מקדם", metric_kinds=["coefficient"]))
    assert {m.facet["status"] for m in ws.measurements.values()} == {"stated"} and not _notes_queries(seen)
    # two more tables whose generic row label only a note explains: both read in ONE notes query
    _noted_tables(office, (1, 2), [NOTE])
    ws = workspace(office)
    _, seen = _statements(lambda: T.tool_find_measurements(ws, "מקדם", metric_kinds=["coefficient"]))
    assert len(_notes_queries(seen)) == 1, "the notes of every bare table of a page come from one statement"
    noted = [m for m in ws.measurements.values() if m.row.statement_key.startswith(("t1:", "t2:"))]
    assert len(noted) == 2
    for m in noted:
        assert (m.facet["status"], m.facet["part"], m.facet["tier"], m.facet["written"]) == (
            "stated", "table_note", "context", "עוצמת ניצול")
    # a note that names another row's label settles nothing for this one
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE extracted_tables SET structure = jsonb_set(structure, '{notes}', CAST(:n AS jsonb))"
                          " WHERE table_index IN (1, 2)"), {"n": json.dumps(["שטח: הפחתה בגין עוצמת ניצול."], ensure_ascii=False)})
    ws = workspace(office)
    T.tool_find_measurements(ws, "מקדם", metric_kinds=["coefficient"])
    assert {m.facet["status"] for m in ws.measurements.values() if m.row.statement_key.startswith(("t1:", "t2:"))} == {"none"}


def _open_the_way(conn) -> None:
    """Blocks 3 and 4 become plain paragraphs without a number: a heading ends the context read, a number stands between."""
    conn.execute(text("UPDATE document_blocks SET text = 'הנתונים נבדקו.' WHERE block_index = 3"))
    conn.execute(text("UPDATE document_blocks SET text = 'המסמכים נסקרו.', kind = 'paragraph' WHERE block_index = 4"))


def _two_appraisals(office, *, leak: bool) -> None:
    """The fixture's blocks as one section of two appraisal units (blocks 0-3 and 4-8): the consideration is named in the first
    unit (block 2, a lead-in), the reduction (block 6) is bare and sits in the second. One section path: only the unit's first
    block (the floor) keeps the first unit's lead-in from being borrowed. ``leak``: block 3 carries no number, so nothing but the
    floor stands between them."""
    from app.chat import units

    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_blocks SET section = :s, section_path = :sp"), {"s": SECTION_A, "sp": [SECTION_A]})
        if leak:  # no number and no heading between the lead-in and the reduction but the unit edge
            _open_the_way(conn)
        ver = conn.execute(text("SELECT id FROM document_versions WHERE document_id = :d"), {"d": office.doc}).scalar_one()
        reading = units.reading_of(conn, ver)
        units._insert(conn, ver, reading, [
            units.Unit(1, "appraisal", "שומה סינתטית א", (), {}, 1, 1, 0, 3, "certain", {}),
            units.Unit(2, "appraisal", "שומה סינתטית ב", (), {}, 1, 1, 4, 8, "certain", {})])


def test_the_context_read_stops_at_the_first_block_of_the_appraisal_unit_for_take_value_and_for_a_listing(office):
    # without a second unit the lead-in two blocks above is the reduction's component (the read reaches it)
    _add_text_measurement(office, 6, BLOCKS[6][2], "10%", 10, "b6:x")
    _two_appraisals(office, leak=True)
    ws = workspace(office)
    ws.doc = office.doc
    T.tool_outline(ws, ws.doc)
    # the second unit's text starts before its first heading: its section is the one the outline lists under context 2
    second = re.search(r'<source id="(S\d+)"', T.tool_read(ws, {"section": "§2"})).group(1)
    out = run(ws, "take_value", source=second, locator=quote(BLOCKS[6][2].rstrip("."), "10"), meaning=_meaning(),
              label="שיעור")
    assert out.startswith("V1 נרשם"), out
    taken = ws.values["V1"].facet
    assert (taken["status"], taken["reason"]) == ("none", "not_in_range"), "unit 1's lead-in is not unit 2's component"
    listed = workspace(office)
    T.tool_find_measurements(listed, "שיעור", metric_kinds=["rate"])
    by = {m.row.statement_key: m for m in listed.measurements.values()}
    assert (by["b6:x"].facet["status"], by["b6:x"].facet["reason"]) == ("none", "not_in_range")
    assert by["b1:x"].facet["part"] == "sentence"  # a value with its own phrase is unchanged


def test_without_a_unit_edge_the_same_read_borrows_the_lead_in_so_the_floor_is_what_stops_it(office):
    """The control of the test above: same text, one unit."""
    _add_text_measurement(office, 6, BLOCKS[6][2], "10%", 10, "b6:x")
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_blocks SET section = :s, section_path = :sp"), {"s": SECTION_A, "sp": [SECTION_A]})
        _open_the_way(conn)
    ws = workspace(office)
    ws.doc = office.doc
    out = run(ws, "take_value", source=_source_of(ws, SECTION_A), locator=quote(BLOCKS[6][2].rstrip("."), "10"),
              meaning=_meaning(), label="שיעור")
    assert out.startswith("V1 נרשם"), out
    assert (ws.values["V1"].facet["status"], ws.values["V1"].facet["part"]) == ("stated", "lead_in")


def test_more_than_the_candidate_blocks_of_filler_above_a_value_make_the_read_capped_not_none(office):
    filler = T.CANDIDATE_BLOCKS + 5
    target = 11 + filler
    with tenant_tx(office.system) as conn:
        ver = conn.execute(text("SELECT id FROM document_versions WHERE document_id = :d"), {"d": office.doc}).scalar_one()
        rows = [("heading", SECTION_C, 9), ("paragraph", LEAD_IN, 10)]
        rows += [("paragraph", "נבדק.", 11 + i) for i in range(filler)] + [("paragraph", "שיעור היוון 12%.", target)]
        for kind, body, index in rows:
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, page, text, status, table_index) VALUES (app_office(), :d, :v, :b, :k, :s, :sp, 1,"
                " :t, 'read', NULL)"),
                {"d": office.doc, "v": ver, "b": index, "k": kind, "s": SECTION_C, "sp": [SECTION_C], "t": body})
    _add_text_measurement(office, target, "שיעור היוון 12%.", "12%", 12, "far:x")
    ws = workspace(office)
    ws.doc = office.doc
    out = run(ws, "take_value", source=_source_of(ws, SECTION_C), locator=quote("שיעור היוון 12%".rstrip("."), "12"),
              meaning=_meaning(), label="שיעור")
    assert out.startswith("V1 נרשם"), out
    taken = ws.values["V1"].facet
    assert (taken["status"], taken["reason"]) == ("not_read", "cap"), "a search cut by the block limit is not 'none'"
    listed = workspace(office)
    T.tool_find_measurements(listed, "שיעור היוון", metric_kinds=["rate"])
    far = next(m for m in listed.measurements.values() if m.row.statement_key == "far:x")
    assert (far.facet["status"], far.facet["reason"]) == ("not_read", "cap")


# --- a PDF paragraph block keeps its line wraps: a wrap is no sentence end (round 14 follow-up) -----------------------------

# the block as PDF extraction stores it: lines joined by "\n", wrapping mid-sentence ("בגין\nאינטנסיביות", "נוסף\nשל 10%.")
WRAPPED_BLOCK = ("בחשבון הפחתה בגין מיקום של 20% זאת בהתחשב בכך שמדובר\nבנכס פינתי. הפחתה כאמור הובאה בחשבון\nגם בחוות דעת זו.\n"
                 "השמאי הקודם לא מצא לנכון לתת הפחתה בגין\nאינטנסיביות שימוש. יצוין כי ממועד עריכת הטבלאות פסקה הפעילות, "
                 "לאור האמור, בחוות דעת זו הבאתי בחשבון מקדם הפחתה נוסף\nשל 10%.")
WRAPPED_QUOTE = "בחוות דעת זו הבאתי בחשבון מקדם הפחתה נוסף של 10%"
WRAPPED_BLOCK_INDEX = 10


def _add_wrapped_block(office) -> None:
    """A heading and the wrapped paragraph as blocks 9 and 10 of a section of their own."""
    with tenant_tx(office.system) as conn:
        ver = conn.execute(text("SELECT id FROM document_versions WHERE document_id = :d"), {"d": office.doc}).scalar_one()
        for index, kind, body in ((9, "heading", SECTION_C), (WRAPPED_BLOCK_INDEX, "paragraph", WRAPPED_BLOCK)):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, page, text, status, table_index) VALUES (app_office(), :d, :v, :b, :k, :s, :sp, 1,"
                " :t, 'read', NULL)"),
                {"d": office.doc, "v": ver, "b": index, "k": kind, "s": SECTION_C, "sp": [SECTION_C], "t": body})


def _assert_wrapped_context(facet: dict) -> None:
    assert (facet["status"], facet["written"], facet["part"], facet["tier"]) == (
        "stated", "אינטנסיביות שימוש", "preceding_sentence", "context"), facet
    assert (facet["block"], facet["sentence"]) == (WRAPPED_BLOCK_INDEX, 2)  # the third sentence, wraps read as spaces
    segment = facet["at"]["segments"][0]  # `at` is located in the STORED block text, wraps included
    assert segment[0] == WRAPPED_BLOCK_INDEX
    assert " ".join(WRAPPED_BLOCK[segment[1]:segment[2]].split()) == "השמאי הקודם לא מצא לנכון לתת הפחתה בגין אינטנסיביות שימוש."


def test_a_value_in_a_wrapped_paragraph_block_takes_its_component_from_the_earlier_sentence_across_the_wraps(office):
    _add_wrapped_block(office)
    ws = workspace(office)
    ws.doc = office.doc
    out = run(ws, "take_value", source=_source_of(ws, SECTION_C), locator=quote(WRAPPED_QUOTE, "10"), meaning=_meaning(),
              label="שיעור")
    assert out.startswith("V1 נרשם"), out
    _assert_wrapped_context(ws.values["V1"].facet)
    assert "אינטנסיביות שימוש" in out  # the model is told


def test_a_text_measurement_in_a_wrapped_paragraph_block_has_the_same_component(office):
    _add_wrapped_block(office)
    _add_text_measurement(office, WRAPPED_BLOCK_INDEX, "של 10%.", "10%", 10, "wrap:x")
    ws = workspace(office)
    T.tool_find_measurements(ws, "שיעור", metric_kinds=["rate"])
    row = next(m for m in ws.measurements.values() if m.row.statement_key == "wrap:x")
    _assert_wrapped_context(row.facet)


# --- label-value lines with no end punctuation: each value takes its own line's component ---------------------------------

# a PDF paragraph block of label-value lines (synthetic); no line ends with punctuation
LABEL_LINES = [("שיעור היוון לתזרים הפיקדונות 7.75%", "7.75%", "7.75", "תזרים הפיקדונות"),
               ("שיעור היוון לתזרים התפעול 10.00%", "10.00%", "10.00", "תזרים התפעול"),
               ('שיעור היוון לתזרים תשלומי מע"מ ופחת 8.00%', "8.00%", "8.00", 'תזרים תשלומי מע"מ ופחת'),
               ("שיעור היוון להכנסות משכירות 7.25%", "7.25%", "7.25", "הכנסות משכירות")]
LABEL_BLOCK = "\n".join(line for line, *_ in LABEL_LINES)
LABEL_BLOCK_INDEX = 10


def _add_label_block(office) -> None:
    with tenant_tx(office.system) as conn:
        ver = conn.execute(text("SELECT id FROM document_versions WHERE document_id = :d"), {"d": office.doc}).scalar_one()
        for index, kind, body in ((9, "heading", SECTION_C), (LABEL_BLOCK_INDEX, "paragraph", LABEL_BLOCK)):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, page, text, status, table_index) VALUES (app_office(), :d, :v, :b, :k, :s, :sp, 1,"
                " :t, 'read', NULL)"),
                {"d": office.doc, "v": ver, "b": index, "k": kind, "s": SECTION_C, "sp": [SECTION_C], "t": body})


@pytest.mark.parametrize(("line", "_written", "number", "component"), LABEL_LINES)
def test_a_value_on_a_label_value_line_of_a_stored_block_takes_its_own_lines_component(office, line, _written, number, component):
    _add_label_block(office)
    ws = workspace(office)
    ws.doc = office.doc
    out = run(ws, "take_value", source=_source_of(ws, SECTION_C), locator=quote(line, number), meaning=_meaning(), label="שיעור")
    assert out.startswith("V1 נרשם"), out
    facet = ws.values["V1"].facet
    assert (facet["status"], facet["written"], facet["part"], facet["tier"]) == ("stated", component, "sentence", "own"), facet


def test_text_measurements_on_label_value_lines_each_have_their_own_lines_component(office):
    _add_label_block(office)
    for i, (line, written, _number, _component) in enumerate(LABEL_LINES):
        _add_text_measurement(office, LABEL_BLOCK_INDEX, line, written, float(written.rstrip("%")), f"lv{i}:x")
    ws = workspace(office)
    T.tool_find_measurements(ws, "שיעור", metric_kinds=["rate"])
    for i, (_line, _written, _number, component) in enumerate(LABEL_LINES):
        row = next(m for m in ws.measurements.values() if m.row.statement_key == f"lv{i}:x")
        assert (row.facet["status"], row.facet["written"], row.facet["tier"]) == ("stated", component, "own"), row.facet
