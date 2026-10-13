"""The structure of a standard, derived from its stored blocks (round 10 U4: R3, R21–R24; KTD9, KTD11).

``derive`` is a pure function of the blocks of one reading: sections with parents, depths, block and page ranges and a
role; the condition and exception sentences of each section (``limits``); the internal and external references it
mentions (``refs``); the modality counts. ``text_digest`` is the hash of the normalized text of all blocks, the same for
the same text however the reader cut it into blocks. Every text here is synthetic and says so; the standard numbers are
invented (98.x, 97.x, 99)."""

from __future__ import annotations

from types import SimpleNamespace

from app.professional import sections as S

MARK = "תקן בדיקה סינתטי — אינו תקן אמיתי"


def blk(i, kind, text, label=None, path=(), page=1):
    """A stored block row, with the columns ``derive`` reads."""
    return SimpleNamespace(block_index=i, kind=kind, label=label, section_path=list(path), paragraph_no=None, page=page,
                           text=text, status="read")


def doc(*rows):
    """Blocks numbered in order from ``(kind, text, label, path, page)`` tuples."""
    return [blk(i, *row) for i, row in enumerate(rows)]


def H(text, label=None, path=None, page=1):
    return ("heading", text, label, [text] if path is None else path, page)


def P(text, path=(), page=1):
    return ("paragraph", text, None, path, page)


def by_label(secs):
    return {s.label: s for s in secs if s.label}


def standard():
    """A synthetic standard: a preamble line, sections 1, 2, 2.1, 2.2, a definitions section with three terms, and an
    appendix, over three pages."""
    return doc(
        P(MARK),                                                           # 0
        H("1. כללי", "1"),                                                 # 1
        P("תקן זה מסדיר את מדידת השטח.", ["1. כללי"]),                      # 2
        H("2. דרישות", "2"),                                               # 3
        P("על השמאי למדוד את השטח במלואו.", ["2. דרישות"]),                 # 4
        H("2.1 מדידה", "2.1", ["2. דרישות", "2.1 מדידה"]),                   # 5
        P("השטח נמדד בקווי המעטפת החיצוניים.", ["2. דרישות", "2.1 מדידה"]),  # 6
        H("2.2 תיעוד", "2.2", ["2. דרישות", "2.2 תיעוד"], page=2),          # 7
        P("מומלץ לצרף תרשים לדוגמה.", ["2. דרישות", "2.2 תיעוד"], page=2),   # 8
        H("הגדרות", None, ["הגדרות"], page=2),                              # 9
        P("שטח נטו – השטח הפנימי הסינתטי.", ["הגדרות"], page=2),             # 10
        P("שטח ברוטו - השטח הכולל הסינתטי.", ["הגדרות"], page=2),            # 11
        P("מעטפת — קו החיצוני הסינתטי.", ["הגדרות"], page=2),                # 12
        H("נספח א'", None, ["נספח א'"], page=3),                            # 13
        P("טבלת התאמות סינתטית.", ["נספח א'"], page=3),                     # 14
    )


# --- sections -------------------------------------------------------------------------------------------------

def test_sections_get_labels_parents_depths_roles_and_ranges():
    secs = S.derive(standard())
    assert [(s.section_no, s.label, s.role, s.depth, s.parent_no) for s in secs] == [
        (1, None, "preamble", 0, None),
        (2, "1", "chapter", 1, None),
        (3, "2", "chapter", 1, None),
        (4, "2.1", "section", 2, 3),
        (5, "2.2", "section", 2, 3),
        (6, None, "definitions", 1, None),
        (7, "שטח נטו", "definition", 2, 6),
        (8, "שטח ברוטו", "definition", 2, 6),
        (9, "מעטפת", "definition", 2, 6),
        (10, "נספח א'", "appendix", 1, None),
    ]
    b = {s.section_no: s for s in secs}
    assert (b[1].block_first, b[1].block_last) == (0, 0)
    assert (b[2].block_first, b[2].block_last) == (1, 2)
    # a section runs to the next sibling or a higher heading, so "2" holds its subsections' blocks
    assert (b[3].block_first, b[3].block_last) == (3, 8)
    assert (b[4].block_first, b[4].block_last) == (5, 6)
    assert (b[5].block_first, b[5].block_last) == (7, 8)
    assert (b[6].block_first, b[6].block_last) == (9, 12)
    assert (b[7].block_first, b[7].block_last) == (10, 10)
    assert (b[10].block_first, b[10].block_last) == (13, 14)
    assert (b[3].page_first, b[3].page_last) == (1, 2)
    assert (b[10].page_first, b[10].page_last) == (3, 3)
    assert b[4].title == "מדידה" and b[10].title == "נספח א'" and b[6].title == "הגדרות"


def test_a_derivation_is_repeatable_and_holds_plain_data():
    assert S.derive(standard()) == S.derive(standard())
    s = S.derive(standard())[3]
    assert isinstance(s.limits, tuple) and isinstance(s.refs, tuple) and isinstance(s.modality, dict)


def test_a_docx_heading_keeps_its_number_in_the_label_not_in_the_text():
    secs = S.derive(doc(
        H("כללי", "1.", ["כללי"]),
        H("מדידה", "1.1.", ["כללי", "מדידה"]),
        P("תוכן סינתטי.", ["כללי", "מדידה"]),
    ))
    assert [(s.label, s.title, s.depth, s.parent_no) for s in secs] == [("1", "כללי", 1, None), ("1.1", "מדידה", 2, 1)]


def test_a_letter_numbered_heading_takes_its_depth_from_the_heading_path():
    secs = S.derive(doc(
        H("כללי", "1", ["כללי"]),
        H("מדידה", "א", ["כללי", "מדידה"]),
        H("תיעוד", "ב", ["כללי", "תיעוד"]),
    ))
    assert [(s.label, s.depth, s.parent_no) for s in secs] == [("1", 1, None), ("א", 2, 1), ("ב", 2, 1)]


def test_a_label_is_taken_from_the_blocks_own_text_or_numbering_only():
    secs = S.derive(doc(
        H("מדידה", None, ["מדידה"]),
        P("ראה סעיף 5 ונספח ג'.", ["מדידה"]),
        H("3.4 מספר בכותרת", None, ["3.4 מספר בכותרת"]),
    ))
    assert [s.label for s in secs] == [None, "3.4"]
    assert not [s for s in secs if s.label == "5"]  # a mentioned section is no section


def test_an_unnumbered_heading_does_not_take_a_number_from_its_position():
    secs = S.derive(doc(H("רקע", None, ["רקע"]), H("מטרה", None, ["רקע", "מטרה"])))
    assert [(s.label, s.depth, s.parent_no) for s in secs] == [(None, 1, None), (None, 2, 1)]


def test_the_definitions_section_makes_a_definition_of_each_term_dash_text_paragraph():
    secs = S.derive(standard())
    terms = [s for s in secs if s.role == "definition"]
    assert [t.label for t in terms] == ["שטח נטו", "שטח ברוטו", "מעטפת"]
    assert [t.block_first for t in terms] == [10, 11, 12]
    # a paragraph that is no term-dash-text stays the definitions section's own text
    more = S.derive(doc(H("הגדרות", None, ["הגדרות"]), P("בתקן זה:", ["הגדרות"]), P("מונח – הסבר סינתטי.", ["הגדרות"])))
    assert [(s.role, s.label) for s in more] == [("definitions", None), ("definition", "מונח")]


def test_a_numbered_definitions_heading_is_a_definitions_section_too():
    secs = S.derive(doc(H("1. הגדרות", "1", ["1. הגדרות"]), P("מונח – הסבר סינתטי.", ["1. הגדרות"])))
    assert [(s.role, s.label) for s in secs] == [("definitions", "1"), ("definition", "מונח")]


def test_an_appendix_heading_and_a_short_appendix_line_open_an_appendix():
    secs = S.derive(doc(
        H("1. כללי", "1"), P("גוף סינתטי."),
        P("נספח ב' – טבלאות"),  # a PDF whose appendix line is no styled heading
        P("שורה בנספח."),
        H("נספח 3", None, ["נספח 3"]),
    ))
    assert [(s.role, s.label, s.depth) for s in secs] == [("chapter", "1", 1), ("appendix", "נספח ב'", 1),
                                                          ("appendix", "נספח 3", 1)]
    assert (secs[0].block_first, secs[0].block_last) == (0, 1)  # an appendix closes the chapter before it
    # a sentence that merely begins with the word is no appendix
    sentence = S.derive(doc(H("1. כללי", "1"), P("נספח א' מפרט את אופן המדידה ומציג את הכללים לשימוש השמאי.")))
    assert [s.role for s in sentence] == ["chapter"]


def test_a_note_paragraph_and_a_notes_heading_are_notes():
    secs = S.derive(doc(
        H("2. דרישות", "2"),
        P("חובה למדוד."),
        P("הערה: הדוגמה סינתטית."),
        H("הערות", None, ["הערות"]),
        P("הערה אחרונה סינתטית."),
    ))
    assert [(s.role, s.depth) for s in secs] == [("chapter", 1), ("note", 2), ("note", 1)]
    assert (secs[1].block_first, secs[1].block_last, secs[1].parent_no) == (2, 2, 1)
    assert (secs[2].block_first, secs[2].block_last) == (3, 4)


def test_text_before_the_first_heading_is_a_preamble_section():
    secs = S.derive(doc(P(MARK), P("פתיחה סינתטית."), H("1. כללי", "1")))
    assert (secs[0].role, secs[0].depth, secs[0].block_first, secs[0].block_last) == ("preamble", 0, 0, 1)
    assert secs[0].evidence["structure"]["rule"] == "before_first_heading"


def test_a_document_with_no_headings_is_one_preamble_section_marked_uncertain():
    secs = S.derive(doc(P(MARK), P("טקסט סינתטי ראשון.", page=1), P("טקסט סינתטי שני.", page=2)))
    assert len(secs) == 1
    s = secs[0]
    assert (s.role, s.label, s.title, s.depth, s.parent_no) == ("preamble", None, None, 0, None)
    assert (s.block_first, s.block_last, s.page_first, s.page_last) == (0, 2, 1, 2)
    assert s.evidence["structure"] == {"rule": "no_headings", "certain": False}


def test_no_blocks_no_sections():
    assert S.derive([]) == ()
    assert S.derive(doc(P("   "), P(""))) == ()


# --- limits ---------------------------------------------------------------------------------------------------

def test_a_limit_stays_with_its_provision_in_its_own_section_only():
    secs = S.derive(doc(
        H("2. דרישות", "2"),
        H("2.1 מדידה", "2.1", ["2. דרישות", "2.1 מדידה"]),
        P("חובה למדוד את כל השטח הבנוי. למעט שטחים משותפים, אלא אם נקבע אחרת.", ["2. דרישות", "2.1 מדידה"]),
        P("למעט מחסנים סינתטיים.", ["2. דרישות", "2.1 מדידה"]),   # the next sentence starts with the marker
        H("2.2 תיעוד", "2.2", ["2. דרישות", "2.2 תיעוד"]),
        P("יש לתעד. בכפוף לאמור בסעיף 2.1.", ["2. דרישות", "2.2 תיעוד"]),
    ))
    b = by_label(secs)
    assert [(x["block"], x["sentence"]) for x in b["2.1"].limits] == [
        (2, "למעט שטחים משותפים, אלא אם נקבע אחרת."), (3, "למעט מחסנים סינתטיים.")]
    # the provision's own sentence and its limit are in the same blocks of the same section: nothing is split off
    assert b["2.1"].limits[0]["provision_block"] == 2 and b["2.1"].limits[1]["provision_block"] == 2
    assert b["2.1"].block_first <= 2 <= 3 <= b["2.1"].block_last
    assert [(x["block"], x["marker"]) for x in b["2.2"].limits] == [(5, "בכפוף ל")]
    assert b["2"].limits == ()  # a limit belongs to its own section, not to the chapter around it


def test_every_limit_marker_is_found_with_its_sentence_capped():
    markers = ["למעט", "אלא אם", "בכפוף ל", "על אף האמור", "בתנאי ש", "חריג", "לא יחול"]
    rows = [H("1. כללי", "1")] + [P(f"סינתטי {m} מקרה {i}.") for i, m in enumerate(markers)]
    secs = S.derive(doc(*rows))
    assert [x["marker"] for x in secs[0].limits] == markers
    long = S.derive(doc(H("1. כללי", "1"), P("למעט " + "מילה " * 300 + ".")))
    assert len(long[0].limits[0]["sentence"]) <= S.LIMIT_SENTENCE_MAX


def test_a_heading_is_no_provision_and_carries_no_limit():
    secs = S.derive(doc(H("חריגים", None, ["חריגים"]), P("טקסט סינתטי.")))
    assert secs[0].limits == () and secs[0].modality["exception"] == 0


def test_a_limit_beyond_the_marker_prefixes_is_found():
    secs = S.derive(doc(H("1. כללי", "1"), P("הכלל חל ולמעט בנייה זמנית, ובכפוף לאישור.")))
    assert [x["marker"] for x in secs[0].limits] == ["למעט"]  # one sentence, one limit, the first marker


# --- modality -------------------------------------------------------------------------------------------------

def test_modality_counts_each_class_apart_per_section():
    secs = S.derive(doc(
        H("2. דרישות", "2"),
        P("השמאי חייב למדוד. יש לתעד את המדידה. נדרש לצרף תרשים. על השמאי לבדוק. חובה לחתום."),
        P("מומלץ לצלם. רצוי לשמור עותק. ראוי לתעד."),
        P("לדוגמה, שטח סינתטי. למשל חדר."),
        P("לחלופין ניתן למדוד בדרך אחרת. השמאי רשאי לבחור."),
        P("למעט חריג סינתטי."),
    ))
    assert secs[0].modality == {"requirement": 5, "recommendation": 3, "example": 2, "alternative": 2, "exception": 2}


def test_modality_of_a_section_without_markers_is_all_zero():
    secs = S.derive(doc(H("1. כללי", "1"), P("טקסט סינתטי ניטרלי.")))
    assert secs[0].modality == dict.fromkeys(S.MODALITY_CLASSES, 0)


def test_the_marker_lists_classify_standard_text_not_questions():
    assert "classify standard text" in S.__doc__
    assert {"למעט", "אלא אם", "בכפוף ל"} <= set(S.EXCEPTION_MARKERS)
    assert not set(S.EXCEPTION_MARKERS) & set(S.REQUIREMENT_MARKERS)


# --- references -----------------------------------------------------------------------------------------------

def test_a_section_reference_resolves_to_the_section_and_an_unknown_one_stays_unresolved():
    secs = S.derive(doc(
        H("1. כללי", "1"),
        P("ראה סעיף 2.1 וגם סעיף 7.", ["1. כללי"]),
        H("2. דרישות", "2"),
        H("2.1 מדידה", "2.1", ["2. דרישות", "2.1 מדידה"]),
    ))
    refs = secs[0].refs
    target = by_label(secs)["2.1"].section_no
    assert (refs[0]["type"], refs[0]["label"], refs[0]["target"], refs[0]["block"]) == ("section", "2.1", target, 1)
    assert (refs[1]["label"], refs[1]["target"]) == ("7", None)


def test_a_list_of_sections_and_a_prefixed_mention_are_references_too():
    secs = S.derive(doc(
        H("1. כללי", "1"), P("כמפורט בסעיפים 2.1 ו-2.2, ובהתאם לסעיף 2."),
        H("2. דרישות", "2"), H("2.1 מדידה", "2.1", ["2. דרישות", "2.1 מדידה"]),
        H("2.2 תיעוד", "2.2", ["2. דרישות", "2.2 תיעוד"]),
    ))
    assert [(r["label"], r["target"] is not None) for r in secs[0].refs] == [("2.1", True), ("2.2", True), ("2", True)]


def test_an_appendix_reference_resolves_and_a_standard_reference_is_external_with_no_target():
    secs = S.derive(doc(
        H("1. כללי", "1"), P("ראה נספח א' וכמפורט בתקן 99, ובתקן שמאי מס' 98.1."),
        H("נספח א'", None, ["נספח א'"]), P("טבלה סינתטית."),
    ))
    refs = secs[0].refs
    appendix = by_label(secs)["נספח א'"]
    assert (refs[0]["type"], refs[0]["label"], refs[0]["target"]) == ("appendix", "נספח א'", appendix.section_no)
    assert refs[1] == {"type": "standard", "standard": "99", "target": None, "block": 1, "text": "תקן 99"}
    assert (refs[2]["standard"], refs[2]["target"]) == ("98.1", None)


def test_a_heading_mentioning_a_standard_makes_no_reference():
    secs = S.derive(doc(H("הבהרה לתקן 98.1", None, ["הבהרה לתקן 98.1"]), P("טקסט סינתטי.")))
    assert secs[0].refs == ()


def test_a_label_shared_by_an_appendix_and_a_chapter_resolves_inside_the_asking_part():
    secs = S.derive(doc(
        H("1. כללי", "1"), P("ראה סעיף 1.1.", ["1. כללי"]), H("1.1 א", "1.1", ["1. כללי", "1.1 א"]),
        H("נספח א'", None, ["נספח א'"]), P("ראה סעיף 1.1.", ["נספח א'"]), H("1.1 ב", "1.1", ["נספח א'", "1.1 ב"]),
    ))
    chapter, appendix = secs[0], [s for s in secs if s.role == "appendix"][0]
    inner = [s for s in secs if s.label == "1.1"]
    assert len(inner) == 2
    assert chapter.refs[0]["target"] == inner[0].section_no
    assert appendix.refs[0]["target"] == inner[1].section_no
    assert inner[1].parent_no == appendix.section_no  # a subsection belongs to the part it sits in


# --- the text digest ------------------------------------------------------------------------------------------

def test_the_digest_is_the_same_for_the_same_text_cut_into_different_blocks():
    pdf = doc(
        ("heading", "2.1 מדידה", "2.1", ["2.1 מדידה"], 1),
        P("השטח נמדד בקווי המעטפת"),            # a PDF cuts the paragraph in two
        P("החיצוניים של הבניין."),
        P("עמוד 1"),                              # page furniture
        ("heading", "2.2 תיעוד", "2.2", ["2.2 תיעוד"], 2),
        P("(1) יש לצרף תרשים."),
    )
    docx = doc(
        ("heading", "מדידה", "2.1.", ["מדידה"], None),
        P("השטח  נמדד בקווי המעטפת החיצוניים של הבניין.", page=None),
        ("heading", "תיעוד", "2.2.", ["תיעוד"], None),
        ("paragraph", "יש לצרף תרשים.", "1)", ["תיעוד"], None),
    )
    assert S.text_digest(pdf) == S.text_digest(docx)
    assert len(S.text_digest(pdf)) == 64


def test_edited_or_reordered_text_has_another_digest():
    base = doc(P("השטח נמדד בקווי המעטפת."), P("יש לצרף תרשים."))
    edited = doc(P("השטח נמדד בקווי המעטפת."), P("יש לצרף שני תרשימים."))
    swapped = doc(P("יש לצרף תרשים."), P("השטח נמדד בקווי המעטפת."))
    assert len({S.text_digest(base), S.text_digest(edited), S.text_digest(swapped)}) == 3


def test_the_digest_ignores_niqqud_quote_style_and_case_but_not_numbers():
    a = doc(P("השטח הוא 12 מ\"ר בתקן A."))
    b = doc(P("הַשטח הוא 12 מ״ר בתקן a."))
    c = doc(P("השטח הוא 13 מ\"ר בתקן A."))
    assert S.text_digest(a) == S.text_digest(b) != S.text_digest(c)


def test_a_number_that_opens_a_paragraph_without_a_list_mark_is_text():
    assert S.text_digest(doc(P("15 דירות נבדקו."))) != S.text_digest(doc(P("דירות נבדקו.")))


def test_an_empty_document_has_the_digest_of_no_text():
    assert S.text_digest([]) == S.text_digest(doc(P("  "), P("")))
