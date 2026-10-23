"""A document's source kind at upload and its change by an admin (round 10 U2: KTD8; R1, R16, R17, R20).

The kind is chosen at upload (``appraisal`` by default, ``standard``, ``professional``), applies to a new document
only, is never read from a file name, and is listed with the document. An admin changes it afterwards with a PATCH
that is audited and moves the data version; an employee cannot, and another office's document is a 404. A standard
lives in a group like any document: outside the group it is not seen. Synthetic data only."""

import pytest
from sqlalchemy import text

from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_document, make_group, make_office, make_user
from tests.helpers import tiny_pdf, upload

pytestmark = pytest.mark.db


@pytest.fixture
def office(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")
    return a, b


def kind_of(office, document_id) -> str:
    with tenant_tx(office.ctx()) as conn:
        return conn.execute(text("SELECT source_kind FROM documents WHERE id = :d"), {"d": document_id}).scalar_one()


def post_kind(client, name: str, data: bytes, group, kind: str | None, document_id=None):
    form = {"group_id": str(group)} if document_id is None else {"document_id": str(document_id)}
    if kind is not None:
        form["source_kind"] = kind
    return client.post("/api/documents", files=[("files", (name, data, "application/octet-stream"))], data=form)


@pytest.mark.parametrize("kind", ["standard", "professional", "appraisal"])
def test_an_upload_stores_the_requested_kind_and_lists_it(client, office, kind):
    a, _ = office
    login(client, "admin-a@example.test")
    r = post_kind(client, "doc.pdf", tiny_pdf("k-" + kind), a.default_group_id, kind)
    res = r.json()["results"][0]
    assert r.status_code == 200 and res["status"] == "accepted"
    assert kind_of(a, res["document_id"]) == kind
    assert client.get(f"/api/documents/{res['document_id']}").json()["source_kind"] == kind
    listed = client.get("/api/documents").json()["documents"]
    assert [d["source_kind"] for d in listed] == [kind]


def test_an_upload_without_the_field_is_an_appraisal(client, office):
    a, _ = office
    login(client, "admin-a@example.test")
    res = upload(client, [("plain.pdf", tiny_pdf("plain"))], group_id=a.default_group_id).json()["results"][0]
    assert kind_of(a, res["document_id"]) == "appraisal"
    assert post_kind(client, "blank.pdf", tiny_pdf("blank"), a.default_group_id, "").json()["results"][0]["status"] \
        == "accepted"  # an empty field is the same as none


def test_an_invalid_kind_is_refused_with_a_hebrew_reason_and_stores_nothing(client, office):
    a, _ = office
    login(client, "admin-a@example.test")
    r = post_kind(client, "bad.pdf", tiny_pdf("bad"), a.default_group_id, "guide")
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert isinstance(detail, str) and any("א" <= ch <= "ת" for ch in detail) and "standard" in detail
    with tenant_tx(a.ctx()) as conn:
        assert conn.execute(text("SELECT count(*) FROM documents")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM jobs")).scalar_one() == 0


def test_a_file_named_like_a_standard_is_an_appraisal_when_no_kind_is_given(client, office):
    a, _ = office
    login(client, "admin-a@example.test")
    res = upload(client, [("תקן 19 - X.pdf", tiny_pdf("takan19"))], group_id=a.default_group_id).json()["results"][0]
    assert res["status"] == "accepted"
    assert kind_of(a, res["document_id"]) == "appraisal"  # R17: never inferred from the name


def test_a_new_version_keeps_the_kind_of_its_document(client, office):
    a, _ = office
    login(client, "admin-a@example.test")
    first = post_kind(client, "s.pdf", tiny_pdf("v1"), a.default_group_id, "standard").json()["results"][0]
    doc = first["document_id"]
    for requested in (None, "appraisal", "professional"):
        res = post_kind(client, "s2.pdf", tiny_pdf(f"v-{requested}"), None, requested, document_id=doc)
        assert res.json()["results"][0]["status"] == "accepted"
        assert kind_of(a, doc) == "standard"
    assert post_kind(client, "s3.pdf", tiny_pdf("v-bad"), None, "guide", document_id=doc).status_code == 422


def test_an_employee_who_may_upload_stores_a_standard_in_a_group_of_theirs_only(client, office):
    a, _ = office
    mine, other = make_group(a, "קבוצה שלי"), make_group(a, "קבוצה אחרת")
    make_user(a, "uploader@example.test", [mine], can_upload=True)
    login(client, "uploader@example.test")
    res = post_kind(client, "p.pdf", tiny_pdf("p"), mine, "professional").json()["results"][0]
    assert kind_of(a, res["document_id"]) == "professional"
    assert post_kind(client, "q.pdf", tiny_pdf("q"), other, "standard").status_code == 403


def test_the_same_file_held_in_a_group_the_user_cannot_see_is_cloned_with_its_own_kind(client, office):
    a, _ = office
    g1, g2 = make_group(a, "G1"), make_group(a, "G2")
    make_user(a, "u1@example.test", [g1], can_upload=True)
    pdf = tiny_pdf("shared-kind")
    login(client, "admin-a@example.test")
    hidden = post_kind(client, "s.pdf", pdf, g2, "appraisal").json()["results"][0]
    with tenant_tx(a.ctx()) as conn:  # the hidden copy is already read, so the same file is cloned from it
        conn.execute(text("UPDATE document_versions SET status = 'ready' WHERE id = :v"), {"v": hidden["version_id"]})
    client.post("/api/auth/logout")
    login(client, "u1@example.test")
    res = post_kind(client, "s.pdf", pdf, g1, "standard").json()["results"][0]
    assert res["status"] == "accepted" and res["document_id"] != hidden["document_id"]
    assert kind_of(a, res["document_id"]) == "standard"  # the clone keeps the kind asked of its own document
    assert kind_of(a, hidden["document_id"]) == "appraisal"
    with tenant_tx(a.ctx()) as conn:
        assert conn.execute(text("SELECT cloned_from_version_id IS NOT NULL FROM document_versions WHERE document_id = :d"),
                            {"d": res["document_id"]}).scalar_one()
    again = post_kind(client, "s.pdf", pdf, g1, "professional").json()["results"][0]
    assert again["status"] == "duplicate" and again["document_id"] == res["document_id"]  # as today
    assert kind_of(a, res["document_id"]) == "standard"


def test_an_admin_changes_the_kind_with_an_audit_entry_and_a_new_data_version(client, office):
    a, _ = office
    doc, _ver = make_document(a, a.default_group_id, title="מסמך סינתטי")
    login(client, "admin-a@example.test")
    with tenant_tx(a.ctx()) as conn:
        before = conn.execute(text("SELECT version FROM office_data_versions")).scalar_one()
    r = client.patch(f"/api/documents/{doc}", json={"source_kind": "professional"})
    assert r.status_code == 200 and r.json()["source_kind"] == "professional"
    assert kind_of(a, doc) == "professional"
    with tenant_tx(a.ctx()) as conn:
        assert conn.execute(text("SELECT version FROM office_data_versions")).scalar_one() == before + 1
        event = conn.execute(text("SELECT user_id, target_type, target_id, details FROM audit_events"
                                  " WHERE action = 'document_kind_change'")).one()
    assert (event.user_id, event.target_type, event.target_id) == (a.admin_id, "document", str(doc))
    assert event.details == {"from": "appraisal", "to": "professional"}
    # nothing was removed: the document and its version are as they were
    with tenant_tx(a.ctx()) as conn:
        assert conn.execute(text("SELECT count(*) FROM document_versions WHERE document_id = :d AND is_current"),
                            {"d": doc}).scalar_one() == 1


def test_a_change_to_the_same_kind_or_an_invalid_one_changes_nothing(client, office):
    a, _ = office
    doc, _ver = make_document(a, a.default_group_id, title="תקן סינתטי", source_kind="standard")
    login(client, "admin-a@example.test")
    assert client.patch(f"/api/documents/{doc}", json={"source_kind": "guide"}).status_code == 422
    assert client.patch(f"/api/documents/{doc}", json={}).status_code == 422
    assert client.patch(f"/api/documents/{doc}", json={"source_kind": "standard", "title": "x"}).status_code == 422
    with tenant_tx(a.ctx()) as conn:
        before = conn.execute(text("SELECT version FROM office_data_versions")).scalar_one()
    r = client.patch(f"/api/documents/{doc}", json={"source_kind": "standard"})
    assert r.status_code == 200 and r.json()["source_kind"] == "standard"
    with tenant_tx(a.ctx()) as conn:
        assert conn.execute(text("SELECT version FROM office_data_versions")).scalar_one() == before
        assert conn.execute(text("SELECT count(*) FROM audit_events WHERE action = 'document_kind_change'")).scalar_one() == 0


def test_an_employee_cannot_change_the_kind_and_another_offices_admin_gets_a_404(client, office):
    a, _ = office
    make_user(a, "emp@example.test", [a.default_group_id], can_upload=True)
    doc, _ver = make_document(a, a.default_group_id, title="מסמך סינתטי")
    login(client, "emp@example.test")
    assert client.patch(f"/api/documents/{doc}", json={"source_kind": "standard"}).status_code == 403
    assert kind_of(a, doc) == "appraisal"
    client.post("/api/auth/logout")
    login(client, "admin-b@example.test")
    assert client.patch(f"/api/documents/{doc}", json={"source_kind": "standard"}).status_code == 404
    assert client.patch("/api/documents/not-a-uuid", json={"source_kind": "standard"}).status_code == 404
    assert kind_of(a, doc) == "appraisal"


def test_a_deleted_document_cannot_have_its_kind_changed(client, office):
    a, _ = office
    doc, _ver = make_document(a, a.default_group_id, title="מסמך סינתטי")
    login(client, "admin-a@example.test")
    assert client.delete(f"/api/documents/{doc}").status_code == 200
    assert client.patch(f"/api/documents/{doc}", json={"source_kind": "standard"}).status_code == 404


def test_a_standard_is_seen_only_through_its_group_and_never_by_another_office(client, office):
    a, _ = office
    closed = make_group(a, "קבוצה סגורה")
    make_user(a, "inside@example.test", [a.default_group_id, closed])
    make_user(a, "outside@example.test", [a.default_group_id])
    doc, _ver = make_document(a, closed, title="תקן בדיקה סינתטי — אינו תקן אמיתי", source_kind="standard")
    login(client, "inside@example.test")
    assert [d["id"] for d in client.get("/api/documents").json()["documents"]] == [str(doc)]
    client.post("/api/auth/logout")
    login(client, "outside@example.test")
    assert client.get("/api/documents").json()["documents"] == []
    assert client.get(f"/api/documents/{doc}").status_code == 404
    client.post("/api/auth/logout")
    login(client, "admin-b@example.test")
    assert client.get("/api/documents").json()["documents"] == []
    assert client.get(f"/api/documents/{doc}").status_code == 404
