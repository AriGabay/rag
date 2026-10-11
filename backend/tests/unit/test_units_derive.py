"""Appraisal units derived from a stored reading, without the database (round 9 U7: KTD5; R6–R9, AE2).

Blocks are built the way ingestion stores them: a title block (a prominent one-line title, letterhead lines, a
labelled address), numbered headings, paragraphs and tables, each with its page and box; each page's stored text ends
with its page-number line. The fixtures of rounds 1–9 are extracted for real (no OCR) and their units derived: every
single-appraisal file derives exactly one unit (the stop condition of the round), the two-appraisal file two.
Synthetic text only: every project, street, block and parcel is invented."""

from __future__ import annotations

import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pypdfium2 as pdfium
import pytest

from app.chat import units
from app.chat.units import (
    APPENDIX,
    APPRAISAL,
    CERTAIN,
    COMPARABLES,
    DOCUMENT,
    FRONT_MATTER,
    PROBABLE,
    UNSPLIT,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANIFEST = json.loads((FIXTURES / "round9" / "manifest.json").read_text(encoding="utf-8"))["documents"]
LETTERHEAD = ["מסמך סינתטי לדמו — כל הנתונים בדויים", "משרד: שמאות דמו ב׳ (סינתטי)"]
CHAPTERS = ["מבוא", "תיאור הנכס", "מצב תכנוני", "נתוני שוק", "הערכת שווי", "סיכום ומסקנות"]


def B(i: int, kind: str, page: int | None, text: str, top: float | None, h: float = 11.0, status: str = "read",
      table_index: int | None = None, label: str | None = None, content_hash: str | None = None):
    bbox = None if top is None else [300.0, top, 550.0, top + h]
    return SimpleNamespace(block_index=i, kind=kind, page=page, text=text, bbox=bbox, status=status,
                           table_index=table_index, label=label, content_hash=content_hash, page_end=None)


def page(n: int, label: int | None, printed: str | None = None):
    """A stored page: its text ends with a page-number line (the reading keeps no label for a numbering that
    restarts)."""
    foot = f"מסמך סינתטי לדמו | עמוד {label}" if label is not None else ""
    return SimpleNamespace(page_no=n, printed_label=printed, edge=f"תוכן העמוד\n{foot}")


class Doc:
    """Blocks and pages appended in reading order."""

    def __init__(self):
        self.blocks: list = []
        self.pages: list = []
        self.chapter = 0

    def add(self, kind, page_no, text, top, **kw):
        self.blocks.append(B(len(self.blocks), kind, page_no, text, top, **kw))
        return len(self.blocks) - 1

    def page(self, n: int, label: int | None):
        self.pages.append(page(n, label))

    def title_block(self, page_no: int, name: str, street: str | None = None, block: str = "31102", parcel: str = "7",
                    letterhead: bool = True) -> int:
        first = self.add("paragraph", page_no, name, 48.0, h=15.0)
        if letterhead:
            self.add("paragraph", page_no, LETTERHEAD[0], 72.0, h=10.0)
            self.add("paragraph", page_no, LETTERHEAD[1], 89.0, h=10.0)
        if street:
            self.add("paragraph", page_no, f"כתובת הנכס: {street}, כפר הדמה\nגוש: {block} חלקה: {parcel}", 111.0, h=45.0)
        return first

    def chapters(self, page_no: int, names: list[str], restart: bool = False, top: float = 176.0) -> None:
        for n, name in enumerate(names):
            self.chapter = 1 if restart and n == 0 else self.chapter + 1
            self.add("heading", page_no, f"{self.chapter}. {name}", top, h=12.0, label=str(self.chapter))
            self.add("paragraph", page_no, "הנכס נבדק בסיור שנערך לצורך ההערכה.", top + 20, h=11.0)
            top += 51

    def appraisal(self, first_page: int, name: str, street: str, block: str, parcel: str, label: int = 1,
                  restart: bool = False) -> None:
        """Two pages: the title block and three chapters, then three more chapters."""
        self.page(first_page, label)
        self.title_block(first_page, name, street, block, parcel)
        self.chapters(first_page, CHAPTERS[:3], restart)
        self.page(first_page + 1, label + 1)
        self.chapters(first_page + 1, CHAPTERS[3:], top=54.0)

    def appendix(self, first_page: int, title: str, names: list[str], label: int = 1) -> None:
        self.page(first_page, label)
        self.title_block(first_page, title)
        self.add("paragraph", first_page, title, 119.0, h=12.0)
        self.add("paragraph", first_page, "העסקאות הבאות נאספו להשוואה ואינן מתייחסות לנכס הנישום.", 140.0)
        table = self.add("table", first_page, "\n".join(f"{n} | 300 | 4,100,000" for n in names), 159.0, h=500.0,
                         table_index=0)
        self.blocks[table].page_end = first_page + 1  # the table runs on to the next page
        self.page(first_page + 1, label + 1)

    def derive(self):
        return units.derive(self.blocks, self.pages, "v")


def compendium() -> Doc:
    d = Doc()
    d.appraisal(1, "פרויקט שדרות האלון", "שדרות האלון 14", "31102", "7")
    d.appraisal(3, "מתחם גבעת הרקפות", "רחוב הרקפת 8", "31140", "22")
    d.appraisal(5, "פרויקט נחל הדס", "דרך נחל הדס 3", "31188", "3")
    d.appendix(7, "נספח: עסקאות השוואה", ["מתחם רכס הצבעוני", "בית השקד", "פרויקט גבעת הלוטם"])
    return d


# --- AE2: a compendium of project-named appraisals ------------------------------------------------------------------


def test_a_compendium_derives_three_appraisal_units_and_an_appendix_with_names_and_page_ranges():
    found = compendium().derive()
    assert [(u.kind, u.page_first, u.page_last) for u in found] == [
        (APPRAISAL, 1, 2), (APPRAISAL, 3, 4), (APPRAISAL, 5, 6), (APPENDIX, 7, 8)]
    assert [u.title for u in found[:3]] == ["פרויקט שדרות האלון", "מתחם גבעת הרקפות", "פרויקט נחל הדס"]
    assert {"שדרות האלון", "האלון"} <= set(found[0].aliases)
    assert {"גבעת הרקפות", "הרקפות"} <= set(found[1].aliases)
    assert {"נחל הדס", "הדס"} <= set(found[2].aliases)
    assert found[0].certainty == CERTAIN and all(u.certainty == CERTAIN for u in found)
    assert found[1].evidence["boundary"]["signals"] == [units.LABEL_RESTART, units.TITLE_BLOCK_REPEAT]
    assert [(u.block_first, u.block_last) for u in found][0][0] == 0
    assert all(a.block_last + 1 == b.block_first for a, b in zip(found, found[1:], strict=False))
    assert found[0].identifiers["parcels"] == [["31102", "7"]]


def test_the_manifest_of_the_round_9_compendium_is_met_by_the_synthetic_shape():
    expected = MANIFEST["compendium"]["units"]
    found = compendium().derive()
    assert [u.kind for u in found] == [e["kind"] for e in expected]
    assert [(u.page_first, u.page_last) for u in found] == [(e["first_page"], e["last_page"]) for e in expected]
    for u, e in zip(found, expected, strict=True):
        assert set(e["aliases"]) <= set(u.aliases)
        assert u.title == (e["title"] if e["kind"] == "appraisal" else u.title)


def test_properties_of_a_comparables_appendix_are_neither_units_nor_aliases():
    d = compendium()
    others = ["מתחם רכס הצבעוני", "בית השקד", "פרויקט גבעת הלוטם", "מגדלי הנרקיס"]
    d.add("paragraph", 7, "בין העסקאות: פרויקט גבעת הלוטם, מתחם רכס הצבעוני וגם בית השקד.", 600.0)
    found = d.derive()
    assert len(found) == 4
    every = " ".join(a for u in found for a in u.aliases) + " " + " ".join(u.title or "" for u in found)
    assert not any(n in every for n in ("רכס הצבעוני", "בית השקד", "גבעת הלוטם", "הנרקיס")), others
    # a comparable named in the body of an appraisal does not open a unit either
    d2 = compendium()
    d2.add("paragraph", 2, "נבדק גם פרויקט הצבעוני הצפוני בסביבה.", 300.0)
    assert len(d2.derive()) == 4


def test_similar_project_names_give_two_units_whose_aliases_differ():
    d = Doc()
    d.appraisal(1, "מתחם הגפן א", "רחוב הגפן 5", "31201", "14", label=1)
    d.appraisal(3, "מתחם הגפן ב", "רחוב הגפן 9", "31201", "16", label=1)
    first, second = d.derive()
    assert (first.kind, second.kind) == (APPRAISAL, APPRAISAL)
    assert "הגפן א" in first.aliases and "הגפן ב" in second.aliases
    assert "הגפן ב" not in first.aliases and "הגפן א" not in second.aliases
    assert "הגפן 5" in first.aliases and "הגפן 9" in second.aliases
    assert first.identifiers["parcels"] != second.identifiers["parcels"]


def test_a_title_read_from_a_picture_is_a_weaker_alias_and_the_unit_is_not_presented_as_an_appraisal():
    d = Doc()
    d.page(1, 1)
    d.add("image", 1, "פרויקט גבעת השקמים", 42.5, h=58.0, content_hash="a" * 64)
    d.add("paragraph", 1, LETTERHEAD[0], 113.0, h=10.0)
    d.add("paragraph", 1, LETTERHEAD[1], 131.0, h=10.0)
    d.add("paragraph", 1, "כתובת הנכס: רחוב השקמה 11, כפר הדמה\nגוש: 31265 חלקה: 4", 153.0, h=27.0)
    d.chapters(1, CHAPTERS[:2], top=194.0)
    d.page(2, 2)
    (only,) = d.derive()
    assert (only.kind, only.certainty) == (DOCUMENT, UNSPLIT)
    assert only.title == "פרויקט גבעת השקמים"
    assert {"גבעת השקמים", "השקמים"} <= set(only.aliases)
    weak = [e for a, e in zip(only.aliases, only.evidence["aliases"], strict=True) if a in ("גבעת השקמים", "השקמים")]
    assert weak and all(e["weak"] and e["rule"] == "image_title" and e["block"] == 0 for e in weak)
    assert only.evidence["title"] == {"rule": "image_title", "block": 0, "weak": True}
    # the same line as typed text is not weak
    d.blocks[0].kind, d.blocks[0].bbox = "paragraph", [300.0, 42.5, 550.0, 57.5]
    (typed,) = d.derive()
    assert typed.evidence["title"]["rule"] == "prominent_title_line" and "weak" not in typed.evidence["title"]
    # a picture not read, or the file's repeated picture, is no title evidence
    d.blocks[0].kind, d.blocks[0].bbox, d.blocks[0].status = "image", None, "unread"
    (unread,) = d.derive()
    assert "גבעת השקמים" not in unread.aliases
    d.blocks[0].status = "read"
    (furniture,) = units.derive(d.blocks, d.pages, "v", furniture_images=["a" * 12])
    assert "גבעת השקמים" not in furniture.aliases


# --- R8: signals, certainty and the unsplit file ---------------------------------------------------------------------


def test_a_file_with_no_boundary_signal_is_one_document_unit_not_an_appraisal():
    d = Doc()
    d.page(1, 1)
    d.title_block(1, "שומת מקרקעין — רחוב הדמומית 12, כפר הדמה", "רחוב הדמומית 12")
    d.chapters(1, CHAPTERS[:3])
    d.page(2, 2)
    d.chapters(2, CHAPTERS[3:], top=54.0)
    (only,) = d.derive()
    assert (only.kind, only.certainty, only.unit_no) == (DOCUMENT, UNSPLIT, 1)
    assert (only.block_first, only.block_last, only.page_first, only.page_last) == (0, len(d.blocks) - 1, 1, 2)
    assert only.evidence["kind"]["rule"] == "numbered_chapters"  # the evidence is kept, the file is not called one
    assert only.evidence["boundary"] == {"rule": "file_start", "signals": []}


def test_a_boundary_with_one_signal_is_cut_and_marked_probable():
    d = Doc()
    d.page(1, 1)
    d.title_block(1, "פרויקט שדרות האלון", "שדרות האלון 14")
    d.chapters(1, CHAPTERS[:3])
    d.page(2, 2)
    d.chapters(2, CHAPTERS[3:], top=54.0)
    d.page(3, 1)  # the printed numbering restarts, but the page opens with an ordinary paragraph
    d.add("paragraph", 3, "המשך הדיון בנושאים שונים הנוגעים לנכס.", 48.0)
    d.chapters(3, ["נספח טכני"], top=70.0)
    first, second = d.derive()
    assert second.certainty == PROBABLE and second.evidence["boundary"]["signals"] == [units.LABEL_RESTART]
    assert first.certainty == CERTAIN  # the file's start
    assert second.block_first == next(i for i, b in enumerate(d.blocks) if b.page == 3)


def test_repeated_letterhead_lines_never_open_a_unit_or_become_a_name():
    d = Doc()
    for n in range(1, 7):  # a running header on every page, at the top
        d.page(n, n)
        d.add("paragraph", n, "שמאות מקרקעין דמו — כל הזכויות שמורות", 20.0, h=15.0)
        d.add("paragraph", n, "טלפון 03-0000000", 40.0, h=10.0)
        d.chapters(n, [CHAPTERS[(n - 1) % 6]], top=70.0)
    (only,) = d.derive()
    assert only.unit_no == 1 and only.certainty == UNSPLIT
    assert not only.aliases or all("שמאות" not in a for a in only.aliases)
    # the letterhead of a compendium's title blocks is not an alias of any unit
    for u in compendium().derive():
        assert not any("סינתטי" in a or "שמאות דמו" in a for a in u.aliases)


def test_a_toc_entry_is_a_boundary_signal_for_a_page_that_opens_with_its_title():
    d = Doc()
    d.page(1, 1)
    first = d.add("paragraph", 1, "שומת מקרקעין — מתחם הרימון", 48.0, h=15.0)
    d.add("paragraph", 1, LETTERHEAD[1], 72.0, h=10.0)
    d.add("paragraph", 1, "תוכן עניינים", 90.0, h=12.0)
    d.add("paragraph", 1, "מתחם הרימון ........ 1", 110.0)
    d.add("paragraph", 1, "מתחם התאנה ........ 3", 125.0)
    d.chapters(1, CHAPTERS[:2], top=150.0)
    d.page(2, 2)
    d.chapters(2, CHAPTERS[2:4], top=54.0)
    d.page(3, 3)  # no restart, a different layout (the title sits lower and smaller): only the TOC names it
    d.add("paragraph", 3, "מתחם התאנה", 90.0, h=14.0)
    d.add("paragraph", 3, "משרד: שמאות דמו ב׳ (סינתטי)", 110.0, h=10.0)
    d.chapters(3, CHAPTERS[:2], top=140.0)
    assert d.blocks[first].page == 1
    found = d.derive()
    assert len(found) == 2
    assert found[1].evidence["boundary"]["signals"] == [units.TOC_ENTRY] and found[1].certainty == PROBABLE
    assert "התאנה" in " ".join(found[1].aliases) or found[1].title == "מתחם התאנה"


def test_the_identifier_rule_of_the_appraisal_contexts_is_one_signal_and_needs_no_boxes_or_labels():
    """A file without boxes or page labels (a reading that kept neither): two appraisals found by the restated
    identifiers and the restarting numbering are two units, the second cut with that single signal."""
    d = Doc()
    for n, (street, block, parcel) in enumerate((("רחוב הדמומית 12", "30871", "15"), ("רחוב הצפצפה 7", "30874", "9"))):
        d.add("paragraph", n + 1, f"שומת מקרקעין — {street}, כפר הדמה", None)
        d.add("paragraph", n + 1, f"כתובת הנכס: {street}, כפר הדמה\nגוש: {block} חלקה: {parcel}", None)
        d.chapter = 0
        d.chapters(n + 1, CHAPTERS[:3], top=0.0)
    for b in d.blocks:
        b.bbox = None
    first, second = units.derive(d.blocks, [], "v")
    assert second.evidence["boundary"]["signals"] == [units.IDENTIFIER_RESTART]
    assert (first.certainty, second.certainty) == (CERTAIN, PROBABLE)
    assert first.title == "שומת מקרקעין — רחוב הדמומית 12, כפר הדמה"
    assert first.identifiers["addresses"] and second.identifiers["parcels"] == [["30874", "9"]]
    assert (first.kind, second.kind) == (APPRAISAL, APPRAISAL)


def test_two_independent_signals_make_a_cut_certain():
    d = Doc()
    d.appraisal(1, "שומת מקרקעין — רחוב הדמומית 12", "רחוב הדמומית 12", "30871", "15")
    d.appraisal(3, "שומת מקרקעין — רחוב הצפצפה 7", "רחוב הצפצפה 7", "30874", "9", restart=True)
    first, second = d.derive()
    signals = second.evidence["boundary"]["signals"]
    assert set(signals) == {units.LABEL_RESTART, units.TITLE_BLOCK_REPEAT, units.IDENTIFIER_RESTART}
    assert second.certainty == CERTAIN


def test_page_numbers_come_from_the_label_or_the_page_number_line():
    pages = [page(1, 1), page(2, 2), page(3, 1), page(4, None, printed="4"), page(5, None, printed="iv"),
             SimpleNamespace(page_no=6, printed_label=None, edge="נתונים\nעוד שורה\nטבלה\n12")]
    assert units.page_numbers(pages) == {1: 1, 2: 2, 3: 1, 4: 4, 6: 12}
    assert set(units.label_restarts({1: 1, 2: 2, 3: 1, 4: 2})) == {3}
    assert set(units.label_restarts({2: 1, 3: 2})) == set()  # the first numbered page is not a restart


def test_a_page_number_block_by_position_is_read_even_when_other_lines_follow_it():
    d = Doc()
    d.page(1, None)
    d.add("paragraph", 1, "הנכס נבדק בסיור שנערך לצורך ההערכה.", 100.0)
    d.add("paragraph", 1, "מסמך סינתטי לדמו | עמוד 7", 709.0, h=9.0)  # the footer, then more lines in reading order
    d.add("paragraph", 1, "כתובת המשרד: רחוב הדמיון 3, כפר הדמה", 735.0, h=9.0)
    d.add("paragraph", 1, "טלפון 03-0000000", 748.0, h=9.0)
    d.pages[0].edge = "הנכס נבדק בסיור\nשורה\nשורה\nשורה\nשורה\nשורה\nשורה\nשורה\nשורה\nשורה"  # no number in the edge
    assert units.page_numbers(d.pages, d.blocks) == {1: 7}
    assert units.page_numbers(d.pages) == {}  # without the blocks, nothing in the text edge says it
    d.pages[0].printed_label = "9"
    assert units.page_numbers(d.pages, d.blocks) == {1: 9}  # a stored label comes first
    # a body line that merely mentions a page is not a page-number block, nor is a short block in the middle
    d.pages[0].printed_label = None
    d.blocks[1].bbox = [300.0, 400.0, 550.0, 409.0]
    assert units.page_numbers(d.pages, d.blocks) == {}
    # the text edge reaches past the last lines: the number line is the 6th from the end
    lines = ["שורה"] * 4 + ["מסמך סינתטי לדמו | עמוד 12"] + ["שורה"] * 5
    d.pages[0].edge = "\n".join(lines)
    assert units.page_numbers(d.pages) == {1: 12}


def test_a_numbering_that_drops_places_the_boundary_at_the_first_unnumbered_page_after_the_old_sequence():
    # pages 1-4 are numbered 1-4, pages 5-6 are the next report's cover, page 7 is numbered 3, page 8 numbered 4
    found = units.label_restarts({1: 1, 2: 2, 3: 3, 4: 4, 7: 3, 8: 4})
    assert found == {5: {"rule": "number_drop", "previous_page": 4, "numbered_page": 7}}
    assert units.label_restarts({1: 1, 2: 2, 3: 1}) == {3: {"rule": "number_drop", "previous_page": 2,
                                                            "numbered_page": 3}}
    assert units.label_restarts({1: 5, 2: 6, 4: 2}) .keys() == {3}  # one unnumbered page, then a drop
    d = Doc()
    for n in range(1, 9):
        d.page(n, {1: 1, 2: 2, 3: 3, 4: 4, 7: 3, 8: 4}.get(n))
        d.chapters(n, [CHAPTERS[(n - 1) % 6]], top=60.0)
    first, second = d.derive()
    assert second.page_first == 5 and first.page_last == 4  # the cover pages open the second unit
    assert second.certainty == PROBABLE and second.evidence["boundary"]["signals"] == [units.LABEL_RESTART]
    assert second.evidence["boundary"]["detail"] == {
        "label_restart": {"rule": "number_drop", "previous_page": 4, "numbered_page": 7}}


def test_a_repeated_title_layout_alone_never_cuts_but_with_a_strong_signal_the_cut_is_certain():
    def report(labels: dict[int, int | None]) -> Doc:
        d = Doc()
        for n in range(1, 5):
            d.page(n, labels.get(n))
            d.title_block(n, "פרק " + CHAPTERS[n - 1], letterhead=False)  # every page opens with a title block
            d.add("paragraph", n, f"המועד הקובע לפרק: 0{n}/04/2025", 72.0, h=10.0)
            d.chapters(n, [CHAPTERS[n - 1]], top=150.0)
        return d

    (only,) = report({}).derive()
    assert (only.kind, only.certainty) == (DOCUMENT, UNSPLIT)
    first, second = report({1: 1, 2: 2, 3: 1, 4: 2}).derive()
    assert second.page_first == 3
    assert second.evidence["boundary"]["signals"] == [units.LABEL_RESTART, units.TITLE_BLOCK_REPEAT]
    assert second.certainty == CERTAIN


# --- kind --------------------------------------------------------------------------------------------------------------


def test_a_table_dominant_part_is_comparables_and_a_cover_before_the_first_appraisal_is_front_matter():
    d = Doc()
    d.page(1, None)
    d.title_block(1, "אוסף שומות של משרד דמו")
    d.add("paragraph", 1, "המסמך כולל שלוש שומות ונספח.", 119.0)
    d.add("paragraph", 1, "תוכן עניינים", 135.0, h=12.0)
    d.add("paragraph", 1, "פרויקט שדרות האלון ........ 2", 150.0)  # the cover names where the first appraisal starts
    d.appraisal(2, "פרויקט שדרות האלון", "שדרות האלון 14", "31102", "7", label=1)
    d.appraisal(4, "מתחם גבעת הרקפות", "רחוב הרקפת 8", "31140", "22", label=1)
    d.page(6, 1)
    d.title_block(6, "עסקאות שנאספו")
    for t in range(3):
        d.add("table", 6, "נכס | שטח\nמתחם רכס | 300", 130.0 + 60 * t, h=50.0, table_index=t)
    d.page(7, 2)
    cover, a, b, tables = d.derive()
    assert (cover.kind, a.kind, b.kind, tables.kind) == (FRONT_MATTER, APPRAISAL, APPRAISAL, COMPARABLES)
    assert cover.evidence["kind"]["rule"] == "before_first_appraisal"
    assert tables.evidence["kind"]["rule"] == "table_dominant"


def test_evidence_holds_block_indexes_and_rule_names_only():
    allowed = {"file_start", "prominent_title_line", "project_phrase", "address", "numbered_chapters",
               "appendix_marker", "image_title", "quoted_name", "number_drop", *units.SIGNALS}

    def strings(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield k
                yield from strings(v)
        elif isinstance(node, list):
            for v in node:
                yield from strings(v)
        elif isinstance(node, str):
            yield node

    for u in compendium().derive():
        assert not {s for s in strings(u.evidence)} - allowed - {
            "boundary", "rule", "signals", "block", "page", "title", "aliases", "kind", "blocks", "weak", "detail",
            "previous_page", "numbered_page"}
        assert json.dumps(u.evidence, ensure_ascii=False)


def test_quoted_names_and_project_phrases_become_aliases_with_their_blocks():
    d = Doc()
    d.page(1, 1)
    d.add("paragraph", 1, "הערכת שווי — פרויקט ״גני הצבעוני״", 48.0, h=15.0)
    d.add("paragraph", 1, "מתחם הדקל הצפוני, שדרות הדקל 9", 72.0, h=10.0)
    d.add("paragraph", 1, "מרכז מסחרי קטן באזור", 90.0, h=10.0)
    d.chapters(1, CHAPTERS[:2], top=120.0)
    (only,) = d.derive()
    assert "גני הצבעוני" in only.aliases
    assert {"הדקל הצפוני", "מתחם הדקל הצפוני"} <= set(only.aliases)
    assert "מסחרי" not in " ".join(only.aliases) and "קטן" not in " ".join(only.aliases)
    assert {e["block"] for e in only.evidence["aliases"]} <= {0, 1, 2}


# --- scale and the fixtures -----------------------------------------------------------------------------------------------


def test_a_thousand_pages_of_a_hundred_appraisals_derive_a_hundred_units_quickly():
    d = Doc()
    for c in range(100):
        d.appraisal(1 + 10 * c, f"פרויקט הדולב {c + 1}", f"רחוב הדמיון {c + 1}", str(32000 + 3 * c), str(c + 1),
                    restart=True)
        for p in range(3, 11):
            d.page(10 * c + p, p)
            d.add("paragraph", 10 * c + p, "בסקר שנערך בשטח נמצא כי מצב התחזוקה של המבנה תקין.", 60.0)
    t = time.perf_counter()
    found = d.derive()
    assert len(found) == 100 and all(u.kind == APPRAISAL and u.certainty == CERTAIN for u in found[1:])
    assert time.perf_counter() - t < 5.0
    assert found[7].page_first == 71 and found[7].page_last == 80


def extract(path: Path, vision=None, data: bytes | None = None):
    """A fixture's blocks and pages as the pipeline would store them (no OCR), shaped like the database rows."""
    import time as _time

    from app.platform.pipeline import _blocks_of, _extract

    mime = "application/pdf" if path.suffix == ".pdf" else (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    result = _extract(data if data is not None else path.read_bytes(), mime, _time.monotonic() + 600, vision, None)
    blocks = [SimpleNamespace(block_index=b.index, kind=b.kind, page=b.page, bbox=b.bbox, status=b.status,
                              table_index=b.table_index, text=b.text, label=b.label, content_hash=b.content_hash,
                              page_end=None) for b in _blocks_of(result)]
    for t in result.tables:
        for b in blocks:
            if b.table_index == t.index and b.kind == "table":
                b.page_end = t.page_end
    pages = [SimpleNamespace(page_no=p.page_no, printed_label=p.printed_label,
                             edge=(p.text or "")[:300] + "\n" + (p.text or "")[-300:]) for p in result.pages]
    return blocks, pages


@pytest.fixture(autouse=True)
def no_ocr(monkeypatch):
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)


# files that hold several appraisals; every other fixture is one appraisal (or part of one) and must stay one unit
MULTI = {"R7b_synthetic_two_appraisals_one_file.pdf": 2, "R9a_synthetic_compendium_three_appraisals.pdf": 4,
         "R9b_synthetic_two_similar_names.pdf": 2}
SKIPPED = {"R9d_synthetic_long_file_1000_pages.pdf"}  # a thousand pages: its first 50 are read below


def all_fixtures() -> list[Path]:
    found = [p for p in FIXTURES.rglob("*") if p.suffix in (".pdf", ".docx") and not p.name.startswith("BAD_")
             and p.name not in SKIPPED]
    return sorted(found)


def test_every_single_appraisal_fixture_derives_exactly_one_unit_and_the_multi_appraisal_files_derive_theirs():
    checked = 0
    for path in all_fixtures():
        blocks, pages = extract(path)
        found = units.derive(blocks, pages, "v")
        want = MULTI.get(path.name, 1)
        assert len(found) == want, (path.name, [(u.kind, u.certainty, u.page_first, u.page_last) for u in found])
        assert sum(u.kind == APPRAISAL for u in found) <= (3 if path.name.startswith("R9a") else want), path.name
        if want == 1:
            assert found[0].kind == DOCUMENT and found[0].certainty == UNSPLIT, path.name
        checked += 1
    assert checked >= 80  # the whole set of rounds 1-9 was walked, not a sample


def test_the_two_appraisal_fixture_of_round_7_derives_two_units_with_their_addresses():
    first, second = units.derive(*extract(FIXTURES / "round7" / "R7b_synthetic_two_appraisals_one_file.pdf"), "v")
    assert (first.kind, second.kind) == (APPRAISAL, APPRAISAL)
    assert first.identifiers["parcels"] == [["30871", "15"]] and second.identifiers["parcels"] == [["30874", "9"]]
    assert (first.page_first, first.page_last, second.page_first, second.page_last) == (1, 1, 2, 2)
    assert second.certainty == CERTAIN


def test_the_round_9_fixtures_derive_the_units_of_their_manifest():
    for key, name in (("compendium", "R9a"), ("similar_names", "R9b")):
        entry = MANIFEST[key]
        found = units.derive(*extract(FIXTURES / entry["file"]), "v")
        assert [(u.kind, u.page_first, u.page_last) for u in found] == [
            (e["kind"], e["first_page"], e["last_page"]) for e in entry["units"]], name
        for u, e in zip(found, entry["units"], strict=True):
            assert set(e["aliases"]) <= set(u.aliases), (name, e["id"])
            if e["kind"] == "appraisal":
                assert u.title == e["title"]
    spaced = units.derive(*extract(FIXTURES / MANIFEST["letter_spaced"]["file"]), "v")
    assert len(spaced) == 1


def test_the_picture_title_of_round_9_is_read_by_the_vision_reader_and_stays_a_weak_alias():
    from app.extraction.images import VisionOut
    from tests.unit.test_regions import ScriptedVision

    vision = ScriptedVision(script={(1600, 220): lambda: VisionOut("text", True, "פרויקט גבעת השקמים", [], "", [])})
    (only,) = units.derive(*extract(FIXTURES / MANIFEST["picture_title"]["file"], vision), "v")
    assert only.kind == DOCUMENT and only.certainty == UNSPLIT
    assert {"גבעת השקמים", "השקמים"} <= set(only.aliases)
    assert only.evidence["title"]["weak"] is True
    assert all(e.get("weak") for a, e in zip(only.aliases, only.evidence["aliases"], strict=True) if "השקמים" in a)
    (blind,) = units.derive(*extract(FIXTURES / MANIFEST["picture_title"]["file"]), "v")  # no vision: nothing read
    assert "השקמים" not in " ".join(blind.aliases)


def test_the_first_fifty_pages_of_the_thousand_page_file_derive_five_units():
    entry = MANIFEST["long_file"]
    src = pdfium.PdfDocument((FIXTURES / entry["file"]).read_bytes())
    out = pdfium.PdfDocument.new()
    out.import_pages(src, list(range(50)))
    buf = io.BytesIO()
    out.save(buf)
    found = units.derive(*extract(Path(entry["file"]), data=buf.getvalue()), "v")
    assert [(u.page_first, u.page_last) for u in found] == [(e["first_page"], e["last_page"]) for e in entry["units"][:5]]
    assert [u.title for u in found] == [e["title"] for e in entry["units"][:5]]
    assert all(set(e["aliases"]) <= set(u.aliases) for u, e in zip(found, entry["units"], strict=False))


def test_a_picture_letterhead_opening_every_unit_is_no_title_and_does_not_stop_the_derivation():
    # found on a real office compendium: every report opens with the same read logo picture and no prominent line;
    # the picture is the letterhead, so no picture is left to title the unit (the derivation stopped on it)
    d = Doc()
    for first, name in ((1, "פרויקט שדרות האלון"), (3, "מתחם גבעת הרקפות")):
        d.page(first, 1)
        d.add("image", first, "שמאות דמו בע״מ", 20.0, h=40.0, content_hash=f"{first}" * 64)
        d.add("paragraph", first, name, 70.0, h=11.0)
        d.chapters(first, CHAPTERS[:3])
        d.page(first + 1, 2)
        d.chapters(first + 1, CHAPTERS[3:], top=54.0)
    found = d.derive()
    assert found
    assert all("שמאות דמו" not in (u.title or "") for u in found)
