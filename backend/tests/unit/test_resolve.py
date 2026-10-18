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
    # no ``intent`` part by default: an old script, which settles from the evidence the message holds (round 10 U6)
    base = dict(relation="correction", scope="entity", standalone_question="?", changed_fields=[],
                metric_kind="unknown", unit="unknown", scale="unknown", period="unknown", area_basis="", vat="unknown",
                subject="", document_ids=[], ambiguity="")
    return ResolvedRequest(**(base | kw))


def _intent(value: str, **kw) -> dict:
    return {"intent": value, "reference": "none", "item": None, "topic": "", "standard_refs": []} | kw


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


def test_similar_addresses_in_two_towns_reach_the_model_as_candidates_then_resolve_with_the_town():
    # round 15 U6 (KTD1, R2): the lookup's ambiguity is the model's to weigh; the server asks nothing in its place
    r = _resolved(relation="new_question", changed_fields=_changed(("subject", "הנרקיס 4")), subject="הנרקיס 4")
    req = _validate(r, RENT_FOCUS, "ובהנרקיס 4?")
    assert not req.server_clarify and not req.clarify and req.document_ids == [] and req.entity_changed
    assert {c["document_id"] for c in req.offered} == {NARKIS_A, NARKIS_B} and req.candidates == []
    assert req.hints and req.resolution["decisions"]["entity"] == "ambiguous"
    assert "הנרקיס 4" in resolve.requested_block(req)  # the model is told what the lookup found
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
    # a reply that names two candidates still binds none: they reach the model again, and the old property is no scope
    assert not again.server_clarify and not again.clarify and again.document_ids == [] and DOC not in again.document_ids


def test_nothing_found_is_a_hint_for_the_model_and_never_falls_back_to_the_focus():
    # an address no title holds (a word and its house number), a title word with a number no title has, a parcel
    for words in ("היסמין 3", "הסנונית 99", "גוש 9999 חלקה 8"):
        r = _resolved(changed_fields=_changed(("subject", words)), subject=words)
        req = _validate(r, RENT_FOCUS, f"התכוונתי ל{words}")
        assert not req.server_clarify and not req.clarify and req.document_ids == [] and req.entity_changed, words
        assert req.hints and words.split()[0] in req.hints[0] and req.resolution["decisions"]["entity"] == "not_found"
        block = resolve.requested_block(req)
        assert "לא איתר" in block and "החליף נכס" in block, words


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


def test_a_correction_whose_words_hold_no_hard_evidence_releases_the_old_property_without_a_lookup():
    # round 10 R8, KTD2(b): no address, parcel, label or complete name in the words: nothing is looked up or selected by a
    # title word, and the old property is not silently kept either: its documents are no filter, the tools search
    r = _resolved(changed_fields=_changed(("subject", "בית האגם הצפוני")), subject="בית האגם הצפוני")
    req = _validate(r, RENT_FOCUS, "טעיתי, התכוונתי לבית האגם הצפוני")
    assert not req.server_clarify and "lookup" not in req.resolution
    assert req.document_ids == [] and req.entity_changed and req.subject == "בית האגם הצפוני"
    assert req.resolution["decisions"]["subject"].startswith("released the focus")


def test_a_correction_to_a_pronoun_or_a_word_of_the_trade_keeps_the_property():
    for words in ("בזה", "השומה"):
        r = _resolved(relation="correction", changed_fields=_changed(("subject", words)), subject=words)
        req = _validate(r, RENT_FOCUS, f"לא, התכוונתי ל{words}")
        assert req.document_ids == [DOC] and not req.entity_changed and "lookup" not in req.resolution


# --- calculation terms are request kinds of their own (R22: no wrong-metric notice) ---

INCOME_FOCUS = RENT_FOCUS | {"metric_as_written": "סך ההכנסות", "metric_kind": "income", "unit": "ILS",
                             "period": "year", "area_basis": ""}


@pytest.mark.parametrize("kind", ["income", "cost", "profit", "ratio", "rate"])
def test_income_cost_profit_ratio_and_rate_are_request_and_focus_kinds(kind):
    from app.chat.engine import Focus

    assert _resolved(metric_kind=kind).metric_kind == kind
    Focus(metric_as_written="", metric_kind=kind, unit="unknown", period="unknown", area_basis="", vat="unknown",
          subject="", value_role="unknown", document_ids=[], units=[])
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


# --- the unit in the conversation's focus (round 9 U10: KTD8, R14, AE5) ----------------------------------------------
# A file of several appraisals: the conversation is about one of its units (the focus carries the unit's anchor, not
# its number, as ``located_unit``: ``unit`` is already the datum's measure unit). Names are invented.

FILE, FILE_TITLE = "d-f", "קובץ שומות סינתטי"
FILE_UNITS = {
    2: {"title": "מתחם גבעת הרקפות", "aliases": ["גבעת הרקפות", "הרקפות"], "identifiers": ["רחוב הרקפת 8"],
        "page_first": 3, "page_last": 4},
    3: {"title": "פרויקט נחל הדס", "aliases": ["נחל הדס", "הדס"], "identifiers": ["רחוב הדס 3"],
        "page_first": 5, "page_last": 6},
}


def _unit_dict(no: int, reading: str = "r1", like: int | None = None, **kw) -> dict:
    """A unit of the file: number ``no`` in reading ``reading``, with the names of unit ``like`` (default: ``no``)."""
    return {"document_id": FILE, "reading_id": reading, "unit_no": no, "unit_id": f"v#{reading}#{no}",
            "kind": "appraisal", "first_label": "1", "certainty": "certain", "file_title": FILE_TITLE,
            "name_extra": 0, **FILE_UNITS[like or no], **kw}


def _anchor(no: int, reading: str = "r1", **kw) -> dict:
    return entities.unit_anchor(_unit_dict(no, reading, **kw))


def _unit_lookup(focus_ids: set[str]):
    def lookup(words: str) -> entities.Outcome:
        terms = _scope_terms(words)
        rows = []
        for no, u in FILE_UNITS.items():
            names = set().union(*(entities.title_tokens(n) for n in [u["title"], *u["aliases"]]))
            if terms and all(names & set(forms) for forms in terms):
                rows.append({"document_id": FILE, "title": FILE_TITLE, "hits": 1, "multi": True,
                             "unit": _unit_dict(no)})
        return entities.choose(rows, words, focus_ids)
    return lookup


UNIT_TITLES = [FILE_TITLE, *(n for u in FILE_UNITS.values() for n in [u["title"], *u["aliases"]])]
UNIT_FOCUS = VALUE_FOCUS | {"document_ids": [FILE], "subject": "מתחם גבעת הרקפות",
                                         "located_unit": _anchor(2)}


def _validate_unit(r, focus, message, visible=(FILE,)):
    return validate(r, focus, message, lambda ids: {i for i in ids if i in visible}, UNIT_TITLES,
                    _unit_lookup({FILE}), None, focus_titles=[FILE_TITLE])


def test_a_metric_change_keeps_the_unit_the_conversation_is_in():
    """AE5: the price, then "and the rent?" — a metric change is not a property change."""
    r = _resolved(relation="metric_change", changed_fields=_changed(("metric_kind", "דמי השכירות")), metric_kind="rent")
    req = _validate_unit(r, UNIT_FOCUS, "ומה דמי השכירות?")
    assert req.document_ids == [FILE] and not req.entity_changed and not req.clarify
    assert req.located_unit == UNIT_FOCUS["located_unit"] and req.unit_kept
    assert req.as_dict()["located_unit"]["unit_no"] == 2


@pytest.mark.parametrize("relation", ["same_datum", "scale_change", "correction"])
def test_a_computation_or_a_scale_change_or_no_change_keeps_the_unit(relation):
    req = _validate_unit(_resolved(relation=relation), UNIT_FOCUS, "ובמ״ר?")
    assert req.located_unit == UNIT_FOCUS["located_unit"] and req.unit_kept and not req.entity_changed


def test_a_follow_up_naming_another_unit_of_the_same_file_moves_to_it():
    r = _resolved(relation="correction", changed_fields=_changed(("subject", "בפרויקט נחל הדס")),
                  subject="פרויקט נחל הדס")
    req = _validate_unit(r, UNIT_FOCUS, "ובפרויקט נחל הדס?")
    assert req.document_ids == [FILE]  # the same document, another unit: only the units differ
    assert req.entity_changed and not req.unit_kept
    assert (req.located_unit["unit_no"], req.located_unit["display"]) == (3, "פרויקט נחל הדס")


def test_a_unit_alias_in_a_follow_up_is_an_identifying_word_the_file_title_lacks():
    assert entities.identifying("ומה השכירות בהדס?", [FILE_TITLE], bare_numbers=False) == []
    assert entities.identifying("ומה השכירות בהדס?", UNIT_TITLES, bare_numbers=False) == ["בהדס"]
    # the parse claimed no subject change: the alias alone moves the conversation to the unit it names
    req = _validate_unit(_resolved(relation="same_datum"), UNIT_FOCUS, "ומה השכירות בהדס?")
    assert req.entity_changed and req.located_unit["unit_no"] == 3


def test_naming_the_unit_the_conversation_is_in_changes_nothing():
    r = _resolved(relation="same_datum", changed_fields=_changed(("subject", "בהרקפות")), subject="הרקפות")
    req = _validate_unit(r, UNIT_FOCUS | {"subject": ""}, "ומה השכירות בהרקפות?")
    assert not req.entity_changed and req.unit_kept and req.located_unit["unit_no"] == 2


def test_a_set_question_or_a_new_question_or_an_unseen_file_leaves_no_unit():
    assert _validate_unit(_resolved(relation="same_datum", scope="set"), UNIT_FOCUS, "ובכל הקבצים?").located_unit is None
    assert _validate_unit(_resolved(relation="new_question"), UNIT_FOCUS, "שאלה אחרת: מה הוצאות הניהול?"
                          ).located_unit is None
    # the file is no longer visible: the focus document goes, and the unit with it, silently
    req = _validate_unit(_resolved(relation="same_datum"), UNIT_FOCUS, "ומה השכירות?", visible=())
    assert req.document_ids == [] and req.located_unit is None and not req.unit_kept and not req.clarify


def test_a_focus_with_no_unit_leaves_the_request_with_none():
    req = _validate_unit(_resolved(relation="same_datum"), VALUE_FOCUS | {"document_ids": [FILE]}, "ובמ״ר?")
    assert req.located_unit is None and not req.unit_kept


def test_the_same_unit_across_readings_is_matched_by_its_anchor_never_by_its_number():
    old = _anchor(2, "r1")
    same_number_other_name = _anchor(2, "r2", title="פרויקט אחר", aliases=["אחר"], identifiers=["רחוב אחר 1"])
    renumbered = _anchor(5, "r2", like=2)
    assert entities.same_unit(old, _anchor(2, "r1"))
    assert not entities.same_unit(old, _anchor(3, "r1"))
    assert entities.same_unit(old, renumbered)  # a new reading, a new number, the same names
    assert not entities.same_unit(old, same_number_other_name)  # the number alone proves nothing
    assert not entities.same_unit(None, old) and not entities.same_unit(old, None)


def test_a_new_reading_re_matches_by_title_or_alias_then_by_page_label_and_identifiers():
    # the reading was redone: the same two appraisals under other numbers, with another one in front
    current = [_unit_dict(1, "r2", like=2, title="פרויקט אחר", aliases=[], identifiers=[], page_first=1, page_last=2),
               _unit_dict(4, "r2", like=2), _unit_dict(5, "r2", like=3)]
    assert entities.rematch(_anchor(2, "r1"), current)["unit_no"] == 4
    assert entities.rematch(_anchor(3, "r1"), current)["unit_no"] == 5
    # no name in common: the first printed page label together with an identifier
    renamed = _anchor(2, "r1", title="שם ישן", aliases=["ישן"])
    assert entities.rematch(renamed, current)["unit_no"] == 4  # its identifier and page label survived
    assert entities.rematch(renamed | {"identifiers": []}, current) is None
    # a unit that is gone: nothing matches, and the old number selects nothing
    assert entities.rematch(_anchor(2, "r1", title="נעלם", aliases=["נעלם"], identifiers=["רחוב נעלם 1"]), [
        _unit_dict(2, "r2", title="נעלם לא", aliases=[], identifiers=[])]) is None


# --- the files a lookup hint names are bound to the message (authorization) ---

def _named(outcome: entities.Outcome) -> list[str]:
    r = _resolved(relation="new_question", changed_fields=_changed(("subject", "היסמין 3")), subject="היסמין 3")
    req = validate(r, RENT_FOCUS, "ובהיסמין 3?", authorized, TITLES, lambda words: outcome, None,
                   focus_titles=[CORPUS[DOC][0]])
    assert not req.server_clarify and not req.clarify and req.hints  # the model is told; the server asks nothing
    return req.as_dict()["named_document_ids"]


def test_a_lookup_hint_names_the_ids_of_the_split_files_and_the_shown_unread_files():
    unread = [{"document_id": f"u-{i}", "title": f"תיק {i}", "status": "failed", "reason": "", "progress": None,
               "uploaded_at": None} for i in range(unread_notice_limit() + 2)]
    split = [{"document_id": "s-1", "title": "קובץ מפוצל", "units": 3}]
    named = _named(entities.Outcome("not_found", unread=unread, split=split))
    assert named[0] == "s-1" and len(named) == 1 + unread_notice_limit()  # the unread files beyond the notice are not named
    assert set(named[1:]) <= {d["document_id"] for d in unread}


def test_a_not_read_hint_names_the_unread_documents():
    unread = {"document_id": "u-1", "title": "תיק 1", "status": "failed", "reason": "", "progress": None}
    named = _named(entities.Outcome("not_read", documents=[entities.Candidate("u-1", "תיק 1", unread=unread)]))
    assert named == ["u-1"]


def unread_notice_limit() -> int:
    from app.chat import unread

    return unread.NOTICE_FILES


# --- round 10 U6: intent and evidence-gated lookup (the reported "השומה" failure, on synthetic titles) ---

SYN_FOCUS, SYN_A, SYN_B, SYN_C, SYN_STD = "s-0", "s-1", "s-2", "s-3", "s-4"
SYN = {
    SYN_FOCUS: ("דירת מגורים רחוב התמר 9 עיר-ב (סינתטי)", "דמי שכירות ראויים למ\"ר. הנכס בקומה 3."),
    SYN_A: ("שומה מקרקעין רחוב הדקל 5 עיר-א (סינתטי)", "שומה לפי גישת ההשוואה. הדקל 5."),
    SYN_B: ("תל-סינתטי שומה", "שומה לפי גישת ההכנסות."),
    SYN_C: ("חלקה 7 בגוש שומה 1234 (סינתטי)", "שומה לפי גישת העלות. גוש 1234 חלקה 7."),
    SYN_STD: ("תקן 19 - נכס סינתטי רחוב האלון 3", "תקן 19 סעיף 3 מגדיר את מבנה הדוח. האלון 3."),
}
SYN_TITLES = [t for t, _ in SYN.values()]
SYN_FOCUS_DATUM = RENT_FOCUS | {"subject": "התמר 9", "document_ids": [SYN_FOCUS]}
APPROACHES = "מה הן שלושת גישות השומה הבסיסיות ?"


def _syn_lookup(focus_ids: set[str], calls: list):
    def lookup(words: str) -> entities.Outcome:
        calls.append(words)
        terms = _scope_terms(words)
        found = []
        for doc_id, (title, body) in SYN.items():
            tokens = _tokens(title) | _tokens(body) | entities.title_tokens(title)
            if terms and all(tokens & set(forms) for forms in terms):
                found.append({"document_id": doc_id, "title": title, "hits": 1})
        return entities.choose(found, words, focus_ids)
    return lookup


def _syn_validate(r, message, focus=SYN_FOCUS_DATUM):
    calls: list = []
    ids = set(focus.get("document_ids") or [])
    req = validate(r, focus, message, lambda xs: {i for i in xs if i in SYN}, SYN_TITLES, _syn_lookup(ids, calls), None,
                   focus_titles=[SYN[d][0] for d in ids if d in SYN])
    return req, calls


def _approaches_parse(**kw):
    return _resolved(relation="new_question", changed_fields=[], **kw)


def test_repro_a_general_word_that_titles_share_never_lists_properties():
    # the parse says a new professional question, or says nothing about the intent: either way "השומה" names nothing
    for parsed in (_approaches_parse(intent=_intent("professional", topic="גישות השומה")), _approaches_parse(),
                   _approaches_parse(intent=_intent("unknown"))):
        req, calls = _syn_validate(parsed, APPROACHES)
        assert calls == [] and "lookup" not in req.resolution, parsed.intent
        assert not req.server_clarify and not req.clarify and req.candidates == []
        assert req.intent.value in ("professional", "unknown") and not req.entity_changed


def test_a_professional_question_inherits_no_property_and_no_measure():
    parsed = _approaches_parse(intent=_intent("professional", topic="גישות השומה"))
    req, calls = _syn_validate(parsed, APPROACHES)
    assert req.intent.value == "professional" and req.resolution["decisions"]["intent"] == "professional"
    assert req.document_ids == [] and req.located_unit is None and req.entity_changed is False
    # the measure comes from the parse alone: nothing of the focus's rent, period, VAT, area basis or subject
    assert (req.metric_kind, req.unit, req.period, req.vat, req.area_basis, req.subject) == (
        "unknown", "unknown", "unknown", "unknown", "", "")
    assert req.as_dict()["intent"]["value"] == "professional"
    # the same holds when the parse calls the turn "the same datum"
    req, _ = _syn_validate(_resolved(relation="same_datum", intent=_intent("professional")), APPROACHES)
    assert req.metric_kind == "unknown" and req.document_ids == [] and req.subject == ""


def test_a_property_parse_without_evidence_runs_no_lookup_and_keeps_the_focus():
    req, calls = _syn_validate(_approaches_parse(intent=_intent("property")), APPROACHES)
    assert calls == [] and "lookup" not in req.resolution
    assert req.intent.value == "property"
    # a new question reads the user's words: the focus is context, not a filter, and nothing is searched in it
    assert not req.server_clarify
    parsed = _resolved(relation="same_datum", intent=_intent("property"))
    req, calls = _syn_validate(parsed, "ומה עוד כתוב על זה?")
    assert calls == [] and req.document_ids == [SYN_FOCUS] and not req.entity_changed


def test_a_mixed_parse_that_names_an_address_looks_it_up():
    parsed = _approaches_parse(intent=_intent("mixed", topic="גישות השומה"))
    req, calls = _syn_validate(parsed, "מה דורשת גישת ההשוואה לגבי הדקל 5?")
    assert req.intent.value == "mixed" and calls == ["הדקל 5"]
    assert req.document_ids == [SYN_A] and req.entity_changed and req.resolution["decisions"]["intent"] == "mixed"


def test_a_professional_parse_that_names_an_address_stays_professional_and_looks_nothing_up():
    """Round 15 U6 (KTD2, R1): the analysis' reading is the intent. The address is evidence the server records as a hint; it
    no longer turns a professional claim into a property lookup."""
    parsed = _approaches_parse(intent=_intent("professional", topic="גישות השומה"))
    req, calls = _syn_validate(parsed, "מה דורשת גישת ההשוואה לגבי הדקל 5?")
    assert req.intent.value == "professional" and calls == [] and req.document_ids == [] and not req.entity_changed
    assert [e.text for e in req.intent.evidence] == ["הדקל 5"]


def test_a_standard_number_is_not_an_address_and_names_no_appraisal():
    for parsed in (_approaches_parse(intent=_intent("professional", standard_refs=[
            {"standard": "19", "section": "3"}])), _approaches_parse(intent=_intent("property")),
            _approaches_parse()):
        req, calls = _syn_validate(parsed, "מה דורש תקן 19 בסעיף 3?")
        assert calls == [] and req.document_ids in ([], [SYN_FOCUS]) and not req.server_clarify
        assert not req.entity_changed


def test_a_year_and_a_floor_beside_a_title_word_are_no_evidence_and_a_parcel_pair_is():
    for message in ("מה היה בשומה ב-2024?", "מה יש בקומה 3 בשומה?"):
        req, calls = _syn_validate(_approaches_parse(intent=_intent("property")), message)
        assert calls == [], message
    req, calls = _syn_validate(_approaches_parse(intent=_intent("property")), "מה כתוב על גוש 1234 חלקה 7?")
    assert calls == ["גוש 1234 חלקה 7"] and req.document_ids == [SYN_C]


def test_a_quoted_subject_of_a_title_word_is_rejected_as_evidence():
    for words in ("בזה", "השומה"):
        parsed = _resolved(relation="new_question", changed_fields=_changed(("subject", words)), subject=words,
                           intent=_intent("property", reference="named_entity"))
        req, calls = _syn_validate(parsed, f"ומה לגבי {words}?")
        assert calls == [] and req.document_ids in ([], [SYN_FOCUS]) and not req.server_clarify, words


def test_a_failed_parse_with_an_address_is_a_property_and_without_one_runs_no_lookup():
    req, calls = _syn_validate(_resolved(relation="same_datum"), "ובהדקל 5?")
    assert req.intent.value == "property" and req.intent.claimed == "" and calls == ["הדקל 5"]
    assert req.document_ids == [SYN_A]
    req, calls = _syn_validate(_resolved(relation="same_datum"), "ומה לגבי השומה?")
    assert req.intent.value == "unknown" and calls == []


def test_a_correction_runs_a_lookup_only_on_evidence():
    parsed = _resolved(relation="correction", changed_fields=_changed(("subject", "השומה")), subject="השומה",
                       intent=_intent("property", reference="named_entity"))
    req, calls = _syn_validate(parsed, "לא, התכוונתי לשומה")
    assert calls == [] and not req.server_clarify
    parsed = _resolved(relation="correction", changed_fields=_changed(("subject", "הדקל 5")), subject="הדקל 5",
                       intent=_intent("property", reference="named_entity"))
    req, calls = _syn_validate(parsed, "לא, התכוונתי להדקל 5")
    assert calls == ["הדקל 5"] and req.document_ids == [SYN_A]


def test_the_focus_own_name_is_no_new_evidence():
    focus = SYN_FOCUS_DATUM | {"document_ids": [SYN_A], "subject": "הדקל 5"}
    parsed = _resolved(relation="same_datum", intent=_intent("property", reference="conversation_property"))
    req, calls = _syn_validate(parsed, "ומה עוד כתוב על הדקל 5?", focus=focus)
    assert calls == [] and req.document_ids == [SYN_A] and not req.entity_changed


# --- round 15 U6: the candidates block (KTD1) is data, in a stable order, with the basis of each card -------------------

def _card(no: int, title: str, basis: str = "", fields: tuple = ()) -> entities.UnitCard:
    row = {"document_id": FILE, "version_id": "v-1", "reading_id": "r1", "unit_no": no, "units_tag": "v5",
           "unit_id": f"v-1#r1#{no}@v5", "kind": "appraisal", "title": title, "aliases": [], "identifiers": [f"רחוב ד {no}"],
           "page_first": no, "page_last": no + 1, "block_first": 0, "block_last": 1, "certainty": "certain",
           "file_title": FILE_TITLE}
    return entities.UnitCard(row, basis, fields)


def test_the_candidates_block_lists_the_cards_in_order_with_handles_basis_and_card_text_and_counts_the_rest():
    cards = [_card(2, "פרויקט א", entities.BASIS_NAME, ({"field": "name", "quote": "שם <b>מוזר</b>"},
                                                           {"field": "city", "quote": "עיר-ב"})),
             _card(3, "פרויקט ב", entities.BASIS_SHARED.format(words="פרויקט"))]
    block = resolve.candidates_block(cards, lambda c: f"U{c.row['unit_no'] - 1}", more=4)
    assert block.startswith(resolve.CANDIDATES_HEAD)
    lines = block.splitlines()
    assert lines[1].startswith("- U1 «פרויקט א» | עמודים 2–3 | רחוב ד 2 | קובץ «" + FILE_TITLE + "»")
    assert lines[2] == f"  {resolve.BASIS_PREFIX} {entities.BASIS_NAME}"
    assert "שם «שם ‹b›מוזר‹/b›»" in lines[3] and "עיר «עיר-ב»" in lines[3]  # document text cannot open a tag
    assert lines.index(next(x for x in lines if x.startswith("- U2 «פרויקט ב»"))) > 3
    assert any(x.startswith("ועוד 4 ") for x in lines)  # the rest is counted, never listed
    assert resolve.candidates_block([], lambda c: "U1") == ""
    # nothing in it changes between two runs of the same turn
    assert block == resolve.candidates_block(cards, lambda c: f"U{c.row['unit_no'] - 1}", more=4)


def test_a_unit_gone_after_a_reread_is_told_to_the_model_with_its_old_name_and_no_number():
    hint = resolve.gone_unit_hint({"display": "מתחם <x>", "unit_no": 7})
    assert "מתחם ‹x›" in hint and "7" not in hint
