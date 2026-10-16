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

SECTION_A, SECTION_B = "3. היוון התזרימים", "4. סיכום והערות"
LEAD_IN = "תזרים הכנסות משטחי פרסום על הגג:"
BLOCKS = [  # (kind, section, text, table_index)
    ("heading", SECTION_A, SECTION_A, None),
    ("paragraph", SECTION_A, "שיעור היוון של 6.5% לתזרים דמי שכירות ממשרדים.", None),
    ("paragraph", SECTION_A, LEAD_IN, None),
    ("paragraph", SECTION_A, "שיעור היוון 9%.", None),
    ("heading", SECTION_B, SECTION_B, None),
    ("paragraph", SECTION_B, "השמאי הביא בחשבון את מכלול הנתונים שפורטו.", None),
    ("paragraph", SECTION_B, "שיעור היוון 10%.", None),
    ("table", SECTION_B, "טבלת חישוב שווי הקרקע", 0),
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
