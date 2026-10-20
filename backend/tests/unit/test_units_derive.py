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
        self.tables = 0

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

    def opening(self, page_no: int, subject: str | None, letter: bool = True, in_table: bool = False) -> int:
        """The run a report opens its first page with (round 15 U4, KTD4): letterhead, a date line, an addressee
        ("לכבוד") block, then the subject line (``הנדון``) as a paragraph or as a row of a table block. Returns the
        index of the block that holds the subject line (None without one)."""
        top = 24.0
        self.add("paragraph", page_no, "שמאות דמו בע״מ — מכתב סינתטי", top, h=10.0)
        if letter:
            self.add("paragraph", page_no, "12/03/2025", top + 18, h=10.0)
            self.add("paragraph", page_no, "לכבוד\nמר דוד דמו\nרחוב הדמיון 3, כפר הדמה", top + 36, h=40.0)
        if subject is None:
            return -1
        if in_table:
            self.tables += 1
            return self.add("table", page_no, f"הנדון | {subject}", top + 90, h=30.0, table_index=self.tables)
        return self.add("paragraph", page_no, f"הנדון: {subject}", top + 90, h=12.0)

    def report(self, first_page: int, pages: int, subject: str | None, letter: bool = True, in_table: bool = False,
               ) -> int:
        """A report of ``pages`` pages with no printed numbers (so no label signal), its first page opened by
        ``opening``."""
        found = -1
        for n in range(pages):
            self.page(first_page + n, None)
            if n == 0:
                found = self.opening(first_page, subject, letter, in_table)
            self.chapters(first_page + n, [CHAPTERS[n % 6]], top=150.0 if n == 0 else 54.0)
        return found

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


def test_the_page_number_is_the_one_after_the_page_word_not_the_digits_of_a_file_path_in_the_footer():
    footers = {1: "\\DOC123\\DATA\\:abc.docx 30996.902. עמוד 3", 2: "\\DOC30100\\DATA\\:abc.docx30996.902. עמוד 20",
               3: "עמ׳ 4", 4: "Page 5", 5: "מסמך | עמוד 6 מתוך 20", 6: "\u200fעמוד 7", 7: "- 8 -"}
    d = Doc()
    for n, footer in footers.items():
        d.page(n, None)
        d.add("paragraph", n, "הנכס נבדק בסיור שנערך לצורך ההערכה.", 100.0)
        d.add("paragraph", n, footer, 709.0, h=9.0)  # a footer of up to 90 characters, in the bottom band
        d.add("paragraph", n, "כתובת המשרד: רחוב הדמיון 3, כפר הדמה", 742.0, h=9.0)
        d.pages[-1].edge = "שורה\nשורה\n" + footer + "\nשורה\nשורה"
    want = {1: 3, 2: 20, 3: 4, 4: 5, 5: 6, 6: 7, 7: 8}
    assert units.page_numbers(d.pages, d.blocks) == want  # by position
    assert units.page_numbers(d.pages) == want  # from the text edge: the same rule
    assert len(footers[2]) > units.PAGE_NUMBER_CHARS - 50
    # a line that only mentions a page, or a path with no page word, states no page number
    for line in ("ראו עמוד 5 לעיל", "\\DOC30100\\DATA\\:abc.docx30996.902.", "שטח 120 מ״ר בקומה 3", "עמוד 3 " + "א" * 90):
        assert units.line_page_number(line) is None, line
    d.pages[0].edge = "ראו עמוד 5 לעיל\nשורה"
    d.blocks[1].bbox = None
    assert 1 not in units.page_numbers(d.pages[:1], d.blocks[:3])


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


def test_only_a_short_unit_before_the_first_appraisal_is_front_matter_a_long_one_is_a_document():
    d = Doc()
    for n in range(1, 6):  # five pages of preface: no chapters of its own, too long to be a cover
        d.page(n, n)
        if n == 1:
            d.title_block(1, "אוסף שומות של משרד דמו")
        d.add("paragraph", n, "טקסט פתיחה כללי שאינו שומה.", 200.0)
    d.appraisal(6, "פרויקט שדרות האלון", "שדרות האלון 14", "31102", "7", label=1)
    d.appraisal(8, "מתחם גבעת הרקפות", "רחוב הרקפת 8", "31140", "22", label=1)
    preface, a, b = d.derive()
    assert (preface.page_first, preface.page_last) == (1, 5)
    assert (preface.kind, a.kind, b.kind) == (DOCUMENT, APPRAISAL, APPRAISAL)
    assert preface.evidence["kind"]["rule"] == "no_kind_evidence"
    short = Doc()
    for n in range(1, units.FRONT_MATTER_PAGES_MAX + 1):  # the longest cover that is still front matter
        short.page(n, n)
        short.add("paragraph", n, "טקסט פתיחה כללי שאינו שומה.", 200.0)
    short.appraisal(units.FRONT_MATTER_PAGES_MAX + 1, "פרויקט שדרות האלון", "שדרות האלון 14", "31102", "7", label=1)
    short.appraisal(units.FRONT_MATTER_PAGES_MAX + 3, "מתחם גבעת הרקפות", "רחוב הרקפת 8", "31140", "22", label=1)
    assert short.derive()[0].kind == FRONT_MATTER


def test_spaced_letters_and_accessibility_boilerplate_are_no_alias_and_no_title():
    d = Doc()
    d.page(1, 1)
    d.add("paragraph", 1, "מסמך PDF נגיש", 48.0, h=15.0)
    d.add("paragraph", 1, "״ב ס ר״ — ״מסמך PDF נגיש״ — ״גני הצבעוני״", 72.0, h=10.0)
    d.add("paragraph", 1, "מתחם הדקל הצפוני, שדרות הדקל 9", 90.0, h=10.0)
    d.chapters(1, CHAPTERS[:2], top=120.0)
    (only,) = d.derive()
    assert "גני הצבעוני" in only.aliases and "הדקל הצפוני" in only.aliases
    assert not [a for a in only.aliases if "pdf" in a.lower() or "ב ס ר" in a or a == "מסמך"]
    assert only.title is None  # the boilerplate line was the most prominent: it is no title
    for junk in ("ב ס ר", "ד ר", "ו ל ט", "מסמך", "מסמך pdf נגיש", "PDF"):
        assert units.is_boilerplate(junk), junk
    for name in ("גבעת הרקפות", "נחל הדס", "מתחם ב", "שדרות האלון 14", "בית א ב"):
        assert not units.is_boilerplate(name), name


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
         "R9b_synthetic_two_similar_names.pdf": 2, "R15a_synthetic_compendium_letter_openings.pdf": 3,
         "R16a_synthetic_compendium_running_lines.pdf": 3,
         # one appraisal whose calculation pages restart their printed numbering: derivation cuts them into a second,
         # unnamed unit; verification treats that unit as the appraisal's own (round 18 U3), derivation stays as it is
         "R18d_synthetic_calculation_pages_colon_heading.pdf": 2, "R18e_synthetic_two_reports_in_one_file.pdf": 2,
         # round 19: the same calculation pages with a title-sized first line (a colon sentence, digit noise) are a second unit,
         # and a third unit is another property of the file; two reports to compare derive two units
         "R19a_synthetic_calculation_pages_sentence_heading.pdf": 3,
         "R19b_synthetic_calculation_pages_digit_noise_heading.pdf": 3, "R19c_synthetic_two_appraisals_to_compare.pdf": 2,
         # round 20: two appraisals to compare (the first states a condition), and two titled by name and address
         "R20d_synthetic_two_appraisals_first_with_a_condition.pdf": 2,
         "R20e_synthetic_two_appraisals_titled_by_name_and_address.pdf": 2}
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


# --- round 15 U4 (KTD4, R6): the subject line and the letter opening of a report ---------------------------------------


def test_the_derivation_is_units_v6_and_a_v6_unit_id_carries_its_tag():
    from app.chat import contexts, coverage

    assert units.UNITS_VERSION == "units-v6" and units.current_tag() == "v6"
    data = {"version_id": "ver", "reading_id": "rd", "unit_no": 3, "units_tag": units.current_tag()}
    assert coverage._unit_key(data) == contexts.unit_key("ver", "rd", 3, "v6") == "ver#rd#3@v6"
    assert coverage._unit_key({"version_id": "ver", "reading_id": None, "unit_no": 3}) == "ver#3"  # a units-v4 id


def test_a_subject_line_inside_a_table_block_at_the_top_of_a_page_starts_a_unit():
    d = Doc()
    d.report(1, 4, "שומת מקרקעין — פרויקט שדרות האלון", letter=False)
    table = d.report(5, 4, "שומת מקרקעין — מתחם גבעת הרקפות", letter=False, in_table=True)
    d.report(9, 4, "שומת מקרקעין — פרויקט נחל הדס", letter=False, in_table=True)
    first, second, third = d.derive()
    assert (first.page_first, first.page_last, second.page_first, second.page_last) == (1, 4, 5, 8)
    assert (third.page_first, third.page_last) == (9, 12)
    # a subject line alone is one strong signal: the cut is marked probable, and the signal is recorded by name
    assert second.certainty == third.certainty == PROBABLE
    assert second.evidence["boundary"]["signals"] == [units.SUBJECT_LINE]
    assert second.evidence["boundary"]["detail"][units.SUBJECT_LINE] == {
        "rule": "page_top_run", "block": table, "in_table": True}
    assert second.block_first == next(i for i, b in enumerate(d.blocks) if b.page == 5)


def test_a_subject_line_with_a_letter_opening_in_the_same_run_is_two_signals_and_the_cut_is_certain():
    d = Doc()
    d.report(1, 4, "שומת מקרקעין — פרויקט שדרות האלון")
    d.report(5, 4, "שומת מקרקעין — מתחם גבעת הרקפות")
    d.report(9, 4, "שומת מקרקעין — פרויקט נחל הדס", in_table=True)  # the subject in a table block, the letter around it
    first, second, third = d.derive()
    assert [u.page_first for u in (first, second, third)] == [1, 5, 9]
    assert second.certainty == third.certainty == CERTAIN
    assert second.evidence["boundary"]["signals"] == [units.SUBJECT_LINE, units.LETTER_OPENING]
    assert third.evidence["boundary"]["signals"] == [units.SUBJECT_LINE, units.LETTER_OPENING]
    assert second.evidence["boundary"]["detail"][units.LETTER_OPENING]["rule"] == "page_top_run"


def test_a_letter_opening_alone_and_a_subject_line_alone_are_each_one_probable_signal():
    d = Doc()
    d.report(1, 4, "שומת מקרקעין — פרויקט שדרות האלון")
    d.report(5, 4, None)  # a date and an addressee, no subject line
    d.report(9, 4, "שומת מקרקעין — פרויקט נחל הדס", letter=False)  # a subject line, no letter
    _, letter_only, subject_only = d.derive()
    assert (letter_only.certainty, letter_only.evidence["boundary"]["signals"]) == (PROBABLE, [units.LETTER_OPENING])
    assert (subject_only.certainty, subject_only.evidence["boundary"]["signals"]) == (PROBABLE, [units.SUBJECT_LINE])


def test_a_label_restart_at_the_same_page_adds_to_the_subject_line():
    d = Doc()
    d.report(1, 4, "שומת מקרקעין — פרויקט שדרות האלון", letter=False)
    d.report(5, 4, "שומת מקרקעין — מתחם גבעת הרקפות", letter=False)
    for n, label in zip(range(1, 9), (1, 2, 3, 4, 1, 2, 3, 4), strict=True):
        d.pages[n - 1] = page(n, label)
    _, second = d.derive()
    assert second.evidence["boundary"]["signals"] == [units.LABEL_RESTART, units.SUBJECT_LINE]
    assert second.certainty == CERTAIN


def test_a_second_opening_run_within_three_pages_of_the_previous_cut_does_not_cut_again():
    # a cover letter with a subject line, then (two pages later) the report's own, different subject line: one unit
    d = Doc()
    d.page(1, None)
    d.opening(1, "מכתב מלווה בעניין הערכת הנכס שברחוב הדמיון")
    d.page(2, None)
    d.add("paragraph", 2, "בכבוד רב, שמאי דמו", 60.0)
    d.report(3, 5, "שומת מקרקעין — פרויקט שדרות האלון", letter=False)
    (only,) = d.derive()
    assert (only.kind, only.certainty) == (DOCUMENT, UNSPLIT)
    assert (only.page_first, only.page_last) == (1, 7)
    # inside a compendium: report 2's cover letter cuts, the report's own subject line two pages later does not
    c = Doc()
    c.report(1, 6, "שומת מקרקעין — פרויקט שדרות האלון")
    c.page(7, None)
    c.opening(7, "מכתב מלווה לשומת גבעת הרקפות")
    c.page(8, None)
    c.add("paragraph", 8, "בכבוד רב, שמאי דמו", 60.0)
    c.report(9, 5, "שומת מקרקעין — מתחם גבעת הרקפות")
    found = c.derive()
    assert [(u.page_first, u.page_last) for u in found] == [(1, 6), (7, 13)]
    assert found[1].evidence["boundary"]["signals"] == [units.SUBJECT_LINE, units.LETTER_OPENING]
    assert found[1].certainty == CERTAIN


def test_a_single_appraisal_whose_appendix_quotes_another_appraisals_subject_line_is_not_cut():
    d = Doc()
    d.report(1, 4, "שומת מקרקעין — פרויקט שדרות האלון")
    # an appendix page that opens with a heading and then quotes a letter, and a page where the quote sits mid-page
    d.page(5, None)
    d.add("heading", 5, "נספח ג׳ — העתק מכתב", 48.0, h=14.0, label="נספח")
    d.add("paragraph", 5, "הנדון: שומת מקרקעין — מתחם גבעת הרקפות", 70.0, h=12.0)
    d.add("paragraph", 5, "לכבוד\nמר דוד דמו", 90.0, h=30.0)
    d.add("paragraph", 5, "12/03/2025", 130.0, h=10.0)
    d.page(6, None)
    for n in range(14):
        d.add("paragraph", 6, f"שורת נספח מספר {n + 1} — נתוני עזר לשומה.", 30.0 + 20 * n)
    d.add("paragraph", 6, "הנדון: שומת מקרקעין — פרויקט נחל הדס", 400.0, h=12.0)
    d.add("paragraph", 6, "לכבוד\nמר דוד דמו", 420.0, h=30.0)
    d.add("paragraph", 6, "12/03/2025", 460.0, h=10.0)
    (only,) = d.derive()
    assert only.certainty == UNSPLIT and (only.page_first, only.page_last) == (1, 6)


def test_body_text_that_begins_with_the_subject_word_is_no_subject_line():
    for line in ("בעניין זה נערך סיור בנכס ובסביבתו.", "הנדון נבדק על ידי השמאי ביום הסיור.", "הנדונה בשומה היא חלקה 7."):
        d = Doc()
        d.report(1, 4, "שומת מקרקעין — פרויקט שדרות האלון")
        d.page(5, None)
        d.add("paragraph", 5, line, 40.0, h=11.0)
        d.chapters(5, ["הערות"], top=100.0)
        (only,) = d.derive()
        assert only.certainty == UNSPLIT, line


def test_a_docx_reading_without_pages_derives_exactly_as_before():
    """A DOCX has no pages and no boxes: the subject line and the letter opening are page-top signals, so a DOCX
    compendium derives as it did before (pages None, and the page 0 some readers use for it)."""

    def docx(subject_lines: bool, page_no):
        d = Doc()
        for name in ("פרויקט שדרות האלון", "מתחם גבעת הרקפות", "פרויקט נחל הדס"):
            if subject_lines:
                d.add("paragraph", page_no, "12/03/2025", None)
                d.add("paragraph", page_no, "לכבוד\nמר דוד דמו", None)
                d.add("paragraph", page_no, f"הנדון: שומת מקרקעין — {name}", None)
            else:
                d.add("paragraph", page_no, f"שומת מקרקעין — {name}", None)
            d.chapters(page_no, CHAPTERS[:3], top=0.0)
        for b in d.blocks:
            b.bbox = None
        return units.derive(d.blocks, [], "v")

    def shape(found):
        return [(u.kind, u.certainty, u.evidence["boundary"]["signals"], u.block_last - u.block_first) for u in found]

    for page_no in (None, 0):
        with_lines = docx(True, page_no)
        assert len(with_lines) == 1
        assert not {units.SUBJECT_LINE, units.LETTER_OPENING} & {
            s for u in with_lines for s in u.evidence["boundary"]["signals"]}
        # the same blocks with the subject lines and letter lines made plain text derive the same way
        assert shape(with_lines)[0][:3] == shape(docx(False, page_no))[0][:3]


def test_the_appendices_that_follow_a_report_stay_in_its_unit_and_the_next_report_cuts_after_them():
    d = Doc()
    d.report(1, 4, "שומת מקרקעין — פרויקט שדרות האלון")
    for p in (5, 6):  # the report's appendices: a heading, tables, no printed numbering of their own
        d.page(p, None)
        d.add("heading", p, "נספח א׳ — עסקאות השוואה" if p == 5 else "נספח ב׳ — תשריט", 48.0, h=14.0, label="נספח")
        d.add("table", p, "נכס | שטח\nמתחם רכס | 300", 90.0, h=300.0, table_index=100 + p)
    d.report(7, 4, "שומת מקרקעין — מתחם גבעת הרקפות")
    first, second = d.derive()
    assert (first.page_first, first.page_last) == (1, 6) and (second.page_first, second.page_last) == (7, 10)
    assert first.kind == APPRAISAL and second.kind == APPRAISAL
    assert second.certainty == CERTAIN


def test_the_round_15_compendium_of_letter_openings_derives_a_unit_per_report_with_certain_cuts():
    """A real PDF (extracted, no OCR): three reports that each open with a letter and a subject line, the second's and
    third's as a row of a ruled table; the printed numbering runs on, so only the opening marks the boundary."""
    manifest = json.loads((FIXTURES / "round15" / "manifest.json").read_text(encoding="utf-8"))["documents"]
    entry = manifest["compendium"]
    found = units.derive(*extract(FIXTURES / entry["file"]), "v")
    assert [(u.page_first, u.page_last) for u in found] == [(e["first_page"], e["last_page"]) for e in entry["units"]]
    for u, e in zip(found[1:], entry["units"][1:], strict=True):
        assert u.certainty == CERTAIN and u.kind == APPRAISAL
        assert u.evidence["boundary"]["signals"] == [units.SUBJECT_LINE, units.LETTER_OPENING]
        assert u.evidence["boundary"]["detail"][units.SUBJECT_LINE].get("in_table") is e["subject_in_table"]


# --- round 16 U4 (KTD5, KTD6, R7, R8): a running-line change starts a report; a lone cut is checked for continuity -------

A_FOOT, B_FOOT = "שמאות דמו א׳ (סינתטי)", "משרד שמאות ב׳ (סינתטי)"
A_HEADER = "שומת מקרקעין — מתחם גבעת הכליל"
B_HEADER = "דוח הערכת שווי — מגדל הים"
B_COVER = ("דוח הערכת שווי", "מגדל הים", "רחוב הים 4, עיר הדמה", "הוכן עבור: דמו החזקות בע״מ")


def run_pages(d: Doc, first: int, count: int, top, foot: str, labels=None, body=None) -> None:
    """Pages whose stored text opens with the running header ``top`` (a text, or a function of the page's place in the
    run) and ends with the running footer ``foot``; ``labels`` are the printed numbers (None: unnumbered)."""
    for i in range(count):
        n = first + i
        header = top(i) if callable(top) else top
        label = None if labels is None else labels[i]
        d.pages.append(SimpleNamespace(page_no=n, printed_label=None if label is None else str(label),
                                       edge=f"{header}\nגוף העמוד\n{foot}"))
        d.chapters(n, [CHAPTERS[i % 6]], top=60.0)
        if body:
            d.add("paragraph", n, body, 130.0, h=11.0)


def cover_page(d: Doc, n: int, title: str, *lines: str) -> None:
    """A cover: few short blocks and a prominent title, no running header or footer."""
    d.pages.append(SimpleNamespace(page_no=n, printed_label=None, edge="\n".join([title, *lines])))
    d.add("paragraph", n, title, 120.0, h=24.0)
    for k, line in enumerate(lines):
        d.add("paragraph", n, line, 170.0 + 20 * k, h=11.0)


def report_a(d: Doc, pages: int = 6) -> None:
    """Report A: its first page opens with a letter and a subject line that names the property; every later page
    carries A's running header, and all of them A's office footer."""
    d.pages.append(SimpleNamespace(page_no=1, printed_label="1", edge=f"שמאות דמו א׳ (סינתטי)\nגוף העמוד\n{A_FOOT}"))
    d.opening(1, A_HEADER)
    d.add("paragraph", 1, "כתובת הנכס: רחוב הכליל 7, כפר הדמה", 150.0, h=11.0)
    d.chapters(1, [CHAPTERS[0]], top=180.0)
    run_pages(d, 2, pages - 1, A_HEADER, A_FOOT, labels=list(range(2, pages + 1)))


def appendix_pages(d: Doc, first: int, exports: int = 3, header: str = "תזרים מזומנים — מתחם גבעת הכליל",
                   address: str | None = None) -> int:
    """A divider page, then cash-flow exports of two pages whose numbering restarts at every export and whose pages
    still name A. Returns the next free page."""
    d.pages.append(SimpleNamespace(page_no=first, printed_label=None, edge=f"נספח א׳ — דוחות תזרים\n{A_FOOT}"))
    d.add("paragraph", first, "נספח א׳ — דוחות תזרים", 120.0, h=24.0)
    page_no = first + 1
    for _ in range(exports):
        for number in (1, 2):
            d.pages.append(SimpleNamespace(page_no=page_no, printed_label=str(number),
                                           edge=f"{header}\nגוף העמוד\n{A_FOOT}"))
            d.add("paragraph", page_no, "נכס: מתחם גבעת הכליל", 60.0, h=11.0)
            d.add("paragraph", page_no, "מתחם גבעת הכליל (סכומים באלפי ₪)", 80.0, h=11.0)
            if address:
                d.add("paragraph", page_no, f"כתובת הנכס: {address}", 100.0, h=11.0)
            d.add("table", page_no, "שנה | הכנסות\nשנה 1 | 1,000", 130.0, h=60.0, table_index=500 + page_no)
            page_no += 1
    return page_no


def spans(found) -> list[tuple[int, int]]:
    return [(u.page_first, u.page_last) for u in found]


def test_a_report_with_a_cover_page_and_its_own_running_lines_starts_at_the_cover():
    d = Doc()
    report_a(d)
    cover_page(d, 7, *B_COVER)
    run_pages(d, 8, 6, B_HEADER, B_FOOT)
    first, second = d.derive()
    assert spans((first, second)) == [(1, 6), (7, 13)]
    boundary = second.evidence["boundary"]
    assert boundary["signals"] == [units.RUNNING_LINE_CHANGE] and second.certainty == PROBABLE
    detail = boundary["detail"][units.RUNNING_LINE_CHANGE]
    assert detail["cover_page"] == 7 and {"other_edge", "cover"} <= set(detail["agrees"])
    assert second.block_first == next(i for i, b in enumerate(d.blocks) if b.page == 7)


def test_a_report_sharing_the_previous_footer_still_starts_at_its_cover_when_only_the_header_changes():
    d = Doc()
    report_a(d)
    cover_page(d, 7, *B_COVER)
    run_pages(d, 8, 6, B_HEADER, B_FOOT)
    cover_page(d, 14, "דוח הערכת שווי", "קניון הגפן", "דרך הגפן 2, עיר הדמה")
    run_pages(d, 15, 6, "דוח הערכת שווי — קניון הגפן", B_FOOT)  # the same firm's footer: only the header changes
    found = d.derive()
    assert spans(found) == [(1, 6), (7, 13), (14, 20)]
    detail = found[2].evidence["boundary"]["detail"][units.RUNNING_LINE_CHANGE]
    assert detail["agrees"] == ["cover"] and detail["cover_page"] == 14


def test_a_cover_that_names_only_a_subject_and_no_report_type_is_a_cover():
    d = Doc()
    report_a(d)
    cover_page(d, 7, "פרויקט גני הצבעוני", "שדרות הדקל 9, עיר הדמה")
    run_pages(d, 8, 5, "הערכה — גני הצבעוני", A_FOOT)  # a new header over the same footer
    found = d.derive()
    assert spans(found) == [(1, 6), (7, 12)]
    assert found[1].evidence["boundary"]["detail"][units.RUNNING_LINE_CHANGE]["agrees"] == ["cover"]


def test_a_short_divider_page_is_not_a_cover_so_the_cut_opens_at_the_first_page_of_the_new_run():
    d = Doc()
    report_a(d)
    cover_page(d, 7, "פרק נוסף", "חלק שני")  # a divider: a short page whose title names no report type and no subject
    run_pages(d, 8, 6, B_HEADER, B_FOOT)
    found = d.derive()
    assert spans(found) == [(1, 7), (8, 13)]  # the divider stays where it is: it is not backtracked over
    detail = found[1].evidence["boundary"]["detail"][units.RUNNING_LINE_CHANGE]
    assert detail["agrees"] == ["other_edge"] and "cover_page" not in detail
    appendix = Doc()
    report_a(appendix)
    cover_page(appendix, 7, "נספח ב׳ — דוחות", "חלק שני")  # an appendix title is never a cover
    run_pages(appendix, 8, 6, B_HEADER, B_FOOT)
    assert spans(appendix.derive()) == [(1, 7), (8, 13)]


def test_a_new_run_of_one_edge_alone_cuts_nothing_but_a_page_number_restart_agrees_with_it():
    def file(labels) -> Doc:
        d = Doc()
        run_pages(d, 1, 6, "שומה — מתחם הדקל הצפוני", A_FOOT, labels=list(range(1, 7)))
        run_pages(d, 7, 6, "הערכה — מתחם גבעת הרקפות", A_FOOT, labels=labels)  # the header changes; the footer does not
        return d

    assert spans(file(list(range(7, 13))).derive()) == [(1, 12)]  # a header alone: no cover, no restart, no other edge
    cut = file([1, 2, 3, 4, 5, 6]).derive()
    assert spans(cut) == [(1, 6), (7, 12)]
    assert cut[1].evidence["boundary"]["signals"] == [units.LABEL_RESTART, units.RUNNING_LINE_CHANGE]
    assert cut[1].certainty == CERTAIN
    assert cut[1].evidence["boundary"]["detail"][units.RUNNING_LINE_CHANGE]["agrees"] == ["page_number_restart"]


def test_both_edges_changing_together_cut_at_the_first_page_of_the_new_runs_with_no_cover():
    d = Doc()
    run_pages(d, 1, 6, "שומה — מתחם הדקל הצפוני", A_FOOT)
    run_pages(d, 7, 6, B_HEADER, B_FOOT)
    found = d.derive()
    assert spans(found) == [(1, 6), (7, 12)]
    assert found[1].evidence["boundary"]["detail"][units.RUNNING_LINE_CHANGE]["agrees"] == ["other_edge"]


def test_a_run_shorter_than_the_minimum_is_no_boundary():
    d = Doc()
    run_pages(d, 1, 6, "שומה — מתחם הדקל הצפוני", A_FOOT)
    run_pages(d, 7, units.RUN_MIN_PAGES - 1, B_HEADER, B_FOOT)  # a few pages: a stamp, a short insert, not a report
    run_pages(d, 7 + units.RUN_MIN_PAGES - 1, 6, "שומה — מתחם הדקל הצפוני", A_FOOT)
    assert len(d.derive()) == 1


def test_chapter_running_headers_that_change_inside_a_report_cut_nothing():
    d = Doc()
    report_a(d)
    cover_page(d, 7, *B_COVER)
    for k, chapter in enumerate(("מבוא", "ניתוח שוק", "הערכת שווי")):  # the header gains the chapter's name
        run_pages(d, 8 + 4 * k, 4, f"{B_HEADER} | {chapter}", B_FOOT)
    found = d.derive()
    assert spans(found) == [(1, 6), (7, 19)]
    assert found[1].evidence["boundary"]["signals"] == [units.RUNNING_LINE_CHANGE]  # only the report's own start


def test_a_comparables_table_that_names_other_properties_cuts_nothing():
    d = Doc()
    run_pages(d, 1, 8, B_HEADER, B_FOOT)
    d.add("table", 4, "נכס | שטח | מחיר\nמגדל השקד, רחוב השקד 3 | 310 | 4,100,000\nקניון הגפן | 280 | 3,700,000", 140.0,
          h=80.0, table_index=900)
    d.add("paragraph", 5, "העסקאות בנכס פארק האורן, דרך האורן 6, גוש 33140 חלקה 8 נבחרו להשוואה.", 140.0, h=11.0)
    (only,) = d.derive()
    assert only.certainty == UNSPLIT and (only.page_first, only.page_last) == (1, 8)


def test_a_running_line_that_is_only_a_page_number_or_a_date_is_ignored():
    for line in ("עמוד 12", "12", "- 7 -", "Page 3 of 40", "12/03/2025", "12 מרץ 2025", "3 עמוד", ""):
        assert units.running_line(line) is None, line
    assert units.running_line("שמאות דמו א׳ (סינתטי) | עמוד 12") == units.running_line("שמאות דמו א׳ (סינתטי) | עמוד 13")
    assert units.running_line("Cedar Quay · Page 3") == units.running_line("Cedar Quay · Page 14")
    d = Doc()
    for n in range(1, 13):  # the top line of every page is a date, then another date, and the bottom a page number
        d.pages.append(SimpleNamespace(page_no=n, printed_label=None,
                                       edge=f"{'12/03/2025' if n <= 6 else '13/03/2025'}\nגוף העמוד\nעמוד {n}"))
        d.chapters(n, [CHAPTERS[n % 6]], top=60.0)
    assert len(d.derive()) == 1


def test_a_file_where_every_report_shares_one_footer_cuts_at_subject_lines_as_before():
    d = Doc()
    for first, subject in ((1, "שומת מקרקעין — פרויקט שדרות האלון"), (5, "שומת מקרקעין — מתחם גבעת הרקפות"),
                           (9, "שומת מקרקעין — פרויקט נחל הדס")):
        for n in range(4):
            d.pages.append(SimpleNamespace(page_no=first + n, printed_label=None, edge=f"גוף העמוד\n{A_FOOT}"))
        d.opening(first, subject, letter=False)
        for n in range(4):
            d.chapters(first + n, [CHAPTERS[n]], top=150.0 if n == 0 else 54.0)
    found = d.derive()
    assert spans(found) == [(1, 4), (5, 8), (9, 12)]
    assert all(u.evidence["boundary"]["signals"] == [units.SUBJECT_LINE] for u in found[1:])


def test_appendix_exports_that_restart_the_numbering_and_still_name_the_report_stay_in_its_unit():
    d = Doc()
    report_a(d)
    end = appendix_pages(d, 7)
    # without the continuity check the divider and each export would cut on the number drops and the new header run
    (only,) = d.derive()
    assert (only.page_first, only.page_last) == (1, end - 1)
    assert only.certainty == UNSPLIT and only.evidence["boundary"]["signals"] == []


def test_an_export_that_restarts_the_numbering_and_names_another_property_is_still_a_cut():
    d = Doc()
    report_a(d)
    appendix_pages(d, 7, exports=1)
    # a second export of other property: its pages carry another address, so a lone restart is a real boundary
    d.pages.append(SimpleNamespace(page_no=10, printed_label="1", edge="נספח א׳\nגוף העמוד\nמשרד אחר"))
    d.add("paragraph", 10, "כתובת הנכס: רחוב הדקל 3, כפר הדמה", 60.0, h=11.0)
    d.add("paragraph", 10, "מתחם גבעת הכליל (השוואה)", 80.0, h=11.0)  # it even mentions A: an identifier differs
    d.chapters(10, [CHAPTERS[1]], top=100.0)
    found = d.derive()
    assert [u.page_first for u in found] == [1, 10]
    assert found[1].evidence["boundary"]["signals"] == [units.LABEL_RESTART]


def test_a_lone_number_restart_whose_pages_name_the_previous_report_is_not_a_cut():
    d = Doc()
    report_a(d, pages=4)
    for n in range(5, 9):  # a restarted numbering over four more pages, no running lines, naming A's property
        d.pages.append(SimpleNamespace(page_no=n, printed_label=str(n - 4), edge="גוף העמוד"))
        d.chapters(n, [CHAPTERS[n % 6]], top=100.0)
    d.add("paragraph", 5, "המשך הדוח בעניין מתחם גבעת הכליל", 60.0, h=11.0)
    (only,) = d.derive()
    assert only.certainty == UNSPLIT and (only.page_first, only.page_last) == (1, 8)


def test_the_running_subject_of_the_previous_unit_is_a_name_the_continuity_check_knows():
    def file(opening: str) -> Doc:
        d = Doc()
        run_pages(d, 1, 6, A_HEADER, A_FOOT, labels=list(range(1, 7)))  # no subject line, no title: only the header names A
        for n in range(7, 11):  # a restarted numbering, no running lines of its own
            d.pages.append(SimpleNamespace(page_no=n, printed_label=str(n - 6), edge="גוף העמוד"))
            d.chapters(n, [CHAPTERS[n % 6]], top=100.0)
        d.add("paragraph", 7, opening, 60.0, h=11.0)
        return d

    (only,) = file("נספח: תחשיב עבור מתחם גבעת הכליל").derive()
    assert (only.page_first, only.page_last) == (1, 10)
    assert [u.page_first for u in file("נספח: תחשיב עבור נכס אחר").derive()] == [1, 7]  # it names nothing of A: a cut


def test_a_report_start_with_no_running_lines_and_a_new_address_is_not_dropped_by_the_continuity_check():
    d = Doc()
    report_a(d, pages=4)
    for n in range(5, 9):
        d.pages.append(SimpleNamespace(page_no=n, printed_label=str(n - 4), edge="גוף העמוד"))
        d.chapters(n, [CHAPTERS[n % 6]], top=100.0)
    d.add("paragraph", 5, "כתובת הנכס: רחוב הדקל 3, כפר הדמה", 40.0, h=11.0)
    found = d.derive()
    assert [u.page_first for u in found] == [1, 5]


def test_a_thousand_pages_with_running_headers_that_change_every_ten_pages_stay_fast():
    d = Doc()
    for block in range(100):
        run_pages(d, 1 + 10 * block, 10, f"{B_HEADER} {block % 7} | פרק {block}", B_FOOT if block % 2 else A_FOOT,
                  labels=list(range(1, 11)))
    t = time.perf_counter()
    found = d.derive()
    assert time.perf_counter() - t < 5.0
    assert found  # every block restarts the numbering and changes the footer: cuts, derived linearly


# --- round 16 U5 (KTD6, R9, AE4): a unit's identity includes its running subject and its cover title ---------------------


def names_of(u) -> set[str]:
    return {x for x in (u.title, *u.aliases) if x}


def test_a_running_header_with_a_report_type_and_a_subject_names_the_unit_by_the_subject():
    d = Doc()
    run_pages(d, 1, 6, "שומה — מתחם הדקל הצפוני", A_FOOT)
    run_pages(d, 7, 6, B_HEADER, B_FOOT)
    first, second = d.derive()
    assert first.title == units._norm("מתחם הדקל הצפוני") and second.title == units._norm("מגדל הים")
    assert units._norm("שומה") not in names_of(first) and units._norm("דוח הערכת שווי") not in names_of(second)
    assert first.evidence["title"] == {"rule": "running_subject", "edge": "top"}
    assert len(first.aliases) == len(first.evidence["aliases"])
    assert all(e["rule"] != "running_subject" for e in first.evidence["aliases"])  # the office footer is no name
    assert units._norm("שמאות דמו א") not in " ".join(names_of(first)) and "סינתטי" not in names_of(first)
    assert "מגדל הים" not in json.dumps(second.evidence, ensure_ascii=False)  # evidence holds rule names, no copied text


def test_a_running_subject_is_an_alias_when_the_unit_has_a_title_block():
    d = Doc()
    d.page(1, 1)
    d.title_block(1, "פרויקט גני הצבעוני", "רחוב הדקל 3", "31102", "7")
    d.chapters(1, CHAPTERS[:3])
    run_pages(d, 2, 5, "שומה — גני הצבעוני הדרומיים", A_FOOT)
    run_pages(d, 7, 6, B_HEADER, B_FOOT)
    first, second = d.derive()
    assert first.title == "פרויקט גני הצבעוני" and units._norm("גני הצבעוני הדרומיים") in first.aliases
    assert second.title == units._norm("מגדל הים")


def test_a_running_line_every_unit_shares_gives_no_name():
    d = Doc()
    for first, subject in ((1, "שומת מקרקעין — פרויקט שדרות האלון"), (5, "שומת מקרקעין — מתחם גבעת הרקפות"),
                           (9, "שומת מקרקעין — פרויקט נחל הדס")):
        for n in range(4):
            d.pages.append(SimpleNamespace(page_no=first + n, printed_label=None,
                                           edge=f"שומה — משרד הדמו המשותף\nגוף העמוד\n{A_FOOT}"))
        d.opening(first, subject, letter=False)
        for n in range(4):
            d.chapters(first + n, [CHAPTERS[n]], top=150.0 if n == 0 else 54.0)
    found = d.derive()
    assert len(found) == 3
    for u in found:
        blob = " ".join(names_of(u))
        assert "משרד הדמו המשותף" not in blob and "שמאות דמו" not in blob
        assert all(e["rule"] != "running_subject" for e in u.evidence["aliases"])
        assert u.evidence["title"]["rule"] != "running_subject"


def test_chapter_names_joined_to_a_constant_top_segment_give_no_name_but_the_constant_subject_does():
    d = Doc()
    run_pages(d, 1, 6, lambda i: f"שומה — מתחם הדקל הצפוני | {CHAPTERS[i]}", A_FOOT, labels=list(range(1, 7)))
    run_pages(d, 7, 6, lambda i: f"דוח הערכת שווי — מגדל הים | {CHAPTERS[i]}", B_FOOT, labels=[1, 2, 3, 4, 5, 6])
    first, second = d.derive()
    assert units.RUNNING_LINE_CHANGE in second.evidence["boundary"]["signals"]
    assert names_of(second) == {units._norm("מגדל הים")}  # not a chapter's name, not the report type
    assert second.evidence["title"] == {"rule": "running_subject", "edge": "top"} and second.aliases == ()
    assert names_of(first) == {units._norm("מתחם הדקל הצפוני")}


def test_a_bottom_office_line_and_a_top_line_with_contact_details_give_no_alias():
    d = Doc()
    run_pages(d, 1, 6, "שומה — מתחם הדקל הצפוני", "משרד שמאות אלפא בע״מ office-a@firm.example טל 03-5551234",
              labels=list(range(1, 7)))
    run_pages(d, 7, 6, "משרד שמאות גמא בע״מ gamma@firm.example", "משרד שמאות בטא בע״מ www.beta-firm.example",
              labels=[1, 2, 3, 4, 5, 6])
    first, second = d.derive()
    assert [(u.page_first, u.page_last) for u in (first, second)] == [(1, 6), (7, 12)]
    assert names_of(first) == {units._norm("מתחם הדקל הצפוני")}   # the unit-unique bottom office line names nothing
    assert names_of(second) == set()   # a top line with an e-mail is the office's, not a header
    for line, contact in (("טל 03-5551234 | משרד אלפא", True), ("www.firm.example", True), ("office@firm.example", True),
                          ("דוח 2020-2021 מתחם הדקל", False), ("מגדל הים", False)):
        assert units._has_contact(line) is contact, line


def test_a_cover_title_names_a_unit_that_has_no_running_line_and_no_title_run():
    d = Doc()
    d.appraisal(1, "פרויקט שדרות האלון", "רחוב האלון 4", "31102", "7")
    d.page(3, 1)  # the numbering restarts at the cover: the only signal
    d.add("heading", 3, "מסמך לעיון בלבד", 60.0, h=11.0)  # a heading before the title ends the title run
    d.add("paragraph", 3, "פרויקט גני הצבעוני", 120.0, h=24.0)
    d.add("paragraph", 3, "שדרות הדקל 9, עיר הדמה", 170.0, h=11.0)
    d.chapters(3, CHAPTERS[:2], top=220.0)
    d.page(4, 2)
    d.chapters(4, CHAPTERS[2:4], top=54.0)
    first, second = d.derive()
    assert (second.page_first, second.page_last) == (3, 4)
    assert second.title == "פרויקט גני הצבעוני" and second.evidence["title"]["rule"] == "cover_title"
    assert "block" in second.evidence["title"] and first.title == "פרויקט שדרות האלון"


def untitled_appendix_then_restarts(second_export: str) -> Doc:
    """Report A (named), an appendix export whose running lines change to a printout header that does not name A and that
    has no title of its own, then two exports with no running lines whose numbering restarts and whose opening lines
    name ``second_export`` for the second."""
    d = Doc()
    report_a(d)
    run_pages(d, 7, 4, "תדפיס מערכת פנימי", "מחולל הדוחות הפנימי", labels=[1, 2, 3, 4])
    for first, opening in ((11, "המשך תחשיב — מתחם גבעת הכליל"), (13, second_export)):
        for n, number in ((first, 1), (first + 1, 2)):
            d.pages.append(SimpleNamespace(page_no=n, printed_label=str(number), edge=""))
            d.chapters(n, [CHAPTERS[n % 6]], top=100.0)
        d.add("paragraph", first, opening, 60.0, h=11.0)
    return d


def test_restarted_exports_that_name_the_report_join_an_untitled_appendix_unit_that_follows_it():
    found = untitled_appendix_then_restarts("המשך תחשיב — מתחם גבעת הכליל").derive()
    assert spans(found) == [(1, 6), (7, 14)]  # the two restarts are dropped; the printout header still starts its unit
    assert found[1].evidence["boundary"]["signals"] == [units.LABEL_RESTART, units.RUNNING_LINE_CHANGE]
    assert found[0].title and not found[1].title


def test_a_restarted_export_that_names_another_property_still_cuts_after_an_untitled_appendix_unit():
    found = untitled_appendix_then_restarts("תחשיב עבור נכס אחר — מתחם הדקל הצפוני").derive()
    assert spans(found) == [(1, 6), (7, 12), (13, 14)]
    assert found[2].evidence["boundary"]["signals"] == [units.LABEL_RESTART]


def test_the_round_16_compendium_derives_a_unit_per_report_starting_at_each_cover():
    """A real PDF (extracted, no OCR): report A with appendices that restart the numbering and still name A, then
    reports B and C, each with a cover page and a running header, no subject line and no letter; B's chapters change
    the header; B's comparables table names two other properties."""
    entry = json.loads((FIXTURES / "round16" / "manifest.json").read_text(encoding="utf-8"))["documents"]["compendium"]
    found = units.derive(*extract(FIXTURES / entry["file"]), "v")
    assert spans(found) == [(e["first_page"], e["last_page"]) for e in entry["units"]]
    a, b, c = found
    for u, e in ((b, entry["units"][1]), (c, entry["units"][2])):
        boundary = u.evidence["boundary"]
        assert boundary["signals"] == [units.RUNNING_LINE_CHANGE]
        assert boundary["detail"][units.RUNNING_LINE_CHANGE]["cover_page"] == e["cover_page"] == u.page_first
    assert a.page_last == min(entry["appendix_pages"]) - 1 + len(entry["appendix_pages"])  # A holds its appendices
    assert all(a.page_first <= p <= a.page_last for p in entry["appendix_pages"])
    chapter_pages = [p for pages_ in entry["chapter_pages_of_b"].values() for p in pages_]
    assert all(b.page_first < p <= b.page_last for p in chapter_pages)
    assert b.page_first < entry["comparables_page"] <= b.page_last
    assert json.dumps(b.evidence, ensure_ascii=False)
