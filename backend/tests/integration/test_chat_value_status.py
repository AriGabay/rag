"""Statuses without approval and bounded re-reads in the chat tools (U12, R25, R28).

A stored measurement is labelled as a person's decision only when a review recorded one, and a calculation states
its inputs' statuses without implying a review is needed; an uncertain input makes the result conditional. A value
taken from a region ingestion read uncertainly gets a focused visual re-read through the inspect path, at most twice
per value in a turn, and stays uncertain when the re-read is unclear. A search hit in a partly read document is
marked partial only when its own page has an unread region. A cell of a table read by inspect is verified only when
OCR of the crop sees its number in its row and column (U5, KTD6, R16–R19): AE5's total is certain in the same turn;
a number OCR does not see, sees twice, or sees in another row leaves its value uncertain and its calculation
conditional, with the reason. The vision model and OCR are scripted; synthetic documents only."""

from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import text

from app.chat import tools as T
from app.db import tenant_tx
from app.extraction.base import ChunkResult, ExtractionResult, PageResult
from app.measurements.extract import EXTRACTION_VERSION
from app.platform import pipeline
from tests.factories import make_document, make_group, make_office, make_user
from tests.integration.test_chat_inspect import (
    COST,
    COST_TOTAL,
    PICTURE,
    CostTable,
    ScriptedVision,
    cost_meaning,
    cost_ocr,
    emp,
    ingest_cost_table,
    ingest_r1,
    inspect_cost_table,
    region_of,
    tag,
    take_cost,
)

pytestmark = pytest.mark.db

TITLE = "שומה סינתטית לבדיקת סטטוסים"
RENT = "דמי שכירות (₪)"


# --- measurement statuses ----------------------------------------------------------------------------------------------

def _measure(conn, doc, ver, value: int, row: int, status: str, reviewer=None, lost: bool = False) -> None:
    conn.execute(text(
        "INSERT INTO measurements (office_id, document_id, version_id, block_index, table_index, row_index,"
        " statement_key, metric, metric_kind, value, value_form, value_text, unit, period, vat, subject_role,"
        " value_role, quote, extraction_version, model, status, issues, reviewed_by, anchor_lost) VALUES"
        " (app_office(), :d, :v, :b, 0, :r, :k, 'דמי שכירות למ\"ר', 'rent_per_area', :val, 'exact', :vt,"
        " 'ILS_per_sqm', 'month', 'unknown', 'asking', 'asking_price', :q, :e, 'test', :st, '[]'::jsonb, :u,"
        " CAST(:lost AS jsonb))"),
        {"d": doc, "v": ver, "b": row, "r": row, "k": f"t0:r{row}:{value}", "val": value, "vt": f"{value} ₪",
         "q": f"שורה {row}: {value} ₪", "e": EXTRACTION_VERSION, "st": status, "u": reviewer,
         "lost": json.dumps({"reason": "not found"}) if lost else None})


@pytest.fixture
def measured(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = make_document(a, a.default_group_id, TITLE)
    with tenant_tx(a.system) as conn:
        _measure(conn, doc, ver, 95, 1, "verified", reviewer=a.admin_id)
        _measure(conn, doc, ver, 97, 2, "auto_validated")
        _measure(conn, doc, ver, 99, 3, "needs_review")
        _measure(conn, doc, ver, 101, 4, "corrected", reviewer=a.admin_id, lost=True)
    return a


def _mid(out: str, value: int) -> str:
    m = re.search(rf"(M\d+): [^\n]*= {value} ₪[^\n]*", out)
    assert m, out
    return m.group(1)


def _line(out: str, value: int) -> str:
    return re.search(rf"M\d+: [^\n]*= {value} ₪[^\n]*", out).group(0)


def test_find_measurements_labels_each_status_and_a_person_only_with_a_review(measured):
    ws = T.Workspace(ctx=measured.ctx())
    out = T.tool_find_measurements(ws, "דמי שכירות", ["rent_per_area"])
    assert T.STATUS_HUMAN in _line(out, 95)
    assert T.STATUS_AUTO in _line(out, 97)
    assert T.STATUS_UNCERTAIN in _line(out, 99)
    assert T.STATUS_UNCERTAIN in _line(out, 101)  # a person's value whose place was lost on reprocessing
    assert "ממתין לבדיקה" not in out and "ראשוני" not in out


def test_a_calculation_states_statuses_and_an_uncertain_input_makes_it_conditional(measured):
    ws = T.Workspace(ctx=measured.ctx())
    out = T.tool_find_measurements(ws, "דמי שכירות", ["rent_per_area"])
    a, b, c = _mid(out, 95), _mid(out, 97), _mid(out, 99)
    certain = json.loads(T.tool_calculate(ws, f"{a} + {b}", "סכום"))
    assert certain["conditional"] is False and "טרם אומתו" not in certain["note"]
    assert f"{a} {T.STATUS_HUMAN}" in certain["note"] and f"{b} {T.STATUS_AUTO}" in certain["note"]
    shaky = json.loads(T.tool_calculate(ws, f"{b} + {c}", "סכום"))
    assert shaky["conditional"] is True and c in shaky["note"] and "מותנה" in shaky["note"]
    assert ws.computations[shaky["id"]].public()["conditions"]


# --- a focused re-read of an unclear value -------------------------------------------------------------------------------

@pytest.fixture
def vision_office(db, monkeypatch):
    """An office whose inspect reader is scripted (as in ``test_chat_inspect``): an employee in the closed group."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    a = make_office(db, "משרד א", "admin-a@example.test")
    a.private = make_group(a, "קבוצה סגורה")
    a.emp = make_user(a, "emp@example.test", [a.default_group_id, a.private])
    a.vision = ScriptedVision()
    monkeypatch.setattr(T, "inspect_reader", lambda ctx: a.vision)
    return a


def _uncertain_table(vision_office, monkeypatch, ws) -> str:
    """The synthetic raster table, read uncertainly at ingestion; the source its stored reading was returned as."""
    doc = ingest_r1(vision_office, monkeypatch, at_ingestion=ScriptedVision(uncertain=["תפוסה"]))
    region = region_of(ws, doc, marker="קריאה לא ודאית")
    out = T.tool_inspect(ws, {"region": region})
    assert vision_office.vision.calls == []  # the stored reading: no model call yet
    return tag(out, "id")


def _ocr_sees_the_table(monkeypatch) -> None:
    """OCR of the re-read crop is available and confidently sees the table's words: the independent check a
    focused visual re-read is confirmed by (the host has no Hebrew tessdata, so the words are given)."""
    from tests.unit.test_regions import HEADERS, ROWS

    words = [{"text": w, "conf": 95} for cell in [*HEADERS, *[c for r in ROWS for c in r]] for w in cell.split()]
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: True)
    monkeypatch.setattr("app.extraction.images._ocr_words", lambda gray, languages: words)


def _take(ws, sid: str) -> str:
    return T.tool_take_value(ws, sid, {"row": "צפון", "column": RENT},
                             {"kind": "rent", "unit": "ILS", "period": "none", "vat": "unknown", "area_basis": "",
                              "subject": "אזור צפון", "role": "other"}, "דמי שכירות צפון")


def test_an_unclear_value_is_re_read_at_most_twice_and_then_reported_uncertain(vision_office, monkeypatch):
    ws = emp(vision_office)
    sid = _uncertain_table(vision_office, monkeypatch, ws)
    _ocr_sees_the_table(monkeypatch)
    vision_office.vision.uncertain = ["דמי שכירות"]  # the re-read is unclear too
    outs = [_take(ws, sid) for _ in range(3)]
    # the first re-read calls the model; the second is served from the stored reading; the third is not made
    assert len(vision_office.vision.calls) == 1 and list(ws.rereads.values()) == [2]
    assert all(T.STATUS_UNCERTAIN in o for o in outs) and "כבר נקרא שוב 2 פעמים" in outs[2]
    assert set(ws.uncertain_values) == {"V1", "V2", "V3"}
    other = T.tool_take_value(ws, sid, {"row": "מרכז", "column": RENT},
                              {"kind": "rent", "unit": "ILS", "period": "none", "vat": "unknown", "area_basis": "",
                               "subject": "אזור מרכז", "role": "other"}, "דמי שכירות מרכז")
    assert T.STATUS_UNCERTAIN in other
    result = json.loads(T.tool_calculate(ws, "V1 + V4", "סכום", "סכום דמי השכירות של שני האזורים, כפי שהתבקש"))
    assert result["conditional"] is True and "V1" in result["note"]


def test_a_clear_re_read_settles_the_value(vision_office, monkeypatch):
    ws = emp(vision_office)
    sid = _uncertain_table(vision_office, monkeypatch, ws)
    _ocr_sees_the_table(monkeypatch)
    out = _take(ws, sid)
    assert len(vision_office.vision.calls) == 1 and T.STATUS_AUTO in out
    assert "V1" in ws.settled_values and "V1" not in ws.uncertain_values
    assert T.uncertain_inputs(ws, ["V1"]) == []


# --- partial labels -------------------------------------------------------------------------------------------------------

def test_a_hit_on_a_page_without_unread_regions_in_a_partly_read_document_is_not_partial(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = make_document(a, a.default_group_id, TITLE)
    info = pipeline.VersionInfo(ver, doc, "k", "application/pdf", None)
    texts = ["תיאור הנכס: דירת ארבעה חדרים בקומה שלישית ברחוב הדוגמה 5.",
             "נספח תשריט: מפת הסביבה של חלקת הדוגמה."]
    result = ExtractionResult(2, [PageResult(1, texts[0], "text_layer", 1.0, True),
                                  PageResult(2, texts[1], "text_layer", 1.0, True)], [],
                              [ChunkResult(0, "text", [1], None, texts[0]), ChunkResult(1, "text", [2], None, texts[1])])
    with tenant_tx(a.system) as conn:
        pipeline.persist_extraction(conn, info, result)
    pipeline.embed_stage(a.system, info, 1e18)
    coverage = [{"page": 2, "ok": True, "method": None, "corrected": 0,
                 "regions": [{"block": 1, "kind": "image", "status": "unread", "reason": "תמונה ללא טקסט קריא",
                              "bbox": None, "section": None, "media": None}]}]
    with tenant_tx(a.system) as conn:
        conn.execute(text("UPDATE document_versions SET ingestion = COALESCE(ingestion, '{}'::jsonb)"
                          " || CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": ver, "i": json.dumps({"partial": True, "coverage": coverage}, ensure_ascii=False)})
    ws = T.Workspace(ctx=a.ctx())
    T.tool_search(ws, "דירת ארבעה חדרים", [str(doc)])
    hit = next(s for s in ws.sources.values() if s.page_list == [1])
    assert hit.partial_document is False and "נקרא חלקית" not in T._render_source(hit)
    assert ws.activity[str(doc)]["partial"] is True  # a datum not found may still be in the unread part
    T.tool_search(ws, "נספח תשריט מפת הסביבה", [str(doc)])
    assert next(s for s in ws.sources.values() if s.page_list == [2]).partial_document is True


def test_without_ocr_an_unclear_value_is_not_re_read_and_stays_uncertain(vision_office, monkeypatch):
    ws = emp(vision_office)
    sid = _uncertain_table(vision_office, monkeypatch, ws)  # ingested with OCR off, which stays off
    out = _take(ws, sid)
    assert vision_office.vision.calls == [] and T.STATUS_UNCERTAIN in out and "אין OCR" in out
    assert "V1" in ws.uncertain_values



# --- a table read by inspect: values confirmed by OCR in their cells, in the same turn (U5, KTD6, R16–R19) ---------

def _cell(row: str, column: str, number: str | None = None) -> dict:
    return {"table": None, "row": row, "row_number": None, "column": column, "column_number": None, "quote": None,
            "number": number}


def _anchor(ws, vid: str) -> dict:
    from app.chat import anchors

    answer = {"values": [{"id": vid}]}
    anchors.attach(ws, answer)
    return answer["values"][0]["anchor"]


def test_ae5_two_ocr_confirmed_cells_of_a_table_read_by_inspect_give_a_certain_total_in_the_same_turn(
        client, vision_office, monkeypatch):
    from tests.conftest import login
    from tests.integration.test_chat import cloud, new_conversation, send
    from tests.support.scripted_agent import ScriptedAgent, call, final, read

    doc = ingest_cost_table(vision_office, monkeypatch)
    vision_office.vision = CostTable()
    cost_ocr(monkeypatch)
    q = COST["question_total"]

    def last(items) -> str:
        return [i["output"] for i in items if isinstance(i, dict) and i.get("type") == "function_call_output"][-1]

    def inspect_region(items):
        region = re.search(r"\[אזור שלא נקרא (R\d+)", last(items)).group(1)
        return [call("inspect", target={"region": region, "document": None, "page": None})]

    def take_both(items):
        sid = re.findall(r'<source id="(S\d+)"', last(items))[-1]
        return [call("take_value", source=sid, locator=_cell(row, COST_TOTAL, number), meaning=cost_meaning(),
                     label=row) for row, number in zip(q["rows"], q["inputs"], strict=True)]

    answer = final(f"עלות הבנייה העילית והחניון התת-קרקעי יחד היא {q['result']} ₪ [C1].", documents=[doc])
    agent = ScriptedAgent([[read(pages={"document": doc, "from_page": 1, "to_page": 1})], inspect_region, take_both,
                           [call("calculate", expression="V1 + V2", label="עלות הבנייה והחניון", justification=None)],
                           answer])
    cloud(monkeypatch, vision_office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה עלות הבנייה העילית והחניון יחד?")
    assert m["status"] == "done", m
    a = m["answer"]
    assert a["status"] == "answered" and a["verification"]["removed"] == 0, a
    assert q["result"] in a["markdown"] and vision_office.vision.calls == 1
    (c,) = a["computations"]
    assert c["value"] == "20220000" and c["conditional"] is False, c
    values = {v["id"]: v for v in a["values"]}
    assert set(values) == {"V1", "V2"}
    for v, row in zip((values["V1"], values["V2"]), q["rows"], strict=True):
        assert v["certainty"] == "verified" and v["reading"] == "clear", v
        anchor = v["anchor"]  # the input opens the table, at its cell, with the table's context
        assert anchor["precision"] == "cell" and anchor["table"]["row_label"] == row, anchor
        assert anchor["table"]["column_header"] == COST_TOTAL and anchor["table"]["source"] == "vision"
        assert "table_index" not in v["locator"]


def test_a_conditional_total_shown_without_a_hedge_is_kept_with_the_servers_conditional_qualifier(
        client, vision_office, monkeypatch):
    """R7c in the browser: OCR does not confirm either cell, so C1 = V1 + V2 is conditional; the model writes the
    total without hedging it. The judge (scripted as the real one ruled) fails such a unit unless the server's
    qualifier is shown with it; the server writes that qualifier, so the total stays in the answer as conditional —
    never removed, never a gap, never shown as certain."""
    from tests.conftest import login
    from tests.integration.test_chat import cloud, new_conversation, send
    from tests.support.scripted_agent import ScriptedAgent, call, component, final, read, requirement

    doc = ingest_cost_table(vision_office, monkeypatch)
    vision_office.vision = CostTable()
    q = COST["question_total"]
    cost_ocr(monkeypatch, drop=tuple(q["inputs"]))  # neither number confirmed in its cell

    def last(items) -> str:
        return [i["output"] for i in items if isinstance(i, dict) and i.get("type") == "function_call_output"][-1]

    def inspect_region(items):
        region = re.search(r"\[אזור שלא נקרא (R\d+)", last(items)).group(1)
        return [call("inspect", target={"region": region, "document": None, "page": None})]

    def take_both(items):
        sid = re.findall(r'<source id="(S\d+)"', last(items))[-1]
        return [call("take_value", source=sid, locator=_cell(row, COST_TOTAL, number), meaning=cost_meaning(),
                     label=row) for row, number in zip(q["rows"], q["inputs"], strict=True)]

    judged: list[str] = []

    def judge(input: str) -> dict:
        judged.append(input)
        units = {int(i): cites.split(",") for i, cites in re.findall(r'<unit index="(\d+)" cites="([^"]*)">', input)}
        qualified = {int(i) for i in re.findall(r'<server_qualifier unit="(\d+)">', input)}
        # a conditional result shown as certain fails (uncertain_reading), unless the server's qualifier goes with it
        held = {i: i in qualified or "C1" not in cites for i, cites in units.items()}
        verdicts = [{"index": i, "verdict": "supported" if ok else "unsupported",
                     "reason": "בדיקה" if ok else "תוצאה מותנית שהוצגה כוודאית", "supported_by": [],
                     "failure": "none" if ok else "uncertain_reading"} for i, ok in held.items()]
        return {"verdicts": verdicts, "requirements": [
            requirement(id=r, units=list(units), related=["C1"])
            for r in re.findall(r'<requirement id="([A-Z][\d.]+)"', input)]}

    answer = final(f"עלות הבנייה העילית והחניון התת-קרקעי יחד היא {q['result']} ₪ [C1].", documents=[doc])
    agent = ScriptedAgent([[read(pages={"document": doc, "from_page": 1, "to_page": 1})], inspect_region, take_both,
                           [call("calculate", expression="V1 + V2", label="עלות הבנייה והחניון", justification=None)],
                           answer], judge=judge,
                          request=[component("עלות הבנייה העילית והחניון יחד", kind="calculation")])
    cloud(monkeypatch, vision_office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה עלות הבנייה העילית והחניון יחד?")
    assert m["status"] == "done", m
    a = m["answer"]
    (c,) = a["computations"]
    assert c["value"] == "20220000" and c["conditional"] is True, c
    # the total is in the answer, with the server's conditional qualifier next to it
    assert (f"{q['result']} ₪ (תוצאה מותנית: הערכים V1, V2 אינם ודאיים) [C1]" in a["markdown"]), a["markdown"]
    assert "החישוב לא הושלם" not in a["markdown"] and "כפי שנכתב במקור" not in a["markdown"]
    v = a["verification"]
    assert v["removed"] == 0 and v["removals"] == [] and v["conditional"] == 1, v
    assert v["correctness"] == "partial"  # shown as conditional, never as verified certainty
    (n1,) = [x for x in a["components"] if x["kind"] == "calculation"]
    assert n1["status"] != "not_answered", n1
    # one judged answer: no repair round for the qualifier the server writes itself
    assert len([s for s in agent.seen]) == 5 and any('<server_qualifier unit="0">' in i for i in judged)


def test_a_number_ocr_does_not_see_stays_uncertain_and_its_calculation_is_conditional_and_says_why(
        vision_office, monkeypatch):
    doc = ingest_cost_table(vision_office, monkeypatch)
    vision_office.vision = CostTable()
    cost_ocr(monkeypatch, drop=("4,620,000",))
    ws = emp(vision_office)
    sid = inspect_cost_table(vision_office, ws, doc)
    first, second = take_cost(ws, sid, "בנייה עילית"), take_cost(ws, sid, "חניון תת-קרקעי")
    assert T.STATUS_AUTO in first and T.STATUS_UNCERTAIN in second and "OCR" in second
    out = json.loads(T.tool_calculate(ws, "V1 + V2", "סכום"))
    assert out["conditional"] is True and "V2" in out["note"] and "V1" not in T.uncertain_inputs(ws, ["V1"])
    assert ws.uncertain_values["V2"] in out["note"]  # why it is uncertain, not only that it is
    assert vision_office.vision.calls == 1  # the same crop is not read again: nothing new would confirm it


def test_a_number_seen_twice_in_the_crop_stays_uncertain_and_is_highlighted_as_its_table(vision_office, monkeypatch):
    from app.chat import anchors

    doc = ingest_cost_table(vision_office, monkeypatch)
    vision_office.vision = CostTable()
    cost_ocr(monkeypatch, twice=("15,600,000",))
    ws = emp(vision_office)
    sid = inspect_cost_table(vision_office, ws, doc)
    out = take_cost(ws, sid, "בנייה עילית")
    assert T.STATUS_UNCERTAIN in out and "יותר מפעם אחת" in out
    snap = _anchor(ws, "V1")
    assert (snap["precision"], snap["region"], snap["degraded"]) == ("region", "table", anchors.NO_CELL_BOX), snap
    assert snap["table"]["row_label"] == "בנייה עילית" and snap["table"]["column_header"] == COST_TOTAL


def test_values_the_transcription_swapped_between_rows_both_stay_uncertain_and_their_sum_is_conditional(
        vision_office, monkeypatch):
    rows = [list(r) for r in PICTURE["rows"]]
    rows[0][3], rows[1][3] = rows[1][3], rows[0][3]  # the model put each row's cost in the other row
    doc = ingest_cost_table(vision_office, monkeypatch)
    vision_office.vision = CostTable(rows)
    cost_ocr(monkeypatch)  # OCR sees the table as drawn: both numbers, each in its own row
    ws = emp(vision_office)
    sid = inspect_cost_table(vision_office, ws, doc)
    outs = [take_cost(ws, sid, "בנייה עילית"), take_cost(ws, sid, "חניון תת-קרקעי")]
    assert all(T.STATUS_UNCERTAIN in o for o in outs), outs
    out = json.loads(T.tool_calculate(ws, "V1 + V2", "סכום"))
    assert out["conditional"] is True and "V1" in out["note"] and "V2" in out["note"]


def _garbled_cost_ocr(monkeypatch) -> None:
    """OCR of the crop as Tesseract read the real one (U9): every header and label word junk, the numbers exact and
    each on its row's line."""
    from tests.integration.test_chat_inspect import script_ocr, table_words

    letters = re.compile(r"[א-ת]")
    script_ocr(monkeypatch, lambda gray: [w | {"text": "nn" + str(k)} if letters.search(w["text"]) else w
                                          for k, w in enumerate(table_words(gray.size, PICTURE["headers"],
                                                                            PICTURE["rows"]))])


def test_cells_whose_headers_and_labels_ocr_garbles_are_confirmed_by_their_numbers_and_sum_unconditionally(
        vision_office, monkeypatch):
    doc = ingest_cost_table(vision_office, monkeypatch)
    vision_office.vision = CostTable()
    _garbled_cost_ocr(monkeypatch)
    ws = emp(vision_office)
    sid = inspect_cost_table(vision_office, ws, doc)
    outs = [take_cost(ws, sid, "בנייה עילית"), take_cost(ws, sid, "חניון תת-קרקעי")]
    assert all(T.STATUS_AUTO in o for o in outs), outs
    out = json.loads(T.tool_calculate(ws, "V1 + V2", "סכום"))
    assert out["conditional"] is False and out["value"] == COST["question_total"]["result"].replace(",", "")
    assert ws.anchors["V1"]["vision"]["cell_box"] is not None  # highlighted at its cell


def test_a_reading_stored_before_the_grid_placement_is_placed_from_its_stored_number_boxes(vision_office,
                                                                                            monkeypatch):
    doc = ingest_cost_table(vision_office, monkeypatch)
    vision_office.vision = CostTable()
    _garbled_cost_ocr(monkeypatch)
    inspect_cost_table(vision_office, emp(vision_office), doc)
    with tenant_tx(vision_office.system) as conn:  # as inspect-v3 stored it: label placement only, every cell unplaced
        reading = conn.execute(text("SELECT reading FROM region_readings")).scalar_one()
        ocr = reading["ocr"]
        ocr.pop("placement", None)
        ocr["cells"] = [[[c if c is None else {"status": "not_placed", "box": None, "by": None} for c in r] for r in t]
                        for t in ocr["cells"]]
        conn.execute(text("UPDATE region_readings SET reading = CAST(:r AS jsonb)"), {"r": json.dumps(reading)})
    ws = emp(vision_office)
    sid = inspect_cost_table(vision_office, ws, doc)
    assert T.STATUS_AUTO in take_cost(ws, sid, "בנייה עילית")
    assert vision_office.vision.calls == 1  # the stored reading served: nothing was read again


def test_a_quoted_row_of_a_table_read_by_inspect_is_taken_as_its_cell(vision_office, monkeypatch):
    doc = ingest_cost_table(vision_office, monkeypatch)
    vision_office.vision = CostTable()
    cost_ocr(monkeypatch)
    ws = emp(vision_office)
    sid = inspect_cost_table(vision_office, ws, doc)
    out = T.tool_take_value(ws, sid, {"quote": " | ".join(PICTURE["rows"][0]), "number": "15,600,000"},
                            cost_meaning(), "בנייה עילית")
    assert out.startswith("V1 נרשם") and T.STATUS_AUTO in out, out
    where = ws.values["V1"].locator
    assert (where["row"], where["column"]) == ("בנייה עילית", COST_TOTAL)
