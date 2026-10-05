"""Gate 2 (origin §11): every source opens the right document and page, and the evidence supports
the claim — a link alone is not enough.

For numeric answers each source must (1) stream the original file (``application/pdf``, or the DOCX
for D11), (2) point at the physical page where the answer key puts that row, (3) be a record that
satisfies the answer's conditions (or a certain duplicate of one), and (4) show the record's price
on that page — both in the stored page text and in the text of the served PDF page itself.
Content answers must cite the page that holds the phrase asked about.
"""

from __future__ import annotations

import re

import pytest

from eval.truth import docs, matches, price_token, records_by_id, truth, txn_key
from tests.acceptance.conftest import heb_ocr_available
from tests.acceptance.support import (
    ADMIN_A,
    ADMIN_B,
    NUMERIC_CASES,
    pdf_page_text,
    run_case,
    source_record,
    squash,
    stored_page_text,
    stored_table_cells,
)

pytestmark = pytest.mark.db
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _office(user: str) -> str:
    return "B" if user == ADMIN_B else "A"


@pytest.mark.parametrize("case", NUMERIC_CASES, ids=lambda c: c.id)
def test_numeric_sources_open_the_right_page_and_contain_the_price(world, case):
    client = world.client(case.user)
    answer = run_case(world, case, client).answer
    assert answer["kind"] == "numeric" and answer["sources"]
    matching_txns = set()
    for s in answer["sources"]:
        rec = source_record(world, s, case.filters.data_kind)
        assert rec is not None, f"source does not map to an answer-key record: {s}"
        full = records_by_id()[rec["id"]]
        if matches(full, case.filters):
            matching_txns.add(txn_key(full))
    for s in answer["sources"]:
        rec = records_by_id()[source_record(world, s, case.filters.data_kind)["id"]]
        # (3) the cited record supports the claim: it matches the conditions or is the same transaction
        assert txn_key(rec) in matching_txns, (rec["id"], case.id)
        # (1) the link opens the original through the authenticated endpoint
        base, _, fragment = s["url"].partition("#")
        r = client.get(base)
        assert r.status_code == 200, (s["url"], r.status_code)
        is_docx = docs()[rec["_doc"]]["kind"] == "docx"
        assert r.headers["content-type"].startswith(DOCX if is_docx else "application/pdf")
        token = price_token(rec["id"])
        if is_docx:
            # DOCX has no physical pages (page claims are covered by the xfail test below);
            # the cited row, or the header text for the report's own value, must hold the price
            if s["row"] is not None:
                assert any(token in c for c in stored_table_cells(world, _office(case.user), s["version_id"], s["row"]))
            else:
                assert token in squash(stored_page_text(world, _office(case.user), s["version_id"], 1))
            continue
        # (2) the page is the physical page of that row in the answer key
        page = rec["page"]
        assert s["page_list"] == [page] and fragment == f"page={page}", (rec["id"], s["page_list"], fragment)
        # (4) the evidence on that page contains the record's price
        assert token in squash(stored_page_text(world, _office(case.user), s["version_id"], page)), (rec["id"], token)
        if docs()[rec["_doc"]]["kind"] != "pdf_scanned":
            assert token in squash(pdf_page_text(r.content, page)), (rec["id"], token, "served PDF page")
        if s["row"] is not None:
            cells = stored_table_cells(world, _office(case.user), s["version_id"], s["row"])
            assert any(token in c for c in cells), (rec["id"], cells)


# the D5 price phrase is retrieved non-deterministically (see gate 7's lexical-ranking xfail)
CONTENT_FACTS = [f for f in truth()["content_facts"] if docs()[f["document"]]["office"] == "A"
                 and "1.25" not in f["phrase"]]


@pytest.mark.parametrize("fact", CONTENT_FACTS, ids=lambda f: f"{f['document']}:{f['phrase'][:18]}")
def test_content_sources_cite_the_page_holding_the_phrase(world, fact):
    if fact["document"] == "D2" and not heb_ocr_available():
        pytest.skip("D2 is a scan: needs tesseract with Hebrew data (run in image)")
    client = world.client(ADMIN_A)
    r = client.post("/api/ask", json={"question": f"מה נכתב במסמכים על {fact['phrase']}?"})
    answer = r.json()["answer"]
    assert answer["kind"] in ("content", "combined"), answer["kind"]
    target = world.docs[fact["document"]]
    hits = [s for s in answer["sources"] if s["document_id"] == target["document_id"]
            and (fact["page"] is None or fact["page"] in s["page_list"])]
    assert hits, f"expected {fact['document']} p.{fact['page']} among {[(s['title'], s['page_list']) for s in answer['sources']]}"
    for s in answer["sources"]:
        base = s["url"].partition("#")[0]
        assert client.get(base).status_code == 200
    if fact["page"] is not None:
        page_text = squash(stored_page_text(world, "A", hits[0]["version_id"], fact["page"]))
        if docs()[fact["document"]]["kind"] == "pdf_scanned":
            # OCR text: punctuation such as gershayim may differ; most of the phrase's words must be there
            words = [w for w in re.split(r"[\s״׳\"'*]+", fact["phrase"]) if w]
            found = [w for w in words if w in page_text]
            assert len(found) >= 0.75 * len(words), (words, page_text[:300])
        else:
            assert squash(fact["phrase"]) in page_text


@pytest.mark.xfail(strict=True, reason=(
    "BUG: a DOCX header record (D11 appraised value) is cited as page 1 with '#page=1' although DOCX has no "
    "physical pages (plan assumption: DOCX sources cite heading/paragraph). app/answering/service.py:_source_json "
    "uses occurrences.page_no regardless of the version's mime type."))
def test_docx_sources_do_not_claim_pdf_pages(world):
    from tests.acceptance.support import CASES_BY_ID

    case = CASES_BY_ID["value-by-valuation-date"]
    answer = run_case(world, case).answer
    docx_sources = [s for s in answer["sources"] if s["document_id"] == world.doc_id("D11")]
    assert docx_sources, "D11 should contribute its appraised value"
    for s in docx_sources:
        assert s["page_list"] == [] and "#page=" not in s["url"], s
