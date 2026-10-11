"""The model's absence claims (``requested``) are validated against what the turn did, never written by the model
and never upgraded (round 7 KTD4, R8, R9): "the section was checked" only when that section or table was opened this
turn and read to its end, "read in part" only when a source was, "the sources conflict" only with conflicting values;
otherwise the claim falls to "not found in the search". The server states the gap after the answer, once per datum.
A claim links to the request's component by its id (``component``); an unlinked claim is stated on its own only when
the turn has no requirements at all, so a datum never gets two statements. Synthetic names only."""

from __future__ import annotations

from app.chat import coverage
from app.chat.engine import FinalAnswer, Requested
from app.chat.tools import Workspace
from app.chat.verify import TurnRequirements, VerifyReport

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


def _req(status: str, where: str = "", docs: tuple = (DOC,), label: str = "שטח המגרש", component: str = "") -> Requested:
    return Requested(component=component, label=label, document_ids=list(docs), status=status, checked_where=where)


def _stated(ws: Workspace, a: FinalAnswer, turn: TurnRequirements | None = None) -> FinalAnswer:
    """The answer as the server finishes it, for a turn with no requirements (unless ``turn`` has some)."""
    final, _ = coverage.state_components(ws, a, VerifyReport([], judged=True), turn or TurnRequirements())
    return final


def test_a_checked_section_is_named_and_cited_after_the_near_datum():
    a = _stated(_ws(), _answer("השטח הבנוי (נתון אחר) הוא 180 מ\"ר [S3].", _req("section_checked_absent", "S3")))
    assert a.answer_markdown == ("השטח הבנוי (נתון אחר) הוא 180 מ\"ר [S3].\n\n"
                                 "**שטח המגרש** לא מופיע בסעיף \"תיאור הנכס\" שנבדק [S3].")
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
    a = _stated(_ws(), _answer("", _req("source_partial", docs=(PART,))))
    assert "נקרא חלקית" in a.answer_markdown and "הערבה 2" in a.answer_markdown


def test_source_partial_without_a_partial_document_is_not_found_in_search():
    (r,) = coverage.validate_requested(_ws(), [_req("source_partial")])
    assert r["status"] == "not_found_search"


def test_a_document_the_turn_never_touched_is_dropped():
    (r,) = coverage.validate_requested(_ws(), [_req("not_found_search", docs=("d-other", DOC))])
    assert r["document_ids"] == [DOC]


def test_found_leaves_the_answer_unchanged():
    a = _answer("שטח המגרש הוא 512 מ\"ר [S1].", _req("found"))
    assert _stated(_ws(), a) == a


def test_the_model_writes_no_absence_sentence_and_the_server_adds_none_before_verification():
    # the server's statement is added once, after the answer, when the turn is finished
    a = _answer("טקסט [S1].", _req("section_checked_absent", "S3"), _req("not_found_search", label="שטח הבנייה"))
    out = _stated(_ws(), a).answer_markdown
    assert out.startswith("טקסט [S1].\n\n")
    assert out.split("\n\n", 1)[1].split("\n") == ["**שטח המגרש** לא מופיע בסעיף \"תיאור הנכס\" שנבדק [S3].",
                                                   "**שטח הבנייה** לא נמצא בחיפוש במסמכים שנבדקו."]


# --- exactly one statement per datum (R9, R22) -------------------------------------------------------------------

def test_two_absence_claims_for_the_same_label_give_one_sentence():
    a = _answer("השטח הבנוי (נתון אחר) הוא 180 מ\"ר [S3].", _req("not_found_search"),
                _req("not_found_search", label=" **שטח  המגרש** "), _req("source_partial"))
    out = _stated(_ws(), a).answer_markdown
    assert out.count("שטח המגרש") == 1 and out.endswith("**שטח המגרש** לא נמצא בחיפוש במסמכים שנבדקו.")


def test_a_label_checked_in_its_section_gets_no_second_reason():
    a = _answer("טקסט [S1].", _req("section_checked_absent", "S3"), _req("not_found_search"))
    out = _stated(_ws(), a).answer_markdown
    assert out.count("שטח המגרש") == 1 and "לא מופיע בסעיף" in out and "לא נמצא" not in out


def test_each_status_names_exactly_one_reason_of_the_single_vocabulary():
    ws = _ws()
    rows = coverage.validate_requested(ws, [_req("not_found_search"), _req("source_partial", docs=(PART,)),
                                            _req("section_checked_absent", "S3")])
    assert [r["kind"] for r in rows] == ["not_located", "region_not_read", "not_in_part_read"]
    assert {r["kind"] for r in rows} <= set(coverage.REASONS)


def test_an_unlinked_claim_is_not_stated_beside_the_requests_components():
    ws = _ws()
    turn = TurnRequirements()
    turn.adopt([{"id": "1", "text": "שטח המגרש"}], "analysis")
    from app.chat import verify

    report = VerifyReport([], judged=True)
    report.requirements = [dict(r) for r in turn.items]
    report.requirement_votes = {"N1": [verify.JudgeRequirement(id="N1", status="missing")]}
    final, outcomes = coverage.state_components(ws, _answer("טקסט [S1].", _req("not_found_search")), report, turn)
    # the component is stated (no search covered it); the model's unlinked claim adds no second sentence
    assert final.answer_markdown.count("שטח המגרש") == 1 and "לא נמצא" not in final.answer_markdown
    assert outcomes[0]["limitation"] == "not_located"


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
    out = _stated(ws, _answer("השטח הבנוי הוא 184 מ\"ר [V1] או 192 מ\"ר [V2].",
                              _req("sources_conflict", label="השטח הבנוי")))
    assert "\n\n**השטח הבנוי**: המקורות סותרים" in out.answer_markdown and out.status == "partial"


def test_a_conflict_without_conflicting_values_is_not_stated():
    ws = _ws()
    _value(ws, "V1", DOC, "184")
    _value(ws, "V2", DOC, "192", subject="מחסן")  # another subject: no conflict
    (r,) = coverage.validate_requested(ws, [_req("sources_conflict", label="השטח הבנוי")])
    assert r["status"] == "found"
