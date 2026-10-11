"""A follow-up is resolved in its context and the resolution is held to the user's evidence and to the context: a
correction changes only what the user changed, a change of property never keeps the old documents, and a correct
parse is not rejected for words that are in no list. Synthetic names and phrasings; the resolving model's output
is given here, so these tests prove the server's validation, not the model. The entity lookup runs over a small
corpus with the same rules as ``documents_matching`` (every word in the title or the text)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.chat import entities, resolve
from app.chat.resolve import ChangedField, ResolvedRequest, validate
from app.extraction.normalize_text import base_normalize
from app.platform.search import _scope_terms

DOC, OTHER, NARKIS_A, NARKIS_B, BUILDING, HIDDEN = "d-1", "d-2", "d-3", "d-4", "d-5", "d-x"
CORPUS = {
    DOC: ("שומה - הדרור 5 גבעת השקד", "דירה A3 בבניין. השווי למ\"ר. מחיר עסקה בהסנונית 12 שימש להשוואה."),
    OTHER: ("שומה - הסנונית 12 עין ורד", "דירה B7 בקומה 4. דמי שכירות ראויים. הסנונית 12 שומה תכנית"),
    NARKIS_A: ("שומה - הנרקיס 4 עין ורד", "בניין מגורים הנרקיס 4 הנרקיס 4 הנרקיס 4 תכנית"),
    NARKIS_B: ("שומה - הנרקיס 4 כפר גפן", "מבנה מסחרי הנרקיס 4 תכנית"),
    BUILDING: ("שומה - האלה 9 גבעת השקד", "דירות A1 A2 A3 בבניין. A2 בקומה 2. תכנית"),
}
VISIBLE = set(CORPUS)
TITLES = [t for t, _ in CORPUS.values()]


def authorized(ids) -> set[str]:
    return {i for i in ids if i in VISIBLE}


def _tokens(text: str) -> set[str]:
    return {w.strip(".,") for w in base_normalize(text).split()}


def lookup_in(focus_ids: set[str]):
    def lookup(words: str) -> entities.Outcome:
        terms = _scope_terms(words)
        found = []
        for doc_id, (title, body) in CORPUS.items():
            tokens = _tokens(title) | _tokens(body) | entities.title_tokens(title)
            if terms and all(tokens & set(forms) for forms in terms):
                found.append({"document_id": doc_id, "title": title, "hits": sum(
                    body.count(forms[0]) for forms in terms)})
        if not found:
            for relaxed in (" ".join(entities.identifying(words, TITLES)),
                            " ".join(entities.identifying(words, TITLES, pairs_only=True))):
                if relaxed and relaxed != words:
                    out = lookup(relaxed)
                    if out.kind != "not_found":
                        return out
        return entities.choose(found, words, focus_ids)
    return lookup


RENT_FOCUS = {"metric_as_written": "דמי שכירות ראויים למ\"ר", "metric_kind": "rent_per_area", "unit": "ILS_per_sqm",
              "period": "month", "area_basis": "מ\"ר אקוו'", "vat": "excluded", "subject": "הדרור 5",
              "value_role": "appraiser_determination", "document_ids": [DOC]}
VALUE_FOCUS = RENT_FOCUS | {"metric_as_written": "שווי למ\"ר", "metric_kind": "value_per_area", "period": "unknown"}
PERCENT_FOCUS = RENT_FOCUS | {"metric_as_written": "שיעור אכלוס", "metric_kind": "rate", "unit": "percent",
                              "period": "unknown", "area_basis": ""}


def _resolved(**kw) -> ResolvedRequest:
    base = dict(relation="correction", scope="entity", standalone_question="?", changed_fields=[],
                metric_kind="unknown", unit="unknown", scale="unknown", period="unknown", area_basis="", vat="unknown",
                subject="", document_ids=[], ambiguity="")
    return ResolvedRequest(**(base | kw))


def _changed(*pairs: tuple[str, str]) -> list[ChangedField]:
    return [ChangedField(field=f, user_words=w) for f, w in pairs]


def _validate(r, focus, message, candidates=None):
    focus_ids = set((focus or {}).get("document_ids") or [])
    focus_titles = [CORPUS[d][0] for d in focus_ids if d in CORPUS]
    return validate(r, focus, message, authorized, TITLES, lookup_in(focus_ids), candidates,
                    focus_titles=focus_titles)


# --- reproduced states of round 3 (written first; they failed before the fix) ---

def test_repro_a_subject_change_never_keeps_the_previous_documents():
    r = _resolved(changed_fields=_changed(("subject", "דירה B7 ברחוב הסנונית 12")), subject="הסנונית 12, דירה B7",
                  document_ids=[DOC])
    req = _validate(r, RENT_FOCUS, "טעיתי, התכוונתי לדירה B7 ברחוב הסנונית 12")
    assert req.document_ids == [OTHER] and req.entity_changed and not req.clarify


def test_repro_a_total_parse_outside_the_word_list_is_accepted():
    # the user's words for "the total" are in no list; the parse is right
    r = _resolved(relation="scale_change", changed_fields=_changed(("scale", "לכל השארית שנותרה")),
                  metric_kind="value", unit="ILS", scale="total")
    req = _validate(r, VALUE_FOCUS, "וכמה זה יוצא לכל השארית שנותרה?")
    assert req.metric_kind == "value" and req.unit == "ILS" and req.rejected == []
    assert req.document_ids == [DOC] and resolve.mismatch(req, SimpleNamespace(metric_kind="value")) is None


# --- entities: other property, same building, similar addresses, nothing found ---

def test_another_property_in_another_document():
    r = _resolved(changed_fields=_changed(("subject", "הסנונית 12")), subject="הסנונית 12")
    req = _validate(r, RENT_FOCUS, "רגע, אני צריך את הנתון של הסנונית 12")
    assert req.document_ids == [OTHER]


def test_a_unit_in_the_same_building_keeps_its_document_even_when_another_mentions_it_more():
    focus = VALUE_FOCUS | {"document_ids": [BUILDING], "subject": "האלה 9, דירה A1"}
    r = _resolved(relation="same_datum", changed_fields=_changed(("subject", "A2")), subject="A2")
    req = _validate(r, focus, "ומה עם A2?")
    assert req.document_ids == [BUILDING]


def test_similar_addresses_in_two_towns_ask_then_resolve_with_the_town():
    r = _resolved(relation="new_question", changed_fields=_changed(("subject", "הנרקיס 4")), subject="הנרקיס 4")
    req = _validate(r, RENT_FOCUS, "ובהנרקיס 4?")
    assert req.server_clarify and req.document_ids == []
    assert "עין ורד" in req.clarify and "כפר גפן" in req.clarify
    assert {c["document_id"] for c in req.candidates} == {NARKIS_A, NARKIS_B}
    r2 = _resolved(relation="new_question", changed_fields=_changed(("subject", "הנרקיס 4 בכפר גפן")))
    assert _validate(r2, RENT_FOCUS, "ובהנרקיס 4 בכפר גפן?").document_ids == [NARKIS_B]


def test_a_bare_reply_to_the_clarification_is_resolved_among_its_candidates():
    offered = [{"document_id": NARKIS_A, "title": CORPUS[NARKIS_A][0]},
               {"document_id": NARKIS_B, "title": CORPUS[NARKIS_B][0]}]
    for reply, expected in (("בכפר גפן", NARKIS_B), ("השני", NARKIS_B), ("הראשון", NARKIS_A)):
        r = _resolved(relation="clarification_answer", changed_fields=[])
        req = _validate(r, RENT_FOCUS, reply, candidates=offered)
        assert req.document_ids == [expected], reply
    focus = RENT_FOCUS | {"subject": "הדרור 5, דירה A3"}
    chosen = _validate(_resolved(relation="clarification_answer"), focus, "בכפר גפן", candidates=offered)
    assert "A3" not in chosen.subject and "כפר גפן" in chosen.subject  # nothing of the previous property stays
    again = _validate(_resolved(relation="clarification_answer"), RENT_FOCUS, "בהנרקיס", candidates=offered)
    assert again.server_clarify and again.document_ids == [] and DOC not in again.document_ids


def test_nothing_found_asks_and_never_falls_back_to_the_focus():
    for words in ("היסמין 3", "בית האגם הצפוני"):
        r = _resolved(changed_fields=_changed(("subject", words)), subject=words)
        req = _validate(r, RENT_FOCUS, f"התכוונתי ל{words}")
        assert req.server_clarify and req.document_ids == [] and "לא מצאתי" in req.clarify, words


def test_a_generic_word_claimed_as_a_document_change_stays_on_the_focus():
    focus = RENT_FOCUS | {"document_ids": [OTHER], "subject": "הסנונית 12"}
    r = _resolved(relation="same_datum", changed_fields=_changed(("documents", "תכנית")))
    req = _validate(r, focus, "לפי איזו תכנית נקבע זה?")
    assert req.document_ids == [OTHER] and not req.clarify


def test_an_address_the_parse_did_not_claim_is_looked_up_anyway():
    r = _resolved(relation="same_datum", changed_fields=[])  # the model missed the change
    req = _validate(r, RENT_FOCUS, "ובהסנונית 12?")
    assert req.document_ids == [OTHER]


def test_documents_the_user_cannot_see_never_enter_the_request():
    r = _resolved(relation="same_datum", document_ids=[HIDDEN])
    focus = RENT_FOCUS | {"document_ids": [DOC, HIDDEN]}
    assert _validate(r, focus, "ומה עם זה?").document_ids == [DOC]


def test_a_set_question_as_a_follow_up_leaves_the_set_to_the_tools():
    r = _resolved(relation="new_question", scope="set", changed_fields=_changed(("subject", "בגבעת השקד")),
                  subject="גבעת השקד", metric_kind="count")
    req = _validate(r, RENT_FOCUS, "כמה שומות יש לנו בגבעת השקד?")
    assert req.document_ids == [] and not req.clarify and req.scope == "set"


# --- the parse: evidence and consistency, not a word list ---

def test_rent_to_value_correction_stays_per_area_and_drops_the_rent_qualifiers():
    # the model got it wrong: a total value, with the rent's period carried over
    r = _resolved(changed_fields=_changed(("metric_kind", "השווי")), metric_kind="value", unit="ILS",
                  period="month", vat="excluded", area_basis="מ\"ר אקוו'", document_ids=[DOC])
    req = _validate(r, RENT_FOCUS, "דווקא את השווי, לא השכירות")
    assert req.metric_kind == "value_per_area" and req.unit == "ILS_per_sqm"
    assert req.period == "unknown" and req.vat == "unknown" and req.area_basis == ""
    assert req.document_ids == [DOC] and req.subject == "הדרור 5" and req.rejected == []


def test_a_scale_the_user_said_is_honored():
    r = _resolved(changed_fields=_changed(("metric_kind", "לשווי"), ("scale", "הכולל")), metric_kind="value",
                  unit="ILS", scale="total")
    req = _validate(r, RENT_FOCUS, "התכוונתי לשווי הכולל של הנכס")
    assert req.metric_kind == "value" and req.unit == "ILS"


def test_a_change_without_the_users_words_becomes_unknown_not_the_old_value():
    r = _resolved(changed_fields=_changed(("scale", "הכולל")), metric_kind="rent", unit="ILS", scale="total")
    req = _validate(r, RENT_FOCUS, "ומה המקור לזה?")  # "הכולל" was never said
    assert req.rejected == ["scale"] and req.unit == "unknown"
    r = _resolved(changed_fields=_changed(("metric_kind", "המחיר")), metric_kind="price")
    req = _validate(r, RENT_FOCUS, "ומה לגבי זה?")
    assert req.metric_kind == "unknown" and req.rejected == ["metric_kind"]


def test_function_words_or_a_currency_sign_are_no_evidence():
    for words, message in (("לא", "לא, זה בסדר"), ("ובש״ח", "ובש״ח?"), ("₪", "וב-₪?")):
        r = _resolved(changed_fields=_changed(("scale", words)), metric_kind="value", unit="ILS", scale="total")
        req = _validate(r, VALUE_FOCUS, message)
        assert req.rejected == ["scale"] and "scale" not in req.approved, words


def test_the_same_datum_changes_no_metric():
    r = _resolved(relation="same_datum", changed_fields=_changed(("metric_kind", "המקור")), metric_kind="value")
    req = _validate(r, RENT_FOCUS, "ומה המקור לזה?")
    assert req.metric_kind == "rent_per_area" and req.rejected == ["metric_kind"]


def test_an_area_unit_the_user_wrote_sets_the_scale_the_parse_left_open():
    r = _resolved(relation="metric_change", changed_fields=_changed(("metric_kind", "המחיר")), metric_kind="price",
                  unit="unknown", scale="unknown")
    req = _validate(r, PERCENT_FOCUS, "ומה המחיר למ״ר שם באותו חציון?")
    assert req.metric_kind == "price_per_area" and req.unit == "ILS_per_sqm"
    assert resolve.mismatch(req, SimpleNamespace(metric_kind="price_per_area")) is None


def test_an_area_unit_inside_the_words_quoted_for_a_total_asks():
    r = _resolved(relation="scale_change", changed_fields=_changed(("scale", "לכל המ״ר")), scale="total",
                  metric_kind="value", unit="ILS")
    req = _validate(r, VALUE_FOCUS, "וכמה זה לכל המ״ר?")
    assert req.clarify and req.server_clarify


def test_a_total_that_mentions_the_previous_per_area_figure_stays_a_total():
    r = _resolved(relation="scale_change", changed_fields=_changed(("scale", "הסכום הכולל")), scale="total",
                  metric_kind="value", unit="ILS")
    req = _validate(r, VALUE_FOCUS, "ומה הסכום הכולל לפי ה-13,700 למ״ר?")
    assert req.metric_kind == "value" and not req.clarify


def test_a_new_question_reads_its_own_words_and_starts_without_documents():
    r = _resolved(relation="new_question", changed_fields=_changed(("metric_kind", "שווי דונם")),
                  metric_kind="value", unit="unknown", standalone_question="מה שווי דונם קרקע?")
    req = _validate(r, RENT_FOCUS, "עכשיו משהו אחר: מה שווי דונם קרקע?")
    assert req.kind == "new_topic" and req.document_ids == [] and req.period == "unknown"


def test_an_ambiguous_correction_asks_once():
    r = _resolved(changed_fields=_changed(("metric_kind", "השווי")), metric_kind="value_per_area",
                  ambiguity="התכוונת לשווי למ\"ר או לשווי הכולל של הנכס?")
    req = _validate(r, RENT_FOCUS, "רציתי את השווי")
    assert req.clarify and req.clarify.endswith("?") and not req.server_clarify


def test_the_requested_block_names_the_datum_and_a_changed_entity():
    r = _resolved(changed_fields=_changed(("metric_kind", "השווי")), metric_kind="value")
    block = resolve.requested_block(_validate(r, RENT_FOCUS, "רציתי את השווי"))
    assert "שווי ליחידת שטח" in block and "₪ למ״ר" in block
    r = _resolved(changed_fields=_changed(("subject", "הסנונית 12")))
    block = resolve.requested_block(_validate(r, RENT_FOCUS, "בעצם הסנונית 12"))
    assert "החליף נכס" in block and OTHER in block and DOC not in block


# --- matching the answer against the approved request ---

def test_a_total_for_a_per_area_correction_is_a_mismatch():
    req = _validate(_resolved(changed_fields=_changed(("metric_kind", "השווי")), metric_kind="value"), RENT_FOCUS,
                    "רציתי את השווי")
    assert resolve.mismatch(req, SimpleNamespace(metric_kind="value"))
    assert resolve.mismatch(req, SimpleNamespace(metric_kind="value_per_area")) is None
    assert resolve.mismatch(req, SimpleNamespace(metric_kind="unknown")) is None
    assert resolve.mismatch(req, None) is None


def test_natural_words_for_value_and_price_are_matched_by_metric():
    for words, kind in (("הערך", "value"), ("המחיר", "price"), ("השווי", "value")):
        r = _resolved(relation="metric_change", changed_fields=_changed(("metric_kind", words)), metric_kind=kind)
        req = _validate(r, RENT_FOCUS, f"ומה {words} שנקבע?")
        assert resolve.mismatch(req, SimpleNamespace(metric_kind=f"{kind}_per_area")) is None, words
        assert resolve.mismatch(req, SimpleNamespace(metric_kind="rent_per_area")), words


def test_a_metric_change_after_a_total_is_not_held_to_the_old_scale():
    focus = VALUE_FOCUS | {"metric_kind": "value", "unit": "ILS"}
    r = _resolved(relation="metric_change", changed_fields=_changed(("metric_kind", "דמי השכירות")), metric_kind="rent")
    req = _validate(r, focus, "ומה דמי השכירות?")
    assert resolve.mismatch(req, SimpleNamespace(metric_kind="rent_per_area")) is None


def test_a_follow_up_about_another_kind_of_datum_is_honored_and_not_flagged():
    for words, kind in (("כמה קומות", "count"), ("משך", "duration")):
        r = _resolved(relation="metric_change", changed_fields=_changed(("metric_kind", words)), metric_kind=kind)
        req = _validate(r, RENT_FOCUS, f"ומה {words} בנכס?")
        assert req.metric_kind == kind and req.rejected == []
        assert resolve.mismatch(req, SimpleNamespace(metric_kind=kind)) is None


def test_an_unchanged_metric_is_not_held_to_anything():
    req = _validate(_resolved(relation="same_datum"), RENT_FOCUS, "ומה המקור לזה?")
    assert req.metric_kind == "rent_per_area" and not req.approved
    assert resolve.mismatch(req, SimpleNamespace(metric_kind="area")) is None


def test_the_resolution_records_the_parse_and_the_decisions():
    r = _resolved(changed_fields=_changed(("scale", "הכולל"), ("metric_kind", "השווי")), metric_kind="value")
    req = _validate(r, RENT_FOCUS, "רציתי את השווי")
    assert req.resolution["parse"]["metric_kind"] == "value"
    assert req.resolution["decisions"]["scale"].startswith("rejected") and req.resolution["decisions"][
        "metric_kind"].startswith("accepted")


# --- review round: numbers and forms that name (or do not name) another property ------------------------------

def test_a_single_digit_house_number_on_the_same_street_is_another_property():
    found = [{"document_id": "a", "title": "שומה - הנרקיס 14 עין ורד", "hits": 9},
             {"document_id": "b", "title": "שומה - הנרקיס 4 עין ורד", "hits": 1}]
    assert [d.document_id for d in entities.choose(found, "בהנרקיס 4", {"a"}).documents] == ["b"]
    # the named number is nowhere: the focus on the same street is not the answer
    assert entities.choose(found[:1], "בהנרקיס 4", {"a"}).kind == "not_found"


def test_a_floor_a_year_or_a_duration_names_no_property():
    focus = VALUE_FOCUS | {"document_ids": [BUILDING], "subject": "האלה 9"}
    for message in ("ומה השווי של הדירה בקומה 12?", "ומה השווי ב-2024?", "וכמה זה ל-12 חודשים?"):
        req = _validate(_resolved(relation="same_datum"), focus, message)
        assert req.document_ids == [BUILDING] and not req.entity_changed and not req.clarify, message


def test_a_number_beside_a_title_word_still_names_a_property():
    req = _validate(_resolved(relation="same_datum"), RENT_FOCUS, "ובהסנונית 12?")
    assert req.document_ids == [OTHER] and req.entity_changed


def test_a_prefixed_word_of_the_focus_title_keeps_the_focus():
    focus = RENT_FOCUS | {"document_ids": [NARKIS_B], "subject": "הנרקיס 4"}
    req = validate(_resolved(relation="same_datum"), focus, "ומה כתוב בשומה של כפר גפן על זה?", authorized, TITLES,
                   lookup_in({NARKIS_B}), focus_titles=[CORPUS[NARKIS_B][0]])
    assert req.document_ids == [NARKIS_B] and not req.entity_changed and req.subject == "הנרקיס 4"


def test_a_negated_area_unit_asks_for_the_total_not_for_a_clarification():
    for words, message in (("לא במטר — כמה זה בסך הכול", "לא במטר — כמה זה בסך הכול לכל הקומה?"),
                           ("ולא למ״ר, הכולל", "ולא למ״ר, הכולל בבקשה")):
        r = _resolved(relation="scale_change", changed_fields=_changed(("scale", words)), scale="total",
                      metric_kind="rent", unit="ILS")
        req = _validate(r, RENT_FOCUS, message)
        assert not req.clarify and req.metric_kind == "rent" and req.unit == "ILS", message


def test_a_generic_subject_word_with_a_title_word_the_focus_lacks_looks_up_that_title():
    # the parse quoted only "המתחם"; the user also named a title word the focus does not hold
    r = _resolved(relation="new_question", changed_fields=_changed(("subject", "המתחם")), subject="המתחם")
    req = _validate(r, RENT_FOCUS, "שאלה אחרת: מה שטח המתחם בהסנונית 12?")
    assert req.document_ids == [OTHER] and req.entity_changed


def test_a_comparable_named_inside_the_focus_report_keeps_the_report():
    # "האורן 30" is a row of the report on "האורן 26": the report holds both words
    found = [{"document_id": "f", "title": "שומה - האורן 26 עין ורד", "hits": 3}]
    out = entities.choose(found, "למשרד בהאורן 30", {"f"})
    assert out.kind == "resolved" and [d.document_id for d in out.documents] == ["f"]


def test_a_pronoun_or_a_generic_word_quoted_as_the_subject_keeps_the_focus():
    for relation, field, words, message in (("same_datum", "documents", "לפי איזה מסמך", "ולפי איזה מסמך זה נקבע?"),
                                            ("metric_change", "subject", "בזה", "וכמה מחסנים יש בזה?"),
                                            ("metric_change", "subject", "שלו", "ומה גובה הארנונה שלו?")):
        r = _resolved(relation=relation, changed_fields=_changed((field, words)), metric_kind="count")
        req = _validate(r, RENT_FOCUS, message)
        assert req.document_ids == [DOC] and not req.clarify and not req.entity_changed, message


def test_a_correction_to_a_property_without_a_title_word_still_asks():
    r = _resolved(changed_fields=_changed(("subject", "בית האגם הצפוני")), subject="בית האגם הצפוני")
    req = _validate(r, RENT_FOCUS, "טעיתי, התכוונתי לבית האגם הצפוני")
    assert req.server_clarify and req.document_ids == []


# --- calculation terms are request kinds of their own (R22: no wrong-metric notice) ---

INCOME_FOCUS = RENT_FOCUS | {"metric_as_written": "סך ההכנסות", "metric_kind": "income", "unit": "ILS",
                             "period": "year", "area_basis": ""}


@pytest.mark.parametrize("kind", ["income", "cost", "profit", "ratio", "rate"])
def test_income_cost_profit_ratio_and_rate_are_request_and_focus_kinds(kind):
    from app.chat.engine import Focus

    assert _resolved(metric_kind=kind).metric_kind == kind
    Focus(metric_as_written="", metric_kind=kind, unit="unknown", period="unknown", area_basis="", vat="unknown",
          subject="", value_role="unknown", document_ids=[])
    assert kind in resolve.REQUEST_KINDS


def test_a_profit_question_answered_with_a_profit_figure_gets_no_notice():
    r = _resolved(relation="metric_change", changed_fields=_changed(("metric_kind", "הרווח")), metric_kind="profit")
    req = _validate(r, INCOME_FOCUS, "ומה הרווח?")
    assert req.metric_kind == "profit" and "metric" in req.approved
    assert resolve.mismatch(req, SimpleNamespace(metric_kind="profit")) is None
    # within the family a different datum is still not the one requested
    assert resolve.mismatch(req, SimpleNamespace(metric_kind="cost"))
    assert resolve.mismatch(req, SimpleNamespace(metric_kind="value"))


def test_a_profit_correction_after_a_per_area_rent_gets_no_notice():
    r = _resolved(changed_fields=_changed(("metric_kind", "לרווח")), metric_kind="profit")
    req = _validate(r, RENT_FOCUS, "התכוונתי לרווח")
    assert req.metric_kind == "profit"
    assert resolve.mismatch(req, SimpleNamespace(metric_kind="profit")) is None


def test_a_ratio_and_a_rate_are_one_family():
    r = _resolved(relation="metric_change", changed_fields=_changed(("metric_kind", "שיעור הרווח")), metric_kind="rate")
    req = _validate(r, INCOME_FOCUS, "ומה שיעור הרווח?")
    assert resolve.mismatch(req, SimpleNamespace(metric_kind="ratio")) is None
    assert resolve.mismatch(req, SimpleNamespace(metric_kind="profit"))


def test_an_unspecific_kind_on_either_side_is_no_mismatch():
    r = _resolved(relation="metric_change", changed_fields=_changed(("metric_kind", "הנתון האחר")), metric_kind="other")
    req = _validate(r, INCOME_FOCUS, "ומה הנתון האחר?")
    assert resolve.mismatch(req, SimpleNamespace(metric_kind="profit")) is None
    r = _resolved(relation="metric_change", changed_fields=_changed(("metric_kind", "הרווח")), metric_kind="profit")
    assert resolve.mismatch(_validate(r, INCOME_FOCUS, "ומה הרווח?"), SimpleNamespace(metric_kind="other")) is None


def test_a_metric_word_never_names_a_document_even_when_a_title_holds_it():
    """A correction of the metric ("התכוונתי לשווי, לא לשכירות") keeps the property: "שווי" and "שכירות" name the
    datum, not a document, even where some title holds them ("שומה שווי שוק ..."); a street that merely contains the
    letters of a metric word still names its document."""
    titles = [*TITLES, "שומה שווי שוק - הצבעוני 3 עין ורד", "שומה - שוויצר 8 עין ורד"]
    assert entities.identifying("התכוונתי לשווי, לא לשכירות", titles, bare_numbers=False) == []
    assert entities.identifying("ומה בשוויצר 8?", titles, bare_numbers=False)
    r = _resolved(changed_fields=_changed(("metric_kind", "לשווי")), metric_kind="value_per_area")
    focus_ids = set(RENT_FOCUS["document_ids"])
    req = validate(r, RENT_FOCUS, "התכוונתי לשווי, לא לשכירות", authorized, titles, lookup_in(focus_ids), None,
                   focus_titles=[CORPUS[DOC][0]])
    assert req.document_ids == [DOC] and not req.entity_changed and not req.clarify
