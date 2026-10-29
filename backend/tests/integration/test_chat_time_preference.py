"""The time preference's path through the chat API (round 11 U5: KTD1, KTD15; R2, R14, R15, R16).

A conversation holds the current switch; every user message records the state it was sent with (and its Asia/Jerusalem
anchor date), the assistant row carries a copy, and a retry, a resend or a toggle never reads the conversation's current
value instead. Turns run in limited mode (no provider): what the tests prove is the stored state and what
``_turn_input`` hands the engine, not the answers. Synthetic data only."""

from __future__ import annotations

import threading
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.chat import api as chat_api
from app.chat import engine
from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.integration.test_search import add_chunks

pytestmark = pytest.mark.db

TZ = ZoneInfo("Asia/Jerusalem")
TEXTS = ["נכס סינתטי לבדיקה: השווי למ\"ר נקבע ל-9,500 ₪ ללא מע\"מ.", "תיאור: מבנה מסחרי סינתטי בן 3 קומות."]


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)  # the turn finishes inside the request
    a = make_office(db, "משרד א", "admin-a@example.test")
    make_user(a, "emp@example.test", [a.default_group_id])
    add_chunks(a, a.default_group_id, TEXTS, "1" * 64)
    return a


@pytest.fixture
def turns(monkeypatch):
    """Every ``TurnInput`` ``_turn_input`` builds, in order."""
    seen: list[engine.TurnInput] = []
    real = chat_api._turn_input

    def spy(*args, **kwargs):
        inp, ids = real(*args, **kwargs)
        seen.append(inp)
        return inp, ids

    monkeypatch.setattr(chat_api, "_turn_input", spy)
    return seen


def today() -> str:
    return datetime.now(TZ).date().isoformat()


def new_conversation(client) -> dict:
    r = client.post("/api/chat/conversations")
    assert r.status_code == 200, r.text
    return r.json()


def post(client, cid: str, content: str = "מה השווי למ\"ר?", **extra):
    return client.post(f"/api/chat/conversations/{cid}/messages", json={"content": content, **extra})


def messages(client, cid: str) -> list[dict]:
    return client.get(f"/api/chat/conversations/{cid}/messages").json()["messages"]


def state(prefer_recent: bool, anchor: str | None = None) -> dict:
    return {"version": 1, "prefer_recent": prefer_recent, "anchor_date": anchor or today()}


def test_a_new_conversation_starts_on_and_every_conversation_json_carries_the_switch(client, office):
    login(client, "admin-a@example.test")
    conv = new_conversation(client)
    assert conv["prefer_recent"] is True
    listed = client.get("/api/chat/conversations").json()["conversations"]
    assert [c["prefer_recent"] for c in listed] == [True]
    assert client.get(f"/api/chat/conversations/{conv['id']}/messages").json()["conversation"]["prefer_recent"] is True


def test_a_send_without_the_field_takes_the_conversation_value_and_records_it_on_both_rows(client, office, turns):
    login(client, "admin-a@example.test")
    cid = new_conversation(client)["id"]
    r = post(client, cid)
    assert r.status_code == 200, r.text
    user, assistant = r.json()["user"], r.json()["assistant"]
    assert user["time_preference"] == state(True) and assistant["time_preference"] == state(True)
    assert client.get(f"/api/chat/conversations/{cid}/messages").json()["conversation"]["prefer_recent"] is True
    assert turns[-1].time_preference == state(True)
    # an off conversation sends off when the body says nothing
    assert client.patch(f"/api/chat/conversations/{cid}", json={"prefer_recent": False}).json()["prefer_recent"] is False
    second = post(client, cid, "ומה דמי השכירות?").json()
    assert second["user"]["time_preference"] == state(False) and second["assistant"]["time_preference"] == state(False)
    assert turns[-1].time_preference == state(False)


def test_a_send_with_the_field_records_it_and_updates_the_conversation(client, office, turns):
    login(client, "admin-a@example.test")
    cid = new_conversation(client)["id"]
    r = post(client, cid, prefer_recent=False).json()
    assert r["user"]["time_preference"] == state(False) and r["assistant"]["time_preference"] == state(False)
    assert turns[-1].time_preference == state(False)
    conv = client.get(f"/api/chat/conversations/{cid}/messages").json()["conversation"]
    assert conv["prefer_recent"] is False  # the last chosen value is kept and restored after a reload
    again = post(client, cid, "שאלה נוספת", prefer_recent=True).json()
    assert again["user"]["time_preference"] == state(True)
    assert client.get("/api/chat/conversations").json()["conversations"][0]["prefer_recent"] is True
    # reloaded messages carry the state on both roles
    rows = messages(client, cid)
    assert [m["time_preference"] for m in rows] == [state(False)] * 2 + [state(True)] * 2


def test_the_anchor_is_the_jerusalem_date_not_the_utc_date(client, office, monkeypatch):
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 14, 22, 30, tzinfo=ZoneInfo("UTC")).astimezone(tz) if tz else cls(2026, 10, 14, 22, 30)

    monkeypatch.setattr(chat_api, "datetime", Frozen)
    login(client, "admin-a@example.test")
    cid = new_conversation(client)["id"]
    sent = post(client, cid).json()
    assert sent["user"]["time_preference"]["anchor_date"] == "2026-10-15"  # 22:30 UTC is already the 15th in Jerusalem


def test_a_resend_with_the_same_client_id_returns_the_stored_pair_and_leaves_the_conversation_alone(client, office, turns):
    login(client, "admin-a@example.test")
    cid = new_conversation(client)["id"]
    cid_msg = "11111111-1111-4111-8111-111111111111"
    first = post(client, cid, client_id=cid_msg, prefer_recent=False).json()
    assert first["user"]["time_preference"] == state(False)
    with tenant_tx(office.ctx()) as conn:
        before = conn.execute(text("SELECT updated_at, prefer_recent FROM conversations WHERE id = :c"), {"c": UUID(cid)}).one()
    resend = post(client, cid, client_id=cid_msg, prefer_recent=True).json()
    assert resend["user"]["id"] == first["user"]["id"] and resend["user"]["time_preference"] == state(False)
    assert resend["assistant"]["id"] == first["assistant"]["id"] and resend["assistant"]["time_preference"] == state(False)
    with tenant_tx(office.ctx()) as conn:
        after = conn.execute(text("SELECT updated_at, prefer_recent FROM conversations WHERE id = :c"), {"c": UUID(cid)}).one()
        assert conn.execute(text("SELECT count(*) FROM messages WHERE conversation_id = :c"), {"c": UUID(cid)}).scalar_one() == 2
    assert tuple(after) == tuple(before) and after.prefer_recent is False  # the first send's state wins, nothing re-updated
    assert len(turns) == 1


def test_a_retry_after_toggling_runs_with_the_state_the_message_was_sent_with(client, office, turns):
    """AE5: sent with the switch off, the switch turned on, the answer retried: it runs and shows off."""
    login(client, "admin-a@example.test")
    cid = new_conversation(client)["id"]
    sent = post(client, cid, prefer_recent=False).json()
    assert client.patch(f"/api/chat/conversations/{cid}", json={"prefer_recent": True}).json()["prefer_recent"] is True
    retried = client.post(f"/api/chat/messages/{sent['assistant']['id']}/retry")
    assert retried.status_code == 200, retried.text
    assert retried.json()["time_preference"] == state(False)
    assert [t.time_preference for t in turns] == [state(False), state(False)]
    shown = client.get(f"/api/chat/messages/{retried.json()['id']}").json()
    assert shown["time_preference"] == state(False)
    assert [m["time_preference"] for m in messages(client, cid)] == [state(False), state(False)]
    # the conversation keeps the toggled value for the next message: retry does not write it back
    assert client.get(f"/api/chat/conversations/{cid}/messages").json()["conversation"]["prefer_recent"] is True


def test_a_retry_keeps_the_original_anchor_date(client, office, turns):
    login(client, "admin-a@example.test")
    cid = new_conversation(client)["id"]
    sent = post(client, cid).json()
    with tenant_tx(office.system) as conn:  # the message was sent on an earlier day
        conn.execute(text("UPDATE messages SET time_preference = jsonb_set(time_preference, '{anchor_date}', '\"2026-01-05\"')"
                          " WHERE conversation_id = :c"), {"c": UUID(cid)})
    retried = client.post(f"/api/chat/messages/{sent['assistant']['id']}/retry").json()
    assert turns[-1].time_preference == state(True, "2026-01-05")
    assert retried["time_preference"] == state(True, "2026-01-05")


def test_a_message_from_before_the_round_retries_with_the_switch_off_and_is_not_written_back(client, office, turns):
    login(client, "admin-a@example.test")
    cid = new_conversation(client)["id"]
    sent = post(client, cid).json()
    with tenant_tx(office.system) as conn:  # as if both rows predate migration 0020
        conn.execute(text("UPDATE messages SET time_preference = NULL, created_at = TIMESTAMPTZ '2026-03-09 23:30:00+00'"
                          " WHERE id = :m"), {"m": UUID(sent["user"]["id"])})
        conn.execute(text("UPDATE messages SET time_preference = NULL WHERE id = :m"), {"m": UUID(sent["assistant"]["id"])})
        conn.execute(text("UPDATE messages SET created_at = TIMESTAMPTZ '2026-03-09 23:31:00+00' WHERE id = :m"),
                     {"m": UUID(sent["assistant"]["id"])})
    shown = client.get(f"/api/chat/messages/{sent['assistant']['id']}").json()
    assert shown["time_preference"] is None  # an old answer shows no state
    retried = client.post(f"/api/chat/messages/{sent['assistant']['id']}/retry")
    assert retried.status_code == 200, retried.text
    # off, anchored on the message's own day in Jerusalem (23:30 UTC in March is already the 10th there)
    assert turns[-1].time_preference == state(False, "2026-03-10")
    assert retried.json()["time_preference"] == state(False, "2026-03-10")
    with tenant_tx(office.system) as conn:
        old = conn.execute(text("SELECT time_preference FROM messages WHERE id = :m"), {"m": UUID(sent["user"]["id"])}).scalar_one()
    assert old is None  # nothing is written back to the old user row


def test_a_toggle_during_a_running_turn_succeeds_and_the_running_message_keeps_its_state(client, office, monkeypatch):
    login(client, "admin-a@example.test")
    cid = new_conversation(client)["id"]
    seen: dict = {}
    real = chat_api._turn_input

    def toggle_mid_turn(conn, ctx, conversation_id, user_message_id):
        inp, ids = real(conn, ctx, conversation_id, user_message_id)
        done = threading.Event()

        def press() -> None:  # the user flips the switch while the turn is running
            seen["patch"] = client.patch(f"/api/chat/conversations/{cid}", json={"prefer_recent": False})
            done.set()

        # a separate thread: this one holds the turn's transaction open
        threading.Thread(target=press).start()
        assert done.wait(20)
        seen["input"] = inp
        return inp, ids

    monkeypatch.setattr(chat_api, "_turn_input", toggle_mid_turn)
    sent = post(client, cid).json()
    assert seen["patch"].status_code == 200 and seen["patch"].json()["prefer_recent"] is False
    assert seen["input"].time_preference == state(True)
    rows = messages(client, cid)
    assert [m["time_preference"] for m in rows] == [state(True), state(True)]
    assert rows[1]["id"] == sent["assistant"]["id"]
    assert client.get(f"/api/chat/conversations/{cid}/messages").json()["conversation"]["prefer_recent"] is False


def test_the_switch_can_be_changed_on_an_archived_conversation(client, office):
    login(client, "admin-a@example.test")
    cid = new_conversation(client)["id"]
    assert client.patch(f"/api/chat/conversations/{cid}", json={"archived": True}).json()["archived"] is True
    done = client.patch(f"/api/chat/conversations/{cid}", json={"prefer_recent": False})
    assert done.status_code == 200 and done.json()["prefer_recent"] is False and done.json()["archived"] is True
    # a patch about something else leaves the switch alone
    renamed = client.patch(f"/api/chat/conversations/{cid}", json={"title": "שם חדש"}).json()
    assert renamed["prefer_recent"] is False and renamed["title"] == "שם חדש"


def test_another_users_conversation_is_not_found_for_the_switch_and_the_send(client, office):
    login(client, "admin-a@example.test")
    cid = new_conversation(client)["id"]
    client.post("/api/auth/logout")
    login(client, "emp@example.test")
    assert client.patch(f"/api/chat/conversations/{cid}", json={"prefer_recent": False}).status_code == 404
    assert post(client, cid, prefer_recent=False).status_code == 404
    client.post("/api/auth/logout")
    login(client, "admin-a@example.test")
    assert client.get(f"/api/chat/conversations/{cid}/messages").json()["conversation"]["prefer_recent"] is True


def test_another_offices_user_cannot_change_the_switch(client, office, db):
    b = make_office(db, "משרד ב", "admin-b@example.test")
    make_group(b, "קבוצה")
    login(client, "admin-a@example.test")
    cid = new_conversation(client)["id"]
    client.post("/api/auth/logout")
    login(client, "admin-b@example.test")
    assert client.patch(f"/api/chat/conversations/{cid}", json={"prefer_recent": False}).status_code == 404


def test_the_body_fields_are_typed(client, office):
    login(client, "admin-a@example.test")
    cid = new_conversation(client)["id"]
    assert client.patch(f"/api/chat/conversations/{cid}", json={"prefer_recent": "maybe"}).status_code == 422
    assert post(client, cid, prefer_recent="maybe").status_code == 422
