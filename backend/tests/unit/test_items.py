"""The item ledger (round 17 U2, KTD2; R4, R5, R7): the agent lists, under each frozen component, the members of the asked kind
that the sources it read state; the server keeps only the items that pass the round-16 support checks, decides coverage by
attribution (not by words or numbers somewhere in the answer) and keeps the turn's ledger across answer versions. Synthetic
documents only."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.chat import calc, items
from app.chat.engine import FINAL_SCHEMA, POLICY, FinalAnswer, LedgerItem
from app.chat.tools import TABLE_PATH_PREFIX, Workspace, unit_info
from app.chat.verify import split_units

PROV = {"unit": "source", "vat": "source"}
ADJUSTMENTS = "\n".join(["התאמות לנכס להשוואה", "סוג התאמה | שיעור", "מיקום | 5%", "קומה | 3%", "גיל הבניין | 2%"])
COMPONENTS = [{"id": "N1", "text": "ההתאמות שהשומה יישמה"}]
AGE_TABLE = "\n".join(["התאמות לנכס להשוואה", "סוג התאמה | שיעור | הערה", "קומה | 3% | התאמה לפי קומה",
                       "גיל הבניין | 2% | כאשר הבניין מעל גיל 30 שנה"])


def _ws(*texts: str, same_file: bool = False, kind: str = "table") -> Workspace:
    """S1… each a source of its own document (or of one file, ``same_file``)."""
    ws = Workspace(ctx=None)
    document, version = uuid.uuid4(), uuid.uuid4()
    for t in texts:
        ws.add_source(document_id=document if same_file else uuid.uuid4(), version_id=version if same_file else uuid.uuid4(),
                      title="דוח בדיקה", section="התאמות", location="טבלה 1", kind=kind, text=t)
    return ws


def _in_units(ws: Workspace, *numbers: int) -> None:
    """The sources S1… lie in these appraisal units (one number each) of the one file ``_ws(..., same_file=True)``."""
    from app.chat.contexts import Contexts, Identity, Segment

    first = ws.sources["S1"]
    cx = Contexts(str(first.version_id), tuple(
        Segment(n, Identity(), n * 10, n * 10 + 9, n * 10, None, n, n, "appraisal", "certain", f"שומה {n}")
        for n in (1, 2)), "r1")
    ws.contexts[str(first.version_id)] = cx
    for sid, n in zip(ws.sources, numbers, strict=True):
        ws.sources[sid].contexts, ws.sources[sid].unit = [n], unit_info(cx, n)


def _value(ws: Workspace, vid: str, written: str, source: str = "S1") -> str:
    src = ws.sources[source]
    ws.values[vid] = calc.Value(vid, Decimal(written.rstrip("%").replace(",", "")), written, source, src.document_id,
                                src.version_id, None, "דוח בדיקה", "טבלה 1", "התאמה", "other", "ILS", "none", "unknown", "",
                                "נכס הדגמה", "other", PROV, {"quote": written}, written)
    return vid


def _units(markdown: str):
    return split_units(markdown)


BASE = {"component": "N1", "label": "ההתאמה לגיל הבניין", "source": "S1", "quote": "גיל הבניין | 2%", "value": "2%",
        "condition": "", "state": "included", "reason": ""}


def _item(**kw) -> items.Item:
    return items.Item(**(BASE | kw))


def _raw(**kw) -> dict:
    """An item as the agent sends it."""
    return BASE | kw


def _validate(ws: Workspace, markdown: str, *raw: dict, components=None, ledger=None):
    return items.validate(list(raw), ws, _units(markdown), COMPONENTS if components is None else components, ledger=ledger)


# --- validation: the round-16 support checks, never up -----------------------------------------------------------------------

def test_a_valid_item_quoted_from_a_read_table_row_is_kept_with_an_item_id():
    ws = _ws(ADJUSTMENTS)
    res = _validate(ws, "ההתאמה לגיל הבניין היא 2% [S1].", _raw())
    assert [i.id for i in res.kept] == ["I1"] and res.excluded == []
    kept = res.kept[0]
    assert (kept.component, kept.source, kept.state, kept.origin) == ("N1", "S1", "included", "agent")
    assert kept.document_id == str(ws.sources["S1"].document_id)


def test_an_item_whose_source_was_never_returned_is_dropped_and_reported():
    ws = _ws(ADJUSTMENTS)
    res = _validate(ws, "ההתאמה לגיל הבניין היא 2% [S1].", _raw(source="S9"))
    assert res.kept == []
    assert [(e["source"], e["reason"]) for e in res.excluded] == [("S9", "not_returned_source")]


def test_a_listing_is_no_source_for_an_item():
    ws = _ws(ADJUSTMENTS)
    ws.add_source(document_id=None, version_id=None, title="רשימת מסמכים", section=None, location="רשימה", kind="listing",
                  text="מיקום | 5%\nקומה | 3%\nגיל הבניין | 2%")
    res = _validate(ws, "ההתאמה לגיל הבניין היא 2% [S1].", _raw(source="S2"))
    assert res.kept == [] and res.excluded[0]["reason"] == "not_returned_source"


def test_an_item_whose_quote_is_not_verbatim_is_dropped():
    ws = _ws(ADJUSTMENTS)
    res = _validate(ws, "ההתאמה לגיל הבניין היא 2% [S1].", _raw(quote="גיל הבניין | 4%"))
    assert res.kept == [] and res.excluded[0]["reason"] == "not_verbatim"


def test_a_quote_spelled_with_flattened_cells_and_spaces_is_still_verbatim():
    ws = _ws(ADJUSTMENTS)
    res = _validate(ws, "ההתאמה לגיל הבניין היא 2% [S1].", _raw(quote="גיל   הבניין 2%"))
    assert [i.id for i in res.kept] == ["I1"]


def test_an_item_with_a_quote_too_short_or_too_long_is_dropped():
    ws = _ws(ADJUSTMENTS)
    short = _validate(ws, "ההתאמה לגיל הבניין היא 2% [S1].", _raw(quote="2%"))
    assert short.excluded[0]["reason"] == "too_short"
    long = _validate(_ws("x " * 400), "ההתאמה היא 2% [S1].", _raw(quote="x " * 200))
    assert long.excluded[0]["reason"] == "too_long"


def test_an_item_from_a_document_the_answer_is_not_about_is_dropped():
    ws = _ws(ADJUSTMENTS, "מיקום | 5%\nקומה | 3%\nגיל הבניין | 9%")
    res = _validate(ws, "ההתאמה לגיל הבניין היא 2% [S1].", _raw(source="S2", quote="גיל הבניין | 9%", value="9%"))
    assert res.kept == [] and res.excluded[0]["reason"] == "other_document"


def test_an_item_from_another_appraisal_unit_of_the_same_file_is_dropped():
    ws = _ws(ADJUSTMENTS, "מיקום | 6%\nקומה | 4%\nגיל הבניין | 1%", same_file=True)
    _in_units(ws, 1, 2)
    markdown = "ההתאמה לגיל הבניין היא 2% [S1]."
    own = _validate(ws, markdown, _raw())
    other = _validate(ws, markdown, _raw(source="S2", quote="גיל הבניין | 1%", value="1%"))
    assert [i.id for i in own.kept] == ["I1"]
    assert other.kept == [] and other.excluded[0]["reason"] == "outside_asked_unit"


def test_a_not_verifiable_or_not_applicable_item_without_a_reason_is_invalid_and_recorded():
    ws = _ws(ADJUSTMENTS)
    for state in ("not_verifiable", "not_applicable"):
        res = _validate(ws, "ההתאמה לגיל הבניין היא 2% [S1].", _raw(state=state, reason="  "))
        assert res.kept == [] and res.excluded[0]["reason"] == "no_reason", state
        ok = _validate(ws, "ההתאמה לגיל הבניין היא 2% [S1].", _raw(state=state, reason="הסעיף אינו חל על דירה פרטית"))
        assert [i.state for i in ok.kept] == [state], state


def test_an_unknown_state_is_invalid():
    ws = _ws(ADJUSTMENTS)
    res = _validate(ws, "ההתאמה לגיל הבניין היא 2% [S1].", _raw(state="maybe"))
    assert res.kept == [] and res.excluded[0]["reason"] == "bad_state"


def test_a_component_given_as_text_is_mapped_to_its_frozen_id_and_an_unknown_one_is_unassigned():
    ws = _ws(ADJUSTMENTS)
    mapped = _validate(ws, "ההתאמה לגיל הבניין היא 2% [S1].", _raw(component="ההתאמות שהשומה יישמה"))
    assert [i.component for i in mapped.kept] == ["N1"]
    lower = _validate(ws, "ההתאמה לגיל הבניין היא 2% [S1].", _raw(component=" n1 "))
    assert [i.component for i in lower.kept] == ["N1"]
    unknown = _validate(ws, "ההתאמה לגיל הבניין היא 2% [S1].", _raw(component="שאלה שלא נשאלה"))
    assert unknown.kept == [] and unknown.excluded[0]["reason"] == "component_unassigned"


def test_a_turn_with_no_frozen_components_keeps_a_valid_item_without_a_component():
    ws = _ws(ADJUSTMENTS)
    res = _validate(ws, "ההתאמה לגיל הבניין היא 2% [S1].", _raw(component=""), components=[])
    assert [i.component for i in res.kept] == [""]


def test_the_same_item_listed_twice_is_one_item_and_ids_follow_the_turn_ledger():
    ws = _ws(ADJUSTMENTS)
    ledger = items.Ledger()
    first = _validate(ws, "ההתאמה לגיל הבניין היא 2% [S1].", _raw(), _raw(label="אותה התאמה"), ledger=ledger)
    assert [i.id for i in first.kept] == ["I1"] and [e["reason"] for e in first.excluded] == ["duplicate"]
    ledger.merge(first.kept)
    second = _validate(ws, "ההתאמה לגיל הבניין היא 2% [S1].", _raw(quote="קומה | 3%", value="3%", label="ההתאמה לקומה"),
                       _raw(), ledger=ledger)
    assert sorted(i.id for i in second.kept) == ["I1", "I2"]  # the first item keeps its id in the new version


def test_a_source_given_as_a_value_or_a_table_handle_is_resolved_to_the_text_it_names():
    ws = _ws(ADJUSTMENTS, "מיקום | 6%\nקומה | 4%\nגיל הבניין | 1%")
    _value(ws, "V1", "2%")
    ws.sources["S1"].tags["table"] = "T1"
    ws.sources["S2"].tags["table"] = "T2 T3"
    by_value = _validate(ws, "ההתאמה לגיל הבניין היא 2% [S1].", _raw(source="V1"))
    assert [i.source for i in by_value.kept] == ["S1"]
    by_table = _validate(ws, "ההתאמה לגיל הבניין היא 2% [S1].", _raw(source="T1"))
    assert [i.source for i in by_table.kept] == ["S1"]
    unknown = _validate(ws, "ההתאמה לגיל הבניין היא 2% [S1].", _raw(source="T9"))
    assert unknown.kept == [] and unknown.excluded[0]["reason"] == "not_returned_source"


# --- coverage by attribution (R5) --------------------------------------------------------------------------------------------

def _covered(ws: Workspace, markdown: str, item: items.Item | None = None, siblings=()) -> items.Coverage:
    """The coverage ``Ledger.outcomes`` (the path that ships) decides for ``item``, held by a ledger with its ``siblings``."""
    item = item or _item()
    ledger = items.Ledger()
    ledger.merge([item, *(s for s in siblings if s.key != item.key)])
    got = ledger.outcomes(_units(markdown), ws)[0]
    return items.Coverage(got["state"] == items.COVERED, got["covered_by"], got["why"])


def _condition(ws: Workspace, markdown: str, item: items.Item | None = None, siblings=()) -> dict:
    """The outcome ``Ledger.outcomes`` gives ``item`` (with its condition check) against the answer ``markdown``."""
    item = item or _item()
    ledger = items.Ledger()
    ledger.merge([item, *(s for s in siblings if s.key != item.key)])
    return next(o for o in ledger.outcomes(_units(markdown), ws) if o["id"] == ledger.items[item.key].id)


def _open(ledger: items.Ledger, markdown: str, ws: Workspace) -> list[items.Item]:
    """The items of the ledger still omitted against these surviving units."""
    state = {o["id"]: o["state"] for o in ledger.outcomes(_units(markdown), ws)}
    return [i for i in ledger.items.values() if state[i.id] == items.OMITTED]


def test_an_item_stated_by_a_unit_citing_its_own_source_is_covered():
    ws = _ws(ADJUSTMENTS)
    got = _covered(ws, "ההתאמה לגיל הבניין היא 2% [S1].")
    assert got.ok and got.unit == 0


def test_ae4_the_number_in_a_unit_citing_another_document_does_not_cover_the_item():
    ws = _ws(ADJUSTMENTS, "מיקום | 5%\nקומה | 3%\nגיל הבניין | 2%")
    got = _covered(ws, "בשומה האחרת ההתאמה לגיל הבניין היא 2% [S2].")
    assert not got.ok and got.why == "not_attributed"


def test_a_number_in_a_unit_citing_nothing_does_not_cover_the_item():
    ws = _ws(ADJUSTMENTS)
    assert not _covered(ws, "ההתאמה לגיל הבניין היא 2%.").ok


def test_a_unit_citing_the_source_without_the_items_value_does_not_cover_it():
    ws = _ws(ADJUSTMENTS)
    got = _covered(ws, "ההתאמה לגיל הבניין נקבעה בטבלה [S1]. ההתאמה לקומה היא 3% [S1].")
    assert not got.ok and got.why == "value_not_stated"


def test_a_unit_of_another_appraisal_unit_of_the_same_file_does_not_cover_the_item():
    ws = _ws(ADJUSTMENTS, "מיקום | 6%\nקומה | 4%\nגיל הבניין | 2%", same_file=True)
    _in_units(ws, 1, 2)
    assert not _covered(ws, "ההתאמה לגיל הבניין בשומה השנייה היא 2% [S2].").ok
    assert _covered(ws, "ההתאמה לגיל הבניין היא 2% [S1].").ok


def test_the_value_given_as_a_registered_value_of_the_items_source_covers_the_item():
    ws = _ws(ADJUSTMENTS)
    _value(ws, "V1", "2%")
    assert _covered(ws, "ההתאמה לגיל הבניין היא 2% [V1].").ok


def test_a_registered_value_of_another_source_does_not_cover_the_item():
    ws = _ws(ADJUSTMENTS, "גיל הבניין | 2%")
    _value(ws, "V1", "2%", source="S2")
    assert not _covered(ws, "ההתאמה לגיל הבניין היא 2% [V1].").ok


def test_a_re_read_of_the_same_table_in_the_same_unit_covers_the_item_it_states():
    ws = _ws(ADJUSTMENTS, ADJUSTMENTS + "\nהערה: לפי נתוני השוק", same_file=True)
    _in_units(ws, 1, 1)
    assert _covered(ws, "ההתאמה לגיל הבניין היא 2% [S2].").ok


def test_a_calculation_made_from_a_value_of_the_items_source_covers_the_item_it_states():
    from types import SimpleNamespace

    ws = _ws(ADJUSTMENTS)
    _value(ws, "V1", "2%")
    ws.computations["C1"] = SimpleNamespace(leaves=["V1"])
    assert _covered(ws, "ההתאמה לגיל הבניין, כפי שחושבה, היא 2% [C1].").ok
    ws.computations["C2"] = SimpleNamespace(leaves=["V9"])
    assert not _covered(ws, "ההתאמה לגיל הבניין היא 2% [C2].").ok


def test_a_label_only_item_is_covered_by_a_unit_citing_its_source_with_half_the_labels_words():
    text = "סיבת ההתאמה: הנכס סמוך לכביש ראשי ורועש\nמיקום | 5%"
    ws = _ws(text)
    item = _item(label="הנכס סמוך לכביש ראשי ורועש", quote="הנכס סמוך לכביש ראשי ורועש", value="")
    assert _covered(ws, "ההתאמה ניתנה כי הנכס סמוך לכביש ראשי [S1].", item).ok
    assert not _covered(ws, "ההתאמה ניתנה בגלל הנכס [S1].", item).ok
    assert not _covered(ws, "הנכס סמוך לכביש ראשי ורועש.", item).ok  # the same words, no attribution


def test_items_that_share_a_value_need_their_own_condition_words():
    text = "\n".join(["שיעורי היוון", "שימוש | שיעור", "משרדים בקומות עליונות | 7%", "חנויות בקומת קרקע | 7%"])
    ws = _ws(text)
    upper = _item(label="שיעור למשרדים", quote="משרדים בקומות עליונות | 7%", value="7%", condition="משרדים בקומות עליונות")
    ground = _item(label="שיעור לחנויות", quote="חנויות בקומת קרקע | 7%", value="7%", condition="חנויות בקומת קרקע")
    both = (upper, ground)
    markdown = "השיעור הוא 7% [S1]."
    assert not _covered(ws, markdown, upper, both).ok and not _covered(ws, markdown, ground, both).ok
    assert _covered(ws, "השיעור למשרדים בקומות עליונות הוא 7% [S1].", upper, both).ok
    assert not _covered(ws, "השיעור למשרדים בקומות עליונות הוא 7% [S1].", ground, both).ok
    assert _covered(ws, "השיעור לחנויות בקומת קרקע הוא 7% [S1].", ground, both).ok


def test_an_item_whose_value_no_sibling_shares_is_covered_without_its_condition_words_and_its_condition_is_asked_apart():
    # round 18 U5 (KTD1): the disambiguation check (``shared``) is not the condition check. The item is still covered by its
    # attribution and value, as before, but its condition is no longer free: it has an outcome of its own — undecided here, because
    # the sentence leaves the threshold out — that the requirements call decides. The old test pinned that nothing asked it.
    ws = _ws(AGE_TABLE)
    item = _item(condition="כאשר הבניין מעל גיל 30 שנה")
    sibling = _item(quote="קומה | 3%", value="3%")
    assert _covered(ws, "ההתאמה לגיל הבניין היא 2% [S1].", item, (item, sibling)).ok
    assert _condition(ws, "ההתאמה לגיל הבניין היא 2% [S1].", item, (item, sibling))["condition_check"] == "undecided"
    stated = "ההתאמה לגיל הבניין היא 2% כאשר הבניין מעל גיל 30 שנה [S1]."
    assert _condition(ws, stated, item, (item, sibling))["condition_check"] == "kept"


def test_a_paraphrased_condition_with_the_right_source_value_and_date_still_covers_the_item_and_its_words_settle_the_outcome():
    # round 18 U5: the paraphrase still passes the disambiguation words (a shared value needs half of them), and the same words
    # settle the condition outcome (most of the note's content words are stated). A rephrasing with other words is undecided and
    # goes to the requirements call (the next test). The old test pinned only the first half.
    text = "\n".join(["שיעורי היוון", "שימוש | שיעור | הערה", "משרדים | 7% | נכס מושכר לתקופה של חמש שנים", "חנויות | 7% | נכס פנוי"])
    ws = _ws(text)
    offices = _item(label="שיעור למשרדים", quote="משרדים | 7%", value="7%", condition="נכס מושכר לתקופה של חמש שנים")
    shops = _item(label="שיעור לחנויות", quote="חנויות | 7%", value="7%", condition="נכס פנוי")
    markdown = "שיעור של 7% חל על נכס שהושכר לחמש שנים [S1]."
    assert _covered(ws, markdown, offices, (offices, shops)).ok
    assert _condition(ws, markdown, offices, (offices, shops))["condition_check"] == "kept"


def test_a_rephrasing_with_other_words_is_undecided_not_kept_and_not_a_contradiction():
    text = "\n".join(["שיעורי היוון", "שימוש | שיעור | הערה", "משרדים | 7% | נכס מושכר לתקופה של חמש שנים", "חנויות | 7% | נכס פנוי"])
    ws = _ws(text)
    offices = _item(label="שיעור למשרדים", quote="משרדים | 7%", value="7%", condition="נכס מושכר לתקופה של חמש שנים")
    got = _condition(ws, "שיעור המשרדים הוא 7% בנכס שהוחכר ל-60 חודשים [S1].", offices)
    assert got["condition_check"] == "undecided" and got["condition_hint"] == []


def test_an_item_with_a_date_needs_that_date_stated_and_another_date_does_not_cover_it():
    text = "שיעור היוון ליום 31.12.2023 – 7.5%"
    ws = _ws(text)
    item = _item(label="שיעור ההיוון", quote=text, value="7.5%")
    assert _covered(ws, "שיעור ההיוון הוא 7.5% נכון ל-31/12/2023 [S1].", item).ok  # another spelling of the same date
    other = _covered(ws, "שיעור ההיוון הוא 7.5% נכון ל-30.06.2024 [S1].", item)
    assert not other.ok and other.why == "date_not_stated"
    assert not _covered(ws, "שיעור ההיוון הוא 7.5% [S1].", item).ok


def test_the_date_of_an_item_may_be_stated_in_its_label_or_its_condition():
    ws = _ws("שיעור היוון | 7.5%\nליום 31.12.2023")
    item = _item(label="שיעור ההיוון", quote="שיעור היוון | 7.5%", value="7.5%", condition="למועד 31.12.2023")
    assert not _covered(ws, "שיעור ההיוון הוא 7.5% [S1].", item).ok
    assert _covered(ws, "שיעור ההיוון ליום 31.12.2023 הוא 7.5% [S1].", item).ok


def test_the_value_of_another_item_in_the_same_unit_does_not_cover_this_one():
    ws = _ws(ADJUSTMENTS)
    got = _covered(ws, "ההתאמה למיקום היא 5% וההתאמה לקומה היא 3% [S1].")
    assert not got.ok


def test_coverage_names_the_covering_unit_among_several():
    ws = _ws(ADJUSTMENTS)
    got = _covered(ws, "פתיח.\n\nההתאמה לקומה היא 3% [S1].\n\nההתאמה לגיל הבניין היא 2% [S1].")
    assert got.ok and got.unit == 2


# --- the turn-level ledger across answer versions (KTD2) -----------------------------------------------------------------------

def test_a_judge_support_item_omitted_in_version_one_and_not_listed_in_version_two_stays_open():
    ws = _ws(ADJUSTMENTS)
    ledger = items.Ledger()
    agent = _raw(label="ההתאמה לקומה", quote="קומה | 3%", value="3%")
    judged = items.support_item("N1", "S1", "גיל הבניין | 2%", ledger)
    v1 = ledger.merge(_validate(ws, "ההתאמה לקומה היא 3% [S1].", agent, ledger=ledger).kept + [judged])
    assert {i.origin for i in v1} == {"agent", "judge_support"} and judged.state == items.OMITTED
    # version two: the agent lists only its own item again
    markdown = "ההתאמה לקומה היא 3% [S1]."
    v2 = ledger.merge(_validate(ws, markdown, agent, ledger=ledger).kept)
    assert len(v2) == 2 and len(ledger.items) == 2
    open_items = _open(ledger, markdown, ws)
    assert [i.origin for i in open_items] == ["judge_support"] and open_items[0].quote == "גיל הבניין | 2%"


def test_an_item_leaves_the_open_set_when_a_surviving_unit_covers_it():
    ws = _ws(ADJUSTMENTS)
    ledger = items.Ledger()
    ledger.merge([items.support_item("N1", "S1", "גיל הבניין | 2%", ledger, label="ההתאמה לגיל הבניין", value="2%")])
    assert len(_open(ledger, "ההתאמה לקומה היא 3% [S1].", ws)) == 1
    assert _open(ledger, "ההתאמה לגיל הבניין היא 2% [S1].", ws) == []


def test_an_item_leaves_the_open_set_when_a_later_version_states_it_not_verifiable_with_a_reason():
    ws = _ws(ADJUSTMENTS)
    ledger = items.Ledger()
    judged = items.support_item("N1", "S1", "גיל הבניין | 2%", ledger)
    ledger.merge([judged])
    markdown = "ההתאמה לקומה היא 3% [S1]."
    assert len(_open(ledger, markdown, ws)) == 1
    later = _validate(ws, markdown, _raw(state="not_verifiable", reason="המקור נקרא בקריאה לא ודאית"), ledger=ledger)
    ledger.merge(later.kept)
    assert len(ledger.items) == 1  # the same key (component, source, quote): one item, its state updated
    assert _open(ledger, markdown, ws) == []
    assert [o["state"] for o in ledger.outcomes(_units(markdown), ws)] == ["not_verifiable"]


def test_a_later_version_that_lists_the_item_without_a_reason_does_not_close_it():
    ws = _ws(ADJUSTMENTS)
    ledger = items.Ledger()
    ledger.merge([items.support_item("N1", "S1", "גיל הבניין | 2%", ledger)])
    markdown = "ההתאמה לקומה היא 3% [S1]."
    bad = _validate(ws, markdown, _raw(state="not_applicable", reason=""), ledger=ledger)
    ledger.merge(bad.kept)
    assert len(_open(ledger, markdown, ws)) == 1
    assert [e["reason"] for e in bad.excluded] == ["no_reason"]


def test_an_included_item_no_surviving_unit_covers_is_reported_as_omitted_and_a_covered_one_as_covered():
    ws = _ws(ADJUSTMENTS)
    ledger = items.Ledger()
    kept = _validate(ws, "ההתאמה לקומה היא 3% [S1].", _raw(), _raw(label="ההתאמה לקומה", quote="קומה | 3%", value="3%"),
                     ledger=ledger).kept
    ledger.merge(kept)
    out = {o["quote"]: o for o in ledger.outcomes(_units("ההתאמה לקומה היא 3% [S1]."), ws)}
    assert out["גיל הבניין | 2%"]["state"] == items.OMITTED and out["גיל הבניין | 2%"]["covered_by"] is None
    assert out["קומה | 3%"]["state"] == "covered" and out["קומה | 3%"]["covered_by"] == 0


def test_the_ledger_records_what_was_excluded():
    ws = _ws(ADJUSTMENTS)
    ledger = items.Ledger()
    res = _validate(ws, "ההתאמה לגיל הבניין היא 2% [S1].", _raw(), _raw(source="S9"), ledger=ledger)
    ledger.merge(res.kept, excluded=res.excluded, version=2)
    assert [i.id for i in ledger.items.values()] == ["I1"]
    assert ledger.excluded == [{"version": 2, "component": "N1", "source": "S9", "quote": "גיל הבניין | 2%",
                                "state": "included", "reason": "not_returned_source"}]


def test_an_empty_ledger_for_a_single_fact_component_changes_nothing():
    ws = _ws(ADJUSTMENTS)
    ledger = items.Ledger()
    assert items.validate([], ws, _units("השווי הוא 9,500 ₪ [S1]."), COMPONENTS, ledger=ledger).kept == []
    assert ledger.merge([]) == [] and _open(ledger, "השווי הוא 9,500 ₪ [S1].", ws) == []
    assert ledger.outcomes(_units("השווי הוא 9,500 ₪ [S1]."), ws) == []


# --- the schema and the policy -----------------------------------------------------------------------------------------------

def test_the_final_schema_lists_items_before_the_answer_and_every_item_field_is_required():
    props = list(FINAL_SCHEMA["properties"])
    assert props.index("items") < props.index("answer_markdown")
    assert "items" in FINAL_SCHEMA["required"]
    node = FINAL_SCHEMA["$defs"]["LedgerItem"]
    assert node["additionalProperties"] is False
    assert set(node["required"]) == set(node["properties"]) == {"component", "label", "source", "quote", "value", "condition",
                                                                  "state", "reason"}
    assert set(node["properties"]["state"]["enum"]) == {"included", "not_verifiable", "not_applicable"}


def test_a_final_answer_without_items_is_refused_by_the_strict_model():
    import pytest
    from pydantic import ValidationError

    body = {"status": "answered", "answer_markdown": "x", "claims": [], "clarification_question": "", "missing_info": "",
            "referenced_document_ids": [], "scope_kind": "focused", "scope_query": "", "omitted": [], "focus": None,
            "requested": [], "parts": []}
    with pytest.raises(ValidationError):
        FinalAnswer.model_validate(body)
    assert FinalAnswer.model_validate(body | {"items": []}).items == []
    item = {"component": "N1", "label": "l", "source": "S1", "quote": "q", "value": "", "condition": "",
            "state": "included", "reason": ""}
    assert FinalAnswer.model_validate(body | {"items": [item]}).items == [LedgerItem(**item)]
    with pytest.raises(ValidationError):
        FinalAnswer.model_validate(body | {"items": [{k: v for k, v in item.items() if k != "reason"}]})


def test_the_policy_describes_the_ledger_and_no_longer_says_one_search_is_enough():
    assert "חיפוש אחד ממוקד בדרך כלל מספיק" not in POLICY
    for word in ("items", "included", "not_verifiable", "not_applicable"):
        assert word in POLICY


# --- round 18 U5: the condition of an item, its span, its origin and its outcome (KTD1; R1-R4; AE1-AE4, AE8) ---------------------

PLAN_ROWS = ["סוג שטח | שטח (מ״ר) | הערה", "שטח כולל לבנייה | 1,200 | הזכויות חלות רק על פי תכנית מאושרת", "שטח למרתף | 340 | ללא הערה"]
PLAN_HEAD = [f"{TABLE_PATH_PREFIX} 3. זכויות הבנייה", "טבלה 3.1 — זכויות הבנייה בנכס", "הטבלה: 2 שורות"]
PLAN_TABLE = "\n".join([*PLAN_HEAD, *PLAN_ROWS])
PLAN_QUOTE = "שטח כולל לבנייה | 1,200"
PLAN_NOTE = "הזכויות חלות רק על פי תכנית מאושרת"
PLAN_BARE = "השטח הכולל לבנייה בנכס הוא 1,200 מ״ר [S1]."
PLAN_ASK = [{"id": "N1", "text": "השטח הכולל לבנייה בנכס"}]


def _spans(ws: Workspace, quote: str, source: str = "S1") -> list[tuple[str, str]]:
    return [(s.place, s.text) for s in items.condition_spans(ws.sources[source], quote)]


def test_a_row_note_is_the_condition_span_of_its_item_and_a_no_note_placeholder_is_none():
    ws = _ws(PLAN_TABLE)
    assert _spans(ws, PLAN_QUOTE) == [("note", PLAN_NOTE)]
    assert _spans(ws, "שטח למרתף | 340") == []  # the cell holds the header's own word with a negation: no condition


def test_an_operator_clause_of_the_quote_or_its_sentence_is_a_span_and_a_plain_total_is_not():
    ws = _ws("הזכויות במגרש הן 800 מ״ר, למעט שטחי השירות. השטח הכולל לבנייה הוא 900 מ״ר.")
    assert _spans(ws, "הזכויות במגרש הן 800 מ״ר") == [("row", "הזכויות במגרש הן 800 מ״ר, למעט שטחי השירות")]
    assert _spans(ws, "השטח הכולל לבנייה הוא 900 מ״ר") == []


def test_ae8_a_heading_that_names_a_method_is_a_span_of_the_values_under_it_and_a_plain_heading_is_not():
    method = _ws("\n".join([f"{TABLE_PATH_PREFIX} 4. גישת היוון ההכנסות", "טבלה 4.1 — תוצאות החישוב", "רכיב | סכום (₪)",
                            "הכנסה נטו שנתית | 610,000", "שווי מהוון | 9,384,615"]))
    assert _spans(method, "שווי מהוון | 9,384,615") == [("heading", "4. גישת היוון ההכנסות")]
    assert _spans(_ws(PLAN_TABLE.replace("שטח כולל לבנייה | 1,200 | " + PLAN_NOTE, "שטח כולל לבנייה | 1,200 | ")), PLAN_QUOTE) == []


def test_a_paragraph_attributed_to_the_table_is_a_span_and_one_that_is_not_attributed_is_not():
    from app.chat.tools import TablePart

    ws = _ws("\n".join([*PLAN_HEAD, "סוג שטח | שטח (מ״ר)", "שטח כולל לבנייה | 1,200"]))
    note = "הטבלה שלעיל מתייחסת לשטח מעל הקרקע בלבד"
    ws.sources["S1"].table_parts = [TablePart("after", "paragraph", 47, note, False, 1, 0, 0, "back")]
    assert _spans(ws, PLAN_QUOTE) == [("paragraph", note)]
    ws.sources["S1"].table_parts = [TablePart("after", "paragraph", 47, note, False, 1, 3, 0, "back")]  # blocks stand between
    assert _spans(ws, PLAN_QUOTE) == []


def test_an_agent_condition_found_only_in_another_items_context_is_recorded_as_misattributed_and_the_item_stays():
    text = "\n".join(["סוג | שיעור | הערה", "מגורים | 4% | חל עד שנה מהמסירה", "מסחר | 6% | חל על כל תקופת החוזה"])
    ws = _ws(text)
    res = _validate(ws, "השיעור למסחר הוא 6% [S1].", _raw(label="שיעור למסחר", quote="מסחר | 6%", value="6%",
                                                           condition="חל עד שנה מהמסירה"))
    (kept,) = res.kept
    assert kept.condition == "" and kept.quote == "מסחר | 6%"  # checked against its own context: the foreign words are gone
    assert [(m["quote"], m["condition"]) for m in res.misattributed] == [("מסחר | 6%", "חל עד שנה מהמסירה")]
    assert res.excluded == []  # never silent, and never a reason to drop the item


def test_an_agent_condition_that_stands_in_the_items_own_context_or_in_none_is_kept_as_written():
    ws = _ws(PLAN_TABLE)
    own = _validate(ws, PLAN_BARE, _raw(label="שטח כולל", quote=PLAN_QUOTE, value="1,200", condition=PLAN_NOTE),
                    components=PLAN_ASK)
    assert own.kept[0].condition == PLAN_NOTE and own.misattributed == []
    paraphrase = _validate(ws, PLAN_BARE, _raw(label="שטח כולל", quote=PLAN_QUOTE, value="1,200", condition="אושרה תוכנית בניין עיר"),
                           components=PLAN_ASK)
    assert paraphrase.kept[0].condition == "אושרה תוכנית בניין עיר" and paraphrase.misattributed == []


def test_an_empty_agent_ledger_with_a_cited_row_that_carries_a_condition_gets_an_origin_source_item():
    ws = _ws(PLAN_TABLE)
    ledger = items.Ledger()
    built = items.source_items(_units(PLAN_BARE), ws, PLAN_ASK, ledger)
    (item,) = built
    assert (item.origin, item.component, item.source, item.state) == ("source", "N1", "S1", "included")
    assert item.quote == "שטח כולל לבנייה | 1,200 | " + PLAN_NOTE and item.label == "שטח כולל לבנייה"
    assert item.value == "1,200" and item.condition == PLAN_NOTE and item.id == "I1"


def test_a_cited_row_with_no_material_condition_creates_no_item_and_neither_does_a_row_the_unit_does_not_state():
    ws = _ws(ADJUSTMENTS)
    assert items.source_items(_units("ההתאמה לגיל הבניין היא 2% [S1]."), ws, COMPONENTS, items.Ledger()) == []
    plan = _ws(PLAN_TABLE)
    assert items.source_items(_units("שטח המרתף הוא 340 מ״ר [S1]."), plan, PLAN_ASK, items.Ledger()) == []  # its note is a placeholder
    assert items.source_items(_units("שטח של 500 מ״ר [S1]."), plan, PLAN_ASK, items.Ledger()) == []


def test_a_row_an_agent_item_already_covers_creates_no_source_item():
    ws = _ws(PLAN_TABLE)
    ledger = items.Ledger()
    declared = _validate(ws, PLAN_BARE, _raw(label="שטח כולל", quote=PLAN_QUOTE, value="1,200"), components=PLAN_ASK, ledger=ledger)
    ledger.merge(declared.kept)
    assert items.source_items(_units(PLAN_BARE), ws, PLAN_ASK, ledger) == []


def test_a_source_item_is_covered_by_the_sentence_that_cites_it_and_never_stands_open():
    ws = _ws(PLAN_TABLE)
    ledger = items.Ledger()
    ledger.merge(items.source_items(_units(PLAN_BARE), ws, PLAN_ASK, ledger))
    (out,) = ledger.outcomes(_units(PLAN_BARE), ws)
    assert out["state"] == "covered" and out["origin"] == "source"
    assert ledger.outcomes(_units("אין כאן מידע על השטח."), ws) == []  # the sentence that cited the row is gone: nothing to check
    # an agent that declares the same row later takes the span over: one item, the agent's (``Ledger.merge`` handles the new origin)
    declared = _validate(ws, PLAN_BARE, _raw(label="שטח כולל", quote=PLAN_QUOTE, value="1,200", reason=""), components=PLAN_ASK)
    assert [i.origin for i in ledger.merge(declared.kept, version=2)] == ["agent"]


def test_ae1_a_single_item_whose_row_says_approved_plan_only_is_undecided_when_the_answer_omits_it_and_kept_when_it_states_it():
    ws = _ws(PLAN_TABLE)
    item = _item(label="שטח כולל", quote=PLAN_QUOTE, value="1,200")
    got = _condition(ws, PLAN_BARE, item)
    assert got["state"] == "covered" and got["condition_check"] == "missing"  # covered by its value; the class and the operand are both absent
    assert got["condition_quote"] == PLAN_NOTE and got["condition_spans"] == [{"place": "note", "text": PLAN_NOTE}]
    partial = _condition(ws, "השטח הכולל לבנייה הוא 1,200 מ״ר לפי תכנית מאושרת [S1].", item)  # the operand without the class: the judge decides
    assert partial["condition_check"] == "undecided" and partial["condition_quote"] == ""
    assert _condition(ws, f"השטח הכולל לבנייה הוא 1,200 מ״ר, {PLAN_NOTE} [S1].", item)["condition_check"] == "kept"


def test_an_item_with_no_material_condition_has_no_condition_check():
    ws = _ws(ADJUSTMENTS)
    got = _condition(ws, "ההתאמה לגיל הבניין היא 2% [S1].")
    assert got["condition_check"] == "" and got["condition_spans"] == []


def test_ae3_a_row_note_with_an_exclusion_that_the_sentence_states_as_including_is_a_contradiction_hint():
    text = "\n".join(["סוג שטח | שטח (מ״ר) | הערה", "שטח עיקרי | 900 | השטח אינו כולל קומות החניה"])
    ws = _ws(text)
    item = _item(label="שטח עיקרי", quote="שטח עיקרי | 900", value="900")
    got = _condition(ws, "השטח העיקרי הוא 900 מ״ר וכולל קומות החניה [S1].", item)
    assert got["condition_check"] == "hint" and got["condition_hint"] == ["contradicted"]
    assert _condition(ws, "השטח העיקרי הוא 900 מ״ר ואינו כולל את קומות החניה [S1].", item)["condition_check"] == "kept"


def test_ae4_a_time_limit_of_the_neighbouring_row_in_the_sentence_is_a_hint_from_another_item():
    text = "\n".join(["סוג | שכירות | הערה", "דירה קטנה | 3,000 | עד שנה מהמסירה", "דירה גדולה | 5,000 |"])
    ws = _ws(text)
    small = _item(label="שכירות דירה קטנה", quote="דירה קטנה | 3,000", value="3,000")
    large = _item(label="שכירות דירה גדולה", quote="דירה גדולה | 5,000", value="5,000")
    markdown = "השכירות לדירה גדולה היא 5,000 עד שנה מהמסירה [S1]. השכירות לדירה קטנה היא 3,000 עד שנה מהמסירה [S1]."
    ledger = items.Ledger()
    ledger.merge([small, large])
    out = {o["quote"]: o for o in ledger.outcomes(_units(markdown), ws)}
    assert out["דירה קטנה | 3,000"]["condition_check"] == "kept"
    big = out["דירה גדולה | 5,000"]
    assert big["condition_check"] == "hint" and big["condition_hint"] == ["other_item"]
    assert big["condition_foreign"] == [ledger.items[small.key].id] and big["condition_spans"] == []


def test_a_sentence_that_gives_both_values_does_not_make_either_a_hint_from_the_other():
    text = "\n".join(["סוג | שכירות | הערה", "דירה קטנה | 3,000 | עד שנה מהמסירה", "דירה גדולה | 5,000 |"])
    ws = _ws(text)
    small = _item(label="שכירות דירה קטנה", quote="דירה קטנה | 3,000", value="3,000")
    large = _item(label="שכירות דירה גדולה", quote="דירה גדולה | 5,000", value="5,000")
    ledger = items.Ledger()
    ledger.merge([small, large])
    out = ledger.outcomes(_units("השכירות היא 3,000 עד שנה מהמסירה ו-5,000 לדירה גדולה [S1]."), ws)
    assert {o["condition_check"] for o in out} <= {"", "kept"}


# --- round 18 U6: candidate items from the structure of the sources read (KTD2; R5-R7; AE5, AE6) ------------------------------------

RATE_HEAD = ["תעריף לשימוש בחניון", "סוג שימוש | תעריף לשימוש (₪)"]
RATE_ROWS = ["כניסה בודדת | 12", "כניסה בלילה | 8", "כניסה בסוף שבוע | 15", "דמי מנוי שנתי | 900"]
RATE_TABLE = "\n".join([*RATE_HEAD, *RATE_ROWS])
RATE_ASK = [{"id": "N1", "text": "התעריפים לשימוש בחניון"}]
RATE_ONE = "התעריף לכניסה בודדת הוא 12 ₪ [S1]."
REASONS_LIST = "\n".join(["הנימוקים להתאמה:", "- מיקום מרכזי בעיר", "- קרבה לתחבורה ציבורית", "- מצב תחזוקה טוב", "- חזית מסחרית רחבה"])
REASONS_ASK = [{"id": "N1", "text": "הנימוקים להתאמה"}]
REASONS_THREE = "ההתאמה נובעת ממיקום מרכזי בעיר, מקרבה לתחבורה ציבורית ומצב תחזוקה טוב [S1]."


def _offer(ws: Workspace, markdown: str, components=RATE_ASK, ledger=None, request: str = ""):
    return items.candidates(_units(markdown), ws, ledger if ledger is not None else items.Ledger(), components, request)


def test_ae5_a_cited_row_of_a_rate_table_yields_the_other_data_rows_as_candidates_with_server_built_fields():
    ws = _ws(RATE_TABLE)
    offer = _offer(ws, RATE_ONE)
    assert [(c.id, c.source, c.label, c.value) for c in offer.candidates] == [
        ("K1", "S1", "כניסה בלילה", "8"), ("K2", "S1", "כניסה בסוף שבוע", "15"), ("K3", "S1", "דמי מנוי שנתי", "900")]
    assert [c.quote for c in offer.candidates] == RATE_ROWS[1:] and offer.left_out == {} and offer.sources_left == 0
    assert "תעריף לשימוש (₪)" in offer.candidates[0].own and "תעריף לשימוש בחניון" in offer.candidates[0].own  # header and caption
    # the same candidates in the next call keep their ids
    ledger = items.Ledger()
    first = items.candidates(_units(RATE_ONE), ws, ledger, RATE_ASK, "")
    again = items.candidates(_units(RATE_ONE), ws, ledger, RATE_ASK, "")
    assert [c.id for c in again.candidates] == [c.id for c in first.candidates]


def test_a_candidate_the_answer_covers_or_the_ledger_holds_is_not_offered_and_neither_is_a_header():
    ws = _ws(RATE_TABLE)
    both = _offer(ws, "התעריף לכניסה בודדת הוא 12 ₪ והתעריף לכניסה בלילה הוא 8 ₪ [S1].")
    assert [c.label for c in both.candidates] == ["כניסה בסוף שבוע", "דמי מנוי שנתי"]
    ledger = items.Ledger()
    ledger.merge([_item(label="כניסה בסוף שבוע", quote="כניסה בסוף שבוע | 15", value="15")])
    held = _offer(ws, RATE_ONE, ledger=ledger)
    assert [c.label for c in held.candidates] == ["כניסה בלילה", "דמי מנוי שנתי"]


def test_a_turn_with_no_agent_ledger_still_gets_candidates_from_a_cited_table_and_a_ledger_item_alone_anchors_its_table():
    ws = _ws(RATE_TABLE)
    assert len(_offer(ws, RATE_ONE).candidates) == 3  # no ledger item: the cited table anchors
    ws = _ws(RATE_TABLE, "דוח\nפריט | ערך\nשטח | 40", same_file=True)
    ledger = items.Ledger()
    ledger.merge([_item(label="כניסה בודדת", quote="כניסה בודדת | 12", value="12")])
    anchored = _offer(ws, "השטח הוא 40 [S2].", ledger=ledger)  # the answer cites another source of the file: the ledger item anchors S1
    assert [c.label for c in anchored.candidates if c.source == "S1"] == ["כניסה בלילה", "כניסה בסוף שבוע", "דמי מנוי שנתי"]


def test_a_source_the_answer_never_cites_and_no_ledger_item_names_anchors_nothing():
    ws = _ws(RATE_TABLE, "טבלה אחרת\nשם | ערך\nפריט | 5")
    assert {c.source for c in _offer(ws, RATE_ONE).candidates} == {"S1"}


def test_a_four_member_list_where_the_answer_gives_three_yields_the_fourth_as_a_candidate():
    ws = _ws(REASONS_LIST, kind="context")
    offer = _offer(ws, REASONS_THREE, REASONS_ASK)
    (c,) = offer.candidates
    assert (c.source, c.label, c.value, c.quote) == ("S1", "", "חזית מסחרית רחבה", "- חזית מסחרית רחבה")
    assert "הנימוקים להתאמה" in c.own  # the list's lead-in belongs to its own text


def test_a_list_none_of_whose_members_the_answer_touches_is_no_anchor_and_a_sentence_run_needs_a_covered_sentence():
    ws = _ws(REASONS_LIST, kind="context")
    assert _offer(ws, "השווי נקבע לפי השוואה [S1].", REASONS_ASK).candidates == []
    prose = _ws("שיעור ההיוון למשרדים הוא 6.5%. שיעור ההיוון למסחר הוא 7.0%. שיעור ההיוון לחניה הוא 8.25%.", kind="context")
    ask = [{"id": "N1", "text": "שיעורי ההיוון"}]
    (c,) = _offer(prose, "שיעור ההיוון למשרדים הוא 6.5% ושיעור ההיוון למסחר הוא 7.0% [S1].", ask).candidates
    assert (c.label, c.value, c.quote) == ("שיעור ההיוון לחניה הוא 8.25%", "8.25%", "שיעור ההיוון לחניה הוא 8.25%.")
    assert _offer(prose, "השווי נקבע לפי השוואה [S1].", ask).candidates == []


def test_a_note_that_limits_a_value_is_its_condition_span_and_never_a_candidate_while_a_note_with_a_value_of_its_own_is_one():
    text = "\n".join([*RATE_HEAD, *RATE_ROWS[:2], "הערה: התעריפים חלים רק בימי חול", "תעריף לילד: 5 ₪"])
    ws = _ws(text)
    offer = _offer(ws, RATE_ONE)
    assert [c.label for c in offer.candidates][0] == "כניסה בלילה"
    assert not any("בימי חול" in c.quote for c in offer.candidates)
    assert [c.quote for c in offer.candidates if "5" in c.value] == ["תעריף לילד: 5 ₪"]
    assert [(s.place, s.text) for s in items.condition_spans(ws.sources["S1"], "כניסה בודדת | 12")] == [
        ("paragraph", "הערה: התעריפים חלים רק בימי חול")]  # the limiting note is the value's condition
    night = next(c for c in offer.candidates if c.label == "כניסה בלילה")
    assert night.condition == "הערה: התעריפים חלים רק בימי חול"


def test_candidates_are_capped_per_anchor_source_and_the_count_left_out_is_recorded(monkeypatch):
    monkeypatch.setattr(items, "CANDIDATES_PER_SOURCE", 2)
    ws = _ws("\n".join([*RATE_HEAD, *RATE_ROWS, "כניסה בחג | 20"]))
    offer = _offer(ws, RATE_ONE)
    assert [c.label for c in offer.candidates] == ["כניסה בלילה", "כניסה בסוף שבוע"] and offer.left_out == {"S1": 2}


def test_the_default_cap_is_twenty_candidates_per_source_and_a_few_sources():
    assert items.CANDIDATES_PER_SOURCE == 20 and 1 <= items.CANDIDATE_SOURCES <= 6


def test_a_candidate_of_another_appraisal_unit_of_the_same_file_is_not_offered():
    ws = _ws(RATE_TABLE, RATE_TABLE, same_file=True)
    _in_units(ws, 1, 2)
    ledger = items.Ledger()
    ledger.merge([_item(source="S2", label="כניסה בודדת", quote="כניסה בודדת | 12", value="12")])  # a ledger item of unit 2
    offer = _offer(ws, RATE_ONE, ledger=ledger)  # the answer cites only unit 1's table
    assert {c.source for c in offer.candidates} == {"S1"}


def test_a_row_that_shares_no_word_with_the_request_or_its_components_is_not_offered():
    ws = _ws(RATE_TABLE)
    assert _offer(ws, RATE_ONE, [{"id": "N1", "text": "שטח הבניין"}], request="מה שטח הבניין?").candidates == []
    assert len(_offer(ws, RATE_ONE, [], request="מהם התעריפים לשימוש בחניון?").candidates) == 3  # the request's words count too


def test_a_candidate_becomes_an_origin_source_item_built_from_the_source_and_stays_open_until_a_sentence_gives_it():
    ws = _ws(RATE_TABLE)
    ledger = items.Ledger()
    offer = items.candidates(_units(RATE_ONE), ws, ledger, RATE_ASK, "")
    night = next(c for c in offer.candidates if c.label == "כניסה בלילה")
    item = items.candidate_item(night, "N1", ledger)
    assert (item.origin, item.candidate, item.state, item.component, item.label, item.value, item.quote) == (
        "source", True, "included", "N1", "כניסה בלילה", "8", "כניסה בלילה | 8")
    assert item.id and item.document_id == str(ws.sources["S1"].document_id) and item.condition == ""
    ledger.merge([item])
    (open_,) = ledger.outcomes(_units(RATE_ONE), ws)  # the sentence does not give it: a candidate stays open
    assert (open_["state"], open_["origin"], open_["candidate"]) == ("omitted", "source", True)
    (done,) = ledger.outcomes(_units(RATE_ONE + " התעריף לכניסה בלילה הוא 8 ₪ [S1]."), ws)
    assert done["state"] == "covered"


def test_an_origin_source_item_that_is_not_a_candidate_is_still_dropped_when_no_sentence_gives_it():
    ws = _ws(RATE_TABLE)
    ledger = items.Ledger()
    plain = items.Item("N1", "כניסה בלילה", "S1", "כניסה בלילה | 8", "8", origin="source")
    ledger.merge([plain])
    assert ledger.outcomes(_units("אין כאן נתון."), ws) == []


def test_an_agent_item_for_the_same_row_replaces_the_candidate_item_and_a_second_candidate_does_not_change_it():
    ws = _ws(RATE_TABLE)
    ledger = items.Ledger()
    offer = items.candidates(_units(RATE_ONE), ws, ledger, RATE_ASK, "")
    night = next(c for c in offer.candidates if c.label == "כניסה בלילה")
    ledger.merge([items.candidate_item(night, "N1", ledger)])
    ledger.merge([items.candidate_item(night, "N1", ledger)])
    assert len(ledger.items) == 1
    declared = _validate(ws, RATE_ONE, _raw(label="כניסה בלילה", quote="כניסה בלילה | 8", value="8", state="not_applicable",
                                            reason="הבקשה היא לשעות היום"), components=RATE_ASK, ledger=ledger)
    merged = ledger.merge(declared.kept)
    assert [(i.origin, i.candidate, i.state) for i in merged] == [("agent", False, "not_applicable")]


# --- round 18 U7: coverage compares value forms, and a date may stand in the sentence's lead-in or heading (KTD6, AE10) ------------

def test_ae10_an_item_valued_in_full_digits_is_covered_by_a_sentence_that_writes_it_with_a_scale_word_and_the_other_way_round():
    ws = _ws("שווי הנכס | 16,070,000 ₪", "דמי ניהול | 45 אלף ₪")
    full = _item(label="שווי הנכס", quote="שווי הנכס | 16,070,000 ₪", value="16,070,000 ₪")
    assert _covered(ws, "שווי הנכס הוא 16.07 מיליון ₪ [S1].", full).ok
    thousand = _item(label="דמי ניהול", source="S2", quote="דמי ניהול | 45 אלף ₪", value="45 אלף ₪")
    assert _covered(ws, "דמי הניהול הם 45,000 ₪ [S2].", thousand).ok
    assert _covered(ws, "דמי הניהול הם 45 אלף ₪ [S2].", thousand).ok
    digits = _item(label="דמי ניהול", source="S2", quote="דמי ניהול | 45 אלף ₪", value="45,000 ₪")
    assert _covered(ws, "דמי הניהול הם 45 אלף ₪ [S2].", digits).ok


def test_a_decimal_comma_number_with_a_scale_word_is_scaled_from_the_value_the_reader_read_not_from_its_digits():
    # "13,2 מיליון" is 13.2 million: the forms are scaled from the decimal the number reader produced ("13.2"), never from the raw
    # slice "13,2" with its comma dropped ("132" x a million)
    have = items._have("שווי הנכס 13,2 מיליון ₪")
    assert "13200000" in have and "13.2" in have
    assert "132000000" not in have
    assert "13200000" in items._have("הערך 13.2 מיליון ₪") and "132000000" not in items._have("הערך 13.2 מיליון ₪")
    ((_, _, forms),) = items._numbers_with_scale("שווי 13,2 מיליון ₪")
    assert {"13.2", "13200000"} <= forms and "132000000" not in forms


def test_a_different_amount_is_not_covered_by_a_scale_word_that_does_not_equal_it():
    ws = _ws("שווי הנכס | 16,070,000 ₪")
    item = _item(label="שווי הנכס", quote="שווי הנכס | 16,070,000 ₪", value="16,070,000 ₪")
    got = _covered(ws, "שווי הנכס הוא 16.7 מיליון ₪ [S1].", item)
    assert not got.ok and got.why == "value_not_stated"


DATED = "שיעור היוון ליום 31.12.2023 – 7.5%"


def test_an_items_date_stated_in_the_lead_in_sentence_of_its_list_counts():
    ws = _ws(DATED)
    item = _item(label="שיעור ההיוון", quote=DATED, value="7.5%")
    assert _covered(ws, "הנתונים נכון ליום 31.12.2023:\n\n- שיעור ההיוון הוא 7.5% [S1].", item).ok
    other = _covered(ws, "הנתונים נכון ליום 30.06.2024:\n\n- שיעור ההיוון הוא 7.5% [S1].", item)
    assert not other.ok and other.why == "date_not_stated"


def test_an_items_date_stated_in_the_sentence_before_its_table_or_in_the_heading_above_it_counts():
    ws = _ws(DATED)
    item = _item(label="שיעור ההיוון", quote=DATED, value="7.5%")
    table = "השיעורים נכון ליום 31.12.2023:\n\n| פריט | שיעור |\n| --- | --- |\n| שיעור ההיוון | 7.5% [S1] |"
    assert _covered(ws, table, item).ok
    assert _covered(ws, "## מצב ליום 31.12.2023\n\nשיעור ההיוון הוא 7.5% [S1].", item).ok
    assert not _covered(ws, "## מצב ליום 30.06.2024\n\nשיעור ההיוון הוא 7.5% [S1].", item).ok


def test_a_lead_in_that_is_not_the_sentences_own_does_not_lend_it_its_date():
    ws = _ws(DATED)
    item = _item(label="שיעור ההיוון", quote=DATED, value="7.5%")
    far = "הנתונים נכון ליום 31.12.2023:\n\n- פריט אחר.\n\nפסקה חדשה.\n\nשיעור ההיוון הוא 7.5% [S1]."
    assert not _covered(ws, far, item).ok


# --- a year in a comma-joined list is a number of its own, as in ``answering.verify._NUMBER`` (round 18 U2) -----------------------

YEARS_PASSAGE = "הדגמים 2018,2019,2021 מאושרים רק לפי התוכנית המאושרת."


def test_a_year_of_a_comma_joined_list_is_the_value_of_the_span_a_sentence_cites_and_the_item_is_covered():
    ws = _ws(YEARS_PASSAGE, kind="section")
    markdown = "הדגם הוא 2019 [S1]."
    ledger = items.Ledger()
    (built,) = items.source_items(_units(markdown), ws, COMPONENTS, ledger)
    assert built.value == "2019"  # not the whole list: "2018,2019,2021" is three years, not one number
    ledger.merge([built])
    (out,) = ledger.outcomes(_units(markdown), ws)
    assert out["state"] == items.COVERED


# --- the re-read rule is symmetric for the siblings of a held item (review fix) -----------------------------------------------------

def test_a_held_item_on_the_reread_touches_the_paragraph_of_the_cited_original_and_its_sentences_are_offered():
    from dataclasses import replace

    prose = "הנתונים מפורטים להלן. שיעור ההיוון למשרדים הוא 6.5%. שיעור ההיוון למסחר הוא 7.0%."
    ask = [{"id": "N1", "text": "שיעורי ההיוון"}]
    ws = _ws(prose, "טקסט אחר ללא קשר", kind="context")
    reread = ws.adopt(replace(ws.sources["S1"], sid="", same_as="S1"))  # S3: the same text again, pointing at S1
    assert reread.sid == "S3" and reread.same_as == "S1"
    ledger = items.Ledger()
    # the held item's quote is a line-part of the paragraph that is no value of its own (so only the paragraph rule can match it)
    ledger.merge([_item(source="S3", label="", quote="הנתונים מפורטים להלן", value="הנתונים")])
    offer = _offer(ws, "השווי נקבע לפי השוואה [S1].", ask, ledger=ledger)  # the answer cites the original S1 only
    assert [c.value for c in offer.candidates if c.source == "S1"] == ["6.5%", "7.0%"]


# --- round 19 U3: the evidence and the fills of the ledger (KTD1; R1-R3; AE1, AE2) ------------------------------------------------

ROW_ASK = [{"id": "N1", "text": "השטח הכולל לבנייה בנכס"}, {"id": "N2", "text": "התנאי שבו חלות זכויות הבנייה"}]
ROW_FIRST = "השטח הכולל לבנייה בנכס הוא 1,200 מ״ר [S1]."
ROW_BOTH = ROW_FIRST + "\nהזכויות בשטח של 1,200 מ״ר חלות רק על פי תכנית מאושרת [S1]."
SECOND_ROW = "שטח למרתף | 340 | הזכויות חלות רק עד שנה מהמסירה"
SECOND_NOTE = "הזכויות חלות רק עד שנה מהמסירה"
TWO_ROWS = "\n".join([*PLAN_HEAD, PLAN_ROWS[0], PLAN_ROWS[1], SECOND_ROW])
ROW_QUOTE = "שטח כולל לבנייה | 1,200 | " + PLAN_NOTE


def _declared(ws: Workspace, markdown: str, ledger: items.Ledger, *raw: dict, components=ROW_ASK, version: int = 1) -> list[items.Item]:
    """The agent's items of one answer version, validated and merged into ``ledger`` (as ``verify_answer`` does)."""
    res = _validate(ws, markdown, *raw, components=components, ledger=ledger)
    ledger.merge(res.kept, excluded=res.excluded, version=version, misattributed=res.misattributed)
    return res.kept


def _by_component(ledger: items.Ledger, markdown: str, ws: Workspace) -> dict[str, dict]:
    return {o["component"]: o for o in ledger.outcomes(_units(markdown), ws)}


def test_ae1_a_row_the_agent_declares_under_one_component_keeps_the_server_fill_of_the_other_and_each_has_its_own_state():
    ws = _ws(PLAN_TABLE)
    ledger = items.Ledger()
    _declared(ws, ROW_BOTH, ledger, _raw(label="שטח כולל", quote=ROW_QUOTE, value="1,200"))
    ledger.merge(items.source_items(_units(ROW_BOTH), ws, ROW_ASK, ledger), version=1)  # the second sentence gives the row to N2
    by = _by_component(ledger, ROW_BOTH, ws)
    assert sorted(by) == ["N1", "N2"] and (by["N1"]["origin"], by["N2"]["origin"]) == ("agent", "source")
    assert by["N1"]["state"] == by["N2"]["state"] == "covered"
    assert by["N1"]["id"] != by["N2"]["id"] and by["N1"]["evidence_id"] == by["N2"]["evidence_id"] != ""  # two fills, one evidence
    assert by["N1"]["fill_id"] == by["N1"]["id"]


def test_ae1_the_other_merge_order_the_server_fill_of_the_first_component_survives_the_agents_item_for_the_second():
    ws = _ws(PLAN_TABLE)
    ledger = items.Ledger()
    ledger.merge(items.source_items(_units(PLAN_BARE), ws, ROW_ASK, ledger), version=1)  # version 1: no items; the server builds N1's fill
    (built,) = ledger.items.values()
    assert built.component == "N1" and built.origin == "source"
    _declared(ws, ROW_BOTH, ledger, _raw(component="N2", label="התנאי", quote=PLAN_QUOTE, value="1,200", condition=PLAN_NOTE), version=2)
    assert sorted((i.component, i.origin) for i in ledger.items.values()) == [("N1", "source"), ("N2", "agent")]
    by = _by_component(ledger, ROW_BOTH, ws)
    assert by["N1"]["state"] == by["N2"]["state"] == "covered" and by["N1"]["id"] == built.id


def test_an_agent_fill_for_the_same_component_still_takes_the_place_of_the_server_fill_of_that_component_only():
    ws = _ws(PLAN_TABLE)
    ledger = items.Ledger()
    ledger.merge(items.source_items(_units(ROW_BOTH), ws, ROW_ASK, ledger), version=1)
    assert sorted(i.component for i in ledger.items.values()) == ["N1", "N2"]
    first = ledger.items[next(k for k, i in ledger.items.items() if i.component == "N1")]
    _declared(ws, ROW_BOTH, ledger, _raw(label="שטח כולל", quote=PLAN_QUOTE, value="1,200"), version=2)  # a cell of the row, for N1
    assert sorted((i.component, i.origin) for i in ledger.items.values()) == [("N1", "agent"), ("N2", "source")]
    (n1,) = [i for i in ledger.items.values() if i.component == "N1"]
    assert n1.id == first.id and [h["origin"] for h in n1.history] == ["source", "agent"]  # the same fill: id, place and history kept


def test_two_overlapping_items_with_different_conditions_keep_separate_condition_checks():
    ws = _ws(TWO_ROWS)
    ledger = items.Ledger()
    markdown = "השטח הכולל לבנייה הוא 1,200 מ״ר ושטח המרתף הוא 340 מ״ר [S1]."
    ledger.merge(items.source_items(_units(markdown), ws, PLAN_ASK, ledger), version=1)
    assert len(ledger.items) == 2
    both = f"{ROW_QUOTE} | {SECOND_ROW}"  # one quote of the agent that holds both rows
    _declared(ws, markdown, ledger, _raw(label="שטחים", quote=both, value="1,200"), components=PLAN_ASK, version=2)
    out = ledger.outcomes(_units(markdown), ws)
    rows = {o["quote"]: o for o in out}
    assert ROW_QUOTE in rows and SECOND_ROW in rows and len(out) == 3  # neither row's fill was deleted by the wider quote
    assert [s["text"] for s in rows[ROW_QUOTE]["condition_spans"]] == [PLAN_NOTE]
    assert [s["text"] for s in rows[SECOND_ROW]["condition_spans"]] == [SECOND_NOTE]
    assert len({o["evidence_id"] for o in out}) == 3


def test_an_agent_row_quote_and_a_server_cell_quote_of_one_component_are_one_fill_with_one_condition_check():
    ws = _ws(PLAN_TABLE)
    ledger = items.Ledger()
    ledger.merge(items.source_items(_units(PLAN_BARE), ws, PLAN_ASK, ledger), version=1)  # the server holds the whole row
    _declared(ws, PLAN_BARE, ledger, _raw(label="שטח כולל", quote=PLAN_QUOTE, value="1,200"), components=PLAN_ASK, version=2)
    (out,) = ledger.outcomes(_units(PLAN_BARE), ws)
    assert out["origin"] == "agent" and len(ledger.items) == 1
    assert out["condition_check"] == "missing" and [s["text"] for s in out["condition_spans"]] == [PLAN_NOTE]


def test_a_second_fill_of_one_evidence_carries_no_condition_check_of_its_own():
    ws = _ws(PLAN_TABLE)
    ledger = items.Ledger()
    _declared(ws, ROW_BOTH, ledger, _raw(label="שטח כולל", quote=PLAN_QUOTE, value="1,200"),
              _raw(component="N2", label="התנאי", quote=PLAN_QUOTE, value="1,200"))
    by = _by_component(ledger, ROW_BOTH, ws)
    assert by["N1"]["state"] == by["N2"]["state"] == "covered"
    assert by["N1"]["condition_check"] != "" and by["N2"]["condition_check"] == "" and by["N2"]["condition_spans"] == []


def test_a_re_registration_with_another_spelling_of_the_same_quote_stays_one_item():
    ws = _ws(AGE_TABLE)
    ledger = items.Ledger()
    markdown = "ההתאמה לגיל הבניין היא 2% [S1]."
    full = "גיל הבניין | 2% | כאשר הבניין מעל גיל 30 שנה"
    (first,) = _declared(ws, markdown, ledger, _raw(label="הגיל", quote=full), version=1)
    (second,) = _declared(ws, markdown, ledger, _raw(label="ההתאמה לגיל הבניין", quote="גיל הבניין | 2%"), version=2)
    assert len(ledger.items) == 1 and second.id == first.id
    (fill,) = ledger.items.values()
    assert fill.label == "ההתאמה לגיל הבניין" and fill.quote == full and fill.first_version == 1 and fill.last_version == 2
    assert [h["version"] for h in fill.history] == [1, 2]
    # the other way round: the longer quote of the next version is still the one fill
    (third,) = _declared(ws, markdown, ledger, _raw(quote=full), version=3)
    assert len(ledger.items) == 1 and third.id == first.id


def test_a_repair_that_cites_the_same_passage_under_another_s_number_is_the_same_evidence():
    ws = _ws(ADJUSTMENTS, ADJUSTMENTS, same_file=True)
    ledger = items.Ledger()
    markdown = "ההתאמה לגיל הבניין היא 2% [S1] [S2]."
    (first,) = _declared(ws, markdown, ledger, _raw(), components=COMPONENTS, version=1)
    (second,) = _declared(ws, markdown, ledger, _raw(source="S2"), components=COMPONENTS, version=2)
    assert second.id == first.id and len(ledger.items) == 1 and second.evidence == first.evidence
    twice = _validate(ws, markdown, _raw(), _raw(source="S2"))
    assert [e["reason"] for e in twice.excluded] == ["duplicate"]  # in one version they are one item too


def test_the_same_quote_in_two_appraisal_units_of_one_file_is_two_pieces_of_evidence():
    ws = _ws(ADJUSTMENTS, ADJUSTMENTS, same_file=True)
    _in_units(ws, 1, 2)
    ledger = items.Ledger()
    markdown = "ההתאמה לגיל הבניין היא 2% [S1] [S2]."
    kept = _declared(ws, markdown, ledger, _raw(), _raw(source="S2"), components=COMPONENTS)
    assert len(kept) == 2 and len(ledger.items) == 2
    one, two = kept
    assert one.evidence[2] == two.evidence[2] and one.evidence[1] != two.evidence[1] and one.evidence != two.evidence
    assert one.evidence_id != two.evidence_id and one.id != two.id


def test_ae2_a_decision_closed_with_a_reason_is_not_reopened_by_the_same_item_listed_included_without_one():
    ws = _ws(ADJUSTMENTS)
    ledger = items.Ledger()
    markdown = "ההתאמה לקומה היא 3% [S1]."
    closed = _raw(state="not_applicable", reason="ההתאמה אינה חלה על נכס מסוג זה")
    (first,) = _declared(ws, markdown, ledger, closed, version=1)
    _declared(ws, markdown, ledger, _raw(), version=2)  # the repair round lists it again with the default state
    (fill,) = ledger.items.values()
    assert fill.id == first.id and (fill.state, fill.reason) == ("not_applicable", "ההתאמה אינה חלה על נכס מסוג זה")
    assert [o["state"] for o in ledger.outcomes(_units(markdown), ws)] == ["not_applicable"]
    assert fill.history[-1] == {"version": 2, "state": "not_applicable", "reason": "ההתאמה אינה חלה על נכס מסוג זה",
                                "origin": "agent", "declared": "included"}  # what the agent said and what stayed, by version


def test_a_closed_decision_is_replaced_only_by_another_state_with_a_reason_of_its_own():
    ws = _ws(ADJUSTMENTS)
    ledger = items.Ledger()
    markdown = "ההתאמה לקומה היא 3% [S1]."
    _declared(ws, markdown, ledger, _raw(state="not_applicable", reason="ההתאמה אינה חלה"), version=1)
    _declared(ws, markdown, ledger, _raw(state="not_verifiable", reason="המקור נקרא בקריאה לא ודאית"), version=2)
    (fill,) = ledger.items.values()
    assert (fill.state, fill.reason) == ("not_verifiable", "המקור נקרא בקריאה לא ודאית")
    rejected = _validate(ws, markdown, _raw(state="not_applicable", reason=""), ledger=ledger)  # no reason: invalid, the decision stays
    ledger.merge(rejected.kept, excluded=rejected.excluded, version=3)
    assert fill.state == "not_verifiable" and [e["reason"] for e in ledger.excluded] == ["no_reason"] and ledger.excluded[0]["version"] == 3


def test_a_decision_is_kept_after_a_repair_that_cites_another_s_number_and_after_the_claim_is_removed():
    ws = _ws(ADJUSTMENTS, ADJUSTMENTS, same_file=True)
    ledger = items.Ledger()
    markdown = "ההתאמה לגיל הבניין היא 2% [S1] [S2]. ההתאמה לקומה היא 3% [S1]."
    _declared(ws, markdown, ledger, _raw(state="not_applicable", reason="ההתאמה אינה חלה על הנכס"),
              _raw(label="ההתאמה לקומה", quote="קומה | 3%", value="3%"), components=COMPONENTS, version=1)
    _declared(ws, markdown, ledger, _raw(source="S2"), components=COMPONENTS, version=2)  # another handle of the same passage
    removed = "אין כאן מידע על ההתאמות."  # every sentence of the answer is gone
    out = {o["quote"]: o for o in ledger.outcomes(_units(removed), ws)}
    assert len(ledger.items) == 2
    assert out["גיל הבניין | 2%"]["state"] == "not_applicable" and out["קומה | 3%"]["state"] == "omitted"  # an included item reopens; a decision stays
    back = {o["quote"]: o for o in ledger.outcomes(_units(markdown), ws)}  # a repair that restores the claim: the same fills, the same ids
    # (round 19 U4, R16: the decision is kept, and a surviving sentence that states the evidence covers it: no line is written)
    assert back["קומה | 3%"]["state"] == "covered"
    assert back["גיל הבניין | 2%"]["state"] == "covered" and back["גיל הבניין | 2%"]["declared"] == "not_applicable"
    assert back["גיל הבניין | 2%"]["id"] == out["גיל הבניין | 2%"]["id"]


def test_a_fill_is_recorded_by_version_and_a_candidate_two_components_ask_for_gets_one_fill_each():
    ws = _ws(RATE_TABLE)
    ledger = items.Ledger()
    ask = [{"id": "N1", "text": "התעריפים לשימוש בחניון"}, {"id": "N2", "text": "התעריף לכניסות בלילה"}]
    night = next(c for c in items.candidates(_units(RATE_ONE), ws, ledger, ask, "").candidates if c.label == "כניסה בלילה")
    ledger.merge([items.candidate_item(night, "N1", ledger), items.candidate_item(night, "N2", ledger)])
    ledger.merge([items.candidate_item(night, "N2", ledger)])  # a later call names it again: still one fill per component
    one, two = ledger.items.values()
    assert (one.component, two.component) == ("N1", "N2") and one.id != two.id and one.evidence_id == two.evidence_id != ""
    assert one.quote == two.quote and one.candidate and two.candidate
    out = ledger.outcomes(_units(RATE_ONE), ws)
    assert [(o["component"], o["state"]) for o in out] == [("N1", "omitted"), ("N2", "omitted")]  # each component's own open item
    assert items.Ledger().candidate_id(night_evidence := ("d", "", "x")) == items.Ledger().candidate_id(night_evidence)


def test_a_candidate_has_the_same_k_number_whichever_s_number_names_the_passage():
    ws = _ws(RATE_TABLE, RATE_TABLE, same_file=True)
    ledger = items.Ledger()
    first = items.candidates(_units(RATE_ONE), ws, ledger, RATE_ASK, "").candidates
    second = items.candidates(_units("התעריף לכניסה בודדת הוא 12 ₪ [S2]."), ws, ledger, RATE_ASK, "").candidates
    assert {c.quote: c.id for c in first if c.source == "S1"} == {c.quote: c.id for c in second if c.source == "S2"}


def test_items_shared_by_value_count_only_the_fills_alive_in_the_latest_version():
    ws = _ws("\n".join(["התאמות לנכס להשוואה", "סוג התאמה | שיעור", "מיקום | 5%", "קומה | 5%", "גיל הבניין | 2%"]))
    place = _raw(label="ההתאמה למיקום", quote="מיקום | 5%", value="5%")
    floor = _raw(label="ההתאמה לקומה", quote="קומה | 5%", value="5%")
    stated = _units("השיעור הוא 5% [S1].")
    # both 5% rows are listed again in version 2: they share a value, so each needs its own words
    both = items.Ledger()
    _declared(ws, "ההתאמה למיקום היא 5% [S1].", both, place, floor, components=COMPONENTS, version=1)
    _declared(ws, "ההתאמה למיקום היא 5% [S1].", both, place, floor, components=COMPONENTS, version=2)
    shared = {o["quote"]: o for o in both.outcomes(stated, ws)}
    assert shared["מיקום | 5%"]["state"] == "omitted" and shared["מיקום | 5%"]["why"] == "condition_not_stated"
    # version 2 lists neither: they are items of an earlier version, no twins of the ones that stand
    earlier = items.Ledger()
    _declared(ws, "ההתאמה למיקום היא 5% [S1].", earlier, place, floor, components=COMPONENTS, version=1)
    _declared(ws, "ההתאמה לגיל הבניין היא 2% [S1].", earlier, _raw(), components=COMPONENTS, version=2)
    alone = {o["quote"]: o for o in earlier.outcomes(stated, ws)}
    assert alone["מיקום | 5%"]["state"] == alone["קומה | 5%"]["state"] == "covered"


def test_a_wider_quote_of_the_same_row_is_one_fill_with_the_narrower_one_and_never_a_twin_that_makes_the_row_share_its_value():
    ws = _ws(TWO_ROWS)
    ledger = items.Ledger()
    markdown = "השטח הכולל לבנייה הוא 1,200 מ״ר [S1]."
    _declared(ws, markdown, ledger, _raw(label="שטח", quote=PLAN_QUOTE, value="1,200"),
              _raw(label="שטח כולל", quote=ROW_QUOTE, value="1,200"), components=PLAN_ASK)
    assert len(ledger.items) == 1  # one passage, one fill (the longer quote is only another spelling of it)
    two = items.Ledger()
    two.merge([_item(label="שטח", quote=PLAN_QUOTE, value="1,200"), _item(label="שטח כולל", quote=ROW_QUOTE, value="1,200",
                                                                          condition=PLAN_NOTE)])
    # round 20 U1 (KTD1): the wider quote adds the row's note as its condition, which the narrower item lacks — another datum, so two fills
    assert len(two.items) == 2
    assert {o["label"]: o["state"] for o in two.outcomes(_units(markdown), ws)} == {"שטח": "covered", "שטח כולל": "covered"}
    # (not "shared": the row's condition words are not asked of the sentence)


def test_a_narrower_covered_quote_of_another_component_does_not_carry_the_condition_check_of_the_wider_quote():
    ws = _ws(PLAN_TABLE)
    ledger = items.Ledger()
    # the agent's cell quote for N1 and the server's whole row for N2: two pieces of evidence of one row, checked once
    _declared(ws, ROW_BOTH, ledger, _raw(label="שטח כולל", quote=PLAN_QUOTE, value="1,200"))
    ledger.merge(items.source_items(_units(ROW_BOTH), ws, ROW_ASK, ledger), version=1)
    by = _by_component(ledger, ROW_BOTH, ws)
    assert by["N1"]["evidence_id"] != by["N2"]["evidence_id"] and by["N2"]["quote"] == ROW_QUOTE
    # round 20 U1 (KTD1): a narrow item of another component defers nothing, so each checks the row's condition itself; the second
    # sentence states it
    assert by["N1"]["condition_check"] == "kept" and by["N2"]["condition_check"] == "kept"


# --- round 19 U4: one coverage decision over the final answer, by block (KTD4; R13-R17; AE7) --------------------------------------
# The guard tests come first: the shapes of the lines the round-18 build wrote RIGHTLY (a figure of the item is in no block of the
# answer that cites its source) keep their line; then the shapes it wrote wrongly.

RIGHTS_ASK = [{"id": "N1", "text": "זכויות הבנייה בנכס"}]
RIGHTS = "\n".join(["זכויות הבנייה", "רכיב | שטח | קומות | תכנית", "שטח עיקרי | 1,200 | 6 | ג/4521", "שטח מרתף | 340 | 2 | ג/4521",
                    "שטח גג | 95 | 1 | ג/4521"])
MAIN = dict(label="שטח עיקרי", quote="שטח עיקרי | 1,200 | 6 | ג/4521", value="1,200 מ״ר; תכנית ג/4521")
BASEMENT = dict(label="שטח מרתף", quote="שטח מרתף | 340 | 2 | ג/4521", value="340 מ״ר; 2 קומות")
ROOF = dict(label="שטח גג", quote="שטח גג | 95 | 1 | ג/4521", value="95 מ״ר")


def _states(ws: Workspace, markdown: str, *given: items.Item, units=None) -> dict[str, dict]:
    """The outcome of each fill (by its quote's first cell) against the answer ``markdown``."""
    ledger = items.Ledger()
    ledger.merge(list(given))
    out = ledger.outcomes(_units(markdown) if units is None else units, ws)
    return {o["quote"].split(" | ")[0]: o for o in out}


def test_guard_a_row_none_of_whose_figures_a_block_of_the_answer_states_stays_omitted_with_nothing_partly_given():
    ws = _ws(RIGHTS)
    # the answer states other figures of the same table, and a number of the omitted rows only as a count of something else
    markdown = "השטח העיקרי הוא 1,200 מ״ר [S1]. הבניין בן 7 שנים, ובו 5 דיירים [S1]."
    got = _states(ws, markdown, _item(**MAIN), _item(**BASEMENT), _item(**ROOF))
    assert got["שטח עיקרי"]["state"] == "covered"
    for name in ("שטח מרתף", "שטח גג"):
        assert got[name]["state"] == "omitted" and got[name]["outcome"] == "omitted" and got[name]["missing"] == []
        assert got[name]["why"] == "value_not_stated"


def test_guard_a_candidate_the_answer_does_not_state_stays_an_open_item_of_its_component():
    ws = _ws(RATE_TABLE)
    ledger = items.Ledger()
    offer = _offer(ws, RATE_ONE, ledger=ledger)
    night = next(c for c in offer.candidates if c.label == "כניסה בלילה")
    ledger.merge([items.candidate_item(night, "N1", ledger)])
    (out,) = ledger.outcomes(_units(RATE_ONE), ws)
    assert out["state"] == "omitted" and out["outcome"] == "omitted" and out["candidate"] is True


def test_guard_a_rounded_value_is_not_given_by_a_small_number_of_another_sentence_of_the_cited_passage():
    ws = _ws("\n".join(["תוצאות החישוב", "מדד | ערך", "רווחי ניהול מהוונים | 12,658,300", "תקופת היוון | 8"]))
    item = _item(label="רווחי ניהול מהוונים", quote="רווחי ניהול מהוונים | 12,658,300", value="12.66 מיליון")
    got = _states(ws, "תקופת ההיוון היא 8 שנים [S1].", item)
    assert got["רווחי ניהול מהוונים"]["state"] == "omitted"


def test_guard_an_answer_that_holds_no_unit_leaves_every_item_of_the_ledger_omitted():
    ws = _ws(RIGHTS)
    got = _states(ws, "", _item(**MAIN), _item(**BASEMENT), units=[])
    assert {o["state"] for o in got.values()} == {"omitted"} and {o["why"] for o in got.values()} == {"not_attributed"}


def test_guard_a_figure_in_a_block_that_cites_another_document_covers_nothing_and_neither_does_a_figure_in_another_list_item():
    ws = _ws(RIGHTS, "שטח עיקרי | 1,200 | 6 | ג/4521")  # another document of the same shape
    other = _states(ws, "השטח העיקרי הוא 1,200 מ״ר [S2].", _item(**MAIN))
    assert other["שטח עיקרי"]["state"] == "omitted" and other["שטח עיקרי"]["why"] == "not_attributed"
    # a list item is a block of its own: the citation of one item does not carry to the figure of the next
    split = _states(ws, "- השטח העיקרי נקבע בתכנית [S1].\n- הוא 1,200 מ״ר.", _item(**MAIN))
    assert split["שטח עיקרי"]["state"] == "omitted"


def test_guard_two_items_of_one_value_are_still_told_apart_by_the_words_that_differ_between_them():
    ws = _ws("\n".join(["שיעורי היוון", "שימוש | שיעור", "משרדים בקומות עליונות | 7%", "חנויות בקומת קרקע | 7%"]))
    upper = _item(label="שיעור למשרדים", quote="משרדים בקומות עליונות | 7%", value="7%", condition="משרדים בקומות עליונות")
    ground = _item(label="שיעור לחנויות", quote="חנויות בקומת קרקע | 7%", value="7%", condition="חנויות בקומת קרקע")
    got = _states(ws, "- השיעור למשרדים בקומות עליונות הוא 7% [S1].", upper, ground)
    assert got["משרדים בקומות עליונות"]["state"] == "covered" and got["חנויות בקומת קרקע"]["state"] == "omitted"
    assert got["חנויות בקומת קרקע"]["why"] == "condition_not_stated"


def test_ae7_a_list_item_that_states_an_item_over_two_sentences_gives_it_though_the_value_holds_a_plan_identifier():
    ws = _ws(RIGHTS)
    markdown = "- השטח העיקרי בנכס הוא 1,200 מ״ר [S1]. הוא נקבע לפי תכנית ג/4521 [S1]."
    assert items.headline(MAIN["value"], MAIN["quote"]) == (frozenset({"1200", "1.2E+3"}),)  # the plan number is no headline value
    got = _states(ws, markdown, _item(**MAIN))
    assert got["שטח עיקרי"]["state"] == "covered" and got["שטח עיקרי"]["outcome"] == "covered"


def test_ae7_an_item_with_one_of_two_figures_stated_is_partial_and_names_the_missing_figure_as_its_source_does():
    ws = _ws(RIGHTS)
    got = _states(ws, "- שטח המרתף בנכס הוא 340 מ״ר [S1].", _item(**BASEMENT))["שטח מרתף"]
    assert got["state"] == "omitted" and got["outcome"] == "partial" and got["why"] == "value_partial"
    assert got["missing"] == ["קומות: 2"]  # the cell and the header of its column, both the source's own


def test_the_figures_of_one_item_spread_over_the_sentences_of_a_list_item_are_given_by_that_block():
    ws = _ws(RIGHTS)
    markdown = "- שטח המרתף בנכס הוא 340 מ״ר [S1]. יש בו 2 קומות."
    assert _states(ws, markdown, _item(**BASEMENT))["שטח מרתף"]["state"] == "covered"
    # the same two sentences as two paragraphs are two blocks: neither gives the whole item
    apart = _states(ws, "שטח המרתף בנכס הוא 340 מ״ר [S1].\n\nיש בו 2 קומות.", _item(**BASEMENT))["שטח מרתף"]
    assert apart["outcome"] == "partial"


def test_a_paragraph_of_several_sentences_is_one_block_whose_citations_are_the_union_of_its_sentences():
    ws = _ws(RIGHTS)
    got = _states(ws, "זכויות הבנייה בנכס מפורטות להלן. שטח המרתף הוא 340 מ״ר ובו 2 קומות. הנתון נקרא [S1].", _item(**BASEMENT))
    assert got["שטח מרתף"]["state"] == "covered"


def test_a_table_row_of_the_answer_under_its_heading_gives_the_item_and_the_heading_gives_its_date():
    ws = _ws("\n".join(["רשימת מחירים", "פריט | מחיר | תאריך", "יחידה א | 1,500 | 01.02.2020"]))
    item = _item(label="יחידה א", quote="יחידה א | 1,500 | 01.02.2020", value="1,500")
    table = "### נכון ליום 01.02.2020\n\n| פריט | מחיר |\n|---|---|\n| יחידה א | 1,500 [S1] |"
    got = _states(ws, table, item)["יחידה א"]
    assert got["state"] == "covered"
    other_date = _states(ws, table.replace("01.02.2020", "01.03.2021"), item)["יחידה א"]
    assert other_date["state"] == "omitted" and other_date["why"] == "date_not_stated"


def test_a_shared_lead_in_gives_the_date_of_every_item_of_its_list():
    ws = _ws("\n".join(["נתוני השכרה", "יחידה | דמי שכירות | מועד", "יחידה ב | 2,300 | 01.05.2021"]))
    item = _item(label="יחידה ב", quote="יחידה ב | 2,300 | 01.05.2021", value="2,300")
    markdown = "נכון ליום 01.05.2021:\n- דמי השכירות ליחידה ב הם 2,300 ₪ [S1]."
    assert _states(ws, markdown, item)["יחידה ב"]["state"] == "covered"


def test_a_calculation_result_that_shows_the_figure_of_an_item_gives_it_by_the_values_it_rests_on():
    from types import SimpleNamespace

    ws = _ws(RIGHTS)
    _value(ws, "V1", "1,200")
    ws.computations["C1"] = SimpleNamespace(leaves=["V1"])
    got = _states(ws, "- השטח הכולל, כפי שחושב, הוא 1,200 מ״ר [C1].", _item(**MAIN))["שטח עיקרי"]
    assert got["state"] == "covered"


def test_a_declared_not_applicable_item_the_answer_states_is_covered_and_keeps_its_declaration_in_the_record():
    ws = _ws(RIGHTS)
    declared = _item(**ROOF, state="not_applicable", reason="הגג אינו חלק מהשטח שנשאל עליו")
    stated = _states(ws, "- שטח הגג הוא 95 מ״ר [S1].", declared)["שטח גג"]
    assert stated["state"] == "covered" and stated["declared"] == "not_applicable" and stated["outcome"] == "covered"
    unstated = _states(ws, "- השטח העיקרי הוא 1,200 מ״ר [S1].", declared)["שטח גג"]  # the protection: not stated, the declaration stands
    assert unstated["state"] == "not_applicable" and unstated["reason"] == "הגג אינו חלק מהשטח שנשאל עליו"
    declared_partial = _item(**BASEMENT, state="not_verifiable", reason="הקריאה לא ודאית")
    assert _states(ws, "- שטח המרתף הוא 340 מ״ר [S1].", declared_partial)["שטח מרתף"]["state"] == "not_verifiable"


def test_the_first_value_of_a_sentence_is_never_an_identifier_a_footnote_mark_or_an_ordinal():
    assert items._first_value("(1) ההכנסה השנתית 9,190 ₪") == "9,190"
    assert items._first_value("2. ההכנסה השנתית 9,190 ₪") == "9,190"
    assert items._first_value("לפי תכנית בי/1/483 ההכנסה 9,190 ₪") == "9,190"
    assert items._first_value("בתא שטח 101 ההכנסה 9,190 ₪") == "9,190"
    assert items._first_value("ההיתר 2021/4455 קבע 20%") == "20%"
    assert items._first_value("השיעור 20% לפי תכנית 9190") == "20%"
    assert items._first_value("תכנית 45%") == "45%"  # a quantity after the word is a quantity
    assert items._first_value("סעיף 7 ללא מספרים נוספים") == ""


def test_the_value_cell_of_a_table_row_is_not_the_cell_under_an_identifier_header_nor_an_identifier():
    header = ["מס' חלקה", "מחיר", "גוש"]
    assert items._row_value(["חלקה א", "38081", "1,250,000"], ["חלקה", "גוש", "מחיר"]) == "1,250,000"
    assert items._row_value(["חלקה א", "ג/4521", "1,250,000"], []) == "1,250,000"
    assert items._row_value(["חלקה א", "38081"], ["חלקה", "גוש"]) == "38081"  # no other cell: the first with a digit
    assert header  # (the header words are the ones the pattern reads)


def test_a_candidate_row_is_valued_by_its_amount_and_not_by_a_serial_number_or_an_id_column():
    table = "\n".join(["עסקאות שנבדקו", "מס' | מספר גוש | מחיר", "29 | 38081 | 1,450,000", "30 | 38082 | 1,520,000"])
    ws = _ws(table)
    offer = items.candidates(_units("העסקה הראשונה בסכום 1,450,000 ₪ [S1]."), ws, items.Ledger(),
                             [{"id": "N1", "text": "עסקאות שנבדקו"}], "")
    assert [c.value for c in offer.candidates] == ["1,520,000"]


def test_a_content_word_check_ignores_an_identifier_written_with_a_slash():
    assert items._word_groups("תכנית בי/1/483") == items._word_groups("תכנית")
    assert items._enough("תכנית בי/1/483 למגרש", items._words("התכנית חלה על המגרש"))


def test_a_value_the_quote_does_not_write_is_no_headline_and_the_item_is_asked_by_its_words():
    assert items.headline("7.5% לפי עמדת המבקשת", "דחיית הבקשה בהתאם לתקנות") == ()
    ws = _ws("דחיית הבקשה בהתאם לתקנות")
    item = _item(label="דחיית הבקשה", quote="דחיית הבקשה בהתאם לתקנות", value="7.5% לפי עמדת המבקשת")
    got = _states(ws, "הבקשה נדחתה, ובהתאם לתקנות נקבע שהדחייה סופית [S1].", item, units=_units("דחיית הבקשה בהתאם לתקנות [S1]."))
    assert next(iter(got.values()))["state"] == "covered"


def test_a_sentence_made_of_server_spans_covers_nothing_and_a_server_span_inside_a_model_sentence_is_not_its_text():
    ws = _ws(RIGHTS)
    markdown = "השטח העיקרי נקרא.\n\nשטח המרתף הוא 340 מ״ר ובו 2 קומות [S1] (אומת חלקית)."
    units = _units(markdown)
    units[1].server = [units[1].text]  # the whole sentence is a server line (a gap line, an item line, a note)
    got = _states(ws, markdown, _item(**BASEMENT), units=units)["שטח מרתף"]
    assert got["state"] == "omitted" and got["why"] == "not_attributed"
    mixed = _units(markdown)
    mixed[1].server = ["(אומת חלקית)"]
    assert items.model_units(mixed)[1].text.endswith("[S1]") is False and "אומת" not in items.model_units(mixed)[1].text
    assert _states(ws, markdown, _item(**BASEMENT), units=mixed)["שטח מרתף"]["state"] == "covered"


def test_a_check_that_cannot_be_completed_is_neither_given_nor_omitted(monkeypatch):
    ws = _ws(RIGHTS)

    def broken(*args, **kwargs):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(items, "_check_block", broken)
    got = _states(ws, "השטח העיקרי הוא 1,200 מ״ר [S1].", _item(**MAIN))["שטח עיקרי"]
    assert got["state"] == "unchecked" and got["outcome"] == "unchecked" and got["why"] == "unchecked" and got["covered_by"] is None


def test_an_item_whose_source_the_workspace_does_not_hold_is_unchecked_and_never_omitted():
    ws = _ws(RIGHTS)
    item = _item(**MAIN)
    ledger = items.Ledger()
    ledger.merge([item])
    del ws.sources["S1"]
    (out,) = ledger.outcomes(_units("השטח העיקרי הוא 1,200 מ״ר [S1]."), ws)
    assert out["state"] == "unchecked"


def test_blocks_are_the_lines_the_list_items_the_paragraphs_and_the_table_rows_of_the_answer():
    markdown = "\n".join(["## כותרת", "הנכס פנוי. הנכס מושכר.", "המשך הפסקה.", "", "- פריט ראשון. עוד משפט.", "- פריט שני.", "",
                          "| ח | ט |", "|---|---|", "| 1 | 2 |", "| 3 | 4 |", "", "פסקה נוספת."])
    shape = [[u.text for u in b.parts] for b in items.blocks_of(_units(markdown))]
    assert shape == [["## כותרת"], ["הנכס פנוי.", "הנכס מושכר.", "המשך הפסקה."], ["- פריט ראשון.", "עוד משפט."], ["- פריט שני."], ["| ח | ט |"],
                     ["| 1 | 2 |"], ["| 3 | 4 |"], ["פסקה נוספת."]]


def test_the_presence_test_of_a_quote_reads_a_block_and_ignores_the_footnote_mark_of_the_quote():
    from app.chat.verify import _in_answer

    units = _units("- שיעור ההיוון למשרדים הוא 6.5% [S1]. הוא חל על כל המבנים.")
    assert _in_answer("שיעור ההיוון למשרדים (43) 6.5%", units)  # the footnote mark is no value the block must state
    assert not _in_answer("שיעור ההיוון למסחר 7.0%", units)
    both = _units("- שיעור ההיוון למשרדים הוא 6.5% [S1]. ולמסחר הוא 7.0% [S1].")
    assert _in_answer("שיעור ההיוון למשרדים 6.5% ולמסחר 7.0%", both)


def test_a_value_that_holds_a_coefficient_the_quote_does_not_write_is_given_by_the_figure_the_sentence_states():
    ws = _ws("\n".join(["שיעורי התאמה", "התאמה | שיעור", "התאמה לגודל | 20%"]))
    item = _item(label="התאמה לגודל", quote="התאמה לגודל | 20%", value="20%; מקדם 0.8")
    assert items.headline(item.value, item.quote) == (frozenset({"20", "2E+1"}),)  # 0.8 is the agent's own number
    assert _states(ws, "ההתאמה לגודל היא 20% [S1].", item)["התאמה לגודל"]["state"] == "covered"


def test_a_sentence_candidate_is_valued_by_its_amount_and_not_by_a_footnote_mark_or_a_plan_number():
    assert items._named("(1) ההכנסה השנתית מהשכרה 9,190 ₪")[1] == "9,190"
    assert items._named("תכנית 9190 קובעת שיעור 6.5%")[1] == "6.5%"
    assert items._named("1. הנימוק הראשון ללא מספרים")[1] == "הנימוק הראשון ללא מספרים"  # an ordinal is no value: the member is its own value


# --- twins told apart by their date (round 19 U4, KTD4; R13-R16) ------------------------------------------------------------------

_PAY = {
    "8.24": "סך של 12.5% מסך התמורה ישולמו ביום 21.8.2024.",
    "2.24": "סך של 12.5% מסך התמורה ישולמו ביום 21.2.2024.",
    "8.23": "סך של 12.5% מסך התמורה ישולמו ביום 21.8.2023.",
    "2.23": "סך של 12.5% מסך התמורה ישולמו ביום 21.2.2023.",
    "2.25": "סך של 10% מסך התמורה ישולמו 21.2.2025 .",
    "hand": "סך של 10% מסך התמורה ישולמו 7 ימים לפני מסירת החזקה בדירה .",
}
_PAY_ANSWER = ("**תשלומי התמורה**, לפי המועדים הידועים: 10% ביום 21.2.2025; 12.5% ביום 21.8.2024; 12.5% ביום 21.2.2024; "
               "12.5% ביום 21.8.2023; ו־12.5% ביום 21.2.2023 [S1]. בנוסף, 10% ישולמו 7 ימים לפני מסירת החזקה [S1].")


def _payment_states(markdown: str) -> dict[str, dict]:
    ws = _ws("\n".join(_PAY.values()), kind="paragraph")

    def pay(date: str, key: str, pct: str = "12.5%") -> items.Item:
        return _item(label=f"תשלום ביום {date}", quote=_PAY[key], value=f"{pct} מהתמורה", condition=f"ביום {date}")

    ledger = items.Ledger()
    ledger.merge([pay("21.8.2024", "8.24"), pay("21.2.2024", "2.24"), pay("21.8.2023", "8.23"), pay("21.2.2023", "2.23"),
                  pay("21.2.2025", "2.25", "10%"),
                  _item(label="תשלום של 10% לפני מסירת החזקה", quote=_PAY["hand"], value="10% מהתמורה",
                        condition="7 ימים לפני מסירת החזקה בדירה")])
    return {o["label"]: o for o in ledger.outcomes(_units(markdown), ws)}


def test_twins_with_one_value_that_differ_only_by_their_date_are_told_apart_by_the_date_the_paragraph_states_for_each():
    states = _payment_states(_PAY_ANSWER)
    assert len(states) == 6
    assert {label: o["state"] for label, o in states.items()} == dict.fromkeys(states, "covered")  # no "condition_not_stated" gap line


def test_a_twin_whose_date_the_paragraph_leaves_out_is_the_only_one_omitted():
    states = _payment_states(_PAY_ANSWER.replace("12.5% ביום 21.8.2023; ", ""))
    omitted = {label for label, o in states.items() if o["state"] != "covered"}
    assert omitted == {"תשלום ביום 21.8.2023"}
    assert states["תשלום ביום 21.8.2023"]["why"] in {"date_not_stated", "value_not_stated", "not_attributed"}


def test_twins_that_share_their_date_and_differ_by_a_word_still_need_that_word():
    quotes = ["הדייר ישלם 5% ביום 1.1.2024 עבור חניה.", "הדייר ישלם 5% ביום 1.1.2024 עבור מחסן."]
    ws = _ws("\n".join(quotes), kind="paragraph")

    def state(markdown: str) -> dict[str, str]:
        ledger = items.Ledger()
        ledger.merge([_item(label="תשלום עבור חניה", quote=quotes[0], value="5%", condition="ביום 1.1.2024 עבור חניה"),
                      _item(label="תשלום עבור מחסן", quote=quotes[1], value="5%", condition="ביום 1.1.2024 עבור מחסן")])
        return {o["label"]: o["state"] for o in ledger.outcomes(_units(markdown), ws)}

    assert state("הדייר ישלם 5% ביום 1.1.2024 עבור חניה, ו־5% ביום 1.1.2024 עבור מחסן [S1].") == dict.fromkeys(
        ["תשלום עבור חניה", "תשלום עבור מחסן"], "covered")
    only_one = state("הדייר ישלם 5% ביום 1.1.2024 עבור חניה [S1].")
    assert only_one["תשלום עבור חניה"] == "covered" and only_one["תשלום עבור מחסן"] != "covered"


# --- round 20 U1: the same-datum gate of the ledger (KTD1; R1-R4; AE1-AE3) ---------------------------------------------------------
# Overlapping or nested quotes alone never make two items one: two overlapping fills are one only when they state the same datum
# (headline values, dates, position and condition words). Synthetic text and figures only.

FEES = "דמי הניהול הם 6.5% מהשכירות החל מיום 1.1.2025. דמי השיפוץ הם 7.0% מהשכירות החל מיום 1.7.2025."
FEES_A = "דמי הניהול הם 6.5% מהשכירות החל מיום 1.1.2025"
FEES_B = "דמי השיפוץ הם 7.0% מהשכירות החל מיום 1.7.2025"
FEES_ANSWER = "דמי הניהול הם 6.5% החל מיום 1.1.2025 [S1]."


def _fee(label: str, quote: str, value: str, condition: str = "", **kw) -> dict:
    return _raw(label=label, quote=quote, value=value, condition=condition, **kw)


def _fills(ledger: items.Ledger) -> dict[str, tuple[str, str]]:
    """The fills of the ledger by label: (value, state)."""
    return {i.label: (i.value, i.state) for i in ledger.items.values()}


@pytest.mark.parametrize("narrow_first", [True, False], ids=["narrow-first", "wide-first"])
def test_two_values_in_one_paragraph_declared_narrow_and_wide_in_one_batch_are_two_items_each_with_its_own_value(narrow_first):
    ws = _ws(FEES, kind="paragraph")
    ledger = items.Ledger()
    narrow, wide = _fee("דמי הניהול", FEES_A, "6.5%"), _fee("דמי השיפוץ", FEES, "7.0%")
    kept = _declared(ws, FEES_ANSWER, ledger, *((narrow, wide) if narrow_first else (wide, narrow)), components=COMPONENTS)
    assert _fills(ledger) == {"דמי הניהול": ("6.5%", "included"), "דמי השיפוץ": ("7.0%", "included")}
    assert len({i.id for i in kept}) == 2 == len({i.id for i in ledger.items.values()})  # two fills, two ids
    by_label = {i.label: i for i in ledger.items.values()}
    assert by_label["דמי הניהול"].quote == FEES_A and by_label["דמי השיפוץ"].quote == FEES  # no fill carries another datum's quote


@pytest.mark.parametrize("order", [(0, 1, 2), (2, 1, 0), (1, 2, 0), (1, 0, 2)])
def test_nested_quotes_in_the_same_batch_stay_separate_items_whatever_the_order(order):
    ws = _ws(FEES, kind="paragraph")
    ledger = items.Ledger()
    given = [_fee("דמי הניהול", FEES_A, "6.5%"), _fee("דמי השיפוץ", FEES_B, "7.0%"), _fee("שני הדמים", FEES, "6.5%; 7.0%")]
    _declared(ws, FEES_ANSWER, ledger, *(given[i] for i in order), components=COMPONENTS)
    assert _fills(ledger) == {"דמי הניהול": ("6.5%", "included"), "דמי השיפוץ": ("7.0%", "included"),
                              "שני הדמים": ("6.5%; 7.0%", "included")}


def test_an_aggregate_item_beside_its_parts_does_not_absorb_a_part():
    ws = _ws(FEES, kind="paragraph")
    for parts_first in (True, False):
        ledger = items.Ledger()
        parts = [_fee("דמי הניהול", FEES_A, "6.5%"), _fee("דמי השיפוץ", FEES_B, "7.0%")]
        total = _fee("סך הדמים", FEES, "6.5%; 7.0%")
        _declared(ws, FEES_ANSWER, ledger, *((*parts, total) if parts_first else (total, *parts)), components=COMPONENTS)
        assert len(ledger.items) == 3, (parts_first, _fills(ledger))


def test_several_payments_in_one_quote_declared_separately_are_one_item_each_with_its_own_date():
    ws = _ws(FEES, kind="paragraph")
    ledger = items.Ledger()
    kept = _declared(ws, FEES_ANSWER, ledger, _fee("דמי הניהול", FEES, "6.5% מהשכירות", "החל מיום 1.1.2025"),
                     _fee("דמי השיפוץ", FEES, "7.0% מהשכירות", "החל מיום 1.7.2025"), components=COMPONENTS)
    assert len(kept) == 2  # the same quote is no duplicate when the datum differs (value and date)
    assert _fills(ledger) == {"דמי הניהול": ("6.5% מהשכירות", "included"), "דמי השיפוץ": ("7.0% מהשכירות", "included")}
    assert len({i.id for i in ledger.items.values()}) == 2 and len({i.key for i in ledger.items.values()}) == 2
    assert {i.evidence for i in ledger.items.values()} == {kept[0].evidence}  # one evidence, two fills


def test_identical_values_with_different_conditions_are_two_items_and_a_closed_decision_does_not_move_to_the_other():
    rights = "השטח העיקרי הוא 1,200 מ״ר רק על פי תכנית מאושרת, והשטח העיקרי הוא 1,200 מ״ר רק לאחר פינוי המגרש."
    approved = "השטח העיקרי הוא 1,200 מ״ר רק על פי תכנית מאושרת"
    ws = _ws(rights, kind="paragraph")
    ledger = items.Ledger()
    answer = "השטח העיקרי הוא 1,200 מ״ר [S1]."
    _declared(ws, answer, ledger, _fee("לפי תכנית מאושרת", approved, "1,200 מ״ר", "רק על פי תכנית מאושרת", state="not_applicable",
                                       reason="התכנית המאושרת אינה חלה"), components=COMPONENTS, version=1)
    _declared(ws, answer, ledger, _fee("לאחר פינוי", rights, "1,200 מ״ר", "רק לאחר פינוי המגרש"), components=COMPONENTS, version=2)
    assert sorted((i.condition, i.state) for i in ledger.items.values()) == [("רק לאחר פינוי המגרש", "included"),
                                                                              ("רק על פי תכנית מאושרת", "not_applicable")]
    # the same pair in one batch, in both orders
    for order in (1, -1):
        both = items.Ledger()
        _declared(ws, answer, both, *[_fee("לפי תכנית מאושרת", approved, "1,200 מ״ר", "רק על פי תכנית מאושרת", state="not_applicable",
                                          reason="התכנית המאושרת אינה חלה"),
                                      _fee("לאחר פינוי", rights, "1,200 מ״ר", "רק לאחר פינוי המגרש")][::order], components=COMPONENTS)
        assert sorted((i.condition, i.state) for i in both.items.values()) == [("רק לאחר פינוי המגרש", "included"),
                                                                                ("רק על פי תכנית מאושרת", "not_applicable")]


def test_a_later_version_that_rewords_the_same_datum_is_one_item_with_its_history_and_the_first_fingerprint():
    ws = _ws(FEES, kind="paragraph")
    ledger = items.Ledger()
    _declared(ws, FEES_ANSWER, ledger, _fee("דמי הניהול", FEES_A, "6.5%", "רק לשנה הראשונה"), components=COMPONENTS, version=1)
    (first,) = ledger.items.values()
    frozen = first.datum
    assert frozen is not None
    reworded = "דמי הניהול הם 6.5% מהשכירות החל מיום 1.1.2025"
    _declared(ws, FEES_ANSWER, ledger, _fee("שיעור דמי הניהול", reworded, "6.5% מהשכירות", "רק לשנה הראשונה"), components=COMPONENTS, version=2)
    (only,) = ledger.items.values()
    assert only is first and only.label == "שיעור דמי הניהול" and only.value == "6.5% מהשכירות"
    assert [h["version"] for h in only.history] == [1, 2] and only.datum is frozen  # `_declare` never recomputes the fingerprint
    assert only.superseded_by == ""


def test_a_condition_added_in_version_two_leaves_one_open_item_and_version_ones_is_superseded_without_a_gap_line():
    ws = _ws(FEES, kind="paragraph")
    ledger = items.Ledger()
    _declared(ws, FEES_ANSWER, ledger, _fee("דמי הניהול", FEES_A, "6.5%"), components=COMPONENTS, version=1)
    _declared(ws, FEES_ANSWER, ledger, _fee("דמי הניהול", FEES_A, "6.5%", "רק לשנה הראשונה"), components=COMPONENTS, version=2)
    assert len(ledger.items) == 2
    older, newer = ledger.items.values()
    assert older.superseded_by == newer.id and older.condition == "" and newer.condition == "רק לשנה הראשונה"
    assert older.id != newer.id
    # the answer states the figure: the one open (here covered) item is version two's, nothing is scored for version one's
    rows = ledger.outcomes(_units(FEES_ANSWER), ws)
    assert [(r["id"], r["state"]) for r in rows] == [(newer.id, "covered")]
    # an answer that states neither: one gap, version two's
    rows = ledger.outcomes(_units("השכירות נקבעת בחוזה [S1]."), ws)
    assert [(r["id"], r["state"]) for r in rows] == [(newer.id, "omitted")]


def test_an_item_the_latest_version_lists_with_the_same_datum_is_not_superseded_and_a_dropped_closed_decision_stays_closed():
    ws = _ws(FEES, kind="paragraph")
    ledger = items.Ledger()
    _declared(ws, FEES_ANSWER, ledger, _fee("דמי הניהול", FEES_A, "6.5%"),
              _fee("דמי השיפוץ", FEES_B, "7.0%", state="not_verifiable", reason="המקור נקרא בקריאה לא ודאית"), components=COMPONENTS, version=1)
    _declared(ws, FEES_ANSWER, ledger, _fee("דמי הניהול", FEES, "6.5%; 7.0%"), components=COMPONENTS, version=2)
    closed = [i for i in ledger.items.values() if i.state == "not_verifiable"]
    assert len(closed) == 1 and closed[0].superseded_by == ""  # a closed decision is not scored away by a later wide item


def test_the_same_value_date_and_condition_on_different_source_blocks_are_two_items():
    row = "גיל הבניין | 2% | כאשר הבניין מעל גיל 30 שנה"
    ws = _ws(row, row, same_file=True)
    ws.sources["S1"].table_index, ws.sources["S2"].table_index = 1, 2
    ledger = items.Ledger()
    kept = _declared(ws, "ההתאמה לגיל הבניין היא 2% [S1] [S2].", ledger,
                     _raw(quote="גיל הבניין | 2% | כאשר הבניין מעל גיל 30 שנה", value="2%", condition="כאשר הבניין מעל גיל 30 שנה"),
                     _raw(source="S2", quote="גיל הבניין | 2% | כאשר הבניין מעל גיל 30 שנה", value="2%",
                          condition="כאשר הבניין מעל גיל 30 שנה"), components=COMPONENTS)
    assert len(kept) == 2 and len(ledger.items) == 2 and kept[0].id != kept[1].id
    # on the same block, the same datum is one item
    one = items.Ledger()
    ws.sources["S2"].table_index = 1
    _declared(ws, "ההתאמה לגיל הבניין היא 2% [S1] [S2].", one,
              _raw(quote="גיל הבניין | 2%", value="2%"), _raw(source="S2", quote="גיל הבניין | 2%", value="2%"), components=COMPONENTS)
    assert len(one.items) == 1


def test_a_server_candidate_beside_an_agent_item_of_a_different_datum_is_kept_and_beside_the_same_datum_is_dropped():
    ws = _ws(FEES, kind="paragraph")
    ledger = items.Ledger()
    _declared(ws, FEES_ANSWER, ledger, _fee("דמי הניהול", FEES, "6.5%"), components=COMPONENTS)
    judged = items.support_item("N1", "S1", FEES_B, ledger, ws=ws)  # the judge's support: the other fee, 7.0%
    ledger.merge([judged])
    assert len(ledger.items) == 2 and judged.state == items.OMITTED  # a different datum is no echo of the agent's wide item
    other = items.Ledger()
    _declared(ws, FEES_ANSWER, other, _fee("דמי הניהול", FEES_A, "6.5%"), components=COMPONENTS)
    other.merge([items.support_item("N1", "S1", FEES_B, other, ws=ws)])  # beside the narrow item: the other fee is kept as well
    assert len(other.items) == 2
    echo = items.Ledger()
    _declared(ws, FEES_ANSWER, echo, _fee("דמי הניהול", FEES, "6.5%"), components=COMPONENTS)
    echo.merge([items.support_item("N1", "S1", FEES_A, echo, ws=ws)])  # the same figure in the same passage
    assert len(echo.items) == 1


def test_a_narrow_covered_item_of_another_component_does_not_defer_the_wide_items_condition_check():
    paragraph = "דמי הניהול הם 5% מהשכירות. השיעור חל רק על חוזה מעל 5 שנים."
    ws = _ws(paragraph, kind="paragraph")
    asked = [{"id": "N1", "text": "דמי הניהול"}, {"id": "N2", "text": "שיעור דמי הניהול"}]
    answer = "דמי הניהול הם 5% [S1]."

    def checks(narrow_component: str) -> dict[str, dict]:
        ledger = items.Ledger()
        _declared(ws, answer, ledger, _raw(label="דמי הניהול", quote=paragraph, value="5%", condition="חל רק על חוזה מעל 5 שנים"),
                  _raw(component=narrow_component, label="שיעור", quote="דמי הניהול הם 5% מהשכירות", value="5%"), components=asked)
        return {o["quote"]: o for o in ledger.outcomes(_units(answer), ws)}

    other = checks("N2")
    assert other[paragraph]["condition_check"] != "", other[paragraph]  # the wide item's condition is checked: not deferred across components
    same = checks("N1")
    assert same[paragraph]["condition_check"] != "", same[paragraph]  # nor to a narrow item of another datum (a condition the narrow lacks)


def test_protection_a_true_duplicate_is_one_item_in_a_batch_and_across_versions():
    ws = _ws(FEES, kind="paragraph")
    ledger = items.Ledger()
    res = _validate(ws, FEES_ANSWER, _fee("דמי הניהול", FEES_A, "6.5%"), _fee("דמי הניהול", FEES_A, "6.5%"), components=COMPONENTS, ledger=ledger)
    assert len(res.kept) == 1 and [e["reason"] for e in res.excluded] == ["duplicate"]
    ledger.merge(res.kept, version=1)
    _declared(ws, FEES_ANSWER, ledger, _fee("דמי הניהול", FEES_A, "6.5%"), components=COMPONENTS, version=2)
    (only,) = ledger.items.values()
    assert only.id == "I1" and only.last_version == 2 and only.superseded_by == ""


def test_a_wide_item_defers_its_condition_check_to_a_covered_narrow_item_of_the_same_component_datum_and_sentence():
    narrow_quote = "דמי הניהול הם 5% מהשכירות רק בשנה הראשונה"
    other_quote = "דמי הניהול בחוזה ארוך הם 6% מהשכירות"
    paragraph = f"{narrow_quote}. {other_quote}."
    ws = _ws(paragraph, kind="paragraph")
    answer = "דמי הניהול הם 5% מהשכירות [S1]."
    ledger = items.Ledger()
    # two narrow fills inside the wide quote: the wide item has no single twin, but it states the first narrow item's datum
    _declared(ws, answer, ledger, _raw(label="דמי הניהול", quote=narrow_quote, value="5%"),
              _raw(label="דמי הניהול בחוזה ארוך", quote=other_quote, value="6%"),
              _raw(label="דמי הניהול הכוללים", quote=paragraph, value="5%"), components=COMPONENTS)
    assert len(ledger.items) == 3
    checks = {o["label"]: o["condition_check"] for o in ledger.outcomes(_units(answer), ws)}
    assert checks["דמי הניהול"] == "missing"  # the narrow item's span ("only in the first year") is left out of the sentence
    assert checks["דמי הניהול הכוללים"] == ""  # the wide item adds no second check of the same span


def test_a_superseded_item_a_later_version_lists_again_is_scored_again():
    ws = _ws(FEES, kind="paragraph")
    ledger = items.Ledger()
    _declared(ws, FEES_ANSWER, ledger, _fee("דמי הניהול", FEES_A, "6.5%"), components=COMPONENTS, version=1)
    _declared(ws, FEES_ANSWER, ledger, _fee("דמי הניהול", FEES_A, "6.5%", "רק לשנה הראשונה"), components=COMPONENTS, version=2)
    older, newer = ledger.items.values()
    assert older.superseded_by == newer.id
    _declared(ws, "השכירות נקבעת בחוזה [S1].", ledger, _fee("דמי הניהול", FEES_A, "6.5%"), components=COMPONENTS, version=3)
    assert older.superseded_by == "" and older.last_version == 3 and older.id == "I1"
    # both are scored (omitted) now: version 2's item, not listed again, closes nothing
    assert [r["id"] for r in ledger.outcomes(_units("השכירות נקבעת בחוזה [S1]."), ws)] == ["I1", "I2"]
