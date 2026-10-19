"""The item ledger (round 17 U2, KTD2; R4, R5, R7): the agent lists, under each frozen component, the members of the asked kind
that the sources it read state; the server keeps only the items that pass the round-16 support checks, decides coverage by
attribution (not by words or numbers somewhere in the answer) and keeps the turn's ledger across answer versions. Synthetic
documents only."""

from __future__ import annotations

import uuid
from decimal import Decimal

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
