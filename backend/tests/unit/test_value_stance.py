"""Value stance and attribution (U10, R10, R23, R24, KTD8): database-free.

A value taken in chat records who stated it, whether it is the adopted conclusion, a party's claim, a proposal or an
estimate, and the scenario it belongs to. Each is marked as found in the text around the value (its clause, its
sentence, its table header, or its section) or as asserted by the model; an asserted one leaves the value
uncertain, never verified. A figure is never the decision's because it appears in the decision document: the words
that say who stated it decide. The judge sees the value's section path and stance, and an answer that presents a
party's figure as the decision's is caught. A value whose region stayed unclear after a focused re-read carries
that on the value itself, so coverage and the stored answer see it. Synthetic decision text only; the parties,
property and figures are invented."""

from __future__ import annotations

import contextlib
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.chat import calc, coverage, reader, verify
from app.chat import tools as T
from app.measurements import extract as X

RESPONDENT = "4. טענות המשיבה"
DECISION = "6. דיון והכרעה"
CLAIM_TEXT = "המשיבה טוענת כי שווי הנכס הוא 200 ₪ למ\"ר."
ADOPT_TEXT = "לאחר שבחנתי את טענות הצדדים, אני קובע כי שווי הנכס הוא 190 ₪ למ\"ר."
PLAIN_TEXT = "שווי הנכס הוא 210 ₪ למ\"ר."


def _at(text: str, number: str) -> X.Attribution | None:
    i = text.index(number)
    return X.attribution_at(text, i, i + len(number))


# --- the words that say who stated a value ------------------------------------------------------------------------

def test_a_claim_is_attributed_to_the_party_that_makes_it():
    a = _at("לטענת המשיבה, שווי הנכס הוא 200 ₪ למ\"ר.", "200")
    assert a is not None and a.stance == "claim" and a.stated_by == "המשיבה"
    a = _at(CLAIM_TEXT, "200")
    assert a.stance == "claim" and a.stated_by == "המשיבה"


def test_in_one_sentence_each_number_takes_the_words_of_its_own_clause():
    text = "המשיבה טענה לשווי של 200 ₪ למ\"ר ואני קובע שווי של 190 ₪ למ\"ר."
    assert _at(text, "200").stance == "claim" and _at(text, "200").stated_by == "המשיבה"
    adopted = _at(text, "190")
    assert adopted.stance == "adopted" and adopted.stated_by == ""
    assert _at("בניגוד לטענת המשיבה, אני קובע כי השווי 190 ₪.", "190").stance == "adopted"


def test_a_determination_by_a_party_is_still_that_partys_position():
    a = _at("שמאי המשיבה קבע שווי של 200 ₪ למ\"ר.", "200")
    assert a.stance == "claim" and a.stated_by == "שמאי המשיבה"
    b = _at("השמאי המכריע קבע שווי של 190 ₪ למ\"ר.", "190")
    assert b.stance == "adopted" and b.stated_by == "השמאי המכריע"


def test_an_adoption_and_a_claim_in_the_same_clause_are_both_kept():
    a = _at("אני מקבל את עמדת המשיבה ולכן השווי 200 ₪.", "200")
    assert a.stances == frozenset({"adopted", "claim"}) and a.stance is None


def test_a_number_without_attribution_words_has_none():
    assert _at(PLAIN_TEXT, "210") is None
    assert _at("המועד הקובע הוא 2020 והשווי 210 ₪.", "210") is None  # "הקובע" is an adjective here


def test_a_section_gives_a_partys_position_but_never_an_adoption():
    a = X.attribution_in(RESPONDENT, adopted=False)
    assert a.stance == "claim" and a.stated_by == "המשיבה"
    assert X.attribution_in(DECISION, adopted=False) is None
    assert X.attribution_in("הכרעה (₪)", adopted=True).stance == "adopted"  # a column header may say it


def test_names_match_tolerates_hebrew_prefixes():
    assert X.names_match("המשיבה", "לטענת המשיבה, השווי 200")
    assert X.names_match("מצב תכנוני קודם", "במצב התכנוני הקודם השווי 150")
    assert not X.names_match("העוררת", "לטענת המשיבה, השווי 200")
    assert not X.names_match("", "המשיבה")


# --- take_value: recorded and marked by where it was found -------------------------------------------------------

VERSION = reader.Version(uuid.uuid4(), uuid.uuid4(), "החלטה בערר סינתטי", "reading-1", "application/pdf", True, 3,
                         False, "k", "f.pdf")


def _block(i: int, text: str, path: list[str], status: str = "read") -> SimpleNamespace:
    return SimpleNamespace(block_index=i, text=text, page=i + 1, status=status, section=path[-1] if path else None,
                           section_path=path, bbox=None, media=None, table_index=None, method=None, note="")


@pytest.fixture
def decision(monkeypatch):
    """A synthetic decision read in one source: the respondent's section, then the decision's."""
    blocks = [_block(0, RESPONDENT, [RESPONDENT]), _block(1, CLAIM_TEXT, [RESPONDENT]),
              _block(2, DECISION, [DECISION]), _block(3, ADOPT_TEXT, [DECISION]), _block(4, PLAIN_TEXT, [DECISION])]
    return _workspace(monkeypatch, blocks)


def _workspace(monkeypatch, blocks, *, status="complete", table=None) -> SimpleNamespace:
    monkeypatch.setattr(T, "tenant_tx", lambda ctx: contextlib.nullcontext(None))
    monkeypatch.setattr(T.reader, "version", lambda conn, vid: VERSION)
    # the version's appraisal contexts are read from the database too: one context, as a single report has (U6)
    monkeypatch.setattr(T.contexts, "of_version", lambda conn, vid, reading: T.contexts.derive([], str(vid)))
    monkeypatch.setattr(T.reader, "blocks_between",
                        lambda conn, vid, a, b, limit=None: [x for x in blocks if a <= x.block_index <= b])
    if table is not None:
        monkeypatch.setattr(T.reader, "table_structure", lambda conn, vid, ti: table)
        monkeypatch.setattr(T.reader, "table_block", lambda conn, vid, ti: blocks[0])
    ws = T.Workspace(ctx=None)
    src = ws.add_source(document_id=VERSION.document_id, version_id=VERSION.version_id, title=VERSION.title,
                        section=blocks[0].section, location="עמודים 1–5", kind="table" if table else "context",
                        text="\n".join([*(b.text for b in blocks), *_table_lines(table)]), block_start=0,
                        block_end=len(blocks) - 1,
                        reading_id="reading-1", status=status, table_index=0 if table else None)
    return SimpleNamespace(ws=ws, sid=src.sid)


def _table_lines(table: dict | None) -> list[str]:
    if table is None:
        return []
    return [" | ".join(table["headers"]), *(" | ".join(r["cells"]) for r in table["rows"])]


def _meaning(**kw) -> dict:
    base = {"kind": "value_per_area", "unit": "ILS_per_sqm", "period": "none", "vat": "unknown", "area_basis": "",
            "subject": "הנכס בערר", "role": "other", "stated_by": "", "stance": "unknown", "scenario": ""}
    return base | kw


def _quote(q: str, number: str) -> dict:
    return {"table": None, "row": None, "row_number": None, "column": None, "column_number": None, "quote": q,
            "number": number}


def _take(d, q: str, number: str, **meaning_) -> calc.Value:
    T.tool_take_value(d.ws, d.sid, _quote(q, number), _meaning(**meaning_), "שווי למ\"ר")
    return list(d.ws.values.values())[-1]


def test_a_partys_figure_is_recorded_as_its_claim_from_the_text(decision):
    v = _take(decision, CLAIM_TEXT, "200")
    assert (v.stance, v.stated_by) == ("claim", "המשיבה")
    assert v.provenance["stance"] == "source" and v.provenance["stated_by"] == "source"
    assert v.section == RESPONDENT and v.certainty == "verified"
    assert v.public()["stance"] == "claim" and v.public()["section"] == RESPONDENT


def test_the_decisions_figure_is_recorded_as_adopted_from_the_text(decision):
    v = _take(decision, ADOPT_TEXT, "190", stance="adopted")
    assert v.stance == "adopted" and v.provenance["stance"] == "source"
    assert v.provenance["stated_by"] == "not_stated" and v.section == DECISION and v.certainty == "verified"


def test_a_partys_figure_given_as_adopted_is_asserted_and_uncertain(decision):
    out = T.tool_take_value(decision.ws, decision.sid, _quote(CLAIM_TEXT, "200"), _meaning(stance="adopted"), "שווי")
    v = decision.ws.values["V1"]
    assert v.stance == "adopted" and v.provenance["stance"] == "model_asserted"
    assert v.certainty == "model_asserted" and T.value_uncertain(decision.ws, "V1")
    assert "טענה" in out and "המשיבה" in out and T.STATUS_UNCERTAIN in out


def test_a_stance_and_speaker_the_text_does_not_give_are_asserted_not_verified(decision):
    v = _take(decision, PLAIN_TEXT, "210", stance="adopted", stated_by="השמאי המכריע")
    assert v.provenance["stance"] == "model_asserted" and v.provenance["stated_by"] == "model_asserted"
    assert v.certainty != "verified" and T.value_status(decision.ws, v.vid) == T.STATUS_UNCERTAIN
    head, body, _ = verify._source_parts(decision.ws, v.vid)
    assert "לא נמצא במקור" in body


def test_a_stance_the_source_says_nothing_about_tells_the_model_to_take_it_again_unknown(decision):
    """The model's own stance or speaker, where the words around the number say nothing of who stated it or how
    (U9: a cost table taken as "estimate"): the value stays uncertain, and the tool says plainly that the source
    does not state it and how to take it again; the re-take with stance unknown and no speaker is verified."""
    out = T.tool_take_value(decision.ws, decision.sid, _quote(PLAIN_TEXT, "210"),
                            _meaning(stance="estimate", stated_by="השמאי"), "שווי")
    assert decision.ws.values["V1"].certainty == "model_asserted" and T.STATUS_UNCERTAIN in out
    assert T.MSG_ATTRIBUTION_UNSTATED in out
    again = T.tool_take_value(decision.ws, decision.sid, _quote(PLAIN_TEXT, "210"), _meaning(), "שווי")
    assert decision.ws.values["V2"].certainty == "verified" and T.STATUS_AUTO in again
    assert T.MSG_ATTRIBUTION_UNSTATED not in again
    # where the source does say who stated it, the tool shows its words instead (and never this hint)
    other = T.tool_take_value(decision.ws, decision.sid, _quote(CLAIM_TEXT, "200"), _meaning(stance="adopted"), "שווי")
    assert T.MSG_ATTRIBUTION_UNSTATED not in other and "המשיבה" in other


def test_the_take_value_schema_says_to_leave_stance_and_speaker_unknown_unless_the_source_states_them():
    tool = next(t for t in T.TOOLS if t["name"] == "take_value")
    props = tool["parameters"]["properties"]["meaning"]["properties"]
    for key in ("stance", "stated_by"):
        description = props[key]["description"]
        assert "המילים המצוטטות" in description and "כותרת השורה או העמודה" in description and "הסעיף" in description


def test_an_unknown_stance_with_no_attribution_words_stays_unknown_and_certain(decision):
    v = _take(decision, PLAIN_TEXT, "210")
    assert v.stance == "unknown" and v.provenance["stance"] == "not_stated" and v.certainty == "verified"


def test_a_section_heading_gives_the_stance_when_the_sentence_does_not(monkeypatch):
    text = "שווי הנכס הוא 200 ₪ למ\"ר."
    d = _workspace(monkeypatch, [_block(0, RESPONDENT, [RESPONDENT]), _block(1, text, [RESPONDENT])])
    v = _take(d, text, "200")
    assert (v.stance, v.stated_by, v.provenance["stance"]) == ("claim", "המשיבה", "source")


def test_a_scenario_is_found_in_the_text_or_asserted(monkeypatch):
    text = "במצב התכנוני הקודם שווי הנכס הוא 150 ₪ למ\"ר."
    d = _workspace(monkeypatch, [_block(0, DECISION, [DECISION]), _block(1, text, [DECISION])])
    assert _take(d, text, "150", scenario="מצב תכנוני קודם").provenance["scenario"] == "source"
    v = _take(d, text, "150", scenario="מצב תכנוני מוצע")
    assert v.provenance["scenario"] == "model_asserted" and v.certainty == "model_asserted"


def test_an_unknown_stance_value_is_rejected():
    with pytest.raises(T.ToolError):
        T.tool_take_value(T.Workspace(ctx=None), "S1", _quote("x", "1"), _meaning(stance="decided"), "")


# --- a table: the unit from its header, the stance from the column (R10) ---------------------------------------------

TABLE = {"headers": ["רכיב", "עמדת המשיבה (₪ למ\"ר)", "הכרעה (₪ למ\"ר)"], "caption": "השוואת עמדות", "title": [],
         "notes": [], "rows": [{"cells": ["קרקע", "200", "190"], "page": 2}]}


def _cell(column: str) -> dict:
    return {"table": None, "row": "קרקע", "row_number": None, "column": column, "column_number": None,
            "quote": None, "number": None}


def test_a_unit_written_only_in_the_column_header_is_the_sources_with_the_header_anchor(monkeypatch):
    d = _workspace(monkeypatch, [_block(0, "השוואת עמדות", [DECISION])], table=TABLE)
    T.tool_take_value(d.ws, d.sid, _cell(TABLE["headers"][2]), _meaning(), "שווי קרקע")
    v = d.ws.values["V1"]
    assert v.unit == "ILS_per_sqm" and v.provenance["unit"] == "source" and v.meaning_from["unit"] == "header"
    assert v.public()["meaning_from"] == {"unit": "header"}
    stub = d.ws.anchors["V1"]  # the cell's stub: its snapshot carries the column header's box (anchors._table_ctx)
    assert (stub["kind"], stub["table_index"], stub["row"], stub["column"]) == ("cell", 0, 0, 2)
    assert stub["context"] == {"unit": "header"}


def test_a_column_header_gives_each_cell_its_stance(monkeypatch):
    d = _workspace(monkeypatch, [_block(0, "השוואת עמדות", [DECISION])], table=TABLE)
    T.tool_take_value(d.ws, d.sid, _cell(TABLE["headers"][1]), _meaning(), "עמדת המשיבה")
    T.tool_take_value(d.ws, d.sid, _cell(TABLE["headers"][2]), _meaning(), "הכרעה")
    claim, adopted = d.ws.values["V1"], d.ws.values["V2"]
    assert (claim.stance, claim.stated_by) == ("claim", "המשיבה") and adopted.stance == "adopted"
    assert claim.provenance["stance"] == adopted.provenance["stance"] == "source"


# --- the judge's evidence and the deterministic check (AE7) ---------------------------------------------------------

def test_the_value_evidence_names_its_section_and_stance(decision):
    claim = _take(decision, CLAIM_TEXT, "200")
    head, body, _ = verify._source_parts(decision.ws, claim.vid)
    assert RESPONDENT in body and "טענה" in body and "המשיבה" in body and "נמצא במקור" in body
    unknown = _take(decision, PLAIN_TEXT, "210")
    assert "ייחוס: לא ידוע" in verify._source_parts(decision.ws, unknown.vid)[1]


def test_the_verdict_cache_key_follows_the_stance(decision):
    v = _take(decision, CLAIM_TEXT, "200")
    unit = verify.Unit(0, "השווי 200 [V1].", "השווי 200.", ["V1"])
    before = verify.unit_key(unit, decision.ws, "שאלה", "", {})
    v.stance = "adopted"
    assert verify.unit_key(unit, decision.ws, "שאלה", "", {}) != before


def _unit(text: str, ids: list[str]) -> verify.Unit:
    return verify.Unit(0, text, text, ids)


def test_ae7_a_partys_figure_presented_as_the_decisions_is_caught(decision):
    _take(decision, CLAIM_TEXT, "200")
    _take(decision, ADOPT_TEXT, "190", stance="adopted")
    wrong = verify.deterministic([_unit("ההחלטה קבעה שווי של 200 ₪ למ\"ר.", ["V1"])], decision.ws, "מה נקבע?")
    assert wrong and "המשיבה" in wrong[0].reason and wrong[0].removes_unit
    wrong = verify.deterministic([_unit("השווי שנקבע הוא 200 ₪ למ\"ר.", ["V1"])], decision.ws, "מה נקבע?")
    assert wrong


@pytest.mark.parametrize("text, ids", [
    ("השמאי המכריע קבע שווי של 190 ₪ למ\"ר.", ["V2"]),
    ("לטענת המשיבה השווי 200 ₪ למ\"ר, ואילו בהחלטה נקבע 190 ₪ למ\"ר.", ["V1", "V2"]),
    ("שמאי המשיבה העמיד את השווי על 200 ₪ למ\"ר.", ["V1"]),
    ("שווי של 200 ₪ למ\"ר.", ["V1"]),  # no attribution: the judge decides with the section and stance
])
def test_ae7_the_adopted_figure_and_an_attributed_party_figure_are_kept(decision, text, ids):
    _take(decision, CLAIM_TEXT, "200")
    _take(decision, ADOPT_TEXT, "190", stance="adopted")
    assert verify.deterministic([_unit(text, ids)], decision.ws, "מה נקבע?") == []


def test_the_right_number_given_to_the_wrong_party_is_removed_as_wrong_subject(decision):
    """Round 7 U4 (KTD5, R12): the number is right, its party is not — a removal of kind wrong property or party."""
    _take(decision, CLAIM_TEXT, "200")
    (problem,) = verify.deterministic([_unit("לטענת העוררת השווי 200 ₪ למ\"ר.", ["V1"])], decision.ws, "מה נטען?")
    assert (problem.failure_kind, problem.check, problem.checked_ids) == ("wrong_subject", "misattribution", ["V1"])
    assert problem.removes_unit


def test_a_figure_attributed_to_another_party_is_caught(decision):
    _take(decision, CLAIM_TEXT, "200")
    assert verify.deterministic([_unit("לטענת העוררת השווי 200 ₪ למ\"ר.", ["V1"])], decision.ws, "מה נטען?")


def test_an_asserted_stance_is_not_trusted_by_the_check(decision):
    _take(decision, PLAIN_TEXT, "210", stance="claim", stated_by="המשיבה")  # not in the text: asserted
    assert verify.deterministic([_unit("ההחלטה קבעה 210 ₪ למ\"ר.", ["V1"])], decision.ws, "מה נקבע?") == []


def test_the_judge_policy_has_the_attribution_rule():
    assert "הופעת המספר במסמך" in verify.JUDGE_POLICY and "לא ידוע" in verify.JUDGE_POLICY


# --- reading certainty on the value (U12b follow-up) -----------------------------------------------------------------

def _plain_value(ws, sid: str, **kw) -> calc.Value:
    v = calc.Value("V1", Decimal("48600"), "48,600", sid, uuid.uuid4(), uuid.uuid4(), None, "שומה", "עמוד 1",
                   "תפוסה", "rent", "ILS", "none", "unknown", "", "", "other", {"unit": "source"}, {"quote": "x"},
                   "48,600", **kw)
    ws.values["V1"] = v
    return v


def test_a_value_left_unclear_by_its_re_read_is_uncertain_on_the_value_itself():
    ws = T.Workspace(ctx=None)
    sid = ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="שומה", section=None,
                        location="עמוד 1", kind="context", text="48,600", status="complete").sid
    v = _plain_value(ws, sid, reading="uncertain", reading_note="קריאה לא ודאית")
    assert v.certainty == "uncertain_reading" and v.public()["certainty"] == "uncertain_reading"
    assert coverage._uncertain_value(ws, ["V1"]) and T.value_uncertain(ws, "V1")


def test_a_value_settled_by_a_clear_re_read_stays_settled_in_an_uncertain_source():
    ws = T.Workspace(ctx=None)
    sid = ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="שומה", section=None,
                        location="עמוד 1", kind="context", text="48,600", status="uncertain_reading").sid
    v = _plain_value(ws, sid, reading="clear")
    assert v.certainty == "verified"
    assert not coverage._uncertain_value(ws, ["V1"]) and not T.value_uncertain(ws, "V1")
    v.reading = ""
    assert coverage._uncertain_value(ws, ["V1"]) and T.value_uncertain(ws, "V1")  # its own region was never checked


@pytest.mark.parametrize("clear", [True, False])
def test_take_value_records_the_re_reads_outcome_on_the_value(monkeypatch, clear):
    text = "התפוסה 48,600 מ\"ר."
    d = _workspace(monkeypatch, [_block(0, text, ["3. נתונים"], status=reader.UNCERTAIN)], status="uncertain_reading")
    monkeypatch.setattr(T, "_reread_value", lambda ws, v, block, forms, key: (clear, "" if clear else "לא ודאי"))
    T.tool_take_value(d.ws, d.sid, _quote(text, "48,600"),
                      _meaning(kind="area", unit="sqm", subject="הנכס"), "תפוסה")
    v = d.ws.values["V1"]
    assert v.reading == ("clear" if clear else "uncertain")
    assert v.certainty == ("verified" if clear else "uncertain_reading")
    assert coverage._uncertain_value(d.ws, ["V1"]) is (not clear)


# --- measurements: the same fields, optional, from the text only ----------------------------------------------------

def _passage(text: str, section: str) -> X.Passage:
    return X.Passage("P1", "text", text, 1, None, section, False)


def _text_measurement(text: str, value: str) -> X.TextMeasurement:
    return X.TextMeasurement(passage_id="P1", quote=text.rstrip("."), metric_quote="שווי הנכס", metric="שווי הנכס",
                             metric_kind="value_per_area", value_text=value, unit="ILS_per_sqm", period="none",
                             period_quote="", area_basis="", vat="unknown", vat_quote="", subject="הנכס",
                             subject_role="appraised_property", value_role="other", effective_date="")


def test_a_measurement_records_the_stance_its_text_or_section_gives():
    claim = X.validate_text([_text_measurement(CLAIM_TEXT, "200 ₪")], {"P1": _passage(CLAIM_TEXT, DECISION)})[0]
    assert (claim.stance, claim.stated_by) == ("claim", "המשיבה")
    by_section = X.validate_text([_text_measurement(PLAIN_TEXT, "210 ₪")], {"P1": _passage(PLAIN_TEXT, RESPONDENT)})
    assert (by_section[0].stance, by_section[0].stated_by) == ("claim", "המשיבה")
    plain = X.validate_text([_text_measurement(PLAIN_TEXT, "210 ₪")], {"P1": _passage(PLAIN_TEXT, DECISION)})[0]
    assert plain.stance is None and plain.stated_by is None and plain.status == "auto_validated"


def test_the_take_value_schema_carries_the_attribution_fields():
    tool = next(t for t in T.TOOLS if t["name"] == "take_value")
    props = tool["parameters"]["properties"]["meaning"]["properties"]
    assert {"stated_by", "stance", "scenario"} <= set(props)
    assert props["stance"]["enum"] == list(X.STANCES)


# --- the calculation breakdown's steps and rounding (requested for U7) ----------------------------------------------

def _computation(expression: str, operands: list) -> calc.Computation:
    out = calc.evaluate(calc.parse(expression), {o.id: o for o in operands}, None)
    return calc.Computation("C1", "רווח", expression, expression, out, [], [], 1, "computed", None, "", None, [])


def test_a_computation_lists_its_intermediate_results_at_full_precision_and_as_shown():
    income = calc.operand("V1", "12450000", "ILS", kind="income", subject="פרויקט הדגמה", vat="excluded")
    cost = calc.operand("V2", "10400000", "ILS", kind="cost", subject="פרויקט הדגמה", vat="excluded")
    rise = calc.operand("A1", "5", "percent", assumption=True)
    pub = _computation("V1 - V2*(1+A1%)", [income, cost, rise]).public()
    steps = {s["expression"]: s for s in pub["steps"]}
    assert steps["1 + A1%"]["value"] == "1.05" and steps["V2 × (1 + A1%)"]["display"] == "10,920,000"
    assert all(s["expression"] != "V1 - V2 × (1 + A1%)" for s in pub["steps"])  # the result is not a step
    assert pub["display"]["value"] == "1,530,000" and Decimal(pub["value"]) == 1530000


def test_a_computation_states_the_rounding_rule_its_display_follows():
    a = calc.operand("V1", "143155", "ILS", kind="cost", subject="הפרויקט")
    b = calc.operand("V2", "1000000", "ILS", kind="income", subject="הפרויקט")
    pub = _computation("V1 / V2", [a, b]).public()
    r = pub["rounding"]
    assert (r["rule"], r["decimals"], r["percent"], r["percent_decimals"]) == ("half_up", 4, True, 2)
    assert pub["display"] == {"value": "0.1432", "percent": "14.32%"}  # half up at those places
    assert r["text"] and r["trailing_zeros"] is False
    big = _computation("V1 + V2", [a, calc.operand("V2", "1000000", "ILS", kind="cost", subject="הפרויקט")]).public()
    assert big["rounding"]["decimals"] == 2 and big["rounding"]["percent"] is False and big["steps"] == []


def test_a_scenario_the_source_does_not_name_tells_the_model_to_take_it_again_without_one(decision):
    """A scenario label of the model's own ("בדיקה בסיסית" for the only figures there are) leaves the value
    uncertain and every result built on it conditional: the tool says the source names no such scenario and how
    to take the value again; the re-take with no scenario is verified."""
    out = T.tool_take_value(decision.ws, decision.sid, _quote(PLAIN_TEXT, "210"),
                            _meaning(scenario="בדיקה בסיסית"), "שווי")
    assert decision.ws.values["V1"].certainty == "model_asserted" and T.MSG_SCENARIO_UNSTATED in out
    again = T.tool_take_value(decision.ws, decision.sid, _quote(PLAIN_TEXT, "210"), _meaning(), "שווי")
    assert decision.ws.values["V2"].certainty == "verified" and T.MSG_SCENARIO_UNSTATED not in again
