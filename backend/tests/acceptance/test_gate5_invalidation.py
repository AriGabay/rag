"""Gate 5 (origin §11): correcting a fact, deleting a source, uploading a new version and changing a
permission each stop a previous result from being reused: the next identical question recomputes
(no ``cached`` flag, new numbers equal to the oracle) and the earlier history message is marked
stale or hidden."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db import tenant_tx
from eval.flows import ask_flow
from eval.truth import Filters, docs, expected_stats
from tests.acceptance.conftest import _SHARED, build_world, drain, upload_file
from tests.acceptance.support import ADMIN_A, CASES_BY_ID, DANA, HAROZIM, RG, TX, assert_numeric

pytestmark = pytest.mark.db

STATE: dict = {"overrides": {}, "dropped": set()}


def occurrence_id(world, gt_doc: str, row: int | None):
    with tenant_tx(world.system("A")) as conn:
        return conn.execute(text(
            "SELECT o.id FROM occurrences o JOIN document_versions v ON v.id = o.version_id AND v.is_current"
            " WHERE v.filename = :f AND o.row_index IS NOT DISTINCT FROM :r AND o.data_kind = 'transaction_price'"),
            {"f": docs()[gt_doc]["filename"], "r": row}).scalar_one()


def expected(world, case, groups=None):
    return expected_stats(case.filters, "A", groups, world.exclude, overrides=STATE["overrides"],
                          dropped_docs=STATE["dropped"])


def message(client, conversation_id: str) -> dict:
    return client.get(f"/api/conversations/{conversation_id}").json()["messages"][-1]


def test_correcting_a_fact_invalidates_the_cached_answer(mutable_world):
    w = mutable_world
    admin, case = w.client(ADMIN_A), CASES_BY_ID["harozim-2024-net"]
    first = ask_flow(admin, case.question, case.answers)
    assert_numeric(first.answer, expected(w, case))
    assert ask_flow(admin, case.question, case.answers).answer.get("cached") is True
    assert message(admin, first.conversation_id)["stale"] is False

    occ = occurrence_id(w, "D12", 0)  # D12-T00 contributes (97 m² net, 2,510,000)
    r = admin.post(f"/api/review/records/{occ}/correct", json={"field": "price", "value": "2,600,000",
                                                               "note": "תיקון לפי הסכם המכר"})
    assert r.status_code == 200, r.text
    STATE["overrides"]["D12-T00"] = {"price": "2600000"}

    after = ask_flow(admin, case.question, case.answers).answer
    assert after.get("cached") is not True
    assert_numeric(after, expected(w, case))
    assert after["numeric"]["mean_price_per_sqm"] != first.answer["numeric"]["mean_price_per_sqm"]
    assert message(admin, first.conversation_id)["stale"] is True


def test_deleting_a_source_invalidates_and_hides_history(mutable_world):
    w = mutable_world
    admin, case = w.client(ADMIN_A), CASES_BY_ID["harozim-2024-gross-over-top-k"]
    first = ask_flow(admin, case.question, case.answers)
    assert_numeric(first.answer, expected(w, case))
    d3 = w.docs["D3"]
    assert d3["document_id"] in {s["document_id"] for s in first.answer["sources"]}
    assert admin.delete(f"/api/documents/{d3['document_id']}").json() == {"ok": True}
    STATE["dropped"].add("D3")

    after = ask_flow(admin, case.question, case.answers).answer
    assert after.get("cached") is not True
    assert_numeric(after, expected(w, case))
    assert d3["document_id"] not in {s["document_id"] for s in after["sources"]}
    old = message(admin, first.conversation_id)
    assert old["hidden"] is True and old["answer"] is None
    # the deleted file, its search hits and its review items are gone immediately
    assert admin.get(f"/api/documents/{d3['document_id']}/versions/{d3['version_id']}/file").status_code == 404
    hits = admin.get("/api/search", params={"q": "רעש מכביש ז׳בוטינסקי"}).json()["results"]
    assert d3["document_id"] not in {h["document_id"] for h in hits}


def test_permission_change_invalidates_for_that_user(mutable_world):
    w = mutable_world
    admin, dana = w.client(ADMIN_A), w.client(DANA)
    case = CASES_BY_ID["harozim-2024-net"]
    first = ask_flow(dana, case.question, case.answers)
    assert first.answer["kind"] == "numeric"
    assert ask_flow(dana, case.question, case.answers).answer.get("cached") is True
    user = next(u for u in admin.get("/api/admin/users").json()["users"] if u["email"] == DANA)
    assert admin.patch(f"/api/admin/users/{user['id']}", json={"group_ids": []}).status_code == 200

    after = ask_flow(dana, case.question, case.answers).answer
    assert after.get("cached") is not True
    assert after["kind"] in ("abstain", "content") and not after.get("numeric")
    assert not after.get("sources")
    old = message(dana, first.conversation_id)
    assert old["hidden"] is True or old["stale"] is True

    assert admin.patch(f"/api/admin/users/{user['id']}", json={"group_ids": [w.groups["G1"]]}).status_code == 200
    back = ask_flow(dana, case.question, case.answers).answer
    assert_numeric(back, expected(w, case, frozenset({"G1"})))


def test_new_version_invalidates_and_needs_fresh_approval(owner_engine):
    """D1 is answered, then D1v2 arrives as a new version (row 3: 1,790,000 -> 1,820,000)."""
    if _SHARED["world"] is not None:
        _SHARED["world"].close()
        _SHARED["world"] = None
    w = build_world(owner_engine, with_new_version=False)
    w.dirty()
    try:
        admin = w.client(ADMIN_A)
        f = Filters(TX, "transaction_date", 2023, city=RG, neighborhood=HAROZIM, area_type="registered",
                    property_type="apartment", vat_basis="included")
        q, answers = "מחירי עסקאות למ״ר בחרוזים שנחתמו ב-2023", {"area_type": "registered",
                                                               "property_type": "apartment", "vat_basis": "included"}
        # the oracle for the D1 world: D1 is current, D1v2 does not exist yet
        before = ask_flow(admin, q, answers)
        exp_before = expected_stats(f, exclude=w.exclude, not_current={"D1v2"})
        assert "D1-T03" in exp_before.record_ids
        assert_numeric(before.answer, exp_before)
        assert ask_flow(admin, q, answers).answer.get("cached") is True

        res = upload_file(admin, docs()["D1v2"]["filename"], document_id=w.docs["D1"]["document_id"])
        assert res["status"] == "accepted" and res["document_id"] == w.docs["D1"]["document_id"]
        w.docs["D1v2"] = res
        drain()
        w.refresh_verification()
        after = ask_flow(admin, q, answers).answer
        assert after.get("cached") is not True
        assert "D1v2-T03" in w.unverified  # a new version's records wait for a human again
        assert_numeric(after, expected_stats(f, exclude=w.exclude))
        assert after["numeric"]["record_count"] == exp_before.count - 1
        assert after["coverage"]["records_awaiting_verification"] >= 1
        assert message(admin, before.conversation_id)["stale"] is True

        occ = occurrence_id(w, "D1v2", 3)
        assert admin.post(f"/api/review/records/{occ}/approve", json={"note": "אושר"}).status_code == 200
        w.refresh_verification()
        approved = ask_flow(admin, q, answers).answer
        assert approved.get("cached") is not True
        assert_numeric(approved, expected_stats(f, exclude=w.exclude))
        assert approved["numeric"]["record_count"] == exp_before.count
        assert approved["numeric"]["mean_price_per_sqm"] != exp_before.as_answer()["mean_price_per_sqm"]
    finally:
        w.close()

