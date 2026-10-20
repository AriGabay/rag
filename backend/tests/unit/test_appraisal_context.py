"""Appraisal contexts inside one file (round 7 U6: KTD7; R20–R22), without the database.

The derivation reads labelled property identifiers only (a labelled block and parcel pair, an address with a street
label and a house number) from title blocks and headings; numbered headings, plan numbers, years, slash pairs and
tables never open a context, and a page break alone never does. A file with one identifier set is one context. The
calculator refuses to combine values of two contexts unless a comparison allows it; two contexts' different values
are not a source conflict. Synthetic text only: every street, block, parcel and amount is invented."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.chat import calc, contexts, coverage
from app.chat.tools import Workspace


def B(i: int, kind: str, page: int | None, text: str, top: float = 100.0, table_index: int | None = None,
      status: str = "read"):
    return SimpleNamespace(block_index=i, kind=kind, page=page, text=text, bbox=[50.0, top, 500.0, top + 12],
                           status=status, table_index=table_index)


def appraisal(first: int, page: int, street: str, block: str, parcel: str, per_sqm: str, top: float = 60.0) -> list:
    """One synthetic appraisal on one page: a title block, numbered chapters, a planning chapter naming plans, a
    comparison table and a comparison list of other parcels."""
    rows = [
        ("paragraph", f"שומת מקרקעין — רחוב {street}, כפר הדמה"),
        ("paragraph", f"כתובת הנכס: רחוב {street}, כפר הדמה\nגוש: {block} חלקה: {parcel}\nהמועד הקובע: 01/03/2025"),
        ("heading", "1. מבוא"), ("paragraph", "שומה זו נערכה בשנת 2025 לבקשת הבעלים."),
        ("heading", "2. תיאור הנכס"), ("paragraph", "הנכס הוא דירת מגורים בשטח 120 מ״ר, בקומה 3."),
        ("heading", "3. מצב תכנוני"), ("paragraph", "על המקרקעין חלות תכנית דמו/5110 (מאושרת) ותכנית דמו/5112."),
        ("heading", "4. נתוני השוואה"),
        ("paragraph", "עסקאות להשוואה: רחוב הסחלב 9, גוש 30871 חלקה 31; רחוב הלוטם 21, גוש 30875 חלקה 3."),
        ("table", "גוש/חלקה | כתובת | מחיר למ״ר\n30871/22 | רחוב הדמומית 4 | 20,900"),
        ("heading", "5. תחשיב השווי"), ("paragraph", f"השווי למ״ר שנקבע לנכס: {per_sqm} ₪."),
    ]
    return [B(first + n, kind, page, t, top + 20 * n, table_index=0 if kind == "table" else None)
            for n, (kind, t) in enumerate(rows)]


TWO = appraisal(0, 1, "הדמומית 12", "30871", "15", "21,400") + appraisal(13, 2, "הצפצפה 7", "30874", "9", "22,100")


def test_a_governing_scale_stops_at_an_appraisal_title_with_an_inherited_section_path(monkeypatch):
    from app.chat import reader, tools

    rows = [B(0, "paragraph", 1, "כתובת הנכס: רחוב הדמומית 12; גוש: 30871 חלקה: 15"),
            B(1, "heading", 1, "1. מבוא"), B(2, "heading", 1, "2. תחשיב"),
            B(3, "paragraph", 1, "ממצאי הבדיקה באלפי ₪"),
            B(4, "paragraph", 2, "כתובת הנכס: רחוב הצפצפה 7; גוש: 30874 חלקה: 9"),
            B(5, "paragraph", 2, "סך העלויות 104,610"), B(6, "heading", 2, "1. מבוא")]
    for row in rows:
        row.section_path = ["2. תחשיב"]
    cx = contexts.derive(rows, "version")
    assert cx.multi and cx.segment_at(5).first == 4
    ws = tools.Workspace(ctx=None)
    ws.contexts["version"] = cx
    monkeypatch.setattr(reader, "blocks_between", lambda *args: rows[:6])
    assert tools._governing_note(ws, None, "version", 5) == ""


def test_identifiers_are_labelled_only():
    ident = contexts.identifiers("כתובת הנכס: רחוב הצפצפה 7, כפר הדמה\nגוש: 30874 חלקה: 9")
    assert ident.addresses == {("צפצפה", "7")} and ident.parcels == {("30874", "9")}
    assert ident.label == "רחוב הצפצפה 7 · גוש 30874 חלקה 9"
    assert contexts.identifiers("ברחוב הדמומית 12").addresses == {("דמומית", "12")}
    # a slash pair, a plan number, a year, a section number, a street without a label, a measure: none of them
    for text in ("30871/22", "תכנית דמו/5110", "בשנת 2025", "4. נתוני השוואה", "הדמומית 12",
                 "רחוב הדמומית 120 מ״ר", "חלקה 15"):
        assert contexts.identifiers(text).empty, text


def test_two_appraisals_in_one_file_are_two_contexts_split_at_the_second_title_block():
    cx = contexts.derive(TWO, "v")
    assert cx.multi and cx.numbers == [1, 2]
    one, two = cx.segments
    assert (one.first, one.last, one.first_page, one.last_page) == (0, 12, 1, 1)
    assert (two.first, two.last, two.first_page) == (13, 25, 2)
    assert cx.label(1) == "רחוב הדמומית 12 · גוש 30871 חלקה 15"
    assert cx.label(2) == "רחוב הצפצפה 7 · גוש 30874 חלקה 9"
    assert two.lead == 15  # its title part comes before its first heading
    assert cx.describe(2) == "הקשר 2: רחוב הצפצפה 7 · גוש 30874 חלקה 9 (עמוד 2)"
    # a table row belongs to the context of its own position
    assert cx.at_position(1, 500.0) == 1 and cx.at_position(2, 400.0) == 2 and cx.at_position(2, 10.0) == 1
    assert cx.at_block(14) == 2 and cx.spanned(10, 20) == [1, 2]


def test_a_subject_names_a_context_with_or_without_its_label():
    cx = contexts.derive(TWO, "v")
    assert cx.named("הנכס ברחוב הצפצפה 7") == [2]
    assert cx.named("הצפצפה 7") == [2]
    assert cx.named("גוש 30871 חלקה 15") == [1] and cx.named("30874/9") == [2]
    assert cx.named("הנכס") == [] and cx.named("הצפצפה 15") == []
    assert sorted(cx.named("השוואה בין רחוב הדמומית 12 לרחוב הצפצפה 7")) == [1, 2]


def test_a_single_appraisal_with_chapters_plans_and_comparables_stays_one_context():
    cx = contexts.derive(appraisal(0, 1, "הדמומית 12", "30871", "15", "21,400"), "v")
    assert not cx.multi and len(cx.segments) == 1
    assert cx.named("רחוב הדמומית 12") == []  # one context: nothing to tell apart


def test_headings_without_identifiers_and_page_breaks_never_split():
    rows = appraisal(0, 1, "הדמומית 12", "30871", "15", "21,400")
    # the same chapters again on the next pages, numbered again, without a title block naming another property
    more = [B(20, "heading", 2, "1. מבוא"), B(21, "paragraph", 2, "המשך."), B(22, "paragraph", 3, "המשך נוסף."),
            B(23, "heading", 3, "6. סיכום")]
    cx = contexts.derive(rows + more, "v")
    assert not cx.multi


def test_a_page_top_run_naming_several_properties_is_a_list_not_a_title():
    rows = appraisal(0, 1, "הדמומית 12", "30871", "15", "21,400")
    rows += [B(20, "paragraph", 2, "רחוב הסחלב 9, גוש 30871 חלקה 31; רחוב הלוטם 21, גוש 30875 חלקה 3", top=40),
             B(21, "heading", 2, "6. סיכום")]
    assert not contexts.derive(rows, "v").multi


def test_a_restatement_with_fewer_kinds_does_not_open_a_context():
    rows = appraisal(0, 1, "הדמומית 12", "30871", "15", "21,400")
    rows += [B(20, "heading", 2, "נספח א׳ — רחוב הסחלב 9")]  # an address only, where the title has both kinds
    assert not contexts.derive(rows, "v").multi


def test_a_comparison_property_appendix_with_its_own_identifiers_is_its_own_context():
    # an appended report on a comparison property: its identifiers in its heading, and its own numbering from 1
    rows = appraisal(0, 1, "הדמומית 12", "30871", "15", "21,400")
    rows += [B(20, "heading", 2, "נספח א׳ — נכס השוואה: רחוב הסחלב 9, גוש 30871 חלקה 31"),
             B(21, "heading", 2, "1. תיאור נכס ההשוואה"), B(22, "paragraph", 2, "מחיר העסקה למ״ר: 21,800 ₪.")]
    cx = contexts.derive(rows, "v")
    assert cx.multi and cx.at_block(22) == 2 and cx.at_block(12) == 1
    # the same appendix without its own numbering is a part of the report, not another one
    unnumbered = [r for r in rows if r.block_index != 21]
    assert not contexts.derive(unnumbered, "v").multi


def test_the_main_property_after_an_appendix_is_the_first_context_again():
    rows = appraisal(0, 1, "הדמומית 12", "30871", "15", "21,400")
    rows += [B(20, "heading", 2, "נספח א׳ — רחוב הסחלב 9, גוש 30871 חלקה 31"), B(21, "heading", 2, "1. תיאור"),
             B(22, "heading", 3, "6. השלמה — רחוב הדמומית 12, גוש 30871 חלקה 15"), B(23, "paragraph", 3, "סוף.")]
    cx = contexts.derive(rows, "v")
    # the report's own numbering goes on (5, then 6): it is the first context again
    assert cx.numbers == [1, 2] and [s.number for s in cx.segments] == [1, 2, 1] and cx.at_block(23) == 1


def test_a_file_whose_title_block_names_no_property_is_one_context():
    rows = [B(0, "paragraph", 1, "שומת מקרקעין"), B(1, "heading", 1, "1. מבוא"), B(2, "paragraph", 1, "טקסט."),
            B(3, "paragraph", 2, "כתובת הנכס: רחוב הצפצפה 7\nגוש: 30874 חלקה: 9", top=30),
            B(4, "heading", 2, "1. מבוא")]
    assert not contexts.derive(rows, "v").multi


def test_tables_and_unread_blocks_are_never_read_for_identifiers():
    rows = appraisal(0, 1, "הדמומית 12", "30871", "15", "21,400")
    rows += [B(20, "table", 2, "כתובת הנכס: רחוב הצפצפה 7 גוש: 30874 חלקה: 9", top=30, table_index=1),
             B(21, "heading", 2, "6. סיכום"),
             B(22, "image", 3, "רחוב הצפצפה 7 גוש 30874 חלקה 9", top=30), B(23, "heading", 3, "7. נספח")]
    assert not contexts.derive(rows, "v").multi


# --- the calculator: values of two contexts ---------------------------------------------------------------------------

def _value(vid: str, amount: str, context: tuple[str, str] | None) -> calc.Operand:
    return calc.operand(vid, Decimal(amount), "ILS_per_sqm", kind="value_per_area", context=context)


def test_calculate_refuses_values_of_two_contexts_without_a_comparison():
    a, b = ("v#1", "רחוב הדמומית 12"), ("v#2", "רחוב הצפצפה 7")
    operands = {"V1": _value("V1", "21400", a), "V2": _value("V2", "22100", b)}
    node = calc.parse("V2 - V1")
    with pytest.raises(calc.CalcError) as refused:
        calc.evaluate(node, operands, contexts=[])
    assert "רחוב הדמומית 12" in str(refused.value) and "רחוב הצפצפה 7" in str(refused.value)
    assert "משווה" in str(refused.value)  # the reason: no component of the request compares them
    # a frozen calculation component comparing both allows it
    out = calc.evaluate(node, operands, contexts=[frozenset({"v#1", "v#2"})])
    assert out.value == Decimal("700")
    # off (None): the calculator does not look at contexts
    assert calc.evaluate(node, operands).value == Decimal("700")
    # one context, or values of no context, never need a comparison
    same = {"V1": _value("V1", "21400", a), "V2": _value("V2", "120", a)}
    assert calc.evaluate(calc.parse("V1 + V2"), same, contexts=[]).value == Decimal("21520")


def _grounded(vid: str, amount: str, number: int, document) -> calc.Value:
    """A value per area the source states (kind, unit, area basis), in appraisal ``number`` of file ``document``."""
    from uuid import uuid4

    return calc.Value(vid, Decimal(amount), amount, "S1", document, uuid4(), "r1", "t", "loc", "l", "value_per_area",
                      "ILS_per_sqm", "none", "not_applicable", "נטו", "הנכס", "appraised_property",
                      {"kind": "source", "unit": "source", "area_basis": "source"}, {}, "q",
                      context={"key": f"v#{number}", "number": number, "subject": True})


def test_two_contexts_with_different_values_are_not_a_source_conflict():
    """Round 13 U4 (R3, KTD4): the conflict check reads the timeline's datum classes, and the appraisal unit (its context
    number) is the identity of a value no property group holds — so two appraisals of one file never conflict, whatever
    ``chat_appraisal_context_enforced`` says, and two values of one appraisal do."""
    from uuid import uuid4

    doc = uuid4()
    ws = Workspace(ctx=None)
    ws.values.update({"V1": _grounded("V1", "21400", 1, doc), "V2": _grounded("V2", "22100", 2, doc)})
    assert not coverage.conflicting(ws)
    ws.values["V3"] = _grounded("V3", "22500", 2, doc)  # the same context, the same datum, another amount
    assert coverage.conflicting(ws)


# --- only a real report start opens a context: the top-level numbering restarts there (U6 follow-up) ------------------

def report(first: int, page: int, street: str, chapters: list[tuple[int, str]], top: float = 60.0) -> list:
    """A report identified by an address only, with numbered top-level chapters, one per block pair."""
    rows = [B(first, "paragraph", page, f"שומת מקרקעין — רחוב {street}", top)]
    for n, (number, title) in enumerate(chapters):
        rows += [B(first + 1 + 2 * n, "heading", page, f"{number}. {title}", top + 40 * n + 20),
                 B(first + 2 + 2 * n, "paragraph", page, f"תוכן הפרק {number}.", top + 40 * n + 30)]
    return rows


def test_a_mid_report_sub_building_heading_with_its_own_address_stays_one_context():
    rows = report(0, 1, "הדמומית 12", [(1, "מבוא"), (2, "תיאור המתחם")])
    # inside chapter 2, on the next page: a sub-building of the same complex under a heading with its own address
    rows += [B(10, "heading", 2, "מבנה ב׳ במתחם — רחוב הדמומית 14", 40), B(11, "paragraph", 2, "תיאור המבנה.", 60),
             B(12, "heading", 2, "2.1 מבנה ג׳ — רחוב הדמומית 16", 90), B(13, "paragraph", 2, "תיאור.", 110),
             B(14, "heading", 3, "3. מצב תכנוני", 40), B(15, "paragraph", 3, "טקסט.", 60)]
    assert not contexts.derive(rows, "v").multi


def test_a_page_top_bulleted_comparables_run_stays_one_context():
    rows = report(0, 1, "הדמומית 12", [(1, "מבוא"), (2, "תיאור"), (3, "נתוני השוואה")])
    # chapter 3 continues on the next page: a page-top run of comparables under a short heading, numbering continues
    rows += [B(10, "paragraph", 2, "• עסקה ברחוב הסחלב 9, מחיר 21,800 ₪ למ״ר", 40),
             B(11, "heading", 2, "עסקאות נוספות", 60),
             B(12, "paragraph", 2, "• עסקה ברחוב הלוטם 21", 80), B(13, "paragraph", 2, "• עסקה ברחוב השיטה 3", 100),
             B(14, "paragraph", 3, "עסקה ברחוב הצאלון 5, 2024", 40), B(15, "heading", 3, "השוואה", 60),
             B(16, "heading", 3, "4. תחשיב השווי", 90), B(17, "paragraph", 3, "השווי 21,400 ₪ למ״ר.", 110)]
    assert not contexts.derive(rows, "v").multi


def test_a_second_report_whose_numbering_restarts_opens_a_context():
    rows = report(0, 1, "הדמומית 12", [(1, "מבוא"), (2, "תיאור"), (3, "תחשיב")])
    rows += report(10, 2, "הצפצפה 7", [(1, "מבוא"), (2, "תיאור")], top=40)
    cx = contexts.derive(rows, "v")
    assert cx.multi and cx.at_block(10) == 2 and cx.label(2) == "רחוב הצפצפה 7"


def test_without_numbered_headings_an_identifier_run_alone_never_splits():
    rows = [B(0, "paragraph", 1, "כתובת הנכס: רחוב הדמומית 12\nגוש: 30871 חלקה: 15"), B(1, "heading", 1, "מבוא"),
            B(2, "paragraph", 1, "טקסט."),
            B(3, "paragraph", 2, "כתובת הנכס: רחוב הצפצפה 7\nגוש: 30874 חלקה: 9", 30), B(4, "heading", 2, "מבוא")]
    assert not contexts.derive(rows, "v").multi


# --- a turned page with a CropBox offset: block tops and row tops in one frame ---------------------------------------
# Stored block boxes are in pdfplumber's frame and a table's cell boxes in the frame of the rendered page; on a page
# turned by /Rotate 90 whose CropBox is offset, the two differ by a translation (here 40 points across, 150 down), so
# a segment's start is converted to the rendered frame before a row's top is compared with it.

TURNED = {"mediabox": [0, 0, 612, 792], "cropbox": [150, 40, 600, 700], "rotation": 90, "display_width": 660.0,
          "display_height": 450.0, "geometry_issue": None}
SHIFT = 150.0  # the rendered frame's top is the pdfplumber frame's top less this, on a TURNED page


def turned_two(geometry: dict | None = TURNED) -> list:
    """Two synthetic appraisals: the first on page 1 and the top of page 2, the second opening mid-page 2 with a
    heading that restates its address and parcel; ``top`` is in pdfplumber's frame, as ``document_blocks.bbox``."""
    rows = [(1, "paragraph", "שומת מקרקעין — רחוב הדמומית 12, כפר הדמה\nגוש: 30871 חלקה: 15", 200.0),
            (1, "heading", "1. מבוא", 230.0), (1, "paragraph", "שומה זו נערכה לבקשת הבעלים.", 250.0),
            (1, "heading", "2. נתוני השוואה", 270.0), (1, "table", "עסקאות להשוואה", 290.0),
            (2, "paragraph", "המשך טבלת העסקאות מהעמוד הקודם.", 170.0),
            (2, "heading", "שומת מקרקעין — רחוב הצפצפה 7, גוש 30874 חלקה 9", 260.0),
            (2, "heading", "1. מבוא", 330.0), (2, "paragraph", "שומה נוספת לנכס אחר.", 350.0),
            (2, "heading", "2. תחשיב השווי", 380.0), (2, "paragraph", "השווי למ״ר שנקבע לנכס: 22,100 ₪.", 400.0)]
    out = []
    for i, (page, kind, t, top) in enumerate(rows):
        b = B(i, kind, page, t, top, table_index=0 if kind == "table" else None)
        for k, v in (geometry or {}).items():
            setattr(b, k, v)
        out.append(b)
    return out


def test_a_table_row_on_a_turned_cropped_page_lands_in_the_context_of_its_rendered_position():
    from app.chat import reader

    cx = contexts.derive(turned_two(), "v")
    assert cx.multi and cx.numbers == [1, 2]
    second = cx.segments[1]
    assert second.first == 6 and second.start == (2, 260.0 - SHIFT)  # the heading's top on the rendered page
    # the merged comparables table: one row on page 1, three on page 2 with their cells' boxes on the rendered page —
    # above the second heading (rendered top 110), just below it, and further down
    st = {"rows": [{"page": 1, "cells": ["א"], "cell_boxes": [[40.0, 160.0, 90.0, 172.0]]},
                   {"page": 2, "cells": ["ב"], "cell_boxes": [[40.0, 60.0, 90.0, 72.0]]},
                   {"page": 2, "cells": ["ג"], "cell_boxes": [[40.0, 130.0, 90.0, 142.0]]},
                   {"page": 2, "cells": ["ד"], "cell_boxes": [[40.0, 300.0, 90.0, 312.0]]}]}
    groups = reader.table_contexts(cx, st, 4, 1)
    assert [(g["context"], g["first"], g["last"]) for g in groups] == [(1, 1, 2), (2, 3, 4)]


def test_a_page_without_usable_geometry_keeps_its_stored_block_top():
    for geometry in (None, dict(TURNED, geometry_issue="frame_mismatch"), dict(TURNED, rotation=None)):
        cx = contexts.derive(turned_two(geometry), "v")
        assert cx.segments[1].start == (2, 260.0), geometry


# --- contexts built from stored units (round 9 U8: KTD6; R6, R10) ---------------------------------------------------------

def _unit(no: int, kind: str, title: str | None, aliases: tuple = (), shown: tuple = (), first: int = 0, last: int = 0,
          pages: tuple = (1, 1), identifiers: dict | None = None, signals: tuple | None = None):
    """A synthetic stored unit; ``signals``: the signals of the cut that opens it (``evidence["boundary"]``), unknown by default."""
    from app.chat import units

    evidence = {} if signals is None else {"boundary": {"block": first, "signals": list(signals)}}
    return units.Unit(no, kind, title, aliases, identifiers if identifiers is not None else {"shown": list(shown)},
                      pages[0], pages[1], first, last, "certain", evidence)


def _units_contexts(reading: str | None = "r1") -> contexts.Contexts:
    parts = [_unit(1, "front_matter", None, first=0, last=1, pages=(1, 1)),
             _unit(2, "appraisal", "פרויקט שדרות האלון", ("שדרות האלון", "האלון"), first=2, last=5, pages=(2, 3)),
             _unit(3, "appraisal", "מתחם גבעת הרקפות", ("גבעת הרקפות", "הרקפות"), ("גוש 31140 חלקה 22",),
                   first=6, last=9, pages=(4, 5),
                   identifiers={"addresses": [], "parcels": [["31140", "22"]], "shown": ["גוש 31140 חלקה 22"]}),
             _unit(4, "appendix", "נספח: עסקאות השוואה", first=10, last=12, pages=(6, 7))]
    first = {n: SimpleNamespace(block_index=n, page=p, bbox=[50.0, 40.0, 500.0, 52.0])
             for n, p in ((2, 2), (6, 4), (10, 6))}
    return contexts.from_units(parts, first, [3, 7, 11], "v", reading)


def test_a_units_name_is_found_in_free_text_with_the_hebrew_prefix_letters():
    cx = _units_contexts()
    assert cx.multi and cx.numbers == [1, 2, 3, 4]
    assert cx.named("מה שווי הנכס בגבעת הרקפות?") == [3]
    assert cx.named("השווי של מתחם גבעת הרקפות") == [3] and cx.named("והרקפות") == [3]
    assert cx.named("מה השטח בפרויקט שדרות האלון") == [2] and cx.named("שדרות האלון") == [2]
    assert cx.named("מה הכנסה נטו בנכס בפרויקט הדס") == []  # a name that is in no unit
    assert cx.named("גוש 31140 חלקה 22") == [3]  # its identifiers still name it
    assert cx.named("שווי הרקפות והאלון") == [2, 3]
    assert cx.named("") == [] and cx.named(None) == []


def test_an_appendix_or_comparables_unit_is_labelled_as_such_and_is_never_a_subject():
    cx = _units_contexts()
    assert cx.named("נספח: עסקאות השוואה") == [] and cx.named("הנספח עסקאות השוואה") == []
    assert cx.kind(4) == "appendix" and cx.subjects == [1, 2, 3]
    assert cx.describe(4) == "הקשר 4: נספח: עסקאות השוואה (עמודים 6–7)"
    assert cx.describe(3) == "הקשר 3: מתחם גבעת הרקפות · גוש 31140 חלקה 22 (עמודים 4–5)"
    assert cx.describe(1) == "הקשר 1: פתיח (עמוד 1)"  # front matter is a subject candidate, labelled as what it is
    assert 1 in cx.subjects
    comparables = contexts.from_units([_unit(1, "appraisal", "שומה א", first=0, last=3),
                                       _unit(2, "comparables", None, first=4, last=6)], {}, [], "v")
    assert comparables.label(2) == "עסקאות השוואה" and comparables.subjects == [1]
    assert comparables.named("עסקאות השוואה") == []
    nameless = contexts.from_units([_unit(1, "document", None, first=0, last=3)], {}, [], "v")
    assert nameless.label(1) == contexts.MSG_UNNAMED


def test_a_unit_with_names_but_no_title_or_identifiers_is_labelled_by_its_names():
    """A compendium unit with no title block and no labelled identifiers still has the names its identity card or
    aliases give it: its label is those names (the first two, in identity order), so a source from it is never shown
    to the judge as unnamed beside a file title that names another property."""
    named = contexts.from_units([_unit(1, "appraisal", "מתחם הגפן", first=0, last=2),
                                 _unit(2, "appraisal", None, ("גבעת הנרקיס", "הנרקיס", "שכונת הצבעונים"), first=3, last=5)],
                                {}, [], "v")
    assert named.label(2) == "גבעת הנרקיס · הנרקיס"
    assert named.describe(2) == "הקשר 2: גבעת הנרקיס · הנרקיס (עמוד 1)"
    carded = contexts.from_units(
        [_unit(1, "appraisal", None, first=0, last=2)], {}, [], "v",
        cards={(0, 2): [{"field": "name", "quote": "מתחם הנרקיס"}, {"field": "city", "quote": "כפר הדמה"}]})
    assert carded.label(1) == "מתחם הנרקיס"
    # a title still labels its unit alone, and identifiers still follow it
    assert named.label(1) == "מתחם הגפן"
    titled = contexts.from_units([_unit(1, "appraisal", "מתחם הגפן", ("הגפן",), ("גוש 31140 חלקה 22",), first=0, last=2,
                                        identifiers={"addresses": [], "parcels": [["31140", "22"]],
                                                     "shown": ["גוש 31140 חלקה 22"]})], {}, [], "v")
    assert titled.label(1) == "מתחם הגפן · גוש 31140 חלקה 22"
    # names do not replace a part's kind mark; a unit with nothing stays unnamed
    appendix = contexts.from_units([_unit(1, "appendix", None, ("טבלאות הנרקיס",), first=0, last=2)], {}, [], "v")
    assert appendix.label(1) == "נספח · טבלאות הנרקיס"
    assert contexts.from_units([_unit(1, "document", None, first=0, last=3)], {}, [], "v").label(1) == contexts.MSG_UNNAMED


def test_a_name_inside_a_longer_name_does_not_name_its_unit_where_the_longer_one_is_written():
    cx = contexts.from_units([_unit(1, "appraisal", "מתחם הגפן", ("הגפן",), first=0, last=2),
                              _unit(2, "appraisal", "מתחם הגפן הצפוני", ("הגפן הצפוני",), first=3, last=5)], {}, [], "v")
    assert cx.named("הגפן") == [1] and cx.named("שווי מתחם הגפן הצפוני") == [2]
    assert cx.named("השווה בין הגפן לבין הגפן הצפוני") == [1, 2]  # both are written, one of them alone


def test_a_context_segments_follow_its_units_with_their_leads_and_starts():
    cx = _units_contexts()
    assert [s.number for s in cx.segments] == [1, 2, 3, 4] and cx.at_block(7) == 3 and cx.at_block(11) == 4
    assert [s.lead for s in cx.segments] == [None, 3, 7, 11]
    assert cx.segments[0].start is None and cx.segments[2].start == (4, 40.0)
    assert cx.at_position(5, 100.0) == 3 and cx.at_position(6, 50.0) == 4 and cx.at_position(1, 10.0) == 1
    assert cx.pages(2) == (2, 3)


def test_a_key_carries_the_reading_so_it_is_never_reused_across_readings():
    one, two = _units_contexts("r1"), _units_contexts("r2")
    assert one.key(2) != two.key(2) and one.key(2) != one.key(3)
    assert contexts.split_key(one.key(2)) == ("v", 2) == contexts.split_key(contexts.Contexts("v", ()).key(2))
    assert contexts.derive(TWO, "v").key(2) == "v#2"  # the derivation from blocks keeps its key


def test_calculate_refuses_values_of_two_appraisal_units_unless_the_request_compares_them():
    cx = _units_contexts()
    a, b = (cx.key(2), cx.describe(2)), (cx.key(3), cx.describe(3))
    operands = {"V1": _value("V1", "21400", a), "V2": _value("V2", "22100", b)}
    with pytest.raises(calc.CalcError):
        calc.evaluate(calc.parse("V2 - V1"), operands, contexts=[])
    assert calc.evaluate(calc.parse("V2 - V1"), operands, contexts=[frozenset({cx.key(2), cx.key(3)})]).value == 700


def test_the_unit_kinds_a_context_ignores_are_those_of_the_unit_store():
    from app.chat import units

    assert contexts.NON_SUBJECT_KINDS == {units.APPENDIX, units.COMPARABLES} and set(contexts.UNIT_KIND_MARKS) <= set(
        units.KINDS)


# --- an appendix or comparables part serves the appraisal before it: it is evidence, never "another property" ----------

def _real_value(cx: contexts.Contexts, number: int, vid: str, amount: str) -> calc.Value:
    from uuid import uuid4

    from app.chat import tools

    return calc.Value(vid, Decimal(amount), amount, "S1", uuid4(), uuid4(), "r1", "t", "loc", "l", "value_per_area",
                      "ILS_per_sqm", "none", "unknown", "", "הנכס", "appraised_property", {}, {}, "q",
                      context=tools.value_context(cx, number))


def test_calculate_combines_an_appraisals_value_with_a_comparables_average_but_not_two_appraisals():
    cx = _units_contexts()
    one, three, appendix = (_real_value(cx, 2, "V1", "21400"), _real_value(cx, 3, "V2", "18400"),
                            _real_value(cx, 4, "V3", "9000"))
    assert appendix.context["subject"] is False and one.context["subject"] is True and appendix.operand().leaves[0].context is None
    out = calc.evaluate(calc.parse("V1 + V3"), {"V1": one.operand(), "V3": appendix.operand()}, contexts=[])
    assert out.value == Decimal("30400")
    with pytest.raises(calc.CalcError):  # two appraisals are still two properties
        calc.evaluate(calc.parse("V1 + V2"), {"V1": one.operand(), "V2": three.operand()}, contexts=[])
    # the appendix average does not make a second appraisal's value combinable with the first's
    with pytest.raises(calc.CalcError):
        calc.evaluate(calc.parse("V1 + V2 + V3"),
                      {"V1": one.operand(), "V2": three.operand(), "V3": appendix.operand()}, contexts=[])
    # a request that compares the two appraisals allows it
    assert calc.evaluate(calc.parse("V1 + V2 + V3"), {"V1": one.operand(), "V2": three.operand(),
                         "V3": appendix.operand()}, contexts=[frozenset({cx.key(2), cx.key(3)})]).value == Decimal("48800")


def _claim_ws(cx: contexts.Contexts, cited: dict[str, int], asked: str) -> SimpleNamespace:
    values = {vid: SimpleNamespace(context=_real_value(cx, n, vid, "1").context, written="14,850,000", version_id="v",
                                   subject_from="", subject="הנכס") for vid, n in cited.items()}
    return SimpleNamespace(requirement_items=[], user_messages=[{"text": asked, "current": True}], values=values,
                           measurements={}, computations={}, sources={}, contexts={"v": cx})


def test_a_claim_about_appraisal_one_citing_a_comparables_row_is_not_removed_as_wrong_subject(monkeypatch):
    from app.chat import verify
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", True)
    cx = _units_contexts()
    asked = "מה שווי שדרות האלון?"
    unit = verify.Unit(0, "שווי הנכס 14,850,000 ₪ [V1].", "שווי הנכס 14,850,000 ₪", ["V1"])
    assert verify._wrong_context(unit, _claim_ws(cx, {"V1": 4}, asked), asked) is None  # the appendix row
    assert verify._wrong_context(unit, _claim_ws(cx, {"V1": 2}, asked), asked) is None  # its own value
    elsewhere = verify._wrong_context(unit, _claim_ws(cx, {"V1": 3}, asked), asked)  # appraisal 3's value
    assert elsewhere and cx.describe(3) in elsewhere


def test_an_appendix_value_is_no_second_answer_for_the_subjects_datum(monkeypatch):
    from uuid import uuid4

    from app.config import get_settings

    cx = _units_contexts()
    doc = uuid4()

    def value(vid: str, n: int, amount: str):
        v = _grounded(vid, amount, n, doc)
        v.context = _real_value(cx, n, vid, "1").context
        return v

    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", True)
    ws = Workspace(ctx=None)
    ws.values.update({"V1": value("V1", 2, "21400"), "V2": value("V2", 4, "9000")})
    assert not coverage.conflicting(ws)


def test_only_a_contradicted_appendix_value_is_removed_and_a_calculation_from_appendix_values_never_is(monkeypatch):
    from app.chat import verify
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", True)
    cx = _units_contexts()
    asked = "מה שווי שדרות האלון?"
    unit = verify.Unit(0, "שווי הנכס 14,850,000 ₪ [V1].", "שווי הנכס 14,850,000 ₪", ["V1"])
    ws = _claim_ws(cx, {"V1": 4}, asked)
    assert verify._wrong_context(unit, ws, asked) is None  # an appendix row, its subject not contradicted
    ws.values["V1"].subject_from = "contradicted"  # the source places it on another property
    assert cx.describe(4) in verify._wrong_context(unit, ws, asked)
    calc_unit = verify.Unit(0, "הממוצע 14,850,000 ₪ [C1].", "הממוצע 14,850,000 ₪", ["C1"])
    leaves = [SimpleNamespace(context=_real_value(cx, 4, "V1", "1").operand().leaves[0].context)]
    ws.computations = {"C1": SimpleNamespace(outcome=SimpleNamespace(leaves=leaves))}
    assert verify._wrong_context(calc_unit, ws, asked) is None


# --- a name or identifier names a unit only if no other subject unit of the file holds it (round 15 U7, KTD8, R10) -----------

def _ident(street: str = "", parcel: tuple[str, str] | None = None) -> dict:
    """The stored identifiers of a synthetic unit (``units.Unit.identifiers``): what the ingestion keeps of the labelled ones."""
    written = ([f"רחוב {street}"] if street else []) + ([f"גוש {parcel[0]} חלקה {parcel[1]}"] if parcel else [])
    found = contexts.identifiers("\n".join(written))
    return {"addresses": [list(a) for a in found.addresses], "parcels": [list(p) for p in found.parcels],
            "shown": list(found.shown)}


def _shared_word_contexts() -> contexts.Contexts:
    """Units 2 and 3 share the project word «פרויקט הגפן» (an alias of both) and the street «הגפן 4»; each has its own letter
    title and parcel; unit 4 is a comparables part that names unit 3."""
    parts = [_unit(1, "front_matter", None, first=0, last=1),
             _unit(2, "appraisal", "פרויקט הגפן א", ("פרויקט הגפן", "הגפן א"), first=2, last=5, pages=(2, 3),
                   identifiers=_ident("הגפן 4", ("100", "5"))),
             _unit(3, "appraisal", "פרויקט הגפן ב", ("פרויקט הגפן", "הגפן ב"), first=6, last=9, pages=(4, 5),
                   identifiers=_ident("הגפן 4", ("100", "6"))),
             _unit(4, "comparables", "עסקאות השוואה - פרויקט הגפן ב", ("עסקאות השוואה",), first=10, last=12, pages=(6, 6))]
    first = {n: SimpleNamespace(block_index=n, page=p, bbox=[50.0, 40.0, 500.0, 52.0]) for n, p in ((2, 2), (6, 4), (10, 6))}
    return contexts.from_units(parts, first, [3, 7, 11], "v", "r1")


def test_a_name_or_identifier_held_by_two_subject_units_names_neither_and_a_unique_one_still_names_its_unit():
    cx = _shared_word_contexts()
    assert cx.numbers == [1, 2, 3, 4] and cx.subjects == [1, 2, 3]
    assert cx.named("מה שווי הנכס בפרויקט הגפן?") == []  # the shared alias decides nothing
    assert cx.named("רחוב הגפן 4") == []  # the shared address decides nothing
    assert cx.named("פרויקט הגפן א") == [2] and cx.named("שווי הגפן ב") == [3]  # a name of one unit only
    assert cx.named("גוש 100 חלקה 5") == [2] and cx.named("100/6") == [3]  # a unique identifier keeps naming
    assert cx.named("השווה בין פרויקט הגפן א לפרויקט הגפן ב") == [2, 3]
    assert cx.named("עסקאות השוואה") == []  # a comparables part is never a subject, held or not


def test_a_name_held_only_by_an_appendix_or_comparables_part_still_names_its_subject_unit():
    parts = [_unit(1, "appraisal", "פרויקט הדקל", ("הדקל",), first=0, last=3),
             _unit(2, "appraisal", "פרויקט האורן", ("האורן",), first=4, last=7),
             _unit(3, "comparables", "השוואה לפרויקט הדקל", ("הדקל",), first=8, last=9)]
    cx = contexts.from_units(parts, {}, [], "v", "r1")
    assert cx.named("שווי הדקל") == [1]  # the comparables part is no subject: it holds the name against nobody


# --- the asked set of the context check: distinctive naming first, then the declared units (round 15 U7, KTD7, R3, R10) ----

def _declared_ws(cx: contexts.Contexts, cited: dict[str, int], question: str, declared: list[int] | None = None,
                 sources: dict | None = None) -> SimpleNamespace:
    """A workspace whose answer cites ``cited`` (id -> context number) and declares the units ``declared`` (numbers of
    ``cx``): their anchors and the handles the server issued for them, as the engine keeps them."""
    ws = _claim_ws(cx, cited, question)
    ws.handles = {f"U{n}": {"kind": "U", "document_id": "d", "version_id": cx.version_id, "reading_id": cx.reading_id,
                            "unit_no": n} for n in cx.numbers}
    ws.declared_units = [{"document_id": "d", "reading_id": cx.reading_id, "unit_no": n, "units_tag": cx.tag,
                          "display": cx.label(n)} for n in declared or []]
    ws.declared_handles = {f"U{a['unit_no']}": a for a in ws.declared_units}
    ws.located_unit = ws.unit_focus = None
    if sources:
        ws.sources = sources
    return ws


def _wrong(cx, cited, question, declared=None, claim="שווי הנכס 14,850,000 ₪", sources=None, ids=None):
    from app.chat import verify

    ws = _declared_ws(cx, cited, question, declared, sources)
    unit = verify.Unit(0, f"{claim} [{', '.join(ids or cited)}].", claim, list(ids or cited))
    return verify._wrong_context(unit, ws, question)


def test_a_value_of_the_declared_unit_is_kept_when_the_units_share_a_project_word(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", True)
    cx = _shared_word_contexts()
    ask = "מה שווי הנכס בפרויקט הגפן?"
    assert _wrong(cx, {"V1": 2}, ask, declared=[2]) is None
    assert _wrong(cx, {"V1": 3}, ask, declared=[3]) is None
    # the other unit's value under that declaration is another property's
    elsewhere = _wrong(cx, {"V1": 3}, ask, declared=[2])
    assert elsewhere and cx.describe(3) in elsewhere and cx.describe(2) in elsewhere
    # several declared units are a comparison: a value of either is theirs
    assert _wrong(cx, {"V1": 2}, ask, declared=[2, 3]) is None and _wrong(cx, {"V1": 3}, ask, declared=[2, 3]) is None
    # nothing declared and nothing distinctly named: the shared word decides nothing, so nothing is removed on it
    assert _wrong(cx, {"V1": 3}, ask) is None


def test_a_question_that_names_a_unit_distinctively_outranks_the_declaration(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", True)
    cx = _shared_word_contexts()
    ask = "מה שווי הנכס בפרויקט הגפן א?"
    # the answer declares and cites unit 3, the question names unit 2: the declaration is contradicted
    assert _wrong(cx, {"V1": 3}, ask, declared=[3]) is not None
    # the value of the unit the question names is kept, though the declaration excluded it
    assert _wrong(cx, {"V1": 2}, ask, declared=[3]) is None
    assert _wrong(cx, {"V1": 2}, ask, declared=[2]) is None
    # a declaration that adds a unit does not widen what the question named
    assert _wrong(cx, {"V1": 3}, ask, declared=[2, 3]) is not None


def test_the_request_components_naming_a_unit_distinctively_outrank_the_declaration(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", True)
    cx = _shared_word_contexts()
    ws = _declared_ws(cx, {"V1": 3}, "מה השווי?", declared=[3])
    ws.requirement_items = [{"subject": "פרויקט הגפן א"}]
    from app.chat import verify

    unit = verify.Unit(0, "שווי הנכס 14,850,000 ₪ [V1].", "שווי הנכס 14,850,000 ₪", ["V1"])
    assert verify._wrong_context(unit, ws, "מה השווי?") is not None
    ws.requirement_items = [{"subject": "פרויקט הגפן"}]  # the shared word names nobody: the declaration is the asked set
    assert verify._wrong_context(unit, ws, "מה השווי?") is None


def test_a_claim_naming_the_asked_unit_distinctively_is_removed_when_it_cites_a_sibling_whatever_the_declaration(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", True)
    cx = _shared_word_contexts()
    ask = "מה שווי הנכס בפרויקט הגפן?"
    claim = "שווי הנכס בפרויקט הגפן א הוא 14,850,000 ₪"
    assert _wrong(cx, {"V1": 3}, ask, claim=claim) is not None  # no declaration
    assert _wrong(cx, {"V1": 3}, ask, declared=[3], claim=claim) is not None  # a declaration of the sibling
    assert _wrong(cx, {"V1": 2}, ask, declared=[2], claim=claim) is None


def test_a_declaration_the_server_dropped_falls_back_to_name_matching(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", True)
    cx = _shared_word_contexts()
    ask = "מה שווי הנכס בפרויקט הגפן ב?"  # ``declared_units`` is empty: U6 dropped a handle that was neither offered nor read
    assert _wrong(cx, {"V1": 2}, ask, declared=[]) is not None
    assert _wrong(cx, {"V1": 3}, ask, declared=[]) is None


def test_a_mention_of_another_appraisal_in_the_units_comparables_part_is_left_to_the_judge(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", True)
    cx = _shared_word_contexts()
    ask = "מה שווי הנכס בפרויקט הגפן?"
    claim = "בעסקאות ההשוואה של פרויקט הגפן א נרשם פרויקט הגפן ב בסך 14,850,000 ₪"
    # the figure is in the comparables part (context 4): no subject context, so the check leaves it to the judge
    assert _wrong(cx, {"V1": 4}, ask, declared=[2], claim=claim) is None


def test_a_neighbour_units_passage_is_evidence_for_the_neighbour_not_for_the_declared_unit(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", True)
    cx = _shared_word_contexts()
    # the passage a search of unit 2 returned across a probable edge lies in unit 3: its sources say so
    neighbour = SimpleNamespace(contexts=[3], version_id="v", text="שווי הנכס 14,850,000 ₪", document_id="d")
    own = SimpleNamespace(contexts=[2], version_id="v", text="שווי הנכס 14,850,000 ₪", document_id="d")
    ask = "מה שווי הנכס בפרויקט הגפן?"
    assert _wrong(cx, {}, ask, declared=[2], sources={"S1": neighbour}, ids=["S1"]) is not None
    assert _wrong(cx, {}, ask, declared=[2], sources={"S1": own}, ids=["S1"]) is None
    # declaring both says the answer is about both
    assert _wrong(cx, {}, ask, declared=[2, 3], sources={"S1": neighbour}, ids=["S1"]) is None


def test_a_professional_question_still_removes_nothing_under_a_declaration(monkeypatch):
    from app.chat import verify
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", True)
    cx = _shared_word_contexts()
    ws = _declared_ws(cx, {"V1": 3}, "מה שווי הנכס בפרויקט הגפן?", declared=[2])
    unit = verify.Unit(0, "שווי הנכס 14,850,000 ₪ [V1].", "שווי הנכס 14,850,000 ₪", ["V1"])
    ws.intent = "property"
    assert verify._wrong_context(unit, ws, "מה שווי הנכס בפרויקט הגפן?")
    ws.intent = "professional"
    assert verify._wrong_context(unit, ws, "מה שווי הנכס בפרויקט הגפן?") is None


def test_the_judge_header_of_a_passage_in_the_declared_unit_names_it_by_handle_and_structural_title_only():
    import uuid

    from app.chat import tools, verify

    cx = _shared_word_contexts()
    doc, ws = uuid.uuid4(), Workspace(ctx=None)
    ws.contexts["v"] = cx
    handles = {n: tools.unit_handle_of(ws, doc, "v", "r1", n) for n in (2, 3)}
    for n in (2, 3):
        src = ws.add_source(document_id=doc, version_id="v", title="קובץ שומות סינתטי", section=None, location="עמוד 2",
                            kind="context", text="שווי הנכס 14,850,000 ₪.")
        tools._mark(src, cx, [n], ws)
        src.date_line = ""
    ws.declared_units = [{"document_id": str(doc), "reading_id": "r1", "unit_no": 2, "units_tag": None, "display": "x"}]
    ws.declared_handles = {handles[2]: ws.declared_units[0]}
    ws.sources["S1"].tags.update(unit=handles[2])
    head_declared, _, _ = verify._source_parts(ws, "S1")
    head_other, _, _ = verify._source_parts(ws, "S2")
    assert f"{verify.MSG_DECLARED_UNIT}: {handles[2]} «פרויקט הגפן א»" in head_declared
    assert verify.MSG_DECLARED_UNIT not in head_other  # the neighbour's passage is not the declared unit's
    assert verify.MSG_DECLARED_UNIT not in verify._source_parts(ws, "S1", dated=False)[0]
    ws.declared_units, ws.declared_handles = [], {}
    assert verify.MSG_DECLARED_UNIT not in verify._source_parts(ws, "S1")[0]


# --- a name the evidence's own unit writes separates nothing (round 15, held-out) ----------------------------------------------

def _brand_contexts(other: dict | None = None) -> contexts.Contexts:
    """Unit 2's card holds the synthetic name «מרכז אופנה סינתטי» (and the street «הדקל 7»); unit 3 has no identity at all, or — with
    ``other`` — the identifiers of a property of its own (round 19 U2: absence of an identity proves nothing, an identity of its own
    is another property's)."""
    parts = [_unit(1, "front_matter", None, first=0, last=1),
             _unit(2, "appraisal", "מרכז אופנה סינתטי", ("מרכז אופנה סינתטי",), first=2, last=5, pages=(2, 3),
                   identifiers=_ident("הדקל 7")),
             _unit(3, "appraisal", None, first=6, last=9, pages=(4, 5), identifiers=other or {})]
    first = {n: SimpleNamespace(block_index=n, page=p, bbox=[50.0, 40.0, 500.0, 52.0]) for n, p in ((2, 2), (6, 4))}
    return contexts.from_units(parts, first, [3, 7], "v", "r1")


def _brand_wrong(monkeypatch, passage: str, claim: str, other: dict | None = None):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", True)
    cx = _brand_contexts(other)
    assert cx.subjects == [1, 2, 3] and cx.named("מרכז אופנה סינתטי") == [2]
    source = SimpleNamespace(contexts=[3], version_id="v", text=passage, document_id="d")
    return _wrong(cx, {"V1": 3}, "מה ה-NOI?", claim=claim, sources={"S1": source}), cx


def test_a_name_the_evidence_unit_writes_in_its_own_passages_does_not_make_another_unit_the_asked_one(monkeypatch):
    removed, _ = _brand_wrong(monkeypatch, "מרכז אופנה סינתטי — הכנסה נטו 14,850,000 ₪, מרכז אופנה סינתטי ממוקם בקומה 2.",
                              "ה-NOI של מרכז אופנה סינתטי הוא 14,850,000 ₪")
    assert removed is None  # unit 3 itself writes that name: it separates unit 2 from unit 3 by nothing


def test_a_name_the_evidence_unit_does_not_write_still_marks_its_value_as_another_properties_when_the_unit_has_an_identity(monkeypatch):
    removed, cx = _brand_wrong(monkeypatch, "הכנסה נטו 14,850,000 ₪ לשנה.", "ה-NOI של מרכז אופנה סינתטי הוא 14,850,000 ₪",
                               other=_ident("הצפצפה 3"))
    assert removed and cx.describe(3) in removed and cx.describe(2) in removed  # the protection is kept


def test_a_unit_named_by_an_identifier_is_kept_asked_though_the_passage_writes_its_other_name(monkeypatch):
    removed, _ = _brand_wrong(monkeypatch, "מרכז אופנה סינתטי — רחוב הצפצפה 3 — הכנסה נטו 14,850,000 ₪.",
                              "ה-NOI של מרכז אופנה סינתטי, רחוב הדקל 7, הוא 14,850,000 ₪", other=_ident("הצפצפה 3"))
    assert removed  # the address names unit 2 whatever the passage writes; the passage also writes unit 3's own address


# --- a unit's running subject is its name: a brand phrase on one card, the longer name on another (round 16 U5, AE4, R9) ---

BRAND, CITY = "אלמוג סינתטי", "עכו הדמה"


def _brand_and_city_file() -> contexts.Contexts:
    """Two reports derived through the real chain (``units.derive``) from synthetic pages: unit 1 opens with a title block
    that holds only the brand phrase and runs under the header «דוח הערכת שווי — <brand>»; unit 2 has no title block and runs
    under «דוח הערכת שווי — <brand> <city>» with another footer. Unit 2's only name is its running subject."""
    from app.chat import units
    from tests.unit.test_units_derive import A_FOOT, B_FOOT, Doc, run_pages

    d = Doc()
    d.page(1, 1)
    d.title_block(1, BRAND, letterhead=False)
    d.chapters(1, ["מבוא"])
    run_pages(d, 2, 5, f"דוח הערכת שווי — {BRAND}", A_FOOT)
    run_pages(d, 7, 6, f"דוח הערכת שווי — {BRAND} {CITY}", B_FOOT)
    found = d.derive()
    assert [(u.page_first, u.page_last) for u in found] == [(1, 6), (7, 12)]
    assert found[0].title == BRAND and units._norm(f"{BRAND} {CITY}") in found[1].aliases + (found[1].title or "",)
    return contexts.from_units(list(found), {}, [], "v", "r1")


def test_a_claim_naming_the_longer_name_of_a_unit_whose_only_name_is_its_running_subject_is_not_another_properties(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", True)
    cx = _brand_and_city_file()
    assert cx.named(f"{BRAND} {CITY}") == [2] and cx.named(BRAND) == [1]
    source = SimpleNamespace(contexts=[2], version_id="v", text="הכנסה נטו 14,850,000 ₪ לשנה.", document_id="d")
    own = _wrong(cx, {"V1": 2}, "מה ה-NOI?", claim=f"ה-NOI של {BRAND} {CITY} הוא 14,850,000 ₪", sources={"S1": source})
    assert own is None   # unit 2's value, said of the property unit 2 is named after
    brand_only = _wrong(cx, {"V1": 2}, "מה ה-NOI?", claim=f"ה-NOI של {BRAND} הוא 14,850,000 ₪", sources={"S1": source})
    assert brand_only and cx.describe(1) in brand_only   # the brand phrase alone is still unit 1: the protection is kept


# --- a context cut only at a restarted numbering continues the one before it (round 18 U3, KTD3, R8) -----------------------

def _calc_pages_file(second: dict) -> contexts.Contexts:
    """An appraisal of invented ``מתחם גבעת הברוש`` (unit 1, with its address) and the unit its calculation pages made when their
    printed numbering restarted (unit 2): ``second`` are the fields of unit 2 (its kind, title, identifiers and cut signals)."""
    parts = [_unit(1, "appraisal", "מתחם גבעת הברוש", ("גבעת הברוש",), ("רחוב הברוש 12",), first=0, last=7, pages=(1, 2),
                   identifiers=_ident("הברוש 12", ("33410", "27"))),
             _unit(2, **({"kind": "document", "title": None, "first": 8, "last": 12, "pages": (3, 4)} | second))]
    first = {8: SimpleNamespace(block_index=8, page=3, bbox=[50.0, 40.0, 500.0, 52.0])}
    return contexts.from_units(parts, first, [4, 9], "v", "r1")


RESTART = {"signals": ("label_restart",)}
ASK_CALC = "מה ההכנסה נטו השנתית של מתחם גבעת הברוש?"


def test_calculation_pages_cut_only_at_a_restarted_numbering_are_the_appraisals_own_not_another_property(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", True)
    cx = _calc_pages_file(RESTART)
    assert cx.subjects == [1, 2] and cx.named(ASK_CALC) == [1]  # the question names the appraisal, and the cut is a context
    assert _wrong(cx, {"V1": 2}, ASK_CALC, claim="ההכנסה נטו השנתית היא 14,850,000 ₪") is None  # a value of the calculation pages
    passage = SimpleNamespace(contexts=[2], version_id="v", text="הכנסה נטו שנתית 14,850,000 ₪.", document_id="d")
    assert _wrong(cx, {}, ASK_CALC, claim="ההכנסה נטו השנתית היא 14,850,000 ₪", sources={"S1": passage}, ids=["S1"]) is None
    assert cx.owner(2) == 1 and cx.owner(1) == 1


@pytest.mark.parametrize(("why", "second"), [
    ("an address of its own", {"identifiers": _ident("הצפצפה 7")}),
    ("a parcel of its own", {"identifiers": _ident(parcel=("30874", "9"))}),
    ("a name of its own", {"title": "פרויקט נחל הצבר", "aliases": ("נחל הצבר",)}),
], ids=lambda x: x if isinstance(x, str) else "")
def test_a_value_of_a_context_that_is_not_a_mere_continuation_is_still_another_property(monkeypatch, why, second):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", True)
    cx = _calc_pages_file(RESTART | second)
    removed = _wrong(cx, {"V1": 2}, ASK_CALC, claim="ההכנסה נטו השנתית היא 14,850,000 ₪")
    assert removed and cx.describe(2) in removed, why
    assert cx.owner(2) == 2, why


@pytest.mark.parametrize(("why", "second"), [
    ("a subject line opens it", {"signals": ("subject_line", "label_restart")}),
    ("a letter opening opens it", {"signals": ("letter_opening",)}),
    ("the signals of the cut are not known", {"signals": None}),
    ("its heading is digit noise", {"title": "2315 63", "signals": ("subject_line",)}),
    ("its heading is a sentence ending with a colon", {"title": "להלן פירוט החישוב של שווי הנכס לפי נתוני השומה:",
                                                       "signals": ("subject_line",)}),
], ids=lambda x: x if isinstance(x, str) else "")
def test_a_context_that_states_no_identity_is_neither_the_appraisals_continuation_nor_another_property(monkeypatch, why, second):
    """Round 19 U2 (R5, R6): an opening signal or an unknown cut proves the pages are not an established continuation, but without
    an address, a parcel or a name of their own they are not another property either: nothing is removed deterministically."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", True)
    cx = _calc_pages_file(RESTART | second)
    assert cx.owner(2) == 2, why  # no established continuation: the cut opens it
    assert cx.compare(2, [1]) == contexts.UNKNOWN, why
    assert _wrong(cx, {"V1": 2}, ASK_CALC, claim="ההכנסה נטו השנתית היא 14,850,000 ₪") is None, why


def test_a_continuation_after_an_appendix_has_nothing_to_continue(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", True)
    parts = [_unit(1, "appraisal", "מתחם גבעת הברוש", ("גבעת הברוש",), first=0, last=3, pages=(1, 1)),
             _unit(2, "appendix", "נספח", first=4, last=7, pages=(2, 2), signals=("label_restart",)),
             _unit(3, "document", None, first=8, last=10, pages=(3, 3), signals=("label_restart",))]
    cx = contexts.from_units(parts, {}, [], "v", "r1")
    assert cx.owner(3) == 3  # an appendix is nothing to continue
    assert cx.compare(3, [1]) == contexts.UNKNOWN  # and the pages state no identity of their own: not another property either
    assert _wrong(cx, {"V1": 3}, ASK_CALC, claim="ההכנסה נטו השנתית היא 14,850,000 ₪") is None


def test_a_second_continuation_continues_the_same_appraisal():
    parts = [_unit(1, "appraisal", "מתחם גבעת הברוש", ("גבעת הברוש",), first=0, last=3, pages=(1, 1)),
             _unit(2, "document", None, first=4, last=7, pages=(2, 2), signals=("label_restart",)),
             _unit(3, "document", None, first=8, last=10, pages=(3, 3), signals=("label_restart", "running_line_change"))]
    cx = contexts.from_units(parts, {}, [], "v", "r1")
    assert (cx.owner(2), cx.owner(3)) == (1, 1)


def test_the_header_of_a_passage_in_a_file_of_several_reports_names_its_own_unit_first_and_the_file_title_only_as_the_file():
    import uuid

    from app.chat import tools, verify

    cx = _shared_word_contexts()
    doc, ws = uuid.uuid4(), Workspace(ctx=None)
    ws.contexts["v"] = cx
    src = ws.add_source(document_id=doc, version_id="v", title="שומת מקרקעין — פרויקט הגפן א", section=None,
                        location="עמוד 4", kind="context", text="שווי הנכס 14,850,000 ₪.")
    tools._mark(src, cx, [3], ws)
    head = verify._source_parts(ws, src.sid)[0]
    assert head.startswith(cx.describe(3)), head  # the unit the passage is in, by its own subject
    assert head.endswith(f" — {verify.MSG_FILE}: שומת מקרקעין — פרויקט הגפן א"), head  # the file title, as the file, last
    # a file of one subject unit is shown as before: its title, its location and the identity the document states
    cx1 = contexts.from_units([_unit(1, "appraisal", "מתחם הגפן", ("הגפן",), first=0, last=3)], {}, [], "v1", "r1")
    one = ws.add_source(document_id=doc, version_id="v1", title="שומת מקרקעין", section=None, location="עמוד 1",
                        kind="context", text="טקסט.")
    ws.contexts["v1"] = cx1
    tools._mark(one, cx1, [1], ws)
    assert verify._source_parts(ws, one.sid)[0].startswith("שומת מקרקעין — עמוד 1 — זיהוי מתוך המסמך: מתחם הגפן")


# --- three-state ownership: same, different or unknown (round 19 U2, KTD2, R4-R8; AE3, AE4) ----------------------------------

COLON = "להלן פירוט החישוב של שווי הנכס בגישת היוון ההכנסות לפי נתוני השומה:"
ASK_OWNER = "מה ההכנסה נטו השנתית של מתחם גבעת האלה?"
OWNER_CLAIM = "ההכנסה נטו השנתית של מתחם גבעת האלה היא 610,000 ₪"
OWNER_TEXT = "טבלה 1 — חישוב ההיוון לגוש 40100 חלקה 12: הכנסה נטו שנתית 610,000 ₪."


def _ownership_file(second: dict | None = None) -> contexts.Contexts:
    """Unit 1: the appraisal of the invented «מתחם גבעת האלה» (street «הדס 5», parcel 40100/12). Unit 2: pages that a restarted
    numbering cut (``second`` are its fields). Unit 3 and unit 4: appraisals of other invented properties, with a parcel and a name each."""
    parts = [_unit(1, "appraisal", "מתחם גבעת האלה", ("גבעת האלה",), first=0, last=7, pages=(1, 2),
                   identifiers=_ident("הדס 5", ("40100", "12"))),
             _unit(2, **({"kind": "document", "title": None, "first": 8, "last": 12, "pages": (3, 4),
                          "signals": ("label_restart",)} | (second or {}))),
             _unit(3, "appraisal", "פרויקט נחל הלוטם", ("נחל הלוטם",), first=13, last=20, pages=(5, 6),
                   identifiers=_ident("הלוטם 9", ("40100", "31")), signals=("subject_line",)),
             _unit(4, "appraisal", "פרויקט עין השקמה", ("עין השקמה",), first=21, last=28, pages=(7, 8),
                   identifiers=_ident("השקמה 2", ("40100", "55")), signals=("subject_line",))]
    first = {8: SimpleNamespace(block_index=8, page=3, bbox=[50.0, 40.0, 500.0, 52.0]),
             13: SimpleNamespace(block_index=13, page=5, bbox=[50.0, 40.0, 500.0, 52.0]),
             21: SimpleNamespace(block_index=21, page=7, bbox=[50.0, 40.0, 500.0, 52.0])}
    return contexts.from_units(parts, first, [4, 9, 14, 22], "v", "r1")


def _passage(number: int, text: str, link: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(contexts=[number], version_id="v", text=text, document_id="d", link=link)


def _owned(cx, number: int, text: str, claim: str = OWNER_CLAIM, question: str = ASK_OWNER, link: str | None = None):
    """The context check of a claim citing a passage of context ``number`` that reads ``text``."""
    from app.chat import verify

    ws = _declared_ws(cx, {}, question, sources={"S1": _passage(number, text, link)})
    return verify._context_check(verify.Unit(0, f"{claim} [S1].", claim, ["S1"]), ws, question)


@pytest.fixture
def enforced(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", True)


@pytest.mark.parametrize("heading", ["2315 63", "0 7 1 8 3 4 2", "  ", "1.", COLON, "שם הנכס:"])
def test_a_digit_noise_heading_and_a_colon_sentence_are_no_name(heading):
    assert contexts.noise_heading(heading)
    cx = _ownership_file({"title": heading, "aliases": (heading,)})
    assert cx.identity(2).names == () and cx.identity(2).empty


@pytest.mark.parametrize("heading", ["מתחם גבעת האלה", "פרויקט נחל הלוטם ב", "Harbor Tower"])
def test_a_title_with_words_and_no_colon_is_still_a_name(heading):
    assert not contexts.noise_heading(heading)
    assert contexts.name_key(heading) in contexts_names(_ownership_file({"title": heading}), 2)


def contexts_names(cx, number):
    return cx.identity(number).names


@pytest.mark.parametrize("heading", [COLON, "2315 63"], ids=["a colon sentence", "digit noise"])
def test_ae3_a_value_on_pages_under_a_noise_heading_is_the_asked_propertys_when_the_cited_text_names_its_parcel(enforced, heading):
    cx = _ownership_file({"title": heading, "signals": ("subject_line",)})  # the cut even opens with a (false) subject line
    assert cx.owner(2) == 2 and cx.compare(2, [1]) == contexts.UNKNOWN  # no continuity, no identity: nothing is known by the unit
    found = _owned(cx, 2, OWNER_TEXT)
    assert found.reason is None and found.facts == []  # the cited text names the asked property by its parcel: the same
    # the same pages, the text silent about the property: unknown, never "another property", and the judge is told
    silent = _owned(cx, 2, "הכנסה נטו שנתית 610,000 ₪.")
    assert silent.reason is None and len(silent.facts) == 1
    assert cx.describe(2) in silent.facts[0] and cx.describe(1) in silent.facts[0]


def test_ae3_protection_a_value_of_a_property_with_another_parcel_in_the_same_file_is_still_removed(enforced):
    cx = _ownership_file({"title": COLON})
    other = _owned(cx, 3, "גוש 40100 חלקה 31: שווי הנכס בגישת ההשוואה 610,000 ₪.")  # unit 3's text names its own parcel
    assert other.reason and cx.describe(3) in other.reason and cx.describe(1) in other.reason
    # unit 3's value cited with a text that names nothing: its own identity (parcel, name) says whose it is
    assert _owned(cx, 3, "שווי הנכס בגישת ההשוואה 610,000 ₪.").reason
    # pages that state no identity and open with a subject line, whose text names ANOTHER property's parcel: that property's
    opened = _ownership_file({"title": COLON, "signals": ("subject_line",)})
    assert _owned(opened, 2, "גוש 40100 חלקה 55: הכנסה נטו שנתית 610,000 ₪.").reason


def test_a_text_that_names_the_asked_property_and_a_unit_that_states_another_leave_it_unknown_not_removed(enforced):
    cx = _ownership_file()
    claim = "ההכנסה נטו השנתית בגוש 40100 חלקה 12 היא 610,000 ₪"
    found = _owned(cx, 3, "בהשוואה למתחם גבעת האלה (גוש 40100 חלקה 12): הכנסה נטו שנתית 610,000 ₪.", claim=claim)
    assert found.reason is None and len(found.facts) == 1
    assert _owned(cx, 3, "גוש 40100 חלקה 31: הכנסה נטו שנתית 610,000 ₪.", claim=claim).reason  # the text names only unit 3's parcel


def test_a_link_to_the_same_property_is_ownership_when_nothing_conflicts_and_never_overrides_an_identity(enforced):
    cx = _ownership_file({"title": COLON, "signals": ("subject_line",)})
    link = "שומה של אותו נכס: חלקה 40100/12 משותפת"
    assert cx.compare(2, [1], linked=True) == contexts.SAME and cx.compare(2, [1]) == contexts.UNKNOWN
    assert _owned(cx, 2, "הכנסה נטו שנתית 610,000 ₪.", link=link) == _owned(cx, 2, OWNER_TEXT)  # a link alone: the same
    # a link on a source of unit 3 does not make unit 3's own parcel the asked one
    assert _owned(cx, 3, "גוש 40100 חלקה 31: הכנסה נטו שנתית 610,000 ₪.", link=link).reason


def test_ae3_established_continuity_still_says_same(enforced):
    cx = _ownership_file({"title": COLON})  # a restarted numbering and nothing of its own
    assert cx.owner(2) == 1 and cx.compare(2, [1]) == contexts.SAME
    assert _owned(cx, 2, "הכנסה נטו שנתית 610,000 ₪.") .reason is None


def test_compare_never_says_different_for_an_asked_side_that_resolved_only_in_part_or_not_at_all():
    cx = _ownership_file()
    assert cx.compare(3, [1]) == contexts.DIFFERENT
    assert cx.compare(3, [1], complete=False) == contexts.UNKNOWN
    assert cx.compare(3, []) == contexts.UNKNOWN
    assert cx.compare(1, [1], complete=False) == contexts.SAME


def _appendix_file(heading: str, street: str = "", parcel: tuple[str, str] | None = None) -> contexts.Contexts:
    """Unit 1: the appraisal of the invented «מתחם גבעת האלה» (street «הדס 5», parcel 40100/12). Unit 2: an appendix whose lead
    heading (block 9, the first heading of the unit) is ``heading`` and whose stored identification states ``street`` and ``parcel``
    (round 19 U2, KTD2, R5)."""
    parts = [_unit(1, "appraisal", "מתחם גבעת האלה", ("גבעת האלה",), first=0, last=7, pages=(1, 2),
                   identifiers=_ident("הדס 5", ("40100", "12"))),
             _unit(2, "appendix", None, first=8, last=12, pages=(3, 4), identifiers=_ident(street, parcel),
                   signals=("appendix_marker",))]
    first = {8: SimpleNamespace(block_index=8, page=3, bbox=[50.0, 40.0, 500.0, 52.0])}
    return contexts.from_units(parts, first, [4, 9], "v", "r1", lead_texts={9: heading, 4: "1. מבוא"})


def test_an_appendix_whose_own_heading_states_another_address_and_parcel_is_another_property(enforced):
    from app.chat import tools

    cx = _appendix_file("נספח א׳ — נכס השוואה ברחוב הדקל 8, גוש 40105 חלקה 2")  # the unit's title run stopped at this heading
    assert cx.compare(2, [1]) == contexts.DIFFERENT
    assert cx.identity(2).empty  # the heading's identity is no label and no subject's identity
    assert cx.compare(2, [1], complete=False) == contexts.UNKNOWN  # an asked side that resolved only in part never says different
    assert tools.subject_from(cx, 2, "מתחם גבעת האלה") == "contradicted"
    # the same identity stated in the unit's stored identification, and a noise heading (a sentence ending with a colon) that is none
    assert _appendix_file("נספח ב׳", "הדקל 8", ("40105", "2")).compare(2, [1]) == contexts.DIFFERENT
    assert _appendix_file("להלן נכס השוואה ברחוב הדקל 8, גוש 40105 חלקה 2:").compare(2, [1]) == contexts.UNKNOWN


def test_an_appendix_with_no_address_or_parcel_of_its_own_is_not_another_property(enforced):
    from app.chat import tools

    cx = _appendix_file("נספח א׳ — תחשיב")
    assert cx.compare(2, [1]) == contexts.UNKNOWN
    assert tools.subject_from(cx, 2, "מתחם גבעת האלה") == "asserted"


def test_an_appendix_naming_the_asked_address_or_parcel_is_not_another_property(enforced):
    from app.chat import tools

    for street, parcel in (("הדס 5", None), ("", ("40100", "12")), ("הדס 5", ("40100", "12"))):
        cx = _appendix_file("נספח א׳ — נספח לשומה", street, parcel)
        assert cx.compare(2, [1]) != contexts.DIFFERENT, (street, parcel)
        assert tools.subject_from(cx, 2, "מתחם גבעת האלה") != "contradicted", (street, parcel)
    cx = _appendix_file("נספח א׳ — תחשיב לנכס ברחוב הדס 5, גוש 40100 חלקה 12")  # the heading names the asked property
    assert cx.compare(2, [1]) != contexts.DIFFERENT and tools.subject_from(cx, 2, "מתחם גבעת האלה") != "contradicted"
    # one shared identifier is enough: the asked side holds it, so another address beside it proves no other property
    assert _appendix_file("נספח ב׳", "הדקל 8", ("40100", "12")).compare(2, [1]) != contexts.DIFFERENT


def test_a_page_range_names_the_one_unit_that_holds_it_and_none_when_it_spans_several():
    cx = _ownership_file()
    assert cx.at_pages(5, 6) == [3] and cx.at_pages(3, 4) == [2] and cx.at_pages(1, 2) == [1]
    assert cx.resolve("השומה בעמודים 7–8") == [4] and cx.resolve("עמוד 5") == [3]
    assert cx.resolve("השומה בעמודים 2–5") == []  # spans several units: it names none
    assert cx.resolve("מתחם גבעת האלה ופרויקט נחל הלוטם") == [1, 3]


def test_a_compound_subject_is_divided_into_its_sides_and_resolved_side_by_side():
    from app.chat import tools

    cx = _ownership_file()
    assert tools.subject_sides("מתחם גבעת האלה והשומה בעמודים 7–8") == ["מתחם גבעת האלה", "השומה בעמודים 7–8"]
    assert tools.subject_sides("פרויקט וילה גן") == ["פרויקט וילה גן"]  # a name that starts with the conjunction letter is one side
    assert tools.subject_sides("שומות מתחם גבעת האלה ובית 20 בנחל") == ["שומות מתחם גבעת האלה", "בית 20 בנחל"]
    assert tools.resolved_sides(cx, "מתחם גבעת האלה והשומה בעמודים 7–8") == ({1, 4}, True)
    assert tools.resolved_sides(cx, "מתחם גבעת האלה והשומה בעמודים 90–91") == ({1}, False)  # only in part
    assert tools.resolved_sides(cx, "הנכס") == (set(), True)  # a plain subject that names nothing is no partial compound


def _compound_ws(cx, cited: dict[str, int], subject: str, question: str = "השווה"):
    ws = _declared_ws(cx, cited, question)
    ws.requirement_items = [{"subject": subject}]
    return ws


def test_a_compound_subject_that_resolves_only_in_part_is_unknown_and_nothing_is_removed(enforced):
    from app.chat import verify

    cx = _ownership_file()
    unit = verify.Unit(0, "שווי הנכס 14,850,000 ₪ [V1].", "שווי הנכס 14,850,000 ₪", ["V1"])
    # side two (pages 90-91) is in no unit: a value of unit 3 is not provably another property's
    partial = _compound_ws(cx, {"V1": 3}, "מתחם גבעת האלה והשומה בעמודים 90–91")
    found = verify._context_check(unit, partial, "השווה")
    assert found.reason is None and len(found.facts) == 1 and verify.MSG_OWNER_PARTIAL in found.facts[0]
    # both sides resolved: unit 3 is neither side, unit 4 is a side
    resolved = _compound_ws(cx, {"V1": 3}, "מתחם גבעת האלה והשומה בעמודים 7–8")
    assert verify._context_check(unit, resolved, "השווה").reason
    assert verify._context_check(unit, _compound_ws(cx, {"V1": 4}, "מתחם גבעת האלה והשומה בעמודים 7–8"), "השווה") == verify.ContextCheck()


def _cells_ws(cx, cited: dict[str, tuple[int, str]], subject: str = "השוואת שתי השומות") -> SimpleNamespace:
    ws = _declared_ws(cx, {}, "השווה")
    ws.requirement_items = [{"subject": subject}]
    ws.values = {vid: SimpleNamespace(context=_real_value(cx, n, vid, "1").context, written=amount, version_id="v",
                                      subject_from="", subject="הנכס") for vid, (n, amount) in cited.items()}
    return ws


def _row(a: str, b: str):
    from app.chat import verify

    raw = f"| שווי | {a} | {b} |"
    return verify.Unit(0, raw, raw.replace("[V1]", "").replace("[V2]", ""), verify.ids_in(raw), 0, len(raw), False,
                       (0, 60), "| נתון | מתחם גבעת האלה | פרויקט נחל הלוטם |\nהשוואה:")


def test_ae4_each_cell_of_a_comparison_is_checked_against_the_side_its_column_names(enforced):
    from app.chat import verify

    cx = _ownership_file()
    own = _row("7,100,000 ₪ [V1]", "8,200,000 ₪ [V2]")
    assert verify._cited_cells(own)[0][0] == "מתחם גבעת האלה"
    assert verify._context_check(own, _cells_ws(cx, {"V1": (1, "7,100,000"), "V2": (3, "8,200,000")}), "השווה") == verify.ContextCheck()
    # the cell under side A's column cites side B, though the row as a whole cites both sides' contexts
    swapped = _row("8,200,000 ₪ [V2]", "7,100,000 ₪ [V1]")
    found = verify._context_check(swapped, _cells_ws(cx, {"V1": (1, "7,100,000"), "V2": (3, "8,200,000")}), "השווה")
    assert found.reason and cx.describe(3) in found.reason and cx.describe(1) in found.reason
    # a cell citing a third property under either column
    third = _row("7,100,000 ₪ [V1]", "8,200,000 ₪ [V2]")
    assert verify._context_check(third, _cells_ws(cx, {"V1": (1, "7,100,000"), "V2": (4, "8,200,000")}), "השווה").reason


def test_a_comparison_cell_under_a_column_that_names_no_property_is_checked_against_the_whole_requests_sides(enforced):
    from app.chat import verify

    cx = _ownership_file()
    raw = "| שווי | 7,100,000 ₪ [V1] | 8,200,000 ₪ [V2] |"
    row = verify.Unit(0, raw, raw.replace("[V1]", "").replace("[V2]", ""), ["V1", "V2"], 0, len(raw), False, (0, 60),
                      "| נתון | צד א | צד ב |\nהשוואה:")
    ws = _cells_ws(cx, {"V1": (1, "7,100,000"), "V2": (3, "8,200,000")}, "מתחם גבעת האלה והשומה בעמודים 5–6")
    assert verify._context_check(row, ws, "השווה") == verify.ContextCheck()  # both sides asked: each cell is one of them
    ws = _cells_ws(cx, {"V1": (1, "7,100,000"), "V2": (4, "8,200,000")}, "מתחם גבעת האלה והשומה בעמודים 5–6")
    assert verify._context_check(row, ws, "השווה").reason  # unit 4 is neither side


def test_subject_from_is_contradicted_only_when_the_identities_say_another_property(enforced):
    from app.chat import tools

    cx = _ownership_file({"title": COLON, "signals": ("subject_line",)})
    subject = "מתחם גבעת האלה"
    assert tools.subject_from(cx, 1, subject) == "context"
    assert tools.subject_from(cx, 3, subject) == "contradicted"  # unit 3 states a parcel and a name of its own
    assert tools.subject_from(cx, 2, subject) == "asserted"  # pages with no identity: the subject is the model's word only
    assert tools.subject_from(cx, 3, "מתחם גבעת האלה והשומה בעמודים 90–91") == "asserted"  # a compound resolved in part
    assert tools.subject_from(cx, 2, "") == "" and tools.subject_from(cx, 2, "הנכס") == "asserted"
    assert tools.subject_from(_ownership_file({"title": COLON}), 2, subject) == "context"  # established continuity


def test_the_value_hint_calls_a_value_another_propertys_only_when_the_identities_say_so(enforced):
    from app.chat import tools

    cx = _ownership_file({"title": COLON, "signals": ("subject_line",)})
    ws = SimpleNamespace(requirement_items=[{"subject": "מתחם גבעת האלה"}], user_messages=[], located_unit=None)
    for number, said in ((2, False), (3, True), (1, False)):
        value = SimpleNamespace(context={"described": cx.describe(number), "number": number}, subject_from="", subject="")
        lines = tools._context_lines(ws, cx, value)
        assert any("אינו ערך של הנכס שנשאל עליו" in x for x in lines) is said, (number, lines)
