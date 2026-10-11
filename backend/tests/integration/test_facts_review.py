"""Facts review: list, approve, reject, correct, group scoping and read-time conflicts (U11, KTD15, KTD9, R16)."""

from __future__ import annotations

import json
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import text

from app.answering import facts
from app.answering.attributes import AttributeDef, resolve_attribute
from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_document, make_group, make_office, make_user

pytestmark = pytest.mark.db

BP = "bp:6158/40/-"


# --- builders ---------------------------------------------------------------------------------------

def make_attr(office, label="שטח ממ״ד", dimension="area") -> AttributeDef:
    with tenant_tx(office.ctx()) as conn:
        return resolve_attribute(conn, handle=None, description=label, unit_dimension=dimension)


def add_fact(office, attr: AttributeDef, group_id, title, value, status, *, entity_key=None, unit="sqm", page=3,
             role="subject", quote=None) -> tuple[UUID, UUID, UUID]:
    """A current version with one stored fact and a ``found`` ledger entry. Returns (fact, document, version)."""
    doc, ver = make_document(office, group_id, title, sha=title.encode().hex().ljust(64, "0")[:64])
    ext = facts.extraction_version(attr)
    with tenant_tx(office.system) as conn:
        fid = conn.execute(text(
            "INSERT INTO facts (office_id, document_id, version_id, attribute_id, entity_role, entity_key,"
            " entity_descriptor, value_numeric, value_text, unit, canonical_value, quote, source_path,"
            " extraction_version, model, status) VALUES (app_office(), :d, :v, :a, :role, :key, :desc, :num, :txt,"
            " :unit, :canon, :quote, CAST(:sp AS jsonb), :e, 'scripted', :s) RETURNING id"),
            {"d": doc, "v": ver, "a": attr.id, "role": role, "key": entity_key or f"doc:{doc}",
             "desc": "גוש 6158 חלקה 40" if entity_key else None, "num": Decimal(value), "txt": value,
             "unit": unit, "canon": Decimal(value), "quote": quote or f"שטח הממ״ד {value} מ״ר",
             "sp": json.dumps({"page": page, "chunk_id": None, "handle": "C1"}), "e": ext, "s": status},
        ).scalar_one()
        conn.execute(text(
            "INSERT INTO fact_extraction_ledger (office_id, document_id, version_id, attribute_id, extraction_version,"
            " state) VALUES (app_office(), :d, :v, :a, :e, 'found') ON CONFLICT DO NOTHING"),
            {"d": doc, "v": ver, "a": attr.id, "e": ext})
    return fid, doc, ver


def compute(ctx, attr, operation="mean"):
    with tenant_tx(ctx) as conn:
        return facts.compute_facts(conn, attr, None, operation)


def facts_version(office, attr) -> int:
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT facts_version FROM attribute_definitions WHERE id = :a"),
                            {"a": attr.id}).scalar_one()


def fact_row(office, fid):
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT * FROM facts WHERE id = :f"), {"f": fid}).one()


def audit_actions(office) -> list[str]:
    with tenant_tx(office.system) as conn:
        return list(conn.execute(text("SELECT action FROM audit_events WHERE target_type = 'fact'"
                                      " ORDER BY created_at")).scalars())


def listed(client) -> dict[str, dict]:
    """Fact id -> listed fact, across all attribute and document groups."""
    r = client.get("/api/review/facts")
    assert r.status_code == 200, r.text
    out = {}
    for group in r.json()["attributes"]:
        for doc in group["documents"]:
            for f in doc["facts"]:
                out[f["id"]] = f
    return out


@pytest.fixture
def office(db):
    return make_office(db, "משרד א", "admin-a@example.test")


# --- listing ------------------------------------------------------------------------------------------

def test_list_groups_by_attribute_then_document_with_needs_review_first(client, office):
    mamad = make_attr(office)
    balcony = make_attr(office, "שטח מרפסת")
    g = office.default_group_id
    auto, _, ver = add_fact(office, mamad, g, "דוח א", "12", "auto_validated", page=4)
    review, _, _ = add_fact(office, mamad, g, "דוח ב", "9", "needs_review")
    add_fact(office, mamad, g, "דוח ג", "11", "verified")  # already reviewed: not listed
    add_fact(office, mamad, g, "דוח ד", "7", "rejected")
    other, _, _ = add_fact(office, balcony, g, "דוח ה", "6", "auto_validated")
    login(client, "admin-a@example.test")
    body = client.get("/api/review/facts").json()
    groups = {grp["attribute"]["label"]: grp for grp in body["attributes"]}
    assert set(groups) == {"שטח ממ״ד", "שטח מרפסת"}
    assert body["attributes"][0]["attribute"]["label"] == "שטח ממ״ד"  # holds a needs_review fact
    docs = groups["שטח ממ״ד"]["documents"]
    assert [d["document"]["title"] for d in docs] == ["דוח ב", "דוח א"]
    assert [f["id"] for d in docs for f in d["facts"]] == [str(review), str(auto)]
    f = docs[1]["facts"][0]
    assert f["status"] == "auto_validated" and f["value"] == "12" and f["unit_label"] == "מ״ר"
    assert f["quote"] == "שטח הממ״ד 12 מ״ר" and f["page"] == 4
    assert f["url"].endswith(f"/versions/{ver}/file#page=4")
    attr = groups["שטח ממ״ד"]["attribute"]
    assert attr["canonical_unit"] == "sqm" and {"sqm", "dunam"} <= {o["code"] for o in attr["unit_options"]}
    assert "ILS" not in {o["code"] for o in attr["unit_options"]}
    assert [f["id"] for d in groups["שטח מרפסת"]["documents"] for f in d["facts"]] == [str(other)]


def test_empty_list(client, office):
    login(client, "admin-a@example.test")
    assert client.get("/api/review/facts").json() == {"attributes": []}


# --- approve ------------------------------------------------------------------------------------------

def test_approving_auto_validated_moves_it_into_main_figure_and_bumps_facts_version(client, office):
    attr = make_attr(office)
    other = make_attr(office, "שטח מרפסת")
    fid, _, _ = add_fact(office, attr, office.default_group_id, "דוח א", "12", "auto_validated")
    before = compute(office.ctx(), attr)
    assert before.main.n == 0 and before.preliminary.n == 1
    v0, other_v0 = facts_version(office, attr), facts_version(office, other)

    login(client, "admin-a@example.test")
    r = client.post(f"/api/review/facts/{fid}/approve", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "verified" and r.json()["facts_version"] == v0 + 1

    # a cache entry keyed on the old facts_version can no longer match; only this attribute moved
    assert facts_version(office, attr) == v0 + 1 and facts_version(office, other) == other_v0
    after = compute(office.ctx(), attr)
    assert after.facts_version == v0 + 1
    assert after.main.n == 1 and after.main.value == Decimal("12") and after.preliminary is None
    assert after.sources[0]["tier"] == "verified"
    row = fact_row(office, fid)
    assert row.reviewed_by == office.admin_id and row.reviewed_at is not None
    assert row.previous[0]["action"] == "approve" and row.previous[0]["status"] == "auto_validated"
    assert audit_actions(office) == ["fact_approve"]
    assert str(fid) not in listed(client)


def test_employee_in_the_group_may_review(client, office):
    g = make_group(office, "צוות")
    make_user(office, "emp@example.test", [g])
    attr = make_attr(office)
    fid, _, _ = add_fact(office, attr, g, "דוח א", "12", "needs_review")
    login(client, "emp@example.test")
    assert str(fid) in listed(client)
    assert client.post(f"/api/review/facts/{fid}/approve", json={"note": "נבדק מול הנסח"}).json()["status"] == "verified"
    assert fact_row(office, fid).review_note == "נבדק מול הנסח"


# --- correct ------------------------------------------------------------------------------------------

def test_correct_records_previous_value_and_source_and_recomputes(client, office):
    attr = make_attr(office)
    fid, _, ver = add_fact(office, attr, office.default_group_id, "דוח א", "12", "needs_review", page=5)
    add_fact(office, attr, office.default_group_id, "דוח ב", "10", "verified")
    assert compute(office.ctx(), attr).main.values == [Decimal("10")]
    v0 = facts_version(office, attr)

    login(client, "admin-a@example.test")
    r = client.post(f"/api/review/facts/{fid}/correct", json={"value": "0.0145", "unit": "dunam", "note": "לפי תשריט"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "corrected" and d["value"] == "14.5" and d["facts_version"] == v0 + 1
    prev = d["previous"][0]
    assert prev["action"] == "correct" and prev["status"] == "needs_review"
    assert prev["value_numeric"] == "12" and prev["canonical_value"] == "12" and prev["unit"] == "sqm"
    assert prev["quote"] == "שטח הממ״ד 12 מ״ר" and prev["source_path"]["page"] == 5
    row = fact_row(office, fid)
    assert row.canonical_value == Decimal("14.5") and row.unit == "dunam" and row.value_numeric == Decimal("0.0145")
    assert row.review_note == "לפי תשריט"
    comp = compute(office.ctx(), attr)
    assert comp.main.values == [Decimal("10"), Decimal("14.5")] and comp.main.value == Decimal("12.25")
    assert audit_actions(office) == ["fact_correct"]


def test_invalid_correction_is_422_with_hebrew_message(client, office):
    attr = make_attr(office)
    fid, _, _ = add_fact(office, attr, office.default_group_id, "דוח א", "12", "needs_review")
    v0 = facts_version(office, attr)
    login(client, "admin-a@example.test")
    wrong_unit = client.post(f"/api/review/facts/{fid}/correct", json={"value": "14", "unit": "ILS"})
    assert wrong_unit.status_code == 422 and "יחידה" in wrong_unit.json()["detail"]
    no_unit = client.post(f"/api/review/facts/{fid}/correct", json={"value": "14", "unit": ""})
    assert no_unit.status_code == 422 and "יחידה" in no_unit.json()["detail"]
    for bad in ("abc", "12abc", "-3", ""):
        r = client.post(f"/api/review/facts/{fid}/correct", json={"value": bad, "unit": "sqm"})
        assert r.status_code == 422 and "מספר" in r.json()["detail"], bad
    assert fact_row(office, fid).status == "needs_review" and facts_version(office, attr) == v0
    assert audit_actions(office) == []


def test_unitless_dimension_accepts_a_plain_number(client, office):
    attr = make_attr(office, "מספר קומות במבנה", "count")
    fid, _, _ = add_fact(office, attr, office.default_group_id, "דוח א", "4", "needs_review", unit="floor")
    login(client, "admin-a@example.test")
    options = client.get(f"/api/review/facts/{fid}").json()["attribute"]["unit_options"]
    assert options == [{"code": "", "label": "ללא יחידה"}]
    assert client.post(f"/api/review/facts/{fid}/correct", json={"value": "5", "unit": "sqm"}).status_code == 422
    r = client.post(f"/api/review/facts/{fid}/correct", json={"value": "5", "unit": ""})
    assert r.status_code == 200, r.text
    assert r.json()["value"] == "5"


# --- reject -------------------------------------------------------------------------------------------

def test_reject_removes_from_both_figures_and_counts_as_rejected(client, office):
    attr = make_attr(office)
    auto, _, _ = add_fact(office, attr, office.default_group_id, "דוח א", "10", "auto_validated")
    verified, _, _ = add_fact(office, attr, office.default_group_id, "דוח ב", "14", "verified")
    before = compute(office.ctx(), attr)
    assert before.main.n == 1 and before.preliminary.n == 2 and before.coverage["rejected"] == 0

    login(client, "admin-a@example.test")
    assert client.post(f"/api/review/facts/{auto}/reject", json={"note": " "}).status_code == 422
    assert fact_row(office, auto).status == "auto_validated"
    r = client.post(f"/api/review/facts/{auto}/reject", json={"note": "הערך שייך לנכס אחר"})
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    mid = compute(office.ctx(), attr)
    assert mid.preliminary is None and mid.main.values == [Decimal("14")] and mid.coverage["rejected"] == 1

    client.post(f"/api/review/facts/{verified}/reject", json={"note": "טעות"})
    after = compute(office.ctx(), attr)
    assert after.main.n == 0 and after.preliminary is None and after.coverage["rejected"] == 2
    assert audit_actions(office) == ["fact_reject", "fact_reject"]
    assert str(auto) not in listed(client)


# --- group scoping and conflicts ----------------------------------------------------------------------

def test_employee_without_the_group_gets_404_on_view_and_act(client, office):
    g1, g2 = make_group(office, "צוות 1"), make_group(office, "צוות 2")
    make_user(office, "emp@example.test", [g1])
    attr = make_attr(office)
    hidden, _, _ = add_fact(office, attr, g2, "דוח חסוי", "12", "needs_review")
    v0 = facts_version(office, attr)
    login(client, "emp@example.test")
    assert str(hidden) not in listed(client)
    assert client.get(f"/api/review/facts/{hidden}").status_code == 404
    assert client.post(f"/api/review/facts/{hidden}/approve", json={}).status_code == 404
    assert client.post(f"/api/review/facts/{hidden}/reject", json={"note": "x"}).status_code == 404
    assert client.post(f"/api/review/facts/{hidden}/correct", json={"value": "1", "unit": "sqm"}).status_code == 404
    assert client.get("/api/review/facts/not-a-uuid").status_code == 404
    assert fact_row(office, hidden).status == "needs_review" and facts_version(office, attr) == v0
    assert audit_actions(office) == []


def test_other_office_gets_404(client, office, db):
    make_office(db, "משרד ב", "admin-b@example.test")
    attr = make_attr(office)
    fid, _, _ = add_fact(office, attr, office.default_group_id, "דוח א", "12", "needs_review")
    login(client, "admin-b@example.test")
    assert client.get(f"/api/review/facts/{fid}").status_code == 404
    assert client.post(f"/api/review/facts/{fid}/approve", json={}).status_code == 404
    assert listed(client) == {}


def test_conflict_with_hidden_group_document_is_invisible_to_employee_and_visible_to_admin(client, office):
    g1, g2 = make_group(office, "צוות 1"), make_group(office, "צוות 2")
    make_user(office, "emp@example.test", [g1])
    attr = make_attr(office)
    seen, _, _ = add_fact(office, attr, g1, "דוח גלוי", "12", "auto_validated", entity_key=BP)
    hidden, _, _ = add_fact(office, attr, g2, "דוח חסוי", "15", "verified", entity_key=BP)

    login(client, "admin-a@example.test")
    admin_view = listed(client)
    conflicts = admin_view[str(seen)]["conflicts"]
    assert [(c["id"], c["value"], c["document"]["title"]) for c in conflicts] == [(str(hidden), "15", "דוח חסוי")]
    assert conflicts[0]["quote"] == "שטח הממ״ד 15 מ״ר" and conflicts[0]["url"].endswith("#page=3")
    assert client.get(f"/api/review/facts/{seen}").json()["conflicts"][0]["id"] == str(hidden)

    client.post("/api/auth/logout")
    login(client, "emp@example.test")
    emp_view = listed(client)
    assert set(emp_view) == {str(seen)} and emp_view[str(seen)]["conflicts"] == []
    detail = client.get(f"/api/review/facts/{seen}").json()
    assert detail["conflicts"] == [] and "דוח חסוי" not in json.dumps(detail, ensure_ascii=False)


def test_equal_values_for_the_same_entity_are_not_a_conflict(client, office):
    attr = make_attr(office)
    a, _, _ = add_fact(office, attr, office.default_group_id, "דוח א", "12", "needs_review", entity_key=BP)
    add_fact(office, attr, office.default_group_id, "דוח ב", "12.0", "verified", entity_key=BP)
    login(client, "admin-a@example.test")
    assert listed(client)[str(a)]["conflicts"] == []


# --- stale-list preconditions -------------------------------------------------------------------------

def test_stale_approve_after_reject_is_409_and_changes_nothing(client, office):
    attr = make_attr(office)
    fid, _, _ = add_fact(office, attr, office.default_group_id, "דוח א", "12", "needs_review")
    login(client, "admin-a@example.test")
    seen = listed(client)[str(fid)]
    pre = {"expected_status": seen["status"], "expected_value": seen["value"]}
    # another reviewer rejects it after this list was loaded
    r = client.post(f"/api/review/facts/{fid}/reject", json={"note": "נכס אחר", **pre})
    assert r.status_code == 200, r.text
    v1 = facts_version(office, attr)

    stale = client.post(f"/api/review/facts/{fid}/approve", json={"expected_status": seen["status"]})
    assert stale.status_code == 409 and stale.json()["detail"] == "הערך השתנה בינתיים; טענו את הרשימה מחדש"
    row = fact_row(office, fid)
    assert row.status == "rejected" and row.review_note == "נכס אחר" and len(row.previous) == 1
    assert facts_version(office, attr) == v1
    assert audit_actions(office) == ["fact_reject"]


def test_stale_correct_after_another_correct_is_409_and_keeps_the_first_correction(client, office):
    attr = make_attr(office)
    fid, _, _ = add_fact(office, attr, office.default_group_id, "דוח א", "12", "needs_review")
    login(client, "admin-a@example.test")
    seen = listed(client)[str(fid)]
    pre = {"expected_status": seen["status"], "expected_value": seen["value"]}
    first = client.post(f"/api/review/facts/{fid}/correct", json={"value": "14", "unit": "sqm", **pre})
    assert first.status_code == 200, first.text
    v1 = facts_version(office, attr)

    stale = client.post(f"/api/review/facts/{fid}/correct", json={"value": "15", "unit": "sqm", **pre})
    assert stale.status_code == 409 and "השתנה" in stale.json()["detail"]
    # the value alone is checked too: the status matches, the value moved on
    by_value = client.post(f"/api/review/facts/{fid}/correct",
                           json={"value": "15", "unit": "sqm", "expected_status": "corrected", "expected_value": "12"})
    assert by_value.status_code == 409
    row = fact_row(office, fid)
    assert row.status == "corrected" and row.canonical_value == Decimal("14") and len(row.previous) == 1
    assert facts_version(office, attr) == v1
    assert audit_actions(office) == ["fact_correct"]


def test_matching_precondition_is_applied(client, office):
    attr = make_attr(office)
    approve_id, _, _ = add_fact(office, attr, office.default_group_id, "דוח א", "12", "auto_validated")
    correct_id, _, _ = add_fact(office, attr, office.default_group_id, "דוח ב", "9", "needs_review")
    reject_id, _, _ = add_fact(office, attr, office.default_group_id, "דוח ג", "7", "needs_review")
    login(client, "admin-a@example.test")
    seen = listed(client)

    def pre(fid):
        return {"expected_status": seen[str(fid)]["status"], "expected_value": seen[str(fid)]["value"]}

    r = client.post(f"/api/review/facts/{approve_id}/approve", json={"expected_status": "auto_validated"})
    assert r.status_code == 200 and r.json()["status"] == "verified", r.text
    # a number compares by value: "9.0" matches the listed "9"
    r = client.post(f"/api/review/facts/{correct_id}/correct",
                    json={"value": "10", "unit": "sqm", "expected_status": "needs_review", "expected_value": "9.0"})
    assert r.status_code == 200 and r.json()["value"] == "10", r.text
    r = client.post(f"/api/review/facts/{reject_id}/reject", json={"note": "טעות", **pre(reject_id)})
    assert r.status_code == 200 and r.json()["status"] == "rejected", r.text
    assert audit_actions(office) == ["fact_approve", "fact_correct", "fact_reject"]
