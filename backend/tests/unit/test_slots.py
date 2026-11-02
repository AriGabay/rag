"""The pending slot of a clarification, version 3 (round 26 U3; KTD3): the record the conversation stores for a structured clarification,
the slot's reader for a version-2 row, and the answered record a continuation leaves for a correction. Pure data, no database."""

from __future__ import annotations

from app.chat import slots

RECORD = {"id": "c1", "question": "מי?", "reason": "", "kind": "unit", "allow_all": False, "original_question": "מה השטח?",
          "options": [{"id": "a", "label": "א", "detail": "", "ref": {"document_id": "d1"}},
                      {"id": "b", "label": "ב", "detail": "", "ref": {"document_id": "d2"}}]}


def test_the_version_is_three_and_a_clarification_record_is_a_pending_kind():
    assert slots.VERSION == 3
    pending = {"kind": "clarification", "payload": RECORD | {"candidates": []}, "asked_at": "2026-11-02T10:00:00"}
    out = slots.carry(None, intent="property", pending=pending, message_id="m-1")
    assert out["version"] == 3 and out["pending"] == pending
    assert slots.clarification_of(out) == pending["payload"]


def test_a_parameter_or_candidates_pending_is_no_clarification_record():
    assert slots.clarification_of({"pending": {"kind": "parameter", "payload": {}}}) is None
    assert slots.clarification_of({"pending": {"kind": "candidates", "payload": {"candidates": []}}}) is None
    assert slots.clarification_of({"pending": None}) is None and slots.clarification_of(None) is None


def test_a_version_two_context_is_read_as_version_three_with_its_pending_as_it_was():
    old = {"version": 2, "topic": None, "lists": [], "tombstones": [], "property": None, "params": None, "period": None,
           "sources": [], "pending": {"kind": "candidates", "payload": {"candidates": [{"document_id": "d1", "title": "א"}]},
                                      "asked_at": None}}
    new = slots.upgrade_v2(old)
    assert new["version"] == 3 and new["pending"] == old["pending"] and new["lists"] == []
    assert slots.upgrade_v1({"version": 1, "topic": {}, "sources": []})["version"] == 3


def test_the_answered_record_lives_for_the_next_message_only():
    answered = {"record": RECORD, "chosen": [{"kind": "unit", "id": "a", "label": "א", "detail": "", "ref": {}}]}
    out = slots.carry(None, intent="property", message_id="m-1", answered=answered)
    assert out["answered"] == answered
    assert not slots.carry(out, intent="property", message_id="m-2").get("answered")
