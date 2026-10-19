"""The end of a turn rechecks permissions (AE7): an answer whose document the user lost while it was being prepared
is not stored as answered, then hidden; the turn fails with a specific message, and its model calls are still
logged. The model is scripted; synthetic text only."""

from __future__ import annotations

import hashlib
import json
import re

import pytest
from sqlalchemy import text

from app.chat import api as chat_api
from app.chat import verify
from app.chat.compose import SERVER_SPANS_FIELD
from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_search import add_chunks
from tests.support.scripted_agent import ScriptedAgent, call, final

pytestmark = pytest.mark.db

MARKER = "מגדל-הדולב-5820"
SECRET = f"נספח: דמי הניהול במגדל {MARKER} הם 33 ₪ למ\"ר לחודש."
PERMISSIONS_CHANGED = "הרשאות המסמכים השתנו בזמן ההכנה; אפשר לשאול שוב."


@pytest.fixture
def setup(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    a = make_office(db, "משרד א", "admin-a@example.test")
    g2, g3 = make_group(a, "קבוצה 2"), make_group(a, "קבוצה 3")
    make_user(a, "emp@example.test", [g2])
    secret, _ = add_chunks(a, g2, [SECRET], "7" * 64)
    a.secret, a.g3 = str(secret), g3
    return a


def _revoke(office) -> None:
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE documents SET group_id = :g WHERE id = :d"), {"g": office.g3, "d": office.secret})


def _turn(client, setup, monkeypatch, revoke_at: int | None) -> dict:
    agent = ScriptedAgent([[call("search", query="דמי ניהול", document_ids=[setup.secret], limit=None)],
                           final(f"דמי הניהול במגדל {MARKER} הם 33 ₪ למ\"ר לחודש [S1].", documents=[setup.secret])])
    if revoke_at is not None:
        agent.on_step = lambda index: _revoke(setup) if index == revoke_at else None
    cloud(monkeypatch, setup, agent)
    login(client, "emp@example.test")
    return send(client, new_conversation(client), "מה דמי הניהול?")


def test_a_document_revoked_during_the_turn_fails_it_with_permissions_changed(client, setup, monkeypatch):
    m = _turn(client, setup, monkeypatch, revoke_at=1)  # after the search read it, before the answer
    assert m["status"] == "failed" and m["error"] == PERMISSIONS_CHANGED
    assert m["answer"] is None and m["content"] == "" and MARKER not in json.dumps(m, ensure_ascii=False)
    assert m["usage"], "the turn's model calls stay on the message"
    with tenant_tx(setup.system) as conn:
        row = conn.execute(text("SELECT status, answer, content FROM messages WHERE id = :m"), {"m": m["id"]}).one()
        assert row.status == "failed" and row.answer is None and not row.content
        assert conn.execute(text("SELECT count(*) FROM message_diagnostics WHERE message_id = :m"),
                            {"m": m["id"]}).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM provider_usage")).scalar_one() >= 2  # agent steps and judge


def test_an_answer_whose_documents_stay_visible_is_stored(client, setup, monkeypatch):
    m = _turn(client, setup, monkeypatch, revoke_at=None)
    assert m["status"] == "done" and MARKER in m["answer"]["markdown"]


# --- a correct sentence that cites nothing, beside one citing the table: kept, and cited by the server ---------------

STAGES = "טבלת שלבים במגדל-הדולב: שלב א — סף 40 נקודות, יחס 0.62, עבר. שלב ב — סף 55 נקודות, יחס 0.71, עבר."


def _stage_judge(input: str) -> dict:
    """Supported; the unit citing nothing is supported by the shown table (S1), as the judge policy asks."""
    units = re.findall(r'<unit index="(\d+)" cites="([^"]*)">', input)
    return {"verdicts": [{"index": int(i), "verdict": "supported", "reason": "בדיקה",
                          "supported_by": [] if cites else ["S1"]} for i, cites in units]}


def test_an_uncited_correct_sentence_is_kept_with_the_table_cited(client, db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    a = make_office(db, "משרד א", "admin-a@example.test")
    g = make_group(a, "קבוצה 2")
    make_user(a, "emp@example.test", [g])
    doc, _ = add_chunks(a, g, [STAGES], "8" * 64)
    agent = ScriptedAgent([[call("search", query="סף לכל שלב", document_ids=[str(doc)], limit=None)],
                           final("בשלב א הסף היה 40 נקודות [S1]. בשלב ב הסף היה 55 נקודות והיחס 0.71, ולכן עבר.",
                                 documents=[str(doc)])], judge=_stage_judge)
    cloud(monkeypatch, a, agent)
    login(client, "emp@example.test")
    m = send(client, new_conversation(client), "מה הסף בכל שלב?")
    assert m["status"] == "done"
    md = m["answer"]["markdown"]
    assert "והיחס 0.71, ולכן עבר [S1]." in md and "הוסרו" not in md


# --- what diagnostics keep of verification: only the units removed, each with its decision (round 7 U4, KTD5) -------

def _marked_judge(input: str) -> dict:
    """The rent sentence is partly supported with no concrete defect (kept, marked); the rest supported."""
    units = re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S)
    return {"verdicts": [{"index": int(i), "verdict": "partial" if "לחודש" in t else "supported", "reason": "בדיקה",
                          "defect": "none"} for i, t in units]}


def test_the_stored_diagnostics_hold_only_the_removed_units_each_with_its_decision(client, setup, monkeypatch):
    answer = final(f"דמי הניהול במגדל {MARKER} הם 33 ₪ למ\"ר לחודש [S1]. השטח הוא 812 מ\"ר [S1].",
                   documents=[setup.secret])
    agent = ScriptedAgent([[call("search", query="דמי ניהול", document_ids=[setup.secret], limit=None)],
                           answer, answer, answer], judge=_marked_judge)
    cloud(monkeypatch, setup, agent)
    login(client, "emp@example.test")
    m = send(client, new_conversation(client), "מה דמי הניהול והשטח?")
    assert m["status"] == "done" and "812" not in m["answer"]["markdown"], m
    v = m["answer"]["verification"]
    assert v["removed"] == 1 and v["partial"] == 1
    assert [r["failure_kind"] for r in v["removals"]] == ["absent_from_source"]
    with tenant_tx(setup.system) as conn:
        row = conn.execute(text("SELECT removed, rounds FROM message_diagnostics WHERE message_id = :m"),
                           {"m": m["id"]}).one()
    # round 8 U7 (R22): the marked sentence is logged too (qualified), before the removal — the log is in the text's order
    marked, decision = row.removed
    assert marked["action"] == "qualified" and decision["action"] == "removed"
    assert "812" in decision["text"] and decision["failure_kind"] == "absent_from_source"
    assert decision["checked_ids"] == ["S1"] and decision["repair_attempted"] is True
    assert any(p["severity"] == "partial" for p in row.rounds[-1])  # every problem stays in the rounds


# --- round 8 U7 (R17, R22-R25, KTD8): the digest, the claim-linked rows and the full per-claim log -----------------------

CLAUSE = "שנקבעו בוועדה"
MIXED = f"דמי הניהול במגדל {MARKER} הם 33 ₪ למ\"ר לחודש"


def _handled_judge(input: str) -> dict:
    """The first sentence is partly supported with no concrete defect (marked); a clause of the mixed sentence is
    unsupported (cut); the rest supported."""
    out = []
    for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S):
        if CLAUSE in t:
            out.append({"index": int(i), "verdict": "partial", "reason": "הוועדה אינה במקור", "defect": "part",
                        "claims": [{"span": MIXED, "verdict": "supported"},
                                   {"span": CLAUSE, "verdict": "unsupported", "failure": "absent_from_source"}]})
        else:
            out.append({"index": int(i), "verdict": "partial" if "לחודש" in t else "supported", "reason": "בדיקה",
                        "defect": "none"})
    return {"verdicts": out}


def _handled_turn(client, setup, monkeypatch, judge=_handled_judge) -> dict:
    """One answer with a cut claim (first), a removed unit (second) and a marked one (third)."""
    answer = final(f"{MIXED}, {CLAUSE} [S1]. השטח הוא 812 מ\"ר [S1]. דמי הניהול בנוסף הם 33 ₪ למ\"ר לחודש [S1].",
                   documents=[setup.secret])
    agent = ScriptedAgent([[call("search", query="דמי ניהול", document_ids=[setup.secret], limit=None)],
                           answer, answer, answer], judge=judge)
    cloud(monkeypatch, setup, agent)
    login(client, "emp@example.test")
    m = send(client, new_conversation(client), "מה דמי הניהול והשטח?")
    assert m["status"] == "done", m
    return m


def _diagnostics_row(setup, message_id: str):
    with tenant_tx(setup.system) as conn:
        return conn.execute(text("SELECT removed, rounds, document_ids FROM message_diagnostics WHERE message_id = :m"),
                            {"m": message_id}).one()


def _spy_outcomes(monkeypatch) -> list:
    """The turn's outcomes, as ``run_turn`` returned them (the digest in them is the final check's own)."""
    real, seen = chat_api.engine.run_turn, []

    def run_turn(*args, **kwargs):
        seen.append(real(*args, **kwargs))
        return seen[-1]

    monkeypatch.setattr(chat_api.engine, "run_turn", run_turn)
    return seen


def test_the_payload_digest_is_the_one_the_final_check_returned_for_the_saved_text_and_the_actions_are_counted(
        client, setup, monkeypatch):
    seen = _spy_outcomes(monkeypatch)
    m = _handled_turn(client, setup, monkeypatch)
    v = m["answer"]["verification"]
    with tenant_tx(setup.system) as conn:
        saved = conn.execute(text("SELECT content FROM messages WHERE id = :m"), {"m": m["id"]}).scalar_one()
    assert saved == m["content"] == m["answer"]["markdown"]
    (outcome,) = seen
    assert v["final_digest"] == outcome.final_digest  # stored from the final check, not recomputed from the saved text
    assert outcome.final_digest == hashlib.sha256(saved.encode("utf-8")).hexdigest()  # and it is the saved text's
    assert v["actions"] == {"removed": 1, "cut": 1, "rewritten": 0, "withheld": 0, "qualified": 1, "cited": 0}
    # after a refresh: the same text, the same digest
    again = client.get(f"/api/chat/messages/{m['id']}").json()
    assert again["content"] == saved and again["answer"]["verification"]["final_digest"] == v["final_digest"]


def test_a_text_that_is_not_the_one_the_final_check_verified_is_never_saved_as_an_answer(client, setup, monkeypatch):
    real = chat_api.engine.run_turn

    def run_turn(*args, **kwargs):
        out = real(*args, **kwargs)
        out.answer = out.answer.model_copy(update={"answer_markdown": out.answer.answer_markdown + " טענה שלא נבדקה."})
        return out

    monkeypatch.setattr(chat_api.engine, "run_turn", run_turn)
    answer = final(MIXED + " [S1].", documents=[setup.secret])
    agent = ScriptedAgent([[call("search", query="דמי ניהול", document_ids=[setup.secret], limit=None)], answer, answer,
                           answer])
    cloud(monkeypatch, setup, agent)
    login(client, "emp@example.test")
    m = send(client, new_conversation(client), "מה דמי הניהול?")
    assert m["status"] == "failed" and m["error"] == chat_api.FAILURE_TEXT["verify_unavailable"] + " אפשר לנסות שוב.", m
    assert not m.get("answer") and "שלא נבדקה" not in json.dumps(m, ensure_ascii=False)
    with tenant_tx(setup.system) as conn:
        row = conn.execute(text("SELECT status, content, answer FROM messages WHERE id = :m"), {"m": m["id"]}).one()
    assert row.status == "failed" and row.answer is None and not row.content


def test_the_rows_of_the_normal_payload_name_the_claim_the_action_and_the_marker_in_the_texts_order(
        client, setup, monkeypatch):
    m = _handled_turn(client, setup, monkeypatch)
    rows = m["answer"]["verification"]["removals"]
    # the cut claim stands before the removed unit in the answer, so its row does too
    assert [(r["action"], r["failure_kind"]) for r in rows] == [("cut", "absent_from_source"),
                                                              ("removed", "absent_from_source")]
    assert rows[0]["ordinal"] == 1 and "ordinal" not in rows[1]
    assert "«[1: חלק שלא אומת הושמט]»" in m["content"]
    assert all(re.fullmatch(r"k[0-9a-f]{12}", r["claim_id"]) for r in rows) and rows[0]["claim_id"] != rows[1]["claim_id"]
    # nothing but the fixed sentence of the kind reaches the normal path: not the draft, not a verdict, not an offset
    assert {k for r in rows for k in r} == {"claim_id", "action", "ordinal", "failure_kind", "component", "text"}
    assert [r["text"] for r in rows] == [verify.REMOVAL_SENTENCES["absent_from_source"]] * 2
    assert set(m["answer"]["verification"]) <= {"judged", "judge_status", "removed", "partial", "annotated",
                                                "request_mismatch", "conditional", "correctness", "completeness",
                                                "removals", "final_digest", "actions", "edited", "partials", "scoped"}


def test_every_handled_claim_has_a_log_entry_with_the_whole_record_and_the_rows_pair_with_it_by_claim_id(
        client, setup, monkeypatch):
    m = _handled_turn(client, setup, monkeypatch)
    rows = m["answer"]["verification"]["removals"]
    log = client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()["removed"]
    # the log is in the text's order: cut, removed, qualified
    assert [e["action"] for e in log] == ["cut", "removed", "qualified"]
    cut, removed, qualified = log
    needed = {"claim_id", "action", "text", "remaining", "verdict", "reason", "failure_kind", "cause", "check",
              "sources_checked", "sources", "rereads", "post_check"}
    assert all(needed <= set(e) for e in log), [needed - set(e) for e in log]
    assert cut["text"] == CLAUSE and cut["remaining"] == "«[1: חלק שלא אומת הושמט]»" and cut["ordinal"] == 1
    assert cut["verdict"] == "unsupported" and cut["failure_kind"] == "absent_from_source"
    assert "הוועדה אינה במקור" in cut["reason"]
    assert "812" in removed["text"] and removed["remaining"] == "" and removed["check"] == "unstated_number"
    assert removed["sources_checked"] == ["S1"] and [s["document_id"] for s in removed["sources"]] == [setup.secret]
    assert removed["sources"][0]["id"] == "S1" and "anchor" in removed["sources"][0]
    # the number was looked for in the rest of the source before the unit was given up (U5), and in the part of the
    # section before the passage (round 13): every attempt is logged
    assert [(x["source"], x["scope"], x["outcome"]) for x in removed["rereads"]] == [
        ("S1", "next_page", "not_found"), ("S1", "continuation", "not_found"), ("S1", "preceding", "not_found")]
    assert cut["rereads"] == [] and removed["post_check"] and cut["post_check"]
    assert qualified["text"].startswith("דמי הניהול בנוסף") and qualified["remaining"] == qualified["text"]
    # a summary finds its decision by claim id, whatever the order of the list
    by_id = {e["claim_id"]: e for e in reversed(log)}
    for r in rows:
        assert by_id[r["claim_id"]]["failure_kind"] == r["failure_kind"] and by_id[r["claim_id"]]["action"] == r["action"]
    # the same record is what is stored
    assert [e["claim_id"] for e in _diagnostics_row(setup, m["id"]).removed] == [e["claim_id"] for e in log]


def test_the_serialized_answer_holds_no_draft_text_and_its_registry_only_offsets_and_kinds(client, setup, monkeypatch):
    m = _handled_turn(client, setup, monkeypatch)
    spans = m["answer"][SERVER_SPANS_FIELD]
    # a mark also carries its ordinal and the claims it covers (round 14 U7, KTD9); no other span does
    assert spans and all(set(s) == ({"start", "end", "kind", "ordinal", "claim_ids"} if s["kind"] == "partial_mark"
                                    else {"start", "end", "kind"}) for s in spans)
    assert {s["kind"] for s in spans} >= {"marker", "partial_mark", "removal_note"}
    assert all(m["content"][s["start"]:s["end"]] for s in spans)
    shown = json.dumps(m["answer"], ensure_ascii=False)
    assert "812" not in shown and CLAUSE not in shown and CLAUSE not in m["content"]
    # no claim list, no verdict, no offsets of claims
    assert m["answer"]["claims"] == []
    assert not {"claim_records", "verdict", "citation", "sources_checked", "rereads", "post_check"} & (
        set(m["answer"]) | set(m["answer"]["verification"]))


def test_claim_ids_of_two_messages_with_the_same_text_differ(client, setup, monkeypatch):
    answer = final(f"{MIXED}, {CLAUSE} [S1]. השטח הוא 812 מ\"ר [S1].", documents=[setup.secret])
    search = [call("search", query="דמי ניהול", document_ids=[setup.secret], limit=None)]
    agent = ScriptedAgent([search, answer, answer, answer, search, answer, answer, answer], judge=_handled_judge)
    cloud(monkeypatch, setup, agent)
    login(client, "emp@example.test")
    first = send(client, new_conversation(client), "מה דמי הניהול והשטח?")
    second = send(client, new_conversation(client), "מה דמי הניהול והשטח?")
    ids = [[r["claim_id"] for r in m["answer"]["verification"]["removals"]] for m in (first, second)]
    assert len(ids[0]) == len(ids[1]) == 2 and not set(ids[0]) & set(ids[1])


def test_a_message_stored_before_claim_rows_and_the_registry_still_serializes(client, setup, monkeypatch):
    m = _handled_turn(client, setup, monkeypatch)
    with tenant_tx(setup.system) as conn:
        old = conn.execute(text("SELECT answer FROM messages WHERE id = :m"), {"m": m["id"]}).scalar_one()
        old.pop(SERVER_SPANS_FIELD)
        old["claims"] = [{"text": "דמי הניהול", "source_ids": ["S1"], "basis": "explicit"}]
        old["verification"] = {"judged": True, "judge_status": "ok", "removed": 1, "removals": [
            {"failure_kind": "absent_from_source", "component": None, "text": "x"}]}
        conn.execute(text("UPDATE messages SET answer = CAST(:a AS jsonb) WHERE id = :m"),
                     {"a": json.dumps(old, ensure_ascii=False), "m": m["id"]})
        conn.execute(text("UPDATE message_diagnostics SET removed = CAST(:r AS jsonb) WHERE message_id = :m"),
                     {"r": json.dumps([{"text": "השטח הוא 812", "reason": "r", "failure_kind": "absent_from_source"}]),
                      "m": m["id"]})
    shown = client.get(f"/api/chat/messages/{m['id']}").json()
    v = shown["answer"]["verification"]
    assert v["removals"] == [{"failure_kind": "absent_from_source", "component": None,
                              "text": verify.REMOVAL_SENTENCES["absent_from_source"]}]
    assert "final_digest" not in v and "actions" not in v and shown["answer"]["claims"]  # its own, as stored
    assert client.get(f"/api/chat/messages/{m['id']}/diagnostics").status_code == 200


# --- round 14 U7 (R14, KTD9, AE10): a partial mark pairs with one diagnostics entry and opens its detail ------------------

def test_a_partial_mark_pairs_with_one_diagnostics_entry_by_claim_id_and_the_normal_path_has_only_the_class_sentence(
        client, setup, monkeypatch):
    m = _handled_turn(client, setup, monkeypatch)
    v = m["answer"]["verification"]
    marks = [s for s in m["answer"][SERVER_SPANS_FIELD] if s["kind"] == "partial_mark"]
    # one row per mark, in the text's order: the sentence the final check found only partly supported once its clause was
    # cut, and the marked rent sentence
    assert len(marks) == len(v["partials"]) == 2
    assert [s["ordinal"] for s in marks] == [row["ordinal"] for row in v["partials"]] == [1, 2]
    log = client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()["removed"]
    for span, row in zip(marks, v["partials"], strict=True):
        assert span["claim_ids"] == row["claim_ids"] and row["claim_id"] == row["claim_ids"][0]
        assert re.fullmatch(r"k[0-9a-f]{12}", row["claim_id"])
        assert {k for k in row} == {"ordinal", "claim_id", "claim_ids", "failure_kind", "check", "text"}
        # every mark pairs with exactly one diagnostics entry per claim id, and the entry states the detail
        for cid in row["claim_ids"]:
            (entry,) = [e for e in log if e["claim_id"] == cid]
            assert entry["action"] in ("qualified", "cut")
            assert entry["supported"] and entry["not_verified"] and set(entry["checked"]) == {"sources", "rereads"}
            assert entry["checked"]["sources"] == ["S1"] and entry["why"]["check"]
    rent = v["partials"][1]
    assert rent["text"] == verify.PARTIAL_SENTENCES["unspecified"] and rent["failure_kind"] is None
    assert rent["check"] == "judge"
    (qualified,) = [e for e in log if e["claim_id"] == rent["claim_id"]]
    assert qualified["supported"].startswith("דמי הניהול בנוסף") and qualified["why"] == {
        "failure_kind": None, "check": "judge"}
    assert "דמי הניהול בנוסף" not in json.dumps(v, ensure_ascii=False)  # no draft text
    # the owner may open the detail; the entries of the stored record are the ones served
    assert m["can_view_diagnostics"] is True
    assert [e["claim_id"] for e in _diagnostics_row(setup, m["id"]).removed] == [e["claim_id"] for e in log]


def test_another_office_user_is_refused_the_diagnostics_of_a_message_with_partial_marks(client, setup, monkeypatch):
    m = _handled_turn(client, setup, monkeypatch)
    assert m["answer"]["verification"]["partials"] and m["can_view_diagnostics"] is True
    make_user(setup, "emp2@example.test", [setup.g3])
    login(client, "emp2@example.test")
    assert client.get(f"/api/chat/messages/{m['id']}/diagnostics").status_code == 404
    assert client.get(f"/api/chat/messages/{m['id']}").status_code == 404  # not theirs: nothing, so no flag either


def test_a_message_stored_before_partial_rows_reads_with_none_and_without_error(client, setup, monkeypatch):
    m = _handled_turn(client, setup, monkeypatch)
    with tenant_tx(setup.system) as conn:
        old = conn.execute(text("SELECT answer FROM messages WHERE id = :m"), {"m": m["id"]}).scalar_one()
        old["verification"].pop("partials")
        for span in old[SERVER_SPANS_FIELD]:
            span.pop("ordinal", None), span.pop("claim_ids", None)
        conn.execute(text("UPDATE messages SET answer = CAST(:a AS jsonb) WHERE id = :m"),
                     {"a": json.dumps(old, ensure_ascii=False), "m": m["id"]})
    shown = client.get(f"/api/chat/messages/{m['id']}")
    assert shown.status_code == 200
    assert "partials" not in shown.json()["answer"]["verification"]
    assert shown.json()["answer"]["verification"]["partial"] == 2
