"""Identity cards of the units of a multi-appraisal file, through the store, the job queue and the backfill (round 15
U5: KTD5, KTD6; R5, R7; KD5, KD8, KD9).

One model call per unit reads the unit's opening blocks; the server keeps only the verified subject fields. The model
is a scripted provider and the files are synthetic units built from blocks (``tests/support/unit_files.py``): no real
model is called and nothing here is customer data."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from app import worker
from app.chat import cards, contexts
from app.config import get_settings
from app.db import tenant_tx
from app.platform import pipeline
from app.platform.jobs import enqueue_unit_cards
from app.providers.llm import CallStatus, Purpose
from tests.factories import make_group, make_office, make_user
from tests.support.scripted_provider import ScriptedProvider
from tests.support.unit_files import READING, unit_file

pytestmark = pytest.mark.db

UNITS = [
    {"title": "מתחם אלון", "blocks": ["הנדון: שומת מקרקעין למתחם אלון", "לכבוד: חברת דוגמה בע״מ, רחוב הנשיא 9",
                                    "רחוב האלון 5, עיר הדוגמה", "|גוש 4001 חלקה 7", "סוג הנכס: מבנה משרדים"]},
    {"title": "מתחם ברוש", "blocks": ["הנדון: שומת מקרקעין למתחם ברוש", "רחוב הברוש 12, עיר הדוגמה",
                                      "גוש 4002 חלקה 9"]},
    {"title": "עסקאות השוואה", "kind": "comparables", "blocks": ["נספח א: עסקאות השוואה", "|רחוב הצפצפה 3 | 9,000"]},
]


def subject(kind, quote, role="subject"):
    return {"role": role, "field": kind, "quote": quote}


# what the scripted model reads out of each unit's opening (it answers by the unit's own words, as a model would)
ANSWERS = {
    "מתחם אלון": [subject("name", "מתחם אלון"), subject("address", "רחוב האלון 5"),
                  subject("block_parcel", "גוש 4001 חלקה 7"), subject("property_type", "מבנה משרדים"),
                  subject("address", "רחוב הנשיא 9", role="addressee"),
                  subject("name", "חברת דוגמה בע״מ", role="addressee"),
                  subject("name", "מגדל שלא נכתב"), subject("party_name", "חברת דוגמה בע״מ")],
    "מתחם ברוש": [subject("name", "מתחם ברוש"), subject("address", "רחוב הברוש 12"),
                  subject("block_parcel", "גוש 4002 חלקה 9")],
}


def card_model(instructions: str, input: str) -> dict:
    for key, fields in ANSWERS.items():
        if f"שומת מקרקעין ל{key}" in input:
            return {"fields": fields}
    return {"fields": []}


def scripted(model=card_model) -> ScriptedProvider:
    return ScriptedProvider().on(Purpose.IDENTIFY, model, repeat=True)


@pytest.fixture
def office(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    g2 = make_group(a, "קבוצה 2")
    make_user(a, "emp@example.test", [g2])
    a.doc, a.ver = unit_file(a, a.default_group_id, "דוח סינתטי", UNITS)
    return a


def enable_cloud(office, monkeypatch):
    with tenant_tx(office.ctx()) as conn:
        conn.execute(text("UPDATE office_settings SET cloud_llm_enabled = true"))
    monkeypatch.setattr(get_settings(), "openai_api_key", SecretStr("test-openai-key-not-real-0001"))  # no network


def card_rows(office, version=None) -> dict[int, object]:
    """The version's cards by the first block of their unit."""
    with tenant_tx(office.system) as conn:
        rows = conn.execute(text(
            "SELECT block_first, block_last, state, fields, model, error FROM unit_cards WHERE version_id = :v"
            " ORDER BY block_first"), {"v": version or office.ver}).all()
    return {r.block_first: r for r in rows}


def unit_ranges(office) -> list[tuple[int, int]]:
    with tenant_tx(office.system) as conn:
        return [(r.block_first, r.block_last) for r in conn.execute(text(
            "SELECT block_first, block_last FROM document_units WHERE version_id = :v ORDER BY unit_no"),
            {"v": office.ver})]


def stored_fields(row) -> list[tuple[str, str, int]]:
    return [(f["field"], f["quote"], f["block_index"]) for f in row.fields]


# --- what is stored ----------------------------------------------------------------------------------------------------


def test_a_subject_name_address_and_parcel_are_stored_with_their_blocks_and_the_rest_is_not(office):
    provider = scripted()
    run = cards.generate(office.system, office.ver, provider)
    first, second, appendix = (card_rows(office)[a] for a, _ in unit_ranges(office))
    assert first.state == "done" and second.state == "done"
    # block indexes are the file's own: the first unit opens at 0, the second right after its five blocks
    assert stored_fields(first) == [("name", "מתחם אלון", 0), ("address", "רחוב האלון 5", 2),
                                    ("block_parcel", "גוש 4001 חלקה 7", 3), ("property_type", "מבנה משרדים", 4)]
    assert stored_fields(second) == [("name", "מתחם ברוש", 5), ("address", "רחוב הברוש 12", 6),
                                     ("block_parcel", "גוש 4002 חלקה 9", 7)]
    # the dropped: a quote the blocks do not hold, the addressee's name and address, a party name
    stored = " ".join(q for _, q, _ in stored_fields(first))
    assert "מגדל שלא נכתב" not in stored and "חברת דוגמה" not in stored and "הנשיא" not in stored
    assert (run.done, run.called) == (2, 2)
    assert first.model and first.error is None and len(provider.calls) == 2


def test_a_comparables_unit_gets_no_call_and_a_recorded_reason(office):
    provider = scripted()
    cards.generate(office.system, office.ver, provider)
    appendix = card_rows(office)[unit_ranges(office)[2][0]]
    assert appendix.state == "empty" and appendix.fields == [] and appendix.error
    assert all("עסקאות" not in c.input for c in provider.calls)


def test_the_model_sees_the_opening_as_untrusted_data_with_table_blocks(office):
    provider = scripted()
    cards.generate(office.system, office.ver, provider)
    first = next(c for c in provider.calls if "מתחם אלון" in c.input)
    assert first.purpose == Purpose.IDENTIFY and "גוש 4001 חלקה 7" in first.input  # the table block is in
    assert "שומת מקרקעין למתחם ברוש" not in first.input                                # the next unit is not
    assert re.search(r"נתוני מסמך", first.input)


def test_a_model_failure_stores_failed_and_the_unit_keeps_its_structural_title(office):
    provider = ScriptedProvider().on(Purpose.IDENTIFY, CallStatus.TIMEOUT, repeat=True)
    with pytest.raises(cards.CardRunFailed):
        cards.generate(office.system, office.ver, provider)
    rows = card_rows(office)
    first = rows[0]
    assert first.state == "failed" and first.fields == [] and first.error
    with tenant_tx(office.system) as conn:
        cx = contexts.of_version(conn, office.ver, READING)
    assert cx.segment(1).title == "מתחם אלון" and cx.named("מה שווי מתחם אלון") == [1]


def test_a_failed_card_is_called_again_by_the_next_run_and_a_done_one_is_not(office):
    flaky = ScriptedProvider().on(Purpose.IDENTIFY, CallStatus.ERROR).on(Purpose.IDENTIFY, card_model, repeat=True)
    run = cards.generate(office.system, office.ver, flaky)      # the first call fails, the second unit is read
    assert (run.failed, run.done) == (1, 1)
    again = scripted()
    run = cards.generate(office.system, office.ver, again)
    assert (run.called, run.done) == (1, 1) and len(again.calls) == 1
    assert {r.state for r in card_rows(office).values()} == {"done", "empty"}
    third = scripted()
    assert cards.generate(office.system, office.ver, third).called == 0 and third.calls == []


def test_a_version_over_the_ceiling_calls_up_to_it_and_leaves_the_rest_pending(office, monkeypatch):
    monkeypatch.setattr(get_settings(), "unit_cards_max_per_version", 1)
    provider = scripted()
    run = cards.generate(office.system, office.ver, provider)
    states = {k: r.state for k, r in card_rows(office).items()}
    assert len(provider.calls) == 1 and run.pending == 1
    assert sorted(states.values()) == ["done", "empty", "pending"]
    # the unit that waits still has its structural identity
    with tenant_tx(office.system) as conn:
        cx = contexts.of_version(conn, office.ver, READING)
    assert cx.named("מה שווי מתחם ברוש") == [2]
    monkeypatch.setattr(get_settings(), "unit_cards_max_per_version", 60)
    assert cards.generate(office.system, office.ver, scripted()).done == 1
    assert sorted(r.state for r in card_rows(office).values()) == ["done", "done", "empty"]


def test_the_call_is_logged_under_the_identify_purpose(office):
    cards.generate(office.system, office.ver, scripted())
    with tenant_tx(office.system) as conn:
        rows = conn.execute(text("SELECT purpose, ok FROM provider_usage")).all()
    assert [(r.purpose, r.ok) for r in rows] == [("identify", True)] * 2


def test_other_offices_and_users_outside_the_group_do_not_read_the_cards(office, db):
    cards.generate(office.system, office.ver, scripted())
    other = make_office(db, "משרד ב", "admin-b@example.test")
    with tenant_tx(other.system) as conn:
        assert conn.execute(text("SELECT count(*) FROM unit_cards")).scalar_one() == 0
        assert cards.cards_of(conn, office.ver, READING) == {}
    with tenant_tx(office.system) as conn:
        emp = conn.execute(text("SELECT id FROM users WHERE email = 'emp@example.test'")).scalar_one()
    with tenant_tx(office.ctx("employee", emp)) as conn:   # in another group than the document's
        assert conn.execute(text("SELECT count(*) FROM unit_cards")).scalar_one() == 0


# --- the card is the unit's identity in the contexts --------------------------------------------------------------------


def test_a_card_that_completes_after_the_first_read_is_seen_by_the_next_one(office):
    with tenant_tx(office.system) as conn:
        before = contexts.of_version(conn, office.ver, READING)
    assert not before.identity(1).name_spans("רחוב האלון 5") and not before.identity(1).parcels
    cards.generate(office.system, office.ver, scripted())
    with tenant_tx(office.system) as conn:
        after = contexts.of_version(conn, office.ver, READING)
        same = contexts.of_version(conn, office.ver, READING)
    assert after is not before and same is after          # a new card state is a new entry, then it is served again
    assert after.identity(1).parcels == {("4001", "7")} and after.identity(1).identifier_in("הנכס ברחוב האלון 5")
    assert after.named("מה השווי של הנכס ברחוב הברוש 12") == [2]
    assert after.named("הנכס בגוש 4002 חלקה 9") == [2]


def test_the_readers_return_done_subject_fields_by_unit_range(office):
    cards.generate(office.system, office.ver, scripted())
    with tenant_tx(office.system) as conn:
        got = cards.subject_fields(conn, office.ver, READING)
        every = cards.cards_for_units(conn, [office.ver])
    first, second, appendix = unit_ranges(office)
    assert set(got) == {first, second}        # a card that is not done has no subject fields
    assert [f["field"] for f in got[first]] == ["name", "address", "block_parcel", "property_type"]
    version = UUID(str(office.ver))
    assert every[(version, *first)].state == "done" and every[(version, *appendix)].state == "empty"


# --- who enqueues, and when -------------------------------------------------------------------------------------------


def info(office):
    return pipeline.VersionInfo(office.ver, office.doc, "k", "application/pdf", None)


def jobs(office) -> list:
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT kind, idempotency_key, status FROM jobs ORDER BY created_at")).all()


def test_publish_queues_the_cards_of_a_multi_unit_version_in_cloud_mode_once(office, monkeypatch):
    enable_cloud(office, monkeypatch)
    assert pipeline.queue_unit_cards(office.system, info(office)) is True
    assert [(j.kind, j.idempotency_key) for j in jobs(office)] == [("unit_cards", f"cards:{office.ver}:{cards.CARD_VERSION}")]
    assert pipeline.queue_unit_cards(office.system, info(office)) is False   # queued already: one job per version


def test_a_single_unit_file_queues_nothing(office, monkeypatch):
    enable_cloud(office, monkeypatch)
    doc, ver = unit_file(office, office.default_group_id, "דוח יחיד", [UNITS[0]], sha="s" * 64)
    assert pipeline.queue_unit_cards(office.system, pipeline.VersionInfo(ver, doc, "k", "application/pdf", None)) is False
    assert jobs(office) == []


def test_an_office_that_is_not_in_cloud_mode_queues_nothing(office):
    assert pipeline.queue_unit_cards(office.system, info(office)) is False
    assert jobs(office) == []


def test_the_worker_runs_the_job_through_the_provider_abstraction_and_logs_it(office, monkeypatch):
    enable_cloud(office, monkeypatch)
    provider = scripted()
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: provider)
    with tenant_tx(office.system) as conn:
        assert enqueue_unit_cards(conn, office.ver, cards.CARD_VERSION)
    assert worker.run_one("test-worker") is True
    assert [j.status for j in jobs(office)] == ["done"] and len(provider.calls) == 2
    assert {r.state for r in card_rows(office).values()} == {"done", "empty"}


def test_a_job_of_an_office_that_left_cloud_mode_makes_no_call(office, monkeypatch):
    provider = scripted()
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: provider)
    with tenant_tx(office.system) as conn:
        enqueue_unit_cards(conn, office.ver, cards.CARD_VERSION)
    worker.run_one("test-worker")
    assert provider.calls == [] and card_rows(office) == {}


def test_a_job_whose_calls_all_fail_is_tried_again_by_the_queue(office, monkeypatch):
    enable_cloud(office, monkeypatch)
    failing = ScriptedProvider().on(Purpose.IDENTIFY, CallStatus.TIMEOUT, repeat=True)
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: failing)
    with tenant_tx(office.system) as conn:
        enqueue_unit_cards(conn, office.ver, cards.CARD_VERSION)
    worker.run_one("test-worker")
    (job,) = jobs(office)
    assert job.status == "queued" and {r.state for r in card_rows(office).values()} == {"failed", "empty"}


# --- the backfill -------------------------------------------------------------------------------------------------------


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location(
        "derive_unit_cards", Path(__file__).resolve().parents[2] / "scripts" / "derive_unit_cards.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_dry_run_writes_nothing_and_reports_units_skips_and_a_token_estimate(office, script, monkeypatch, capsys):
    enable_cloud(office, monkeypatch)
    doc, ver = unit_file(office, office.default_group_id, "דוח יחיד", [UNITS[0]], sha="s" * 64)
    result = script.backfill_office(office.system, dry_run=True)
    assert result["versions"] == 2 and result["multi_unit_versions"] == 1 and result["single_unit_skipped"] == 1
    assert result["units"] == 3 and result["to_call"] == 2 and result["not_subject_units"] == 1
    assert result["over_ceiling"] == 0 and result["not_cloud_skipped"] == 0 and result["tokens_estimate"] > 0
    assert card_rows(office) == {} and jobs(office) == []
    script.main(["--office", str(office.office_id), "--dry-run"])
    out = capsys.readouterr().out
    assert str(office.office_id) in out and "3 units" in out and "tokens" in out and "single-unit" in out


def test_the_backfill_reports_an_office_outside_cloud_mode_as_skipped_and_calls_nothing(office, script):
    result = script.backfill_office(office.system, provider=scripted())
    assert result["not_cloud_skipped"] == 1 and result["called"] == 0 and card_rows(office) == {}


def test_the_backfill_inserts_the_cards_once_and_a_second_run_adds_nothing(office, script, monkeypatch):
    enable_cloud(office, monkeypatch)
    provider = scripted()
    first = script.backfill_office(office.system, provider=provider)
    assert first["called"] == 2 and first["done"] == 2
    kept = {k: (r.state, r.fields) for k, r in card_rows(office).items()}
    again = script.backfill_office(office.system, provider=provider)
    assert again["called"] == 0 and len(provider.calls) == 2
    assert {k: (r.state, r.fields) for k, r in card_rows(office).items()} == kept
    with tenant_tx(office.system) as conn:   # nothing but cards was written: no document or unit was touched
        assert conn.execute(text("SELECT count(*) FROM unit_cards")).scalar_one() == 3
        assert conn.execute(text("SELECT count(*) FROM document_units")).scalar_one() == 3
        assert conn.execute(text("SELECT count(*) FROM documents")).scalar_one() == 1


# --- through the real ingestion -----------------------------------------------------------------------------------------


def test_a_published_compendium_queues_its_cards_and_the_worker_reads_them(db, client, monkeypatch):
    """A synthetic compendium of three appraisals and an appendix goes through the upload API and the worker in an
    office in cloud mode: publication queues the cards job, the worker reads it through the (scripted) provider."""
    import json

    from tests.conftest import login
    from tests.helpers import upload

    office = make_office(db, "משרד א", "admin-a@example.test")
    login(client, "admin-a@example.test")
    enable_cloud(office, monkeypatch)
    fixtures = Path(__file__).resolve().parents[1] / "fixtures"
    manifest = json.loads((fixtures / "round9" / "manifest.json").read_text(encoding="utf-8"))["documents"]
    path = fixtures / manifest["compendium"]["file"]
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    monkeypatch.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
    read = pipeline._extract
    monkeypatch.setattr(pipeline, "_extract", lambda data, mime, deadline, vision, *a, **k: read(
        data, mime, deadline, None, *a, **k))   # a picture is read by OCR alone here: no vision call

    def first_line(instructions: str, input: str) -> dict:
        body = re.search(r'<block [^>]*>\n(.*?)\n</block>', input, re.S).group(1).splitlines()[0]
        return {"fields": [{"role": "subject", "field": "name", "quote": body[:25]}]}

    provider = scripted(first_line)
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: provider)
    r = upload(client, [(path.name, path.read_bytes())], group_id=office.default_group_id)
    assert r.status_code == 200, r.text
    while worker.run_one("test-worker"):
        pass
    version = r.json()["results"][0]["version_id"]
    assert [(j.kind, j.status) for j in jobs(office) if j.kind == "unit_cards"] == [("unit_cards", "done")]
    states = [c.state for _, c in sorted(cards_by_range(office, version).items())]
    assert states == ["done", "done", "done", "empty"] and len(provider.calls) == 3   # the appendix: no call


def cards_by_range(office, version) -> dict:
    with tenant_tx(office.system) as conn:
        return cards.cards_of(conn, version)
