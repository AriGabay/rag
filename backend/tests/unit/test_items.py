"""The item ledger (round 17 U2, KTD2; R4, R5, R7): the agent lists, under each frozen component, the members of the asked kind
that the sources it read state; the server keeps only the items that pass the round-16 support checks, decides coverage by
attribution (not by words or numbers somewhere in the answer) and keeps the turn's ledger across answer versions. Synthetic
documents only."""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.chat import calc, items
from app.chat.engine import FINAL_SCHEMA, POLICY, FinalAnswer, LedgerItem
from app.chat.tools import Workspace, unit_info
from app.chat.verify import split_units

PROV = {"unit": "source", "vat": "source"}
ADJUSTMENTS = "\n".join(["התאמות לנכס להשוואה", "סוג התאמה | שיעור", "מיקום | 5%", "קומה | 3%", "גיל הבניין | 2%"])
COMPONENTS = [{"id": "N1", "text": "ההתאמות שהשומה יישמה"}]


def _ws(*texts: str, same_file: bool = False) -> Workspace:
    """S1… each a source of its own document (or of one file, ``same_file``)."""
    ws = Workspace(ctx=None)
    document, version = uuid.uuid4(), uuid.uuid4()
    for t in texts:
        ws.add_source(document_id=document if same_file else uuid.uuid4(), version_id=version if same_file else uuid.uuid4(),
                      title="דוח בדיקה", section="התאמות", location="טבלה 1", kind="table", text=t)
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
    return items.covered(item or _item(), _units(markdown), ws, siblings=siblings)


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


def test_an_item_whose_value_no_sibling_shares_does_not_need_its_condition_words():
    ws = _ws(ADJUSTMENTS)
    item = _item(condition="כאשר הבניין מעל גיל 30 שנה")
    assert _covered(ws, "ההתאמה לגיל הבניין היא 2% [S1].", item, (item, _item(quote="קומה | 3%", value="3%"))).ok


def test_a_paraphrased_condition_with_the_right_source_value_and_date_still_covers_the_item():
    text = "\n".join(["שיעורי היוון", "שימוש | שיעור", "משרדים | 7%", "חנויות | 7%"])
    ws = _ws(text)
    offices = _item(label="שיעור למשרדים", quote="משרדים | 7%", value="7%", condition="נכס מושכר לתקופה של חמש שנים")
    shops = _item(label="שיעור לחנויות", quote="חנויות | 7%", value="7%", condition="נכס פנוי")
    got = _covered(ws, "שיעור של 7% חל על נכס שהושכר לחמש שנים [S1].", offices, (offices, shops))
    assert got.ok


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
    open_items = ledger.open_items(_units(markdown), ws)
    assert [i.origin for i in open_items] == ["judge_support"] and open_items[0].quote == "גיל הבניין | 2%"


def test_an_item_leaves_the_open_set_when_a_surviving_unit_covers_it():
    ws = _ws(ADJUSTMENTS)
    ledger = items.Ledger()
    ledger.merge([items.support_item("N1", "S1", "גיל הבניין | 2%", ledger, label="ההתאמה לגיל הבניין", value="2%")])
    assert len(ledger.open_items(_units("ההתאמה לקומה היא 3% [S1]."), ws)) == 1
    assert ledger.open_items(_units("ההתאמה לגיל הבניין היא 2% [S1]."), ws) == []


def test_an_item_leaves_the_open_set_when_a_later_version_states_it_not_verifiable_with_a_reason():
    ws = _ws(ADJUSTMENTS)
    ledger = items.Ledger()
    judged = items.support_item("N1", "S1", "גיל הבניין | 2%", ledger)
    ledger.merge([judged])
    markdown = "ההתאמה לקומה היא 3% [S1]."
    assert len(ledger.open_items(_units(markdown), ws)) == 1
    later = _validate(ws, markdown, _raw(state="not_verifiable", reason="המקור נקרא בקריאה לא ודאית"), ledger=ledger)
    ledger.merge(later.kept)
    assert len(ledger.items) == 1  # the same key (component, source, quote): one item, its state updated
    assert ledger.open_items(_units(markdown), ws) == []
    assert [o["state"] for o in ledger.outcomes(_units(markdown), ws)] == ["not_verifiable"]


def test_a_later_version_that_lists_the_item_without_a_reason_does_not_close_it():
    ws = _ws(ADJUSTMENTS)
    ledger = items.Ledger()
    ledger.merge([items.support_item("N1", "S1", "גיל הבניין | 2%", ledger)])
    markdown = "ההתאמה לקומה היא 3% [S1]."
    bad = _validate(ws, markdown, _raw(state="not_applicable", reason=""), ledger=ledger)
    ledger.merge(bad.kept)
    assert len(ledger.open_items(_units(markdown), ws)) == 1
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


def test_the_ledger_records_what_was_excluded_in_its_diagnostics():
    ws = _ws(ADJUSTMENTS)
    ledger = items.Ledger()
    res = _validate(ws, "ההתאמה לגיל הבניין היא 2% [S1].", _raw(), _raw(source="S9"), ledger=ledger)
    ledger.merge(res.kept, excluded=res.excluded, version=2)
    diag = ledger.diagnostics()
    assert [i["id"] for i in diag["items"]] == ["I1"]
    assert diag["excluded"] == [{"version": 2, "component": "N1", "source": "S9", "quote": "גיל הבניין | 2%",
                                 "state": "included", "reason": "not_returned_source"}]


def test_an_empty_ledger_for_a_single_fact_component_changes_nothing():
    ws = _ws(ADJUSTMENTS)
    ledger = items.Ledger()
    assert items.validate([], ws, _units("השווי הוא 9,500 ₪ [S1]."), COMPONENTS, ledger=ledger).kept == []
    assert ledger.merge([]) == [] and ledger.open_items(_units("השווי הוא 9,500 ₪ [S1]."), ws) == []
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
