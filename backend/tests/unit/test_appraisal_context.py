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


def test_two_contexts_with_different_values_are_not_a_source_conflict(monkeypatch):
    from app.config import get_settings

    def value(vid: str, amount: str, ctx: dict | None):
        return SimpleNamespace(vid=vid, document_id="d", kind="value_per_area", unit="ILS_per_sqm", period="none",
                               area_basis="", subject="הנכס", scenario="", stance="unknown", value=Decimal(amount),
                               context=ctx)

    ws = SimpleNamespace(values={"V1": value("V1", "21400", {"key": "v#1"}),
                                 "V2": value("V2", "22100", {"key": "v#2"})}, measurements={})
    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", True)
    assert not coverage.conflicting(ws)
    ws.values["V3"] = value("V3", "22500", {"key": "v#2"})  # the same context, the same datum, another amount
    assert coverage.conflicting(ws)
    monkeypatch.setattr(get_settings(), "chat_appraisal_context_enforced", False)
    del ws.values["V3"]
    assert coverage.conflicting(ws)  # off: keyed on the subject's words, as before


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
