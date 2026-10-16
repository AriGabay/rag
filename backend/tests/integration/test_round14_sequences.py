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

import pytest
from sqlalchemy import text

from app.chat import timeline
from app.chat import tools as T
from app.db import tenant_tx
from tests.integration.test_chat_inspect import emp
from tests.integration.test_chat_timeline import ADOPTED
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
from tests.integration.test_round12_sequences import bare_world
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
from tests.support.scripted_agent import call

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
    # (d) after a refresh the same rows
    stored = refreshed_equals_live(o, t, live)
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
    stored = refreshed_equals_live(o, t, live)
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


# --- a component read from an earlier sentence: its location, quote and resolved anchor ------------------------------------------

MANIFEST14 = json.loads((FIXTURES / "round14" / "manifest.json").read_text(encoding="utf-8"))
DOC14 = MANIFEST14["documents"]["context_reader"]
DATA14 = {d["id"]: d for d in DOC14["data"]}


def plot_hits(items: list) -> list[dict]:
    """The search hits the model has seen, as attributes (a hit that repeats an earlier one is self-closing: ``same_as``)."""
    out = []
    for it in items:
        if isinstance(it, dict) and it.get("type") == "function_call_output":
            for m in re.finditer(r"<source ([^>]*?)/?>", it["output"]):
                out.append(dict(re.findall(r'(\w+)="([^"]*)"', m.group(1))))
    return out


def take14(o, datum_id: str, items: list) -> dict:
    d = DATA14[datum_id]
    quote = d["where"]["quote"].rstrip(".")
    locator = {"table": None, "row": None, "row_number": None, "column": None, "column_number": None, "quote": quote,
               "number": re.sub(r"[^\d.,]", "", d["shown"])}
    section = d["where"]["section"]  # the same sentence is in several plots: the hit is the one of the datum's section
    source = next(h["id"] for h in plot_hits(items) if section in h.get("location", "") and "same_as" not in h)
    return call("take_value", source=source, locator=locator, meaning=ADOPTED | {"kind": "coefficient", "unit": "percent",
                                                                               "subject": "הנכס"}, label=datum_id)


def test_a_component_read_from_an_earlier_sentence_carries_its_location_quote_and_resolved_anchor_never_the_stub(
        db, client, monkeypatch, calls, live):
    o = bare_world(db, client, monkeypatch, calls, "admin-r14u6@example.test")
    o.ids["ctx14"] = upload(o, FIXTURES / DOC14["file"], DOC14["source_kind"], monkeypatch)
    pair = ["r14.same_block", "r14.own_phrase"]
    steps = [[call("search", query=DATA14[i]["where"]["quote"].rstrip("."), document_ids=[o.ids["ctx14"]], limit=None) for i in pair],
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
    assert ctx["anchor"]["document_id"] == o.ids["ctx14"] and ctx["anchor"]["precision"] == "structured"
    assert ctx["quote"] == "השמאי לא נתן הפחתה בגין עוצמת ניצול.", "the sentence the snapshot highlights"
    assert (second["status"], second["tier"], second["context"]) == ("stated", "own", None)
    assert "'at'" not in str(a["time"]) and '"at"' not in json.dumps(a["time"], ensure_ascii=False), "the raw stub is never sent"
    refreshed_equals_live(o, t, live)


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
