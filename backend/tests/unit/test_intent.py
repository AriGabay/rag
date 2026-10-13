"""The question intent and the hard evidence that lets a lookup name a property or a document (round 10 U6).

A word that titles share is no identity: a general question that uses one must never name a property. These tests give
the parse (the model's output) and prove what the server makes of it, on synthetic titles only."""

from __future__ import annotations

import pytest

from app.chat import intent
from app.chat.intent import IntentClaim, settle
from app.chat.request import RequestAnalysis
from app.chat.resolve import ResolvedRequest

# invented, clearly synthetic titles: three of them hold the general word "שומה", one is a standard-like title
VOCABULARY = ["שומה מקרקעין רחוב הדקל 5 עיר-א (סינתטי)", "תל-סינתטי שומה", "חלקה 7 בגוש שומה 1234 (סינתטי)",
              "תקן 19 - נכס סינתטי רחוב האלון 3", "דירה מגורים רחוב התמר 9 עיר-ב (סינתטי)", "מתחם הגפן ב",
              "מתחם הגפן א"]
APPROACHES = "מה הן שלושת גישות השומה הבסיסיות ?"


def claim(value: str, **kw) -> IntentClaim:
    return IntentClaim(**({"intent": value, "reference": "none", "item": None, "topic": "", "standard_refs": []} | kw))


def kinds(message: str, vocabulary=VOCABULARY) -> list[tuple[str, str]]:
    return [(e.kind, e.text) for e in intent.evidence(message, vocabulary)]


# --- evidence ---------------------------------------------------------------------------------------------------------

def test_a_general_word_that_titles_hold_is_no_evidence():
    assert kinds(APPROACHES) == []
    assert kinds("מה זה שומה ומה ההבדל בין השומות?") == []


def test_an_address_is_a_title_word_with_its_house_number_after_it():
    assert kinds("ובהדקל 5?") == [("address", "הדקל 5")]
    assert kinds("ומה עם רחוב התמר מס' 9") == [("address", "התמר 9")]  # "מס'" between them changes nothing
    assert kinds("כמה קומות יש בהדקל?") == []  # the title word alone names nothing


def test_the_words_a_model_quoted_as_a_new_subject_may_name_an_address_no_title_holds():
    assert [(e.kind, e.text) for e in intent.evidence("היסמין 3", VOCABULARY, quoted=True)] == [("address", "היסמין 3")]
    assert intent.evidence("היסמין 3", VOCABULARY) == []  # a whole message needs a title word
    assert intent.evidence("בית האגם הצפוני", VOCABULARY, quoted=True) == []
    assert intent.evidence("תקן 19", VOCABULARY, quoted=True) == [] and intent.evidence("קומה 3", VOCABULARY, quoted=True) == []


def test_a_block_and_parcel_pair_is_evidence_and_one_of_them_is_not():
    assert kinds("מה כתוב על גוש 6995 חלקה 46") == [("parcel", "גוש 6995 חלקה 46")]
    assert kinds("מה כתוב על חלקה 46 בגוש 6995") == [("parcel", "גוש 6995 חלקה 46")]
    assert kinds("מה כתוב על חלקה 46") == []


def test_a_standard_or_section_number_is_a_professional_reference_never_a_house_number():
    # the title "תקן 19 - ..." holds the word "תקן" and the number 19: still no address (AE8, R9)
    assert kinds("מה דורש תקן 19 בסעיף 3?") == []
    assert kinds("מה אומר תקן 1.1 ובסעיף 15.0") == []
    assert kinds("מה נאמר בסעיפים 3.2 ו-3.4 של תקן 19") == []
    assert kinds("מה אומר תקן 19 לגבי האלון 3?") == [("address", "האלון 3")]  # the address beside it still counts


def test_a_year_a_floor_and_a_duration_are_never_names():
    assert kinds("מה השתנה בשומה ב-2024?") == []
    assert kinds("מה יש בקומה 3 של הדקל?") == []
    assert kinds("מה בדקל בשנת 2024") == []
    assert kinds("מה בשומה לתקופה של 36 חודשים?") == []
    assert kinds("מה בהדקל 5 ב-2024?") == [("address", "הדקל 5")]


def test_a_latin_unit_label_is_evidence_but_a_latin_acronym_is_not():
    assert kinds("ומה עם דירה B7?") == [("label", "b7")]
    assert kinds("איך מחשבים NOI ושיעור היוון?") == []


def test_a_complete_name_stated_as_a_phrase_is_evidence_and_a_shared_word_is_not():
    assert kinds("מה דמי השכירות במתחם הגפן ב?") == [("name", "מתחם הגפן ב")]
    assert kinds("מה דמי השכירות בגפן?") == []
    # a one-word name counts only when no other name holds the word
    assert kinds("מה כתוב ברקפות?", ["רקפות"]) == [("name", "רקפות")]
    assert kinds("מה כתוב ברקפות?", ["רקפות", "מתחם רקפות א", "מתחם רקפות ב"]) == []
    # the unit's title and its aliases are forms of one name: the alias alone is that unit's name
    assert kinds("מה כתוב ברקפות?", ["רקפות", "מתחם גבעת רקפות", "גבעת רקפות"]) == [("name", "רקפות")]
    assert kinds("מה זו השומה?", ["שומה", "תל שומה", "שומה מקרקעין רחוב הדקל 5"]) == []


# --- settling ---------------------------------------------------------------------------------------------------------

def test_professional_without_evidence_stays_professional():
    got = settle(claim("professional", topic="גישות השומה"), APPROACHES, VOCABULARY)
    assert got.value == "professional" and got.claimed == "professional" and got.topic == "גישות השומה"
    assert got.evidence == () and not got.refers


def test_professional_with_evidence_becomes_mixed():
    got = settle(claim("professional"), "מה דורש התקן לגבי הדקל 5?", VOCABULARY)
    assert got.value == "mixed" and got.refers and [e.text for e in got.evidence] == ["הדקל 5"]
    assert any("professional claim" in r for r in got.reasons)


def test_property_and_document_keep_their_value_with_no_evidence_but_say_so():
    for value in ("property", "document"):
        got = settle(claim(value), APPROACHES, VOCABULARY, has_property=False)
        assert got.value == value and got.evidence == () and any("no lookup" in r for r in got.reasons)


def test_a_missing_or_invalid_parse_settles_from_the_evidence():
    assert settle(None, "ובהדקל 5?", VOCABULARY).value == "property"
    assert settle(None, APPROACHES, VOCABULARY).value == "unknown"
    assert settle(claim("unknown"), "ובהדקל 5?", VOCABULARY).value == "property"
    assert settle(claim("unknown"), APPROACHES, VOCABULARY).value == "unknown"
    assert settle(None, APPROACHES, VOCABULARY).claimed == ""


def test_scope_set_decides_intent_set():
    assert settle(claim("property"), "כמה שומות יש?", VOCABULARY, scope="set").value == "set"


def test_an_invalid_intent_part_is_dropped_and_the_rest_of_the_parse_stands():
    base = dict(relation="new_question", scope="entity", standalone_question="?", changed_fields=[],
                metric_kind="unknown", unit="unknown", scale="unknown", period="unknown", area_basis="", vat="unknown",
                subject="", document_ids=[], ambiguity="")
    for bad in ({"intent": "bogus"}, {"intent": "property"}, "professional", 7):
        parsed = ResolvedRequest(**base, intent=bad)
        assert parsed.intent is None and parsed.relation == "new_question"
    good = ResolvedRequest(**base, intent=claim("professional").model_dump())
    assert good.intent.intent == "professional"
    assert ResolvedRequest(**base).intent is None  # an old script: no intent part
    analysis = RequestAnalysis(components=[], intent={"intent": "nope"})
    assert analysis.intent is None and analysis.components == []


def test_the_claim_schema_is_strict():
    with pytest.raises(ValueError):
        IntentClaim(intent="property", reference="none", item=None, topic="", standard_refs=[], extra=1)


def test_a_phrase_that_only_names_hold_refers_to_them_even_when_it_fits_two():
    # round 9 AE4 after round 10: "מתחם הגפן" of "מתחם הגפן א" and "מתחם הגפן ב" is a reference to be asked about, while
    # one word that titles share, or a phrase at the head of many reports, is none
    from app.chat import intent

    names = ["מתחם הגפן א", "מתחם הגפן ב"] + [f"שומה מקרקעין רחוב {n} 5" for n in ("הדקל", "האלה", "השקד", "הגפן")]
    assert [e.text for e in intent.evidence("ובמתחם הגפן?", names)] == ["מתחם הגפן"]
    assert [e.text for e in intent.evidence("מה השווי במתחם הגפן ב", names)] == ["מתחם הגפן ב"]
    assert intent.evidence("מה זו שומה מקרקעין כללית?", names) == []
    assert intent.evidence("מה הן שלוש גישות השומה הבסיסיות?", names) == []
