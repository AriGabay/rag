"""Gate 1 (origin §11): every calculation and filter over the verified fixtures is exactly right,
including deduplication, mean vs weighted, and more records than the retrieval top-k.

Expected values come from ``eval.truth`` (ground_truth.yaml, Decimal, ROUND_HALF_UP), never from the
application. Only records a human verified count: the answer key minus what the review queue still
lists (and minus records that were never extracted, e.g. the scanned D2 on a host without Hebrew OCR).
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.answering.content import EVIDENCE_LIMIT
from app.db import tenant_tx
from app.platform.search import CANDIDATES
from eval.flows import ask_flow
from eval.truth import Filters, expected_stats, records_by_id
from tests.acceptance.support import (
    ADMIN_A,
    CASES_BY_ID,
    HAROZIM,
    NUMERIC_CASES,
    RG,
    TX,
    assert_numeric,
    clarifications_for,
    expected_for,
    run_case,
    source_record,
)

pytestmark = pytest.mark.db


@pytest.mark.parametrize("case", NUMERIC_CASES, ids=lambda c: c.id)
def test_numeric_answer_equals_independent_calculation(world, case):
    flow = run_case(world, case)
    assert flow.error is None, flow.error
    expected = expected_for(world, case)
    assert expected.count > 0, f"case {case.id} has no verified records in the answer key"
    assert_numeric(flow.answer, expected, case.id)
    assert flow.answer["provider"] == "template"
    # clarifications fire exactly when the verified records mix a result-changing attribute
    asked_mixed = [k for k in flow.clarifications if k in ("area_type", "property_type", "vat_basis")]
    assert asked_mixed == clarifications_for(world, case), (case.id, flow.clarifications)


def test_mean_and_weighted_are_distinguished(world):
    case = CASES_BY_ID["harozim-2024-net"]
    n = run_case(world, case).answer["numeric"]
    exp = expected_for(world, case)
    assert exp.mean != exp.weighted  # the case really separates the two definitions
    assert (n["mean_price_per_sqm"], n["weighted_price_per_sqm"]) == (str(exp.mean), str(exp.weighted))
    text_ = run_case(world, case).answer["text"]
    assert "ממוצע" in text_ and "משוקלל" in text_


def test_certain_duplicate_counted_once_with_both_sources(world):
    case = CASES_BY_ID["dedup-2023"]
    a = run_case(world, case).answer
    exp = expected_for(world, case)
    assert a["numeric"]["record_count"] == exp.count
    recs = {source_record(world, s, TX)["id"] for s in a["sources"]}
    assert {"D1v2-T01", "D4-T00"} <= recs, recs  # both occurrences cited ...
    assert len(a["sources"]) == exp.count + 1  # ... for one transaction
    # the employee of G2 sees the same transaction through D4 only, still once
    y = run_case(world, CASES_BY_ID["yossi-g2-2023"]).answer
    assert {source_record(world, s, TX)["id"] for s in y["sources"]} == {"D4-T00"}


def _explore(client, question, path, leaves, depth=0):
    flow = ask_flow(client, question, path)
    a = flow.answer
    if a["kind"] == "clarification" and a["clarification"]["key"] not in path:
        key = a["clarification"]["key"]
        for option in a["clarification"]["options"]:
            _explore(client, question, path | {key: option["value"]}, leaves, depth + 1)
    else:
        leaves.append((path, a))


def test_every_partition_is_exact_and_all_30_d9_rows_are_counted(world):
    """Walk the whole clarification tree of one question: each leaf equals the oracle, the leaves
    add up to every verified 2024 Harozim transaction, and all 30 D9 rows (more than the search
    top-k) are counted."""
    assert CANDIDATES >= EVIDENCE_LIMIT and 30 > EVIDENCE_LIMIT
    base = Filters(TX, "transaction_date", 2024, city=RG, neighborhood=HAROZIM)
    leaves: list = []
    _explore(world.client(ADMIN_A), "מחירי עסקאות למ״ר בחרוזים שנחתמו ב-2024", {}, leaves)
    assert len(leaves) > 4
    total, d9_rows = 0, set()
    for path, answer in leaves:
        f = base.with_(**{k: v for k, v in path.items() if k in ("area_type", "property_type", "vat_basis")})
        exp = expected_stats(f, exclude=world.exclude)
        assert_numeric(answer, exp, str(path))
        total += answer["numeric"]["record_count"]
        for s in answer["sources"]:
            rec = source_record(world, s, TX)
            assert rec is not None, s
            if rec["_doc"] == "D9":
                d9_rows.add(rec["row_index"])
    assert total == expected_stats(base, exclude=world.exclude).count
    assert d9_rows == set(range(30))


def test_unverified_records_are_excluded_and_reported(world):
    assert "D1v2-T03" in world.unverified  # new-version price change, not approved by the seed rule
    f = Filters(TX, "transaction_date", 2023, city=RG, neighborhood=HAROZIM, area_type="registered",
                property_type="apartment", vat_basis="included")
    flow = ask_flow(world.client(ADMIN_A), "מחירי עסקאות למ״ר בחרוזים שנחתמו ב-2023",
                    {"area_type": "registered", "property_type": "apartment", "vat_basis": "included"})
    exp = expected_stats(f, exclude=world.exclude)
    assert_numeric(flow.answer, exp)
    assert "D1v2-T03" not in exp.record_ids
    assert flow.answer["coverage"]["records_awaiting_verification"] == 1
    with_unverified = expected_stats(f, exclude=world.exclude - {"D1v2-T03"})
    assert with_unverified.count == exp.count + 1  # the record would change the answer


def test_new_version_is_not_reported_as_an_uncertain_duplicate(world):
    items = world.client(ADMIN_A).get("/api/review/queue").json()["items"]
    for item in [i for i in items if i["kind"] == "dedup"]:
        docs_a = {s["document_id"] for s in item["a"]["sources"]}
        docs_b = {s["document_id"] for s in item["b"]["sources"]}
        assert not (docs_a == docs_b and len(docs_a) == 1), (item["reason"], item["a"]["address"])


def test_ae3_mean_vs_weighted_after_human_approval(world):
    """AE3: D6 rows 0-1 (1,000,000/50 and 3,000,000/100). Row 0 waits for review because its stated
    price per sqm conflicts; once a human approves it, mean = 25,000.00, weighted = 26,666.67."""
    world.dirty()
    admin = world.client(ADMIN_A)
    rec = records_by_id()["D6-T00"]
    assert rec["price_per_sqm_conflict"] is True
    with tenant_tx(world.system("A")) as conn:
        occ = conn.execute(text(
            "SELECT o.id FROM occurrences o JOIN document_versions v ON v.id = o.version_id AND v.is_current"
            " WHERE v.filename = :f AND o.row_index = 0"), {"f": "D6_synthetic_givatayim_conflicting_ppsm.pdf"}).scalar()
    r = admin.post(f"/api/review/records/{occ}/approve", json={"note": "נבדק מול המסמך"})
    assert r.status_code == 200, r.text
    world.refresh_verification()
    flow = ask_flow(admin, "מחיר למ״ר בעסקאות בגבעתיים שנחתמו ב-2024", {})
    n = flow.answer["numeric"]
    assert (n["record_count"], n["mean_price_per_sqm"], n["weighted_price_per_sqm"]) == (2, "25000.00", "26666.67")
    assert n["conflicts"] == 1
    exp = expected_stats(Filters(TX, "transaction_date", 2024, city="גבעתיים"), exclude=world.exclude)
    assert_numeric(flow.answer, exp)
