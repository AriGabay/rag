"""The calculator over verified values (U10): ``take_value``, ``assume`` and ``calculate``.

The model is scripted; these tests prove what the server checks and records: a value is registered only when the
number is the one at the named row and column (or inside the exact quote), with what the source attests about it;
an assumption only when the user wrote it; a calculation keeps full precision, chains, refuses what does not combine,
and labels a scenario; the answer that shows its results rounded verifies with nothing removed; and a value from a
document the user can no longer see cannot be used. Synthetic documents only; the project and streets are invented."""

from __future__ import annotations

import json
import re
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.chat import tools as T
from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_document, make_group, make_office, make_user
from tests.integration.test_chat import cloud, new_conversation, send
from tests.support.scripted_agent import ScriptedAgent, call, final, read

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
