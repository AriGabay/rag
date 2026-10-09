"""The calculator over verified values (U10): ``take_value``, ``assume`` and ``calculate``.

The model is scripted; these tests prove what the server checks and records: a value is registered only when the
number is the one at the named row and column (or inside the exact quote), with what the source attests about it;
an assumption only when the user wrote it; a calculation keeps full precision, chains, refuses what does not combine,
and labels a scenario; the answer that shows its results rounded verifies with nothing removed; and a value from a
document the user can no longer see cannot be used. Round 7 U7 (KTD8, KTD9, R23–R25, AE7, AE8): over the synthetic
round-7 residual report, a product of a rounded rate is reported beside the amount its section states, a user's rate
is the requested assumption, a report-stated scenario rate is computed without asking, and a rate nobody gave leads
to one clarification that keeps the found values, whose reply computes in the next turn without a new search.
Synthetic documents only; the project and streets are invented."""

from __future__ import annotations

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from app.chat import tools as T
from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_document, make_group, make_office, make_user
from tests.integration.test_chat import cloud, new_conversation, send
from tests.support.scripted_agent import ScriptedAgent, call, component, final, read, requirement

pytestmark = pytest.mark.db

TITLE = "בדיקת כדאיות פרויקט שדרות האלון 3"
CAPTION = "סיכום הכנסות ועלויות"
SECTION = "5. תחשיב הכדאיות"
INCOME, COST = "הכנסות (₪)", "עלויות (₪)"
ROWS = [["שלב א", "5,200,000", "4,300,000"], ["שלב ב", "7,250,000", "6,100,000"],
        ["סה\"כ", "12,450,000", "10,400,000"]]
PROSE = ("השווי למ\"ר אקוו' נקבע ל-9,500 ₪. שטח הדירה 120 מ\"ר אקוו'. השטח ברוטו 135 מ\"ר. "
         "הרווח היזמי הצפוי 2,050,000 ₪.")
QUESTION = "מה יהיה הרווח אם העלויות יעלו ב-5%, ומה שיעורו מההכנסות?"


def add_document(office, group=None, sha: str = "c" * 64) -> tuple[str, object]:
    doc, ver = make_document(office, group or office.default_group_id, TITLE, sha=sha)
    structure = {"headers": ["רכיב", INCOME, COST], "caption": CAPTION, "title": [],
                 "notes": ["הסכומים ללא מע\"מ."], "section": SECTION, "block_index": 2,
                 "rows": [{"cells": r} for r in ROWS]}
    blocks = [("heading", SECTION, None), ("paragraph", PROSE, None), ("table", CAPTION, 0)]
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET page_count = 1, ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": ver, "i": json.dumps({"reading_id": "reading-1"})})
        for i, (kind, t, table) in enumerate(blocks):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, page, text, status, table_index) VALUES (app_office(), :d, :v, :b, :k, :s, :sp, 1,"
                " :t, 'read', :ti)"),
                {"d": doc, "v": ver, "b": i, "k": kind, "s": SECTION, "sp": [SECTION], "t": t, "ti": table})
        conn.execute(text("INSERT INTO extracted_tables (office_id, document_id, version_id, table_index, page_start,"
                          " page_end, structure) VALUES (app_office(), :d, :v, 0, 1, 1, CAST(:s AS jsonb))"),
                     {"d": doc, "v": ver, "s": json.dumps(structure, ensure_ascii=False)})
    return str(doc), ver


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    a = make_office(db, "משרד א", "admin-a@example.test")
    a.private = make_group(a, "קבוצה סגורה")
    a.emp = make_user(a, "emp@example.test", [a.default_group_id, a.private])
    a.doc, a.ver = add_document(a)
    return a


def handle_of(output: str, name: str) -> str:
    m = re.search(r"([§T]\d+) «" + re.escape(name) + "»", output)
    assert m, output
    return m.group(1)


def _last_output(items) -> str:
    return [i["output"] for i in items if isinstance(i, dict) and i.get("type") == "function_call_output"][-1]


def _outputs(items) -> list[str]:
    return [i["output"] for i in items if isinstance(i, dict) and i.get("type") == "function_call_output"]


def cell(row: str, column: str, number: str | None = None) -> dict:
    return {"table": None, "row": row, "row_number": None, "column": column, "column_number": None, "quote": None,
            "number": number}


def quote(q: str, number: str) -> dict:
    return {"table": None, "row": None, "row_number": None, "column": None, "column_number": None, "quote": q,
            "number": number}


def meaning(kind: str, unit: str = "ILS", role: str = "other", *, vat: str = "unknown", basis: str = "",
            period: str = "none", subject: str = "פרויקט שדרות האלון") -> dict:
    return {"kind": kind, "unit": unit, "period": period, "vat": vat, "area_basis": basis, "subject": subject,
            "role": role}


def take(source: str, locator: dict, meaning_: dict, label: str) -> dict:
    return call("take_value", source=source, locator=locator, meaning=meaning_, label=label)


def read_table(ws, doc: str) -> str:
    """The synthetic table read through its outline handle; the source id it was returned as."""
    t = handle_of(T.tool_outline(ws, doc), CAPTION)
    out = T.tool_read(ws, {"table": t})
    return re.search(r'<source id="(S\d+)"', out).group(1)


def workspace(office, user=None, question: str = QUESTION) -> T.Workspace:
    ws = T.Workspace(ctx=office.ctx() if user is None else office.ctx("employee", user))
    ws.user_messages = [{"turn": 1, "text": question, "current": True}]
    return ws


def run(ws, name: str, **args) -> str:
    return T.run_tool(ws, name, json.dumps(args, ensure_ascii=False))


# --- AE4: a scenario on totals, end to end ----------------------------------------------------------------------

def _ae4_steps(doc: str, quote_text: str = "העלויות יעלו ב-5%") -> list:
    return [
        [call("outline", document=doc)],
        lambda items: [read(table=handle_of(_last_output(items), CAPTION))],
        [take("S1", cell("סה\"כ", INCOME), meaning("income", role="income", vat="excluded"), "סה״כ הכנסות"),
         take("S1", cell("סה\"כ", COST), meaning("cost", role="cost", vat="excluded"), "סה״כ עלויות"),
         call("assume", value="5%", quote=quote_text, label="עליית העלויות")],
        [call("calculate", expression="V1 - V2*(1+A1%)", label="הרווח בתרחיש", justification=None)],
        [call("calculate", expression="C1 / V1", label="שיעור הרווח מההכנסות", justification=None)],
    ]


ANSWER = ("לפי הנחתך שהעלויות יעלו ב-5% [A1]: ההכנסות בטבלה הן 12,450,000 ₪ [V1] והעלויות 10,400,000 ₪ [V2]. "
          "הרווח בתרחיש יהיה 1,530,000 ₪ [C1], שהם 12.3% מההכנסות [C2].")


def test_ae4_a_scenario_on_totals_is_verified_chained_at_full_precision_and_labelled(client, office, monkeypatch):
    agent = ScriptedAgent([*_ae4_steps(office.doc), final(ANSWER, documents=[office.doc])])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), QUESTION)
    assert m["status"] == "done", m
    outputs = agent.tool_outputs(5)
    assert outputs[2].startswith("V1 נרשם") and "שורת סה\"כ" in outputs[2]
    assert outputs[4].startswith("A1 נרשם") and "מההודעה הנוכחית" in outputs[4]
    a = m["answer"]
    assert a["status"] == "answered" and a["verification"]["removed"] == 0 and a["verification"]["partial"] == 0
    assert "12.3%" in a["markdown"] and "1,530,000" in a["markdown"]
    c1, c2 = sorted(a["computations"], key=lambda c: c["id"])
    assert c1["expression"] == "V1 − V2 × (1 + A1%)" and Decimal(c1["value"]) == Decimal("1530000")
    assert c1["formula"] == "«סה״כ הכנסות» − «סה״כ עלויות» × (1 + «עליית העלויות»%)"
    assert Decimal(c2["value"]) == Decimal(1530000) / Decimal(12450000)  # nothing rounded on the way
    assert c2["display"] == {"value": "0.1229", "percent": "12.29%"}
    assert c1["result_kind"] == c2["result_kind"] == "scenario" and c2["assumptions"] == ["A1"]
    assert {x["id"] for x in c2["inputs"]} == {"C1", "V1"}
    # the inputs open their sources; the assumption carries the user's words
    values = {v["id"]: v for v in a["values"]}
    assert set(values) == {"V1", "V2"} and values["V1"]["source_id"] == "S1"
    assert values["V1"]["provenance"]["unit"] == "source" and values["V1"]["provenance"]["vat"] == "source"
    assert values["V1"]["certainty"] == "verified" and values["V1"]["total"] is True
    assert values["V1"]["locator"]["row"] == "סה\"כ" and values["V1"]["locator"]["column"] == INCOME
    (assumption,) = a["assumptions"]
    assert assumption["quote"] == "העלויות יעלו ב-5%" and assumption["current"] is True
    assert any(s["id"] == "S1" for s in a["sources"])
    assert a["documents"] == [{"document_id": office.doc, "title": TITLE}]


def test_an_assumption_from_the_first_turn_is_used_in_the_second_without_asking_again(client, office, monkeypatch):
    first = ScriptedAgent([*_ae4_steps(office.doc)[:2],
                           [take("S1", cell("סה\"כ", INCOME), meaning("income", role="income"), "סה״כ הכנסות")],
                           final("ההכנסות הכוללות הן 12,450,000 ₪ [V1].", documents=[office.doc])])
    cloud(monkeypatch, office, first)
    login(client, "admin-a@example.test")
    cid = new_conversation(client)
    assert send(client, cid, "העלויות בפרויקט יעלו ב-5%. מה ההכנסות הכוללות?")["status"] == "done"
    steps = _ae4_steps(office.doc, quote_text="העלויות בפרויקט יעלו ב-5%")[:4]
    second = ScriptedAgent([*steps, final("לפי הנחתך שהעלויות יעלו ב-5% [A1], הרווח יהיה 1,530,000 ₪ [C1].",
                                          documents=[office.doc])])
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: second)
    m = send(client, cid, "ומה יהיה הרווח בתרחיש הזה?")
    a = m["answer"]
    assert a["status"] == "answered" and a["verification"]["removed"] == 0, a
    (assumption,) = a["assumptions"]
    assert assumption["turn"] == 1 and assumption["current"] is False
    assert a["computations"][0]["result_kind"] == "scenario"


# --- take_value: the number must be the one at the named place ------------------------------------------------

def test_take_value_refuses_a_number_that_is_in_another_row_of_the_table(office):
    ws = workspace(office)
    s = read_table(ws, office.doc)
    out = run(ws, "take_value", source=s, locator=cell("שלב א", INCOME, number="12,450,000"),
              meaning=meaning("income", role="income"), label="הכנסות")
    assert out.startswith("שגיאה") and "שורה «סה\"כ»" in out and "לא ב" in out and not ws.values
    # the right row is accepted, and the number may be written with ₪ and a no-break space
    ok = run(ws, "take_value", source=s, locator=cell("סה\"כ", INCOME, number="₪ 12,450,000"),
             meaning=meaning("income", role="income"), label="הכנסות")
    assert ok.startswith("V1 נרשם") and ws.values["V1"].value == Decimal("12450000")
    assert ws.values["V1"].vat == "excluded"  # the table's note, filled in from the source


def test_take_value_from_a_quote_needs_the_exact_quote_with_the_number_inside_it(office):
    ws = workspace(office)
    sec = handle_of(T.tool_outline(ws, office.doc), SECTION)
    s = re.search(r'<source id="(S\d+)"', T.tool_read(ws, {"section": sec})).group(1)
    loose = run(ws, "take_value", source=s, locator=quote("השווי למ\"ר נקבע ל-9,500 ₪", "9,500"),
                meaning=meaning("value_per_area", "ILS_per_sqm"), label="שווי למ״ר")
    assert loose.startswith("שגיאה") and "כלשונו" in loose
    outside = run(ws, "take_value", source=s, locator=quote("שטח הדירה 120 מ\"ר", "9,500"),
                  meaning=meaning("value_per_area", "ILS_per_sqm"), label="שווי למ״ר")
    assert outside.startswith("שגיאה") and "לא בתוך הציטוט" in outside
    contradicted = run(ws, "take_value", source=s, locator=quote("השטח ברוטו 135 מ\"ר", "135"),
                       meaning=meaning("area", "sqm", basis="נטו"), label="שטח")
    assert contradicted.startswith("שגיאה") and "ברוטו" in contradicted
    ok = run(ws, "take_value", source=s, locator=quote("השווי למ\"ר אקוו' נקבע ל-9,500 ₪", "9,500"),
             meaning=meaning("value_per_area", "ILS_per_sqm"), label="שווי למ״ר")
    assert ok.startswith("V1 נרשם")
    v = ws.values["V1"]
    assert v.area_basis == "מ״ר אקוו׳" and v.provenance["area_basis"] == "source" and v.certainty == "verified"


# --- calculate: compatibility depends on the operation ----------------------------------------------------------

def _section_values(ws, office) -> str:
    sec = handle_of(T.tool_outline(ws, office.doc), SECTION)
    s = re.search(r'<source id="(S\d+)"', T.tool_read(ws, {"section": sec})).group(1)
    for q, n, m, label in [
        ("השווי למ\"ר אקוו' נקבע ל-9,500 ₪", "9,500", meaning("value_per_area", "ILS_per_sqm"), "שווי למ״ר"),
        ("שטח הדירה 120 מ\"ר אקוו'", "120", meaning("area", "sqm"), "שטח הדירה"),
        ("השטח ברוטו 135 מ\"ר", "135", meaning("area", "sqm"), "שטח ברוטו")]:
        assert run(ws, "take_value", source=s, locator=quote(q, n), meaning=m, label=label).startswith("V")
    return s


def test_a_per_area_value_times_an_area_is_a_total_and_mixed_bases_need_a_justification(office):
    ws = workspace(office)
    _section_values(ws, office)  # V1 9,500 ₪ למ״ר אקוו׳, V2 120 מ״ר אקוו׳, V3 135 מ״ר ברוטו
    total = json.loads(T.tool_calculate(ws, "V1 * V2", "שווי הדירה"))
    assert Decimal(total["value"]) == Decimal("1140000") and total["unit"] == "₪" and total["conditional"] is False
    refused = run(ws, "calculate", expression="V1 * V3", label="שווי", justification=None)
    assert refused.startswith("שגיאה: אי אפשר לחשב") and "בסיס שטח" in refused and "C2" not in ws.computations
    conditional = json.loads(T.tool_calculate(ws, "V1 * V3", "שווי לפי שטח ברוטו", "המשתמש ביקש לפי השטח ברוטו"))
    assert conditional["conditional"] is True and "המשתמש ביקש לפי השטח ברוטו" in conditional["note"]
    assert ws.computations[conditional["id"]].public()["conditions"]


def test_income_minus_cost_is_accepted_and_reproduces_the_value_the_report_writes(office):
    ws = workspace(office)
    _section_values(ws, office)  # S1: the section, which writes the developer's profit
    s = read_table(ws, office.doc)
    for row_label, column, kind in (("סה\"כ", INCOME, "income"), ("סה\"כ", COST, "cost")):
        assert run(ws, "take_value", source=s, locator=cell(row_label, column),
                   meaning=meaning(kind, role=kind), label=kind).startswith("V")
    out = json.loads(T.tool_calculate(ws, "V4 - V5", "רווח יזמי"))
    assert Decimal(out["value"]) == Decimal("2050000") and out["result_kind"] == "משחזר ערך מהשומה"
    c = ws.computations[out["id"]]
    assert c.result_kind == "reproduces_report_value" and c.reproduces == {"source": "S1", "as_written": "2,050,000"}


def test_ae5_a_total_with_its_own_components_and_rent_with_value_are_refused(office):
    ws = workspace(office)
    s = read_table(ws, office.doc)
    for row_label in ("שלב א", "שלב ב", "סה\"כ"):
        role = "total" if row_label == "סה\"כ" else "component"
        run(ws, "take_value", source=s, locator=cell(row_label, INCOME), meaning=meaning("income", role=role),
            label=f"הכנסות {row_label}")
    out = run(ws, "calculate", expression="sum(V1, V2, V3)", label="סכום", justification="בדיקה")
    assert out.startswith("שגיאה: אי אפשר לחשב") and "סה״כ" in out and "הרכיבים" in out
    assert json.loads(T.tool_calculate(ws, "V1 + V2", "סכום השלבים"))["value"] == "12450000"
    sec = handle_of(T.tool_outline(ws, office.doc), SECTION)
    p = re.search(r'<source id="(S\d+)"', T.tool_read(ws, {"section": sec})).group(1)
    run(ws, "take_value", source=p, locator=quote("השווי למ\"ר אקוו' נקבע ל-9,500 ₪", "9,500"),
        meaning=meaning("value_per_area", "ILS_per_sqm"), label="שווי למ״ר")
    run(ws, "take_value", source=p, locator=quote("הרווח היזמי הצפוי 2,050,000 ₪", "2,050,000"),
        meaning=meaning("rent", period="month"), label="דמי שכירות (בדיקה)")
    mixed = run(ws, "calculate", expression="V5 + V4", label="סכום", justification="בדיקה")
    assert mixed.startswith("שגיאה: אי אפשר לחשב")


# --- assume: only what the user wrote ---------------------------------------------------------------------------

def test_assume_refuses_a_value_the_user_did_not_write_and_asks_to_ask_the_user(office):
    ws = workspace(office, question="מה יהיה הרווח אם העלויות יעלו?")
    ws.user_messages.insert(0, {"turn": 1, "text": "שלום", "current": False})
    out = run(ws, "assume", value="5%", quote="העלויות יעלו ב-5%", label="עליית העלויות")
    assert out.startswith("שגיאה") and "שאל את המשתמש" in out and not ws.assumptions
    out = run(ws, "assume", value="5%", quote="העלויות יעלו", label="עליית העלויות")
    assert out.startswith("שגיאה") and "שאל את המשתמש" in out and "אינו בתוך הציטוט" in out
    # a number from an answer or a document is not the user's: only user messages are searched
    ws.user_messages.append({"turn": 3, "text": "תודה", "current": False})
    assert run(ws, "assume", value="12,450,000", quote="12,450,000", label="הכנסות").startswith("שגיאה")


# --- permissions --------------------------------------------------------------------------------------------------

def test_a_value_from_a_document_the_user_can_no_longer_see_cannot_be_used(office):
    office.doc, office.ver = add_document(office, office.private, sha="d" * 64)
    ws = workspace(office, office.emp)
    s = read_table(ws, office.doc)
    assert run(ws, "take_value", source=s, locator=cell("סה\"כ", INCOME),
               meaning=meaning("income", role="income"), label="הכנסות").startswith("V1")
    assert json.loads(T.tool_calculate(ws, "V1 * 12", "בדיקה"))["id"] == "C1"
    with tenant_tx(office.ctx()) as conn:
        conn.execute(text("DELETE FROM user_groups WHERE user_id = :u AND group_id = :g"),
                     {"u": office.emp, "g": office.private})
    out = run(ws, "calculate", expression="V1 + V1", label="בדיקה", justification=None)
    assert out.startswith("שגיאה") and "אינו זמין" in out and TITLE not in out
    # through an earlier result as well
    assert "אינו זמין" in run(ws, "calculate", expression="C1 / 12", label="בדיקה", justification=None)
    assert "C2" not in ws.computations


# --- the step bound fits a calculation flow ---------------------------------------------------------------------

def _sequential_flow(doc: str) -> list:
    """A calculation reached one tool per step, as a cautious model does: the scope, the stored measurements, a
    search, the outline, the table, each value, the assumption and both calculations — ten steps before the answer."""
    table: dict = {}

    def source(items) -> str:
        if "id" not in table:  # the table read is the step before the first value
            table["id"] = re.search(r'<source id="(S\d+)"', _last_output(items)).group(1)
        return table["id"]

    return [
        [call("find_documents", query="שדרות האלון", page=None)],
        [call("find_measurements", query="רווח יזמי", metric_kinds=None, document_ids=None, value_roles=None,
              page=None)],
        [call("search", query="הכנסות ועלויות", document_ids=[doc], limit=None)],
        [call("outline", document=doc)],
        lambda items: [read(table=handle_of(_last_output(items), CAPTION))],
        lambda items: [take(source(items), cell("סה\"כ", INCOME), meaning("income", role="income", vat="excluded"),
                            "סה״כ הכנסות")],
        lambda items: [take(source(items), cell("סה\"כ", COST), meaning("cost", role="cost", vat="excluded"),
                            "סה״כ עלויות")],
        [call("assume", value="5%", quote="העלויות יעלו ב-5%", label="עליית העלויות")],
        [call("calculate", expression="V1 - V2*(1+A1%)", label="הרווח בתרחיש", justification=None)],
        [call("calculate", expression="C1 / V1", label="שיעור הרווח מההכנסות", justification=None)],
    ]


def test_a_calculation_reached_in_ten_sequential_tool_steps_completes_within_the_default_bound(
        client, office, monkeypatch):
    agent = ScriptedAgent([*_sequential_flow(office.doc), final(ANSWER, documents=[office.doc])])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), QUESTION)
    assert m["status"] == "done", (m.get("error"), len(agent.seen))
    a = m["answer"]
    assert len(agent.seen) == 11 and not agent.steps  # every scripted step ran, the answer at the eleventh
    assert a["limits_hit"] == [] and "מספר הצעדים המרבי" not in a["markdown"]
    assert a["status"] == "answered" and a["verification"]["removed"] == 0
    assert sorted(c["id"] for c in a["computations"]) == ["C1", "C2"] and "12.3%" in a["markdown"]
    assert a["steps"] == 11


def test_parallel_tool_calls_of_one_step_all_run_and_count_as_one_step(client, office, monkeypatch):
    # a bound of seven model steps (two kept for repairs): AE4 takes six, its third step calling three tools; counted
    # per call it would take eight and reach the bound
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_max_steps", 9)
    agent = ScriptedAgent([*_ae4_steps(office.doc), final(ANSWER, documents=[office.doc])])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), QUESTION)
    assert m["status"] == "done", m
    # the step after the parallel one sees all three outputs, each answering its own call
    third = [i for i in agent.seen[3] if isinstance(i, dict) and i.get("type") == "function_call_output"][-3:]
    assert [i["call_id"] for i in third] == ["call_2_0", "call_2_1", "call_2_2"]
    assert [i["output"][:2] for i in third] == ["V1", "V2", "A1"] and all("נרשם" in i["output"] for i in third)
    assert len(agent.seen) == 6 and m["answer"]["limits_hit"] == [] and m["answer"]["status"] == "answered"
    assert m["answer"]["steps"] == 6


# --- U8: a computed result is verified as a computation -----------------------------------------------------------

SCENARIO_LEAD = "לפי הנחתך שהעלויות יעלו ב-5% [A1]. "


def test_a_computed_profit_cited_only_through_its_inputs_is_kept_and_cites_its_calculation(client, office,
                                                                                           monkeypatch):
    answer = SCENARIO_LEAD + "הרווח בתרחיש יהיה 1,530,000 ₪ [V1][V2]."
    agent = ScriptedAgent([*_ae4_steps(office.doc)[:4], final(answer, documents=[office.doc])])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), QUESTION)
    assert m["status"] == "done", m
    a = m["answer"]
    assert a["verification"]["removed"] == 0 and a["status"] == "answered", a
    assert "1,530,000 ₪ [V1][V2][C1]" in a["markdown"]
    assert [c["id"] for c in a["computations"]] == ["C1"] and a["computations"][0]["vat"] == "excluded"


@pytest.mark.parametrize("shown, kept", [
    ("הרווח בתרחיש יהיה 1.53 מיליון ₪, ללא מע״מ [C1].", True),
    ("הרווח בתרחיש יהיה 1.53 מיליון ₪ [V1][V2].", True),
    ("הרווח בתרחיש יהיה 1.6 מיליון ₪ [C1].", False),
    ("הרווח בתרחיש יהיה 1,530,000 ₪ כולל מע״מ [C1].", False),
    ("השומה מציינת רווח של 1,530,000 ₪ [V1][V2].", False),
])
def test_a_computed_result_is_kept_at_its_scale_and_vat_basis_and_removed_when_misstated_or_misattributed(
        client, office, monkeypatch, shown, kept):
    answer = final(SCENARIO_LEAD + shown, documents=[office.doc])
    # a misstated result stays misstated through the repair and the rewrite
    agent = ScriptedAgent([*_ae4_steps(office.doc)[:4], answer, answer, answer])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), QUESTION)
    assert m["status"] == "done", m
    a = m["answer"]
    if kept:
        assert a["verification"]["removed"] == 0 and "[C1]" in a["markdown"], a
    else:
        assert a["verification"]["removed"] == 1 and "1,530,000" not in a["markdown"] and "1.6" not in a["markdown"]
        assert "[C1]" not in a["markdown"]


def test_a_division_by_zero_on_found_values_is_reported_as_a_failed_calculation_not_missing_data(office):
    ws = workspace(office)
    s = read_table(ws, office.doc)
    for label in ("הכנסות", "הכנסות (שוב)"):
        assert run(ws, "take_value", source=s, locator=cell("סה\"כ", INCOME), meaning=meaning("income", role="income"),
                   label=label).startswith("V")
    assert run(ws, "take_value", source=s, locator=cell("סה\"כ", COST), meaning=meaning("cost", role="cost"),
               label="עלויות").startswith("V3")
    out = run(ws, "calculate", expression="V3 / (V1 - V2)", label="יחס", justification=None)
    assert out.startswith("שגיאה") and "חלוקה באפס" in out
    assert "החישוב נכשל" in out and "לא נתון חסר" in out and not ws.computations


# --- round 7 U7: a stated amount over a rounded rate, and a parameter nobody gave (KTD8, KTD9, R23–R25) -----------

ROUND7 = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "round7" / "manifest.json").read_text(
    encoding="utf-8"))["documents"]["residual"]
FACTS = ROUND7["facts"]
CALC_SECTION, CHECK_SECTION = FACTS["cost"]["section"], FACTS["threshold"]["section"]
SENSITIVITY = FACTS["sensitivity_rate"]["section"]
SUBJECT = "פרויקט ברחוב התאנה 30"
RISE_QUESTION = "מה יהיה הרווח היזמי אם עלויות הבנייה והפיתוח יעלו?"
RISE = component("הרווח היזמי אם עלויות הבנייה והפיתוח יעלו", "calculation", subject=SUBJECT,
                 parameters=[{"name": "שיעור העלייה של העלויות", "source": "not_given_by_user", "quote": ""}])


def add_residual(office, amount: str | None = None, sensitivity: bool = True, sha: str = "8" * 64) -> str:
    """R7d's sections as stored blocks (the synthetic round-7 residual report): the calculation section — income,
    cost, the developer's profit as "כ-17%" of cost and as an amount, the land value — the threshold and, when
    ``sensitivity``, the sensitivity section with its 6% cost increase. ``amount``: another stated profit amount."""
    calc_lines = [FACTS[k]["text"] for k in ("income", "cost", "profit_rate", "profit_amount", "land_value")]
    if amount is not None:
        calc_lines[3] = calc_lines[3].replace(FACTS["profit_amount"]["value"], amount)
    sections = [(CALC_SECTION, calc_lines), (CHECK_SECTION, [FACTS["threshold"]["text"]])]
    if sensitivity:
        sections.append((SENSITIVITY, [FACTS["sensitivity_rate"]["text"]]))
    doc, ver = make_document(office, office.default_group_id, "בדיקת כדאיות — רחוב התאנה 30 (סינתטי)", sha=sha)
    blocks = [(kind, section, t) for section, lines in sections
              for kind, t in [("heading", section), *(("paragraph", x) for x in lines)]]
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET page_count = 1, ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": ver, "i": json.dumps({"reading_id": "reading-r7"})})
        for i, (kind, section, t) in enumerate(blocks):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, page, text, status) VALUES (app_office(), :d, :v, :b, :k, :s, :sp, 1, :t, 'read')"),
                {"d": doc, "v": ver, "b": i, "k": kind, "s": section, "sp": [section], "t": t})
    return str(doc)


def rmeaning(kind: str, unit: str = "ILS", role: str = "other", scenario: str = "") -> dict:
    return {"kind": kind, "unit": unit, "period": "none", "vat": "unknown", "area_basis": "", "subject": SUBJECT,
            "role": role, "stated_by": "", "stance": "unknown", "scenario": scenario}


def fact(key: str, source: str, meaning_: dict, label: str, text_: str | None = None) -> dict:
    f = FACTS[key]
    return take(source, quote((text_ or f["text"]).rstrip("."), f["value"]), meaning_, label)


def _section_source(ws, doc: str, name: str) -> str:
    out = T.tool_read(ws, {"section": handle_of(T.tool_outline(ws, doc), name)})
    return re.search(r'<source id="(S\d+)"', out).group(1)


def _open(name: str):
    return lambda items: [read(section=handle_of(next(o for o in reversed(_outputs(items)) if "«" + name + "»" in o),
                                                 name))]


def test_ae7_cost_times_a_rounded_rate_reports_the_amount_the_section_states_and_keeps_it_in_the_record(office):
    doc = add_residual(office)
    ws = workspace(office, question="מה הפער בין הרווח היזמי לבין הרווח המינימלי הנדרש?")
    s = _section_source(ws, doc, CALC_SECTION)
    t = _section_source(ws, doc, CHECK_SECTION)
    for step in (fact("cost", s, rmeaning("cost", role="cost"), "סך העלויות"),
                 fact("profit_rate", s, rmeaning("rate", "percent", "rate"), "שיעור הרווח היזמי"),
                 fact("threshold", t, rmeaning("profit"), "הרווח המינימלי הנדרש")):
        assert run(ws, "take_value", **step["arguments"]).startswith("V"), step
    out = json.loads(T.tool_calculate(ws, "V1 * V2%", "הרווח היזמי"))
    assert out["display"]["value"] == ROUND7["gap_to_threshold"]["cost_times_rate"]
    near = out["explicit_amount_available"]
    # the amount the section states for the same quantity, within 16.5%–17.5% of the cost — never the land value,
    # which the interval holds too but which states another quantity
    assert near["amount"] == FACTS["profit_amount"]["value"] and near["source"] == s
    assert FACTS["profit_amount"]["text"].rstrip(".") in near["quote"] and near["rate"] == "V2"
    assert near["interval"] == ["16.5", "17.5"] and FACTS["land_value"]["value"] not in json.dumps(near)
    assert "take_value" in out["note"] and FACTS["profit_amount"]["value"] in out["note"]
    assert ws.computations["C1"].public()["explicit_amount_available"] == near  # kept in the calculation record
    # the gap built on it carries the stated amount; chained at full precision, rounded once for display
    gap = json.loads(T.tool_calculate(ws, "C1 - V3", "הפער לסף"))
    assert Decimal(gap["value"]) == Decimal("18350000") * Decimal("0.17") - Decimal("2800000")
    assert gap["display"]["value"] == ROUND7["gap_to_threshold"]["from_rounded_rate"]
    assert gap["explicit_amount_available"]["amount"] == near["amount"] and gap["explicit_amount_available"]["from"] == "C1"
    # nothing was replaced: both results are the computation the model asked for
    assert ws.computations["C1"].value == Decimal("3119500.00")


def test_a_stated_amount_outside_the_rounding_interval_is_no_near_miss_and_both_figures_stay(client, office,
                                                                                            monkeypatch):
    doc = add_residual(office, amount="3,400,000")  # 18.5% of the cost: not what "כ-17%" rounds from
    stated = FACTS["profit_amount"]["text"].replace(FACTS["profit_amount"]["value"], "3,400,000").rstrip(".")
    answer = ("לפי חישוב, 17% מהעלויות [V2] הם 3,119,500 ₪ [C1], ואילו התחשיב מציין סכום רווח יזמי של "
              "3,400,000 ₪ [V3]; בין השניים פער מהותי.")
    agent = ScriptedAgent([
        [call("outline", document=doc)], _open(CALC_SECTION),
        [fact("cost", "S1", rmeaning("cost", role="cost"), "סך העלויות"),
         fact("profit_rate", "S1", rmeaning("rate", "percent", "rate"), "שיעור הרווח היזמי"),
         take("S1", quote(stated, "3,400,000"), rmeaning("profit"), "סכום הרווח היזמי")],
        [call("calculate", expression="V1 * V2%", label="הרווח לפי השיעור", justification=None)],
        final(answer, documents=[doc])])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), "מה הרווח היזמי בתחשיב?")
    assert m["status"] == "done", m
    out = json.loads(agent.tool_outputs(4)[-1])
    assert out["explicit_amount_available"] is None
    assert out["stated_amount_differs"]["amount"] == "3,400,000"  # recorded, and the note says to show both
    a = m["answer"]
    assert a["verification"]["removed"] == 0, a
    assert "3,119,500" in a["markdown"] and "3,400,000" in a["markdown"]


USE_RATE = "חשב את הרווח היזמי לפי 17% מסך העלויות"


def test_a_rate_the_user_asks_for_is_used_as_the_requested_assumption_with_no_flag(client, office, monkeypatch):
    doc = add_residual(office)
    answer = "לפי הנחתך, 17% [A1] מסך העלויות 18,350,000 ₪ [V1] הם 3,119,500 ₪ [C1] (חישוב)."
    agent = ScriptedAgent([
        [call("outline", document=doc)], _open(CALC_SECTION),
        [fact("cost", "S1", rmeaning("cost", role="cost"), "סך העלויות"),
         call("assume", value="17%", quote="לפי 17% מסך העלויות", label="שיעור הרווח שביקשת")],
        [call("calculate", expression="V1 * A1%", label="הרווח לפי השיעור שביקשת", justification=None)],
        final(answer, documents=[doc])])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), USE_RATE)
    assert m["status"] == "done", m
    out = json.loads(agent.tool_outputs(4)[-1])
    assert out["explicit_amount_available"] is None and out["assumptions"] == ["A1"]
    a = m["answer"]
    assert a["verification"]["removed"] == 0 and a["status"] == "answered", a
    (c1,) = a["computations"]
    assert c1["result_kind"] == "scenario" and c1["explicit_amount_available"] is None
    (assumption,) = a["assumptions"]
    assert assumption["quote"] == "לפי 17% מסך העלויות"
    rounds = client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()["rounds"]
    assert not [p for r in rounds for p in r if p["kind"] == "input_choice"]


def _scripted_judge(missing_without: str = "[C", given: str = "full"):
    """A judge that supports every unit and scores each frozen component ``given`` (full) when a unit cites a
    calculation (``missing_without``), else missing, naming the values the turn found as related."""
    def respond(input: str) -> dict:
        units = {int(i): t for i, t in re.findall(r'<unit index="(\d+)" cites="([^"]*)"', input)}
        raw = dict(re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S))
        out = {"verdicts": [{"index": i, "verdict": "supported", "reason": "בדיקה", "supported_by": []} for i in units]}
        if "<requirements>" in input:
            computed = [i for i, cites in units.items() if "C" in cites or missing_without in raw.get(str(i), "")]
            out["requirements"] = [requirement(id=r, status=given if computed else "missing", units=computed,
                                               related=[] if computed else ["V1", "V2"])
                                   for r in re.findall(r'<requirement id="([A-Z][\d.]+)"', input)]
        return out

    return respond


def test_a_cost_increase_rate_the_report_states_is_computed_as_the_reports_scenario_and_nothing_is_asked(
        client, office, monkeypatch):
    doc = add_residual(office)
    answer = ("לפי תרחיש הרגישות בתחשיב, עלייה של 6% בעלויות [V3]: ההכנסות 24,600,000 ₪ [V1] והעלויות "
              "18,350,000 ₪ [V2], והרווח היזמי בתרחיש זה יהיה, לפי חישוב, 5,149,000 ₪ [C1].")
    agent = ScriptedAgent([
        [call("outline", document=doc)], _open(CALC_SECTION), _open(SENSITIVITY),
        [fact("income", "S1", rmeaning("income", role="income"), "ההכנסות"),
         fact("cost", "S1", rmeaning("cost", role="cost"), "העלויות"),
         fact("sensitivity_rate", "S2", rmeaning("rate", "percent", "rate", scenario="תרחיש הרגישות"),
              "עליית העלויות בתרחיש הרגישות")],
        [call("calculate", expression="V1 - V2 * (1 + V3%)", label="הרווח בתרחיש הרגישות", justification=None)],
        final(answer, documents=[doc])], judge=_scripted_judge(), request=[RISE])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), RISE_QUESTION)
    assert m["status"] == "done", m
    a = m["answer"]
    assert a["status"] == "answered" and a["verification"]["removed"] == 0, a
    assert a["computations"][0]["value"] == "5149000.00" and "5,149,000" in a["markdown"]
    assert [c["status"] for c in a["components"]] == ["full"] and a.get("pending") is None
    assert a["computations"][0]["explicit_amount_available"] is None


GAP_QUESTION = "בכמה עולה הרווח היזמי שבתחשיב על הסף המינימלי הנדרש? חשב."
# the analysis took the report's own figures for parameters of the user's (U9 evidence): document data, not a scenario
GAP = component("הפער בין הרווח היזמי שבתחשיב לבין הסף המינימלי הנדרש", "calculation", subject=SUBJECT,
                parameters=[{"name": "הרווח היזמי שבתחשיב", "source": "not_given_by_user", "quote": ""},
                            {"name": "הסף המינימלי הנדרש", "source": "not_given_by_user", "quote": ""}])


def test_a_computation_over_the_reports_figures_fills_parameters_that_were_document_data_and_nothing_is_asked(
        client, office, monkeypatch):
    doc = add_residual(office)
    right = ROUND7["gap_to_threshold"]["right"]
    answer = (f"סכום הרווח היזמי בתחשיב הוא {FACTS['profit_amount']['value']} ₪ [V1], הרווח המינימלי הנדרש "
              f"{FACTS['threshold']['value']} ₪ [V2], והפער ביניהם הוא {right} ₪ [C1].")
    agent = ScriptedAgent([
        [call("outline", document=doc)], _open(CALC_SECTION), _open(CHECK_SECTION),
        [fact("profit_amount", "S1", rmeaning("profit"), "סכום הרווח היזמי"),
         fact("threshold", "S2", rmeaning("profit"), "הרווח המינימלי הנדרש")],
        [call("calculate", expression="V1 - V2", label="הפער לסף", justification=None)],
        final(answer, documents=[doc]), final(answer, documents=[doc]), final(answer, documents=[doc])],
        judge=_scripted_judge(given="partial"), request=[GAP])  # the judge found the answer partial (U9 evidence)
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), GAP_QUESTION)
    assert m["status"] == "done", m
    a = m["answer"]
    assert a["verification"]["removed"] == 0 and right in a["markdown"] and a.get("pending") is None, a
    assert [c["status"] for c in a["components"]] == ["partial"], a["components"]
    # the report's figures were never a detail for the user: no gap says one is missing, and no repair round was told
    # to ask the user for one
    assert not [g for g in a.get("gaps") or [] if g["reason"] == "detail_missing"], a.get("gaps")
    rounds = client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()["rounds"]
    asked = [p for r in rounds for p in r if p["kind"] == "requirement" and "שהמשתמש לא נתן" in p["reason"]]
    assert not asked, asked


def test_a_rate_nobody_gave_is_refused_as_a_literal_and_its_assumed_result_repaired_into_a_clarification(
        client, office, monkeypatch):
    doc = add_residual(office, sensitivity=False)
    income, cost = FACTS["income"]["value"], FACTS["cost"]["value"]
    assumed = (f"ההכנסות {income} ₪ [V1] והעלויות {cost} ₪ [V2]. אם העלויות יעלו ב-12%, הרווח היזמי יהיה "
               "כ-4 מיליון ₪.")
    ask = (f"ההכנסות {income} ₪ [V1] והעלויות {cost} ₪ [V2]. הרווח בתרחיש הוא ההכנסות פחות העלויות אחרי העלייה. "
           "באיזה שיעור יעלו העלויות?")
    agent = ScriptedAgent([
        [call("outline", document=doc)], _open(CALC_SECTION),
        [fact("income", "S1", rmeaning("income", role="income"), "ההכנסות"),
         fact("cost", "S1", rmeaning("cost", role="cost"), "העלויות")],
        [call("calculate", expression="V1 - V2 * (1 + 12%)", label="הרווח בתרחיש", justification=None)],
        final(assumed, documents=[doc]),
        final(ask, status="clarification", clarification="באיזה שיעור יעלו העלויות?", documents=[doc])],
        judge=_scripted_judge(), request=[RISE])
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), RISE_QUESTION)
    assert m["status"] == "done", m
    refused = agent.tool_outputs(4)[-1]
    assert refused.startswith("שגיאה") and "קבוע" in refused and "שאל את המשתמש" in refused
    rounds = client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()["rounds"]
    (assumption,) = [p for p in rounds[0] if p["kind"] == "assumption"]
    assert assumption["failure_kind"] == "wrong_calculation" and assumption["check"] == "unrequested_assumption"
    a = m["answer"]
    assert a["status"] == "clarification" and "4 מיליון" not in a["markdown"]
    assert income in a["markdown"] and cost in a["markdown"] and "[V1]" in a["markdown"]  # found data and citations kept
    assert {v["id"] for v in a["values"]} == {"V1", "V2"}
    assert a["pending"]["parameters"] == ["שיעור העלייה של העלויות"] and a["missing"]


def test_ae8_a_cost_increase_without_a_rate_asks_once_and_the_reply_computes_without_searching_again(
        client, office, monkeypatch):
    doc = add_residual(office, sensitivity=False)
    income, cost = FACTS["income"]["value"], FACTS["cost"]["value"]
    found = f"ההכנסות הצפויות הן {income} ₪ [V1] והעלויות {cost} ₪ [V2]."
    ask = found + " באיזה שיעור לדעתך יעלו העלויות?"
    first = ScriptedAgent([
        [call("outline", document=doc)], _open(CALC_SECTION),
        [fact("income", "S1", rmeaning("income", role="income"), "ההכנסות"),
         fact("cost", "S1", rmeaning("cost", role="cost"), "העלויות")],
        final(found, documents=[doc]),  # the found values, and no question: the repair asks for the missing rate
        final(ask, status="clarification", clarification="באיזה שיעור יעלו העלויות?", documents=[doc])],
        judge=_scripted_judge(), request=[RISE])
    cloud(monkeypatch, office, first)
    login(client, "admin-a@example.test")
    cid = new_conversation(client)
    m = send(client, cid, RISE_QUESTION)
    assert m["status"] == "done", m
    a = m["answer"]
    repair = [i["content"] for i in first.seen[-1] if isinstance(i, dict) and i.get("role") == "user"][-1]
    assert "שאל" in repair and "שיעור העלייה של העלויות" in repair
    assert a["status"] == "clarification" and income in a["markdown"] and cost in a["markdown"]
    pending = a["pending"]
    assert pending["component"] == "N1" and pending["parameters"] == ["שיעור העלייה של העלויות"]
    assert [v["id"] for v in pending["found"]] == ["V1", "V2"] and pending["found"][0]["value_text"] == income

    def reopen(items):
        context = items[0]["content"]
        assert "שיעור העלייה של העלויות" in context and "assume" in context  # the reply is bound to the parameter
        return [read(source=re.search(r"(P\d+)", context.split("שיעור העלייה של העלויות", 1)[1]).group(1))]

    answer = (f"לפי הנחתך שהעלויות יעלו ב-8% [A1]: ההכנסות {income} ₪ [V1] והעלויות {cost} ₪ [V2], והרווח היזמי "
              "יהיה, לפי חישוב, 4,782,000 ₪ [C1].")
    second = ScriptedAgent([
        reopen,
        [fact("income", "S1", rmeaning("income", role="income"), "ההכנסות"),
         fact("cost", "S1", rmeaning("cost", role="cost"), "העלויות"),
         call("assume", value="8%", quote="8%", label="שיעור העלייה של העלויות")],
        [call("calculate", expression="V1 - V2 * (1 + A1%)", label="הרווח היזמי בתרחיש", justification=None)],
        final(answer, documents=[doc])], judge=_scripted_judge())
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: second)
    m2 = send(client, cid, "8%")
    assert m2["status"] == "done", m2
    b = m2["answer"]
    calls = [i["name"] for step in second.seen for i in step if isinstance(i, dict) and i.get("type") == "function_call"]
    assert "search" not in calls and "read" in calls  # the found values reopened, never searched again
    assert b["status"] == "answered" and b["verification"]["removed"] == 0, b
    assert b["computations"][0]["value"] == "4782000.00" and "4,782,000" in b["markdown"]
    (assumption,) = b["assumptions"]
    assert assumption["quote"] == "8%" and assumption["current"] is True
    assert [c["status"] for c in b["components"]] == ["full"]
