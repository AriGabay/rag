"""Round 14 U1 sequences, end to end with a scripted model (R6, R15, R16; AE4): one datum per source span.

The synthetic round-13 capitalization fixture (rates by cash flow in one section, read by the real worker) goes through the whole
turn with a scripted agent that registers the same quote and number more than once, and once with a quote the source does not
hold. What is proven is what the server decides, stores and shows, never wording:

- the tool: a repeated registration returns the first V# and says so, and creates no second value or source;
- the answer: the components table holds one row for the number, and the answer read back after the turn equals the live one;
- the diagnostics: a refused ``take_value`` is recorded with its reason, source and clipped place (``resolution.registrations``),
  and a turn whose registrations all succeeded has no such record.

Round 14 U6 (R9, R11-R13, R16; KTD7, KTD8; AE9) adds the rows of the answer: the components table holds what the final text uses, each
row states its reading, component, claim, support and comparison, a mark names rows on screen, and a context facet carries its
resolved anchor. Every name, address and amount is invented."""

# ruff: noqa: F811 - the tests take the imported fixtures by name, as the round-13 sequences do
from __future__ import annotations

import json
import re
from collections import Counter

import pytest
from sqlalchemy import text

from app.chat import datum, timeline
from app.chat import tools as T
from app.chat.compose import SERVER_SPANS_FIELD
from app.db import tenant_tx
from app.measurements.extract import EXTRACTION_VERSION
from tests.integration.test_chat_inspect import emp
from tests.integration.test_chat_timeline import (
    ADOPTED,
    _focus_of,
    m_diagnostics,
    partial,  # noqa: F401 - three dated appraisals and an undated one of a property: coverage that can never be complete (the fixture)
    setup,  # noqa: F401 - the office of those appraisals (the fixture ``partial`` takes)
)
from tests.integration.test_chat_timeline import agent as timeline_agent
from tests.integration.test_chat_timeline import ask as ask_timeline
from tests.integration.test_chat_value_status import (
    _ocr_sees_the_table,
    _uncertain_table,
    vision_office,  # noqa: F401 - the office with a scripted inspect reader (the fixture)
)
from tests.integration.test_property_scope import FIXTURES, upload
from tests.integration.test_round10_sequences import (
    calls,  # noqa: F401 - the recorded tool calls (the fixture)
)
from tests.integration.test_round10_sequences import (
    no_models as no_models,  # noqa: F401 - the autouse inline-run setup
)
from tests.integration.test_round12_sequences import bare_world, unit_appraisal
from tests.integration.test_round13_sequences import (
    LAND,
    LAND_Q,
    answer_of,
    ask13,
    hit_with,
    kept_whole,
    listing,
    live,  # noqa: F401 - the payload the turn built (the fixture)
    refreshed_equals_live,
    registering,
    rows_in,
    search_for,
    sentence_of,
    stored_of,
    take,
    world13,
)
from tests.support.scripted_agent import call, final

pytestmark = pytest.mark.db

QUESTION = "מהם שיעורי ההיוון בנכס ברחוב הכלנית 8?"
TWO = ["cap.offices", "cap.parking"]


def diagnostics_of(o, t) -> dict:
    r = o.client.get(f"/api/chat/messages/{t.message['id']}/diagnostics")
    assert r.status_code == 200, r.text
    return r.json()


def labelled(o, datum_id: str, items: list, label: str) -> dict:
    """The model's ``take_value`` of a datum under a label of its own (the model names the same figure differently each time)."""
    c = take(o, datum_id, items, kind="rate", unit="percent")
    c["arguments"]["label"] = label
    return c


def test_ae4_one_quote_and_number_registered_three_times_is_one_value_and_one_row_in_the_components_table(
        db, client, monkeypatch, calls, live):
    o = world13(db, client, monkeypatch, calls, ("cap_rates",))

    def register(items):
        return [labelled(o, "cap.offices", items, "שיעור היוון משרדים"), labelled(o, "cap.offices", items, "היוון"),
                labelled(o, "cap.offices", items, "שיעור משרדים שוב"), labelled(o, "cap.parking", items, "שיעור חניות")]

    steps = [search_for(o, "שיעור היוון", ("cap_rates",), TWO), register,
             answer_of(listing(TWO), "הכלנית 8", "rate", "percent")]
    t = ask13(o, QUESTION, steps)
    a = t.answer
    # the tool: the second and third calls returned the first value and said so; the fourth is a new value, V2
    takes = [c for c in calls if c.name == "take_value"]
    assert [c.output.split()[0] for c in takes] == ["V1", "V1", "V1", "V2"]
    assert T.MSG_ALREADY_REGISTERED not in takes[0].output and T.MSG_ALREADY_REGISTERED not in takes[3].output
    assert all(T.MSG_ALREADY_REGISTERED in c.output for c in takes[1:3])
    # (a) the verification report: both figures are grounded, nothing is removed
    kept_whole(o, t)
    assert [v["id"] for v in a["values"]] == ["V1", "V2"]
    # (c) the components table: one row for the offices rate and one for the parking rate, never three for the same figure
    (block,) = a["time"]["breakdown"]
    assert [r["sid"] for r in block["rows"]] == ["V1", "V2"] and [r["value_text"] for r in block["rows"]] == ["6.5", "7.25"]
    assert all(r["marks"] == [] for r in block["rows"]) and a["time"]["timeline"] == []
    # (d) after a refresh the same rows; the body and the table agree and the turn made no model call beyond the script
    stored = after_turn(o, t, live)
    assert rows_in(stored["time"]["breakdown"]).keys() == {"V1", "V2"}
    # the diagnostics of a turn whose registrations all succeeded hold no record of failures
    assert "registrations" not in (diagnostics_of(o, t)["resolution"] or {})


def test_a_refused_take_value_is_recorded_in_the_diagnostics_with_its_reason_and_place_and_a_successful_one_is_not(
        db, client, monkeypatch, calls, live):
    o = world13(db, client, monkeypatch, calls, ("cap_rates",))
    missing = "שיעור היוון של 11% לתזרים שאינו כתוב במסמך הזה בכלל"

    def register(items):
        sid = hit_with(items, o.ids["cap_rates"], "שיעור היוון")
        bad = call("take_value", source=sid, meaning=take(o, "cap.offices", items, kind="rate", unit="percent")["arguments"]["meaning"],
                   locator={"table": None, "row": None, "row_number": None, "column": None, "column_number": None,
                            "quote": missing, "number": "11"}, label="לא קיים")
        return [bad, labelled(o, "cap.offices", items, "שיעור משרדים")]

    steps = [search_for(o, "שיעור היוון", ("cap_rates",), ["cap.offices"]), register,
             answer_of(listing(["cap.offices"]), "הכלנית 8", "rate", "percent")]
    t = ask13(o, QUESTION, steps)
    bad, good = [c for c in calls if c.name == "take_value"]
    assert bad.output.startswith("שגיאה") and good.output.startswith("V1")
    res = diagnostics_of(o, t)["resolution"]
    (entry,) = res["registrations"]  # the refused call only
    assert entry["tool"] == "take_value" and entry["source"] == bad.args["source"] and entry["key"] == "refused"
    assert "11" in entry["locator"] and len(entry["locator"]) <= 200 and entry["message"] and entry["at"]
    assert json.dumps(res["registrations"], ensure_ascii=False).count(missing) <= 1  # a clipped locator, not the document's text
    refreshed_equals_live(o, t, live)


RENT_COLUMN = "דמי שכירות (₪)"
# a one-time amount with no VAT to state: every field of its datum key is known, so the two registrations meet in one bucket
STATED_MEANING = {"kind": "rent", "unit": "ILS", "period": "one_time", "vat": "not_applicable", "area_basis": "",
                  "subject": "אזור צפון", "role": "other"}


def test_an_unclear_reading_taken_again_is_re_read_and_still_one_class_and_one_row(vision_office, monkeypatch):
    """A repeated take of a place whose reading stays unclear is not answered with the first id: it goes the whole way again
    (the re-read, KTD2), and the datum rule joins the registrations by their span, so the table shows one row (R6)."""
    ws = emp(vision_office)
    sid = _uncertain_table(vision_office, monkeypatch, ws)
    _ocr_sees_the_table(monkeypatch)
    vision_office.vision.uncertain = ["דמי שכירות"]  # the re-read is unclear too
    outs = [T.tool_take_value(ws, sid, {"row": "צפון", "column": RENT_COLUMN}, STATED_MEANING, label) for label in ("דמי שכירות צפון", "שכירות")]
    # (a) the second take re-read (the first re-read calls the model, the second is served from the stored reading) and has an id of its own
    assert [o.split()[0] for o in outs] == ["V1", "V2"] and list(ws.rereads.values()) == [2]
    assert T.MSG_ALREADY_REGISTERED not in outs[1] and T.STATUS_UNCERTAIN in outs[1]
    # (b) both registrations are at one place: one class, so one row
    assert ws.spans["V1"] is not None and ws.spans["V1"] == ws.spans["V2"]
    (bucket,) = timeline.buckets(ws, {"V1", "V2"})
    assert [[d.ref for d in cls] for cls in bucket.classes] == [["V1", "V2"]]


# =======================================================================================================================
# round 14 U6: the rows of the answer come from its final text, each with a structured state (R9, R11-R13, R16; KTD7, KTD8; AE9)
# =======================================================================================================================

CAP_Q = "מהם שיעורי ההיוון בנכס ברחוב הכלנית 8?"
ROW_STATE_KEYS = {"reading", "component", "claim", "support", "comparison", "reason"}


def every_row(answer: dict) -> list[dict]:
    return [r for key in ("timeline", "breakdown") for b in answer["time"][key] for r in b["rows"]]


def named_by_marks(answer: dict) -> set[str]:
    return {x for r in every_row(answer) for k in ("undecided_with", "conflict_with") for x in r.get(k, [])}


def test_ae9_values_the_answer_does_not_cite_are_no_rows_and_no_payload_values_and_a_mark_names_rows_on_screen(
        db, client, monkeypatch, calls, live):
    """Six rates are registered, the answer cites the first two and the bare one (V6): the table holds those three; the other three are
    in neither the table nor the payload's values, and V6's undecided mark names the two rows and says of the three that they are not
    in the answer (title and location)."""
    o = world13(db, client, monkeypatch, calls, ("cap_rates",))
    ids = ["cap.offices", "cap.parking", "cap.storage", "cap.residual", "cap.billboard", "cap.bare"]
    body = "; ".join(f"{sentence_of(ids[n - 1])} [V{n}]" for n in (1, 2, 6)) + "."
    steps = [search_for(o, "שיעור היוון", ("cap_rates",), ids), registering(o, ids, kind="rate", unit="percent"),
             answer_of(body, "הכלנית 8", "rate", "percent")]
    t = ask13(o, CAP_Q, steps)
    a = t.answer
    kept_whole(o, t)
    assert [v["id"] for v in a["values"]] == ["V1", "V2", "V6"], "the registered values the answer does not cite are not payload values"
    (block,) = a["time"]["breakdown"]
    assert [r["sid"] for r in block["rows"]] == ["V1", "V2", "V6"] and a["time"]["timeline"] == []
    rows = rows_in(a["time"]["breakdown"])
    assert rows["V6"]["marks"] == ["undecided"] and rows["V6"]["undecided_with"] == ["V1", "V2"]
    assert named_by_marks(a) <= set(rows), "no mark names a value that has no row"
    away = rows["V6"]["not_in_answer_with"]
    assert [x["sid"] for x in away] == ["V3", "V4", "V5"] and all(x["title"] and x["location"] for x in away)
    assert rows["V6"]["state"]["comparison"] == {"status": "undecided", "with": ["V1", "V2"]}
    assert timeline.not_in_answer_text(away[0]) in a["time"]["text"] and "V3" not in a["time"]["text"]
    # every row has the whole state, and a row-level reason
    for r in every_row(a):
        assert set(r["state"]) == ROW_STATE_KEYS and r["state"]["reason"] and r["state"]["support"] == "cited"
        assert r["state"]["reading"] == "verified" and r["state"]["claim"]["result"] == "supported"
    assert rows["V6"]["state"]["component"]["status"] == "none" and rows["V6"]["state"]["reason"] == "not_in_range"
    assert rows["V1"]["state"]["component"]["status"] == "stated" and rows["V1"]["state"]["comparison"]["status"] == "different"
    stored = after_turn(o, t, live)
    assert stored["time"]["breakdown"] == a["time"]["breakdown"]
    # an answer stored before round 14 has no state, note or counterpart list: the API returns it as stored
    with tenant_tx(o.ctx()) as conn:
        old = json.loads(json.dumps(a, default=str))
        for r in every_row(old):
            for key in ("state", "note", "not_in_answer_with"):
                r.pop(key, None)
        conn.execute(text("UPDATE messages SET answer = CAST(:a AS jsonb) WHERE id = :m"),
                     {"a": json.dumps(old, ensure_ascii=False), "m": t.message["id"]})
    read_back = stored_of(o, t)
    assert read_back["time"] == old["time"] and not any("state" in r for r in every_row(read_back))


BULLET_1 = "- מקדם הפחתה בשל זכויות בנייה חלקיות הוא 0.8 [V1]"
BULLET_2 = "- מקדם הפחתה בשל שיתוף במקרקעין הוא 0.9 [V2]"


def second_bullet_judge(input: str) -> dict:
    """The judge of the R16 trace: the bullet that gives 0.9 is unsupported, the other stands."""
    out = []
    for index, unit in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S):
        verdict = ({"verdict": "unsupported", "failure": "absent_from_source"} if "שיתוף" in unit else {"verdict": "supported"})
        out.append({"index": int(index), "reason": "בדיקה", "supported_by": []} | verdict)
    return {"verdicts": out}


def test_r16_when_verification_removes_the_only_claim_citing_a_value_its_row_leaves_and_the_other_is_text_only(
        db, client, monkeypatch, calls, live):
    o = world13(db, client, monkeypatch, calls, ("land_table",))
    ans = answer_of("\n".join([BULLET_1, BULLET_2]), "הצבעוני 14")
    steps = [search_for(o, "מקדם הפחתה", ("land_table",), LAND), registering(o, LAND), ans, ans, ans]
    t = ask13(o, LAND_Q, steps, judge=second_bullet_judge)
    a = t.answer
    assert a["verification"]["removed"] == 1 and "0.9" not in a["markdown"] and "0.8" in a["markdown"]
    # one used row is left: it is in the text only, never a one-row table, and nothing names the removed value
    assert a["time"]["breakdown"] == [] and a["time"]["timeline"] == []
    assert [v["id"] for v in a["values"]] == ["V1"] and "V2" not in json.dumps(a["time"], ensure_ascii=False)
    refreshed_equals_live(o, t, live)


# =======================================================================================================================
# round 14 U9: the acceptance shapes (AE1-AE11) end to end, with a reload, and no model call beyond the script (R1-R17)
# =======================================================================================================================

CITED = re.compile(r"\[([VM]\d+)\]")
FACET_FIELDS = ("status", "written", "part", "tier")
MODEL_PURPOSES = {"agent", "request", "verify", "verify_final"}


def rows_of_answer(answer: dict) -> list[dict]:
    return [r for key in ("timeline", "breakdown") for b in answer["time"][key] for r in b["rows"]]


def component_of(row: dict) -> dict:
    return {k: row["state"]["component"].get(k) for k in FACET_FIELDS}


def consistent_with_body(answer: dict) -> None:
    """The body and the table agree (R7, R9, R11, R13; KTD7, KTD8): every value the final answer cites that is in a components
    block is on screen exactly once (a row of its own, or the reduction of its coefficient's row), no value is on screen twice, a
    components row is one the answer cites, and every row's component state is the facet of its value: the table says what the
    reading found, whatever words the body used. A component the source states is never shown as missing."""
    cited = set(CITED.findall(answer["markdown"])) | {i["id"] for c in answer.get("computations") or [] for i in c.get("inputs") or []}
    on_screen = Counter()
    for r in rows_of_answer(answer):
        on_screen[r["sid"]] += 1
        if r.get("reduction"):
            on_screen[r["reduction"]["sid"]] += 1
    assert all(n == 1 for n in on_screen.values()), f"a value on screen twice: {on_screen}"
    for b in answer["time"]["breakdown"]:
        for r in b["rows"]:
            for sid in (r["sid"], (r.get("reduction") or {}).get("sid")):
                assert sid is None or sid in cited, f"{sid} is on screen in a components block and the answer does not cite it"
    values = {v["id"]: v for v in [*answer["values"], *(answer.get("measurements") or [])]}
    for r in rows_of_answer(answer):
        facet = {k: values[r["sid"]]["facet"].get(k) for k in FACET_FIELDS}
        assert component_of(r) == facet, (r["sid"], component_of(r), facet)
        if facet["status"] == datum.STATUS_STATED:
            assert r["facet"] == facet["written"] and r["facet"] != timeline.MSG_NO_FACET
        else:
            assert r["facet"] != timeline.MSG_NO_FACET or r["state"]["component"]["reason"] == datum.REASON_NOT_NEEDED
        assert r["state"]["component"]["status"] != datum.STATUS_STATED or r["state"]["component"]["written"]


def model_calls(message: dict) -> Counter:
    """The model calls of a turn by purpose, as the message stored them."""
    return Counter(u["purpose"] for u in message["usage"])


def no_extra_model_calls(message: dict, agent, exhausted: bool = True) -> None:
    """The new paths make no model call (KTD3, KTD4; the plan's cost criterion): the calls the turn recorded by purpose are the
    scripted agent's own steps, the request analysis and the verification judge, exactly as the scripted provider received them."""
    used = model_calls(message)
    assert set(used) <= MODEL_PURPOSES, used
    received = Counter(c.purpose.value for c in agent.calls)
    assert set(received) <= {"resolve", "verify"}, received  # nothing else reached the provider
    assert used["agent"] == len(agent.seen)
    assert used["request"] == received.get("resolve", 0)
    assert used["verify"] + used["verify_final"] == received.get("verify", 0)
    assert not exhausted or agent.steps == [], "the script was used up: no step was left over and none was asked beyond it"


def after_turn(o, t, live, exhausted: bool = True) -> dict:
    """What every sequence ends with: the answer read back equals the live one, the body and the table agree, the turn made no model
    call beyond the script. Returns the stored answer."""
    stored = refreshed_equals_live(o, t, live)
    assert stored["verification"]["correctness"] == live[-1]["verification"]["correctness"]
    consistent_with_body(stored)
    no_extra_model_calls(t.message, t.agent, exhausted)
    return stored


# --- a component read from an earlier sentence: its location, quote and resolved anchor ------------------------------------------

MANIFEST14 = json.loads((FIXTURES / "round14" / "manifest.json").read_text(encoding="utf-8"))
DOCS14 = MANIFEST14["documents"]
DOC14 = DOCS14["context_reader"]
DATA14 = {d["id"]: d for d in DOC14["data"]}
DATUM14 = {d["id"]: d | {"doc": key} for key, spec in DOCS14.items() for d in spec["data"]}


def world14(db, client, monkeypatch, calls, keys: tuple[str, ...], email: str = "admin-r14u9@example.test"):
    """A synthetic office with the given round-14 fixtures uploaded through the worker (single appraisals), cloud use on."""
    o = bare_world(db, client, monkeypatch, calls, email)
    for key in keys:
        o.ids[key] = upload(o, FIXTURES / DOCS14[key]["file"], DOCS14[key]["source_kind"], monkeypatch)
    return o


def hits14(items: list, doc: str) -> list[dict]:
    """The search hits of ``doc`` the model has seen, as attributes and body, in order (a hit that repeats an earlier one is
    self-closing, ``same_as``, and holds no text: it is not a candidate)."""
    out = []
    for it in items:
        if isinstance(it, dict) and it.get("type") == "function_call_output":
            for m in re.finditer(r"<source ([^>]*?[^/>])>(.*?)</source>", it["output"], re.S):
                attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
                if attrs.get("document_id") == doc:
                    out.append(attrs | {"body": m.group(2)})
    return out


def take14(o, datum_id: str, items: list, **meaning) -> dict:
    """A ``take_value`` of a round-14 manifest datum, from the hit of its document that holds it: a cell by its row and column
    header, a sentence by its quote (the same sentence is in several plots: the hit is the one of the datum's section), with the
    kind and unit the manifest gives."""
    d = DATUM14[datum_id]
    doc, where = o.ids[d["doc"]], d["where"]
    number = re.sub(r"[^\d.,]", "", d["shown"])
    if "table" in where:
        t = where["table"]
        locator = {"table": None, "row": t["row_label"], "row_number": None, "column": t["column_header"], "column_number": None,
                   "quote": None, "number": number}
        needles, section = (t["row_label"], number), ""
    else:
        quote = where["quote"].rstrip(".")
        locator = {"table": None, "row": None, "row_number": None, "column": None, "column_number": None, "quote": quote,
                   "number": number}
        needles, section = (quote,), where.get("section", "")
    held = [h for h in hits14(items, doc) if all(n in h["body"] for n in needles)]
    source = next(h["id"] for h in held if section in h.get("location", ""))
    base = ADOPTED | {"kind": d["kind"], "unit": d["unit"], "subject": "הנכס"}
    return call("take_value", source=source, locator=locator, meaning=base | meaning, label=datum_id)


def search14(o, datum_ids: list[str]) -> list:
    """The model's first step: a search per datum, by its row label or its sentence, in the datum's document."""
    asked = []
    for i in datum_ids:
        d = DATUM14[i]
        where = d["where"]
        asked.append((o.ids[d["doc"]], where["table"]["row_label"] if "table" in where else where["quote"].rstrip(".")))
    return [call("search", query=q, document_ids=[doc], limit=None) for doc, q in dict.fromkeys(asked)]


def sequence14(o, question: str, datum_ids: list[str], body: str, subject: str, kind: str = "coefficient", unit: str = "percent",
               **agent):
    """One turn: search, register the data in order (V1, V2 ...) and answer with ``body``."""
    steps = [search14(o, datum_ids), lambda items: [take14(o, i, items) for i in datum_ids],
             answer_of(body, subject, kind, unit)]
    return ask13(o, question, steps, **agent)


def test_a_component_read_from_an_earlier_sentence_carries_its_location_quote_and_resolved_anchor_never_the_stub(
        db, client, monkeypatch, calls, live):
    o = world14(db, client, monkeypatch, calls, ("context_reader",), "admin-r14u6@example.test")
    pair = ["r14.same_block", "r14.own_phrase"]
    steps = [[call("search", query=DATA14[i]["where"]["quote"].rstrip("."), document_ids=[o.ids["context_reader"]], limit=None) for i in pair],
             lambda items: [take14(o, i, items) for i in pair],
             answer_of("; ".join(f"מקדם הפחתה {DATA14[i]['shown']} [V{n}]" for n, i in enumerate(pair, 1)) + ".", "הרימון 7",
                       "coefficient", "percent")]
    t = ask13(o, "מהם מקדמי ההפחתה בנכס ברחוב הרימון 7?", steps)
    a = t.answer
    rows = rows_in(a["time"]["breakdown"])
    assert set(rows) == {"V1", "V2"}
    first, second = rows["V1"]["state"]["component"], rows["V2"]["state"]["component"]
    assert (first["status"], first["tier"], first["written"]) == ("stated", "context", "עוצמת ניצול") and first["part"] in (
        "preceding_sentence", "paragraph")
    ctx = first["context"]
    assert ctx["location"] and ctx["quote"] and len(ctx["quote"]) <= timeline.CONTEXT_QUOTE_MAX
    assert ctx["anchor"]["document_id"] == o.ids["context_reader"] and ctx["anchor"]["precision"] == "structured"
    assert ctx["quote"] == "השמאי לא נתן הפחתה בגין עוצמת ניצול.", "the sentence the snapshot highlights"
    assert (second["status"], second["tier"], second["context"]) == ("stated", "own", None)
    assert "'at'" not in str(a["time"]) and '"at"' not in json.dumps(a["time"], ensure_ascii=False), "the raw stub is never sent"
    after_turn(o, t, live)


def second_bullet_partial_judge(input: str) -> dict:
    """The judge of the claim-state trace: the bullet that gives 0.9 is only partly supported, the other is supported."""
    out = []
    for index, unit in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S):
        verdict = ({"verdict": "partial", "defect": "part", "reason": "חלק מהטענה נתמך"} if "שיתוף" in unit
                   else {"verdict": "supported", "defect": "none", "reason": "בדיקה"})
        out.append({"index": int(index), "supported_by": []} | verdict)
    return {"verdicts": out}


def test_a_row_states_the_verification_result_of_the_claim_citing_it_and_a_partly_supported_claim_is_partial(
        db, client, monkeypatch, calls, live):
    o = world13(db, client, monkeypatch, calls, ("land_table",))
    ans = answer_of("\n".join([BULLET_1, BULLET_2]), "הצבעוני 14")
    steps = [search_for(o, "מקדם הפחתה", ("land_table",), LAND), registering(o, LAND), ans, ans, ans]
    t = ask13(o, LAND_Q, steps, judge=second_bullet_partial_judge)
    a = t.answer
    assert a["verification"]["partial"] == 1 and "0.9" in a["markdown"]
    rows = rows_in(a["time"]["breakdown"])
    assert rows["V1"]["state"]["claim"] == {"result": "supported", "reason": None}
    assert rows["V2"]["state"]["claim"]["result"] == "partial" and rows["V2"]["state"]["claim"]["reason"]
    # the reading and the comparison are apart from the claim: a partly supported value is still a verified reading
    assert rows["V2"]["state"]["reading"] == "verified" and rows["V2"]["state"]["comparison"]["status"] == "different"
    refreshed_equals_live(o, t, live)


# --- AE2, AE3: another value stands between, or another plot's section: no component is borrowed --------------------------------

def reduction_line(datum_id: str, n: int, words: str = "מקדם הפחתה") -> str:
    return f"{words} {DATUM14[datum_id]['shown']} [V{n}]"


def test_ae2_a_value_between_the_consideration_and_the_value_leaves_the_10_percent_without_the_component_and_says_why(
        db, client, monkeypatch, calls, live):
    o = world14(db, client, monkeypatch, calls, ("context_reader",))
    ids = ["r14.between", "r14.same_block"]
    body = "; ".join(reduction_line(i, n) for n, i in enumerate(ids, 1)) + "."
    t = sequence14(o, "מהן ההפחתות בנכס ברחוב הרימון 7?", ids, body, "הרימון 7")
    a = t.answer
    kept_whole(o, t)
    (block,) = a["time"]["breakdown"]
    rows = rows_in(a["time"]["breakdown"])
    ten = rows["V1"]["state"]["component"]
    assert (ten["status"], ten["reason"], ten["written"]) == ("unparsed", "intervening_value", None)
    assert rows["V1"]["facet"] == timeline.FACET_TEXT[("unparsed", "intervening_value")]
    assert rows["V1"]["facet"] != "עוצמת ניצול", "the consideration before the 20% is not the 10%'s"
    # the same sentence in the plot with no value between is stated: the difference is the value that stands between
    assert rows["V2"]["state"]["component"]["written"] == "עוצמת ניצול"
    assert not any("conflict" in r["marks"] for r in block["rows"]) and named_by_marks(a) <= set(rows)
    assert [v["facet"]["written"] for v in a["values"]] == [None, "עוצמת ניצול"]
    after_turn(o, t, live)


def test_ae3_a_value_across_a_section_heading_and_one_behind_a_table_take_no_component_from_the_plot_before_and_are_not_merged(
        db, client, monkeypatch, calls, live):
    """Both edges in one answer: the value across a section heading and the one behind a table each read no component, and the two
    bare 10% (two places of one appraisal) are two values — the equal amount merges nothing (R7, KTD1)."""
    o = world14(db, client, monkeypatch, calls, ("context_reader",))
    ids = ["r14.same_block", "r14.second_plot", "r14.table_edge"]
    body = "; ".join(reduction_line(i, n) for n, i in enumerate(ids, 1)) + "."
    t = sequence14(o, "מהם מקדמי ההפחתה בנכס ברחוב הרימון 7?", ids, body, "הרימון 7")
    a = t.answer
    kept_whole(o, t)
    rows = rows_in(a["time"]["breakdown"])
    assert set(rows) == {"V1", "V2", "V3"}
    first = rows["V1"]["state"]["component"]
    assert (first["status"], first["written"], first["part"]) == ("stated", "עוצמת ניצול", "preceding_sentence")
    # the read stopped at the heading (or the table): read, with no component, and the consideration of the plot before is not its
    for sid in ("V2", "V3"):
        second = rows[sid]["state"]
        assert (second["component"]["status"], second["component"]["reason"], second["component"]["written"]) == (
            "none", "not_in_range", None) and second["reason"] == "not_in_range"
        assert rows[sid]["facet"] == timeline.FACET_TEXT[("none", "not_in_range")] and rows[sid]["facet"] != first["written"]
    assert [v["facet"]["written"] for v in a["values"]] == ["עוצמת ניצול", None, None]
    # equal amounts, one with a component and two without: nothing to set against each other, so no conflict mark of any kind
    assert not any("conflict" in r["marks"] for r in rows.values()) and all("conflict_with" not in r for r in rows.values())
    # the two bare values are two data of two places: neither is the unit's headline datum, and equal numbers alone never merge them
    assert rows["V2"]["state"]["comparison"]["status"] == rows["V3"]["state"]["comparison"]["status"] != "same"
    after_turn(o, t, live)


def test_ae1_the_reported_shape_a_second_reduction_explained_two_sentences_earlier_is_one_row_with_that_component_beside_the_first(
        db, client, monkeypatch, calls, live):
    """Two reductions of one appraisal: the first names its own consideration, the second (its sentence points back: "in light of the
    above") takes the consideration from the sentence two before it, in its paragraph."""
    o = world14(db, client, monkeypatch, calls, ("reported",))
    ids = ["reported.first", "reported.second"]
    body = ("מקדם הפחתה בגין סחירות 20% [V1]; מקדם הפחתה נוסף בגין אינטנסיביות שימוש 10% [V2].")
    t = sequence14(o, "מהם מקדמי ההפחתה שהובאו בחשבון בנכס ברחוב הברוש 3?", ids, body, "הברוש 3")
    a = t.answer
    kept_whole(o, t)
    rows = rows_in(a["time"]["breakdown"])
    assert [(r["value_text"], r["facet"]) for r in rows.values()] == [("20", "סחירות"), ("10", "אינטנסיביות שימוש")]
    second = rows["V2"]["state"]["component"]
    assert (second["status"], second["part"], second["tier"]) == ("stated", "preceding_sentence", "context")
    assert second["context"]["quote"] == "השמאי לא מצא לנכון לתת הפחתה בגין אינטנסיביות שימוש."
    assert all(r["marks"] == [] for r in rows.values()) and named_by_marks(a) == set()
    after_turn(o, t, live)


# --- the own-sentence rule: a sentence that describes its value borrows nothing --------------------------------------------------

def test_a_value_whose_own_sentence_describes_it_is_unparsed_with_that_reason_and_borrows_nothing_from_the_paragraph_above(
        db, client, monkeypatch, calls, live):
    o = world14(db, client, monkeypatch, calls, ("own_sentence",))
    ids = ["own_sentence.rate", "own_sentence.other"]
    body = ("שיעור ההיוון המקובל בשוק המסחר הוא 6.7% [V1]; הפחתה של 8% בגין סחירות [V2].")
    t = sequence14(o, "מהם שיעורי ההיוון וההפחתות בנכס ברחוב האלה 9?", ids, body, "האלה 9", "rate", "percent")
    a = t.answer
    kept_whole(o, t)
    rows = rows_in(a["time"]["breakdown"])
    own = rows["V1"]["state"]
    assert (own["component"]["status"], own["component"]["reason"], own["component"]["tier"], own["component"]["part"]) == (
        "unparsed", "own_sentence", "own", "sentence")
    assert own["component"]["written"] is None and own["component"]["context"] is None, "no borrowed place"
    assert rows["V1"]["facet"] == timeline.FACET_TEXT[("unparsed", "own_sentence")]
    # the consideration of the paragraph above (uncertainty of planning) is nowhere in the table or in what the model was told
    assert "אי ודאות" not in json.dumps(a["time"], ensure_ascii=False) + json.dumps(a["values"], ensure_ascii=False)
    told = next(c for c in calls if c.name == "take_value").output
    assert "אי ודאות" not in told and datum.line(datum.unparsed(datum.REASON_OWN_SENTENCE, datum.PART_SENTENCE, datum.TIER_OWN)) in told
    assert rows["V2"]["state"]["component"]["written"] == "סחירות"
    after_turn(o, t, live)


# --- AE5: three equal rates for three adjustments --------------------------------------------------------------------------------

def test_ae5_three_equal_reductions_for_three_considerations_are_three_rows_with_three_components_and_no_mark(
        db, client, monkeypatch, calls, live):
    o = world14(db, client, monkeypatch, calls, ("three_equal",))
    ids = ["three_equal.1", "three_equal.2", "three_equal.3"]
    body = "; ".join(f"הפחתה של 10% בגין {DATUM14[i]['facet']['written']} [V{n}]" for n, i in enumerate(ids, 1)) + "."
    t = sequence14(o, "מהן ההפחתות בנכס ברחוב הדס 5?", ids, body, "הדס 5", "rate", "percent")
    a = t.answer
    kept_whole(o, t)
    (block,) = a["time"]["breakdown"]
    assert [r["sid"] for r in block["rows"]] == ["V1", "V2", "V3"] and [r["value_text"] for r in block["rows"]] == ["10"] * 3
    assert [r["facet"] for r in block["rows"]] == ["סחירות", "מושע", "גמישות שימושים"]
    assert all(r["marks"] == [] and "conflict_with" not in r and "undecided_with" not in r for r in block["rows"])
    assert all(r["state"]["comparison"]["status"] == "different" for r in block["rows"])
    assert "conflict" not in json.dumps(a["time"], ensure_ascii=False) and "undecided" not in json.dumps(a["time"], ensure_ascii=False)
    assert a["time"]["timeline"] == [] and a["requested"] == [] and a["gaps"] == []
    after_turn(o, t, live)


# --- AE6: a table coefficient and the text's reduction of the same consideration ---------------------------------------------------

ADJUSTMENT_Q = "מהם מקדמי ההתאמה וההפחתות בנכס ברחוב התמר {n}?"


def adjustment_turn(db, client, monkeypatch, calls, key: str, n: str, email: str):
    o = world14(db, client, monkeypatch, calls, (key,), email)
    ids = [f"{key}.coef", f"{key}.location", f"{key}.reduction"]
    reduction = DATUM14[ids[2]]["shown"]
    body = ("- מקדם הפחתה בשל שיתוף במקרקעין הוא 0.9 [V1]\n- מקדם מיקום הוא 1.1 [V2]\n"
            f"- ההפחתה בשל שיתוף במקרקעין היא {reduction} [V3]")
    return o, sequence14(o, ADJUSTMENT_Q.format(n=n), ids, body, f"התמר {n}")


def test_ae6_a_table_coefficient_and_the_texts_reduction_for_the_same_consideration_are_one_row_with_the_derived_computation(
        db, client, monkeypatch, calls, live):
    o, t = adjustment_turn(db, client, monkeypatch, calls, "adjust_match", "11", "admin-r14u9a@example.test")
    a = t.answer
    kept_whole(o, t)
    (block,) = a["time"]["breakdown"]
    assert [r["sid"] for r in block["rows"]] == ["V1", "V2"], "the reduction is no row of its own"
    row = block["rows"][0]
    assert row["value_text"] == "0.9" and row["reduction"]["sid"] == "V3" and row["reduction"]["value_text"] == "10"
    assert row["marks"] == [] and "conflict_with" not in row and "undecided_with" not in row
    (c,) = a["computations"]
    assert row["derived"] == c["id"] and c["result_kind"] == "derived" and c["expression"] == "1 − V1" and c["value"] == "0.1"
    assert [i["id"] for i in c["inputs"]] == ["V1"] and "conflict" not in json.dumps(a["time"], ensure_ascii=False)
    # the reduction's own facet is the coefficient's consideration, which is why they are one adjustment
    values = {v["id"]: v for v in a["values"]}
    assert values["V3"]["facet"]["written"] in values["V1"]["facet"]["written"]
    after_turn(o, t, live)


def test_ae6_a_reduction_of_15_percent_against_the_coefficient_0_9_of_the_same_consideration_is_a_conflict_naming_each_other(
        db, client, monkeypatch, calls, live):
    o, t = adjustment_turn(db, client, monkeypatch, calls, "adjust_conflict", "13", "admin-r14u9b@example.test")
    a = t.answer
    rows = rows_in(a["time"]["breakdown"])
    assert set(rows) == {"V1", "V2", "V3"} and "reduction" not in rows["V1"]
    assert rows["V1"]["marks"] == ["conflict"] and rows["V1"]["conflict_with"] == ["V3"]
    assert rows["V3"]["marks"] == ["conflict"] and rows["V3"]["conflict_with"] == ["V1"]
    assert rows["V2"]["marks"] == [] and a["computations"] == [], "nothing reconciles: no derived computation"
    assert "מקדם הפחתה בשל שיתוף במקרקעין" in (rows["V1"]["facet"], rows["V3"]["facet"]) or rows["V3"]["facet"] == "שיתוף במקרקעין"
    after_turn(o, t, live)


# --- AE10: a partial mark opens the detail of the claim it marks -----------------------------------------------------------------

def test_ae10_a_claim_marked_verified_in_part_has_a_partials_row_a_mark_in_the_text_and_a_diagnostics_entry_with_its_detail(
        db, client, monkeypatch, calls, live):
    o = world13(db, client, monkeypatch, calls, ("land_table",))
    ans = answer_of("\n".join([BULLET_1, BULLET_2]), "הצבעוני 14")
    steps = [search_for(o, "מקדם הפחתה", ("land_table",), LAND), registering(o, LAND), ans, ans, ans]
    t = ask13(o, LAND_Q, steps, judge=second_bullet_partial_judge)
    a = t.answer
    v = a["verification"]
    assert v["partial"] == 1 and v["removed"] == 0 and "0.9" in a["markdown"]
    # the partials row, the mark in the text and the diagnostics entry are one claim, paired by its id
    (row,) = v["partials"]
    (span,) = [s for s in a[SERVER_SPANS_FIELD] if s["kind"] == "partial_mark"]
    assert span["ordinal"] == row["ordinal"] == 1 and span["claim_ids"] == row["claim_ids"] and row["claim_id"] == row["claim_ids"][0]
    log = diagnostics_of(o, t)["removed"]
    (entry,) = [e for e in log if e["claim_id"] == row["claim_id"]]
    assert entry["action"] in ("qualified", "cut")
    assert entry["supported"] and entry["not_verified"], "what was verified and what was not"
    assert entry["why"]["check"] and set(entry["checked"]) == {"sources", "rereads"}, "why, and what was checked"
    assert entry["checked"]["sources"], "the sources the check read"
    # the owner may open it, and the value's row says the claim is partial
    assert client.get(f"/api/chat/messages/{t.message['id']}").json()["can_view_diagnostics"] is True
    assert rows_in(a["time"]["breakdown"])["V2"]["state"]["claim"]["result"] == "partial"
    # after a reload the mark, the row and the entry are the same
    stored = after_turn(o, t, live, exhausted=False)
    assert stored["verification"]["partials"] == v["partials"]
    assert [s for s in stored[SERVER_SPANS_FIELD] if s["kind"] == "partial_mark"] == [span]
    assert [e for e in diagnostics_of(o, t)["removed"] if e["claim_id"] == row["claim_id"]] == [entry]


# --- AE11: "current" describing the document is no recency claim ------------------------------------------------------------------

OLDEST = "1,950,000 ₪ כולל מע״מ"  # the 2022 appraisal's value: V3 of the timeline office, an older value than the latest


def older_value_answer(words: str) -> dict:
    return final(f"{words} הוא {OLDEST} [V3].", focus=_focus_of(),
                 claims=[{"text": f"{words} הוא {OLDEST}", "source_ids": ["V3"], "basis": "explicit"}])


def recency_turn(client, partial, monkeypatch, words: str, insists: bool):
    steps = timeline_agent(partial, ["y2025", "y2024", "y2022"], older_value_answer(words))
    if insists:
        steps.steps += [older_value_answer(words)] * 2  # the model keeps the wording through the repair rounds
    m = ask_timeline(client, partial, monkeypatch, steps)
    return m, steps


@pytest.mark.parametrize("words", ["בחוות הדעת הנוכחית השווי", "בשומה הנוכחית השווי"])
def test_ae11_current_describing_the_document_raises_no_recency_problem_under_partial_coverage(
        client, partial, monkeypatch, words):
    m, steps = recency_turn(client, partial, monkeypatch, words, insists=False)
    a = m["answer"]
    assert a["time"]["coverage"]["complete"] is False, "the coverage is partial: the recency check is armed"
    assert "recency_claim" not in json.dumps(m_diagnostics(client, m), ensure_ascii=False)
    assert a["markdown"] == f"{words} הוא {OLDEST} [V3]." and a["verification"]["removed"] == 0
    assert client.get(f"/api/chat/messages/{m['id']}").json()["answer"]["markdown"] == a["markdown"]
    no_extra_model_calls(m, steps)


@pytest.mark.parametrize("words", ["בשומה האחרונה השווי", "השווי העדכני"])
def test_ae11_the_latest_appraisal_and_the_latest_value_still_raise_the_recency_problem(client, partial, monkeypatch, words):
    m, steps = recency_turn(client, partial, monkeypatch, words, insists=True)
    assert "recency_claim" in json.dumps(m_diagnostics(client, m), ensure_ascii=False)
    assert "1,950,000" in m["answer"]["markdown"] and m["answer"]["verification"]["removed"] == 0, "supported content is marked, not removed"
    no_extra_model_calls(m, steps, exhausted=False)


# --- AE7: a cached value with no component is read again when a later turn takes it ----------------------------------------------

def cached_rows(o) -> list:
    with tenant_tx(o.system) as conn:
        return conn.execute(text("SELECT locator, value FROM verified_values ORDER BY created_at, locator::text")).all()


@pytest.mark.parametrize("stale", [datum.none, datum.not_read], ids=["none", "not_read"])
def test_ae7_a_cached_value_whose_component_state_is_none_or_not_read_is_read_again_on_a_later_turn_and_the_row_is_completed(
        db, client, monkeypatch, calls, live, stale):
    o = world14(db, client, monkeypatch, calls, ("context_reader",))
    ids = ["r14.same_block", "r14.own_phrase"]
    body = "; ".join(reduction_line(i, n) for n, i in enumerate(ids, 1)) + "."
    first = sequence14(o, "מהם מקדמי ההפחתה בנכס ברחוב הרימון 7?", ids, body, "הרימון 7")
    assert rows_in(first.answer["time"]["breakdown"])["V1"]["state"]["component"]["written"] == "עוצמת ניצול"
    before = cached_rows(o)
    assert len(before) == 2 and all(r.value["record"]["facet"]["status"] == "stated" for r in before)
    # an earlier build cached the first value read without its context: the record says none (or not read) and names no reader
    sentence = DATUM14["r14.same_block"]["where"]["quote"].rstrip(".")
    with tenant_tx(o.system) as conn:
        conn.execute(text("UPDATE verified_values SET value = jsonb_set(value #- '{record,facet_reader}', '{record,facet}',"
                          " CAST(:f AS jsonb)) WHERE value->'record'->>'quote' = :q"), {"f": json.dumps(stale()), "q": sentence})
    (old,) = [r for r in cached_rows(o) if r.value["record"]["quote"] == sentence]
    assert old.value["record"]["facet"]["status"] in ("none", "not_read") and "facet_reader" not in old.value["record"]

    # a later turn takes it from the cache (Q#), and the unit's other value afresh
    def cached(items):
        q = re.search(r"\b(Q\d+)\b", [i["output"] for i in items if isinstance(i, dict) and i.get("type") == "function_call_output"][-2])
        empty = {"table": None, "row": None, "row_number": None, "column": None, "column_number": None, "quote": None, "number": None}
        base = ADOPTED | {"kind": "coefficient", "unit": "percent", "subject": "הנכס"}
        return [call("take_value", source=q.group(1), locator=empty, meaning=base, label="r14.same_block"),
                take14(o, "r14.own_phrase", items)]

    doc = o.ids["context_reader"]
    steps = [[call("find_measurements", query="בהתאם לאמור לעיל", metric_kinds=None, document_ids=[doc], value_roles=None, page=None),
              *search14(o, ["r14.own_phrase"])], cached, answer_of(body, "הרימון 7", "coefficient", "percent")]
    second = ask13(o, "מהם מקדמי ההפחתה בנכס ברחוב הרימון 7?", steps)
    a = second.answer
    taken = [c for c in calls if c.name == "take_value"][-2]
    assert "Q" in taken.args["source"] and taken.output.startswith("V1"), taken.output
    rows = rows_in(a["time"]["breakdown"])
    component = rows["V1"]["state"]["component"]
    assert (component["status"], component["written"], component["part"]) == ("stated", "עוצמת ניצול", "preceding_sentence")
    assert rows["V1"]["facet"] == "עוצמת ניצול" and "עוצמת ניצול" in taken.output
    # the record was completed in place: the same row, only its facet and its reader changed; nothing was added or deleted
    after = cached_rows(o)
    assert len(after) == 2
    (done,) = [r for r in after if r.value["record"]["quote"] == sentence]
    assert done.locator == old.locator and done.value["record"]["facet"]["status"] == "stated"
    assert done.value["record"]["facet_reader"] == T.FACET_READER_VERSION
    changed = {k for k in set(done.value["record"]) | set(old.value["record"]) if done.value["record"].get(k) != old.value["record"].get(k)}
    assert changed == {"facet", "facet_reader"}
    kept_whole(o, second)
    after_turn(o, second, live)


# --- AE8: a cell of a picture says its context was not read ----------------------------------------------------------------------

def test_ae8_a_value_read_from_the_text_of_a_picture_says_its_context_was_not_read_never_that_the_source_states_no_component(
        db, client, monkeypatch, calls, live):
    """The listing path: two stored rate measurements of one appraisal, one in a paragraph (its sentence names its cash flow) and one
    in the text of a picture (a bare sentence, whose neighbours are not what it was read with), both cited by their M#."""
    o = bare_world(db, client, monkeypatch, calls, "admin-r14u9c@example.test")
    stated, picture = "שיעור היוון של 6.5% לתזרים דמי שכירות ממשרדים (31/03/2024).", "שיעור היוון 9% (31/03/2024)."
    doc = unit_appraisal(o, "r14-picture", "עיר-סינתטית", "31/03/2024", "1,000,000", street="הכלנית", number="8", block="9102",
                         parcel="12", extra=[stated, picture])
    with tenant_tx(o.system) as conn:
        ver = conn.execute(text("SELECT id FROM document_versions WHERE document_id = :d"), {"d": doc}).scalar_one()
        blocks = {t: i for i, t in conn.execute(text("SELECT block_index, text FROM document_blocks WHERE document_id = :d"),
                                                {"d": doc}).all()}
        conn.execute(text("UPDATE document_blocks SET kind = 'image' WHERE document_id = :d AND text = :t"), {"d": doc, "t": picture})
        for quote, shown, amount in ((stated, "6.5%", 6.5), (picture, "9%", 9)):
            conn.execute(text(
                "INSERT INTO measurements (office_id, document_id, version_id, block_index, statement_key, metric, metric_kind,"
                " value, value_form, value_text, unit, period, vat, subject_role, value_role, quote, effective_date,"
                " extraction_version, model, status, issues) VALUES (app_office(), :d, :v, :bi, :sk, 'שיעור היוון', 'rate', :val,"
                " 'exact', :vt, 'percent', 'none', 'unknown', 'appraised_property', 'appraiser_determination', :q, '2024-03-31',"
                " :e, 'test', 'auto_validated', '[]'::jsonb)"),
                {"d": doc, "v": ver, "bi": blocks[quote], "sk": f"b{blocks[quote]}:x", "val": amount, "vt": shown, "q": quote,
                 "e": EXTRACTION_VERSION})

    def answer(items):
        listing_ = [i["output"] for i in items if isinstance(i, dict) and i.get("type") == "function_call_output"][-1]
        ids = {shown: re.search(rf"(M\d+): [^\n]*= {re.escape(shown)}", listing_).group(1) for shown in ("6.5%", "9%")}
        return answer_of(f"שיעור היוון 6.5% [{ids['6.5%']}]; שיעור היוון 9% [{ids['9%']}].", "הכלנית 8", "rate", "percent", [doc])

    steps = [[call("find_measurements", query="שיעור היוון", metric_kinds=["rate"], document_ids=[doc], value_roles=None, page=None)],
             answer]
    t = ask13(o, "מהם שיעורי ההיוון בנכס ברחוב הכלנית 8?", steps)
    a = t.answer
    kept_whole(o, t)
    rows = rows_in(a["time"]["breakdown"])
    (pic,) = [r for r in rows.values() if r["value_text"] == "9%"]
    (ok,) = [r for r in rows.values() if r["value_text"] == "6.5%"]
    state = pic["state"]["component"]
    assert (state["status"], state["reason"], state["written"]) == ("not_read", "picture_cell", None) and pic["state"]["reason"] == "picture_cell"
    assert pic["facet"] == timeline.FACET_TEXT[("not_read", "picture_cell")]
    assert pic["facet"] not in (timeline.MSG_NO_FACET, timeline.FACET_TEXT[("none", "not_in_range")]), "not read is never not stated"
    assert ok["state"]["component"]["status"] == "stated" and ok["facet"] == "תזרים דמי שכירות ממשרדים"
    # the payload's measurement carries the same state, and the listing the model read did not say the component is missing
    assert {m["value_text"]: m["facet"]["reason"] for m in a["measurements"] if m["facet"]["status"] != "stated"} == {"9%": "picture_cell"}
    assert timeline.MSG_NO_FACET not in [c for c in calls if c.name == "find_measurements"][0].output
    after_turn(o, t, live)
