"""Choosing among units of a file of several appraisals, without a database (round 9 U9: KTD7, KTD8; R13, AE2, AE4).

``choose`` takes the rows ``units_matching`` gives (a unit per row, with its name fit), orders ambiguous candidates by
how well the unit's name fits the user's words and then by document order — never by the number of passages — and
cuts the list without dropping a better fit; ``among`` reads a reply by an ordinal, a number in the list, or a word of
a name or an identifier that only some candidates hold. Every name is invented."""

from __future__ import annotations

from app.chat import entities, resolve

FILE = "קובץ מרוכז סינתטי"


def unit(no: int, title: str | None, aliases: list[str], *, extra: int = 0, pages=(1, 1), identifiers=(), doc="d1",
         file_title: str = FILE) -> dict:
    return {"document_id": doc, "title": file_title, "hits": 1, "multi": True,
            "unit": {"document_id": doc, "reading_id": "r1", "unit_no": no, "unit_id": f"v#r1#{no}", "kind": "appraisal",
                     "title": title, "aliases": aliases, "identifiers": list(identifiers), "page_first": pages[0],
                     "page_last": pages[1], "first_label": "1", "certainty": "certain", "file_title": file_title,
                     "name_extra": extra}}


def test_a_unique_name_match_selects_the_unit():
    rows = [unit(1, "פרויקט הארז", ["הארז"]), unit(2, None, [], pages=(2, 3))]
    # the unnamed unit holds the word in its passages only: it fits no name, and is not a tie
    out = entities.choose([rows[0]], "הארז", set())
    assert out.kind == "resolved" and out.documents[0].unit["display"] == "פרויקט הארז"
    assert out.documents[0].unit["title"] == "פרויקט הארז" and out.documents[0].unit["aliases"] == ["הארז"]


def test_a_candidate_unit_carries_the_anchor_that_does_not_depend_on_the_unit_number():
    (c,) = entities.choose([unit(3, "מתחם גבעת הרקפות", ["גבעת הרקפות", "הרקפות"], pages=(3, 4),
                                 identifiers=["רחוב הרקפת 8"])], "הרקפות", set()).documents
    a = c.unit
    assert (a["document_id"], a["reading_id"], a["title"], a["first_label"]) == ("d1", "r1", "מתחם גבעת הרקפות", "1")
    assert a["aliases"] == ["גבעת הרקפות", "הרקפות"] and a["identifiers"] == ["רחוב הרקפת 8"] and a["pages"] == [3, 4]
    assert c.as_dict()["unit"] == a and c.as_dict()["file_title"] == FILE


def test_ambiguous_units_are_ordered_by_name_fit_then_document_order():
    rows = [unit(1, "מגדל הים הצפוני", ["מגדל הים הצפוני"], extra=1),
            unit(2, "מגדל הים הדרומי", ["מגדל הים הדרומי"], extra=1),
            unit(3, "מגדל הים", ["מגדל הים"], extra=0)]
    for r in rows:
        r["hits"] = {1: 9, 2: 5, 3: 1}[r["unit"]["unit_no"]]  # passages: the best fit has the fewest
    out = entities.choose(rows, "מגדל הים", set())
    assert out.kind == "ambiguous"
    assert [d.unit["unit_no"] for d in out.documents] == [3, 1, 2]


def test_the_cut_off_keeps_the_better_fit_whatever_its_passages():
    rows = [unit(n, f"מגדל הים {w}", [f"מגדל הים {w}"], extra=1) for n, w in enumerate("אבגדה", 1)]
    rows.append(unit(6, "מגדל הים", ["מגדל הים"], extra=0))
    for r in rows[:5]:
        r["hits"] = 50
    out = entities.choose(rows, "מגדל הים", set())
    assert len(out.documents) == entities.CLARIFY_TITLES and out.documents[0].unit["unit_no"] == 6


def test_a_document_level_candidate_is_ordered_by_title_not_by_passages():
    found = [{"document_id": "b", "title": "שומה - הגפן 12", "hits": 1},
             {"document_id": "a", "title": "שומה - הגפן 12 ב", "hits": 99}]
    out = entities.choose(found, "הגפן 12", set())
    assert out.kind == "ambiguous" and [d.document_id for d in out.documents] == ["b", "a"]


def test_the_clarification_names_a_unit_by_title_pages_identifiers_and_file():
    out = entities.choose([unit(1, "מתחם הגפן א", ["הגפן א"], pages=(1, 1), identifiers=["רחוב הגפן 4"]),
                           unit(2, None, [], pages=(2, 5))], "הגפן", set())
    assert out.kind == "resolved"  # one name fit: the unnamed unit holds nothing of the words but passages
    both = entities.choose([unit(1, "מתחם הגפן א", ["הגפן א"], pages=(1, 1), identifiers=["רחוב הגפן 4"]),
                            unit(2, "מתחם הגפן ב", ["הגפן ב"], pages=(2, 5), identifiers=["גוש 1 חלקה 2"])],
                           "הגפן", set())
    asked = resolve._clarify_titles(both, "הגפן")
    assert "1. «מתחם הגפן א» (עמוד 1; רחוב הגפן 4; בקובץ «קובץ מרוכז סינתטי»)" in asked
    assert "2. «מתחם הגפן ב» (עמודים 2–5; גוש 1 חלקה 2; בקובץ «קובץ מרוכז סינתטי»)" in asked
    assert entities.candidate_label(entities.Candidate("d", "x", unit={"display": None, "pages": [4, 4],
                                                                       "identifiers": []}, file_title="f").as_dict()
                                    ) == "«ללא כותרת» (עמוד 4; בקובץ «f»)"


def _two() -> list[dict]:
    return [c.as_dict() for c in entities.choose(
        [unit(1, "מתחם הגפן א", ["הגפן א"], identifiers=["רחוב הגפן 4"]),
         unit(2, "מתחם הגפן ב", ["הגפן ב"], pages=(2, 2), identifiers=["רחוב הגפן 6"])], "הגפן", set()).documents]


def test_a_reply_picks_a_unit_by_ordinal_number_or_a_word_only_one_holds():
    cands = _two()
    ok = {"d1"}
    for reply, no in [("השני", 2), ("הראשון", 1), ("2", 2), ("מספר 1", 1), ("ב", 2), ("הגפן א", 1), ("רחוב הגפן 6", 2),
                      ("זה של 4", 1)]:
        out = entities.among(cands, reply, ok)
        assert out.kind == "resolved" and out.documents[0].unit["unit_no"] == no, reply


def test_an_ambiguous_reply_asks_again_with_the_same_candidates():
    cands = _two()
    for reply in ("הגפן", "מתחם", "לא יודע", "3", ""):
        out = entities.among(cands, reply, {"d1"})
        assert out.kind == "ambiguous" and len(out.documents) == 2, reply
    assert entities.among(cands, "השני", set()).kind == "not_found"  # none of them is visible any more


def test_document_level_candidates_are_still_matched_by_a_title_word_or_an_ordinal():
    cands = [{"document_id": "a", "title": "שומה - הנרקיס 4 עין ורד"}, {"document_id": "b", "title": "שומה - הנרקיס 4 כפר גפן"}]
    assert entities.among(cands, "בכפר גפן", {"a", "b"}).documents[0].document_id == "b"
    assert entities.among(cands, "השני", {"a", "b"}).documents[0].document_id == "b"
    assert entities.among(cands, "הנרקיס", {"a", "b"}).kind == "ambiguous"


def test_unit_names_in_the_vocabulary_make_a_project_name_an_identifying_word():
    titles = ["שומה - הנרקיס 4 עין ורד", "גבעת הרקפות", "הרקפות"]
    assert entities.identifying("ומה השכירות בהרקפות?", titles, bare_numbers=False) == ["בהרקפות"]
    assert entities.identifying("ומה השכירות בהרקפות?", titles[:1], bare_numbers=False) == []
