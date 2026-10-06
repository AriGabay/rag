"""Consistency checks between the committed synthetic fixtures and ground_truth.yaml (U17).

Runs on the host without Docker: it only reads the committed files.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from pypdf import PdfReader

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
TRUTH = yaml.safe_load((FIXTURES / "ground_truth.yaml").read_text(encoding="utf-8"))
DOCS = {d["id"]: d for d in TRUTH["documents"]}
RECORDS = [(d, r) for d in TRUTH["documents"] for r in d["records"]]
MARKER = "מסמך סינתטי לדמו"
TABLE_HEADERS = [
    "כתובת",
    "גוש/חלקה",
    "תאריך עסקה",
    "סוג נכס",
    "חדרים",
    "שטח (מ״ר)",
    "סוג שטח",
    "מחיר (₪)",
    "מחיר למ״ר (₪)",
]


@pytest.mark.parametrize("doc", TRUTH["documents"], ids=lambda d: d["id"])
def test_every_ground_truth_file_exists_and_is_marked_synthetic(doc):
    assert "synthetic" in doc["filename"]
    assert (FIXTURES / doc["filename"]).is_file()


def test_no_untracked_fixture_documents():
    listed = {d["filename"] for d in TRUTH["documents"]}
    on_disk = {p.name for p in FIXTURES.iterdir() if p.suffix in {".pdf", ".docx"}}
    assert on_disk == listed


@pytest.mark.parametrize(
    "doc",
    [d for d in TRUTH["documents"] if d["kind"] in {"pdf_digital", "pdf_visual", "pdf_scanned"}],
    ids=lambda d: d["id"],
)
def test_pdf_opens_and_page_count_matches(doc):
    reader = PdfReader(FIXTURES / doc["filename"])
    assert not reader.is_encrypted
    assert len(reader.pages) == doc["page_count"]
    for section in doc["sections"]:
        assert 1 <= section["page"] <= doc["page_count"]
    if doc["kind"] != "pdf_scanned":
        text = "".join(page.extract_text() for page in reader.pages)
        assert text.strip(), "digital PDFs must have a text layer"


def test_scanned_pdf_has_no_text_layer():
    reader = PdfReader(FIXTURES / DOCS["D2"]["filename"])
    assert all(not (page.extract_text() or "").strip() for page in reader.pages)


def test_encrypted_pdf_reports_encrypted_and_opens_with_password():
    doc = DOCS["BAD_encrypted"]
    reader = PdfReader(FIXTURES / doc["filename"])
    assert reader.is_encrypted
    assert reader.decrypt(doc["password"])
    assert len(reader.pages) == doc["page_count"]
    assert doc["records"] == []


def test_truncated_pdf_fails_to_open():
    doc = DOCS["BAD_truncated"]
    with pytest.raises(Exception):  # noqa: B017 - any parse error is the expected outcome
        reader = PdfReader(FIXTURES / doc["filename"])
        _ = [page.extract_text() for page in reader.pages]
    assert doc["records"] == []


def test_docx_contains_marker_and_table_header():
    import docx

    document = docx.Document(FIXTURES / DOCS["D11"]["filename"])
    assert any(MARKER in p.text for p in document.paragraphs)
    assert [c.text for c in document.tables[0].rows[0].cells] == TABLE_HEADERS
    assert len(document.tables[0].rows) - 1 == len(DOCS["D11"]["tables"][0]["rows"])


def test_record_ids_unique():
    ids = [r["id"] for _, r in RECORDS]
    assert len(ids) == len(set(ids))


def test_office_a_has_enough_transaction_records():
    count = sum(
        1
        for d, r in RECORDS
        if d["office"] == "A" and d["version_of"] is None and r["data_kind"] == "transaction_price"
    )
    assert count >= 32


def test_every_report_has_one_appraised_value_record():
    for doc in TRUTH["documents"]:
        if doc["kind"] in {"encrypted", "truncated"}:
            continue
        kinds = [r["data_kind"] for r in doc["records"]]
        assert kinds.count("appraised_value") == 1, doc["id"]


def test_stated_price_per_sqm_matches_price_over_area_except_d6_conflict():
    conflicts = []
    for _doc, r in RECORDS:
        if r["price_per_sqm_stated"] is None or r["area"] is None:
            continue
        computed = Decimal(r["price"]) / Decimal(r["area"])
        stated = Decimal(r["price_per_sqm_stated"])
        if abs(stated - computed) / computed > Decimal("0.005"):
            conflicts.append(r["id"])
            assert r["price_per_sqm_conflict"] is True
        else:
            assert r["price_per_sqm_conflict"] is False
    assert conflicts == ["D6-T00"]


def test_d9_has_thirty_rows_and_records():
    table = DOCS["D9"]["tables"][0]
    assert len(table["rows"]) == 30
    assert sum(r["data_kind"] == "transaction_price" for r in DOCS["D9"]["records"]) == 30


def test_d3_table_crosses_pages_without_repeated_header():
    table = DOCS["D3"]["tables"][0]
    assert len(table["rows"]) >= 14
    assert table["page_start"] < table["page_end"]
    assert table["header_repeated_on_continuation"] is False
    assert {row["page"] for row in table["rows"]} == {table["page_start"], table["page_end"]}


def test_tables_and_records_are_consistent():
    for doc in TRUTH["documents"]:
        for table in doc["tables"]:
            assert table["headers"] == TABLE_HEADERS
            for row in table["rows"]:
                assert len(row["cells"]) == len(TABLE_HEADERS)
        for r in doc["records"]:
            assert r["currency"] == "ILS"
            assert r["area_type"] in {None, "net", "gross", "registered", "equivalent"}
            if r["data_kind"] == "transaction_price":
                row = doc["tables"][r["table_index"]]["rows"][r["row_index"]]
                assert r["page"] == row["page"]


def test_dedup_expectations():
    certain = [sorted(o["record"] for o in g["occurrences"]) for g in TRUTH["dedup"]["certain"]]
    uncertain = [sorted(o["record"] for o in g["occurrences"]) for g in TRUTH["dedup"]["uncertain"]]
    assert certain == [["D1-T01", "D4-T00"]]
    assert uncertain == [["D1-T00", "D4-T01"]]
    by_id = {r["id"]: r for _, r in RECORDS}
    a, b = by_id["D1-T00"], by_id["D4-T01"]
    assert (a["area"], a["area_type"]) == ("95", "net")
    assert (b["area"], b["area_type"]) == ("110", "gross")


def test_d1v2_changes_exactly_one_price():
    old = {r["row_index"]: r for r in DOCS["D1"]["records"] if r["row_index"] is not None}
    new = {r["row_index"]: r for r in DOCS["D1v2"]["records"] if r["row_index"] is not None}
    changed = [i for i in old if old[i]["price"] != new[i]["price"]]
    assert changed == [3]


def test_content_facts_point_at_known_documents():
    for fact in TRUTH["content_facts"]:
        doc = DOCS[fact["document"]]
        if doc["page_count"] is None:
            assert fact["page"] is None
        else:
            assert 1 <= fact["page"] <= doc["page_count"]


# --- held-out general corpus (U12, KTD16): tests/fixtures/general/ and the `general_facts` section ----------

GENERAL = TRUTH["general_facts"]
GENERAL_DIR = FIXTURES / GENERAL["directory"]
GENERAL_DOCS = {d["id"]: d for d in GENERAL["documents"]}
GENERAL_FACTS = GENERAL["facts"]


def _general_extract(doc_id: str):
    import time

    from app.extraction.default import DefaultExtractor

    doc = GENERAL_DOCS[doc_id]
    mime = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if doc["kind"] == "docx"
        else "application/pdf"
    )
    return DefaultExtractor().extract((GENERAL_DIR / doc["filename"]).read_bytes(), mime, time.monotonic() + 600)


def _squash(text: str) -> str:
    from app.extraction.normalize_text import base_normalize

    return base_normalize(text or "")


def test_general_files_exist_are_synthetic_and_listed():
    listed = {d["filename"] for d in GENERAL["documents"]}
    on_disk = {p.name for p in GENERAL_DIR.iterdir() if p.suffix in {".pdf", ".docx"}}
    assert on_disk == listed
    assert all("synthetic" in name for name in listed)
    assert {d["office"] for d in GENERAL["documents"]} == {"A"}
    assert {d["group"] for d in GENERAL["documents"]} == {GENERAL["group"]["code"]}
    assert not set(GENERAL_DOCS) & set(DOCS), "held-out documents never join the record answer key"


def test_general_section_leaves_existing_answer_key_unchanged():
    current_a = [r for d, r in RECORDS if d["office"] == "A" and d["version_of"] is None]
    assert sum(r["data_kind"] == "transaction_price" for r in current_a) == 80
    assert sum(r["data_kind"] == "transaction_price" for d, r in RECORDS if d["office"] == "A") == 86
    assert all(d["records"] == [] for d in GENERAL["documents"])
    phrases = {f["phrase"] for f in TRUTH["content_facts"]}
    general_text = " ".join(f["quote"] for f in GENERAL_FACTS)
    assert not [p for p in phrases if p in general_text]


@pytest.mark.parametrize("doc", GENERAL["documents"], ids=lambda d: d["id"])
def test_general_document_yields_no_records_and_states_its_facts(doc):
    """The rules extractor finds no appraised value and no comparables table (so no occurrences, no
    gazetteer or metadata change), and every fact's quote is on its stated physical page."""
    from app.appraisal.extract import extract_records

    result = _general_extract(doc["id"])
    assert result.page_count == doc["page_count"]
    pages = [(p.page_no, p.text) for p in result.pages]
    assert MARKER in _squash(" ".join(t for _, t in pages))
    tables = [{"index": t.index, "headers": t.headers, "rows": [{"page": r.page, "cells": r.cells} for r in t.rows],
               "ocr": t.ocr} for t in result.tables]
    assert extract_records(pages, tables, set()) == []
    by_page = {n: _squash(t) for n, t in pages}
    every = _squash(" ".join(t for _, t in pages))
    for fact in [f for f in GENERAL_FACTS if f["document"] == doc["id"]]:
        if fact["source"]["kind"] == "text":
            text = every if fact["page"] is None else by_page[fact["page"]]
            assert _squash(fact["quote"]) in text, fact
        else:
            want = doc["tables"][fact["source"]["table_index"]]
            heads = [_squash(h) for h in want["headers"]]
            got = [(t, 0) for t in result.tables if [_squash(h) for h in t.headers] == heads]
            # a two-column key/value table may come back headerless, its header row read as row 0
            got += [(t, 1) for t in result.tables if not t.headers and t.rows
                    and [_squash(c) for c in t.rows[0].cells] == heads]
            assert len(got) == 1, (fact, [t.headers for t in result.tables])
            table, offset = got[0]
            row = table.rows[fact["source"]["row_index"] + offset]
            assert row.page == fact["page"]
            assert _squash(fact["quote"]) == _squash(row.cells[want["headers"].index(fact["source"]["column"])])


def test_general_version_pair_differs_only_in_the_changed_assumptions():
    version = GENERAL["versions"][0]
    assert GENERAL_DOCS[version["document"]]["version_of"] == version["replaces"]
    assert {c["attribute"] for c in version["changed"]} == {"planning_status", "adjustment_rate"}
    old = {(f["attribute"], str(f["value"])) for f in GENERAL_FACTS if f["document"] == version["replaces"]}
    new = {(f["attribute"], str(f["value"])) for f in GENERAL_FACTS if f["document"] == version["document"]}
    assert {a for a, _ in old ^ new} == {"planning_status", "adjustment_rate"}
    assert version["replaces"] not in GENERAL["current_documents"]


def test_general_conflict_and_same_subject():
    conflict = GENERAL["conflicts"][0]
    a, b = conflict["statements"]
    assert a["value"] != b["value"]
    da, db = GENERAL_DOCS[a["document"]], GENERAL_DOCS[b["document"]]
    assert (da["block"], da["parcel"], da["sub_parcel"]) == (db["block"], db["parcel"], db["sub_parcel"])
    assert da["subject_key"] == db["subject_key"] == conflict["subject_key"]
    assert da["version_of"] is None and db["version_of"] is None  # two appraisals, not two versions


def test_general_not_stated_is_the_complement_of_the_facts():
    for attribute, missing in GENERAL["not_stated"].items():
        stating = {f["document"] for f in GENERAL_FACTS if f["attribute"] == attribute}
        assert set(missing) == set(GENERAL_DOCS) - stating, attribute
    # a datum absent from some documents, for every main attribute of the corpus
    for attribute in ("safe_room_area", "balcony_area", "parking_spaces", "ceiling_height", "elevator_count",
                      "zoning", "renovation", "building_permit"):
        assert GENERAL["not_stated"][attribute], attribute


def test_general_cities_cover_ramat_gan_givatayim_and_tel_aviv():
    assert {d["city"] for d in GENERAL["documents"]} == {"רמת גן", "גבעתיים", "תל אביב-יפו"}
    assert any(d["kind"] == "docx" for d in GENERAL["documents"])


# --- eval/questions_general.yaml: every expected value derives from general_facts -------------------------

QUESTIONS_GENERAL = yaml.safe_load(
    (Path(__file__).resolve().parents[2] / "eval" / "questions_general.yaml").read_text(encoding="utf-8")
)["items"]
GENERAL_FACT_BY_ID = {f["id"]: f for f in GENERAL_FACTS}
TURNS = [(item, turn) for item in QUESTIONS_GENERAL for turn in item["turns"]]


def _number(fact) -> Decimal | None:
    """The fact's numeric value in its canonical unit; a year for dated text values; None for "none"."""
    import re

    value = (fact.get("normalized") or {}).get("value", fact["value"])
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return Decimal(str(value))
    match = re.match(r"^(\d{4})(?::|$)", str(value)) or re.match(r"^(\d+(?:\.\d+)?)$", str(value))
    return Decimal(match.group(1)) if match else None


def _in_scope(doc_id: str, scope) -> bool:
    return scope == "all" or all(GENERAL_DOCS[doc_id][key] == want for key, want in scope.items())


def test_general_question_set_shape():
    ids = [item["id"] for item in QUESTIONS_GENERAL]
    assert len(ids) == len(set(ids)) and len(ids) >= 40
    categories = {item["category"] for item in QUESTIONS_GENERAL}
    assert categories >= {"explicit_text_fact", "table_datum", "locate", "explanation", "comparison", "version_diff",
                          "conflict", "computation", "unextracted_datum", "missing_info", "follow_up",
                          "clarification", "new_topic"}
    asked = {turn["ask"] for _, turn in TURNS}
    for verbatim in ("מה גודל ממ״ד ממוצע ברמת גן?", "ומה לגבי השנה הקודמת?",
                     "עכשיו בנושא אחר: אילו שומות מזכירות היתר בנייה?", "התכוונתי לעסקאות",
                     "אילו הנחות השתנו בין הגרסאות?"):
        assert verbatim in asked, verbatim  # AE1-AE6 (AE2 is AE1 asked with cloud use off)
    assert any(item.get("cloud") == "off" for item in QUESTIONS_GENERAL)
    for _, turn in TURNS:
        expect = turn["expect"]
        assert expect["outcome"] in {"answer", "computation", "abstain", "clarification", "comparison"}
        if expect["outcome"] == "abstain":
            assert expect.get("abstention_kind") or expect.get("abstention_kind_any_of"), turn["ask"]
        if expect["outcome"] == "clarification":
            assert expect.get("clarify_key"), turn["ask"]


@pytest.mark.parametrize("item_turn", TURNS, ids=lambda it: f"{it[0]['id']}:{it[1]['ask'][:24]}")
def test_general_question_expectations_derive_from_general_facts(item_turn):
    item, turn = item_turn
    expect = turn["expect"]
    sources = expect.get("sources") or {}
    for ref in (sources.get("all_of") or []) + (sources.get("also_valid") or []):
        doc = GENERAL_DOCS.get(ref["doc"]) or DOCS[ref["doc"]]
        assert ref["page"] is None if doc["page_count"] is None else 1 <= ref["page"] <= doc["page_count"], ref
    refs = list(expect.get("values") or []) + [{"fact": f} for side in expect.get("sides") or [] for f in side["facts"]]
    for ref in refs:
        fact = GENERAL_FACT_BY_ID[ref["fact"]]
        if "value" in ref:
            assert str(ref["value"]) == str(fact["value"]), ref
        if "page" in ref:
            assert ref["page"] == fact["page"], ref
        cited = {(s["doc"], s["page"]) for s in (sources.get("all_of") or []) + (sources.get("also_valid") or [])}
        if sources and expect["outcome"] != "computation":
            assert (fact["document"], fact["page"]) in cited, (ref, cited)
    for side in expect.get("sides") or []:
        assert all(GENERAL_FACT_BY_ID[f]["document"] == side["doc"] for f in side["facts"]), side
    if "changed" in expect:
        assert set(expect["changed"]) == {c["attribute"] for c in GENERAL["versions"][0]["changed"]}

    result = expect.get("result")
    if not result:
        return
    import operator

    facts = [GENERAL_FACT_BY_ID[f] for f in result["facts"]]
    attribute, scope = result["attribute"], result["scope"]
    assert {f["attribute"] for f in facts} == {attribute}
    current = [d for d in GENERAL["current_documents"] if _in_scope(d, scope)]
    stating = {f["document"] for f in GENERAL_FACTS if f["attribute"] == attribute}
    # exhaustive over the documents in scope (never top-k), never a superseded version
    assert sorted(f["document"] for f in facts) == sorted(d for d in current if d in stating)
    assert result["n"] == len(facts) == expect["coverage"]["values_found"]
    coverage = expect["coverage"]
    missing = {d for d in current if d not in stating}
    assert set(coverage.get("not_stated_includes", [])) | set(coverage.get("stated_absent", [])) == missing
    numbers = [_number(f) for f in facts]
    metric = result["metric"]
    if metric == "mean":
        mean = sum(numbers) / len(numbers)
        assert mean.quantize(Decimal("0.01"), rounding="ROUND_HALF_UP") == Decimal(result["value"])
    elif metric == "min":
        assert min(numbers) == Decimal(result["value"])
    elif metric == "count":
        op, bound = result["where"].replace("year ", "").split()[:2]
        test = {">": operator.gt, "<": operator.lt, ">=": operator.ge}[op]
        assert sum(1 for n in numbers if n is not None and test(n, Decimal(bound))) == int(result["value"])
    else:
        assert metric == "values"
