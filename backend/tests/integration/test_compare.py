"""The two-sided compare tool (U8; R10, R11, R26, AE6, KTD10) and source re-authorization for compared
versions."""

import pytest
from sqlalchemy import text

from app.answering.compare import CompareSide, compose_comparison, gather_sides
from app.answering.compose import sources_still_authorized
from app.answering.content import log_usages, select_provider
from app.db import TenantContext, tenant_tx
from app.providers.llm import Purpose
from tests.factories import make_group, make_office, make_user
from tests.integration.test_search import _add_version, add_chunks
from tests.support.scripted_provider import ScriptedProvider

pytestmark = pytest.mark.db

QUESTION = "אילו הנחות השתנו בין הגרסאות?"
QUERIES = ["שיעור ההתאמה לגודל"]


def claim(t, ids):
    return {"text": t, "evidence_ids": ids, "kind": "explicit", "numbers": []}


def compare_answer(*claims, conflicts=()):
    return {"claims": list(claims), "insufficient": False, "missing_info": None,
            "conflicts": [{"datum": d, "claims": list(c)} for d, c in conflicts]}


def verdicts(n):
    return {"verdicts": [{"claim": i, "verdict": "supported"} for i in range(n)]}


@pytest.fixture
def office(db):
    return make_office(db, "משרד א", "admin-a@example.test")


def cloud(office, monkeypatch, provider):
    with tenant_tx(office.ctx()) as conn:
        conn.execute(text("UPDATE office_settings SET cloud_llm_enabled = true"))
    monkeypatch.setattr("app.answering.content.selected_provider_configured", lambda: True)
    monkeypatch.setattr("app.answering.content.get_selected_provider", lambda: provider)


def run(ctx, sides, queries=QUERIES):
    """The tool's three phases as the orchestrator runs them: gather, compose with no connection, log."""
    with tenant_tx(ctx) as conn:
        gathered = gather_sides(conn, QUESTION, sides, queries=queries)
        provider, state = select_provider(conn)
    out = compose_comparison(gathered, provider, state)
    with tenant_tx(ctx) as conn:
        log_usages(conn, provider, out.usage)
    return out


def usage(office):
    with tenant_tx(office.system) as conn:
        return [tuple(r) for r in conn.execute(text("SELECT purpose, ok, status FROM provider_usage ORDER BY id"))]


def test_two_versions_with_a_changed_rate_are_cited_and_labeled_by_version(office, monkeypatch):
    """AE6: the older version is read explicitly; each statement is labeled by its version."""
    doc, old_v = add_chunks(office, office.default_group_id, ["שיעור ההתאמה לגודל הוא 5%."], "e" * 64)
    new_v = _add_version(office, doc, ["שיעור ההתאמה לגודל הוא 7%."])
    p = ScriptedProvider().on(Purpose.ANSWER, compare_answer(
        claim("שיעור ההתאמה לגודל הוא 5%", ["E1"]), claim("שיעור ההתאמה לגודל הוא 7%", ["E2"]),
        conflicts=[("שיעור ההתאמה לגודל", (0, 1))])).on(Purpose.VERIFY, verdicts(2))
    cloud(office, monkeypatch, p)
    out = run(office.ctx(), [CompareSide(version_id=old_v), CompareSide(version_id=new_v)])
    a = out.answer
    assert a["kind"] == "content" and a["provider"] == "cloud" and a["compare"]["incomplete"] is False
    assert {s["version_id"] for s in a["sources"]} == {str(old_v), str(new_v)}
    assert "דוח eee, גרסה 1: שיעור ההתאמה לגודל הוא 5% [E1]" in a["text"]
    assert "דוח eee, גרסה 2: שיעור ההתאמה לגודל הוא 7% [E2]" in a["text"]
    assert [s["label"] for s in a["compare"]["conflicts"][0]["sides"]] == ["דוח eee, גרסה 1", "דוח eee, גרסה 2"]
    assert "הבדל בין המקורות לגבי שיעור ההתאמה לגודל" in a["text"]
    assert '<evidence id="E1" document="דוח eee" side="דוח eee, גרסה 1"' in p.calls[0].input
    assert all(s["explicit_version"] for s in a["sources"])
    assert out.cacheable
    with tenant_tx(office.ctx()) as conn:
        assert sources_still_authorized(conn, a["sources"])  # the superseded side stays valid as a compare side
        old = [s for s in a["sources"] if s["version_id"] == str(old_v)]
        assert not sources_still_authorized(conn, [dict(s, explicit_version=False) for s in old])
        conn.execute(text("UPDATE documents SET deleted_at = now() WHERE id = :d"), {"d": doc})
        assert not sources_still_authorized(conn, a["sources"])


def test_one_sided_evidence_gives_an_incomplete_comparison(office, monkeypatch):
    doc, old_v = add_chunks(office, office.default_group_id, ["שיעור ההתאמה לגודל הוא 5%."], "e" * 64)
    new_v = _add_version(office, doc, ["הדירה משופצת ומוארת."])
    p = ScriptedProvider()
    cloud(office, monkeypatch, p)
    out = run(office.ctx(), [CompareSide(version_id=old_v), CompareSide(version_id=new_v)])
    a = out.answer
    assert a["kind"] == "abstain" and a["compare"]["incomplete"] is True
    assert "ההשוואה אינה שלמה" in a["text"] and "דוח eee, גרסה 2" in a["text"]
    assert a["compare"]["missing_sides"] == ["דוח eee, גרסה 2"]
    assert p.calls == [] and not out.cacheable  # no one-sided comparison is composed or cached
    assert {s["version_id"] for s in a["sources"]} == {str(old_v)}


def test_conflict_between_two_documents_is_reported_with_both_sources(office, monkeypatch):
    d1, _ = add_chunks(office, office.default_group_id, ["שיעור ההיוון שנקבע בשומה הוא 5%."], "a" * 64)
    d2, _ = add_chunks(office, office.default_group_id, ["שיעור ההיוון שנקבע בשומה הוא 6%."], "b" * 64)
    p = ScriptedProvider().on(Purpose.ANSWER, compare_answer(
        claim("שיעור ההיוון הוא 5%", ["E1"]), claim("שיעור ההיוון הוא 6%", ["E2"]),
        conflicts=[("שיעור ההיוון", (0, 1))])).on(Purpose.VERIFY, verdicts(2))
    cloud(office, monkeypatch, p)
    a = run(office.ctx(), [CompareSide(document_id=d1), CompareSide(document_id=d2)], ["שיעור ההיוון"]).answer
    conflict = a["compare"]["conflicts"][0]
    assert conflict["datum"] == "שיעור ההיוון"
    assert [(s["label"], s["evidence_ids"]) for s in conflict["sides"]] == [("דוח aaa", ["E1"]), ("דוח bbb", ["E2"])]
    by_id = {s["evidence_id"]: s["document_id"] for s in a["sources"]}
    assert (by_id["E1"], by_id["E2"]) == (str(d1), str(d2))
    assert not any(s.get("explicit_version") for s in a["sources"])  # current versions keep the current check


def test_without_cloud_each_side_is_still_labeled(office):
    doc, old_v = add_chunks(office, office.default_group_id, ["שיעור ההתאמה לגודל הוא 5%."], "e" * 64)
    new_v = _add_version(office, doc, ["שיעור ההתאמה לגודל הוא 7%."])
    a = run(office.ctx(), [CompareSide(version_id=old_v), CompareSide(version_id=new_v)]).answer
    assert a["provider"] in ("mock", "extractive") and a["mode"] == "demo"
    assert "דוח eee, גרסה 1" in a["text"] and "דוח eee, גרסה 2" in a["text"]


def test_hidden_side_is_incomplete_without_revealing_it(office, monkeypatch):
    hidden = make_group(office, "נסתר")
    emp = make_user(office, "emp@example.test", [office.default_group_id])
    mine, _ = add_chunks(office, office.default_group_id, ["שיעור ההתאמה לגודל הוא 5%."], "c" * 64)
    secret, _ = add_chunks(office, hidden, ["סודי: שיעור ההתאמה לגודל הוא 9%."], "d" * 64)
    p = ScriptedProvider()
    cloud(office, monkeypatch, p)
    a = run(TenantContext(office.office_id, emp, "employee"),
            [CompareSide(document_id=mine), CompareSide(document_id=secret)]).answer
    assert a["compare"]["incomplete"] is True and p.calls == []
    assert "דוח ddd" not in a["text"] and "9%" not in a["text"]
    assert all(s["document_id"] == str(mine) for s in a["sources"])


def test_gather_reads_evidence_without_a_model_call_and_compose_reports_its_usage(office, monkeypatch):
    doc, old_v = add_chunks(office, office.default_group_id, ["שיעור ההתאמה לגודל הוא 5%."], "e" * 64)
    new_v = _add_version(office, doc, ["שיעור ההתאמה לגודל הוא 7%."])
    p = ScriptedProvider().on(Purpose.ANSWER, compare_answer(
        claim("שיעור ההתאמה לגודל הוא 5%", ["E1"]), claim("שיעור ההתאמה לגודל הוא 7%", ["E2"]))
    ).on(Purpose.VERIFY, verdicts(2))
    cloud(office, monkeypatch, p)
    with tenant_tx(office.ctx()) as conn:
        gathered = gather_sides(conn, QUESTION, [CompareSide(version_id=old_v), CompareSide(version_id=new_v)],
                                queries=QUERIES)
        provider, state = select_provider(conn)
    assert p.calls == [] and not gathered.missing
    assert [s["evidence_count"] for s in gathered.side_info] == [1, 1]
    out = compose_comparison(gathered, provider, state)
    assert [(u.purpose, u.ok) for u in out.usage] == [(Purpose.ANSWER, True), (Purpose.VERIFY, True)]
    assert usage(office) == []  # composing writes nothing; the caller logs afterwards
    with tenant_tx(office.ctx()) as conn:
        log_usages(conn, provider, out.usage)
    assert usage(office) == [("answer", True, "ok"), ("verify", True, "ok")]
    assert {r["version_id"] for r in out.source_rows} == {str(old_v), str(new_v)}


def test_verified_claims_from_one_side_only_give_an_incomplete_comparison(office, monkeypatch):
    """Both sides had evidence, but the answer says nothing from one side: not presented as a full comparison."""
    doc, old_v = add_chunks(office, office.default_group_id, ["שיעור ההתאמה לגודל הוא 5%."], "e" * 64)
    new_v = _add_version(office, doc, ["שיעור ההתאמה לגודל הוא 7%."])
    p = ScriptedProvider().on(Purpose.ANSWER, compare_answer(
        claim("שיעור ההתאמה לגודל הוא 7%", ["E2"]))).on(Purpose.VERIFY, verdicts(1))
    cloud(office, monkeypatch, p)
    out = run(office.ctx(), [CompareSide(version_id=old_v), CompareSide(version_id=new_v)])
    a = out.answer
    assert a["compare"]["incomplete"] is True and a["compare"]["missing_sides"] == ["דוח eee, גרסה 1"]
    assert any("נמצאה ראיה רק מצד אחד" in lim for lim in a["limitations"]) and not out.cacheable


def test_a_side_whose_claim_verification_dropped_is_quoted_with_its_version(office, monkeypatch):
    """GQ24: the model answered from both versions, the old version's claim did not pass verification; the old
    version is shown by the passage the model used, quoted and labeled with its version."""
    doc, old_v = add_chunks(office, office.default_group_id, ["שיעור ההתאמה לגודל הוא 5%."], "e" * 64)
    new_v = _add_version(office, doc, ["שיעור ההתאמה לגודל הוא 7%."])
    p = ScriptedProvider().on(Purpose.ANSWER, compare_answer(
        claim("שיעור ההתאמה לגודל הוא 6%", ["E1"]), claim("שיעור ההתאמה לגודל הוא 7%", ["E2"]))
    ).on(Purpose.VERIFY, verdicts(1))
    cloud(office, monkeypatch, p)
    out = run(office.ctx(), [CompareSide(version_id=old_v), CompareSide(version_id=new_v)])
    a = out.answer
    assert a["compare"]["incomplete"] is False and a["compare"]["missing_sides"] == []
    assert [c["evidence_ids"] for c in a["claims"]] == [["E2"], ["E1"]]
    assert "דוח eee, גרסה 1: ציטוט מהמסמך: „שיעור ההתאמה לגודל הוא 5%.” [E1]" in a["text"]
    assert any("דוח eee, גרסה 1" in lim and "כלשונו" in lim for lim in a["limitations"]) and out.cacheable


def test_claims_naming_their_version_label_are_kept(office, monkeypatch):
    """GQ24: "בגרסה 1 ... 5%" repeats the server's label "דוח eee, גרסה 1"; its "1" is a reference, not a figure."""
    doc, old_v = add_chunks(office, office.default_group_id, ["שיעור ההתאמה לגודל הוא 5%."], "e" * 64)
    new_v = _add_version(office, doc, ["שיעור ההתאמה לגודל הוא 7%."])
    p = ScriptedProvider().on(Purpose.ANSWER, compare_answer(
        claim("בגרסה 1 שיעור ההתאמה לגודל הוא 5%", ["E1"]), claim("בגרסה 2 שיעור ההתאמה לגודל הוא 7%", ["E2"]),
        conflicts=[("שיעור ההתאמה לגודל", (0, 1))])).on(Purpose.VERIFY, verdicts(2))
    cloud(office, monkeypatch, p)
    a = run(office.ctx(), [CompareSide(version_id=old_v), CompareSide(version_id=new_v)]).answer
    assert a["dropped_claims"] == 0 and a["compare"]["incomplete"] is False and len(a["compare"]["conflicts"]) == 1
    assert "דוח eee, גרסה 1: בגרסה 1 שיעור ההתאמה לגודל הוא 5% [E1]" in a["text"]


def test_a_side_with_no_datum_is_missing_not_a_conflict(office, monkeypatch):
    """GQ20 (AE6): the second document states no value; the model says so (``asserts_absence``) and lists a
    conflict. The comparison is incomplete with that side named, and nothing is shown as a difference."""
    d1, _ = add_chunks(office, office.default_group_id, ["שיעור ההיוון שנקבע בשומה הוא 5%."], "a" * 64)
    d2, _ = add_chunks(office, office.default_group_id, ["שיעור ההיוון לא נבחן בשומה זו."], "b" * 64)
    p = ScriptedProvider().on(Purpose.ANSWER, compare_answer(
        claim("שיעור ההיוון הוא 5%", ["E1"]),
        {**claim("בדוח bbb לא נמסר שיעור ההיוון", ["E2"]), "asserts_absence": True},
        conflicts=[("שיעור ההיוון", (0, 1))])).on(Purpose.VERIFY, verdicts(1))
    cloud(office, monkeypatch, p)
    out = run(office.ctx(), [CompareSide(document_id=d1), CompareSide(document_id=d2)], ["שיעור ההיוון"])
    a = out.answer
    assert a["compare"]["incomplete"] is True and a["compare"]["missing_sides"] == ["דוח bbb"]
    assert a["compare"]["conflicts"] == [] and "הבדל" not in a["text"]
    assert [c["evidence_ids"] for c in a["claims"]] == [["E1"]] and not out.cacheable


def test_every_claim_dropped_keeps_the_comparison_incomplete(office, monkeypatch):
    """GQ20 r4: with no verified claim left the comparison is incomplete and names every side."""
    doc, old_v = add_chunks(office, office.default_group_id, ["שיעור ההתאמה לגודל הוא 5%."], "e" * 64)
    new_v = _add_version(office, doc, ["שיעור ההתאמה לגודל הוא 7%."])
    p = ScriptedProvider().on(Purpose.ANSWER, compare_answer(
        claim("שיעור ההתאמה לגודל הוא 6%", ["E1"]), claim("שיעור ההתאמה לגודל הוא 8%", ["E2"])))
    cloud(office, monkeypatch, p)
    out = run(office.ctx(), [CompareSide(version_id=old_v), CompareSide(version_id=new_v)])
    a = out.answer
    assert a["provider"] == "extractive" and a["compare"]["incomplete"] is True and not out.cacheable
    assert a["compare"]["missing_sides"] == ["דוח eee, גרסה 1", "דוח eee, גרסה 2"]


def test_past_the_turn_deadline_the_comparison_makes_no_model_call(office, monkeypatch):
    import time

    doc, old_v = add_chunks(office, office.default_group_id, ["שיעור ההתאמה לגודל הוא 5%."], "e" * 64)
    new_v = _add_version(office, doc, ["שיעור ההתאמה לגודל הוא 7%."])
    p = ScriptedProvider().on(Purpose.ANSWER, compare_answer(claim("שיעור ההתאמה לגודל הוא 5%", ["E1"])))
    cloud(office, monkeypatch, p)
    with tenant_tx(office.ctx()) as conn:
        gathered = gather_sides(conn, QUESTION, [CompareSide(version_id=old_v), CompareSide(version_id=new_v)],
                                queries=QUERIES)
        provider, state = select_provider(conn)
    out = compose_comparison(gathered, provider, state, deadline=time.monotonic())
    assert p.calls == [] and out.answer["provider"] == "extractive" and not out.cacheable
    assert [(u.purpose, str(u.status)) for u in out.usage] == [(Purpose.ANSWER, "timeout")]
