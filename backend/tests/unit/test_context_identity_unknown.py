"""Context identity: an identity the parser cannot read is unknown, never "another property" (round 23 U3: R7, R9, R19, R20; KTD10; AE6).

A unit's identity card can quote its block and parcel in a notation ``contexts.identifiers`` did not read (a slash pair, a Latin label,
a joint "block/parcel" label) or as a garbled, digit-only text. The quote used to become a distinguishing NAME of the unit, so a claim
about the asked property that cites the unit was removed as another property's. A readable parcel is now an identifier (the same
parcel as the asked one: ``SAME``; another one: ``DIFFERENT``), and a quote that parses to nothing is noise: ``UNKNOWN``, no removal.
Synthetic text only: every name, block, parcel and amount is invented."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.chat import contexts
from tests.unit.test_appraisal_context import (  # noqa: F401 - the fixture, taken by name
    _declared_ws,
    _ident,
    _unit,
    enforced,
)

ASKED = ("40100", "12")
ASK = "מה ההכנסה נטו השנתית של מתחם גבעת האלה?"
CLAIM = "ההכנסה נטו השנתית של מתחם גבעת האלה היא 610,000 ₪"

# the same parcel (block 40100, parcel 12) as the notations a card quote can write it in
SAME_NOTATIONS = [
    "גוש 40100 חלקה 12",
    "גוש: 40100 | חלקה: 12",
    "גוש/חלקה: 40100/12",
    "גוש / חלקה  40100 / 12",
    "40100/12",
    "40100 / 12",
    "Block 40100 Parcel 12",
    "BLOCK 40100, PARCEL 12",
    "Block: 40100 Plot 12",
    "Blk. 40100 Lot 12",
    "Gush 40100 Helka 12",
    "Block/Parcel: 40100/12",
]
# the same notations of another readable parcel (block 40100, parcel 31)
OTHER_NOTATIONS = [x.replace("12", "31") for x in SAME_NOTATIONS]
# a quote that names no parcel: a bare digit pair (no label), digit noise, a label with no number, an empty mark
UNREADABLE = ["40100 12", "2315 63", "0 7 1 8 3 4", "—", "Block", "גוש", "n/a", "12", "גוש 40100", "Parcel 12"]


def _file(card: list[dict], extra: list[dict] | None = None, between: bool = False) -> contexts.Contexts:
    """Unit 1: the appraisal of the invented «מתחם גבעת האלה» (parcel 40100/12). Unit 2: an appraisal with no title and no alias whose
    identity card holds ``card`` (and nothing else names it). ``between``: an unrelated unit lies between them; ``extra``: fields of
    the card of that other unit."""
    parts = [_unit(1, "appraisal", "מתחם גבעת האלה", ("גבעת האלה",), first=0, last=7, pages=(1, 2),
                   identifiers=_ident("הדס 5", ASKED))]
    cards: dict = {}
    start = 8
    if between:
        parts.append(_unit(2, "appraisal", "פרויקט עין השקמה", ("עין השקמה",), first=8, last=12, pages=(3, 3),
                           identifiers=_ident("השקמה 2", ("40100", "55")), signals=("subject_line",)))
        cards[(8, 12)] = list(extra or [])
        start = 13
    parts.append(_unit(len(parts) + 1, "appraisal", None, first=start, last=start + 5, pages=(4, 5), signals=("subject_line",)))
    cards[(start, start + 5)] = card
    first = {p.block_first: SimpleNamespace(block_index=p.block_first, page=p.page_first, bbox=[50.0, 40.0, 500.0, 52.0])
             for p in parts[1:]}
    return contexts.from_units(parts, first, [], "v", "r1", cards=cards)


def _card(quote: str, kind: str = "block_parcel", position: str = "alone") -> list[dict]:
    fields = [{"field": kind, "quote": quote}]
    other = [{"field": "city", "quote": "כפר הדמה"}, {"field": "property_type", "quote": "מבנה משרדים"}]
    return {"alone": fields, "before": other + fields, "after": fields + other, "between": other[:1] + fields + other[1:]}[position]


def _last(cx: contexts.Contexts) -> int:
    return cx.numbers[-1]


POSITIONS = ["alone", "before", "after", "between"]


@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("between", [False, True])
@pytest.mark.parametrize("quote", SAME_NOTATIONS)
def test_the_same_parcel_in_any_notation_is_the_asked_property(quote, between, position):
    cx = _file(_card(quote, position=position), between=between)
    n = _last(cx)
    assert cx.identity(n).parcels == {ASKED}, quote
    assert cx.compare(n, [1]) == contexts.SAME, quote
    assert cx.names_of(n) == () and cx.identity(n).names == ()  # a parcel is no name of the unit


@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("between", [False, True])
@pytest.mark.parametrize("quote", OTHER_NOTATIONS)
def test_negative_control_another_readable_parcel_is_still_another_property(quote, between, position):
    cx = _file(_card(quote, position=position), between=between)
    n = _last(cx)
    assert cx.identity(n).parcels == {("40100", "31")}, quote
    assert cx.compare(n, [1]) == contexts.DIFFERENT, quote
    assert cx.compare(n, [1], complete=False) == contexts.UNKNOWN


@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("between", [False, True])
@pytest.mark.parametrize("quote", UNREADABLE)
def test_a_block_parcel_quote_that_parses_to_nothing_is_noise_and_the_decision_is_unknown(quote, between, position):
    cx = _file(_card(quote, position=position), between=between)
    n = _last(cx)
    assert cx.identity(n).empty and cx.identity(n).names == (), quote  # neither an identifier nor a name
    assert cx.compare(n, [1]) == contexts.UNKNOWN, quote


@pytest.mark.parametrize("quote", ["2315 63", "0 7 1 8", "12", "—", "n/a", "7:"])
def test_an_address_quote_with_no_word_is_noise_like_a_name_heading(quote):
    cx = _file(_card(quote, kind="address"))
    assert cx.identity(_last(cx)).empty and cx.compare(_last(cx), [1]) == contexts.UNKNOWN


def test_an_address_quote_with_words_is_still_a_name_the_unit_answers_to():
    cx = _file(_card("הצפצפה 7, כפר הדמה", kind="address"))
    assert cx.identity(_last(cx)).names == ("הצפצפה 7 כפר הדמה",)
    assert cx.compare(_last(cx), [1]) == contexts.DIFFERENT  # an unlabelled street is still a positive identity of another property


def test_a_name_card_field_is_still_a_name_and_a_noise_name_is_none():
    assert contexts.noise_heading("2315 63")
    cx = _file(_card("מתחם הנרקיס", kind="name"))
    assert cx.identity(_last(cx)).names == ("מתחם הנרקיס",) and cx.compare(_last(cx), [1]) == contexts.DIFFERENT
    assert _file(_card("2315 63", kind="name")).compare(2, [1]) == contexts.UNKNOWN


@pytest.mark.parametrize("quote", SAME_NOTATIONS)
def test_a_title_that_is_only_a_parcel_label_is_an_identifier_not_a_name(quote):
    """The same rule for the unit's stored title: a title that says nothing but a parcel (in any notation) is that parcel's identifier."""
    parts = [_unit(1, "appraisal", "מתחם גבעת האלה", ("גבעת האלה",), first=0, last=7, identifiers=_ident("הדס 5", ASKED)),
             _unit(2, "appraisal", quote, (quote,), first=8, last=12, signals=("subject_line",))]
    cx = contexts.from_units(parts, {}, [], "v", "r1")
    assert cx.compare(2, [1]) in (contexts.SAME, contexts.UNKNOWN), quote  # never "another property" for a notation of the asked parcel
    assert contexts.noise_heading(quote) or cx.identity(2).parcels == {ASKED}, quote


# --- the verifier: a claim about the asked property is not removed because the unit's label is unreadable (AE6) -----------------

def _check(cx, number: int):
    from app.chat import verify

    src = SimpleNamespace(contexts=[number], version_id="v", text="הכנסה נטו שנתית 610,000 ₪.", document_id="d", link=None)
    ws = _declared_ws(cx, {}, ASK, sources={"S1": src})
    return verify._context_check(verify.Unit(0, f"{CLAIM} [S1].", CLAIM, ["S1"]), ws, ASK)


@pytest.mark.parametrize("quote", SAME_NOTATIONS + UNREADABLE)
def test_ae6_a_claim_citing_a_unit_with_an_unreadable_or_equal_label_is_not_removed(enforced, quote):  # noqa: F811
    cx = _file(_card(quote))
    found = _check(cx, _last(cx))
    assert found.reason is None, quote
    # an unreadable label adds a neutral line for the judge; the same parcel is positively the asked property's
    assert len(found.facts) == (0 if quote in SAME_NOTATIONS else 1), quote


@pytest.mark.parametrize("quote", OTHER_NOTATIONS)
def test_ae6_protection_a_claim_citing_a_unit_of_another_readable_parcel_is_still_removed(enforced, quote):  # noqa: F811
    cx = _file(_card(quote))
    assert _check(cx, _last(cx)).reason, quote


# --- two units of one file with an equal value are told apart by their readable parcels, never merged ------------------------------

@pytest.mark.parametrize(("a", "b"), [("40100/31", "Block 40100 Parcel 55"), ("Gush 40100 Helka 31", "40100 / 55"),
                                      ("גוש/חלקה: 40100/31", "גוש 40100 חלקה 55")])
def test_two_units_of_one_file_with_different_readable_parcels_are_not_merged(a, b):
    parts = [_unit(1, "appraisal", "מתחם גבעת האלה", ("גבעת האלה",), first=0, last=7, identifiers=_ident("הדס 5", ASKED)),
             _unit(2, "appraisal", None, first=8, last=12, signals=("subject_line",)),
             _unit(3, "appraisal", None, first=13, last=18, signals=("subject_line",))]
    cx = contexts.from_units(parts, {}, [], "v", "r1", cards={(8, 12): _card(a), (13, 18): _card(b)})
    assert cx.named("גוש 40100 חלקה 31") == [2] and cx.named("Block 40100 Parcel 55") == [3]
    assert cx.named("גוש 40100 חלקה 12") == [1]
    assert cx.compare(2, [3]) == contexts.DIFFERENT and cx.compare(3, [2]) == contexts.DIFFERENT
    assert cx.compare(2, [1]) == contexts.DIFFERENT and cx.compare(1, [2]) == contexts.DIFFERENT


def test_identifiers_read_the_notations_units_use_and_nothing_unlabelled_without_a_card():
    for text in SAME_NOTATIONS:
        found = contexts.identifiers(text, card=True)
        assert found.parcels == {ASKED}, text
        assert found.label == "גוש 40100 חלקה 12"
    # the structural reading of title blocks stays labelled-only: a bare slash pair (a plan, a year, a comparable) is no identifier;
    # a card quote is a block/parcel field by its kind, so a slash pair there is a parcel, but a bare digit pair, a date or a plan is none
    for text in ("30871/22", "2024/25", "תכנית דמו/5110", "01/03/2025", "40100 12"):
        assert contexts.identifiers(text).empty, text
    for text in ("תכנית דמו/5110", "01/03/2025", "40100 12", "2315 63"):
        assert contexts.identifiers(text, card=True).empty, text
    # a Latin or joint label is a label wherever it stands
    for text in ("Block 40100 Parcel 12", "גוש/חלקה: 40100/12", "Gush 40100 Helka 12"):
        assert contexts.identifiers(text).parcels == {ASKED}, text
