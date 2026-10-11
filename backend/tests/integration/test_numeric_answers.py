"""Structured question path end to end over HTTP (U8, U11): clarification, SQL, template, cache."""

from decimal import ROUND_HALF_UP, Decimal

import pytest
from sqlalchemy import text

from app.answering.conditions import QueryConditions
from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.integration.test_dedup import HEADER_TEXT, HEADERS, add_version, publish  # noqa: F401

pytestmark = pytest.mark.db


def row(addr, bp, d, area, price, area_type="נטו", ptype="דירה"):
    return [addr, bp, d, ptype, "4", area, area_type, price, ""]


AE3 = [row("הגפן 1", "6158/1", "10/01/2024", "50", "1,000,000"), row("הגפן 2", "6158/2", "11/02/2024", "100", "3,000,000")]


def approve_all(office):
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE occurrences SET verification_status = 'human_verified'"))
        conn.execute(text("UPDATE office_data_versions SET version = version + 1"))


@pytest.fixture
def office(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = add_version(a, a.default_group_id, AE3 + [row("הגפן 3", "6158/3", "05/05/2023", "80", "2,000,000")], "1" * 64)
    publish(a, doc, ver)
    approve_all(a)
    return a


def ask(client, question=None, conversation_id=None, clarification=None, filters=None):
    r = client.post("/api/ask", json={"question": question, "conversation_id": conversation_id,
                                      "clarification": clarification, "filters": filters})
    assert r.status_code == 200, r.text
    return r.json()


def test_core_example_clarifies_then_computes(client, office):
    login(client, "admin-a@example.test")
    r1 = ask(client, "מה מחיר למ״ר ברמת גן בשכונת חרוזים בשנת 2024?")
    assert r1["answer"]["kind"] == "clarification" and r1["answer"]["clarification"]["key"] == "data_kind"
    cid = r1["conversation_id"]
    r2 = ask(client, conversation_id=cid, clarification={"key": "data_kind", "value": "transaction_price"})
    assert r2["answer"]["clarification"]["key"] == "date_field"
    labels = [o["value"] for o in r2["answer"]["clarification"]["options"]]
    assert labels == ["transaction_date", "valuation_date"]
    r3 = ask(client, conversation_id=cid, clarification={"key": "date_field", "value": "transaction_date"})
    a = r3["answer"]
    assert a["kind"] == "numeric" and a["provider"] == "template"
    assert a["numeric"]["record_count"] == 2
    assert a["numeric"]["mean_price_per_sqm"] == "25000.00"
    assert a["numeric"]["weighted_price_per_sqm"] == "26666.67"
    assert len(a["sources"]) == 2 and a["sources"][0]["url"].endswith("#page=2")
    assert "מתוך הרשומות המאומתות" in a["text"]


def test_follow_up_changes_year_only(client, office):
    login(client, "admin-a@example.test")
    r = ask(client, "מחיר למ״ר בעסקאות שנחתמו ב-2024 בחרוזים")
    assert r["answer"]["numeric"]["record_count"] == 2
    r2 = ask(client, "ומה לגבי 2023?", conversation_id=r["conversation_id"])
    assert r2["answer"]["numeric"]["record_count"] == 1
    assert r2["answer"]["numeric"]["mean_price_per_sqm"] == "25000.00"
    conv = client.get(f"/api/conversations/{r['conversation_id']}").json()
    assert {"label": "שכונה", "value": "חרוזים"} in conv["confirmed_conditions"]


def test_no_matching_records_abstains(client, office):
    login(client, "admin-a@example.test")
    r = ask(client, "מחיר למ״ר בעסקאות שנחתמו ב-2019 בחרוזים")
    assert r["answer"]["kind"] == "abstain" and r["answer"].get("numeric") is None
    assert "לא חושב מספר" in r["answer"]["text"]
    unknown = ask(client, "מה מחיר העסקאות למ״ר בשכונת נווה צדק ב-2024?")
    assert unknown["answer"]["kind"] == "abstain" and "נווה צדק" in unknown["answer"]["text"]


def test_unverified_records_are_excluded_and_reported(client, office):
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE occurrences SET verification_status = 'auto_extracted' WHERE address = 'הגפן 1'"))
    login(client, "admin-a@example.test")
    a = ask(client, "מחיר למ״ר בעסקאות שנחתמו ב-2024 בחרוזים")["answer"]
    assert a["numeric"]["record_count"] == 1
    assert a["coverage"]["records_awaiting_verification"] == 1


def test_mixed_area_basis_triggers_clarification(client, office):
    a = office
    doc, ver = add_version(a, a.default_group_id, [row("הגפן 9", "6158/9", "01/06/2024", "120", "3,600,000", "ברוטו")], "2" * 64)
    publish(a, doc, ver)
    approve_all(a)
    login(client, "admin-a@example.test")
    r = ask(client, "מחיר למ״ר בעסקאות שנחתמו ב-2024 בחרוזים")
    assert r["answer"]["clarification"]["key"] == "area_type"
    r2 = ask(client, conversation_id=r["conversation_id"], clarification={"key": "area_type", "value": "net"})
    assert r2["answer"]["numeric"]["record_count"] == 2


def test_more_records_than_top_k_are_all_counted(client, db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    rows = [row(f"השקמה {i}", f"7000/{i}", "15/03/2024", "100", f"{2_000_000 + i * 10_000:,}") for i in range(30)]
    doc, ver = add_version(a, a.default_group_id, rows, "3" * 64)
    publish(a, doc, ver)
    approve_all(a)
    login(client, "admin-a@example.test")
    n = ask(client, "מחיר למ״ר בעסקאות שנחתמו ב-2024 בחרוזים")["answer"]["numeric"]
    assert n["record_count"] == 30 and n["mean_price_per_sqm"] == "21450.00"


def test_structured_path_makes_no_model_call(client, office, monkeypatch):
    calls = []
    monkeypatch.setattr("app.providers.llm.MockLLM.answer", lambda *a, **k: calls.append(1))
    login(client, "admin-a@example.test")
    ask(client, "מחיר למ״ר בעסקאות שנחתמו ב-2024 בחרוזים")
    assert calls == []
    with tenant_tx(office.system) as conn:
        assert conn.execute(text("SELECT count(*) FROM provider_usage")).scalar() == 0


def test_cache_hit_and_invalidation_on_correction(client, office):
    login(client, "admin-a@example.test")
    q = "מחיר למ״ר בעסקאות שנחתמו ב-2024 בחרוזים"
    first = ask(client, q)["answer"]
    second = ask(client, q)["answer"]
    assert second.get("cached") is True and second["numeric"] == first["numeric"]
    with tenant_tx(office.system) as conn:
        occ = conn.execute(text("SELECT id FROM occurrences WHERE address = 'הגפן 1'")).scalar_one()
    client.post(f"/api/review/records/{occ}/correct", json={"field": "price", "value": "1,100,000", "note": "תיקון"})
    third = ask(client, q)["answer"]
    assert third.get("cached") is None and third["numeric"]["mean_price_per_sqm"] == "26000.00"


def test_permission_scope_separates_cache_and_results(client, db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    g1, g2 = make_group(a, "G1"), make_group(a, "G2")
    make_user(a, "g1@example.test", [g1])
    d1, v1 = add_version(a, g1, AE3[:1], "1" * 64)
    d2, v2 = add_version(a, g2, AE3[1:], "2" * 64)
    publish(a, d1, v1)
    publish(a, d2, v2)
    approve_all(a)
    q = "מחיר למ״ר בעסקאות שנחתמו ב-2024 בחרוזים"
    login(client, "admin-a@example.test")
    assert ask(client, q)["answer"]["numeric"]["record_count"] == 2
    client.post("/api/auth/logout")
    login(client, "g1@example.test")
    a1 = ask(client, q)["answer"]
    assert a1.get("cached") is None and a1["numeric"]["record_count"] == 1
    assert {s["document_id"] for s in a1["sources"]} == {str(d1)}


def test_other_office_never_contributes(client, db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")
    da, va = add_version(a, a.default_group_id, AE3[:1], "1" * 64)
    db_, vb = add_version(b, b.default_group_id, AE3, "1" * 64)
    publish(a, da, va)
    publish(b, db_, vb)
    approve_all(a)
    approve_all(b)
    login(client, "admin-a@example.test")
    n = ask(client, "מחיר למ״ר בעסקאות שנחתמו ב-2024 בחרוזים")["answer"]
    assert n["numeric"]["record_count"] == 1 and all(s["document_id"] == str(da) for s in n["sources"])


def test_history_marks_stale_and_hides_deleted_sources(client, office):
    login(client, "admin-a@example.test")
    r = ask(client, "מחיר למ״ר בעסקאות שנחתמו ב-2024 בחרוזים")
    cid = r["conversation_id"]
    conv = client.get(f"/api/conversations/{cid}").json()
    assert conv["messages"][0]["stale"] is False
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE office_data_versions SET version = version + 1"))
    assert client.get(f"/api/conversations/{cid}").json()["messages"][0]["stale"] is True
    doc = r["answer"]["sources"][0]["document_id"]
    client.delete(f"/api/documents/{doc}")
    msg = client.get(f"/api/conversations/{cid}").json()["messages"][0]
    assert msg["hidden"] is True and msg["answer"] is None


def test_new_conversation_takes_title_from_first_question(client, office):
    login(client, "admin-a@example.test")
    cid = client.post("/api/conversations").json()["id"]
    ask(client, "מחיר למ״ר בעסקאות שנחתמו ב-2024 בחרוזים", conversation_id=cid)
    titles = {c["id"]: c["title"] for c in client.get("/api/conversations").json()["conversations"]}
    assert titles[cid] == "מחיר למ״ר בעסקאות שנחתמו ב-2024 בחרוזים"


def test_follow_up_year_conflicting_with_filter_asks(client, office):
    login(client, "admin-a@example.test")
    r = ask(client, "מחיר למ״ר בעסקאות שנחתמו ב-2024 בחרוזים", filters={"year_from": 2024})
    assert r["answer"]["kind"] == "numeric"
    r2 = ask(client, "ומה לגבי 2023?", conversation_id=r["conversation_id"], filters={"year_from": 2024})
    clar = r2["answer"]["clarification"]
    assert r2["answer"]["kind"] == "clarification" and clar["key"] == "filter_conflict"
    assert {o["value"] for o in clar["options"]} == {"year_from:2023", "year_from:2024"}
    r3 = ask(client, conversation_id=r["conversation_id"], clarification={"key": "filter_conflict", "value": "year_from:2023"})
    assert r3["answer"]["numeric"]["record_count"] == 1


def test_impossible_filter_range_is_a_clarification_not_an_error(client, office):
    login(client, "admin-a@example.test")
    r = client.post("/api/ask", json={"question": "מחיר למ״ר בעסקאות לפי תאריך עסקה בחרוזים",
                                      "filters": {"year_from": 2025, "year_to": 2023}})
    assert r.status_code == 200 and r.json()["answer"]["kind"] == "clarification"


# --- U6: structured attributes through compute_records (KTD7) ---------------------------------------

def records(office, column, operation, **conditions):
    from app.appraisal.query import compute_records

    c = QueryConditions(data_kind="transaction_price", **conditions)
    with tenant_tx(office.ctx()) as conn:
        return compute_records(conn, c, column, operation)


def test_price_per_sqm_through_compute_records_equals_compute_stats(office):
    from app.appraisal.query import compute_stats

    cond = {"neighborhood": "חרוזים", "date_field": "transaction_date", "year_from": 2024}
    with tenant_tx(office.ctx()) as conn:
        stats = compute_stats(conn, QueryConditions(data_kind="transaction_price", **cond))
    assert (stats.count, stats.mean, stats.weighted) == (2, Decimal("25000.00"), Decimal("26666.67"))
    got = {op: records(office, "transactions.price_per_sqm", op, **cond)
           for op in ("mean", "weighted_mean", "median", "min", "max", "count")}
    assert got["mean"].value == stats.mean and got["weighted_mean"].value == stats.weighted
    assert got["median"].value == stats.median
    assert (got["min"].value, got["max"].value) == (stats.minimum, stats.maximum)
    assert got["count"].value == stats.count == got["count"].count
    assert sorted(got["mean"].transaction_ids) == sorted(stats.transaction_ids)
    assert all(isinstance(r.value, Decimal) for op, r in got.items() if op != "count")


AREA_ROWS = [  # (address, block/parcel, date, rooms, area, price)
    ("הדקל 1", "6200/1", "10/01/2024", "3", "71.5", "1,500,000"),
    ("הדקל 2", "6200/2", "11/02/2024", "4.5", "98.25", "2,400,000"),
    ("הדקל 3", "6200/3", "12/03/2023", "5", "120", "3,100,000"),
    ("הדקל 4", "6200/4", "13/04/2024", "2", "55", "1,200,000"),
]
UNVERIFIED = ("הדקל 9", "6200/9", "14/05/2024", "6", "300", "9,000,000")


def _rows(items):
    return [[addr, bp, d, "דירה", rooms, area, "נטו", price, ""] for addr, bp, d, rooms, area, price in items]


@pytest.fixture
def area_office(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    d1, v1 = add_version(a, a.default_group_id, _rows(AREA_ROWS[:3]), "4" * 64)
    d2, v2 = add_version(a, a.default_group_id, _rows(AREA_ROWS[2:]), "5" * 64)  # הדקל 3 cited twice
    other = HEADER_TEXT.replace("שכונת העסקאות: חרוזים", "שכונת העסקאות: הבורסה")
    d3, v3 = add_version(a, a.default_group_id, _rows([("ז׳בוטינסקי 7", "6300/7", "10/01/2024", "3", "400", "9,000,000")]),
                         "6" * 64, header=other)
    d4, v4 = add_version(a, a.default_group_id, _rows([UNVERIFIED]), "7" * 64)
    for d, v in ((d1, v1), (d2, v2), (d3, v3), (d4, v4)):
        publish(a, d, v)
    approve_all(a)
    with tenant_tx(a.system) as conn:
        conn.execute(text("UPDATE occurrences SET verification_status = 'needs_review' WHERE address = :a"),
                     {"a": UNVERIFIED[0]})
        assert conn.execute(text("SELECT count(DISTINCT neighborhood) FROM occurrences")).scalar_one() == 2
    return a


def test_mean_area_over_verified_unique_transactions_equals_ground_truth(area_office):
    truth = [Decimal(area) for *_, area, _price in AREA_ROWS]  # each address once, unverified excluded
    expected = (sum(truth) / len(truth)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    r = records(area_office, "transactions.area", "mean", neighborhood="חרוזים")
    assert (r.count, r.value) == (len(truth), expected)
    assert isinstance(r.value, Decimal) and len(set(r.transaction_ids)) == r.count


def test_count_min_max_range_sum_values_on_structured_attributes(area_office):
    areas = sorted(Decimal(row[4]) for row in AREA_ROWS)
    rooms = sorted(Decimal(row[3]) for row in AREA_ROWS)
    n = "חרוזים"
    assert records(area_office, "transactions.area", "count", neighborhood=n).value == 4
    assert records(area_office, "transactions.area", "min", neighborhood=n).value == areas[0]
    assert records(area_office, "transactions.area", "max", neighborhood=n).value == areas[-1]
    rng = records(area_office, "transactions.area", "range", neighborhood=n)
    assert (rng.minimum, rng.maximum, rng.value) == (areas[0], areas[-1], areas[-1] - areas[0])
    assert records(area_office, "transactions.area", "sum", neighborhood=n).value == sum(areas)
    assert records(area_office, "transactions.area", "values", neighborhood=n).values == areas
    # rooms: one value per unique transaction, read from occurrences
    r = records(area_office, "occurrences.rooms", "values", neighborhood=n)
    assert r.values == rooms and r.count == 4
    assert records(area_office, "occurrences.rooms", "median", neighborhood=n).value == Decimal("3.75")
    y2024 = records(area_office, "transactions.price", "max", neighborhood=n, date_field="transaction_date",
                    year_from=2024)
    assert (y2024.count, y2024.value) == (3, Decimal("2400000.00"))
    everywhere = records(area_office, "transactions.area", "count")
    assert everywhere.value == 5  # the other neighborhood joins without a neighborhood filter


def test_non_whitelisted_column_from_a_plan_is_rejected(area_office):
    from app.appraisal.query import UnsupportedColumn

    for column in ("users.password_hash", "occurrences.address", "transactions.area) FROM users --"):
        with pytest.raises(UnsupportedColumn):
            records(area_office, column, "mean")
