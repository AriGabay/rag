"""A follow-up is resolved in its context and the resolution is held to the user's own words: a correction
changes only what the user changed. Synthetic names and phrasings; the resolving model's output is given here,
so these tests prove the server's validation, not the model."""

from __future__ import annotations

from types import SimpleNamespace

from app.chat import resolve
from app.chat.resolve import ChangedField, ResolvedRequest, validate

DOC, OTHER, HIDDEN = "d-1", "d-2", "d-x"
VISIBLE = {DOC, OTHER}
TITLES = ["שומה רחוב הצאלון 7 קריית אלון", "שומה רחוב הערבה 2 גבעת הרימון"]

RENT_FOCUS = {"metric_as_written": "דמי שכירות ראויים למ\"ר", "metric_kind": "rent_per_area", "unit": "ILS_per_sqm",
              "period": "month", "area_basis": "מ\"ר אקוו'", "vat": "excluded", "subject": "הצאלון 7",
              "value_role": "appraiser_determination", "document_ids": [DOC]}


def _resolved(**kw) -> ResolvedRequest:
    base = dict(kind="correction", standalone_question="?", changed_fields=[], metric_kind="unknown",
                unit="unknown", period="unknown", area_basis="", vat="unknown", subject="", document_ids=[],
                ambiguity="")
    return ResolvedRequest(**(base | kw))


def _changed(*pairs: tuple[str, str]) -> list[ChangedField]:
    return [ChangedField(field=f, user_words=w) for f, w in pairs]


def test_rent_to_value_correction_stays_per_area_and_drops_the_rent_qualifiers():
    # the model got it wrong: a total value, with the rent's period carried over
    r = _resolved(changed_fields=_changed(("metric_kind", "השווי")), metric_kind="value", unit="ILS",
                  period="month", vat="excluded", area_basis="מ\"ר אקוו'", document_ids=[DOC])
    req = validate(r, RENT_FOCUS, "דווקא את השווי, לא השכירות", VISIBLE, TITLES)
    assert req.metric_kind == "value_per_area" and req.unit == "ILS_per_sqm"
    assert req.period == "unknown" and req.vat == "unknown" and req.area_basis == ""
    assert req.document_ids == [DOC] and req.subject == "הצאלון 7"
    assert req.rejected == []


def test_a_unit_change_the_user_said_is_honored():
    r = _resolved(changed_fields=_changed(("metric_kind", "לשווי"), ("unit", "הכולל")), metric_kind="value",
                  unit="ILS")
    req = validate(r, RENT_FOCUS, "התכוונתי לשווי הכולל של הנכס", VISIBLE, TITLES)
    assert req.metric_kind == "value" and req.unit == "ILS"


def test_a_change_without_the_users_words_is_reset():
    r = _resolved(changed_fields=_changed(("unit", "הכולל")), metric_kind="rent", unit="ILS")
    req = validate(r, RENT_FOCUS, "ומה לגבי הערבה?", VISIBLE, TITLES)  # "הכולל" was never said
    assert req.metric_kind == "rent_per_area" and req.unit == "ILS_per_sqm" and req.rejected == ["unit"]


def test_user_words_without_the_fields_vocabulary_do_not_count():
    r = _resolved(changed_fields=_changed(("metric_kind", "לא")), metric_kind="value", unit="ILS")
    req = validate(r, RENT_FOCUS, "לא, זה בסדר", VISIBLE, TITLES)
    assert req.metric_kind == "rent_per_area" and req.rejected == ["metric_kind"]


def test_a_field_changed_silently_keeps_the_focus_value():
    r = _resolved(metric_kind="price_per_area", period="year")  # no changed_fields at all
    req = validate(r, RENT_FOCUS, "ומה המקור לזה?", VISIBLE, TITLES)
    assert req.metric_kind == "rent_per_area" and req.period == "month"


def test_a_follow_up_naming_another_property_leaves_the_documents_to_the_tools():
    r = _resolved(kind="follow_up", changed_fields=_changed(("documents", "הערבה 2")), document_ids=[DOC])
    req = validate(r, RENT_FOCUS, "ובשומה של הערבה 2?", VISIBLE, TITLES)
    assert req.document_ids == [] and req.metric_kind == "rent_per_area"


def test_a_new_topic_without_new_subject_or_documents_is_a_follow_up():
    r = _resolved(kind="new_topic", metric_kind="value", unit="ILS")
    req = validate(r, RENT_FOCUS, "ומה לגבי השווי?", VISIBLE, TITLES)
    assert req.kind == "follow_up" and req.metric_kind == "rent_per_area"


def test_a_real_new_topic_starts_clean():
    r = _resolved(kind="new_topic", changed_fields=_changed(("subject", "תוכנית המתאר")), metric_kind="unknown",
                  subject="תוכנית המתאר", standalone_question="מה קובעת תוכנית המתאר?")
    req = validate(r, RENT_FOCUS, "שאלה אחרת: מה קובעת תוכנית המתאר בעיר?", VISIBLE, TITLES)
    assert req.kind == "new_topic" and req.metric_kind == "unknown" and req.period == "unknown"


def test_documents_the_user_cannot_see_are_dropped():
    r = _resolved(kind="new_topic", changed_fields=_changed(("subject", "הצאלון")), subject="הצאלון",
                  document_ids=[DOC, HIDDEN])
    assert validate(r, None, "ומה עם הצאלון?", VISIBLE, TITLES).document_ids == [DOC]
    focus = RENT_FOCUS | {"document_ids": [DOC, HIDDEN]}
    assert validate(_resolved(), focus, "ומה עם זה?", VISIBLE, TITLES).document_ids == [DOC]


def test_an_ambiguous_correction_asks_once():
    r = _resolved(changed_fields=_changed(("metric_kind", "השווי")), metric_kind="value_per_area",
                  ambiguity="התכוונת לשווי למ\"ר או לשווי הכולל של הנכס?")
    req = validate(r, RENT_FOCUS, "רציתי את השווי", VISIBLE, TITLES)
    assert req.clarify and req.clarify.endswith("?")


def test_the_requested_block_names_the_datum():
    r = _resolved(changed_fields=_changed(("metric_kind", "השווי")), metric_kind="value")
    block = resolve.requested_block(validate(r, RENT_FOCUS, "רציתי את השווי", VISIBLE, TITLES))
    assert "שווי ליחידת שטח" in block and "₪ למ״ר" in block


def test_a_total_for_a_per_area_request_is_a_mismatch():
    req = validate(_resolved(changed_fields=_changed(("metric_kind", "השווי")), metric_kind="value"), RENT_FOCUS,
                   "רציתי את השווי", VISIBLE, TITLES)
    assert resolve.mismatch(req, SimpleNamespace(metric_kind="value"))
    assert resolve.mismatch(req, SimpleNamespace(metric_kind="value_per_area")) is None
    assert resolve.mismatch(req, SimpleNamespace(metric_kind="unknown")) is None
    assert resolve.mismatch(req, None) is None


def test_a_follow_up_about_another_kind_of_datum_is_honored_and_not_flagged():
    for words, kind in (("כמה", "count"), ("משך", "duration")):
        r = _resolved(kind="follow_up", changed_fields=_changed(("metric_kind", words)), metric_kind=kind,
                      unit="unknown")
        req = validate(r, RENT_FOCUS, f"ומה {words} בנכס?", VISIBLE, TITLES)
        assert req.metric_kind == kind and req.rejected == []
        assert resolve.mismatch(req, SimpleNamespace(metric_kind=kind)) is None


def test_only_a_changed_metric_is_held_to_the_request():
    req = validate(_resolved(kind="follow_up"), RENT_FOCUS, "ומה המקור לזה?", VISIBLE, TITLES)
    assert req.metric_kind == "rent_per_area" and resolve.mismatch(req, SimpleNamespace(metric_kind="area")) is None
