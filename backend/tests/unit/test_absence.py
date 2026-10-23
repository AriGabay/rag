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
    return FinalAnswer(status=status, answer_markdown=markdown, items=[], claims=[], clarification_question="",
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


def _value(ws: Workspace, vid: str, doc: str, value: str, subject: str = "מבנה ראשי", facet: str = "") -> None:
    """A value whose kind, unit and area basis the source states (round 13 U4: the conflict check reads the timeline's datum
    classes, whose key counts only a stated field); ``facet``: the row label its source gives it (own tier)."""
    from decimal import Decimal

    from app.chat import datum
    from app.chat.calc import Value

    ws.values[vid] = Value(vid, Decimal(value.replace(",", "")), value, "S1", doc, "v-1", None, "שומה", "עמ' 2",
                           "שטח בנוי", "area", "sqm", "unknown", "unknown", "נטו", subject, "total",
                           {"kind": "source", "unit": "source", "area_basis": "source"}, {"quote": value}, value,
                           section="2. תיאור הנכס",
                           facet=datum.facet(facet, "row", datum.TIER_OWN) if facet else datum.none())


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
    _value(ws, "V1", DOC, "184", facet="שטח בנוי")
    _value(ws, "V2", DOC, "192", subject="מחסן", facet="שטח המחסן")  # another component of the unit (round 13 U4): no conflict
    (r,) = coverage.validate_requested(ws, [_req("sources_conflict", label="השטח הבנוי")])
    assert r["status"] == "found"


# --- round 22 U6 (KTD6, KTD11; R16, R2): one negation and absence reader --------------------------------------------------------------
#
# «Not found», «not stated», «not computed», «not determined» and «cannot be known» were read by six lists that shared no word
# (``verify.NOT_FOUND`` / ``ABSENCE`` / ``_NOT_DETERMINED``, ``meaning.ABSENCE_SENTENCE``, ``compose.is_absence_claim``, ``verify._NEGATIONS``).
# The reader is ``normalize_text`` 's, over clauses; a clause states an absence of its datum, and a text states one when every clause
# that holds a figure does.

ABSENT_CLAUSES = [
    "מועד הסיום לא נמצא במסמכים.", "מועד הסיום לא צוין בדוח.", "לא נמצא מידע על מועד הסיום.", "אין ציון של מועד הסיום.",
    "מועד הסיום חסר בדוח.", "הדוח אינו מתייחס למועד הסיום.", "מועד הסיום טרם נקבע.", "לא ניתן לדעת את מועד הסיום.",
    "המסמך לא פורסם ומועד הסיום לא ידוע.", "הסכום לא חושב כאן.", "אין אפשרות לחשב את הסכום",
    "לא ניתן לקבוע את השווי", "לא נמצא שטח", "בשומה השנייה לא נמסר גובה התקרה בדירה", "אין בהם נתון ישיר על הגודל",
    "המסמך אינו מציין את השטח", "אין במסמכי הראיות כל מידע על כך", "הנתון אינו מופיע בשומה", "ללא ציון של הגובה",
    "אי אפשר לדעת מהקטעים", "השמאי לא התייחס לכך", "לא קיים בהם מידע על השטח", "לא נמסרו נתונים",
]
NOT_ABSENT_CLAUSES = [
    "מועד הסיום אינו כולל מע״מ.", "הקומה אינה כלולה בשטח", "אין מעלית בבניין", "אין בדירה ממ״ד", "השמאי מציין שאין בבניין מעלית",
    "לא ניתן היתר לתוספת הבנייה", "הדירה לא נמצאת בקומת הקרקע", "השמאי לא הביא בחשבון את הנתונים של העסקה", "הבניין אינו כולל מעלית",
    "בשומה מצוין כי לא קיים ממ״ד בדירה", "דירה ללא ממ״ד", "הנכס אינו רשום כדירת גן", "הסכום 1,200 ₪ ללא מע״מ",
    "לא נמצאו חריגות; מועד הסיום 2026.",                       # an absence of one datum beside a positive figure
    "בבניין א לא צוינו דמי ניהול, ובבניין ב הם 1,200 ₪.",
]


def test_one_reader_tells_an_absence_from_a_fact_about_an_absence():
    from app.extraction import normalize_text as nt

    assert [t for t in ABSENT_CLAUSES if not nt.states_absence(t)] == []
    assert [t for t in NOT_ABSENT_CLAUSES if nt.states_absence(t)] == []


def test_the_chat_and_the_answering_compose_read_absence_through_that_one_reader():
    from app.answering import compose as answering_compose
    from app.chat import meaning, verify
    from app.extraction import normalize_text as nt

    assert verify.states_absence is nt.states_absence
    for text in ABSENT_CLAUSES + NOT_ABSENT_CLAUSES:
        assert verify.states_absence(text) == nt.states_absence(text)
    assert [n for n in ("NOT_FOUND", "ABSENCE", "_NOT_DETERMINED") if hasattr(verify, n)] == []
    assert not hasattr(meaning, "ABSENCE_SENTENCE") and not hasattr(answering_compose, "is_absence_claim")


def test_an_absence_clause_is_found_by_its_span_and_a_positive_clause_beside_it_is_not():
    from app.chat import meaning

    text = "הסכום לא צוין אם הוא כולל מע״מ; דמי הניהול הם 1,200 ₪ ללא מע״מ."
    spans = meaning.qualifier_absence_spans(text)
    assert [text[a:b].strip() for a, b in spans] == ["הסכום לא צוין אם הוא כולל מע״מ"]


def test_the_backstop_removes_an_uncited_absence_sentence_in_any_wording_the_reader_knows():
    """The judge neither links nor marks the sentence (it is «not factual»); the server states the gap itself, so a sentence that says the
    component is absent - «not stated», «not determined», «does not refer to» too, not only «not found» - is replaced by that statement."""
    import json

    from app.chat import calc
    from app.chat.verify import verify_answer
    from tests.unit.test_coverage import _answer as coverage_answer
    from tests.unit.test_coverage import _finish, _scores, _turn
    from tests.unit.test_coverage import _judge as judge
    from tests.unit.test_coverage import _value as value
    from tests.unit.test_coverage import _ws as ws_of

    for sentence in ("היחס לסף לא נמצא בחיפוש במסמכים.", "היחס לסף לא צוין בדוח.", "הדוח אינו מתייחס ליחס לסף.", "אין אפשרות לחשב את היחס לסף."):
        ws = ws_of("סיכום: השווי למ\"ר הוא 9,500 ₪. סף ההשוואה הוא 8,000 ₪ למ\"ר.")
        v1, v2 = value(ws, "9,500", "השווי למ\"ר"), value(ws, "8,000", "סף ההשוואה")
        turn = _turn({"id": "1", "text": "השווי למ\"ר"}, {"id": "2", "text": "היחס לסף", "kind": "calculation"})
        turn.record("calculate", json.dumps({"expression": "V1 / V2", "label": "היחס לסף"}),
                    f"שגיאה: אי אפשר לחשב: היחידות אינן מתאימות (V1 / V2). {calc.MSG_FAILED}")
        a = coverage_answer("השווי למ\"ר הוא 9,500 ₪ [S1].\n" + sentence)
        p = judge(_scores({"N1": {"status": "full", "units": [0]}, "N2": {"status": "missing", "related": [v1, v2, "F1"]}}),
                  rule=lambda t: "not_factual" if "היחס" in t and "[S1]" not in t else "supported")
        r = verify_answer(p, a, ws, "?", [], requirements=turn)
        final, _ = _finish(ws, a, r, turn)
        assert sentence not in final.answer_markdown, sentence
        assert "9,500" in final.answer_markdown


def test_a_claim_of_kind_absence_is_removed_by_the_backstop_whatever_its_wording():
    """The claim record carries the kind: an uncited unit whose claim the judge called an absence is replaced by the server's statement even
    when the reader knows no word of its sentence."""
    from app.chat.verify import verify_answer
    from tests.unit.test_coverage import _answer as coverage_answer
    from tests.unit.test_coverage import _finish, _scores, _turn
    from tests.unit.test_coverage import _judge as judge
    from tests.unit.test_coverage import _ws as ws_of

    ws = ws_of("סיכום: השווי למ\"ר הוא 9,500 ₪.")
    turn = _turn({"id": "1", "text": "השווי למ\"ר"}, {"id": "2", "text": "היחס לסף"})
    sentence = "מה שנוגע ליחס לסף נותר בלי מענה בטקסט."
    a = coverage_answer("השווי למ\"ר הוא 9,500 ₪ [S1].\n" + sentence)
    p = judge(_scores({"N1": {"status": "full", "units": [0]}, "N2": {"status": "missing", "related": ["H1"]}}))
    r = verify_answer(p, a, ws, "?", [], requirements=turn)
    (record,) = r.claims[1]
    record.kind = "absence"  # what the judge's claim kind says; the wording is one the reader does not know
    final, _ = _finish(ws, a, r, turn)
    assert sentence not in final.answer_markdown and "9,500" in final.answer_markdown


def test_a_vat_phrase_is_ignored_only_inside_the_absence_clause_not_in_the_clause_beside_it():
    """Round 22 U6: the one reader of absence is wider than the lists it replaced; the VAT readers skip the absence CLAUSE, so a real VAT basis stated
    beside it (the partial mark of a number whose basis differs) is still read."""
    from app.chat import meaning
    from app.chat.verify import vat_attachments

    def forms(text):
        return {polarity for _, polarity in vat_attachments(text, skip_absence=True)[0]}

    assert forms("לא ניתן לדעת אם 5,000 ₪ כולל מע״מ") == set()
    assert forms("לא צוין אם הסכום 5,000 ₪ כולל מע״מ") == set()
    assert forms("הדוח אינו מתייחס לכך, והשכר 5,000 ₪ ללא מע״מ") == {"excluded"}
    assert forms("הסכום טרם נקבע, והוא 5,000 ₪ כולל מע״מ") == {"included"}
    text = "הדוח אינו מתייחס לכך, והשכר 5,000 ₪ ללא מע״מ."
    assert [text[a:b] for a, b in meaning.qualifier_absence_spans(text)] == ["הדוח אינו מתייחס לכך"]


# --- round 22 review fixes (#6, #7) ----------------------------------------------------------------------------------------------------

def test_missing_counts_as_absence_only_when_it_predicates_an_information_or_document_noun():
    from app.extraction import normalize_text as nt

    for text in ["הנתון חסר", "חסר פירוט על השטח", "חסרים נתונים על הקומה", "חסר מידע לגבי מועד הסיום", "הפירוט חסר בשומה", "חסרה התייחסות לשטח"]:
        assert nt.absence_clause(text) and nt.states_absence(text), text
    for text in ["המחסן חסר היתר בנייה", "הדירה חסרה מעלית", "הבניין חסר חניה", "החדר חסר חלון", "חסרות מדרגות בכניסה"]:
        assert not nt.absence_clause(text) and not nt.states_absence(text), text
        assert nt.absence_spans(text) == [], text


def test_a_figure_before_a_prepositional_negation_keeps_its_clause_positive_and_the_bare_negation_still_reads_as_absence():
    from app.extraction import normalize_text as nt

    for text in ["הוצאות תחזוקה 50,000 ₪ ללא פירוט נוסף", "דמי ניהול 500 ₪ ללא ציון תאריך", "ארנונה 1,200 ₪ בלי פירוט"]:
        assert not nt.absence_clause(text) and not nt.states_absence(text), text
        assert nt.absence_spans(text) == [], text
    for text in ["ללא פירוט", "הוצאות התחזוקה ללא פירוט נוסף", "אין בהם נתון ישיר על הגודל", "הוצאות 50,000 ₪ (ללא פירוט)",
                 "הסכום 50,000 ₪ אינו מופיע בשומה", "ללא פירוט נוסף על 50,000 ₪"]:
        assert nt.states_absence(text) or nt.absence_spans(text), text
    assert nt.states_absence("הוצאות תחזוקה ללא פירוט נוסף")
    assert nt.absence_clause("הוצאות 50,000 ₪ אין בהן נתון")
