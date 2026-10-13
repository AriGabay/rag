"""The stored profile of a standard and its editions (round 10 U5: R18, R19, R30; KD7, KD12; KTD10).

``ensure_profile`` derives the metadata a standard states about itself from its stored blocks, once per version and
reading, with the quote of each value; the model pass runs only when the office allows cloud use and a value is kept
only through the quote check. Editions group by normalized standard number and are ordered by stated dates, never by
upload order, with an explicit ``order_known``. Synthetic data only: every text says so and the numbers are invented
(98.x, 97.x, 15, 11)."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text

from app import professional, worker
from app.db import tenant_tx
from app.platform import pipeline
from app.professional import profile as P
from app.providers.llm import CallStatus, Purpose
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.integration.test_documents_kind import post_kind
from tests.support.professional_files import MARK, professional_file
from tests.support.scripted_provider import ScriptedProvider

pytestmark = pytest.mark.db

D1 = Path(__file__).resolve().parents[1] / "fixtures" / "D1_synthetic_harozim_digital.pdf"


@pytest.fixture
def office(db):
    return make_office(db, "משרד א", "admin-a@example.test")


def rows(number="98.1", published=None, effective=None, edition="גרסה 2.0", issuer=True, title_line="מדידת שטחים סינתטית"):
    """A synthetic standard's blocks: a number line with the title, an edition, dates, an issuer line."""
    out = [("paragraph", None, [], MARK, 1), ("heading", None, [], f"תקן מס' {number} — {title_line}", 1)]
    if edition:
        out.append(("paragraph", None, [], edition, 1))
    if published:
        out.append(("paragraph", None, [], f"פורסם ביום {published}.", 1))
    if effective:
        out.append(("paragraph", None, [], f"תחילתו ביום {effective}.", 1))
    if issuer:
        out.append(("paragraph", None, [], "הוצא על ידי הוועדה לתקינה.", 2))
    out.append(("paragraph", None, [], "גוף סינתטי של התקן.", 2))
    return out


def standard(office, blocks=None, kind="standard", title="פרק", group=None, sha=None):
    blocks = rows() if blocks is None else blocks
    return professional_file(office, group or office.default_group_id, blocks, title=title, source_kind=kind, sha=sha)


def sparse_rows():
    """A standard whose issuer and edition the patterns do not recognise."""
    return [("paragraph", None, [], MARK, 1), ("heading", None, [], "תקן מס' 98.1", 1),
            ("paragraph", None, [], "המסמך הוצא מטעם גוף הסמכה סינתטי ברמת גן.", 1),
            ("paragraph", None, [], "המסמך נכנס לפעולה בעשרים לאוקטובר 2025.", 1)]


def enable_cloud(office, monkeypatch):
    with tenant_tx(office.ctx()) as conn:
        conn.execute(text("UPDATE office_settings SET cloud_llm_enabled = true"))
    monkeypatch.setattr("app.providers.llm.selected_provider_configured", lambda: True)


def scripted(*claims):
    return ScriptedProvider().on(Purpose.EXTRACT, {"claims": list(claims)})


def claim(field, value, quote, block, relation=""):
    return {"field": field, "value": value, "quote": quote, "block": block, "relation": relation}


def profile_rows(office, version):
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT * FROM standard_profiles WHERE version_id = :v"), {"v": UUID(version)}).all()


def fields_of(office, version) -> dict:
    (row,) = profile_rows(office, version)
    return row.fields


# --- the stored profile -------------------------------------------------------------------------------------------

def test_a_standard_stating_its_data_gets_each_field_stored_with_page_block_and_quote(office):
    doc, ver = standard(office, rows(published="05.10.2025", effective="01.01.2026"))
    got = P.ensure_profile(office.system, ver)
    assert got is not None and got.version_id == UUID(ver)
    (row,) = profile_rows(office, ver)
    assert (str(row.document_id), row.reading_id, row.profile_version) == (doc, "reading-professional-1",
                                                                              P.PROFILE_VERSION)
    f = row.fields
    assert f["number"]["value"] == "98.1" and f["number"]["method"] == "pattern"
    assert f["title"]["value"] == "מדידת שטחים סינתטית" and f["edition"]["value"] == "גרסה 2.0"
    assert f["published"]["normalized"] == "2025-10-05" and f["effective"]["normalized"] == "2026-01-01"
    assert f["issuer"]["value"] == "הוועדה לתקינה" and f["issuer"]["source"]["page"] == 2
    assert f["scope"]["status"] == "unknown"
    with tenant_tx(office.system) as conn:
        blocks = {r.block_index: r.text for r in conn.execute(
            text("SELECT block_index, text FROM document_blocks WHERE version_id = :v"), {"v": UUID(ver)})}
    for name in P.FIELDS:
        field = f[name]
        if field["status"] == "stated":  # every stated field: a quote, in the block it cites, holding the value
            assert field["source"]["quote"] in blocks[field["source"]["block"]]
            assert field["value"] in field["source"]["quote"]
        else:
            assert field == {"value": None, "status": "unknown", "method": None, "source": None}
    assert f["model_pass"]["status"] == "off"  # the office has not allowed cloud use


def test_a_standard_with_no_issuer_line_has_an_unknown_issuer(office):
    _, ver = standard(office, rows(issuer=False))
    P.ensure_profile(office.system, ver)
    assert fields_of(office, ver)["issuer"]["status"] == "unknown"


def test_the_title_and_the_file_name_never_set_a_field(office):
    blocks = [("paragraph", None, [], MARK, 1), ("heading", None, [], "פרק כללי סינתטי", 1),
              ("paragraph", None, [], "גוף סינתטי בלי נתוני תקן.", 1)]
    _, ver = standard(office, blocks, title="תקן 98.1 מעודכן")
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET filename = 'תקן 98.1 מעודכן.pdf', created_at ="
                          " '2031-01-01' WHERE id = :v"), {"v": UUID(ver)})
    P.ensure_profile(office.system, ver)
    f = fields_of(office, ver)
    assert all(f[name]["status"] == "unknown" for name in (*P.FIELDS, "amends"))


def test_a_second_ensure_writes_nothing_and_calls_nothing(office, monkeypatch):
    _, ver = standard(office)
    P.ensure_profile(office.system, ver)
    before = [tuple(r) for r in profile_rows(office, ver)]
    xmin = tenant_xmin(office, ver)

    def boom(*a, **k):
        raise AssertionError("an unchanged reading must not be read again")

    monkeypatch.setattr(P, "from_patterns", boom)
    monkeypatch.setattr(P, "run_model", boom)
    again = P.ensure_profile(office.system, ver)
    assert again is not None and [tuple(r) for r in profile_rows(office, ver)] == before
    assert tenant_xmin(office, ver) == xmin


def tenant_xmin(office, version):
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT xmin::text FROM standard_profiles WHERE version_id = :v"),
                            {"v": UUID(version)}).scalar_one()


def test_an_appraisal_gets_no_profile(office):
    _, ver = standard(office, kind="appraisal")
    assert P.ensure_profile(office.system, ver) is None
    assert profile_rows(office, ver) == []


def test_a_new_reading_derives_a_new_profile_and_replaces_the_old_one(office):
    _, ver = standard(office)
    P.ensure_profile(office.system, ver)
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET ingestion = ingestion || '{\"reading_id\": \"reading-2\"}'"
                          " WHERE id = :v"), {"v": UUID(ver)})
        conn.execute(text("UPDATE document_blocks SET text = :t WHERE version_id = :v AND block_index = 1"),
                     {"t": "תקן מס' 97.0 — מדידה", "v": UUID(ver)})
    P.ensure_profile(office.system, ver)
    (row,) = profile_rows(office, ver)
    assert row.reading_id == "reading-2" and row.fields["number"]["value"] == "97.0"


def test_a_user_outside_the_documents_group_gets_no_profile(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    outsider = make_user(a, "outsider@example.test", [make_group(a, "קבוצה אחרת")])
    _, ver = standard(a)
    assert P.ensure_profile(a.ctx("employee", outsider), ver) is None
    assert profile_rows(a, ver) == []


# --- the model pass -----------------------------------------------------------------------------------------------

def test_with_cloud_disabled_there_is_no_model_call_and_only_pattern_fields_are_set(office):
    _, ver = standard(office, sparse_rows())
    provider = scripted(claim("issuer", "גוף הסמכה סינתטי", "הוצא מטעם גוף הסמכה סינתטי", 2))
    P.ensure_profile(office.system, ver, provider=provider)
    assert provider.calls == []
    f = fields_of(office, ver)
    assert f["number"]["method"] == "pattern" and f["issuer"]["status"] == "unknown"
    assert f["model_pass"]["status"] == "off"


def test_a_model_value_is_kept_only_through_the_quote_check(office, monkeypatch):
    enable_cloud(office, monkeypatch)
    doc, ver = standard(office, sparse_rows(), title="תקן 98.1 מעודכן")
    provider = scripted(
        claim("issuer", "גוף הסמכה סינתטי", "הוצא מטעם גוף הסמכה סינתטי", 2),            # accepted
        claim("scope", "מבנים", "המסמך חל על מבנים", 3),                                  # quote not in block 3
        claim("edition", "גרסה 5", "המסמך נכנס לפעולה", 3),                               # value not in its quote
        claim("effective", "1.1.2030", "המסמך נכנס לפעולה בעשרים לאוקטובר 2025", 3))     # value not in its quote
    got = P.ensure_profile(office.system, ver, provider=provider)
    f = got.fields
    assert len(provider.calls) == 1 and provider.calls[0].purpose == Purpose.EXTRACT
    assert f["issuer"] == {"value": "גוף הסמכה סינתטי", "status": "stated", "method": "model",
                           "source": {"page": 1, "block": 2, "quote": "הוצא מטעם גוף הסמכה סינתטי"}}
    assert f["scope"]["status"] == f["edition"]["status"] == f["effective"]["status"] == "unknown"
    assert f["number"]["method"] == "pattern"
    assert f["model_pass"]["status"] == "ok" and f["model_pass"]["claims"] == 4 and f["model_pass"]["accepted"] == 1
    assert sorted(r["reason"] for r in f["model_pass"]["rejected"]) == [
        "quote_not_in_block", "value_not_in_quote", "value_not_in_quote"]
    assert fields_of(office, ver) == f  # what is stored is what was returned
    # the model saw the blocks and nothing about the file: neither its title nor its name
    assert "98.1 מעודכן" not in provider.calls[0].input and "f.pdf" not in provider.calls[0].input
    with tenant_tx(office.system) as conn:  # the call is billed to the office like any other
        used = conn.execute(text("SELECT purpose, ok FROM provider_usage")).all()
    assert [(u.purpose, u.ok) for u in used] == [("extract", True)]


def test_cloud_enabled_later_runs_the_model_pass_once(office, monkeypatch):
    _, ver = standard(office, sparse_rows())
    P.ensure_profile(office.system, ver, provider=scripted())
    assert fields_of(office, ver)["model_pass"]["status"] == "off"
    enable_cloud(office, monkeypatch)
    provider = scripted(claim("issuer", "גוף הסמכה סינתטי", "הוצא מטעם גוף הסמכה סינתטי", 2))
    P.ensure_profile(office.system, ver, provider=provider)
    f = fields_of(office, ver)
    assert len(provider.calls) == 1 and f["issuer"]["method"] == "model" and f["model_pass"]["status"] == "ok"
    P.ensure_profile(office.system, ver, provider=provider)
    assert len(provider.calls) == 1  # done: not asked again


def test_a_failed_model_call_keeps_the_pattern_fields_and_is_not_asked_again_at_once(office, monkeypatch):
    enable_cloud(office, monkeypatch)
    _, ver = standard(office, sparse_rows())
    provider = ScriptedProvider().on(Purpose.EXTRACT, CallStatus.TIMEOUT, repeat=True)
    got = P.ensure_profile(office.system, ver, provider=provider)
    assert got.fields["number"]["value"] == "98.1" and got.fields["issuer"]["status"] == "unknown"
    assert got.fields["model_pass"]["status"] == "failed" and got.fields["model_pass"]["detail"] == "timeout"
    P.ensure_profile(office.system, ver, provider=provider)
    assert len(provider.calls) == 1  # inside the retry window
    with tenant_tx(office.system) as conn:  # after the window it asks again
        conn.execute(text("UPDATE standard_profiles SET fields = jsonb_set(fields, '{model_pass,at}',"
                          " to_jsonb('2020-01-01T00:00:00+00:00'::text)) WHERE version_id = :v"), {"v": UUID(ver)})
    P.ensure_profile(office.system, ver, provider=provider)
    assert len(provider.calls) == 2


def test_the_default_provider_is_the_selected_one(office, monkeypatch):
    enable_cloud(office, monkeypatch)
    _, ver = standard(office, sparse_rows())
    provider = scripted(claim("issuer", "גוף הסמכה סינתטי", "הוצא מטעם גוף הסמכה סינתטי", 2))
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: provider)
    P.ensure_profile(office.system, ver)
    assert len(provider.calls) == 1


# --- the derivation after ingestion -----------------------------------------------------------------------------------

def test_after_publish_derives_sections_and_profile_and_records_a_profile_failure(office, monkeypatch):
    _, ver = standard(office)

    def boom(*a, **k):
        raise RuntimeError("synthetic profile failure")

    with monkeypatch.context() as m:
        m.setattr(P, "ensure_profile", boom)
        assert professional.after_publish(office.system, UUID(ver)) is False
    with tenant_tx(office.system) as conn:
        ing = conn.execute(text("SELECT ingestion FROM document_versions WHERE id = :v"), {"v": UUID(ver)}).scalar_one()
    assert ing["professional_error"] == {"stage": "profile", "error": "RuntimeError"}
    assert ing["sections_derived"]["count"] >= 1 and profile_rows(office, ver) == []
    assert professional.after_publish(office.system, UUID(ver)) is True
    assert len(profile_rows(office, ver)) == 1
    with tenant_tx(office.system) as conn:
        ing = conn.execute(text("SELECT ingestion FROM document_versions WHERE id = :v"), {"v": UUID(ver)}).scalar_one()
    assert "professional_error" not in ing


def test_ingesting_a_standard_stores_its_profile_and_an_appraisal_gets_none(db, client, monkeypatch):
    office = make_office(db, "משרד א", "admin-a@example.test")
    login(client, "admin-a@example.test")
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    monkeypatch.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
    out = {}
    for kind in ("standard", "appraisal"):
        data = D1.read_bytes() + f"\n%{kind}\n".encode()
        res = post_kind(client, f"{kind}.pdf", data, office.default_group_id, kind).json()["results"][0]
        while worker.run_one("test-worker"):
            pass
        out[kind] = res["version_id"]
    assert len(profile_rows(office, out["standard"])) == 1
    assert profile_rows(office, out["appraisal"]) == []
    assert fields_of(office, out["standard"])["model_pass"]["status"] == "off"


# --- editions -----------------------------------------------------------------------------------------------------

def editions(office, number):
    with tenant_tx(office.ctx()) as conn:
        return P.editions(conn, number)


def profiled(office, blocks, title, **kw):
    doc, ver = standard(office, blocks, title=title, **kw)
    P.ensure_profile(office.system, ver)
    return doc, ver


def test_two_editions_with_stated_effective_dates_are_ordered_by_date_not_by_upload(office):
    newer, _ = profiled(office, rows(effective="01.06.2025", edition="גרסה 2.0"), "מהדורה חדשה")   # uploaded first
    older, _ = profiled(office, rows(effective="01.01.2024", edition="גרסה 1.0"), "מהדורה ישנה")   # uploaded second
    group = editions(office, "98.1")
    assert group.number == "98.1" and group.order_known is True
    assert [str(e.document_id) for e in group.editions] == [older, newer]  # the oldest first
    assert [e.edition for e in group.editions] == ["גרסה 1.0", "גרסה 2.0"]
    assert [e.effective for e in group.editions] == ["2024-01-01", "2025-06-01"]


def test_publication_date_orders_when_the_effective_dates_are_not_stated(office):
    a, _ = profiled(office, rows(published="01.03.2025", effective=None), "ב")
    b, _ = profiled(office, rows(published="01.03.2023", effective=None), "א")
    group = editions(office, "98.1")
    assert group.order_known and [str(e.document_id) for e in group.editions] == [b, a]


def test_two_editions_with_no_dates_are_reported_as_order_unknown(office):
    profiled(office, rows(edition="גרסה 1.0"), "ראשונה")
    profiled(office, rows(edition="גרסה 2.0"), "שנייה")
    group = editions(office, "98.1")
    assert len(group.editions) == 2 and group.order_known is False  # nothing orders them, upload date least of all


def test_one_edition_with_a_date_and_one_without_have_no_known_order(office):
    profiled(office, rows(effective="01.01.2024"), "עם תאריך")
    profiled(office, rows(effective=None, edition="גרסה 2.0"), "בלי תאריך")
    assert editions(office, "98.1").order_known is False


def test_a_single_edition_is_trivially_ordered(office):
    profiled(office, rows(), "יחידה")
    group = editions(office, "98.1")
    assert len(group.editions) == 1 and group.order_known is True


def test_fifteen_fifteen_point_zero_and_fifteen_point_zero_zero_are_one_group_and_a_letter_is_kept(office):
    ids = [profiled(office, rows(number=n), f"תקן {n}")[0] for n in ("15", "15.0", "15.00")]
    lettered, _ = profiled(office, rows(number="11.0 א'"), "עם אות")
    plain, _ = profiled(office, rows(number="11.0"), "בלי אות")
    for asked in ("15", "15.0", "15.00"):
        assert {str(e.document_id) for e in editions(office, asked).editions} == set(ids)
    assert [str(e.document_id) for e in editions(office, "11.0 א'").editions] == [lettered]
    assert [str(e.document_id) for e in editions(office, "11").editions] == [plain]
    assert editions(office, "11.0 א'").number == "11 א"


def test_only_visible_current_professional_documents_are_grouped(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    other = make_group(a, "קבוצה אחרת")
    outsider = make_user(a, "outsider@example.test", [other])
    member = make_user(a, "member@example.test", [a.default_group_id])
    visible, _ = profiled(a, rows(), "גלוי")
    hidden, _ = profiled(a, rows(edition="גרסה 3.0"), "נסתר", group=other)
    gone, gone_ver = profiled(a, rows(edition="גרסה 4.0"), "נמחק")
    superseded, sup_ver = profiled(a, rows(edition="גרסה 5.0"), "הוחלף")
    appraisal, apr_ver = profiled(a, rows(edition="גרסה 6.0"), "שומה", kind="appraisal")
    with tenant_tx(a.ctx()) as conn:
        conn.execute(text("UPDATE documents SET deleted_at = now() WHERE id = :d"), {"d": UUID(gone)})
        conn.execute(text("UPDATE document_versions SET is_current = false, status = 'superseded' WHERE id = :v"),
                     {"v": UUID(sup_ver)})
    with tenant_tx(a.ctx("employee", member)) as conn:
        assert [str(e.document_id) for e in P.editions(conn, "98.1").editions] == [visible]
    with tenant_tx(a.ctx("employee", outsider)) as conn:
        assert [str(e.document_id) for e in P.editions(conn, "98.1").editions] == [hidden]
    with tenant_tx(a.ctx()) as conn:
        assert {str(e.document_id) for e in P.editions(conn, "98.1").editions} == {visible, hidden}
    assert appraisal not in {str(e.document_id) for e in editions(a, "98.1").editions}


def test_a_clarification_is_linked_to_the_visible_standard_it_names(office):
    std, std_ver = profiled(office, rows(), "התקן")
    clar, _ = profiled(office, [("paragraph", None, [], MARK, 1),
                                ("heading", None, [], "הבהרה לתקן 98.1", 1),
                                ("paragraph", None, [], "גוף ההבהרה הסינתטית.", 1)], "ההבהרה", kind="professional")
    group = editions(office, "98.1")
    assert [str(e.document_id) for e in group.editions] == [std]  # the clarification is no edition of its own
    (related,) = group.related
    assert str(related.document_id) == clar
    assert related.amends["relation"] == "clarifies" and related.amends["standard"] == "98.1"
    assert str(related.amends["link"]["document_id"]) == std and related.amends["ambiguous"] is False
    assert related.number is None


def test_a_clarification_of_a_standard_that_is_not_uploaded_stays_unlinked_with_the_stated_number(office):
    clar, _ = profiled(office, [("paragraph", None, [], MARK, 1), ("heading", None, [], "הבהרה לתקן 98.1", 1)],
                       "ההבהרה", kind="professional")
    group = editions(office, "98.1")
    assert group.editions == () and group.order_known is True
    (related,) = group.related
    assert str(related.document_id) == clar
    assert related.amends["link"] is None and related.amends["standard"] == "98.1"
    assert related.amends["candidates"] == () and related.amends["ambiguous"] is False


def test_a_clarification_naming_a_number_with_two_editions_is_not_linked_to_either(office):
    a, _ = profiled(office, rows(effective="01.01.2024"), "ראשונה")
    b, _ = profiled(office, rows(effective="01.01.2025", edition="גרסה 3.0"), "שנייה")
    profiled(office, [("paragraph", None, [], MARK, 1), ("heading", None, [], "תיקון לתקן 98.1", 1)], "התיקון",
             kind="professional")
    (related,) = editions(office, "98.1").related
    assert related.amends["link"] is None and related.amends["ambiguous"] is True
    assert {str(c["document_id"]) for c in related.amends["candidates"]} == {a, b}
    assert related.amends["relation"] == "amends"


def test_a_standard_that_replaces_another_is_grouped_by_its_own_number_and_linked_to_the_old_one(office):
    old, _ = profiled(office, rows(number="97.0"), "הישן")
    blocks = rows(number="98.0") + [("paragraph", None, [], "תקן זה מחליף את תקן 97.0.", 2)]
    new, _ = profiled(office, blocks, "החדש")
    assert [str(e.document_id) for e in editions(office, "98.0").editions] == [new]
    (related,) = editions(office, "97.0").related
    assert str(related.document_id) == new and related.amends["relation"] == "replaces"
    assert str(related.amends["link"]["document_id"]) == old


def test_versions_not_profiled_yet_are_counted_and_ensure_all_profiles_them(office):
    standard(office, rows(), title="לא נותח")
    group = editions(office, "98.1")
    assert group.editions == () and group.unprofiled == 1
    assert P.ensure_all(office.system) == 1
    group = editions(office, "98.1")
    assert len(group.editions) == 1 and group.unprofiled == 0
    assert P.ensure_all(office.system) == 0


def test_asking_for_a_text_that_is_no_number_gives_an_empty_group(office):
    profiled(office, rows(), "תקן")
    group = editions(office, "לא מספר")
    assert group.number is None and group.editions == () and group.related == ()
