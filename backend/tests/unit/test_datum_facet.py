"""Round 13 U3 (R1, R2; KTD1, KTD2, KTD3 reading and normalization, KTD19): every registered value (V#) and listed
measurement (M#) carries a facet, the component its source states, read from the source's own words only.

A table cell takes its row label (a column header when the row label states nothing, the caption as context), a quote
value takes the purpose phrase of its own sentence (a list lead-in or a heading within three blocks as context), and
what the model wrote (`label`, `subject`) never feeds it. The expected facets are the round-13 manifest's, read from
the synthetic fixtures; database-free (the preceding blocks come from the fixture's own reading)."""

from __future__ import annotations

import contextlib
import json
import re
import time
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.chat import calc, datum, timeline
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


NONE = {"status": "none", "written": None, "part": None, "tier": None, "reason": "not_in_range"}  # round 14 U2: a state carries its reason


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
    found = _quote(text, number, preceding=[], kind=kind, unit=unit).facet
    assert found["status"] == "none" and found["written"] is None  # read, and nothing in it names a component


def test_a_family_that_needs_no_facet_reads_no_context():
    # a value is not a coefficient or a proportion: an empty facet is the unit's headline datum, no block is read
    v = _quote("שווי הנכס 1,540,000 ₪.", "1,540,000", preceding=[("heading", "תזרים הכנסות מתחנת טעינה")],
               kind="value", unit="ILS")
    assert v.facet == datum.none(datum.REASON_NOT_NEEDED)


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


def test_the_datum_family_of_a_kind_is_the_one_resolve_has():
    from app.chat import resolve

    kinds = {*resolve.REQUEST_KINDS, *resolve._FAMILY, "", "unknown", "other"}
    kinds |= {f"{k}_per_area" for k in kinds}
    for kind in kinds:
        assert datum.family_of(kind) == resolve.family(kind), kind
    assert datum.family_of(None) == ""


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
        taken["preceding"] = datum.reach(rows, at, where["quote"])
    return T._value_facet(Workspace(ctx=None), src, taken, d["kind"])


@pytest.mark.parametrize("key, d", _manifest_cases())
def test_every_manifest_datum_reads_its_expected_facet_from_the_fixture(key, d):
    expected = {k: d["facet"].get(k) for k in ("status", "written", "part", "tier")}
    found = _read_manifest_facet(key, d)  # a context facet also says where it was read (block, sentence, at): round 14 U2
    assert {k: found.get(k) for k in expected} == expected


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


def test_a_text_measurement_takes_its_facet_from_its_quote_and_is_not_read_when_the_quote_names_nothing_and_no_blocks_are_at_hand():
    named = _row(quote="שיעור היוון של 6.5% לתזרים דמי שכירות ממשרדים.", value_text="6.5%")
    assert T._measurement_facet(named, None) == _facet("תזרים דמי שכירות ממשרדים", "sentence")
    # round 14 U3 (R4): "none" would claim a read of the text above that nobody made; the batched block read gives that
    bare = _row(quote="שיעור היוון 10%.", value_text="10%")
    assert T._measurement_facet(bare, None)["status"] == "not_read"


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


# =======================================================================================================================
# round 14 U1: a source span on every datum, and one registration for a repeated span (R6, R7, R15; KTD1, KTD2, KTD11)
# =======================================================================================================================

RATE_TEXT = "שיעור היוון של 6.5% לתזרים דמי שכירות ממשרדים."
AT = RATE_TEXT.index("6.5")  # the number's offset in its block
BARE_TEXT = "הערך 0.8 נקבע בנספח."  # states no unit and no kind: whatever the model says of them is its own


def _cached_world(monkeypatch, text: str = RATE_TEXT, number: str = "6.5", **given):
    """One cached value (Q#) of a visible current version, as ``test_cache_keys`` fakes it; its anchor stub is stored, so a
    registration has an anchor step. Returns the workspace and the Q#."""
    src = _source(text)
    taken = T._take_quote(src, text, {"quote": text, "number": number}, [(0, text, 1)])
    taken["preceding"] = []
    earlier = _register(taken, _given(**given), src)
    stub = {"kind": "quote", "document_id": str(DOC), "version_id": str(VER), "reading_id": "r1", "block_start": 0,
            "block_end": 0, "pages": [1], "section": "3. היוון", **taken["anchor"]}
    doc = json.loads(json.dumps(T._cache_doc(earlier, taken, (), stub, src, (0, 0)), ensure_ascii=False))
    monkeypatch.setattr(T, "tenant_tx", lambda ctx: contextlib.nullcontext())
    monkeypatch.setattr(T, "_bound", lambda conn, ws, handle, data: SimpleNamespace(
        version_id=VER, document_id=DOC, reading_id="r1", title="שומה סינתטית", partial=False))
    monkeypatch.setattr(T.contexts, "of_version", lambda conn, vid, reading: T.contexts.derive([], str(vid)))
    monkeypatch.setattr(T, "_cached_value", lambda conn, version_id, reading_id, locator: doc)
    ws = Workspace(ctx=None)
    return ws, T._cached_handle(ws, VER, "r1", DOC, doc["record"]["locator"])


# --- the span of a value and of a measurement ------------------------------------------------------------------------

def _taken_quote(text: str, quote: str, number: str, block: int = 0) -> dict:
    return T._take_quote(_source(text), text, {"quote": quote, "number": number}, [(block, text, 1)])


def test_two_numbers_of_one_quote_are_two_spans():
    text = "הובאה הפחתה של 10% בגין סחירות והפחתה של 20% בגין עוצמת ניצול."
    ten, twenty = (T.value_span(VER, "r1", _taken_quote(text, text, n)) for n in ("10", "20"))
    assert ten is not None and twenty is not None and ten != twenty and ten[:4] == twenty[:4]  # one block, two offsets


def test_the_same_number_twice_in_a_paragraph_is_two_spans():
    text = "הובאה הפחתה של 10% בגין סחירות. הובאה הפחתה של 10% בגין עוצמת ניצול."
    first, second = (T.value_span(VER, "r1", _taken_quote(text, q, "10")) for q in (
        "הובאה הפחתה של 10% בגין סחירות.", "הובאה הפחתה של 10% בגין עוצמת ניצול."))
    assert first is not None and second is not None and first != second


def test_the_same_quote_in_two_versions_or_two_readings_is_two_spans():
    taken = _taken_quote(RATE_TEXT, RATE_TEXT, "6.5")
    here = T.value_span(VER, "r1", taken)
    assert here == T.value_span(VER, "r1", _taken_quote(RATE_TEXT, RATE_TEXT, "6.5")) and here is not None
    assert T.value_span(uuid.UUID(int=99), "r1", taken) != here and T.value_span(VER, "r2", taken) != here


def test_a_quote_located_to_block_precision_only_has_no_span():
    text = "שיעור היוון של 6.5% לתזרים דמי שכירות ממשרדים."
    twice = [(0, text, 1), (1, text, 1)]  # the quote is in two blocks of the cited range, the same number in both
    taken = T._take_quote(_source(text), text, {"quote": text, "number": "6.5"}, twice)
    assert "number" not in taken["anchor"] and T.value_span(VER, "r1", taken) is None
    assert T.value_span(VER, "r1", T._take_quote(_source(text), text, {"quote": text, "number": "6.5"})) is None  # no blocks


def test_a_cell_is_its_table_row_and_column():
    table = _table(["רכיב", "ערך", "הערה"], [["שטח קרקע", "620", "620"], [STATED_ROW, "0.8", "0.9"]], "טבלת חישוב שווי הקרקע")
    full = T._table_text(table, table["rows"])
    src = _source(full, "table")
    spans = {col: T.value_span(VER, "r1", T._cell_of(src, full, {"row": STATED_ROW, "column": col}, table, 0))
             for col in ("ערך", "הערה")}
    assert spans["ערך"] == ("cell", str(VER), "r1", 0, 2, 2) and spans["הערה"] == ("cell", str(VER), "r1", 0, 2, 3)


def _measurement_row(**kw) -> SimpleNamespace:
    base = dict(version_id=VER, reading_id="r1", value=Decimal("0.8"), quote=RATE_TEXT, block_index=0, table_index=None,
                row_index=None, row_cells=None)
    return SimpleNamespace(**(base | kw))


def test_a_measurement_of_a_text_has_the_span_of_the_value_taken_from_the_same_place():
    value_span = T.value_span(VER, "r1", _taken_quote(RATE_TEXT, RATE_TEXT, "6.5"))
    row = _measurement_row(value=Decimal("6.5"))
    assert T.measurement_span(row, RATE_TEXT) == value_span
    assert T.measurement_span(row) is None  # its block was not read: no span
    assert T.measurement_span(_measurement_row(quote="ציטוט שאינו בבלוק", value=Decimal("6.5")), RATE_TEXT) is None


def test_a_measurement_of_a_table_row_has_the_span_of_its_one_cell_holding_the_value():
    row = _measurement_row(table_index=2, row_index=1, value=Decimal("0.8"), row_cells=[STATED_ROW, "0.8", "0.9"])
    assert T.measurement_span(row) == ("cell", str(VER), "r1", 2, 2, 2)
    # the row holds the value twice: the column is not named, so the measurement has no span (KTD1)
    assert T.measurement_span(_measurement_row(table_index=2, row_index=1, row_cells=["x", "0.8", "0.8"])) is None
    assert T.measurement_span(_measurement_row(table_index=2, row_index=1, row_cells=None)) is None


def test_add_measurement_records_the_span_of_a_listed_table_row_in_the_workspace():
    ws = Workspace(ctx=None)
    row = _measurement_row(id=uuid.uuid4(), document_id=DOC, title="שומה סינתטית", metric="מקדם", metric_kind="coefficient",
                           value_text="0.8", unit="ratio", period="unknown", vat="unknown", area_basis="", subject="הנכס",
                           value_role="other", status="auto_validated", section="3", table_index=2, row_index=1,
                           row_cells=[STATED_ROW, "0.8", "0.9"], table_stored=True, caption="טבלה", title_lines=[],
                           header=None, scenario="", stance="unknown")
    m = ws.add_measurement(row)
    assert ws.spans[m.mid] == ("cell", str(VER), "r1", 2, 2, 2)


# --- a repeated span returns the first value (AE4; KTD2) ---------------------------------------------------------------

def test_ae4_three_registrations_of_one_quote_and_number_are_one_value_and_the_repeats_say_so(monkeypatch):
    ws, handle = _cached_world(monkeypatch)
    first = T.tool_take_value(ws, handle, {}, _given(), "שיעור ראשון")
    second = T.tool_take_value(ws, handle, {}, _given(subject="אחר"), "תווית אחרת")
    third = T.tool_take_value(ws, handle, {}, _given(), "עוד תווית")
    assert list(ws.values) == ["V1"] and list(ws.sources) == ["S1"] and list(ws.spans) == ["V1"]
    assert T.MSG_ALREADY_REGISTERED not in first
    for out in (second, third):
        assert out.startswith("V1") and T.MSG_ALREADY_REGISTERED in out
    assert ws.values["V1"].label == "שיעור ראשון" and ws.values["V1"].subject == "הנכס"  # the first value's label and subject stay


def test_a_registration_at_another_place_is_a_new_value(monkeypatch):
    ws, handle = _cached_world(monkeypatch)
    T.tool_take_value(ws, handle, {}, _given(), "א")
    ws.spans["V1"] = ("quote", str(VER), "r1", 0, 999)  # the registered value stands at another place
    T.tool_take_value(ws, handle, {}, _given(), "ב")
    assert list(ws.values) == ["V1", "V2"] and ws.spans["V2"] != ws.spans["V1"]


def test_a_repeat_whose_meaning_contradicts_a_model_asserted_field_corrects_it(monkeypatch):
    ws, handle = _cached_world(monkeypatch, BARE_TEXT, "0.8", kind="coefficient", unit="ratio")
    T.tool_take_value(ws, handle, {}, _given(kind="coefficient", unit="ratio"), "מקדם")
    v = ws.values["V1"]
    assert v.provenance["unit"] == "model_asserted" and v.unit == "ratio"
    out = T.tool_take_value(ws, handle, {}, _given(kind="coefficient", unit="percent", subject="אחר"), "מקדם שוב")
    assert list(ws.values) == ["V1"] and v.unit == "percent" and v.subject == "הנכס" and v.label == "מקדם"
    assert T.MSG_ALREADY_REGISTERED in out and v.kind == "coefficient"
    # the key fields follow the corrected unit: a fraction and a percent of a coefficient are one unit key (KTD5)
    (d,) = timeline.datums(ws)
    assert d.unit == "percent"


def test_a_repeat_whose_meaning_contradicts_a_source_attested_field_fails_naming_the_first_value(monkeypatch):
    ws, handle = _cached_world(monkeypatch, kind="rate", unit="percent")
    T.tool_take_value(ws, handle, {}, _given(kind="rate", unit="percent"), "שיעור")
    assert ws.values["V1"].provenance["kind"] == "source"
    with pytest.raises(T.ToolError, match="V1"):
        T.tool_take_value(ws, handle, {}, _given(kind="coefficient", unit="percent"), "מקדם")
    assert list(ws.values) == ["V1"] and list(ws.sources) == ["S1"] and ws.values["V1"].kind == "rate"


def test_taking_the_value_again_as_the_source_has_it_withdraws_the_models_unsupported_stance_and_scenario(monkeypatch):
    """The tool tells the model to take a value again with stance unknown and no scenario when the source says nothing of them
    (``MSG_ATTRIBUTION_UNSTATED``, ``MSG_SCENARIO_UNSTATED``): that is the same span, so it is the same V#, now as the source has it."""
    ws, handle = _cached_world(monkeypatch)
    first = T.tool_take_value(ws, handle, {}, _given(stance="estimate", stated_by="השמאי", scenario="תרחיש שהמודל קבע"), "שיעור")
    v = ws.values["V1"]
    assert v.provenance["stance"] == "model_asserted" and v.scenario and T.MSG_SCENARIO_UNSTATED in first
    again = T.tool_take_value(ws, handle, {}, _given(), "שיעור")
    assert list(ws.values) == ["V1"] and v.stance == "unknown" and v.stated_by == "" and v.scenario == ""
    assert v.provenance["stance"] == "not_stated" and v.provenance["scenario"] == "not_stated"
    assert T.MSG_SCENARIO_UNSTATED not in again and T.MSG_ATTRIBUTION_UNSTATED not in again


def test_a_stance_or_scenario_the_source_attests_is_never_replaced_by_a_repeat():
    ws = Workspace(ctx=None)
    first = _existing(ws, NONE)
    first.provenance["scenario"], first.scenario = "source", "מצב תכנוני קודם"
    first.provenance["stance"], first.stance = "source", "claim"
    assert _again(ws, NONE, stance="adopted", scenario="") is not None
    assert (first.scenario, first.stance) == ("מצב תכנוני קודם", "claim")


def _existing(ws: Workspace, facet: dict, span=("quote", str(VER), "r1", 0, AT)) -> calc.Value:
    value = _quote(RATE_TEXT, "6.5", preceding=[])
    value.facet = dict(facet)
    value.vid = "V1"
    T._register_value(ws, value, span)
    return value


def _again(ws: Workspace, facet: dict, span=("quote", str(VER), "r1", 0, AT), **given):
    src = _source(RATE_TEXT)
    taken = T._take_quote(src, RATE_TEXT, {"quote": RATE_TEXT, "number": "6.5"}, [(0, RATE_TEXT, 1)])
    return T._registered_again(ws, span, T._settle(taken, _given(**given), ()), lambda kind: dict(facet))


def test_a_wider_quote_that_reads_a_stated_facet_upgrades_the_first_values_facet():
    stated = _facet("תזרים דמי שכירות ממשרדים", "paragraph", "context")
    for weak in (NONE, datum.not_read()):
        ws = Workspace(ctx=None)
        first = _existing(ws, weak)
        found = _again(ws, stated)
        assert found is not None and found[0] is first and first.facet == stated


def test_a_weaker_read_never_downgrades_a_facet_and_a_stated_one_keeps_the_first():
    stated = _facet("תזרים דמי שכירות ממשרדים", "sentence")
    other = _facet("תזרים אחר", "paragraph", "context")
    for weak in (NONE, datum.not_read()):
        ws = Workspace(ctx=None)
        first = _existing(ws, stated)
        _again(ws, weak)
        assert first.facet == stated
    ws = Workspace(ctx=None)
    first = _existing(ws, NONE)
    _again(ws, datum.not_read())
    assert first.facet == NONE  # not read does not replace "read, nothing stated"
    ws = Workspace(ctx=None)
    first = _existing(ws, stated)
    _again(ws, other)
    assert first.facet == stated


def test_a_span_nobody_registered_is_no_repeat():
    ws = Workspace(ctx=None)
    _existing(ws, NONE)
    assert _again(ws, NONE, span=("quote", str(VER), "r1", 0, 99)) is None and _again(ws, NONE, span=None) is None


# --- a registration changes the workspace in its last step only (KTD2) -----------------------------------------------------

@pytest.mark.parametrize("failing", ["_stamp_date", "_back_date_words"])
def test_a_registration_that_fails_after_the_anchor_step_leaves_no_value_and_no_source(monkeypatch, failing):
    ws, handle = _cached_world(monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    real = getattr(T, failing)
    monkeypatch.setattr(T, failing, boom)
    with pytest.raises(RuntimeError):
        T.tool_take_value(ws, handle, {}, _given(), "שיעור")
    assert not ws.values and not ws.sources and not ws.anchors and not ws.spans
    assert not ws.settled_values and not ws.uncertain_values
    monkeypatch.setattr(T, failing, real)
    out = T.tool_take_value(ws, handle, {}, _given(), "שיעור")
    assert out.startswith("V1") and list(ws.sources) == ["S1"] and ws.anchors["V1"]["kind"] == "quote"
    assert ws.spans["V1"] == ("quote", str(VER), "r1", 0, AT)  # the number's offset in its block


# --- a failed registration is audited (R15, KTD11) ------------------------------------------------------------------------

def _call(ws: Workspace, name: str, **arguments) -> str:
    return T.run_tool(ws, name, json.dumps(arguments, ensure_ascii=False))


def test_a_failed_take_value_is_logged_with_its_reason_and_a_clipped_locator_and_a_successful_one_is_not(monkeypatch):
    ws, handle = _cached_world(monkeypatch)
    meaning = _given()
    locator = {"table": None, "row": None, "row_number": None, "column": None, "column_number": None,
               "quote": "ציטוט ארוך מאוד " * 60, "number": "6.5"}
    out = _call(ws, "take_value", source="S9", locator=locator, meaning=meaning, label="x")
    assert out.startswith("שגיאה")
    (entry,) = ws.registration_log
    assert entry["tool"] == "take_value" and entry["source"] == "S9" and entry["key"] and entry["at"]
    assert 0 < len(entry["locator"]) < len(json.dumps(locator, ensure_ascii=False))
    assert entry["message"] and len(entry["message"]) <= 300
    empty = dict.fromkeys(locator)
    ok = _call(ws, "take_value", source=handle, locator=empty, meaning=meaning, label="x")
    assert ok.startswith("V1") and len(ws.registration_log) == 1
    _call(ws, "assume", value="zzz", quote="q")  # another tool's refusal is no registration
    assert len(ws.registration_log) == 1


def test_the_registration_log_is_bounded():
    ws = Workspace(ctx=None)
    for n in range(T.REGISTRATION_LOG_MAX + 5):
        _call(ws, "take_value", source=f"S{n}", locator={"quote": "x", "number": "1"}, meaning=_given(), label="x")
    assert len(ws.registration_log) == T.REGISTRATION_LOG_MAX


# =======================================================================================================================
# round 14 U2: the context reader — evidence, bounds and where it was found (R1-R4, R12; AE1-AE3, AE8; KTD3, KTD4)
# =======================================================================================================================

MANIFEST14 = json.loads((FIXTURES / "round14" / "manifest.json").read_text(encoding="utf-8"))
DOC14 = MANIFEST14["documents"]["context_reader"]
TAKEN = "בהתאם לאמור לעיל נלקח מקדם הפחתה נוסף בשיעור 10%."  # the value's own sentence: it states no component
CONSIDERATION = "לא ניתנה הפחתה בגין עוצמת ניצול."
SHOWN_KEYS = ("status", "written", "part", "tier", "reason")


def _rows(*blocks, section="3. מגרש 12") -> list:
    """Blocks as the reader stores them: ``(kind, text)`` or ``(kind, text, section)``, numbered from 0."""
    return [SimpleNamespace(block_index=i, kind=b[0], text=b[1], section_path=(b[2] if len(b) > 2 else section,))
            for i, b in enumerate(blocks)]


def _read_at(rows, at: int, quote: str = TAKEN, number: str = "10", *, kind: str = "coefficient", **bounds) -> dict:
    """The facet of the value in block ``at`` of ``rows`` (its own sentence ``quote``), read as ``tools`` reads it: the candidates
    above it, then ``datum.read_quote``."""
    holder = rows[at]
    src = _source(holder.text)
    taken = T._take_quote(src, holder.text, {"quote": quote, "number": number}, [(at, holder.text, 1)])
    taken["preceding"] = datum.reach(rows, at, quote, **bounds)
    return T._value_facet(Workspace(ctx=None), src, taken, kind)


def _manifest14(datum_id: str) -> tuple[dict, list, int]:
    d = next(x for x in DOC14["data"] if x["id"] == datum_id)
    rows = _blocks_of(DOC14)
    section = d["where"]["section"]
    at = next(r.block_index for r in rows if r.kind == "paragraph" and r.section_path[:1] == (section,)
              and d["where"]["quote"] in r.text)
    return d, rows, at


def _manifest14_cases():
    for d in DOC14["data"]:
        yield pytest.param(d["id"], id=d["id"])


@pytest.mark.parametrize("datum_id", _manifest14_cases())
def test_every_round14_datum_reads_the_facet_the_manifest_states(datum_id):
    d, rows, at = _manifest14(datum_id)
    found = _read_at(rows, at, d["where"]["quote"], re.sub(r"[^\d.,]", "", d["shown"]))
    expected = {k: d["facet"].get(k) for k in SHOWN_KEYS}
    assert {k: found.get(k) for k in SHOWN_KEYS} == expected


def test_ae1_the_consideration_two_sentences_back_is_the_component_and_its_place_is_kept():
    d, rows, at = _manifest14("r14.same_block")
    f = _read_at(rows, at, d["where"]["quote"], "10")
    assert (f["status"], f["written"], f["part"], f["tier"]) == ("stated", "עוצמת ניצול", "preceding_sentence", "context")
    assert f["block"] == at and f["sentence"] == 0  # the context sentence is the first of the value's own block
    start, end = f["at"]["segments"][0][1:]
    assert rows[at].text[start:end] == "השמאי לא נתן הפחתה בגין עוצמת ניצול."  # `at` is the stub of the context sentence


def test_a_facet_of_the_values_own_sentence_has_no_context_place():
    f = _read_at(*_manifest14_args("r14.own_phrase"))
    assert f["part"] == "sentence" and f["tier"] == "own" and not {"block", "sentence", "at"} & f.keys()


def _manifest14_args(datum_id: str):
    d, rows, at = _manifest14(datum_id)
    return rows, at, d["where"]["quote"], "10"


def test_the_consideration_in_the_previous_paragraph_block_is_a_paragraph_facet_with_its_block():
    rows, at, quote, number = _manifest14_args("r14.prev_paragraph")
    f = _read_at(rows, at, quote, number)
    assert (f["written"], f["part"]) == ("עוצמת ניצול", "paragraph") and f["block"] == at - 1 and f["sentence"] == 0
    start, end = f["at"]["segments"][0][1:]
    assert rows[at - 1].text[start:end] == CONSIDERATION


def test_ae2_a_reduction_of_another_kind_between_the_consideration_and_the_value_gives_no_component():
    rows, at, quote, number = _manifest14_args("r14.between")
    f = _read_at(rows, at, quote, number)
    assert (f["status"], f["reason"], f["written"]) == ("unparsed", "intervening_value", None)
    assert datum.core_of(datum.Site("coefficient", f)) == frozenset()  # a state without a component has no core
    assert f["part"] == "preceding_sentence" and f["block"] == at  # the rejected text is the 20% sentence, kept for the reader
    assert "ערך אחר" in datum.line(f) and "בין" in datum.line(f)


def test_a_percent_between_with_no_purpose_phrase_of_its_own_is_enough_to_reject_the_consideration():
    rows = _rows(("paragraph", f"{CONSIDERATION} הופחתו 20% בנוסף. {TAKEN}"))
    assert _read_at(rows, 0)["reason"] == "intervening_value"
    rows = _rows(("paragraph", f"{CONSIDERATION} נלקח מקדם 0.85 בנוסף. {TAKEN}"))  # a coefficient between counts as well
    assert _read_at(rows, 0)["reason"] == "intervening_value"
    rows = _rows(("paragraph", f"{CONSIDERATION} שטח המגרש 310 מ״ר, בשנת 2022. {TAKEN}"))  # an area and a year are no values
    assert _read_at(rows, 0)["status"] == "stated"


def test_a_consideration_with_a_number_of_its_own_is_that_numbers_not_the_values():
    rows = _rows(("paragraph", f"ניתנה הפחתה של 20% בגין סחירות. {TAKEN}"))
    f = _read_at(rows, 0)
    assert (f["status"], f["reason"]) == ("unparsed", "intervening_value")


def test_ae3_the_read_stops_at_a_table_at_another_sections_heading_and_at_the_units_first_block():
    # a table block between
    rows = _rows(("paragraph", CONSIDERATION), ("table", "נתון | ערך"), ("paragraph", TAKEN))
    f = _read_at(rows, 2)
    assert (f["status"], f["reason"]) == ("none", "not_in_range")
    # another section's heading, and its text, between
    rows = _rows(("paragraph", CONSIDERATION, "3. מגרש 12"), ("heading", "4. מגרש 14", "4. מגרש 14"),
                 ("paragraph", TAKEN, "4. מגרש 14"))
    assert (f := _read_at(rows, 2))["status"] == "none" and f["reason"] == "not_in_range"
    # the same section path in the unit before: the unit's first block ends the read
    rows = _rows(("paragraph", CONSIDERATION), ("paragraph", "שנת הערכה."), ("paragraph", TAKEN))
    assert _read_at(rows, 2)["status"] == "stated"
    assert (f := _read_at(rows, 2, lo=1))["status"] == "none" and f["reason"] == "not_in_range"
    assert _read_at(rows, 2, lo=0)["written"] == "עוצמת ניצול"


def test_the_values_own_sentence_and_what_follows_it_are_never_candidates():
    rows = _rows(("paragraph", f"{TAKEN} {CONSIDERATION}"))
    assert datum.reach(rows, 0, TAKEN).candidates == []
    assert _read_at(rows, 0)["status"] == "none"  # a consideration after the value is not the value's
    several = _rows(("paragraph", f"{CONSIDERATION} {TAKEN} אחרי."))
    assert [c.text for c in datum.reach(several, 0, TAKEN).candidates] == [CONSIDERATION]


def test_a_candidate_naming_two_components_is_ambiguous_not_attributed():
    for text in ("ניתנה הפחתה לעוצמת ניצול ולגמישות שימושים.", "ניתנה הפחתה בגין עוצמת ניצול ובגין גמישות שימושים.",
                 "ניתנה הפחתה בגין עוצמת ניצול, ובגין גמישות שימושים.", "ניתנה הפחתה בגין עוצמת ניצול עבור גמישות שימושים."):
        f = _read_at(_rows(("paragraph", text), ("paragraph", TAKEN)), 1)
        assert (f["status"], f["reason"], f["written"]) == ("unparsed", "two_components", None), text
    one = "ניתנה הפחתה בגין עוצמת ניצול, ולכן נלקח מקדם נוסף לבחינה."  # a conjunction that opens no purpose is not a second component
    assert _read_at(_rows(("paragraph", one), ("paragraph", TAKEN)), 1)["status"] == "stated"


def test_the_connective_in_accordance_with_is_no_cue_word_and_opens_no_phrase():
    assert datum.read_sentence(TAKEN, "10", "coefficient") is None  # "בהתאם לאמור…": the stem of "התאמות" is not its meaning
    rows = _rows(("paragraph", "בהתאם לעוצמת הניצול שנבדקה נקבע ערך."), ("paragraph", TAKEN))
    assert _read_at(rows, 1)["status"] == "none"


def test_a_candidate_needs_a_cue_word_and_a_purpose_phrase():
    for text in ("ניתנה התחשבות בעוצמת ניצול.",  # no cue word of a coefficient or a rate
                 "ניתנה הפחתה.",  # a cue word, no purpose phrase
                 "ניתנה הפחתה לשנה.",  # a phrase that is a period
                 "הנכס נבדק בגין עוצמת ניצול."):  # a connector and a phrase, no cue
        assert _read_at(_rows(("paragraph", text), ("paragraph", TAKEN)), 1)["status"] == "none", text


def test_a_list_lead_in_is_the_components_of_its_items_but_a_paragraph_value_between_blocks_it():
    lead = "הפחתה בגין עוצמת ניצול:"
    rows = _rows(("paragraph", lead), ("li", "בשיעור 5%."), ("paragraph", "בשיעור 7%."), ("paragraph", "בשיעור 9%."))
    assert _read_at(rows, 3, "בשיעור 9%.", "9")["reason"] == "intervening_value"  # a value of a paragraph stands between
    rows = _rows(("paragraph", lead), ("li", "בשיעור 5%."), ("li", "בשיעור 7%."))
    f = _read_at(rows, 2, "בשיעור 7%.", "7")  # a sibling item does not: the lead-in is the whole list's
    assert (f["status"], f["part"], f["written"]) == ("stated", "lead_in", "הפחתה בגין עוצמת ניצול") and f["block"] == 0
    # a numbered heading with a number of its own kind does not count the section number as a value
    rows = _rows(("heading", "3.2 הפחתה בגין עוצמת ניצול"), ("paragraph", TAKEN))
    assert _read_at(rows, 1)["part"] == "heading"


def test_a_legacy_pair_of_blocks_above_is_still_read():
    f = datum.read_quote("שיעור היוון 9%.", "9", "proportion", [("paragraph", LEAD_IN)])
    assert (f["part"], f["tier"]) == ("lead_in", "context")


def test_the_candidate_text_is_capped_by_characters_and_a_capped_search_is_not_read_never_none():
    filler = "הנכס נבדק בהתאם לנתוני השוק שנאספו בסביבתו. " * 4
    rows = _rows(("paragraph", CONSIDERATION), *[("paragraph", filler)] * 6, ("paragraph", TAKEN))
    wide = datum.reach(rows, 7, TAKEN, chars=10_000)
    assert not wide.capped and _read_at(rows, 7)["written"] == "עוצמת ניצול"
    cut = datum.reach(rows, 7, TAKEN, chars=len(filler) * 3)
    assert cut.capped and sum(len(c.text) for c in cut.candidates) <= len(filler) * 3
    f = _read_at(rows, 7, chars=len(filler) * 3)
    assert (f["status"], f["reason"]) == ("not_read", "cap")
    assert datum.read_context(cut, "coefficient")["status"] == "not_read"
    # an accepted candidate before the cap settles it; the cap is a key of the existing dictionary, not a read scope
    near = _rows(("paragraph", filler), ("paragraph", CONSIDERATION), ("paragraph", TAKEN))
    assert _read_at(near, 2, chars=len(CONSIDERATION) + 5)["written"] == "עוצמת ניצול"
    assert T.CONTEXT_CHARS["candidates"] > 0 and "candidates" not in T.READ_SCOPES
    with pytest.raises(T.ToolError):
        T.read_scope(Workspace(ctx=None), "S1", "candidates")


def test_rows_cut_by_the_reader_limit_are_a_capped_search():
    rows = _rows(("paragraph", "נבדק."), ("paragraph", TAKEN))
    assert datum.reach(rows[1:], 1, TAKEN, truncated=True).capped and not datum.reach(rows[1:], 1, TAKEN).capped
    assert datum.reach(rows, 1, TAKEN, truncated=True).capped is False  # the range was reached: the cut did not matter


def test_a_header_only_table_context_still_gives_the_caption_facet_and_a_note_comes_after_it():
    table = _table(["מדד", "ערך"], [["הערך שנקבע", "0.85"]], "מקדם הפחתה בשל קרבה לקו מתח עליון")
    table["notes"] = ["הערך שנקבע: הפחתה בגין סחירות."]
    assert _cell(table, "הערך שנקבע", "ערך").facet == _facet("מקדם הפחתה בשל קרבה לקו מתח עליון", "caption", "context")


def test_ae_table_note_that_names_the_row_label_gives_the_consideration():
    table = _table(["מדד", "ערך"], [["הערך שנקבע", "0.85"]], "טבלת מקדמי התאמה לנכס")
    table["notes"] = ["הערה כללית לטבלה.", "הערך שנקבע: מקדם הפחתה בגין עוצמת ניצול."]
    f = _cell(table, "הערך שנקבע", "ערך").facet
    assert (f["status"], f["written"], f["part"], f["tier"], f["sentence"]) == ("stated", "עוצמת ניצול", "table_note",
                                                                                  "context", 1)
    # a note about another row names nothing for this one, and a note naming two considerations is ambiguous
    table["notes"] = ["שטח המגרש: הפחתה בגין עוצמת ניצול."]
    assert _cell(table, "הערך שנקבע", "ערך").facet == NONE
    table["notes"] = ["הערך שנקבע: הפחתה בגין עוצמת ניצול ובגין גמישות שימושים."]
    f = _cell(table, "הערך שנקבע", "ערך").facet
    assert (f["status"], f["reason"], f["part"]) == ("unparsed", "two_components", "table_note")
    # the notes are read last: the row label, the column header and the caption come first
    table = _table(["מדד", "ערך"], [[STATED_ROW, "0.85"]], "")
    table["notes"] = [f"{STATED_ROW}: הפחתה בגין סחירות."]
    assert _cell(table, STATED_ROW, "ערך").facet == _facet(STATED_ROW, "row")


def test_a_cell_of_a_family_that_needs_no_facet_reads_no_notes():
    table = _table(["מדד", "ערך"], [["הערך שנקבע", "620"]], "")
    table["notes"] = ["הערך שנקבע: הפחתה בגין סחירות."]
    assert _cell(table, "הערך שנקבע", "ערך", kind="area", unit="sqm").facet == datum.none(datum.REASON_NOT_NEEDED)


def test_ae8_a_cell_of_a_picture_has_a_component_that_was_not_read_with_the_reason():
    src = _source("x")
    taken = {"locator": {"table_index": 0, "row": "מקדם", "row_number": 1, "column": "ערך", "column_number": 1}}
    f = T._value_facet(Workspace(ctx=None), src, taken, "coefficient")  # a picture cell has no facet_in (_take_vision_cell)
    assert (f["status"], f["reason"]) == ("not_read", "picture_cell") and f["written"] is None
    assert datum.core_of(datum.Site("coefficient", f)) == frozenset()
    assert datum.LABEL_NOT_STATED not in datum.line(f) and "תמונה" in datum.line(f)


def test_no_state_but_stated_has_a_core_and_the_model_is_told_each_state_in_words():
    lines = {datum.line(s) for s in (datum.none(), datum.not_read(), datum.not_read(datum.REASON_CAP),
                                     datum.not_read(datum.REASON_PICTURE_CELL), datum.unparsed(datum.REASON_INTERVENING),
                                     datum.unparsed(datum.REASON_TWO_COMPONENTS))}
    assert len(lines) == 6  # six states, six wordings: none is shown as another
    for f in (datum.none(), datum.not_read(), datum.unparsed(datum.REASON_INTERVENING, "paragraph", block=3, sentence=0)):
        assert datum.core_of(datum.Site("coefficient", f)) == frozenset()


def test_an_ambiguous_read_moves_forward_to_a_stated_one_and_never_back():
    stated = _facet("עוצמת ניצול", "paragraph", "context")
    ambiguous = datum.unparsed(datum.REASON_INTERVENING, "paragraph")
    assert datum.forward(ambiguous, stated) == stated and datum.forward(stated, ambiguous) == stated
    assert datum.forward(datum.not_read(), ambiguous) == ambiguous and datum.forward(ambiguous, datum.not_read()) == ambiguous


# --- relation: a facet read from the sentence above is the value's own words (rule 3) ------------------------------------

def _site(facet: dict, section: str, appraisal=("v", 1)) -> datum.Site:
    return datum.Site("coefficient", facet, appraisal, None, section)


def test_a_context_sentence_and_an_own_phrase_in_two_sections_of_one_unit_are_different_when_their_cores_differ():
    sentence = datum.facet("עוצמת ניצול", "preceding_sentence", "context", block=15, sentence=0)
    own = datum.facet("סחירות", "sentence", "own")
    assert datum.relation(_site(sentence, "3. מגרש 12"), _site(own, "9. מגרש 24")) == datum.DIFFERENT
    paragraph = datum.facet("גמישות שימושים", "paragraph", "context", block=19, sentence=0)
    assert datum.relation(_site(sentence, "3. מגרש 12"), _site(paragraph, "5. מגרש 16")) == datum.DIFFERENT
    # in another unit, or when one of them is a heading or a lead-in, nothing new is decided
    assert datum.relation(_site(sentence, "3. מגרש 12"), _site(own, "9. מגרש 24", ("v", 2))) == datum.UNDECIDED
    heading = datum.facet("עוצמת ניצול", "heading", "context")
    assert datum.relation(_site(heading, "3. מגרש 12"), _site(own, "9. מגרש 24")) == datum.UNDECIDED
    # equal cores are not "same" by this rule: only an own facet pair is (rule 1)
    assert datum.relation(_site(sentence, "3"), _site(datum.facet("עוצמת ניצול", "paragraph", "context"), "5")) == datum.UNDECIDED


def test_the_round14_fixtures_two_plots_are_different_components_not_undecided():
    facets = []
    for datum_id in ("r14.same_block", "r14.own_phrase"):
        rows, at, quote, number = _manifest14_args(datum_id)
        facets.append((_read_at(rows, at, quote, number), rows[at].section_path[0]))
    (a, sa), (b, sb) = facets
    assert sa != sb and datum.relation(_site(a, sa), _site(b, sb)) == datum.DIFFERENT


# =======================================================================================================================
# round 14 U3: every extraction path and the caches (R4, R5, R17; AE7, AE8; KTD4, KTD5)
# =======================================================================================================================

PREV_SENTENCE_ROWS = _rows(("paragraph", "לא ניתנה הפחתה בגין עוצמת ניצול. שיעור הפחתה 9%."))


def _text_read(rows=PREV_SENTENCE_ROWS, at: int = 0, quote: str = "שיעור הפחתה 9%.", **kw) -> T._TextRead:
    return T._TextRead(datum.reach(rows, at, quote), rows[at].text, **kw)


def test_the_facet_reader_version_is_two_the_verifier_version_stays_and_a_new_record_carries_the_reader():
    assert T.FACET_READER_VERSION == 2 and T.VALUE_VERIFIER_VERSION == "value-v5"  # KTD5: a bump would drop _known_clear reuse
    record = T._cache_record(_quote("שיעור היוון של 6.5% לתזרים דמי שכירות ממשרדים.", "6.5"))
    assert record["facet_reader"] == T.FACET_READER_VERSION


def test_the_facet_memo_keeps_only_a_stated_result():
    ws = Workspace(ctx=None)
    for status_of, kept in ((lambda: datum.none(), False), (lambda: datum.not_read(), False),
                            (lambda: datum.unparsed(datum.REASON_INTERVENING), False), (lambda: _facet("x y", "sentence"), True)):
        key = ("k", len(ws.facets))
        found = T._facet_memo(ws, key, status_of)
        assert (key in ws.facets) is kept and found["status"] in ("none", "not_read", "unparsed", "stated")
    # a second value at the place after a wider read gets the stated facet (nothing froze the earlier "none")
    key = ("place",)
    assert T._facet_memo(ws, key, datum.none)["status"] == "none"
    assert T._facet_memo(ws, key, lambda: _facet("עוצמת ניצול", "paragraph", "context"))["status"] == "stated"
    assert T._facet_memo(ws, key, datum.none)["status"] == "stated"  # and it is kept from then on


def test_a_stated_facet_is_shared_by_a_value_and_a_measurement_of_one_span():
    text = "לא ניתנה הפחתה בגין עוצמת ניצול. שיעור הפחתה 9%."
    src = _source(text)
    taken = T._take_quote(src, text, {"quote": "שיעור הפחתה 9%.", "number": "9"}, [(0, text, 1)])
    stated = _facet("עוצמת ניצול", "preceding_sentence", "context")
    ws = Workspace(ctx=None)
    ws.facets[T._quote_facet_key(src, taken, "proportion")] = stated
    row = _row(quote="שיעור הפחתה 9%.", value_text="9%", value=Decimal("9"), reading_id="r1", block_index=0)
    span = T.measurement_span(row, text)
    assert span == T.value_span(VER, "r1", taken)
    assert T._measurement_facet_of(ws, None, row, span=span) == stated  # not the measurement's own read (not_read)
    assert T._measurement_facet_of(Workspace(ctx=None), None, row, span=span)["status"] == "not_read"


# --- a stored measurement from text: its context from the batched block read (KTD5) ------------------------------------------

def test_a_text_measurement_in_a_family_that_needs_a_facet_is_not_read_without_its_blocks_never_none():
    bare = _row(quote="שיעור היוון 10%.", value_text="10%")
    f = T._measurement_facet(bare, None)
    assert (f["status"], f["reason"]) == ("not_read", None) and datum.LABEL_NOT_STATED not in datum.line(f)
    area = _row(metric_kind="area", metric="שטח", unit="sqm", value_text="100 מ\"ר", quote="שטח הנכס 100 מ\"ר.")
    assert T._measurement_facet(area, None) == datum.none(datum.REASON_NOT_NEEDED)  # a family that needs none reads none


def test_a_text_measurement_takes_its_component_from_the_previous_sentence_it_was_read_with():
    row = _row(quote="שיעור הפחתה 9%.", value_text="9%", metric="שיעור הפחתה", metric_kind="rate")
    f = T._measurement_facet(row, None, _text_read())
    assert (f["status"], f["written"], f["part"], f["tier"]) == ("stated", "עוצמת ניצול", "preceding_sentence", "context")
    assert f["block"] == 0 and f["at"]["segments"][0][0] == 0  # where the context is, apart from the number's own place
    own = _row(quote="שיעור היוון של 6.5% לתזרים דמי שכירות ממשרדים.", value_text="6.5%")
    assert T._measurement_facet(own, None, _text_read())["part"] == "sentence"  # an own phrase is read before any context


def test_a_text_measurement_listed_beyond_the_cap_or_in_a_picture_is_not_read_with_the_reason():
    row = _row(quote="שיעור הפחתה 9%.", value_text="9%", metric="שיעור הפחתה", metric_kind="rate")
    assert T._measurement_facet(row, None, T._TextRead(capped=True)) == datum.not_read(datum.REASON_CAP)
    assert T._measurement_facet(row, None, T._TextRead(picture=True)) == datum.not_read(datum.REASON_PICTURE_CELL)
    # the reach itself ran into the character cap before an accepted candidate: not read, never none
    cut = T._TextRead(datum.Reach([], True, {}), "x")
    assert T._measurement_facet(row, None, cut) == datum.not_read(datum.REASON_CAP)


def test_a_table_measurement_reads_the_notes_of_its_table_after_the_caption():
    st = _table(["רכיב", "ערך"], [["שטח קרקע", "620"], [STATED_ROW, "0.8"]], "טבלת חישוב")
    st["notes"] = [f"{STATED_ROW} נקבע בגין זכויות בנייה חלקיות."]
    row = _row(table_index=0, row_index=1, metric_kind="coefficient", value_text="0.8", header="ערך",
               quote=f"{STATED_ROW} | 0.8")
    assert T._measurement_facet(row, st)["status"] == "stated"  # the row label is its own facet, first
    generic = _table(["רכיב", "ערך"], [["הערך שנקבע", "0.8"]], "טבלת חישוב")
    generic["notes"] = ["הערך שנקבע הוא מקדם הפחתה בגין עוצמת ניצול."]
    row = _row(table_index=0, row_index=0, metric_kind="coefficient", value_text="0.8", header="ערך")
    f = T._measurement_facet(row, generic)
    assert (f["status"], f["part"], f["tier"]) == ("stated", "table_note", "context")
    listed = _row(table_index=0, row_index=0, metric_kind="coefficient", value_text="0.8", header="ערך",
                  table_stored=True, row_cells=["הערך שנקבע", "0.8"], caption="טבלת חישוב", title_lines=[])
    assert T._listed_facet(listed)["status"] == "none"
    assert T._listed_facet(listed, generic["notes"]) == f


# --- the cache: a locator that names the number, completed in place (KTD5) -------------------------------------------------

def test_two_numbers_of_one_quote_are_keyed_apart_in_the_cache_and_a_cell_keeps_its_own_locator():
    text = "הובאה הפחתה של 10% בגין סחירות והפחתה של 20% בגין עוצמת ניצול."
    one, two = (_taken_quote(text, text, n) for n in ("10", "20"))
    keys = [T._cache_locator(t["locator"], T.value_span(VER, "r1", t)) for t in (one, two)]
    assert keys[0] != keys[1] and all(k["quote"] == text for k in keys)  # the quote alone is no longer the key
    cell = {"table_index": 0, "row": "x", "row_number": 1, "column": "y", "column_number": 2}
    assert T._cache_locator(cell, ("cell", str(VER), "r1", 0, 1, 2)) == cell
    assert T._cache_locator(one["locator"], None) == one["locator"]  # a quote located to block precision: the old key


def test_a_cached_value_is_found_by_its_span_key_else_by_an_older_quote_only_record_of_a_single_number(monkeypatch):
    stored: dict = {}
    monkeypatch.setattr(T, "_cached_value", lambda conn, v, r, locator: stored.get(json.dumps(locator, sort_keys=True)))
    single = "שיעור היוון של 6.5% לתזרים דמי שכירות ממשרדים."
    two = "הובאה הפחתה של 10% בגין סחירות והפחתה של 20% בגין עוצמת ניצול."
    for text, number in ((single, "6.5"), (two, "10")):
        taken = _taken_quote(text, text, number)
        span = T.value_span(VER, "r1", taken)
        legacy = {"verifier_version": "value-v5", "record": {"value": number}}
        stored[json.dumps(taken["locator"], sort_keys=True)] = legacy  # an older record, keyed by the quote alone
        found = T._cached_value_at(None, VER, "r1", taken["locator"], span)
        assert (found is legacy) is (text == single)  # found only when that quote holds a single number
    new = {"verifier_version": "value-v5", "record": {"value": "10"}}
    taken = _taken_quote(two, two, "10")
    stored[json.dumps(T._cache_locator(taken["locator"], T.value_span(VER, "r1", taken)), sort_keys=True)] = new
    assert T._cached_value_at(None, VER, "r1", taken["locator"], T.value_span(VER, "r1", taken)) is new


def _cached_with(monkeypatch, facet: dict, reader_version: int | None, text: str = "שיעור הפחתה 9%.", number: str = "9"):
    """A cached quote value (Q#) whose record carries ``facet`` and ``facet_reader``; the blocks it is read again from are faked.
    The default sentence states no component of its own, so only the text above it can."""
    ws, handle = _cached_world(monkeypatch, text, number)
    doc = T._cached_value(None, VER, "r1", {})
    doc["record"]["facet"] = facet
    if reader_version is None:
        doc["record"].pop("facet_reader", None)
    else:
        doc["record"]["facet_reader"] = reader_version
    calls = {"blocks": 0, "update": []}

    def blocks_between(conn, version_id, a, b):
        calls["blocks"] += 1
        return [SimpleNamespace(block_index=0, kind="paragraph", text=doc["record"]["quote"], section_path=("3. היוון",),
                                status="read")]

    reach_ = datum.reach(PREV_SENTENCE_ROWS, 0, "שיעור הפחתה 9%.")
    monkeypatch.setattr(T.reader, "blocks_between", blocks_between)

    def preceding(*args, **kwargs):
        calls["blocks"] += 1
        return reach_

    monkeypatch.setattr(T, "_preceding_blocks", preceding)
    monkeypatch.setattr(T, "_update_cached_facet", lambda ws_, version_id, reading_id, locator, found:
                        calls["update"].append((locator, found)))
    return ws, handle, calls


def test_ae7_a_cached_none_is_read_again_and_a_facet_found_now_is_shown_and_written_in_place(monkeypatch):
    ws, handle, calls = _cached_with(monkeypatch, datum.none(), T.FACET_READER_VERSION)
    out = T.tool_take_value(ws, handle, {}, _given(), "שיעור")
    f = ws.values["V1"].facet
    assert (f["status"], f["written"], f["part"]) == ("stated", "עוצמת ניצול", "preceding_sentence") and "עוצמת ניצול" in out
    assert calls["blocks"] >= 1
    ((locator, written),) = calls["update"]  # one facet-only update of the existing row, with the stated facet
    assert written == f and locator == ws.handles[handle]["locator"]


def test_a_cached_record_of_the_round_13_reader_is_read_again_even_when_it_states_a_facet(monkeypatch):
    old = _facet("תזרים דמי שכירות ממשרדים", "sentence")
    ws, handle, calls = _cached_with(monkeypatch, old, None, RATE_TEXT, "6.5")  # absent facet_reader: the round-13 reader
    T.tool_take_value(ws, handle, {}, _given(), "שיעור")
    assert ws.values["V1"].facet == old and calls["blocks"] == 0  # its own sentence states it: no block is read for that
    assert len(calls["update"]) == 1  # stated again: the row learns the reader that read it


def test_a_cached_stated_facet_of_the_current_reader_is_reused_with_no_block_read_and_no_write(monkeypatch):
    stated = _facet("תזרים דמי שכירות ממשרדים", "sentence")
    ws, handle, calls = _cached_with(monkeypatch, stated, T.FACET_READER_VERSION, RATE_TEXT, "6.5")
    T.tool_take_value(ws, handle, {}, _given(), "שיעור")
    assert ws.values["V1"].facet == stated and calls["blocks"] == 0 and calls["update"] == []


def test_a_read_again_that_finds_nothing_keeps_the_cached_state_and_writes_nothing(monkeypatch):
    ws, handle, calls = _cached_with(monkeypatch, datum.none(), T.FACET_READER_VERSION)
    monkeypatch.setattr(T, "_preceding_blocks", lambda *a, **k: datum.Reach([], False, {}))
    T.tool_take_value(ws, handle, {}, _given(), "שיעור")
    assert ws.values["V1"].facet["status"] == "none" and calls["update"] == []
    ws, handle, calls = _cached_with(monkeypatch, datum.unparsed(datum.REASON_INTERVENING, "paragraph"), 1)
    monkeypatch.setattr(T, "_preceding_blocks", lambda *a, **k: None)  # the blocks could not be read: never lower a state
    T.tool_take_value(ws, handle, {}, _given(), "שיעור")
    assert ws.values["V1"].facet["status"] == "unparsed" and calls["update"] == []


def test_a_read_again_that_fails_leaves_the_cached_facet_and_the_take_succeeds(monkeypatch):
    ws, handle, calls = _cached_with(monkeypatch, datum.none(), T.FACET_READER_VERSION)

    def boom(*a, **k):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(T.reader, "blocks_between", boom)
    out = T.tool_take_value(ws, handle, {}, _given(), "שיעור")
    assert out.startswith("V1") and ws.values["V1"].facet["status"] == "none" and calls["update"] == []


def test_ae8_a_cached_picture_cell_is_not_read_again_and_says_the_context_was_not_read(monkeypatch):
    ws, handle, calls = _cached_with(monkeypatch, datum.not_read(), None)  # a round-13 picture cell: not read, no reason
    doc = T._cached_value(None, VER, "r1", {})
    doc["facts"]["locator"] = {"region": "p1#0", "vision_table": 0, "row": "מקדם", "row_number": 1, "column": "ערך",
                               "column_number": 2}
    doc["facts"]["table"] = [str(VER), "p1#0#0"]
    doc["facts"]["anchor"] = {"vision": {"region": "p1#0", "table": 0, "row": 0, "column": 1}}
    doc["record"]["locator"] = doc["facts"]["locator"]
    T.tool_take_value(ws, handle, {}, _given(), "שיעור")
    f = ws.values["V1"].facet
    assert (f["status"], f["reason"]) == ("not_read", "picture_cell") and calls["blocks"] == 0 and calls["update"] == []
