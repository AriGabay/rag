"""Value stance and attribution through the stored reading (U10, R10, R23, R24, AE7).

A synthetic decision: the respondent's section states one figure, the decision's section adopts another. Read from
the database, ``take_value`` records who stated each figure and how, from the words around it or its section, and
the section path of its block; an answer giving the respondent's figure as the decision's is caught, while the
adopted figure, and the respondent's figure attributed to the respondent, are kept. A stored measurement's stance is
listed by ``find_measurements``.

Within the right appraisal context (round 7 U6: KTD7; R20–R22, AE6): a file holding two appraisals (the synthetic R7b,
and files built from blocks) is read as two contexts by outline, read, search and take_value, its merged comparables
table split by row position; a value records its context and whether its subject names it; with enforcement on, a
claim about one property resting on the other's figure is removed as ``wrong_subject``, ``calculate`` needs a
comparison component to combine them, an appendix's comparison figure is not the appraised property's, and a party's
figure keeps its stance inside its context. The context count script prints ids, counts and kinds only. Synthetic
text only; the parties, property and figures are invented."""

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import text

from app.chat import tools as T
from app.chat import verify
from app.db import tenant_tx
from app.measurements.extract import EXTRACTION_VERSION
from tests.factories import make_document, make_office

pytestmark = pytest.mark.db

TITLE = "החלטה בערר סינתטי על שווי קרקע"
RESPONDENT = "4. טענות המשיבה"
DECISION = "6. דיון והכרעה"
CLAIM = "המשיבה טוענת כי שווי הקרקע הוא 200 ₪ למ\"ר."
SECTION_ONLY = "שווי המבנה הוא 120 ₪ למ\"ר."
ADOPT = "לאחר שבחנתי את טענות הצדדים, אני קובע כי שווי הקרקע הוא 190 ₪ למ\"ר."
PLAIN = "שווי החניה הוא 80 ₪ למ\"ר."


def _decision(office) -> tuple[str, object]:
    doc, ver = make_document(office, office.default_group_id, TITLE)
    blocks = [("heading", RESPONDENT, [RESPONDENT]), ("paragraph", CLAIM, [RESPONDENT]),
              ("paragraph", SECTION_ONLY, [RESPONDENT]), ("heading", DECISION, [DECISION]),
              ("paragraph", ADOPT, [DECISION]), ("paragraph", PLAIN, [DECISION])]
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET page_count = 1, ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": ver, "i": json.dumps({"reading_id": "reading-1"})})
        for i, (kind, t, path) in enumerate(blocks):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, page, text, status) VALUES (app_office(), :d, :v, :b, :k, :s, :sp, 1, :t, 'read')"),
                {"d": doc, "v": ver, "b": i, "k": kind, "s": path[-1], "sp": path, "t": t})
    return str(doc), ver


@pytest.fixture
def office(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    a.doc, a.ver = _decision(a)
    return a


def _read_page(ws: T.Workspace, doc: str) -> str:
    out = T.tool_read(ws, {"pages": {"document": doc, "from_page": 1, "to_page": 1}})
    return re.search(r'<source id="(S\d+)"', out).group(1)


def _quote(q: str, number: str) -> dict:
    return {"table": None, "row": None, "row_number": None, "column": None, "column_number": None, "quote": q,
            "number": number}


def _meaning(**kw) -> dict:
    return {"kind": "value_per_area", "unit": "ILS_per_sqm", "period": "none", "vat": "unknown", "area_basis": "",
            "subject": "הקרקע", "role": "other", "stated_by": "", "stance": "unknown", "scenario": ""} | kw


def _take(ws, sid: str, q: str, number: str, **kw):
    T.tool_take_value(ws, sid, _quote(q, number), _meaning(**kw), "שווי למ\"ר")
    return list(ws.values.values())[-1]


def test_each_figure_is_attributed_by_its_own_words_or_section(office):
    ws = T.Workspace(ctx=office.ctx())
    sid = _read_page(ws, office.doc)
    claim = _take(ws, sid, CLAIM, "200")
    assert (claim.stance, claim.stated_by, claim.section) == ("claim", "המשיבה", RESPONDENT)
    assert claim.provenance["stance"] == claim.provenance["stated_by"] == "source"
    by_section = _take(ws, sid, SECTION_ONLY, "120")
    assert (by_section.stance, by_section.stated_by) == ("claim", "המשיבה")
    adopted = _take(ws, sid, ADOPT, "190", stance="adopted")
    assert adopted.stance == "adopted" and adopted.provenance["stance"] == "source" and adopted.section == DECISION
    unknown = _take(ws, sid, PLAIN, "80")
    assert unknown.stance == "unknown" and unknown.certainty == "verified"
    asserted = _take(ws, sid, PLAIN, "80", stance="adopted", stated_by="השמאי המכריע")
    assert asserted.certainty == "model_asserted" and T.value_status(ws, asserted.vid) == T.STATUS_UNCERTAIN
    body = verify._source_parts(ws, claim.vid)[1]
    assert RESPONDENT in body and "המשיבה" in body and "נמצא במקור" in body
    assert "ייחוס: לא ידוע" in verify._source_parts(ws, unknown.vid)[1]


def test_ae7_the_respondents_figure_as_the_decisions_is_caught_and_the_rest_is_kept(office):
    ws = T.Workspace(ctx=office.ctx())
    sid = _read_page(ws, office.doc)
    claim = _take(ws, sid, CLAIM, "200")
    adopted = _take(ws, sid, ADOPT, "190", stance="adopted")
    answer = (f"ההחלטה קבעה שווי של 200 ₪ למ\"ר [{claim.vid}].\n\n"
              f"השמאי המכריע קבע שווי של 190 ₪ למ\"ר [{adopted.vid}].\n\n"
              f"לטענת המשיבה השווי 200 ₪ למ\"ר [{claim.vid}].")
    units = verify.split_units(answer)
    problems = verify.deterministic(units, ws, "מה שווי הקרקע שנקבע בהחלטה?")
    flagged = {p.unit.index for p in problems}
    assert len(flagged) == 1 and "200" in units[min(flagged)].text and "ההחלטה" in units[min(flagged)].text
    assert all(p.removes_unit for p in problems)


def test_find_measurements_lists_a_stored_stance(office):
    with tenant_tx(office.system) as conn:
        conn.execute(text(
            "INSERT INTO measurements (office_id, document_id, version_id, block_index, statement_key, metric,"
            " metric_kind, value, value_form, value_text, unit, period, vat, subject_role, value_role, quote,"
            " extraction_version, model, status, issues, stated_by, stance) VALUES (app_office(), :d, :v, 1, 'b1:x',"
            " 'שווי קרקע', 'value_per_area', 200, 'exact', '200 ₪', 'ILS_per_sqm', 'none', 'unknown', 'general',"
            " 'other', :q, :e, 'test', 'auto_validated', '[]'::jsonb, 'המשיבה', 'claim')"),
            {"d": office.doc, "v": office.ver, "q": CLAIM, "e": EXTRACTION_VERSION})
    ws = T.Workspace(ctx=office.ctx())
    out = T.tool_find_measurements(ws, "שווי קרקע", ["value_per_area"])
    assert "ייחוס: טענה של המשיבה" in out
    mid = next(iter(ws.measurements))
    assert "טענה" in verify._source_parts(ws, mid)[1]


# --- appraisal contexts inside one file (round 7 U6: KTD7; R20–R22, AE6) ---------------------------------------------
#
# R7b holds two appraisals in one file, with the same numbered chapters and similar values; the extraction gave both
# appraisals' chapters the same section paths and merged their comparables tables into one table. The tools read each
# appraisal as its own context — outline, read, search and take_value show the context, and the merged table's rows
# are split by their position — and, with enforcement on, a figure of one appraisal is never accepted for the other.

from app.chat import calc, contexts, meaning  # noqa: E402
from app.config import get_settings  # noqa: E402
from tests.integration.test_chat_round7_reproductions import _fact, ingest_round7  # noqa: E402

FIRST_TITLE, SECOND_TITLE = _fact("two_appraisals", "first_title"), _fact("two_appraisals", "second_title")
FIRST_SQM, SECOND_SQM = _fact("two_appraisals", "first_per_sqm"), _fact("two_appraisals", "second_per_sqm")
CALC_SECTION = FIRST_SQM["section"]


@pytest.fixture
def r7b(office, monkeypatch):
    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    return ingest_round7(office, monkeypatch, "two_appraisals")


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", True)


def _group(outline: str, n: int) -> str:
    """The part of an outline listing context ``n``'s sections."""
    part = outline.split(f"\nהקשר {n}: ", 1)[1]
    return re.split(r"\nהקשר \d+: |\nטבלאות:", part, maxsplit=1)[0]


def _handle(part: str, name: str) -> str:
    m = re.search(r"([§T]\d+) «" + re.escape(name) + "»", part)
    assert m, part
    return m.group(1)


def _sid(out: str) -> str:
    return re.search(r'<source id="(S\d+)"', out).group(1)


def _sqm(**kw) -> dict:
    return _meaning(**({"subject": ""} | kw))


def _two(ws: T.Workspace, doc: str) -> tuple[str, str, str]:
    outline = T.tool_outline(ws, doc)
    first = T.tool_read(ws, {"section": _handle(_group(outline, 1), CALC_SECTION)})
    second = T.tool_read(ws, {"section": _handle(_group(outline, 2), CALC_SECTION)})
    return outline, first, second


def test_r7b_each_appraisal_is_its_own_context_in_outline_read_and_table(office, r7b):
    ws = T.Workspace(ctx=office.ctx())
    outline, first, second = _two(ws, r7b)
    label_1 = f"{FIRST_TITLE['street']} · גוש {FIRST_TITLE['block']} חלקה {FIRST_TITLE['parcel']}"
    label_2 = f"{SECOND_TITLE['street']} · גוש {SECOND_TITLE['block']} חלקה {SECOND_TITLE['parcel']}"
    assert f"הקשר 1: {label_1} (עמוד 1)" in outline and f"הקשר 2: {label_2} (עמוד 2)" in outline
    # the same numbered chapter is two sections, one per appraisal, each with its own page
    assert _handle(_group(outline, 1), CALC_SECTION) != _handle(_group(outline, 2), CALC_SECTION)
    assert "עמוד 2" not in _group(outline, 1) and "עמוד 1," not in _group(outline, 2)
    assert FIRST_SQM["value"] in first and SECOND_SQM["value"] not in first
    assert SECOND_SQM["value"] in second and FIRST_SQM["value"] not in second
    assert f'context="הקשר 2: {label_2}' in second and 'location="עמוד 2' in second
    # the second appraisal's title block is its own, not a part of the first's last chapter
    assert SECOND_TITLE["street"] not in T.tool_read(ws, {"section": _handle(_group(outline, 1), "6. סיכום")})
    # the merged comparables table: its rows on page 2 are the second appraisal's, said in the outline, in the second
    # appraisal's comparables chapter and where the table is read
    table = re.search(r"- (T\d+) «[^»]*» — [^\n]*", outline)
    assert "שורות 4–6 (עמוד 2): הקשר 2" in table.group(0), table.group(0)
    chapter = T.tool_read(ws, {"section": _handle(_group(outline, 2), "4. נתוני השוואה")})
    assert f"שורות 4–6 (עמוד 2) של {table.group(1)} שייכות להקשר הזה" in chapter, chapter
    rows = T.tool_read(ws, {"table": table.group(1)})
    assert rows.index("[שורות 1–3 — הקשר 1") < rows.index("30871/22") < rows.index("[שורות 4–6 — הקשר 2") < rows.index(
        "30874/12"), rows
    # a cell of a row on page 2 is the second appraisal's
    T.tool_take_value(ws, _sid(rows), {"row": "30874/12", "column": "מחיר למ״ר (₪)", "number": "22,500"},
                      _sqm(kind="price_per_area"), "עסקה")
    assert list(ws.values.values())[-1].context["number"] == 2


def test_r7b_search_hits_say_their_context(office, r7b):
    ws = T.Workspace(ctx=office.ctx())
    out = T.tool_search(ws, "השווי למ״ר שנקבע לנכס", [r7b], 12)
    hits = re.findall(r'<source id="S\d+"[^>]*>', out)
    assert any('context="הקשר 2: ' + SECOND_TITLE["street"] in h for h in hits), out
    assert any('context="הקשר 1: ' + FIRST_TITLE["street"] in h for h in hits), out


def test_ae6_the_second_property_uses_the_second_figure_and_the_first_figure_for_it_is_removed(office, r7b, enforced):
    ws = T.Workspace(ctx=office.ctx())
    ws.requirements = verify.TurnRequirements()
    ws.requirements.adopt([{"id": "N1", "text": "השווי למ״ר", "subject": SECOND_TITLE["street"]}], "analysis")
    _, first, second = _two(ws, r7b)
    out = T.tool_take_value(ws, _sid(first), _quote(FIRST_SQM["text"].rstrip("."), FIRST_SQM["value"]),
                            _sqm(subject=SECOND_TITLE["street"]), "השווי למ״ר")
    wrong = list(ws.values.values())[-1]
    assert wrong.context["number"] == 1 and wrong.subject_from == "contradicted"
    assert "הקשר 1" in out and "אינו הנכס של ההקשר" in out, out  # the tool says so at once
    T.tool_take_value(ws, _sid(second), _quote(SECOND_SQM["text"].rstrip("."), SECOND_SQM["value"]),
                      _sqm(subject=SECOND_TITLE["street"]), "השווי למ״ר")
    right = list(ws.values.values())[-1]
    assert right.context["number"] == 2 and right.subject_from == "context"
    assert ws.anchors[right.vid]["pages"] == [SECOND_SQM["page"]]
    # the judge reads the context each value belongs to
    assert "הקשר בקובץ (הנכס שהערך שייך לו): הקשר 2" in verify._source_parts(ws, right.vid)[1]
    assert "הנושא שניתן לו הוא נכס של הקשר אחר" in verify._source_parts(ws, wrong.vid)[1]
    answer = (f"השווי למ״ר שנקבע לנכס ב{SECOND_TITLE['street']} הוא {FIRST_SQM['value']} ₪ [{wrong.vid}].\n\n"
              f"השווי למ״ר שנקבע לנכס ב{SECOND_TITLE['street']} הוא {SECOND_SQM['value']} ₪ [{right.vid}].\n\n"
              f"השווי למ״ר שנקבע לנכס הוא {FIRST_SQM['value']} ₪ [{wrong.vid}].")
    units = verify.split_units(answer)
    problems = verify.deterministic(units, ws, f"מה השווי למ״ר של הנכס ב{SECOND_TITLE['street']}?")
    flagged = {p.unit.index: p for p in problems}
    assert set(flagged) == {units[0].index, units[2].index}, [p.as_dict() for p in problems]
    assert all(p.failure_kind == "wrong_subject" and p.removes_unit for p in flagged.values())
    assert "הקשר 1" in flagged[units[0].index].reason


def test_ae6_without_enforcement_nothing_is_removed_for_its_context(office, r7b, monkeypatch):
    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", False)
    ws = T.Workspace(ctx=office.ctx())
    _, first, _ = _two(ws, r7b)
    T.tool_take_value(ws, _sid(first), _quote(FIRST_SQM["text"].rstrip("."), FIRST_SQM["value"]),
                      _sqm(subject=SECOND_TITLE["street"]), "השווי למ״ר")
    vid = next(iter(ws.values))
    units = verify.split_units(f"השווי למ״ר לנכס ב{SECOND_TITLE['street']} הוא {FIRST_SQM['value']} ₪ [{vid}].")
    assert verify.deterministic(units, ws, "מה השווי?") == []


def test_calculate_across_two_appraisals_needs_a_component_comparing_them(office, r7b, enforced):
    ws = T.Workspace(ctx=office.ctx())
    _, first, second = _two(ws, r7b)
    for sid, fact, title in ((_sid(first), FIRST_SQM, FIRST_TITLE), (_sid(second), SECOND_SQM, SECOND_TITLE)):
        T.tool_take_value(ws, sid, _quote(fact["text"].rstrip("."), fact["value"]), _sqm(subject=title["street"]),
                          "השווי למ״ר")
    # the subjects differ, so a justification is needed either way (calc's subject rule); it does not lift contexts
    expression = json.dumps({"expression": "V2 - V1", "label": "הפרש", "justification": "השוואה בין שתי השומות"})
    refused = T.run_tool(ws, "calculate", expression)
    assert refused.startswith("שגיאה:") and "הקשר 1" in refused and "הקשר 2" in refused, refused
    assert "משווה" in refused
    ws.requirements = verify.TurnRequirements()
    ws.requirements.adopt([{"id": "N1", "text": "הפרש השווי למ״ר בין השומות", "kind": "calculation",
                            "compares": [FIRST_TITLE["street"], SECOND_TITLE["street"]]}], "analysis")
    allowed = json.loads(T.run_tool(ws, "calculate", expression))
    assert allowed["value"] == "700"


@pytest.mark.parametrize("key", ["plan_status", "residual", "decision",
                                 "cost_table_image", "D1"])
def test_a_single_appraisal_file_shows_no_context(office, monkeypatch, key):
    """Numbered chapters, a planning chapter naming plans and a comparables table of other parcels: one context."""
    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", True)
    if key == "D1":
        from pathlib import Path

        from tests.integration.test_documents_api import ingest

        doc, _ = ingest(office, Path(__file__).resolve().parents[1] / "fixtures" / "D1_synthetic_harozim_digital.pdf")
    else:
        doc = ingest_round7(office, monkeypatch, key)
    ws = T.Workspace(ctx=office.ctx())
    outline = T.tool_outline(ws, doc)
    assert "הקשר" not in outline
    page = T.tool_read(ws, {"pages": {"document": doc, "from_page": 1, "to_page": 2}})
    assert "context=" not in page and "הקשר" not in page
    found = T.tool_search(ws, "שווי", [doc], 6)
    assert "context=" not in found
    assert not ws.contexts[next(iter(ws.contexts))].multi


# a decision-like second appraisal in one file: a party's figure keeps its stance within its context (R22)
# the first report ends at its chapter 8; the second's numbering starts again below it (a new report)
APPRAISAL_A = [("paragraph", "שומה נגדית — רחוב הערבה 3\nגוש: 40100 חלקה: 12", [], 1, 40),
               ("heading", "8. השומה", ["8. השומה"], 1, 120),
               ("paragraph", "שווי הקרקע הוא 180 ₪ למ\"ר.", ["8. השומה"], 1, 140)]
APPRAISAL_B = [("paragraph", "שומה מכרעת — רחוב השיטה 5\nגוש: 40102 חלקה: 4", ["8. השומה"], 2, 40),
               ("heading", RESPONDENT, [RESPONDENT], 2, 120), ("paragraph", CLAIM, [RESPONDENT], 2, 140),
               ("heading", DECISION, [DECISION], 2, 200), ("paragraph", ADOPT, [DECISION], 2, 220)]


def _two_in_one(office) -> str:
    doc, ver = make_document(office, office.default_group_id, "שתי שומות סינתטיות")
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET page_count = 2, ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": ver, "i": json.dumps({"reading_id": "reading-1"})})
        for i, (kind, t, path, page, top) in enumerate(APPRAISAL_A + APPRAISAL_B):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, page, bbox, text, status) VALUES (app_office(), :d, :v, :b, :k, :s, :sp, :p,"
                " CAST(:bb AS jsonb), :t, 'read')"),
                {"d": doc, "v": ver, "b": i, "k": kind, "s": path[-1] if path else None, "sp": path, "p": page,
                 "bb": json.dumps([50, top, 500, top + 12]), "t": t})
    return str(doc)


def test_a_partys_figure_inside_the_second_appraisal_keeps_its_stance(office, enforced):
    doc = _two_in_one(office)
    ws = T.Workspace(ctx=office.ctx())
    page = T.tool_read(ws, {"pages": {"document": doc, "from_page": 2, "to_page": 2}})
    assert 'context="הקשר 2: רחוב השיטה 5' in page
    claim = _take(ws, _sid(page), CLAIM, "200", subject="רחוב השיטה 5")
    adopted = _take(ws, _sid(page), ADOPT, "190", stance="adopted", subject="רחוב השיטה 5")
    assert (claim.stance, claim.stated_by, claim.provenance["stance"]) == ("claim", "המשיבה", "source")
    assert claim.context["number"] == adopted.context["number"] == 2 and claim.subject_from == "context"
    answer = (f"ההחלטה קבעה לנכס ברחוב השיטה 5 שווי של 200 ₪ למ\"ר [{claim.vid}].\n\n"
              f"השמאי המכריע קבע לנכס ברחוב השיטה 5 שווי של 190 ₪ למ\"ר [{adopted.vid}].\n\n"
              f"לטענת המשיבה השווי של הנכס ברחוב השיטה 5 הוא 200 ₪ למ\"ר [{claim.vid}].")
    units = verify.split_units(answer)
    problems = verify.deterministic(units, ws, "מה השווי שנקבע לנכס ברחוב השיטה 5?")
    assert [(p.unit.index, p.check) for p in problems] == [(units[0].index, "misattribution")]


def test_an_appendix_comparison_figure_is_not_the_appraised_value(office, enforced):
    doc, ver = make_document(office, office.default_group_id, "שומה עם נספח השוואה")
    blocks = [("paragraph", "כתובת הנכס: רחוב הערבה 3\nגוש: 40100 חלקה: 12", [], 1, 40),
              ("heading", "3. השומה", ["3. השומה"], 1, 120),
              ("paragraph", "השווי למ\"ר שנקבע לנכס הוא 180 ₪.", ["3. השומה"], 1, 140),
              ("heading", "נספח א׳ — נכס השוואה ברחוב הדקל 8, גוש 40105 חלקה 2", ["נספח א׳"], 2, 40),
              ("paragraph", "השווי למ\"ר של נכס ההשוואה הוא 210 ₪.", ["נספח א׳"], 2, 60),
              # the appended report has its own numbering, from 1 again
              ("heading", "1. תיאור נכס ההשוואה", ["נספח א׳", "1. תיאור נכס ההשוואה"], 2, 90)]
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET page_count = 2, ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": ver, "i": json.dumps({"reading_id": "reading-1"})})
        for i, (kind, t, path, page, top) in enumerate(blocks):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, page, bbox, text, status) VALUES (app_office(), :d, :v, :b, :k, :s, :sp, :p,"
                " CAST(:bb AS jsonb), :t, 'read')"),
                {"d": doc, "v": ver, "b": i, "k": kind, "s": path[-1] if path else None, "sp": path, "p": page,
                 "bb": json.dumps([50, top, 500, top + 12]), "t": t})
        # the measurement pass may label an appendix figure as the appraised property's: both are stored so
        for block, value in ((2, 180), (4, 210)):
            conn.execute(text(
                "INSERT INTO measurements (office_id, document_id, version_id, block_index, statement_key, metric,"
                " metric_kind, value, value_form, value_text, unit, period, vat, subject_role, value_role, quote,"
                " extraction_version, model, status, issues) VALUES (app_office(), :d, :v, :b, :k, 'שווי למ\"ר',"
                " 'value_per_area', :x, 'exact', :t, 'ILS_per_sqm', 'none', 'unknown', 'appraised_property',"
                " 'appraiser_determination', 'ציטוט', :e, 'test', 'auto_validated', '[]'::jsonb)"),
                {"d": doc, "v": ver, "b": block, "k": f"b{block}:x", "x": value, "t": f"{value} ₪",
                 "e": EXTRACTION_VERSION})
    ws = T.Workspace(ctx=office.ctx())
    page = T.tool_read(ws, {"pages": {"document": str(doc), "from_page": 1, "to_page": 1}})
    sid = _sid(page)
    unit = verify.split_units(f"השווי למ\"ר שנקבע לנכס הוא 180 ₪ [{sid}].")[0]
    rows = meaning.Fetcher(ws).subject_measurements({doc}, unit)
    assert [r.block_index for r in rows] == [2]
    # the appendix figure taken as the appraised property's value is the comparison property's
    appendix = T.tool_read(ws, {"pages": {"document": str(doc), "from_page": 2, "to_page": 2}})
    _take(ws, _sid(appendix), "השווי למ\"ר של נכס ההשוואה הוא 210 ₪", "210", subject="רחוב הערבה 3")
    value = list(ws.values.values())[-1]
    assert value.context["number"] == 2 and value.subject_from == "contradicted"
    units = verify.split_units(f"השווי למ\"ר שנקבע לנכס ברחוב הערבה 3 הוא 210 ₪ [{value.vid}].")
    assert [p.failure_kind for p in verify.deterministic(units, ws, "מה השווי למ\"ר של הנכס?")] == ["wrong_subject"]
    assert contexts.of_version  # derived through the stored reading only
    assert calc.Value  # the value carries its context (``calc.Value.context``)


def _count_script():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "count_appraisal_contexts.py"
    spec = importlib.util.spec_from_file_location("count_appraisal_contexts", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_context_count_script_reads_only_and_prints_no_document_content(office, monkeypatch):
    two = ingest_round7(office, monkeypatch, "two_appraisals")
    one = ingest_round7(office, monkeypatch, "plan_status")
    script = _count_script()
    rows = {r["document_id"]: r for r in script.count_office(office.system)}
    assert [c["pages"] for c in rows[two]["contexts"]] == [(1, 1), (2, 2)]
    assert rows[two]["contexts"][0]["kinds"] == ["address", "block_parcel"]
    assert len(rows[one]["contexts"]) == 1 and len(rows[office.doc]["contexts"]) == 1
    out = script.report(list(rows.values()))
    assert f"{two}  pages=2  contexts=2  [1: page 1, kinds=address+block_parcel] [2: page 2," in out, out
    assert "multi_context=1" in out and f"single_context={len(rows) - 1}" in out
    # ids, counts, page numbers and kinds only: no Hebrew, no identifier value, no title
    assert not re.search(r"[א-ת]", out) and "30871" not in out and "30874" not in out, out


@pytest.mark.parametrize(("subjects", "said"), [
    ([SECOND_TITLE["street"]], "הוא של הקשר 2: "),
    (["רחוב התאנה 40"], "אינו מזוהה באף אחד מההקשרים בקובץ"),
    ([f"{FIRST_TITLE['street']} ו{SECOND_TITLE['street']}"], "מתאים לכמה הקשרים (הקשר 1, הקשר 2)"),
])
def test_the_tools_say_which_context_the_asked_property_is_or_that_none_or_several_are(office, r7b, subjects, said):
    ws = T.Workspace(ctx=office.ctx())
    ws.requirements = verify.TurnRequirements()
    ws.requirements.adopt([{"id": f"N{n}", "text": "השווי", "subject": x} for n, x in enumerate(subjects, 1)],
                          "analysis")
    assert said in T.tool_outline(ws, r7b)


def test_without_components_the_question_names_the_context_or_the_tools_ask_to_choose(office, r7b):
    ws = T.Workspace(ctx=office.ctx())
    ws.user_messages = [{"turn": 1, "text": "מה השווי למ״ר בקובץ?", "current": True}]
    assert "השאלה אינה נוקבת באחד מהנכסים האלה" in T.tool_outline(ws, r7b)
    ws.user_messages = [{"turn": 1, "text": f"מה השווי למ״ר של הנכס ב{SECOND_TITLE['street']}?", "current": True}]
    assert "הוא של הקשר 2" in T.tool_outline(ws, r7b)


def test_on_a_turned_cropped_page_the_stored_reading_places_table_rows_by_their_rendered_position(office):
    """The stored reading (``contexts.of_version``, ``reader.blocks_at``) brings each block's page geometry, so the
    second appraisal's start (stored in pdfplumber's frame) is compared with the rows' cell boxes (stored on the
    rendered page) in one frame, on a page turned by /Rotate 90 whose CropBox is offset."""
    from app.chat import contexts, reader
    from tests.unit.test_appraisal_context import SHIFT, TURNED, turned_two

    doc, ver = make_document(office, office.default_group_id, "שתי שומות בעמוד מסובב (סינתטי)", sha="5" * 64)
    blocks = turned_two(geometry=None)
    rows = [{"page": 1, "cells": ["א"], "cell_boxes": [[40.0, 160.0, 90.0, 172.0]]},
            {"page": 2, "cells": ["ב"], "cell_boxes": [[40.0, 60.0, 90.0, 72.0]]},
            {"page": 2, "cells": ["ג"], "cell_boxes": [[40.0, 130.0, 90.0, 142.0]]},
            {"page": 2, "cells": ["ד"], "cell_boxes": [[40.0, 300.0, 90.0, 312.0]]}]
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET page_count = 2, ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": ver, "i": json.dumps({"reading_id": "reading-turned"})})
        for page in (1, 2):
            conn.execute(text(
                "INSERT INTO pages (office_id, document_id, version_id, page_no, text, method, quality, ok, mediabox,"
                " cropbox, rotation, display_width, display_height) VALUES (app_office(), :d, :v, :n, '',"
                " 'text_layer', 1, true, CAST(:mb AS jsonb), CAST(:cb AS jsonb), :r, :w, :h)"),
                {"d": doc, "v": ver, "n": page, "mb": json.dumps(TURNED["mediabox"]),
                 "cb": json.dumps(TURNED["cropbox"]), "r": TURNED["rotation"], "w": TURNED["display_width"],
                 "h": TURNED["display_height"]})
        for b in blocks:
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, page, bbox, text, status, table_index) VALUES (app_office(), :d, :v, :b, :k, NULL,"
                " '{}', :p, CAST(:bb AS jsonb), :t, 'read', :ti)"),
                {"d": doc, "v": ver, "b": b.block_index, "k": b.kind, "p": b.page, "bb": json.dumps(b.bbox),
                 "t": b.text, "ti": b.table_index})
        cx = contexts.of_version(conn, ver, "reading-turned")
        assert cx.multi and cx.segments[1].start == (2, 260.0 - SHIFT)
        groups = reader.table_contexts(cx, {"rows": rows}, 4, 1)
        assert [(g["context"], g["first"], g["last"]) for g in groups] == [(1, 1, 2), (2, 3, 4)]
        edge = reader.blocks_at(conn, ver, [6])[6]
        assert contexts.top_of(edge) == 260.0 - SHIFT
