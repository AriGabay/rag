"""Round 13 U3 (R1, R2; KTD1, KTD2, KTD3 reading and normalization, KTD19): every registered value (V#) and listed
measurement (M#) carries a facet, the component its source states, read from the source's own words only.

A table cell takes its row label (a column header when the row label states nothing, the caption as context), a quote
value takes the purpose phrase of its own sentence (a list lead-in or a heading within three blocks as context), and
what the model wrote (`label`, `subject`) never feeds it. The expected facets are the round-13 manifest's, read from
the synthetic fixtures; database-free (the preceding blocks come from the fixture's own reading)."""

from __future__ import annotations

import contextlib
import json
import time
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.chat import calc, datum
from app.chat import tools as T
from app.chat.tools import Workspace
from app.config import get_settings
from app.extraction.docx import extract_docx

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANIFEST = json.loads((FIXTURES / "round13" / "manifest.json").read_text(encoding="utf-8"))
DOC, VER = uuid.UUID(int=21), uuid.UUID(int=22)

STATED_ROW = "מקדם הפחתה בשל זכויות בנייה חלקיות"
LEAD_IN = "תזרים הכנסות משטחי פרסום על הגג:"


def _source(text: str, kind: str = "context") -> T.Source:
    return T.Source(sid="S1", document_id=DOC, version_id=VER, title="שומה סינתטית", section="3. היוון התזרימים",
                    location="עמוד 1", kind=kind, text=text, block_start=0, block_end=0, reading_id="r1",
                    status="complete")


def _given(**kw) -> dict:
    return {"kind": "rate", "unit": "percent", "period": "unknown", "vat": "unknown", "area_basis": "",
            "subject": "הנכס", "role": "other", "stated_by": "", "stance": "unknown", "scenario": ""} | kw


def _register(taken: dict, given: dict, src: T.Source, label: str = "תווית") -> calc.Value:
    settled = T._settle(taken, given, ())
    return T._new_value(Workspace(ctx=None), src, "S1", taken, settled, given, label, (), "r1")


def _cell(table: dict, row: str, column: str, **given) -> calc.Value:
    full = T._table_text(table, table["rows"])
    src = _source(full, "table")
    taken = T._cell_of(src, full, {"row": row, "column": column}, table, 0)
    return _register(taken, _given(kind="coefficient", unit="ratio") | given, src)


def _quote(text: str, number: str, preceding=None, **given) -> calc.Value:
    src = _source(text)
    taken = T._take_quote(src, text, {"quote": text, "number": number}, [(0, text, 1)])
    if preceding is not None:
        taken["preceding"] = preceding
    return _register(taken, _given(**given), src)


def _table(headers, rows, caption="", title=()) -> dict:
    return {"headers": list(headers), "caption": caption, "title": list(title), "notes": [],
            "rows": [{"cells": list(r), "page": 1} for r in rows]}


def _facet(written, part, tier="own") -> dict:
    return {"status": "stated", "written": written, "part": part, "tier": tier}


NONE = {"status": "none", "written": None, "part": None, "tier": None}


# --- table cells: the row label, the column header, the caption ----------------------------------------------------

def test_a_cell_in_a_row_labelled_with_a_consideration_gets_that_label_as_an_own_facet_from_the_row():
    table = _table(["רכיב", "ערך"], [["שטח קרקע", "620"], [STATED_ROW, "0.8"]], "טבלת חישוב שווי הקרקע")
    assert _cell(table, STATED_ROW, "ערך").facet == _facet(STATED_ROW, "row")


def test_a_generic_row_label_states_nothing_and_a_matrix_column_header_does():
    table = _table(["נתון", "מקדם מיקום", "מקדם צורת מגרש"], [["הערך שנקבע", "0.95", "1.05"]], "טבלת מקדמי התאמה לנכס")
    assert _cell(table, "הערך שנקבע", "מקדם מיקום").facet == _facet("מקדם מיקום", "column")
    assert _cell(table, "הערך שנקבע", "מקדם צורת מגרש").facet == _facet("מקדם צורת מגרש", "column")


def test_the_caption_is_a_context_facet_only_when_no_row_or_column_states_one():
    table = _table(["מדד", "ערך"], [["הערך שנקבע", "0.85"]], "מקדם הפחתה בשל קרבה לקו מתח עליון")
    assert _cell(table, "הערך שנקבע", "ערך").facet == _facet("מקדם הפחתה בשל קרבה לקו מתח עליון", "caption", "context")
    # a caption that names no consideration (only generic words) is no facet
    plain = _table(["מדד", "ערך"], [["הערך שנקבע", "0.85"]], "טבלת מקדמי התאמה לנכס")
    assert _cell(plain, "הערך שנקבע", "ערך").facet == NONE


def test_a_row_label_and_a_column_header_that_both_name_things_in_a_matrix_are_one_pair():
    table = _table(["נכס", "מקדם מיקום", "מקדם צורת מגרש"], [["הדולב 4", "0.95", "1.05"]])
    f = _cell(table, "הדולב 4", "מקדם מיקום").facet
    assert f["status"] == "stated" and f["part"] == "row" and f["tier"] == "own"
    assert "הדולב 4" in f["written"] and "מקדם מיקום" in f["written"]


def test_a_column_of_years_is_no_facet():
    table = _table(["רכיב", "2022", "2023"], [["שכירות משרדים", "6.5", "7"]])
    f = _cell(table, "שכירות משרדים", "2023", kind="rate", unit="percent").facet
    assert f == _facet("שכירות משרדים", "row")  # the row states it; the year is a period, never the component


# --- quote values: the purpose phrase of the value's own sentence --------------------------------------------------

def test_a_quote_names_its_cash_flow_after_the_number_as_an_own_facet_from_the_sentence():
    text = "שיעור היוון של 6.5% לתזרים דמי שכירות ממשרדים."
    assert _quote(text, "6.5").facet == _facet("תזרים דמי שכירות ממשרדים", "sentence")
    other = "שיעור היוון של 7.25% עבור תזרים דמי שכירות מחניות."
    assert _quote(other, "7.25").facet == _facet("תזרים דמי שכירות מחניות", "sentence")
    third = "שיעור היוון של 7.25% בגין תזרים דמי שכירות ממחסנים."
    assert _quote(third, "7.25").facet == _facet("תזרים דמי שכירות ממחסנים", "sentence")


def test_a_purpose_phrase_between_the_metric_word_and_the_number_is_read_too():
    text = "שיעור היוון לתזרים ערך השייר הוא 8.5%."
    assert _quote(text, "8.5").facet == _facet("תזרים ערך השייר", "sentence")


def test_function_words_at_the_edges_of_a_purpose_phrase_are_trimmed_before_and_after_the_number():
    before = "הובאה בחשבון הפחתה לסחירות וגמישות מועטה של שימושים של 20%."
    f = _quote(before, "20", preceding=[], kind="coefficient", unit="percent").facet
    assert f == _facet("סחירות וגמישות מועטה של שימושים", "sentence")  # the inner "של" stays, the trailing one goes
    after = "שיעור היוון של 6.5% לתזרים דמי שכירות ממשרדים בשיעור -"
    assert _quote(after, "6.5", preceding=[]).facet == _facet("תזרים דמי שכירות ממשרדים", "sentence")
    assert datum.tidy("על: שימושים של ") == "שימושים"
    assert datum.core("סחירות וגמישות מועטה של שימושים של", "coefficient") == datum.core(
        "סחירות וגמישות מועטה של שימושים", "coefficient")


def test_a_lead_in_naming_a_cash_flow_is_a_context_facet_when_the_sentence_names_none():
    f = _quote("שיעור היוון 9%.", "9", preceding=[("paragraph", LEAD_IN)]).facet
    assert f == _facet("תזרים הכנסות משטחי פרסום על הגג", "lead_in", "context")


def test_the_nearest_heading_is_the_context_facet_when_there_is_no_lead_in():
    f = _quote("שיעור היוון 5.5%.", "5.5", preceding=[("heading", "תזרים הכנסות מתחנת טעינה")]).facet
    assert f == _facet("תזרים הכנסות מתחנת טעינה", "heading", "context")


def test_a_rate_with_neither_a_lead_in_nor_a_heading_naming_a_cash_flow_has_status_none():
    bare = [("paragraph", "השמאי הביא בחשבון את מכלול הנתונים שפורטו."), ("heading", "4. סיכום והערות")]
    assert _quote("שיעור היוון 10%.", "10", preceding=bare).facet == NONE
    assert _quote("שיעור היוון 10%.", "10", preceding=[]).facet == NONE
    # only generic words in the section heading: no facet
    assert _quote("שיעור היוון 10%.", "10", preceding=[("heading", "3. היוון התזרימים")]).facet == NONE


def test_a_sentence_read_without_its_preceding_blocks_is_not_read():
    assert _quote("שיעור היוון 10%.", "10").facet["status"] == "not_read"


def test_what_the_model_wrote_never_feeds_the_facet():
    text = "שיעור היוון 10%."
    given = _given(subject="תזרים דמי שכירות ממשרדים")
    src = _source(text)
    taken = T._take_quote(src, text, {"quote": text, "number": "10"}, [(0, text, 1)])
    taken["preceding"] = []
    value = T._new_value(Workspace(ctx=None), src, "S1", taken, T._settle(taken, given, ()), given,
                         "שיעור היוון לתזרים דמי שכירות ממשרדים", (), "r1")
    assert value.facet == NONE and "משרדים" in value.label and "משרדים" in value.subject


@pytest.mark.parametrize("text, number, kind, unit", [
    ("שיעור היוון של 6.5% לשנה.", "6.5", "rate", "percent"),
    ("דמי שכירות של 71 ₪ למ״ר.", "71", "rent_per_area", "ILS_per_sqm"),
    ("שווי הקרקע 4,200 ₪ למ\"ר.", "4,200", "value_per_area", "ILS_per_sqm"),
    ("שיעור היוון של 6% למועד הקובע.", "6", "rate", "percent"),
    ("שיעור היוון של 6% לפי המקור.", "6", "rate", "percent"),
    ("שיעור היוון של 6% לחודש.", "6", "rate", "percent"),
    ("שיעור ההיוון לצורך חישוב השווי הוא 6%.", "6", "rate", "percent"),
])
def test_a_period_unit_date_or_basis_phrase_is_no_facet(text, number, kind, unit):
    assert _quote(text, number, preceding=[], kind=kind, unit=unit).facet == NONE


def test_a_family_that_needs_no_facet_reads_no_context():
    # a value is not a coefficient or a proportion: an empty facet is the unit's headline datum, no block is read
    v = _quote("שווי הנכס 1,540,000 ₪.", "1,540,000", preceding=[("heading", "תזרים הכנסות מתחנת טעינה")],
               kind="value", unit="ILS")
    assert v.facet == NONE


# --- normalization (KTD3): the cores U4 compares --------------------------------------------------------------------

def _written(case: str, name: str, family: str = "proportion") -> frozenset:
    doc = MANIFEST["documents"][case]
    entry = next(d for d in doc["data"] if d["id"] == f"{case}.{name}")
    return datum.core(entry["facet"]["written"], family)


@pytest.mark.parametrize("name", ["first", "second"])
def test_the_two_written_forms_of_one_cash_flow_have_equal_non_empty_cores(name):
    one, two = _written("forms_1", name), _written("forms_2", name)
    assert one and one == two


def test_two_cash_flows_of_one_table_do_not_share_a_core():
    assert _written("forms_1", "first") != _written("forms_1", "second")
    assert not _written("forms_1", "first") & _written("forms_1", "second")


def test_the_land_coefficients_have_disjoint_non_empty_cores():
    building = datum.core(STATED_ROW, "coefficient")
    sharing = datum.core("מקדם הפחתה בשל שיתוף במקרקעין", "coefficient")
    assert building and sharing and not building & sharing


def test_the_cash_flow_cores_of_the_capitalization_section_keep_a_content_word_each():
    cores = {n: datum.core(next(d for d in MANIFEST["documents"]["cap_rates"]["data"]
                                if d["id"] == f"cap.{n}")["facet"]["written"], "proportion")
             for n in ("offices", "parking", "storage", "residual", "billboard")}
    for a in cores:
        assert cores[a]
        for b in cores:
            if a != b:
                assert cores[a] - cores[b], (a, b)  # each keeps a word the other lacks


def test_normalization_unifies_quote_marks_prefixes_plurals_and_drops_the_family_words():
    assert datum.core("תב״ע", "") == datum.core('התב"ע', "") == datum.core("בתב\"ע", "")
    assert datum.core("שיעור ההיוון לתזרים משרדים", "proportion") == datum.core("תזרים ממשרדים", "proportion")
    assert datum.core("שיעורי היוון", "proportion") == frozenset()
    assert datum.core("מקדם הפחתה", "coefficient") == frozenset()
    assert datum.core("מגרש 12", "coefficient") == datum.core("המגרש 12", "coefficient")
    assert "12" in datum.core("מגרש 12", "")  # digits are kept


def test_the_families_that_need_a_facet_are_the_coefficient_and_the_proportion():
    assert datum.FACET_FAMILIES == frozenset({"coefficient", "proportion"})
    assert datum.family_of("rate") == datum.family_of("ratio") == "proportion"
    assert datum.family_of("coefficient") == "coefficient"
    assert datum.family_of("value_per_area") == "value" and datum.family_of("value") == "value"


# --- the manifest's facets, read from the fixtures' own text --------------------------------------------------------

def _structure(doc: dict, caption: str) -> dict:
    table = next(t for t in doc["tables"] if t["caption"] == caption)
    return _table(table["headers"], table["rows"], table["caption"])


def _blocks_of(doc: dict) -> list:
    """The fixture's blocks as the reader stores them (kind, text, section path), by the extraction itself."""
    data = (FIXTURES / doc["file"]).read_bytes()
    result = extract_docx(data, time.monotonic() + 60, get_settings())
    return [SimpleNamespace(block_index=i, kind=b.kind, text=b.text or "", section_path=tuple(b.section_path or ()))
            for i, b in enumerate(result.blocks)]


def _manifest_cases():
    for key, doc in MANIFEST["documents"].items():
        for d in doc.get("data", []):
            yield pytest.param(key, d, id=d["id"])


def _read_manifest_facet(key: str, d: dict) -> dict:
    doc = MANIFEST["documents"][key]
    where = d["where"]
    if "table" in where:
        t = where["table"]
        st = _structure(doc, t["caption"])
        full = T._table_text(st, st["rows"])
        src = _source(full, "table")
        taken = T._cell_of(src, full, {"row": t["row_label"], "column": t["column_header"]}, st, 0)
    else:
        rows = _blocks_of(doc)
        at = next(r.block_index for r in rows if r.text == where["quote"])
        full = "\n".join(r.text for r in rows)
        src = _source(full)
        number = T._numbers_of(where["quote"])[0][0]
        taken = T._take_quote(src, full, {"quote": where["quote"], "number": number},
                              [(r.block_index, r.text, 1) for r in rows])
        taken["preceding"] = datum.preceding(rows, at)
    return T._value_facet(Workspace(ctx=None), src, taken, d["kind"])


@pytest.mark.parametrize("key, d", _manifest_cases())
def test_every_manifest_datum_reads_its_expected_facet_from_the_fixture(key, d):
    expected = {k: d["facet"].get(k) for k in ("status", "written", "part", "tier")}
    assert _read_manifest_facet(key, d) == expected


def test_the_capitalization_section_reads_the_statuses_the_manifest_states():
    ae2 = next(c for c in MANIFEST["cases"] if c["id"] == "AE2")["expected"]["facets"]
    data = {d["id"]: d for d in MANIFEST["documents"]["cap_rates"]["data"]}
    assert {i: _read_manifest_facet("cap_rates", data[i])["status"] for i in ae2} == ae2


# --- stored measurements (M#) ---------------------------------------------------------------------------------------

def _row(**kw):
    base = dict(id=uuid.uuid4(), document_id=DOC, version_id=VER, title="שומה", metric="שיעור היוון", metric_kind="rate",
                value_text="6.5%", unit="percent", period="none", vat="unknown", area_basis=None, subject=None,
                value_role="other", status="auto_validated", quote="", section=None, block_index=3, table_index=None,
                row_index=None, header=None, caption=None, issues=[])
    return SimpleNamespace(**(base | kw))


def test_a_stored_table_measurement_gets_the_row_label_of_its_table_row():
    st = _table(["רכיב", "ערך"], [["שטח קרקע", "620"], [STATED_ROW, "0.8"]], "טבלת חישוב שווי הקרקע")
    row = _row(table_index=0, row_index=1, metric_kind="coefficient", value_text="0.8", header="ערך")
    assert T._measurement_facet(row, st) == _facet(STATED_ROW, "row")


def test_a_stored_table_measurement_whose_table_is_not_stored_is_not_read():
    assert T._measurement_facet(_row(table_index=2, row_index=0), None)["status"] == "not_read"


def test_a_text_measurement_takes_its_facet_from_its_quote_and_none_when_the_quote_names_nothing():
    named = _row(quote="שיעור היוון של 6.5% לתזרים דמי שכירות ממשרדים.", value_text="6.5%")
    assert T._measurement_facet(named, None) == _facet("תזרים דמי שכירות ממשרדים", "sentence")
    bare = _row(quote="שיעור היוון 10%.", value_text="10%")
    assert T._measurement_facet(bare, None) == NONE


def test_a_listed_measurement_exposes_its_facet_in_public():
    ws = Workspace(ctx=None)
    m = ws.add_measurement(_row(quote="שיעור היוון של 6.5% לתזרים דמי שכירות ממשרדים.", value_text="6.5%"))
    assert m.public()["facet"] == _facet("תזרים דמי שכירות ממשרדים", "sentence")
    table_m = ws.add_measurement(_row(table_index=0, row_index=0))  # no structure at hand: not read, never guessed
    assert table_m.public()["facet"]["status"] == "not_read"


# --- the model sees the facet, and the cache keeps it (KTD19) ----------------------------------------------------------

def test_the_value_report_shows_the_facet_to_the_model():
    table = _table(["רכיב", "ערך"], [[STATED_ROW, "0.8"]], "טבלת חישוב שווי הקרקע")
    v = _cell(table, STATED_ROW, "ערך")
    ws = Workspace(ctx=None)
    ws.values["V1"] = v
    ws.sources["S1"] = _source("x")
    report = T._value_report(ws, v, None, None)
    assert "רכיב" in report and STATED_ROW in report
    none_report = T._value_report(ws, _quote("שיעור היוון 10%.", "10", preceding=[]), None, None)
    assert "רכיב: לא צוין במקור" in none_report


def test_value_public_includes_the_facet_and_a_value_without_one_is_not_read():
    v = _quote("שיעור היוון של 6.5% לתזרים דמי שכירות ממשרדים.", "6.5")
    assert v.public()["facet"] == _facet("תזרים דמי שכירות ממשרדים", "sentence")
    bare = calc.Value("V9", Decimal("1"), "1", "S1", DOC, VER, "r1", "t", "l", "x", "rate", "percent", "none", "unknown",
                      "", "", "other", {}, {"quote": "q"}, "q")
    assert bare.public()["facet"]["status"] == "not_read"


def test_the_cache_version_is_v5_and_the_record_round_trips_the_facet(monkeypatch):
    assert T.VALUE_VERIFIER_VERSION == "value-v5"
    assert "facet" in T.RECORD_FIELDS
    text = "שיעור היוון של 6.5% לתזרים דמי שכירות ממשרדים."
    src = _source(text)
    taken = T._take_quote(src, text, {"quote": text, "number": "6.5"}, [(0, text, 1)])
    taken["preceding"] = []
    earlier = _register(taken, _given(), src)
    prov = {k: "source" for k in ("unit",)}
    earlier.provenance = prov | earlier.provenance
    doc = json.loads(json.dumps(T._cache_doc(earlier, taken, (), None, src, (0, 0)), ensure_ascii=False))
    assert doc["verifier_version"] == "value-v5"
    assert doc["record"]["facet"] == _facet("תזרים דמי שכירות ממשרדים", "sentence")

    # a later turn takes the cached value (Q#): its facet comes from the record, with no block read
    monkeypatch.setattr(T, "tenant_tx", lambda ctx: contextlib.nullcontext())
    monkeypatch.setattr(T, "_bound", lambda conn, ws, handle, data: SimpleNamespace(
        version_id=VER, document_id=DOC, reading_id="r1", title="שומה סינתטית", partial=False))
    monkeypatch.setattr(T.contexts, "of_version", lambda conn, vid, reading: T.contexts.derive([], str(vid)))
    monkeypatch.setattr(T, "_cached_value", lambda conn, version_id, reading_id, locator: doc)
    ws = Workspace(ctx=None)
    handle = T._cached_handle(ws, VER, "r1", DOC, doc["record"]["locator"])
    out = T.tool_take_value(ws, handle, {}, _given(), "שיעור")
    assert out.startswith("V1 נרשם")
    assert ws.values["V1"].facet == _facet("תזרים דמי שכירות ממשרדים", "sentence")
