"""The pure side of the identification facts (round 12 U5: R1, R2, R5, R13; KTD1, KTD2, KTD3): which facts the reader
takes from stored blocks (positive scope: the unit's title run, identification sections and labelled lines or cells),
what it never reads (comparables, appendices, cover or index tables, an adjacent-parcel sentence, text before the first
unit, an uncertain unit edge), and how the facts become a unit's identity at read time. No database. The round-12
fixtures' manifest is the expected identity of every file; all texts are synthetic."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.chat import contexts
from app.chat import identity as I  # noqa: E741 - the module is short-named like `dates` and `units`
from app.chat import property as P
from app.chat.units import Unit
from app.config import get_settings
from app.extraction.docx import extract_docx

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "round12"
MANIFEST = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))["documents"]
FIXTURES_13 = Path(__file__).resolve().parents[1] / "fixtures" / "round13"
MANIFEST_13 = json.loads((FIXTURES_13 / "manifest.json").read_text(encoding="utf-8"))["documents"]


def block(i: int, text: str, kind: str = "paragraph", path: tuple = (), page: int | None = None):
    return SimpleNamespace(block_index=i, kind=kind, text=text, section_path=list(path), page=page)


def unit(no: int, first: int, last: int, kind: str = "appraisal", certainty: str = "certain",
         pages: tuple[int, int] | None = None) -> Unit:
    return Unit(no, kind, None, (), {}, pages[0] if pages else None, pages[1] if pages else None, first, last,
                certainty)


def blocks_of(*texts: str, kinds: dict | None = None) -> list:
    return [block(i, t, (kinds or {}).get(i, "paragraph")) for i, t in enumerate(texts)]


def read(blocks, units=None):
    """The facts of the blocks, assigned to the units (one unit over all the blocks when none is given), and the units'
    identities."""
    units = units if units is not None else [unit(1, 0, len(blocks) - 1)]
    assigned = I.assign(I.derive(blocks, units), units)
    return assigned, I.unit_identities(assigned, units)


def addr(street: str, number: str) -> tuple[str, str]:
    """An address as the facts hold it: the street core is the street without its article and prefix letters, as
    `contexts.identifiers` cores it."""
    (core,) = contexts.identifiers(f"רחוב {street} {number}").addresses
    return core


def parcels(blocks, units=None):
    return {u: sorted(i.parcels) for u, i in read(blocks, units)[1].items()}


# --- parcels: lists, ranges, pairs, sub-parcels -----------------------------------------------------------------------

def test_a_parcel_list_and_range_in_an_identification_section_expand_to_the_block():
    blocks = [block(0, "זיהוי הנכס", "heading"), block(1, "גוש 7101 חלקות 24, 40, 42-44")]
    _, ids = read(blocks)
    assert ids[1].parcels == {("7101", p, None) for p in ("24", "40", "42", "43", "44")}


def test_the_labelled_plural_form_of_the_fixtures_reads_a_comma_list():
    _, ids = read(blocks_of("גוש: 8401 חלקות: 24, 40"))
    assert ids[1].parcels == {("8401", "24", None), ("8401", "40", None)}


def test_a_sub_parcel_in_the_same_statement_is_kept_with_its_parcel():
    _, ids = read(blocks_of("גוש 7101 חלקה 15 תת חלקה 3"))
    assert ids[1].parcels == {("7101", "15", "3")}


def test_a_sub_parcel_on_its_own_labelled_line_follows_the_single_parcel_before_it():
    _, ids = read(blocks_of("גוש: 8501 חלקה: 7", "תת חלקה: 3"))
    assert ids[1].parcels == {("8501", "7", "3")}


def test_each_parcel_pairs_with_its_nearest_preceding_block():
    _, ids = read(blocks_of("גוש 7101 חלקות 24, 40 וגוש 7102 חלקה 5"))
    assert ids[1].parcels == {("7101", "24", None), ("7101", "40", None), ("7102", "5", None)}


def test_a_range_is_capped_at_thirty_parcels_per_statement():
    _, ids = read(blocks_of("גוש 7101 חלקות 1-500"))
    assert len(ids[1].parcels) <= 30


def test_a_thousands_figure_after_a_parcel_is_not_a_second_parcel():
    _, ids = read(blocks_of("גוש: 8401 חלקה: 24, 1,200 מ״ר"))
    assert ids[1].parcels == {("8401", "24", None)}


# --- what is never an identification --------------------------------------------------------------------------------

def test_an_adjacent_parcel_sentence_is_no_fact():
    blocks = [block(0, "הנכס גובל בחלקה 17"),
              block(1, "החלקה גובלת בחלקה 15 בגוש 8901 ובחלקה 16 בגוש 8901."),
              block(2, "גוש: 8901 חלקה: 14")]
    _, ids = read(blocks)
    assert ids[1].parcels == {("8901", "14", None)}


def test_a_comparables_table_row_names_no_property():
    table = block(1, "כתובת | גוש וחלקה | מחיר (₪)\nרחוב הצפצפה 21 | גוש 8902 חלקה 3 | 1,640,000", "table")
    facts, ids = read([block(0, "נתוני השוואה", "heading"), table])
    assert facts == [] and not ids[1].identified


def test_a_cover_index_table_listing_six_addresses_gives_no_fact_to_any_unit():
    rows = "\n".join(f"{n} | רחוב הברוש {n} | עיר-סינתטית | גוש 860{n} חלקה {n}" for n in range(1, 7))
    index = block(1, "מס׳ | כתובת | עיר | גוש וחלקה\n" + rows, "table")
    facts, _ = read([block(0, "תוכן עניינים — רשימת השומות בקובץ"), index])
    assert facts == []


def test_a_comparables_heading_closes_its_section_to_labelled_lines_too():
    blocks = [block(0, "עסקאות השוואה", "heading"), block(1, "כתובת: הצפצפה 21"), block(2, "גוש: 8902 חלקה: 3")]
    facts, _ = read(blocks)
    assert facts == []


def test_an_appendix_section_is_never_read_even_below_a_sub_heading():
    blocks = [block(0, "נספח א׳", "heading"), block(1, "פרטי נכס", "heading", path=("נספח א׳",)),
              block(2, "גוש: 8902 חלקה: 3", path=("נספח א׳", "פרטי נכס"))]
    assert read(blocks)[0] == []


def test_a_street_in_running_prose_is_not_read_outside_the_title_run_and_identification():
    blocks = [block(0, "שומת מקרקעין"), *[block(i, f"שורה {i}") for i in range(1, 10)],
              block(10, "הנכס נמצא ברחוב הדקל 9 בעיר-סינתטית וסביבתו שקטה.")]
    assert read(blocks)[0] == []


def test_a_bare_slash_pair_is_no_parcel():
    assert read(blocks_of("סעיף 30871/22 לחוק"))[0] == []


# --- addresses and cities -------------------------------------------------------------------------------------------

def test_a_labelled_address_field_gives_the_street_core_and_number():
    _, ids = read(blocks_of("כתובת הנכס: הדולב 7"))
    assert ids[1].addresses == {addr("הדולב", "7")}


def test_the_title_run_reads_a_labelled_street_and_a_known_city():
    _, ids = read(blocks_of("שומת מקרקעין — רחוב הדולב 7, חיפה"))
    assert ids[1].addresses == {addr("הדולב", "7")} and ids[1].cities == {"חיפה"}


def test_a_labelled_city_is_accepted_without_the_vocabulary():
    _, ids = read(blocks_of("עיר: עיר-סינתטית"))
    assert ids[1].cities == {"עיר-סינתטית"}


def test_an_unlabelled_city_after_the_street_is_accepted_only_when_the_unit_labels_it():
    title = "שומת מקרקעין — רחוב הצאלון 7, עיר-סינתטית-ב"
    _, alone = read(blocks_of(title))
    assert alone[1].addresses == {addr("הצאלון", "7")} and alone[1].cities == frozenset()
    _, labelled = read(blocks_of(title, "עיר: עיר-סינתטית-ב"))
    assert labelled[1].cities == {"עיר-סינתטית-ב"}


def test_a_city_in_a_comma_free_prefix_form_equals_the_labelled_city():
    _, ids = read(blocks_of("עיר: עיר-סינתטית", "זיהוי הנכס", "מסמך לנכס ברחוב הדס 3 בעיר-סינתטית"))
    assert ids[1].cities == {"עיר-סינתטית"}


def test_a_boulevard_label_is_a_street_not_a_city():
    _, ids = read(blocks_of("כתובת: שדרות הרצל 5"))
    assert ids[1].addresses == {addr("הרצל", "5")} and ids[1].cities == frozenset()


def test_a_known_city_name_after_the_house_number_is_a_city_even_when_it_is_a_boulevard_word():
    _, ids = read(blocks_of("שומת מקרקעין — רחוב הרצל 5, שדרות"))
    assert ids[1].addresses == {addr("הרצל", "5")} and ids[1].cities == {"שדרות"}


def test_a_unit_naming_two_different_subject_cities_links_as_cityless_and_keeps_the_set():
    _, ids = read(blocks_of("עיר: עיר-סינתטית", "ישוב: עיר-סינתטית-ב"))
    unit_ = ids[1]
    assert unit_.cities == {"עיר-סינתטית", "עיר-סינתטית-ב"} and unit_.city_conflict and unit_.link_city is None


def test_two_spellings_of_one_city_are_not_a_conflict():
    _, ids = read(blocks_of("עיר: תל אביב", "ישוב: תל אביב-יפו"))
    assert not ids[1].city_conflict and ids[1].link_city == "תל אביב-יפו"


def test_a_city_column_of_a_one_row_table_is_read():
    table = block(0, "כתובת | עיר | גוש וחלקה\nרחוב הדס 3 | עיר-סינתטית | גוש 8401 חלקה 24", "table")
    _, ids = read([table])
    assert ids[1].cities == {"עיר-סינתטית"} and ids[1].addresses == {addr("הדס", "3")}
    assert ids[1].parcels == {("8401", "24", None)}


def test_a_key_value_table_is_read_by_its_labels():
    table = block(0, "עיר | עיר-סינתטית\nגוש | 8401\nחלקה | 24\nתת חלקה | 2", "table")
    _, ids = read([table])
    assert ids[1].cities == {"עיר-סינתטית"} and ids[1].parcels == {("8401", "24", "2")}


# --- header city (KTD3) ---------------------------------------------------------------------------------------------

def test_a_single_unit_file_takes_the_header_city_when_its_body_names_none():
    units = [unit(1, 0, 3)]
    assigned = I.assign(I.derive(blocks_of("כתובת: הדולב 7", "גוש: 8201 חלקה: 11"), units), units)
    ident = I.unit_identities(assigned, units, header_city="עיר-סינתטית")[1]
    assert ident.cities == frozenset() and ident.header_city == "עיר-סינתטית" and ident.link_city == "עיר-סינתטית"


def test_a_multi_unit_file_donates_no_header_city():
    units = [unit(1, 0, 1), unit(2, 2, 3)]
    blocks = blocks_of("כתובת: הדולב 7", "גוש: 8201 חלקה: 11", "כתובת: הדס 3", "גוש: 8401 חלקה: 24")
    ids = I.unit_identities(I.assign(I.derive(blocks, units), units), units, header_city="עיר-סינתטית")
    assert all(i.header_city is None and i.link_city is None for i in ids.values())


def test_the_header_city_never_overrides_a_city_the_unit_states():
    units = [unit(1, 0, 1)]
    blocks = blocks_of("עיר: עיר-סינתטית-ב", "כתובת: הדולב 7")
    ident = I.unit_identities(I.assign(I.derive(blocks, units), units), units, header_city="עיר-סינתטית")[1]
    assert ident.header_city is None and ident.link_city == "עיר-סינתטית-ב"


def test_a_header_naming_more_than_one_city_donates_nothing():
    units = [unit(1, 0, 0)]
    ident = I.unit_identities(I.assign(I.derive(blocks_of("כתובת: הדולב 7"), units), units), units,
                              header_city="עיר-סינתטית, עיר-סינתטית-ב")[1]
    assert ident.header_city is None


def test_a_cover_unit_does_not_make_a_two_unit_file_multi_unit_for_the_header():
    units = [unit(1, 0, 0, kind="front_matter"), unit(2, 1, 2)]
    blocks = blocks_of("תוכן", "כתובת: הדולב 7", "גוש: 8201 חלקה: 11")
    ids = I.unit_identities(I.assign(I.derive(blocks, units), units), units, header_city="עיר-סינתטית")
    assert 1 not in ids and ids[2].header_city == "עיר-סינתטית"


# --- the body of the report -----------------------------------------------------------------------------------------

def test_identification_only_under_the_heading_on_page_three_is_found():
    blocks = [block(0, "שומת מקרקעין — מסמך מספר 17 (סינתטי)", page=1), block(1, "סוג נכס: דירה", page=1),
              block(2, "1. מטרת השומה", "heading", page=2), block(3, "מסמך לנכס בדוי.", path=("1. מטרת השומה",), page=2),
              block(4, "3. זיהוי הנכס", "heading", path=("3. זיהוי הנכס",), page=3),
              block(5, "רחוב הלוטם 8, עיר-סינתטית-ב", path=("3. זיהוי הנכס",), page=3),
              block(6, "גוש: 8901 חלקה: 14", path=("3. זיהוי הנכס",), page=3),
              block(7, "4. סביבת הנכס", "heading", path=("4. סביבת הנכס",), page=3),
              block(8, "החלקה גובלת בחלקה 15 בגוש 8901.", path=("4. סביבת הנכס",), page=3)]
    _, ids = read(blocks, [unit(1, 0, 8, pages=(1, 3))])
    assert ids[1].addresses == {addr("הלוטם", "8")} and ids[1].parcels == {("8901", "14", None)}


def test_a_heading_like_paragraph_opens_an_identification_section_too():
    blocks = [block(0, "שומה"), block(1, "זיהוי הנכס"), block(2, "גוש 7101 חלקה 24")]
    assert read(blocks)[1][1].parcels == {("7101", "24", None)}


def test_an_apartment_is_read_from_its_label_and_a_property_type_is_not_one():
    _, ids = read(blocks_of("סוג נכס: דירה", "דירה: 3", "גוש: 8501 חלקה: 7"))
    assert ids[1].apartments == {"3"} and ids[1].apartment == "3"
    assert read(blocks_of("סוג נכס: דירה"))[1][1].apartments == frozenset()


def test_a_parcel_without_a_sub_parcel_yields_to_the_same_parcel_with_one():
    _, ids = read(blocks_of("גוש 8501 חלקה 7", "כתובת: הנרקיס 12", "גוש: 8501 חלקה: 7 תת חלקה: 3"))
    assert ids[1].parcels == {("8501", "7", "3")}


# --- assignment to units at read time -----------------------------------------------------------------------------------

def test_facts_go_to_the_unit_that_holds_their_block():
    blocks = blocks_of("גוש: 8601 חלקה: 5", "שווי", "גוש: 8601 חלקה: 6", "שווי")
    _, ids = read(blocks, [unit(1, 0, 1), unit(2, 2, 3)])
    assert ids[1].parcels == {("8601", "5", None)} and ids[2].parcels == {("8601", "6", None)}


def test_facts_before_the_first_unit_and_in_non_subject_units_belong_to_none():
    blocks = blocks_of("גוש: 8601 חלקה: 1", "גוש: 8601 חלקה: 2", "גוש: 8601 חלקה: 3", "גוש: 8601 חלקה: 4",
                       "גוש: 8601 חלקה: 5")
    units = [unit(1, 1, 1, kind="front_matter"), unit(2, 2, 2), unit(3, 3, 3, kind="comparables"),
             unit(4, 4, 4, kind="appendix")]
    facts, ids = read(blocks, units)
    assert {f.parcel for f in facts} == {"3"} and set(ids) == {2}


def test_a_fact_in_an_uncertain_unit_edge_is_withheld_but_the_certain_body_is_kept():
    blocks = [block(0, "גוש: 8601 חלקה: 1", page=1), block(1, "גוש: 8601 חלקה: 2", page=2),
              block(2, "שווי", page=2), block(3, "גוש: 8601 חלקה: 3", page=3), block(4, "גוש: 8601 חלקה: 4", page=4),
              block(5, "גוש: 8601 חלקה: 5", page=5)]
    units = [unit(1, 0, 2, pages=(1, 2)), unit(2, 3, 5, certainty="probable", pages=(3, 5))]
    facts, _ = read(blocks, units)
    # page 2 (the previous unit's last page) and page 3 (the probable unit's first page) are the edge
    assert {f.parcel for f in facts} == {"1", "4", "5"}


def test_without_pages_the_edge_is_the_two_blocks_each_side_of_the_cut():
    blocks = [block(i, f"גוש: 8601 חלקה: {i + 1}") for i in range(10)]
    units = [unit(1, 0, 4), unit(2, 5, 9, certainty="probable")]
    facts, _ = read(blocks, units)
    assert {int(f.parcel) for f in facts} == {1, 2, 3, 8, 9, 10}


def test_a_version_without_units_is_one_scope():
    facts = I.assign(I.derive(blocks_of("גוש: 8601 חלקה: 5"), ()), ())
    assert [f.unit_no for f in facts] == [I.DOCUMENT_SCOPE]


def test_a_unit_that_names_nothing_is_not_identified():
    _, ids = read(blocks_of("שווי הנכס: 2,500,000 ₪"))
    assert ids[1].identified is False and ids[1].parcels == frozenset() and ids[1].addresses == frozenset()


# --- the round-12 fixtures: the manifest is the expected identity ----------------------------------------------------------

def _blocks(name: str):
    path = FIXTURES / MANIFEST[name]["file"].split("/", 1)[1]
    got = extract_docx(path.read_bytes(), time.monotonic() + 60, get_settings()).blocks
    return [block(b.index, b.text, b.kind, tuple(b.section_path), b.page) for b in got]   # as the stored rows read


def _split(blocks) -> list[Unit]:
    """Units cut at each appraisal title ("שומת מקרקעין — ..."); a part before the first title that holds a table is a
    front-matter unit (the cover with its index table), otherwise it belongs to the first unit."""
    starts = [b.block_index for b in blocks if re.match(r"שומת מקרקעין — ", b.text)]
    cover = any(b.kind == "table" for b in blocks[:starts[0]])
    units: list[Unit] = [unit(1, 0, starts[0] - 1, kind="front_matter")] if cover else []
    for n, s in enumerate(starts):
        last = (starts[n + 1] - 1) if n + 1 < len(starts) else blocks[-1].block_index
        units.append(unit(len(units) + 1, 0 if n == 0 and not cover else s, last))
    return units


def _expected(identity: dict):
    parcels = {(identity["block"], p, identity["sub_parcel"]) for p in identity["parcels"]} if identity["block"] else set()
    addresses = {addr(identity["street"], identity["number"])}
    return parcels, addresses


@pytest.mark.parametrize("name", [k for k, d in MANIFEST.items() if len(d["units"]) == 1])
def test_every_single_appraisal_fixture_reproduces_its_manifest_identity(name):
    spec = MANIFEST[name]["units"][0]
    identity = spec["identity"]
    blocks = _blocks(name)
    units = [unit(1, 0, blocks[-1].block_index)]
    _, ids = read(blocks, units)
    got = ids[1]
    parcels_, addresses = _expected(identity)
    assert got.parcels == parcels_
    assert got.addresses == addresses
    # KTD3: an unlabelled "STREET N, CITY" in the body names a city only when the unit labels it or it is a known city,
    # so the body-identification fixture reads no city (its manifest names one: see the report of U5)
    if identity["identification_in"] == "body":
        assert got.cities == frozenset()
    else:
        assert got.cities == ({identity["city"]} if identity["city"] else frozenset())
    # the negatives (an adjacent parcel, a comparables-table row) never become part of the identity
    for negative in spec.get("negatives", []):
        if negative["kind"] == "adjacent_parcel":
            assert not {(negative["block"], p) for p in negative["parcels"]} & {(b, p) for b, p, _ in got.parcels}
        if negative["kind"] == "comparables_table":
            assert addr(negative["street"], negative["number"]) not in got.addresses
            assert (negative["block"], negative["parcels"][0]) not in {(b, p) for b, p, _ in got.parcels}


@pytest.mark.parametrize("name", ["compendium_two", "compendium_one"])
def test_a_compendium_gives_each_unit_its_own_identity_and_the_index_table_none(name):
    blocks = _blocks(name)
    units = _split(blocks)
    facts, ids = read(blocks, units)
    appraisals = [u for u in units if u.kind == "appraisal"]
    assert len(appraisals) == len(MANIFEST[name]["units"]) == 6
    for u, spec in zip(appraisals, MANIFEST[name]["units"], strict=True):
        parcels_, addresses = _expected(spec["identity"])
        assert ids[u.unit_no].parcels == parcels_ and ids[u.unit_no].addresses == addresses
        assert ids[u.unit_no].cities == {spec["identity"]["city"]}
    # nothing was read from the cover's index table
    assert not [f for f in facts if blocks[f.block_index].kind == "table"]
    assert all(f.unit_no in {u.unit_no for u in appraisals} for f in facts)


# --- small rules ------------------------------------------------------------------------------------------------------------

def test_two_hyphen_related_synthetic_cities_are_two_places_and_two_known_aliases_are_one():
    assert not I.same_place("עיר-סינתטית", "עיר-סינתטית-ב")   # place_matches alone would equate them (R1)
    assert I.same_place("תל אביב", "תל אביב-יפו") and I.same_place("חיפה", "חיפה")
    assert not I.same_place("חיפה", "אשדוד")


def test_a_sub_parcel_next_to_several_parcels_is_dropped_not_guessed():
    _, ids = read(blocks_of("גוש 7101 חלקות 24, 40 תת חלקה 3"))
    assert ids[1].parcels == {("7101", "24", None), ("7101", "40", None)}


def test_a_slash_pair_under_a_combined_block_and_parcel_label_is_read():
    _, ids = read(blocks_of("גוש/חלקה: 7101/24/3"))
    assert ids[1].parcels == {("7101", "24", "3")}


def test_block_and_parcel_prefixes_and_a_labelled_apartment_after_the_address_are_read():
    prose = "הנכס בגוש 7101 ובחלקה 24, ברחוב הדס 3 דירה 5"
    _, ids = read(blocks_of(*[f"שורה {i}" for i in range(9)], prose))
    assert ids[1].parcels == frozenset()    # a prose sentence outside a title run or an identification section
    _, ids = read(blocks_of("זיהוי הנכס", "הנכס בגוש 7101 ובחלקה 24, ברחוב הדס 3 דירה 5, חיפה"))
    assert ids[1].parcels == {("7101", "24", None)} and ids[1].apartment == "5"
    assert ids[1].addresses == {addr("הדס", "3")} and ids[1].cities == frozenset()   # the city follows the apartment


def test_a_block_is_carried_to_the_parcel_line_below_it_but_not_across_a_gap():
    _, ids = read(blocks_of("גוש: 8501", "חלקה: 7"))
    assert ids[1].parcels == {("8501", "7", None)}
    far = blocks_of("גוש: 8501", "א", "ב", "ג", "ד", "חלקה: 7")
    assert read(far)[1][1].parcels == frozenset()


# --- round 13 U7: party blocks never supply identity facts (R11, KTD8; AE8) -----------------------------------------------

SUBJECT = ["שומת מקרקעין — רחוב הרקפת 18, עיר-סינתטית", "עיר: עיר-סינתטית", "כתובת הנכס: הרקפת 18",
           "גוש: 9106 חלקה: 44"]
OFFICE_ROWS = ["שם המומחה: דנה סינתטית-בדוי", "מספר רישיון: 76543", "כתובת: הנחושת 3, עיר-סינתטית-ב"]


def _subject_only(ids) -> None:
    """The identity of the subject and nothing of the office: one address, one city, no conflict (AE8)."""
    got = ids[1]
    assert got.addresses == {addr("הרקפת", "18")} and got.parcels == {("9106", "44", None)}
    assert got.cities == {"עיר-סינתטית"} and not got.city_conflict and got.link_city == "עיר-סינתטית"


def test_a_declaration_block_naming_the_expert_gives_no_office_address_or_city():
    declaration = "\n".join(OFFICE_ROWS + ["אני החתומה מטה מצהירה כי אין לי עניין אישי בנכס."])
    facts, ids = read(blocks_of(*SUBJECT, "השמאי בחן נתוני שוק.", declaration))
    _subject_only(ids)
    assert not [f for f in facts if f.block_index >= 4]


def test_the_same_declaration_in_a_two_column_table_gives_no_fact():
    table = block(5, "\n".join(f"{a} | {b}" for a, b in (("מספר רישיון", "76543"), ("שם המומחה", "דנה סינתטית-בדוי"),
                                                          ("כתובת", "הנחושת 3, עיר-סינתטית-ב"), ("עיר", "עיר-סינתטית-ב"))),
                  "table")
    blocks = [*blocks_of(*SUBJECT, "השמאי בחן נתוני שוק."), table]
    facts, ids = read(blocks)
    _subject_only(ids)
    assert not [f for f in facts if f.block_index == 5]


def test_a_subject_address_line_in_a_block_that_also_names_the_appraiser_is_still_read():
    mixed = "שם השמאי: דנה סינתטית-בדוי\nמספר רישיון: 76543\nכתובת הנכס: הרקפת 18\nעיר: עיר-סינתטית"
    _, ids = read(blocks_of("שומת מקרקעין", mixed))
    assert ids[1].addresses == {addr("הרקפת", "18")} and ids[1].cities == {"עיר-סינתטית"}


def test_a_cover_table_with_an_appraiser_row_above_the_block_and_parcel_row_keeps_the_subject_address():
    cover = block(1, "מספר תיק | 2024-0183\nשם השמאי | דנה סינתטית-בדוי\nגוש וחלקה | גוש 9109 חלקה 17\n"
                     "כתובת | הנורית 6\nעיר | עיר-סינתטית", "table")
    _, ids = read([block(0, "חוות דעת שמאית — דירת מגורים"), cover])
    assert ids[1].addresses == {addr("הנורית", "6")} and ids[1].parcels == {("9109", "17", None)}
    assert ids[1].cities == {"עיר-סינתטית"}


def test_a_party_row_followed_by_a_bare_address_and_no_subject_label_gives_nothing():
    cover = block(1, "שם השמאי | דנה סינתטית-בדוי\nכתובת | הנחושת 3\nעיר | עיר-סינתטית-ב", "table")
    facts, _ = read([block(0, "חוות דעת שמאית"), cover])
    assert facts == []


def test_a_bare_address_directly_under_the_expert_name_and_licence_rows_gives_no_fact():
    facts, ids = read(blocks_of("חוות דעת שמאית", "שם המומחה: דנה סינתטית-בדוי\nמס' רשיון: 76543\nכתובת: הנחושת 3, עיר-סינתטית-ב"))
    assert facts == [] and not ids[1].identified


@pytest.mark.parametrize("label", ["טלפון", "נייד", "פקס", "דוא\"ל", "אימייל", "משרד", "שם השמאי", "מס׳ רישיון"])
def test_every_party_label_guards_a_following_bare_address_and_city(label):
    block_ = f"{label}: 050-0000000\nכתובת: הנחושת 3\nעיר: עיר-סינתטית-ב"
    facts, _ = read(blocks_of("חוות דעת שמאית", block_))
    assert facts == []


def test_a_bare_address_or_city_outside_a_party_group_is_read_as_before():
    _, ids = read(blocks_of("חוות דעת שמאית", "כתובת: הרקפת 18\nעיר: עיר-סינתטית"))
    assert ids[1].addresses == {addr("הרקפת", "18")} and ids[1].cities == {"עיר-סינתטית"}


def test_the_guard_is_scoped_to_its_block_a_bare_address_in_the_next_block_is_read():
    _, ids = read(blocks_of("חוות דעת שמאית", "שם השמאי: דנה סינתטית-בדוי", "כתובת: הרקפת 18"))
    assert ids[1].addresses == {addr("הרקפת", "18")}


def test_a_parcel_row_between_the_party_row_and_the_address_row_ends_the_guard():
    _, ids = read(blocks_of("חוות דעת שמאית", "שם השמאי: דנה סינתטית-בדוי\nגוש: 9109 חלקה: 17\nכתובת: הנורית 6"))
    assert ids[1].addresses == {addr("הנורית", "6")} and ids[1].parcels == {("9109", "17", None)}


def test_a_heading_of_the_appraisers_details_closes_its_section_until_the_next_heading():
    blocks = [block(0, "חוות דעת שמאית"), block(1, "פרטי השמאי", "heading"),
              block(2, "עיר: עיר-סינתטית-ב"), block(3, "כתובת הנכס: הנחושת 3"), block(4, "גוש: 9199 חלקה: 1"),
              block(5, "2. הנכס", "heading"), block(6, "כתובת הנכס: הרקפת 18"), block(7, "גוש: 9106 חלקה: 44")]
    facts, ids = read(blocks)
    assert {f.block_index for f in facts} == {6, 7}
    assert ids[1].addresses == {addr("הרקפת", "18")} and ids[1].cities == frozenset()


@pytest.mark.parametrize("heading", ["3. הצהרת השמאי", "הצהרת המומחה", "הצהרה", "פרטי המומחה", "פרטי המזמין", "פרטי הלקוח",
                                     "פרטי משרד השמאי", "חתימה", "חתימת השמאי", "פרטי השמאית"])
def test_every_party_heading_closes_its_section(heading):
    assert I._section_of(heading) == "party"
    facts, _ = read([block(0, "חוות דעת שמאית"), block(1, heading, "heading"), block(2, "עיר: עיר-סינתטית-ב")])
    assert facts == []


def test_a_heading_like_paragraph_of_the_declaration_closes_the_section_too():
    facts, _ = read(blocks_of("חוות דעת שמאית", "הצהרת השמאי", "עיר: עיר-סינתטית-ב", "כתובת הנכס: הנחושת 3"))
    assert facts == []


def test_a_party_ancestor_in_the_section_path_closes_a_block_below_a_sub_heading():
    blocks = [block(0, "חוות דעת שמאית"),
              block(1, "עיר: עיר-סינתטית-ב", path=("3. הצהרת השמאי", "3.1 משרד")),
              block(2, "עיר: עיר-סינתטית", path=("2. הנכס",))]
    facts, _ = read(blocks)
    assert [f.block_index for f in facts] == [2]


def test_the_identification_and_unrelated_headings_are_not_party_headings():
    for heading in ("זיהוי הנכס", "פרטי הנכס", "מטרת השומה", "2. שיקולי השמאי", "מקדם המשרד", "תיאור הנכס"):
        assert I._section_of(heading) != "party", heading


def test_the_identity_version_is_v2():
    assert I.IDENTITY_VERSION == "identity-v2"


@pytest.mark.parametrize("name", ["appraiser_office", "appraiser_office_table"])
def test_the_round_13_declaration_fixtures_keep_the_subject_and_name_one_address(name):
    spec = MANIFEST_13[name]
    identity = spec["identity"]
    path = FIXTURES_13 / spec["file"].split("/", 1)[1]
    got_blocks = extract_docx(path.read_bytes(), time.monotonic() + 60, get_settings()).blocks
    blocks = [block(b.index, b.text, b.kind, tuple(b.section_path), b.page) for b in got_blocks]
    facts, ids = read(blocks)
    got = ids[1]
    assert got.addresses == {addr(identity["street"], identity["number"])}
    assert got.parcels == {(identity["block"], p, identity["sub_parcel"]) for p in identity["parcels"]}
    assert got.cities == {identity["city"]} and not got.city_conflict and got.link_city == identity["city"]
    # nothing of the declaration: the office street, its city, the expert's name and licence
    written = " ".join(f.written for f in facts)
    assert "הנחושת" not in written and "עיר-סינתטית-ב" not in written
    assert "76543" not in written and "דנה" not in written
    street, number = addr(identity["street"], identity["number"])   # the label names the cored street (no article)
    label = P.label_of([P.Node("doc", "v", 1, got)])
    assert label == f"{street} {number}, {identity['city']}" and label.count(",") == 1 and "·" not in label


def test_the_manifest_round_13_declaration_blocks_read_as_the_fixture_does():
    """The manifest's own paragraphs, tables and unit blocks rebuilt as blocks (no document reading): the same identity."""
    for name in ("appraiser_office", "appraiser_office_table"):
        spec = MANIFEST_13[name]
        # table rows arrive one block per row in `unit_blocks` ("|label | value"): a run of rows is one table block
        merged: list = []
        for line in spec["unit_blocks"]:
            if line.startswith("|") and merged and merged[-1].kind == "table" and merged[-1].text:
                merged[-1].text += "\n" + line.removeprefix("|").strip()
            elif line.startswith("|"):
                merged.append(block(len(merged), line.removeprefix("|").strip(), "table"))
            else:
                merged.append(block(len(merged), line))
        identity = spec["identity"]
        _, ids = read(merged)
        assert ids[1].addresses == {addr(identity["street"], identity["number"])}, name
        assert ids[1].cities == {identity["city"]} and not ids[1].city_conflict, name
