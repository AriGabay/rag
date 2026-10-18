"""Choosing among units of a file of several appraisals, without a database (round 9 U9: KTD7, KTD8; R13, AE2, AE4).

``choose`` takes the rows ``units_matching`` gives (a unit per row, with its name fit), orders ambiguous candidates by
how well the unit's name fits the user's words and then by document order — never by the number of passages — and
cuts the list without dropping a better fit; ``among`` reads a reply by an ordinal, a number in the list, or a word of
a name or an identifier that only some candidates hold. Every name is invented."""

from __future__ import annotations

from app.chat import entities

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


def test_a_candidate_is_named_by_title_pages_identifiers_and_file():
    out = entities.choose([unit(1, "מתחם הגפן א", ["הגפן א"], pages=(1, 1), identifiers=["רחוב הגפן 4"]),
                           unit(2, None, [], pages=(2, 5))], "הגפן", set())
    assert out.kind == "resolved"  # one name fit: the unnamed unit holds nothing of the words but passages
    both = entities.choose([unit(1, "מתחם הגפן א", ["הגפן א"], pages=(1, 1), identifiers=["רחוב הגפן 4"]),
                            unit(2, "מתחם הגפן ב", ["הגפן ב"], pages=(2, 5), identifiers=["גוש 1 חלקה 2"])],
                           "הגפן", set())
    labels = [entities.candidate_label(d.as_dict()) for d in both.documents]
    assert labels[0] == "«מתחם הגפן א» (עמוד 1; רחוב הגפן 4; בקובץ «קובץ מרוכז סינתטי»)"
    assert labels[1] == "«מתחם הגפן ב» (עמודים 2–5; גוש 1 חלקה 2; בקובץ «קובץ מרוכז סינתטי»)"
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


# --- a reply that is a unit's full name (round 9 R13) -------------------------------------------------------------------


def _nested() -> list[dict]:
    """Two units whose names are one inside the other: the shorter name's words are in the longer one too."""
    return [c.as_dict() for c in entities.choose(
        [unit(1, "מגדל הים", ["מגדל הים"], pages=(1, 1)),
         unit(2, "מגדל הים הצפוני", ["הים הצפוני"], pages=(2, 2))], "מגדל", set()).documents]


def test_a_reply_that_is_one_units_full_name_selects_it_though_the_other_name_holds_the_same_words():
    cands = _nested()
    for reply, no in [("מגדל הים", 1), ("מגדל הים.", 1), ("במגדל הים", 1), ("מגדל הים הצפוני", 2), ("הים הצפוני", 2),
                      ("תכוון למגדל הים הצפוני בבקשה", 2)]:
        out = entities.among(cands, reply, {"d1"})
        assert out.kind == "resolved" and out.documents[0].unit["unit_no"] == no, reply


def test_a_full_name_that_both_candidates_state_or_a_part_of_a_name_selects_nothing():
    a, b = _two()
    both = [a | {"unit": a["unit"] | {"aliases": ["הגפן"]}}, b | {"unit": b["unit"] | {"aliases": ["הגפן"]}}]
    assert entities.among(both, "הגפן", {"d1"}).kind == "ambiguous"  # the same alias on both: no one is named
    assert entities.named_unit(_two(), "מתחם", {"d1"}) is None and entities.named_unit(_two(), "מה דעתך", {"d1"}) is None


def test_a_full_title_is_found_as_a_whole_phrase_in_a_longer_reply_and_never_inside_a_word():
    cands = _two()
    named = entities.named_unit(cands, "אני מתכוון למתחם הגפן ב, תודה", {"d1"})
    assert named is not None and named.kind == "resolved" and named.documents[0].unit["unit_no"] == 2
    assert entities.named_unit(cands, "מתחם הגפן בן", {"d1"}) is None  # "ב" is not a word of this reply
    assert entities.named_unit(cands, "מתחם הגפן ב", set()) is None  # none of them is visible any more


# --- derivations side by side (round 15 U3, KTD6, R7) -----------------------------------------------------------------


def row(no: int, title: str | None, first: int, last: int, *, kind: str = "appraisal", tag: str | None = "v5",
        aliases=(), identifiers=(), label: str = "1") -> dict:
    return {"document_id": "d1", "reading_id": "r1", "unit_no": no, "units_tag": tag, "kind": kind, "title": title,
            "aliases": list(aliases), "identifiers": list(identifiers), "page_first": 1, "page_last": 1,
            "block_first": first, "block_last": last, "first_label": label, "certainty": "certain"}


def old_anchor(no: int, title: str | None = None, **kw) -> dict:
    """An anchor stored before the change: made on a ``units-v4`` unit, so it carries no tag."""
    return entities.unit_anchor(row(no, title, 0, 0, tag=None, **kw))


def test_the_stable_id_of_a_later_derivation_carries_its_tag_and_a_v4_id_is_unchanged():
    from app.chat import contexts, units

    assert contexts.unit_key("v", "r1", 3) == "v#r1#3" and contexts.unit_key("v", None, 3) == "v#3"
    assert contexts.unit_key("v", "r1", 3, "v5") == "v#r1#3@v5" and contexts.unit_key("v", None, 3, "v5") == "v#3@v5"
    for key, parts, tag in (("v#r1#3", ("v", "r1", 3), None), ("v#r1#3@v5", ("v", "r1", 3), "v5"),
                            ("v#3@v5", ("v", None, 3), "v5")):
        assert contexts.split_unit_key(key) == parts and contexts.key_tag(key) == tag
    assert contexts.split_key("v#r1#3@v5") == ("v", 3)
    assert units.tag_of("units-v4") is None and units.tag_of("units-v5") == "v5"
    assert units.version_of_tag(None) == "units-v4" and units.version_of_tag("v5") == "units-v5"
    assert contexts.anchor_key("v", {"reading_id": "r1", "unit_no": 3, "units_tag": "v5"}) == "v#r1#3@v5"
    assert contexts.anchor_key("v", {"reading_id": "r1", "unit_no": 3}) == "v#r1#3"


def test_the_derivation_key_condition_is_the_one_derivation_key_builds():
    from app.chat import units

    sql = units.current_key_sql("u", "v")
    assert f"'{units.UNITS_VERSION}|'" in sql and "u.derivation_key" in sql and "v.ingestion->>'positions'" in sql


def test_an_anchor_carries_the_tag_of_its_derivation_and_a_stored_one_has_none():
    assert entities.unit_anchor(row(2, "מתחם א", 5, 9))["units_tag"] == "v5"
    stored_before = {k: v for k, v in old_anchor(2, "מתחם א").items() if k != "units_tag"}
    assert entities.same_unit(stored_before, old_anchor(2, "מתחם א"))  # no tag at all reads as a units-v4 anchor


def test_the_same_number_is_the_same_unit_only_within_one_derivation():
    a = entities.unit_anchor(row(3, None, 10, 20, tag=None))
    b = entities.unit_anchor(row(3, None, 12, 30, tag="v5"))
    assert not entities.same_unit(a, b)   # unit 3 of v4 and unit 3 of v5 may be two appraisals: nothing to compare
    assert entities.same_unit(a, entities.unit_anchor(row(3, None, 10, 20, tag=None)))
    named_a = entities.unit_anchor(row(3, "מתחם הגפן א", 10, 20, tag=None))
    named_b = entities.unit_anchor(row(4, "מתחם הגפן א", 12, 30, tag="v5"))
    assert entities.same_unit(named_a, named_b)   # the name says it, whatever the number


def test_an_untitled_anchor_whose_range_falls_in_one_new_unit_resolves_to_that_unit():
    rows = [row(1, None, 0, 9), row(2, None, 10, 19), row(3, None, 20, 29), row(4, None, 30, 60)]
    kept = {"block_first": 33, "block_last": 50}   # the v4 unit 3 (untitled) lay here
    found = entities.resolve_older(old_anchor(3), rows, kept)
    assert found is not None and found["unit_no"] == 4   # never the unit with the number 3


def test_an_anchor_whose_range_spans_two_new_units_is_gone_and_a_missing_row_resolves_nothing():
    rows = [row(1, None, 0, 9), row(2, None, 10, 19), row(3, None, 20, 29)]
    assert entities.resolve_older(old_anchor(2), rows, {"block_first": 5, "block_last": 15}) is None
    assert entities.resolve_older(old_anchor(2), rows, {"block_first": 40, "block_last": 50}) is None  # in no unit
    assert entities.resolve_older(old_anchor(2), rows, None) is None   # no kept row: the number alone is not enough


def test_an_old_anchor_resolves_by_name_before_the_range_and_never_into_an_appendix():
    rows = [row(1, "מתחם הגפן א", 0, 9), row(2, "מתחם הגפן ב", 10, 19), row(3, "נספח", 20, 29, kind="appendix")]
    named = old_anchor(7, "מתחם הגפן ב")
    assert entities.resolve_older(named, rows, {"block_first": 0, "block_last": 5})["unit_no"] == 2   # the name wins
    assert entities.resolve_older(old_anchor(5), rows, {"block_first": 21, "block_last": 25}) is None  # an appendix
