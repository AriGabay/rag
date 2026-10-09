"""Round 7 reproductions (U1, R26): one test per failure F1–F8 of the plan
``docs/plans/2026-10-09-0908-fix-request-gaps-removals-in-turn-tables-plan.md``, each failing on the code it was
written against for the traced reason, and each marked ``xfail(strict=True)`` until the unit that fixes it removes
the marker (a fix then shows as an unexpected pass).

Each test asserts what the user sees or what is stored for the user — the answer's text and status, the stored
verification and diagnostics, a tool's output — never an internal name a later unit has yet to add. The model and
the judge are scripted: these tests prove what the server does with what they return, not their judgement. A
scripted judge plays the behaviour the trace found (for instance, a requirement derived from an instruction and
scored ``missing``); a script may also hold the steps a repair round would take once the fix sends the turn back to
the model (unused while the failure lasts). When a fix adds a model call (the request analysis of KTD1, say), the
unit that lands it scripts that call and keeps the asserted outcome: F1, F2, F3 and F8 script the components their
request has (U2); the others leave the analysis empty, so the judge derives the requirements as before.

F1–F4 run offline (the turn's engine with a scripted tool runner, no database); F2 and F5–F8 run against the test
database because their outcome rests on what the turn actually read (an opened section, an inspected region, a
section of one appraisal among two in a file). The documents are the synthetic round-7 fixtures
(``tests/fixtures/round7``, ``scripts/generate_fixtures.py --only round7``); every name, address, plan number,
block, parcel and amount is invented."""

from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from app.chat import api, calc, engine
from app.chat import tools as T
from app.db import TenantContext, tenant_tx
from app.extraction.images import VisionOut, VisionTableOut
from app.providers.llm import CallStatus
from tests.conftest import login
from tests.factories import make_document, make_office
from tests.integration.test_chat import cloud, new_conversation, send
from tests.support.scripted_agent import ScriptedAgent, call, component, final, read, requirement

ROUND7 = Path(__file__).resolve().parents[1] / "fixtures" / "round7"
MANIFEST = json.loads((ROUND7 / "manifest.json").read_text(encoding="utf-8"))
DOCS = MANIFEST["documents"]
# a sentence that says something is missing from the documents
GAP = re.compile(r"לא\s+(?:נמצא|נמצאה|נמצאו|נבדק|נבדקו|מופיע|מופיעים|אותר)")
SEARCH = [call("search", query="מצב תכנוני", document_ids=None, limit=None)]


def _fact(doc: str, key: str) -> dict:
    return DOCS[doc]["facts"][key]


def _units(input: str) -> dict[int, str]:
    return {int(i): t for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S)}


def _judge(asks: list[tuple[str, str | None, list[str]]], verdict=lambda text: "supported", absent: str = "missing"):
    """A judge that answers each unit by ``verdict(text)`` and scores each requirement ``(text, keyword, related)``:
    ``full`` with the units naming ``keyword`` (and not saying it is missing), else ``absent`` with no unit and the
    given ``related`` ids — what the trace found a judge does with an instruction or a whole category. Frozen
    requirements are scored by id (any prefix)."""
    def respond(input: str) -> dict:
        units = _units(input)
        frozen = {t: i for i, t in re.findall(r'<requirement id="([A-Z][\d.]+)"[^>]*>\n(.*?)\n</requirement>', input,
                                               re.S)}
        out = {"verdicts": [{"index": i, "verdict": verdict(t), "reason": "בדיקה", "supported_by": []}
                            for i, t in units.items()]}
        if "<derive_requirements>" in input or "<requirements>" in input:
            reqs = []
            for ask, keyword, related in asks:
                hits = [i for i, t in units.items() if keyword and keyword in t and not GAP.search(t)]
                reqs.append(requirement(text="" if frozen else ask, id=frozen.get(ask, ""),
                                        status="full" if hits else absent, units=hits,
                                        related=[] if hits else related))
            out["requirements"] = reqs
        return out

    return respond


# --- offline turns -------------------------------------------------------------------------------------------------

PLAN_TEXT = "\n".join(_fact("plan_status", k)["text"] for k in (
    "approved_plan", "approved_uses", "approved_units", "approved_floors", "proposed_plan", "proposed_units",
    "proposed_floors"))
PLAN_SECTION = _fact("plan_status", "approved_units")["section"]


def _ctx() -> TenantContext:
    return TenantContext(office_id=uuid.uuid4(), user_id=uuid.uuid4(), role="admin")


@pytest.fixture
def offline(monkeypatch):
    """The turn without a database: a search registers the synthetic passage (``offline.text``) as the section it
    comes from; ``take_value`` registers the number it names; ``calculate`` fails as the calculator does on inputs
    it refuses; the coverage ledger (which looks titles up) is empty."""
    doc, ver = uuid.uuid4(), uuid.uuid4()

    def run_tool(ws, name, arguments):
        args = json.loads(arguments or "{}")
        if name == "calculate":
            return f"שגיאה: אי אפשר לחשב: היחידות אינן מתאימות ({args.get('expression')}). {calc.MSG_FAILED}"
        if name == "take_value":
            loc, written = args["locator"], args["locator"]["number"]
            vid = f"V{len(ws.values) + 1}"
            ws.values[vid] = calc.Value(vid, Decimal(written.replace(",", "")), written, args["source"], doc, ver,
                                        None, "דוח בדיקה", "סעיף \"סיכום\"", args["label"], "other", "ILS", "none",
                                        "unknown", "", "נכס הדגמה", "other", {"unit": "source"},
                                        {"quote": loc["quote"]}, loc["quote"])
            return f"{vid} נרשם"
        src = ws.add_source(document_id=doc, version_id=ver, title="דוח בדיקה", section=run_tool.section,
                            location=f"סעיף \"{run_tool.section}\"", kind="context", text=run_tool.text)
        return f'<source id="{src.sid}">{src.text}</source>'

    run_tool.text, run_tool.section = PLAN_TEXT, PLAN_SECTION
    monkeypatch.setattr(T, "run_tool", run_tool)
    monkeypatch.setattr(engine.coverage, "build", lambda ws, answer, question: ({}, answer))
    return run_tool


def _turn(agent: ScriptedAgent, question: str) -> engine.TurnOutcome:
    inp = engine.TurnInput(question=question, history=[], summary=None, focus_documents=[], prior_refs={})
    return engine.run_turn(_ctx(), agent, inp, lambda *a: None, lambda: False)


def _prompts(agent: ScriptedAgent) -> list[str]:
    """Every user-role message the model was sent during the turn (the question, notices, repair prompts)."""
    return list(dict.fromkeys(i["content"] for step in agent.seen for i in step if isinstance(i, dict)
                              and i.get("role") == "user" and isinstance(i.get("content"), str)))


# F1 -----------------------------------------------------------------------------------------------------------------

F1_QUESTION = ("כתוב פרק מצב תכנוני למתחם שדרות הצבעוני: פרט את השימושים ואת מספר יחידות הדיור לפי התכנית המאושרת, "
               "ככל שהם מופיעים. כתוב בסגנון מקצועי, וצרף מראה מקום מדויק לכל נתון.")
STYLE, CITE = "כתיבה בסגנון מקצועי", "מראה מקום מדויק לכל נתון"
F1_ANSWER = ("לפי תכנית דמו/4521 המאושרת, ייעוד הקרקע מגורים, עם מסחר בקומת הקרקע [S1].\n"
             "לפי תכנית דמו/4521, מספר יחידות הדיור המותר הוא 84 יח״ד [S1].")


def test_f1_instructions_are_never_searched_or_reported_missing_from_the_documents(offline):
    asks = [("השימושים לפי התכנית המאושרת", "ייעוד", []), ("מספר יחידות הדיור", "84", []), (STYLE, None, []),
            (CITE, None, [])]
    # the request analysis (U2): the two data, conditional on their appearing, and the two instructions
    analysed = [component(asks[0][0], conditional=True), component(asks[1][0], conditional=True),
                component(STYLE, "instruction", aspect="style"), component(CITE, "instruction", aspect="citation")]
    agent = ScriptedAgent([SEARCH, final(F1_ANSWER), final(F1_ANSWER), final(F1_ANSWER)], judge=_judge(asks),
                          request=analysed)
    out = _turn(agent, F1_QUESTION)
    # every datum of the shown answer carries a valid citation: the citation instruction is met
    assert all("[S1]" in line for line in out.answer.answer_markdown.splitlines() if re.search(r"\d", line)
               and "דמו/4521" in line)
    # neither instruction is sent to search
    assert not [p for p in _prompts(agent) if "לחפש" in p and (STYLE in p or CITE in p)]
    # neither is reported missing from the documents, and the met citation instruction is not reported at all
    assert not [line for line in out.answer.answer_markdown.splitlines()
                if (STYLE in line or CITE in line) and GAP.search(line)], out.answer.answer_markdown
    missing = (out.report.counts().get("completeness") or {}).get("missing") or []
    assert not [m for m in missing if CITE in m["text"]]


# F3 -----------------------------------------------------------------------------------------------------------------

F3_SOURCE = "סיכום: השווי למ\"ר הוא 9,500 ₪. סף ההשוואה הוא 8,000 ₪ למ\"ר."
F3_QUESTION = "מה השווי למ\"ר, ומה היחס בינו לבין סף ההשוואה?"
RATIO = "היחס בין השווי לסף ההשוואה"
F3_ANSWER = f"השווי למ\"ר הוא 9,500 ₪ [S1].\n{RATIO} לא נמצא בחיפוש במסמכים."


def _quote_cell(quote: str, number: str) -> dict:
    return {"table": None, "row": None, "row_number": None, "column": None, "column_number": None, "quote": quote,
            "number": number}


def test_f3_a_model_not_found_sentence_beside_found_data_gives_way_to_one_precise_reason(offline):
    offline.text, offline.section = F3_SOURCE, "סיכום"
    take = [call("take_value", source="S1", locator=_quote_cell("השווי למ\"ר הוא 9,500 ₪", "9,500"), meaning=None,
                 label="השווי למ״ר"),
            call("take_value", source="S1", locator=_quote_cell("סף ההשוואה הוא 8,000 ₪", "8,000"), meaning=None,
                 label="סף ההשוואה")]
    asks = [("השווי למ\"ר", "9,500", []), (RATIO, None, ["V1", "V2", "F1"])]
    verdict = lambda text: "not_factual" if GAP.search(text) else "supported"  # noqa: E731
    agent = ScriptedAgent([SEARCH, take, [call("calculate", expression="V1 / V2", label=RATIO, justification=None)],
                           final(F3_ANSWER), final(F3_ANSWER), final(F3_ANSWER)], judge=_judge(asks, verdict),
                          request=[component(asks[0][0]), component(RATIO, "calculation")])
    out = _turn(agent, F3_QUESTION)
    md = out.answer.answer_markdown
    assert "9,500" in md
    assert "לא נמצא בחיפוש" not in md, md  # its values were found: "not found in the search" is false
    about = [line for line in md.splitlines() if "היחס" in line]
    assert len(about) == 1 and "חישוב" in about[0], md  # one statement, the precise reason: calculation not completed


# F4 -----------------------------------------------------------------------------------------------------------------

F4_SOURCE = "סיכום: השווי למ\"ר הוא 9,500 ₪. דמי השכירות הם 55 ₪ למ\"ר לחודש. הנכס פנוי."
F4_ANSWER = ("השווי למ\"ר הוא 9,500 ₪ [S1].\n"
             "דמי השכירות הם 1,234 ₪ למ\"ר לחודש [S1].\n"
             "הנכס פנוי ומושכר בחלקו [S1].")


@pytest.mark.xfail(strict=True, reason="round 7 F4: a removal keeps only text, reason, severity and kind "
                                       "(Problem.as_dict); diagnostics 'removed' holds every problem, removals or not "
                                       "(api._diagnostics), and the public verification is counts only "
                                       "(api.public_verification)")
def test_f4_a_removal_is_recorded_with_its_failure_kind_and_the_sources_checked(offline):
    offline.text, offline.section = F4_SOURCE, "סיכום"

    def judge(input: str) -> dict:  # the third sentence is partly supported, with no concrete defect (kept, marked)
        return {"verdicts": [{"index": i, "verdict": "partial" if "בחלקו" in t else "supported", "reason": "בדיקה",
                              "defect": "none"} for i, t in _units(input).items()]}

    agent = ScriptedAgent([[call("search", query="שווי", document_ids=None, limit=None)], final(F4_ANSWER),
                           CallStatus.ERROR], judge=judge)
    out = _turn(agent, "מה השווי למ\"ר ודמי השכירות?")
    counts = out.report.counts()
    assert counts["removed"] == 1 and "1,234" not in out.answer.answer_markdown  # the wrong number was removed
    removed = api._diagnostics(out, {})["removed"]
    # the stored diagnostics hold the removal only (not the kept, marked sentence), with its structured decision
    assert len(removed) == 1, removed
    (decision,) = removed
    assert decision.get("failure_kind") and "S1" in (decision.get("checked_ids") or []), decision
    # the user's normal path names the removal's failure kind, not only a count
    shown = api.public_verification(counts)
    assert any(isinstance(v, list) and len(v) == 1 and decision["failure_kind"] in json.dumps(v, ensure_ascii=False)
               for v in shown.values()), shown


# --- turns over the test database ----------------------------------------------------------------------------------


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    return make_office(db, "משרד א", "admin-a@example.test")


def ingest_round7(office, monkeypatch, key: str) -> str:
    """A round-7 fixture ingested as an office without OCR or vision at ingestion (a picture stays an unread
    region), without the measurement pass."""
    from app.platform import pipeline
    from tests.integration.test_documents_api import ingest

    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    monkeypatch.setattr(pipeline, "vision_reader", lambda ctx: None)
    monkeypatch.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
    doc, _ = ingest(office, ROUND7 / Path(DOCS[key]["file"]).name, DOCS[key]["title"])
    return doc


def handle_of(output: str, name: str) -> str:
    m = re.search(r"([§T]\d+) «" + re.escape(name) + "»", output)
    assert m, output
    return m.group(1)


def _last_output(items) -> str:
    return [i["output"] for i in items if isinstance(i, dict) and i.get("type") == "function_call_output"][-1]


def _open_section(name: str):
    return lambda items: [read(section=handle_of(_last_output(items), name))]


def meaning(kind: str, unit: str = "ILS", role: str = "other", subject: str = "", **kw) -> dict:
    return {"kind": kind, "unit": unit, "period": "none", "vat": "unknown", "area_basis": "", "subject": subject,
            "role": role, "stated_by": "", "stance": "unknown", "scenario": ""} | kw


def take(source: str, locator: dict, meaning_: dict, label: str) -> dict:
    return call("take_value", source=source, locator=locator, meaning=meaning_, label=label)


def cell(row: str, column: str, number: str) -> dict:
    return {"table": None, "row": row, "row_number": None, "column": column, "column_number": None, "quote": None,
            "number": number}


def _ask(client, office, monkeypatch, agent: ScriptedAgent, question: str) -> dict:
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), question)
    assert m["status"] == "done", m
    return m["answer"]


# F2 -----------------------------------------------------------------------------------------------------------------

CATEGORY = "נתוני התכנית: שימושים, יח״ד, שטחים, גובה, קומות וקווי בניין"
F2_QUESTION = f"פרט את {CATEGORY}, ככל שהם מופיעים בפרק המצב התכנוני."
F2_ANSWER = ("לפי תכנית דמו/4521 המאושרת, ייעוד הקרקע מגורים, עם מסחר בקומת הקרקע [S1].\n"
             "מספר יחידות הדיור המותר הוא 84 יח״ד [S1].\n"
             "מספר הקומות המותר הוא 9 קומות מעל קומת הקרקע [S1].")
ABSENT = ("שטחים", "גובה", "קווי בניין")  # in the request's words; the manifest's ``absent`` lists the same three
# the six items the request names, each with what gives it in the answer (None: nothing does)
ITEMS = (("שימושים", "מסחר"), ("יח״ד", "84"), ("שטחים", None), ("גובה", None), ("קומות", "9 קומות"),
         ("קווי בניין", None))


@pytest.mark.db
def test_f2_a_category_partly_answered_is_never_declared_missing_and_each_absent_item_is_named(client, office,
                                                                                             monkeypatch):
    doc = ingest_round7(office, monkeypatch, "plan_status")
    assert DOCS["plan_status"]["absent"]["section"] == PLAN_SECTION
    # the request analysis (U2): the category, conditional on its items appearing, with a child per item; the judge
    # scores each item and, as the trace found, the category as a whole missing
    analysed = [component(CATEGORY, id="1", conditional=True),
                *(component(item, id=f"1.{n}", parent="1") for n, (item, _) in enumerate(ITEMS, 1))]
    agent = ScriptedAgent([[call("outline", document=doc)], _open_section(PLAN_SECTION),
                           final(F2_ANSWER, documents=[doc]), final(F2_ANSWER, documents=[doc]),
                           final(F2_ANSWER, documents=[doc])],
                          judge=_judge([(CATEGORY, None, ["S1"]),
                                        *((item, keyword, [] if keyword else ["S1"]) for item, keyword in ITEMS)]),
                          request=analysed)
    a = _ask(client, office, monkeypatch, agent, F2_QUESTION)
    md = a["markdown"]
    assert "84" in md and "9 קומות" in md and "מסחר" in md
    gaps = [line for line in md.splitlines() if GAP.search(line)]
    # no sentence declares the category, or an item the answer gives, missing
    assert not [g for g in gaps if any(w in g for w in ("נתוני התכנית", "שימושים", "יח״ד", "קומות"))], md
    # each item that does not appear is stated on its own, naming the section that was read
    for item in ABSENT:
        assert [g for g in gaps if item in g and PLAN_SECTION.split(". ", 1)[1] in g], (item, md)


# F5 -----------------------------------------------------------------------------------------------------------------

class ScriptedTable:
    """A vision reader that transcribes R7c's cost table as the manifest records it."""

    config = "scripted-vision:low"

    def __init__(self) -> None:
        self.calls = 0
        self.usage = None
        picture = _fact("cost_table_image", "picture")
        self.table = VisionTableOut("", picture["headers"], [list(r) for r in picture["rows"]], [picture["note"]])

    def read(self, png: bytes, context: str, careful: bool = False, deadline: float | None = None) -> VisionOut:
        self.calls += 1
        return VisionOut("table", True, "", [self.table], "טבלת עלויות", [])


@pytest.mark.db
@pytest.mark.xfail(strict=True, reason="round 7 F5: an inspected table is registered as an image source with no "
                                       "table index (tools._visual), so take_value refuses its cells (_table_of: "
                                       "'S# אינו טבלה') and its rows as quotes (_take_quote); no value from it can "
                                       "enter calculate in the turn")
def test_f5_a_cell_of_a_table_read_by_inspect_is_taken_and_computed_in_the_same_turn(office, monkeypatch):
    doc = ingest_round7(office, monkeypatch, "cost_table_image")
    vision = ScriptedTable()
    monkeypatch.setattr(T, "inspect_reader", lambda ctx: vision)
    ws = T.Workspace(ctx=office.ctx())
    ws.user_messages = [{"turn": 1, "text": "מה עלות הבנייה העילית והחניון יחד?", "current": True}]
    page = T.tool_read(ws, {"pages": {"document": doc, "from_page": 1, "to_page": 1}})
    region = re.search(r"\[אזור שלא נקרא (R\d+)", page).group(1)
    shown = T.tool_inspect(ws, {"region": region})
    sid = re.search(r'<source id="(S\d+)"', shown).group(1)
    assert vision.calls == 1 and "15,600,000" in shown and "4,620,000" in shown
    total = _fact("cost_table_image", "picture")["headers"][3]
    q = DOCS["cost_table_image"]["question_total"]
    outputs = [T.run_tool(ws, "take_value", json.dumps(
        {"source": sid, "locator": cell(row, total, number), "meaning": meaning("cost", role="component",
                                                                                  subject="פרויקט שדרות הדובדבן"),
         "label": row}, ensure_ascii=False)) for row, number in zip(q["rows"], q["inputs"], strict=True)]
    assert outputs[0].startswith("V1 נרשם") and outputs[1].startswith("V2 נרשם"), outputs
    result = T.run_tool(ws, "calculate", json.dumps({"expression": "V1 + V2", "label": "עלות הבנייה והחניון",
                                                      "justification": None}, ensure_ascii=False))
    out = json.loads(result)
    assert out["id"] == "C1" and out["display"]["value"] == q["result"], result
    # its inputs are confirmed beyond the model's transcription (R18): the host's Tesseract may lack Hebrew, so the
    # unit that lands this scripts the OCR words (and boxes) of the crop rather than rely on it
    assert out["conditional"] is False, result


# F6 -----------------------------------------------------------------------------------------------------------------

FIRST, SECOND = (_fact("two_appraisals", f"{w}_title") for w in ("first", "second"))
F6_QUESTION = f"מה השווי למ״ר שנקבע לנכס ב{FIRST['street']}?"


@pytest.mark.db
@pytest.mark.xfail(strict=True, reason="round 7 F6: property identity stops at the file: a value's subject is the "
                                       "model's free text with no provenance (tools._settle_meaning) and no check ties "
                                       "a block to the appraisal it belongs to, so the second appraisal's figure is "
                                       "accepted for the first property")
def test_f6_a_figure_of_the_second_appraisal_in_one_file_is_not_accepted_for_the_first_property(client, office,
                                                                                               monkeypatch):
    doc = ingest_round7(office, monkeypatch, "two_appraisals")
    first, second = _fact("two_appraisals", "first_per_sqm"), _fact("two_appraisals", "second_per_sqm")
    assert first["page"] == FIRST["page"] and second["page"] == SECOND["page"] != FIRST["page"]
    section = first["section"]
    assert second["section"] == section  # the same numbered section in both appraisals

    def per_sqm(fact: dict) -> dict:
        return take("S1", _quote_cell(fact["text"].rstrip("."), fact["value"]),
                    meaning("value_per_area", "ILS_per_sqm", "other", subject=FIRST["street"]), "השווי למ״ר")

    wrong = f"השווי למ״ר שנקבע לנכס ב{FIRST['street']} הוא {second['value']} ₪ [V1]."
    right = f"השווי למ״ר שנקבע לנכס ב{FIRST['street']} הוא {first['value']} ₪ [V2]."
    agent = ScriptedAgent([[call("outline", document=doc)], _open_section(section), [per_sqm(second)],
                           final(wrong, documents=[doc]),
                           # a repair round, once the wrong attribution is caught: the right appraisal's figure
                           [per_sqm(first)], final(right, documents=[doc]), final(right, documents=[doc])])
    a = _ask(client, office, monkeypatch, agent, F6_QUESTION)
    assert second["value"] not in a["markdown"], a["markdown"]


# F7 -----------------------------------------------------------------------------------------------------------------

def _quoted(doc_key: str, key: str, source: str, meaning_: dict, label: str) -> dict:
    f = _fact(doc_key, key)
    return take(source, _quote_cell(f["text"].rstrip("."), f["value"]), meaning_, label)


@pytest.mark.db
@pytest.mark.xfail(strict=True, reason="round 7 F7: calculate compares only its result with numbers written in the "
                                       "input documents (tools._reproduces); nothing compares cost x a rounded rate "
                                       "with the explicit amount the same section states, so the near-miss passes as a "
                                       "plain computation and the gap to the threshold is built on it")
def test_f7_cost_times_a_rounded_rate_is_reported_beside_the_explicit_amount_and_not_accepted(client, office,
                                                                                             monkeypatch):
    doc = ingest_round7(office, monkeypatch, "residual")
    calc_section, check_section = _fact("residual", "cost")["section"], _fact("residual", "threshold")["section"]
    gap = DOCS["residual"]["gap_to_threshold"]
    subject = "פרויקט ברחוב התאנה 30"
    wrong = (f"לפי התחשיב, הרווח היזמי הוא {gap['cost_times_rate']} ₪ [C1], והפער בינו לבין הרווח המינימלי הנדרש הוא "
             f"{gap['from_rounded_rate']} ₪ [C2].")
    right = (f"סכום הרווח היזמי בתחשיב הוא {_fact('residual', 'profit_amount')['value']} ₪ [V4], והפער בינו לבין "
             f"הרווח המינימלי הנדרש הוא {gap['right']} ₪ [C3].")
    agent = ScriptedAgent([
        [call("outline", document=doc)], _open_section(calc_section),
        lambda items: [read(section=handle_of(next(i["output"] for i in items if isinstance(i, dict)
                                                   and i.get("type") == "function_call_output"), check_section))],
        [_quoted("residual", "cost", "S1", meaning("cost", role="cost", subject=subject), "סך העלויות"),
         _quoted("residual", "profit_rate", "S1", meaning("rate", "percent", "rate", subject=subject),
                 "שיעור הרווח היזמי"),
         _quoted("residual", "threshold", "S2", meaning("profit", subject=subject), "הרווח המינימלי הנדרש")],
        [call("calculate", expression="V1 * V2%", label="הרווח היזמי", justification=None)],
        [call("calculate", expression="C1 - V3", label="הפער לסף", justification=None)],
        final(wrong, documents=[doc]),
        # a repair round, once the near-miss is reported: the explicit amount
        [_quoted("residual", "profit_amount", "S1", meaning("profit", subject=subject), "סכום הרווח היזמי")],
        [call("calculate", expression="V4 - V3", label="הפער לסף", justification=None)],
        final(right, documents=[doc]), final(right, documents=[doc])])
    a = _ask(client, office, monkeypatch, agent, "מה הפער בין הרווח היזמי לבין הרווח המינימלי הנדרש?")
    outputs = agent.tool_outputs(len(agent.seen) - 1)
    (profit,) = [o for o in outputs if '"id": "C1"' in o]
    assert json.loads(profit)["display"]["value"] == gap["cost_times_rate"], profit  # cost x the rounded rate
    # the calculator names the amount the section states for the same quantity
    assert _fact("residual", "profit_amount")["value"] in profit, profit
    assert gap["from_rounded_rate"] not in a["markdown"], a["markdown"]


# F8 -----------------------------------------------------------------------------------------------------------------

def add_residual_without_sensitivity(office) -> str:
    """R7d's calculation section (income and cost) as stored blocks, without its sensitivity section: the documents
    give no cost-increase rate."""
    section = _fact("residual", "income")["section"]
    lines = [_fact("residual", k)["text"] for k in ("income", "cost")]
    doc, ver = make_document(office, office.default_group_id, "בדיקת כדאיות — רחוב התאנה 30 (סינתטי)", sha="7" * 64)
    blocks = [("heading", section), *(("paragraph", t) for t in lines)]
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET page_count = 1, ingestion = CAST(:i AS jsonb) WHERE id = :v"),
                     {"v": ver, "i": json.dumps({"reading_id": "reading-r7"})})
        for i, (kind, t) in enumerate(blocks):
            conn.execute(text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, page, text, status) VALUES (app_office(), :d, :v, :b, :k, :s, :sp, 1, :t, 'read')"),
                {"d": doc, "v": ver, "b": i, "k": kind, "s": section, "sp": [section], "t": t})
    return str(doc)


@pytest.mark.db
@pytest.mark.xfail(strict=True, reason="round 7 F8: nothing detects a calculation resting on a parameter the user "
                                       "never gave: no first-turn request analysis, no clarification route besides "
                                       "resolve's entity checks, and calculate accepts its structural literal 12 as a "
                                       "rate ('12%'), so a scenario result with an invented rate is shown as computed")
def test_f8_a_cost_increase_with_no_rate_given_asks_for_the_rate_and_keeps_the_found_values(client, office,
                                                                                           monkeypatch):
    doc = add_residual_without_sensitivity(office)
    section = _fact("residual", "income")["section"]
    subject = "פרויקט ברחוב התאנה 30"
    income, cost = _fact("residual", "income")["value"], _fact("residual", "cost")["value"]
    invented = "4,048,000"  # 24,600,000 - 18,350,000 x 1.12: a 12% increase nobody gave
    wrong = (f"ההכנסות הצפויות הן {income} ₪ [V1] והעלויות {cost} ₪ [V2]. אם העלויות יעלו, הרווח היזמי יהיה "
             f"{invented} ₪ [C1].")
    ask = f"ההכנסות הצפויות הן {income} ₪ [V1] והעלויות {cost} ₪ [V2]. באיזה שיעור לדעתך יעלו העלויות?"
    agent = ScriptedAgent([
        [call("outline", document=doc)], _open_section(section),
        [_quoted("residual", "income", "S1", meaning("income", role="income", subject=subject), "ההכנסות"),
         _quoted("residual", "cost", "S1", meaning("cost", role="cost", subject=subject), "העלויות")],
        [call("calculate", expression="V1 - V2 * (1 + 12%)", label="הרווח בתרחיש", justification=None)],
        final(wrong, documents=[doc]),
        # a repair round, once the unrequested assumption is caught: the found values and one question
        final(ask, status="clarification", clarification="באיזה שיעור יעלו העלויות?", documents=[doc]),
        final(ask, status="clarification", clarification="באיזה שיעור יעלו העלויות?", documents=[doc])],
        # the request analysis (U2): a calculation resting on a rate the user did not give
        request=[component("הרווח היזמי אם עלויות הבנייה והפיתוח יעלו", "calculation", subject=subject,
                           parameters=[{"name": "שיעור העלייה של העלויות", "source": "not_given_by_user",
                                        "quote": ""}])])
    a = _ask(client, office, monkeypatch, agent, "מה יהיה הרווח היזמי אם עלויות הבנייה והפיתוח יעלו?")
    md = a["markdown"]
    assert invented not in md, md
    assert a["status"] == "clarification", (a["status"], md)
    assert income in md and cost in md  # the data already found are kept
