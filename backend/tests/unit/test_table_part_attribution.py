"""The evidence that attributes a paragraph beside a table to that table (round 18 U4, KTD5, R9; AE7): adjacency (no block between
the paragraph and the table), the page (the table's own or the page next to it) and wording (a small closed set of reference
words that point back to the table, or forward to it). A reference word is never read alone, and adjacency is never enough without
it. Synthetic text only."""

from __future__ import annotations

import pytest

from app.chat import tools as T
from app.chat.evidence import TABLE_BETWEEN, TABLE_HEADING_LIKE, TABLE_NO_REFERENCE, TABLE_NOT_ADJACENT

BACK = "הטבלה שלעיל מתייחסת לגישת היוון ההכנסות בלבד."
FORWARD = "הטבלה הבאה מתייחסת לגישת היוון ההכנסות בלבד."
PLAIN = "הנתונים נאספו מדוחות השוק האחרונים."


def after(text: str = BACK, *, gap: int = 0, page_gap: int | None = 0, between: bool = False) -> T.TablePart:
    return T.TablePart("after", "paragraph", 47, text, between, 1, gap, page_gap, T.reference_of(text))


def before(text: str = FORWARD, *, gap: int = 0, page_gap: int | None = 0, between: bool = False) -> T.TablePart:
    return T.TablePart("before", "paragraph", 40, text, between, 1, gap, page_gap, T.reference_of(text))


@pytest.mark.parametrize("text, direction", [
    ("הטבלה שלעיל מתייחסת לגישה אחת", "back"),
    ("נתוני הטבלה לעיל הם באלפי ₪", "back"),
    ("הטבלה הנ\"ל מתייחסת לשנת 2024", "back"),
    ("הטבלה הנ״ל מתייחסת לשנת 2024", "back"),
    ("הטבלה האמורה מתייחסת לשנת 2024", "back"),
    ("הטבלה שלהלן מציגה את השווי", "forward"),
    ("הטבלה הבאה מציגה את השווי", "forward"),
    ("להלן טבלה המציגה את השווי", "forward"),
    ("כדלקמן הטבלה של התרחישים", "forward"),
    ("הטבלה שלעיל והטבלה שלהלן", "both"),
    ("הנתונים שלעיל נאספו מדוחות השוק", ""),  # a reference word without a table
    ("הטבלה מתייחסת לשנת 2024", ""),  # a table without a reference word
    ("הטבלה זו מתייחסת לשנת 2024", ""),
    ("", ""),
])
def test_a_paragraph_points_to_the_table_only_by_a_reference_word_together_with_the_table(text, direction):
    assert T.reference_of(text) == direction


def test_an_adjacent_paragraph_after_the_table_on_its_page_that_points_back_is_attributed():
    assert after().reasons == () and after().attributed


def test_the_same_paragraph_without_a_back_reference_is_not_attributed_and_says_why():
    assert after(PLAIN).reasons == ("no_reference",) and not after(PLAIN).attributed
    assert after("הטבלה מתייחסת לגישת היוון ההכנסות בלבד.").reasons == ("no_reference",)


def test_a_paragraph_after_the_table_with_a_forward_reference_is_not_attributed_to_it():
    assert after(FORWARD).reasons == ("no_reference",)
    assert before(BACK).reasons == ("no_reference",)


def test_a_back_referencing_paragraph_with_a_block_between_it_and_the_table_is_not_attributed():
    assert after(gap=1).reasons == ("not_adjacent",)
    assert before(gap=2).reasons == ("not_adjacent",)


def test_a_paragraph_on_the_page_next_to_the_table_is_adjacent_but_one_further_away_is_not():
    assert after(page_gap=1).reasons == ()
    assert before(page_gap=1).reasons == ()
    assert after(page_gap=2).reasons == ("not_adjacent",)
    assert after(page_gap=None).reasons == ()  # a Word file has no pages: only the blocks tell


def test_a_paragraph_that_lies_between_two_tables_is_attributed_when_it_points_to_this_one_and_not_otherwise():
    assert after(between=True).reasons == () and before(between=True).reasons == ()
    assert after(PLAIN, between=True).reasons == ("between", "no_reference")
    assert after(gap=3, between=True).reasons == ("between", "not_adjacent")
    assert after(PLAIN, gap=3, between=True).reasons == ("between", "not_adjacent", "no_reference")


def test_a_heading_like_label_is_attributed_by_the_headings_scope_whatever_its_wording():
    label = T.TablePart("before", "paragraph", 38, "4. גישת היוון ההכנסות", False, 1, 1, 0, T.reference_of("4. גישת היוון"),
                        heading_like=True)
    assert label.reasons == () and label.attributed


def test_a_part_built_without_evidence_is_attributed_unless_it_lies_between_two_tables():
    assert T.TablePart("after", "paragraph", 47, PLAIN, False).reasons == ()
    assert T.TablePart("after", "paragraph", 47, PLAIN, True).reasons == ("between",)


def test_the_tag_of_a_part_carries_every_reason_it_is_not_attributed_and_the_mark_of_a_heading_like_label():
    marks = lambda p: T._part_line(p).split(" · בלוק")[0]  # noqa: E731 - the tag up to the block reference
    assert marks(after()) == "[אחרי הטבלה · פסקה"
    assert marks(after(PLAIN, gap=2, between=True)) == (
        f"[אחרי הטבלה · {TABLE_BETWEEN} · {TABLE_NOT_ADJACENT} · {TABLE_NO_REFERENCE} · פסקה")
    label = T.TablePart("before", "paragraph", 38, "4. גישת היוון ההכנסות", False, 1, 1, 0, "", heading_like=True)
    assert marks(label) == f"[לפני הטבלה · {TABLE_HEADING_LIKE} · פסקה"
