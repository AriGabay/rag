"""Round 14 U1 sequences, end to end with a scripted model (R6, R15, R16; AE4): one datum per source span.

The synthetic round-13 capitalization fixture (rates by cash flow in one section, read by the real worker) goes through the whole
turn with a scripted agent that registers the same quote and number more than once, and once with a quote the source does not
hold. What is proven is what the server decides, stores and shows, never wording:

- the tool: a repeated registration returns the first V# and says so, and creates no second value or source;
- the answer: the components table holds one row for the number, and the answer read back after the turn equals the live one;
- the diagnostics: a refused ``take_value`` is recorded with its reason, source and clipped place (``resolution.registrations``),
  and a turn whose registrations all succeeded has no such record.

Every name, address and amount is invented."""

# ruff: noqa: F811 - the tests take the imported fixtures by name, as the round-13 sequences do
from __future__ import annotations

import json

import pytest

from app.chat import timeline
from app.chat import tools as T
from tests.integration.test_chat_inspect import emp
from tests.integration.test_chat_value_status import (
    _ocr_sees_the_table,
    _uncertain_table,
    vision_office,  # noqa: F401 - the office with a scripted inspect reader (the fixture)
)
from tests.integration.test_round10_sequences import (
    calls,  # noqa: F401 - the recorded tool calls (the fixture)
)
from tests.integration.test_round10_sequences import (
    no_models as no_models,  # noqa: F401 - the autouse inline-run setup
)
from tests.integration.test_round13_sequences import (
    answer_of,
    ask13,
    hit_with,
    kept_whole,
    listing,
    live,  # noqa: F401 - the payload the turn built (the fixture)
    refreshed_equals_live,
    rows_in,
    search_for,
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
