"""A datum that was not found is said first, at the level the turn actually checked: "not found in the
search", "the document was read in part", or "the section was read and the datum is not there" — the last only
when that section or table was opened this turn. Synthetic names only."""

from __future__ import annotations

from app.chat import coverage
from app.chat.engine import FinalAnswer, Requested
from app.chat.tools import Workspace

DOC, PART = "d-1", "d-2"


def _ws() -> Workspace:
    ws = Workspace(ctx=None)
    ws.touch(DOC, "שומה רחוב הצאלון 7", "retrieved")
    ws.touch(DOC, "שומה רחוב הצאלון 7", "read", opening={"sid": "S3", "scope": "section", "name": "תיאור הנכס"})
    ws.touch(PART, "שומה רחוב הערבה 2", "retrieved", partial=True)
    return ws


def _answer(markdown: str, *requested: Requested, status: str = "answered") -> FinalAnswer:
    return FinalAnswer(status=status, answer_markdown=markdown, claims=[], clarification_question="",
                       missing_info="", referenced_document_ids=[], scope_kind="focused", scope_query="", omitted=[],
                       focus=None, requested=list(requested), parts=[])


def _req(status: str, where: str = "", docs: tuple = (DOC,), label: str = "שטח המגרש") -> Requested:
    return Requested(label=label, document_ids=list(docs), status=status, checked_where=where)


def test_a_checked_section_is_named_and_cited_before_the_near_datum():
    a = coverage.state_absence(_ws(), _answer("השטח הבנוי (נתון אחר) הוא 180 מ\"ר [S3].",
                                              _req("section_checked_absent", "S3")), cited=True)
    assert a.answer_markdown.startswith("**שטח המגרש** לא מופיע בסעיף \"תיאור הנכס\" שנבדק [S3].\n\nהשטח הבנוי")
    assert a.status == "partial"


def test_a_section_claim_without_an_opened_section_falls_to_not_found_in_search():
    (r,) = coverage.validate_requested(_ws(), [_req("section_checked_absent", "S9")])
    assert r["status"] == "not_found_search" and r["claimed"] == "section_checked_absent"


def test_a_section_claim_resting_on_measurements_only_is_downgraded():
    ws = Workspace(ctx=None)
    ws.touch(DOC, "שומה רחוב הצאלון 7", "read")  # find_measurements rows: read, but no section opened
    (r,) = coverage.validate_requested(ws, [_req("section_checked_absent", "S1")])
    assert r["status"] == "not_found_search"


def test_a_partly_read_document_gives_source_partial():
    (r,) = coverage.validate_requested(_ws(), [_req("section_checked_absent", "S9", docs=(PART,))])
    assert r["status"] == "source_partial"
    a = coverage.state_absence(_ws(), _answer("", _req("source_partial", docs=(PART,))), cited=False)
    assert "נקרא חלקית" in a.answer_markdown and "הערבה 2" in a.answer_markdown


def test_source_partial_without_a_partial_document_is_not_found_in_search():
    (r,) = coverage.validate_requested(_ws(), [_req("source_partial")])
    assert r["status"] == "not_found_search"


def test_a_document_the_turn_never_touched_is_dropped():
    (r,) = coverage.validate_requested(_ws(), [_req("not_found_search", docs=("d-other", DOC))])
    assert r["document_ids"] == [DOC]


def test_found_leaves_the_answer_unchanged():
    a = _answer("שטח המגרש הוא 512 מ\"ר [S1].", _req("found"))
    assert coverage.state_absence(_ws(), a, cited=True) == a and coverage.state_absence(_ws(), a, cited=False) == a


def test_each_kind_of_sentence_is_added_at_its_own_point():
    a = _answer("טקסט [S1].", _req("section_checked_absent", "S3"), _req("not_found_search", label="שטח הבנייה"))
    before = coverage.state_absence(_ws(), a, cited=True)
    assert "לא מופיע בסעיף" in before.answer_markdown and "לא נמצא בחיפוש" not in before.answer_markdown
    after = coverage.state_absence(_ws(), before, cited=False)
    assert after.answer_markdown.startswith("**שטח הבנייה** לא נמצא בחיפוש")


# --- exactly one limitation per datum, of the four kinds (R21, R22) ---------------------------------------------

def test_two_absence_claims_for_the_same_label_give_one_sentence():
    a = _answer("השטח הבנוי (נתון אחר) הוא 180 מ\"ר [S3].", _req("not_found_search"),
                _req("not_found_search", label=" **שטח  המגרש** "), _req("source_partial"))
    out = coverage.state_absence(_ws(), a, cited=False).answer_markdown
    assert out.count("שטח המגרש") == 1 and out.startswith("**שטח המגרש** לא נמצא בחיפוש במסמכים שנבדקו.")


def test_a_label_checked_in_its_section_gets_no_second_kind_after_verification():
    a = _answer("טקסט [S1].", _req("section_checked_absent", "S3"), _req("not_found_search"))
    before = coverage.state_absence(_ws(), a, cited=True)
    after = coverage.state_absence(_ws(), before, cited=False)
    assert after.answer_markdown.count("שטח המגרש") == 1 and "לא מופיע בסעיף" in after.answer_markdown


def test_a_checked_section_sentence_removed_by_verification_falls_to_the_next_kind():
    a = _answer("טקסט [S1].", _req("section_checked_absent", "S3"), _req("not_found_search"))
    after = coverage.state_absence(_ws(), a, cited=False)  # the cited sentence is not in the answer
    assert after.answer_markdown.startswith("**שטח המגרש** לא נמצא בחיפוש במסמכים שנבדקו.")


def test_each_status_names_exactly_one_of_the_four_kinds():
    ws = _ws()
    rows = coverage.validate_requested(ws, [_req("not_found_search"), _req("source_partial", docs=(PART,)),
                                            _req("section_checked_absent", "S3")])
    assert [r["kind"] for r in rows] == ["not_found_search", "source_partial", "read_absent"]
    assert set(coverage.ABSENCE_KINDS) == {"not_found_search", "source_partial", "read_absent", "sources_conflict"}


def _value(ws: Workspace, vid: str, doc: str, value: str, subject: str = "מבנה ראשי") -> None:
    from decimal import Decimal

    from app.chat.calc import Value

    ws.values[vid] = Value(vid, Decimal(value.replace(",", "")), value, "S1", doc, "v-1", None, "שומה", "עמ' 2",
                           "שטח בנוי", "area", "sqm", "unknown", "unknown", "", subject, "total", {}, {"quote": value},
                           value)


def test_conflicting_values_in_the_sources_are_stated_as_a_conflict():
    ws = _ws()
    _value(ws, "V1", DOC, "184")
    _value(ws, "V2", DOC, "192")
    (r,) = coverage.validate_requested(ws, [_req("sources_conflict", label="השטח הבנוי")])
    assert r["status"] == "sources_conflict" and r["kind"] == "sources_conflict"
    out = coverage.state_absence(ws, _answer("השטח הבנוי הוא 184 מ\"ר [V1] או 192 מ\"ר [V2].",
                                             _req("sources_conflict", label="השטח הבנוי")), cited=False)
    assert out.answer_markdown.startswith("**השטח הבנוי**: המקורות סותרים") and out.status == "partial"


def test_a_conflict_without_conflicting_values_is_not_stated():
    ws = _ws()
    _value(ws, "V1", DOC, "184")
    _value(ws, "V2", DOC, "192", subject="מחסן")  # another subject: no conflict
    (r,) = coverage.validate_requested(ws, [_req("sources_conflict", label="השטח הבנוי")])
    assert r["status"] == "found"
