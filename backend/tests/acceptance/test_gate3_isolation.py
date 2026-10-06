"""Gate 3 (origin §11): no leakage between two offices or across changing permissions, including
the answer cache and worker jobs.

Office B never sees office A's numbers, sources, search hits, review items, files, conversations or
cache entries; a G2 employee never sees G1 documents and vice versa; removing a group membership
changes the very next answer; processing office B's upload never touches office A's rows.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app import worker
from eval.flows import ask_flow
from eval.truth import Filters, docs, expected_stats, truth
from tests.acceptance.conftest import FIXTURES, drain, upload_file
from tests.acceptance.support import (
    ADMIN_A,
    ADMIN_B,
    CASES_BY_ID,
    DANA,
    HAROZIM,
    RG,
    TX,
    YOSSI,
    assert_numeric,
    expected_for,
    run_case,
)

pytestmark = pytest.mark.db

A_PHRASES = [f["phrase"] for f in truth()["content_facts"] if docs()[f["document"]]["office"] == "A"]


def _ids(world, office=None, group=None) -> set[str]:
    out = set()
    for gt, r in world.docs.items():
        d = docs()[gt]
        g = docs()[d["version_of"]]["group"] if d.get("version_of") else d["group"]
        if r.get("document_id") and (office is None or d["office"] == office) and (group is None or g == group):
            out.add(r["document_id"])
    return out


def test_office_b_numbers_and_sources_are_only_its_own(world):
    case = CASES_BY_ID["office-b-harozim-2024"]
    a = run_case(world, case).answer
    assert_numeric(a, expected_for(world, case))
    assert {s["document_id"] for s in a["sources"]} == {world.doc_id("DB1")}
    # the same question in office A gives A's own (different) result
    a_answer = ask_flow(world.client(ADMIN_A), case.question,
                        {"area_type": "net", "property_type": "apartment", "vat_basis": "included"}).answer
    assert not ({s["document_id"] for s in a_answer["sources"]} & _ids(world, "B"))


def test_client_sent_office_id_is_ignored(world):
    client = world.client(ADMIN_B)
    body = {"question": CASES_BY_ID["office-b-harozim-2024"].question, "office_id": str(world.a.office_id),
            "filters": {"office_id": str(world.a.office_id)}}
    a = client.post("/api/ask", json=body).json()["answer"]
    assert {s["document_id"] for s in a.get("sources", [])} <= _ids(world, "B")
    docs_b = client.get("/api/documents", params={"office_id": str(world.a.office_id)}).json()["documents"]
    assert {d["id"] for d in docs_b} == _ids(world, "B")


@pytest.mark.parametrize("phrase", A_PHRASES)
def test_office_b_search_and_content_never_return_office_a(world, phrase):
    client = world.client(ADMIN_B)
    hits = client.get("/api/search", params={"q": phrase}).json()["results"]
    assert {h["document_id"] for h in hits} <= _ids(world, "B")
    a = client.post("/api/ask", json={"question": f"מה נכתב על {phrase}?"}).json()["answer"]
    assert {s["document_id"] for s in a.get("sources", [])} <= _ids(world, "B")
    assert phrase not in a.get("text", "") or phrase in docs()["DB1"].get("notes", "")


def test_office_b_cannot_open_any_office_a_object(world):
    a_client, b_client = world.client(ADMIN_A), world.client(ADMIN_B)
    for gt in ("D1", "D4", "D9", "D11"):
        r = world.docs[gt]
        assert b_client.get(f"/api/documents/{r['document_id']}").status_code == 404
        assert b_client.get(f"/api/documents/{r['document_id']}/versions/{r['version_id']}/file").status_code == 404
        assert b_client.delete(f"/api/documents/{r['document_id']}").status_code == 404
    queue_a = a_client.get("/api/review/queue").json()["items"]
    rec = next(i for i in queue_a if i["kind"] == "record")
    assert b_client.get(f"/api/review/records/{rec['id']}").status_code == 404
    assert b_client.post(f"/api/review/records/{rec['id']}/approve", json={}).status_code in (403, 404)
    dedup = next((i for i in queue_a if i["kind"] == "dedup"), None)
    if dedup:
        assert b_client.post(f"/api/review/dedup/{dedup['id']}/merge").status_code == 404
    conv = ask_flow(a_client, "מה נכתב על היעדר מעלית?").conversation_id
    assert b_client.get(f"/api/conversations/{conv}").status_code == 404
    assert b_client.post("/api/ask", json={"conversation_id": conv, "question": "ומה לגבי 2023?"}).status_code == 404
    # the review queue and document list of B hold nothing of A
    queue_b = b_client.get("/api/review/queue").json()["items"]
    assert all(i.get("document", {}).get("id") in _ids(world, "B") for i in queue_b if i["kind"] == "record")
    assert {d["id"] for d in b_client.get("/api/documents").json()["documents"]} == _ids(world, "B")
    # a still-unauthorized id and a malformed id look the same
    assert b_client.get("/api/documents/not-a-uuid").status_code == 404


def test_cache_entries_never_cross_offices(world):
    """Office A caches an answer first; office B asking the same conditions must compute its own."""
    question = "מחירי עסקאות למ״ר בשכונת חרוזים שנחתמו ב-2023"
    answers = {"area_type": "gross", "property_type": "apartment", "vat_basis": "included"}
    first_a = ask_flow(world.client(ADMIN_A), question, answers).answer
    again_a = ask_flow(world.client(ADMIN_A), question, answers).answer
    assert again_a.get("cached") is True and again_a["numeric"] == first_a["numeric"]
    b = ask_flow(world.client(ADMIN_B), question, answers).answer
    assert b.get("cached") is not True
    exp_b = expected_stats(Filters(TX, "transaction_date", 2023, city=RG, neighborhood=HAROZIM), "B")
    assert_numeric(b, exp_b)
    assert {s["document_id"] for s in b["sources"]} == {world.doc_id("DB1")}
    # every cache row an office can read is its own and names none of the other office's documents
    for office, foreign in (("A", _ids(world, "B")), ("B", _ids(world, "A"))):
        from app.db import tenant_tx

        with tenant_tx(world.system(office)) as conn:
            rows = conn.execute(text("SELECT office_id, payload::text AS p FROM answer_cache")).all()
        assert rows
        assert {r.office_id for r in rows} == {world.office(office).office_id}
        assert not any(i in r.p for r in rows for i in foreign)


def test_g1_and_g2_employees_see_only_their_groups(world):
    g1, g2 = _ids(world, "A", "G1"), _ids(world, "A", "G2")
    dana, yossi = world.client(DANA), world.client(YOSSI)
    y = run_case(world, CASES_BY_ID["yossi-g2-2023"]).answer
    assert {s["document_id"] for s in y["sources"]} <= g2
    d = run_case(world, CASES_BY_ID["dana-g1-excluded-vat"]).answer
    assert {s["document_id"] for s in d["sources"]} <= g1
    admin = ask_flow(world.client(ADMIN_A), CASES_BY_ID["dana-g1-excluded-vat"].question,
                     {"area_type": "net", "property_type": "apartment"}).answer
    assert admin["numeric"]["record_count"] > d["numeric"]["record_count"]  # D4 adds a record for the admin
    # search, documents, files, review
    assert {h["document_id"] for h in yossi.get("/api/search", params={"q": "מרפסת שמש הפונה מערבה"}).json()["results"]} <= g2
    assert {h["document_id"] for h in dana.get("/api/search", params={"q": "חניה בטאבו"}).json()["results"]} <= g1
    assert {x["id"] for x in yossi.get("/api/documents").json()["documents"]} == g2
    d4 = world.docs["D4"]
    assert dana.get(f"/api/documents/{d4['document_id']}/versions/{d4['version_id']}/file").status_code == 404
    d1 = world.docs["D1v2"]
    assert yossi.get(f"/api/documents/{d1['document_id']}/versions/{d1['version_id']}/file").status_code == 404
    for item in yossi.get("/api/review/queue").json()["items"]:
        if item["kind"] == "record":
            assert item["document"]["id"] in g2
        else:  # a dedup pair is shown only when every source is visible
            assert {s["document_id"] for side in ("a", "b") for s in item[side]["sources"]} <= g2


def test_worker_processing_office_b_upload_never_touches_office_a(world):
    world.dirty()
    tables = ("pages", "chunks", "extracted_tables", "occurrences", "transactions", "fact_values", "answer_cache")
    before = {t: world.scalar("A", f"SELECT count(*) FROM {t}") for t in tables}
    version_a = world.scalar("A", "SELECT version FROM office_data_versions")
    a_answer = run_case(world, CASES_BY_ID["harozim-2024-net"]).answer["numeric"]
    # office B uploads the very same bytes office A already holds: accepted, processed on its own
    res = upload_file(world.client(ADMIN_B), docs()["D1"]["filename"], group_id=world.groups["B"])
    assert res["status"] == "accepted" and "reason" not in res
    assert drain() >= 1
    assert worker.run_one("acceptance-worker") is False
    after = {t: world.scalar("A", f"SELECT count(*) FROM {t}") for t in tables}
    assert after == before
    assert world.scalar("A", "SELECT version FROM office_data_versions") == version_a
    # RLS is forced for every role, so each office's system context sees exactly its own rows
    for table in ("pages", "chunks", "occurrences", "jobs"):
        sql = f"SELECT count(*) FROM {table} WHERE version_id = :v"
        assert world.scalar("A", sql, v=res["version_id"]) == 0
        assert world.scalar("B", sql, v=res["version_id"]) > 0
    assert world.scalar("B", "SELECT count(*) FROM pages WHERE version_id = :v",
                        v=res["version_id"]) == docs()["D1"]["page_count"]
    assert run_case(world, CASES_BY_ID["harozim-2024-net"]).answer["numeric"] == a_answer
    assert (FIXTURES / docs()["D1"]["filename"]).exists()


def test_removing_group_membership_changes_the_next_answer(world):
    world.dirty()
    admin, dana = world.client(ADMIN_A), world.client(DANA)
    case = CASES_BY_ID["harozim-2024-net"]
    first = ask_flow(dana, case.question, case.answers)
    assert first.answer["kind"] == "numeric"
    before_ids = {s["document_id"] for s in first.answer["sources"]}
    assert before_ids <= _ids(world, "A", "G1")
    user = next(u for u in admin.get("/api/admin/users").json()["users"] if u["email"] == DANA)
    assert admin.patch(f"/api/admin/users/{user['id']}", json={"group_ids": [world.groups["G2"]]}).status_code == 200
    second = ask_flow(dana, case.question, case.answers).answer
    assert second.get("cached") is not True
    assert not ({s["document_id"] for s in second.get("sources", [])} & before_ids)
    if second["kind"] == "numeric":
        # only the attributes G2's records still mix were asked; the rest stay open
        f = case.filters.with_(**{k: None for k in ("area_type", "property_type", "vat_basis")
                                  if k not in [c for c in ask_flow(dana, case.question, case.answers).clarifications]})
        exp = expected_stats(f, "A", frozenset({"G2"}), world.exclude)
        assert_numeric(second, exp)
    else:
        assert second["kind"] in ("abstain", "clarification") and not second.get("numeric")
    # the earlier answer in dana's history is now marked out of date
    conv = dana.get(f"/api/conversations/{first.conversation_id}").json()
    last = conv["messages"][-1]  # the numeric answer: its G1 sources are no longer authorized
    assert last["hidden"] is True and last["answer"] is None
    # her files are gone too
    d1 = world.docs["D1v2"]
    assert dana.get(f"/api/documents/{d1['document_id']}/versions/{d1['version_id']}/file").status_code == 404
