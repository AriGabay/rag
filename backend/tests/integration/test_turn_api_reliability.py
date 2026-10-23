"""Turn API reliability (KTD13): abandoned reservations, turn ids scoped to their conversation, and a state
conflict that must not re-apply a clarification answer to another pending clarification.

No model is called: every turn here runs the rules path in limited/demo mode."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.answering import api, turn
from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_dedup import add_version, publish
from tests.integration.test_numeric_answers import AE3, approve_all, row

pytestmark = pytest.mark.db

ADMIN = "admin-a@example.test"
PREVIOUS_YEAR = "ומה לגבי השנה הקודמת?"
AMBIGUOUS = "מה מחיר למ״ר בחרוזים?"  # asks the data_kind clarification
DATA_KIND = {"key": "data_kind", "value": "transaction_price"}


@pytest.fixture
def records(db):
    """Verified transactions in חרוזים: two in 2024, one each in 2023 and 2022."""
    a = make_office(db, "משרד א", ADMIN)
    rows = AE3 + [row("הגפן 3", "6158/3", "05/05/2023", "80", "2,000,000"),
                  row("הגפן 4", "6158/4", "06/06/2022", "100", "1,800,000")]
    a.doc, ver = add_version(a, a.default_group_id, rows, "1" * 64)
    publish(a, a.doc, ver)
    approve_all(a)
    return a


def post(client, question=None, conversation_id=None, *, turn_id=None, clarification=None, status=200):
    body = {"question": question, "conversation_id": conversation_id, "turn_id": turn_id,
            "clarification": clarification}
    r = client.post("/api/ask", json={k: v for k, v in body.items() if v is not None})
    assert r.status_code == status, r.text
    return r.json()


def scalar(office, sql, **params):
    with tenant_tx(office.system) as conn:
        return conn.execute(text(sql), params).scalar()


def years(answer) -> str:
    return " ".join(c["value"] for c in answer["conditions"])


def year_2023(client) -> str:
    r = post(client, "מחיר למ״ר בעסקאות שנחתמו ב-2023 בחרוזים")
    assert r["answer"]["numeric"]["record_count"] == 1
    return r["conversation_id"]


def reserve(office, cid, tid, *, age_seconds: float) -> None:
    """A ``pending`` reservation left by a run that started ``age_seconds`` ago (e.g. its process died)."""
    with tenant_tx(office.system) as conn:
        conn.execute(text(
            "INSERT INTO questions (office_id, conversation_id, user_id, question_text, turn_id, status, created_at)"
            " VALUES (app_office(), :c, :u, :q, :t, 'pending', now() - make_interval(secs => :age))"),
            {"c": cid, "u": office.admin_id, "q": PREVIOUS_YEAR, "t": tid, "age": age_seconds})


# --- abandoned reservations (#12) -------------------------------------------------------------------------

def test_abandonment_bound_is_derived_from_the_deadline_and_the_slowest_model_call():
    from app.config import get_settings
    from app.providers.llm import Purpose, timeout_for

    s = get_settings()
    slowest = max(timeout_for(p) for p in Purpose)
    assert api.abandoned_after_seconds() == s.turn_deadline_seconds + api.MODEL_CALL_ATTEMPTS * slowest
    assert api.abandoned_after_seconds() > s.turn_deadline_seconds + slowest


def test_fresh_pending_reservation_still_answers_409(client, records):
    login(client, ADMIN)
    cid = year_2023(client)
    tid = str(uuid.uuid4())
    reserve(records, cid, tid, age_seconds=api.abandoned_after_seconds() - 10)
    assert post(client, PREVIOUS_YEAR, cid, turn_id=tid, status=409)["detail"] == api.IN_PROGRESS
    assert scalar(records, "SELECT status FROM questions WHERE turn_id = :t", t=tid) == "pending"


def test_abandoned_pending_reservation_runs_again_on_the_same_row(client, records):
    login(client, ADMIN)
    cid = year_2023(client)
    tid = str(uuid.uuid4())
    reserve(records, cid, tid, age_seconds=api.abandoned_after_seconds() + 5)
    qid = scalar(records, "SELECT id FROM questions WHERE turn_id = :t", t=tid)
    r = post(client, PREVIOUS_YEAR, cid, turn_id=tid)
    assert r["question_id"] == str(qid) and "2022" in years(r["answer"])
    assert scalar(records, "SELECT count(*) FROM questions WHERE turn_id = :t", t=tid) == 1
    assert scalar(records, "SELECT status FROM questions WHERE turn_id = :t", t=tid) == "done"
    assert post(client, PREVIOUS_YEAR, cid, turn_id=tid) == r  # and is stored like any finished turn


def test_a_run_whose_reservation_was_taken_over_never_completes_it(client, records, monkeypatch):
    """The abandoned run comes back after a re-run took its reservation: it answers 409 and writes nothing."""
    login(client, ADMIN)
    cid = year_2023(client)
    version = scalar(records, "SELECT state_version FROM conversations")
    original = turn.execute_turn

    def taken_over(ctx, it, L, qid, started):
        with tenant_tx(records.system) as conn:  # a re-run re-stamps the reservation while this one runs
            conn.execute(text("UPDATE questions SET created_at = now() + interval '1 second' WHERE id = :q"),
                         {"q": qid})
        return original(ctx, it, L, qid, started)

    monkeypatch.setattr(turn, "execute_turn", taken_over)
    tid = str(uuid.uuid4())
    assert post(client, PREVIOUS_YEAR, cid, turn_id=tid, status=409)["detail"] == api.IN_PROGRESS
    assert scalar(records, "SELECT status FROM questions WHERE turn_id = :t", t=tid) == "pending"  # not failed
    assert scalar(records, "SELECT state_version FROM conversations") == version


# --- turn ids are scoped to their conversation (#3) --------------------------------------------------------

def test_turn_id_reused_in_another_conversation_is_a_new_turn(client, records):
    login(client, ADMIN)
    first, second = year_2023(client), year_2023(client)
    tid = str(uuid.uuid4())
    a = post(client, PREVIOUS_YEAR, first, turn_id=tid)
    b = post(client, "מחיר למ״ר בעסקאות שנחתמו ב-2024 בחרוזים", second, turn_id=tid)
    assert a["conversation_id"] == first and b["conversation_id"] == second
    assert b["question_id"] != a["question_id"] and b["answer"] != a["answer"]
    assert b["answer"]["numeric"]["record_count"] == 2 and "2024" in years(b["answer"])
    assert scalar(records, "SELECT count(*) FROM questions WHERE turn_id = :t", t=tid) == 2
    # each conversation replays its own turn
    assert post(client, PREVIOUS_YEAR, first, turn_id=tid) == a
    assert post(client, "מחיר למ״ר בעסקאות שנחתמו ב-2024 בחרוזים", second, turn_id=tid) == b


def test_turn_without_conversation_replays_only_a_turn_that_opened_one(client, records):
    login(client, ADMIN)
    tid = str(uuid.uuid4())
    opened = post(client, "מחיר למ״ר בעסקאות שנחתמו ב-2023 בחרוזים", turn_id=tid)
    assert post(client, "מחיר למ״ר בעסקאות שנחתמו ב-2023 בחרוזים", turn_id=tid) == opened  # a lost response
    cid = opened["conversation_id"]
    later = str(uuid.uuid4())
    inside = post(client, PREVIOUS_YEAR, cid, turn_id=later)
    fresh = post(client, "מחיר למ״ר בעסקאות שנחתמו ב-2024 בחרוזים", turn_id=later)
    assert fresh["conversation_id"] != cid and fresh["question_id"] != inside["question_id"]
    assert "2024" in years(fresh["answer"])


# --- a state conflict never re-applies a clarification answer to another pending (#11) ---------------------

def _pending_clarification(client) -> str:
    r = post(client, AMBIGUOUS)
    assert r["answer"]["clarification"]["key"] == "data_kind"
    return r["conversation_id"]


def _concurrent(records, monkeypatch, change_sql: str) -> list[int]:
    """Another turn changes the conversation (``change_sql``) while this one runs its first attempt."""
    original, seen = turn.execute_turn, []

    def concurrent_turn(ctx, it, L, qid, started):
        seen.append(L.state_version)
        if len(seen) == 1:
            with tenant_tx(records.system) as conn:
                conn.execute(text("UPDATE conversations SET state_version = state_version + 1, " + change_sql))
        return original(ctx, it, L, qid, started)

    monkeypatch.setattr(turn, "execute_turn", concurrent_turn)
    return seen


@pytest.mark.parametrize("change_sql", [
    "state = state - 'pending'",  # the clarification was answered by another turn
    "state = jsonb_set(state, '{pending,question}', '\"שאלה אחרת?\"')",  # replaced by another clarification
], ids=["answered", "replaced"])
def test_clarification_answer_is_not_reapplied_when_the_pending_changed(client, records, monkeypatch, change_sql):
    login(client, ADMIN)
    cid = _pending_clarification(client)
    seen = _concurrent(records, monkeypatch, change_sql)
    tid = str(uuid.uuid4())
    r = post(client, conversation_id=cid, clarification=DATA_KIND, turn_id=tid, status=409)
    assert r["detail"] == turn.STATE_CONFLICT
    assert len(seen) == 1  # never executed against the fresh state
    assert scalar(records, "SELECT status FROM questions WHERE turn_id = :t", t=tid) == "failed"


def test_clarification_answer_is_reapplied_when_the_same_pending_is_still_open(client, records, monkeypatch):
    login(client, ADMIN)
    cid = _pending_clarification(client)
    seen = _concurrent(records, monkeypatch, "updated_at = now()")
    a = post(client, conversation_id=cid, clarification=DATA_KIND)["answer"]
    assert seen == [seen[0], seen[0] + 1]  # retried once on the fresh state, same clarification
    assert a["kind"] == "numeric" and a["numeric"]["record_count"] == 4
    assert client.get(f"/api/conversations/{cid}").json()["pending_clarification"] is None
