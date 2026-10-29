"""Round 25 U4 (R1, R7, R11; KTD7, KTD8): the completeness record in a stored turn's diagnostics, through ``GET /api/chat/messages/{id}/diagnostics``.

A scripted turn (the sibling turn of ``test_chat_completeness``: a request for a set of rates, a lenient judge that votes ``full`` and assigns the
candidates it is offered, an answer that gives three of four rates). With a detection setting on, the component's decision in ``requirements_trace``
names the units and the sources they cite, the open item that capped it (id, label prefix, source, kind ``sibling``) and the anchor sources; the item
in ``resolution.item_ledger`` carries the gap cause code its line was written with. With the setting off the stored records have none of these keys.
Ids, codes and short label prefixes only: the open item's value is in no record. Synthetic documents only."""

from __future__ import annotations

import json
import re

import pytest

from app.chat.verify import COMPLETENESS_LABEL_CHARS
from tests.integration.test_chat_completeness import (
    _sibling_turn,
)
from tests.integration.test_chat_completeness import (
    office as office,  # noqa: F401 - the synthetic office with its document
)

pytestmark = pytest.mark.db

# ruff: noqa: F811 - the tests take the imported ``office`` fixture by name

NEW_KEYS = {"open_items", "anchors", "counts"}


def _stored(client, m) -> dict:
    d = client.get(f"/api/chat/messages/{m['id']}/diagnostics")
    assert d.status_code == 200
    return d.json()


def _decision(d: dict, first: bool = False) -> dict:
    return d["requirements_trace"][0 if first else -1]["components"][0]["decision"]


def test_the_stored_decision_names_the_units_the_open_item_that_capped_it_and_the_anchor_sources(client, office, monkeypatch):
    m, _ = _sibling_turn(client, office, monkeypatch, on=True)
    assert m["status"] == "done", m
    d = _stored(client, m)
    decision = _decision(d, first=True)  # (the round that made the offer: a later round holds the member as an item and offers nothing)
    assert decision["status"] == "partial" and decision["caps"] == ["omitted_items"]
    assert decision["units"] and decision["cites"] == ["S1"]
    (item,) = decision["open_items"]
    assert re.fullmatch(r"I\d+", item["id"]) and item["source"] == "S1" and item["kind"] == "sibling"
    assert "למחסנים" in item["label"] and len(item["label"]) <= COMPLETENESS_LABEL_CHARS and "9.1" not in item["label"]
    assert decision["anchors"] == {"sources": ["S1"], "count": 1, "sources_left": 0}
    assert decision["counts"] == {"units": len(decision["units"]), "cites": 1, "open_items": 1, "anchors": 1}
    # the final round, which decided the saved text, still names the item that stays open
    assert [i["id"] for i in _decision(d)["open_items"]] == [item["id"]]


def test_the_stored_item_carries_the_gap_cause_code_its_line_was_written_with(client, office, monkeypatch):
    m, _ = _sibling_turn(client, office, monkeypatch, on=True)
    d = _stored(client, m)
    (item,) = [i for i in d["resolution"]["item_ledger"]["items"] if i["state"] == "omitted"]
    assert item["gap_cause"] == "not_completed_settled"  # the same code as the line (``test_chat_completeness``: no repair round was left)
    assert item["id"] == _decision(d)["open_items"][0]["id"]
    assert m["answer"]["markdown"].rstrip(".").endswith("לא הושלם כי לא נותר סבב תיקון נוסף")
    assert item["gap_cause"].isascii() and " " not in item["gap_cause"]  # a code, not prose


def test_the_record_holds_the_label_prefix_and_never_the_value_of_the_open_item(client, office, monkeypatch):
    m, _ = _sibling_turn(client, office, monkeypatch, on=True)
    d = _stored(client, m)
    record = json.dumps({"decision": _decision(d, first=True), "last": _decision(d), "causes": [i.get("gap_cause") for i in d["resolution"]["item_ledger"]["items"]]},
                        ensure_ascii=False)
    assert "9.1" not in record and "9,1" not in record  # the label is there, the open member's rate is not
    assert "שיעור היוון למחסנים" in record


def test_with_the_setting_off_the_stored_records_have_none_of_the_keys(client, office, monkeypatch):
    m, _ = _sibling_turn(client, office, monkeypatch, on=False)
    assert m["status"] == "done", m
    d = _stored(client, m)
    for round_ in d["requirements_trace"]:
        for component in round_["components"]:
            decision = component["decision"]
            assert not NEW_KEYS & set(decision) and "units" not in decision and "cites" not in decision
    items = (d["resolution"].get("item_ledger") or {}).get("items") or []
    assert all("gap_cause" not in i for i in items)
