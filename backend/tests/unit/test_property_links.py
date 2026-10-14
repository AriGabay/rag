"""The link relation and the per-turn property graph (round 12 U6: R1-R4, R8; AE1, AE2; KTD4, KTD5).

Appraisals are one property only through proving links, and every link keeps its reason. The relation is a pure function
of two unit identities (``identity.UnitIdentity``); the graph is built once per turn over the visible subject units, and
the focus property is the compatible closure from the focus unit. The expected relations of the synthetic fixtures are
read from the round-12 manifest (every name, address, city, block and parcel in it is invented)."""

from __future__ import annotations

import itertools
import json
import random
from pathlib import Path

import pytest

from app.appraisal.normalize import normalize_place
from app.chat import property as P
from app.chat.identity import UnitIdentity

MANIFEST = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "round12" / "manifest.json")
                      .read_text(encoding="utf-8"))
CITY_X, CITY_Y = "עיר סינתטית", "עיר סינתטית ב"


def ident(parcels=(), addresses=(), cities=(), apartments=(), header_city=None, unit_no=1) -> UnitIdentity:
    """A unit identity from the short forms the tests write: parcels "block/parcel" or "block/parcel/sub", addresses
    "street number"."""
    ps = set()
    for p in parcels:
        b, pa, *s = p.split("/")
        ps.add((b, pa, s[0] if s else None))
    ads = {tuple(a.rsplit(" ", 1)) for a in addresses}
    return UnitIdentity(unit_no, frozenset(ps), frozenset(apartments), frozenset(ads), frozenset(cities), header_city)


def node(doc: str, unit_no: int = 1, **kw) -> P.Node:
    return P.Node(doc, "v-" + doc, unit_no, ident(unit_no=unit_no, **kw), f"title {doc}")


def graph(*nodes: P.Node) -> P.PropertyGraph:
    return P.PropertyGraph(list(nodes))


def rel(a: UnitIdentity, b: UnitIdentity) -> tuple[str, str]:
    link = P.relate(a, b)
    assert (link.relation, link.reason) == (P.relate(b, a).relation, P.relate(b, a).reason), "the relation is symmetric"
    return link.relation, link.reason


# --- the relation (KTD4's table) -------------------------------------------------------------------------------------

def test_ae1_the_same_street_and_number_in_two_cities_is_other_and_with_no_city_a_candidate():
    x = ident(addresses=["הצאלון 7"], cities=[CITY_X])
    y = ident(addresses=["הצאלון 7"], cities=[CITY_Y])
    none = ident(addresses=["הצאלון 7"])
    assert rel(x, y) == (P.OTHER, P.REASON_CITIES)
    assert rel(x, none) == (P.CANDIDATE, P.REASON_CITY_MISSING)
    assert rel(y, none) == (P.CANDIDATE, P.REASON_CITY_MISSING)
    assert rel(none, none) == (P.CANDIDATE, P.REASON_CITY_MISSING), "two cityless units never link by themselves"
    assert rel(x, ident(addresses=["הצאלון 7"], cities=[CITY_X])) == (P.ADDRESS_CITY, "address_city")


def test_two_cities_differ_whatever_the_parcel_says_and_a_unit_with_two_cities_is_cityless():
    assert rel(ident(parcels=["7101/15"], cities=[CITY_X]), ident(parcels=["7101/15"], cities=[CITY_Y])) \
        == (P.OTHER, P.REASON_CITIES)
    both = ident(addresses=["הצאלון 7"], cities=[CITY_X, CITY_Y])
    assert rel(both, ident(addresses=["הצאלון 7"], cities=[CITY_X])) == (P.CANDIDATE, P.REASON_CITY_MISSING)


def test_a_shared_parcel_links_with_its_reason_and_a_header_city_counts_as_the_unit_city():
    a = ident(parcels=["7101/15"], addresses=["הצאלון 7"], cities=[CITY_X])
    b = ident(parcels=["7101/15"], addresses=["אחר 3"])
    assert rel(a, b) == (P.PARCEL, "parcel 7101/15"), "a parcel is the registry's name: no city and no street needed"
    assert rel(ident(addresses=["הצאלון 7"], header_city=CITY_X), ident(addresses=["הצאלון 7"], cities=[CITY_X])) \
        == (P.ADDRESS_CITY, "address_city")


def test_disjoint_stated_parcels_are_other_even_at_the_same_address():
    a = ident(parcels=["7101/15"], addresses=["הצאלון 7"], cities=[CITY_X])
    b = ident(parcels=["7101/16"], addresses=["הצאלון 7"], cities=[CITY_X])
    assert rel(a, b) == (P.OTHER, P.REASON_PARCELS)
    assert rel(ident(parcels=["7101/15"]), ident(parcels=["7102/15"])) == (P.OTHER, P.REASON_PARCELS), "the block is part of it"


def test_sub_parcels_and_apartments():
    three = ident(parcels=["8501/7/3"], addresses=["הנרקיס 12"], cities=[CITY_X])
    four = ident(parcels=["8501/7/4"], addresses=["הנרקיס 12"], cities=[CITY_X])
    plain = ident(parcels=["8501/7"], addresses=["הנרקיס 12"], cities=[CITY_X])
    assert rel(three, four) == (P.SIBLINGS, P.REASON_SUB_PARCELS)
    assert rel(three, plain) == (P.CANDIDATE, P.REASON_SUB_MISSING), "one side states a sub-parcel and the other none"
    assert rel(plain, plain) == (P.PARCEL, "parcel 8501/7")
    assert rel(three, three) == (P.PARCEL, "parcel 8501/7")
    # the same street, number and city, parcels stated by one side only: the apartment decides
    a3 = ident(addresses=["הנרקיס 12"], cities=[CITY_X], apartments=["3"])
    a4 = ident(addresses=["הנרקיס 12"], cities=[CITY_X], apartments=["4"])
    assert rel(a3, a4)[0] == P.SIBLINGS
    assert rel(a3, ident(addresses=["הנרקיס 12"], cities=[CITY_X]))[0] == P.CANDIDATE
    assert rel(a3, ident(addresses=["הנרקיס 12"], cities=[CITY_X], apartments=["3"]))[0] == P.ADDRESS_CITY
    # sub-parcels 3 and 4 at the same street, number and city with a parcel on one side only
    s3 = ident(parcels=["8501/7/3"], addresses=["הנרקיס 12"], cities=[CITY_X])
    s4 = ident(addresses=["הנרקיס 12"], cities=[CITY_X], apartments=["4"])
    assert rel(s3, s4)[0] == P.CANDIDATE and not P.relate(s3, s4).gathered, "one side states a sub-parcel, the other none"


def test_a_different_address_without_parcels_is_unrelated_and_an_unidentified_unit_has_nothing_to_compare():
    a = ident(addresses=["הצאלון 7"], cities=[CITY_X])
    assert rel(a, ident(addresses=["הצאלון 11"], cities=[CITY_X]))[0] == P.UNRELATED
    nothing = rel(a, ident())
    assert nothing == (P.UNRELATED, P.REASON_NOTHING_TO_COMPARE)
    assert rel(ident(parcels=["7101/15"]), ident(addresses=["הצאלון 7"]))[1] == P.REASON_NOTHING_TO_COMPARE


@pytest.mark.parametrize("name", ["cities", "parcels", "apartments", "compendium_two", "compendium_one", "beyond_window"])
def test_the_round_12_manifest_relations_are_reproduced(name):
    """Every expected relation of a manifest group (identity from the manifest's own unit identities)."""
    docs = MANIFEST["documents"]
    group = MANIFEST["groups"][name]
    assert group["relations"]

    def of(ref) -> UnitIdentity:
        unit = next(u for u in docs[ref["document"]]["units"] if u["unit_no"] == ref["unit"])
        i = unit["identity"]
        city = normalize_place(i["city"]) if i["city"] else None
        return ident(parcels=[f"{i['block']}/{p}" + (f"/{i['sub_parcel']}" if i["sub_parcel"] else "")
                              for p in i["parcels"]] if i["block"] else [],
                     addresses=[f"{i['street']} {i['number']}"], cities=[city] if city else [])

    for r in group["relations"]:
        link = P.relate(of(r["a"]), of(r["b"]))
        assert (link.relation, link.reason, link.gathered) == (r["relation"], r["reason"], r["gathered"]), r


# --- the closure (AE2) -----------------------------------------------------------------------------------------------

def test_ae2_the_focus_b_gathers_a_with_its_parcel_reason_and_never_c():
    a = node("A", parcels=["7101/24", "7101/40"])
    b = node("B", parcels=["7101/40"])
    c = node("C", parcels=["7101/42"])
    g = graph(a, b, c).gather(b.key)
    assert [m.key for m in g.members] == [a.key, b.key]
    assert g.seed == b.key
    (joined,) = g.joined
    assert (joined.node.key, joined.via, joined.link.relation, joined.link.reason) == (a.key, b.key, P.PARCEL, "parcel 7101/40")
    assert c.key not in {m.key for m in g.members}
    # from A: B joins, and C{42} shares no parcel with A
    assert {m.key for m in graph(a, b, c).gather(a.key).members} == {a.key, b.key}


def test_a_unit_does_not_join_through_a_member_when_it_conflicts_with_another_member():
    """A{24,40}, B{40}, D{24}: from the focus B, D links to A by parcel 24 but its parcels are disjoint from B's."""
    a, b, d = node("A", parcels=["7101/24", "7101/40"]), node("B", parcels=["7101/40"]), node("D", parcels=["7101/24"])
    assert {m.key for m in graph(a, b, d).gather(b.key).members} == {a.key, b.key}
    assert {m.key for m in graph(a, b, d).gather(d.key).members} == {a.key, d.key}


def test_sub_parcels_3_and_4_never_gather_each_other_and_the_unspecified_one_is_a_named_candidate():
    s3 = node("S3", parcels=["8501/7/3"], addresses=["הנרקיס 12"], cities=[CITY_X])
    s4 = node("S4", parcels=["8501/7/4"], addresses=["הנרקיס 12"], cities=[CITY_X])
    sn = node("SN", parcels=["8501/7"], addresses=["הנרקיס 12"], cities=[CITY_X])
    g = graph(s3, s4, sn).gather(s3.key)
    assert [m.key for m in g.members] == [s3.key]
    assert [(c.node.key, c.link.reason) for c in g.candidates] == [(sn.key, P.REASON_SUB_MISSING)]
    assert s4.key not in {c.node.key for c in g.candidates} | {m.key for m in g.members}
    assert {m.key for m in graph(s3, s4, sn).gather(s4.key).members} == {s4.key}


def test_ae1_the_cityless_unit_is_an_unlinked_candidate_and_the_other_city_is_neither():
    x = node("X", addresses=["הדולב 7"], cities=[CITY_X])
    y = node("Y", addresses=["הדולב 7"], cities=[CITY_Y])
    n = node("N", addresses=["הדולב 7"])
    g = graph(x, y, n).gather(x.key)
    assert [m.key for m in g.members] == [x.key]
    assert [(c.node.key, c.link.reason) for c in g.candidates] == [(n.key, P.REASON_CITY_MISSING)]
    assert y.key not in {c.node.key for c in g.candidates}, "a stated other city is proof it is another property"
    # a candidate that conflicts with another gathered member is no candidate: it is proven to be another property
    z = node("Z", addresses=["הדולב 7"], cities=[CITY_X], parcels=["9000/1"])
    w = node("W", addresses=["הדולב 9"], cities=[CITY_X], parcels=["9000/1"])
    cityless = node("N2", addresses=["הדולב 7"], parcels=["9000/2"])  # disjoint parcels from Z and W: other, not a candidate
    gathered = graph(z, w, cityless).gather(z.key)
    assert {m.key for m in gathered.members} == {z.key, w.key} and not gathered.candidates


def test_same_street_number_and_city_with_disjoint_stated_parcels_is_other_and_does_not_gather():
    a = node("A", addresses=["הצאלון 7"], cities=[CITY_X], parcels=["7101/15"])
    b = node("B", addresses=["הצאלון 7"], cities=[CITY_X], parcels=["7101/16"])
    assert [m.key for m in graph(a, b).gather(a.key).members] == [a.key]


def test_units_with_the_same_address_and_city_gather_by_address_city():
    a = node("A", addresses=["הצאלון 7"], cities=[CITY_X])
    b = node("B", addresses=["הצאלון 7"], header_city=CITY_X)
    g = graph(a, b).gather(a.key)
    assert [(j.node.key, j.link.relation, j.link.reason) for j in g.joined] == [(b.key, P.ADDRESS_CITY, "address_city")]


def test_a_hidden_unit_is_not_in_the_graph_so_units_that_only_it_linked_do_not_link():
    """With B visible, A{24} and C{42} are both in B{24,42}'s property; without B they never link."""
    a, c = node("A", parcels=["7101/24"]), node("C", parcels=["7101/42"])
    b = node("B", parcels=["7101/24", "7101/42"])
    visible = graph(a, b, c)
    assert {m.key for m in visible.gather(b.key).members} == {a.key, b.key}, "C conflicts with A, so it stays out"
    hidden = graph(a, c)
    assert hidden.link(a.key, c.key).relation == P.OTHER
    assert [m.key for m in hidden.gather(a.key).members] == [a.key]
    # A{40} and C{40} link directly by the parcel, with or without the hidden B
    a40, c40 = node("A", parcels=["7101/40"]), node("C", parcels=["7101/40"])
    assert {m.key for m in graph(a40, c40).gather(a40.key).members} == {a40.key, c40.key}


# --- the partition: non-focus grouping depends on neither seed nor order ------------------------------------------------

def partition_of(nodes, focus=None):
    g = graph(*nodes)
    part = g.partition(g.gather(focus) if focus else None)
    return {frozenset(m.key for m in grp.members) for grp in part.groups}


def test_non_focus_grouping_of_a_conflicting_component_is_all_singletons_in_any_order():
    base = [node("A", parcels=["7101/24", "7101/40"]), node("B", parcels=["7101/40"]), node("C", parcels=["7101/24"])]
    expected = {frozenset({n.key}) for n in base}
    for perm in itertools.permutations(base):
        assert partition_of(perm) == expected
    other = node("Z", parcels=["9999/1"])
    assert partition_of([*base, other], focus=other.key) == expected | {frozenset({other.key})}


def test_non_focus_grouping_gathers_a_pairwise_compatible_component():
    nodes = [node("A", parcels=["7101/24", "7101/40"]), node("B", parcels=["7101/24"]), node("C", parcels=["7101/24"]),
             node("D", parcels=["8000/1"]), node("E")]
    rng = random.Random(3)
    results = []
    for _ in range(6):
        shuffled = nodes[:]
        rng.shuffle(shuffled)
        results.append(partition_of(shuffled))
    assert all(r == results[0] for r in results)
    assert frozenset({("A", 1), ("B", 1), ("C", 1)}) in results[0], "a pairwise compatible component is one group"
    assert frozenset({("D", 1)}) in results[0] and frozenset({("E", 1)}) in results[0], "no identity: its own group"


def test_the_focus_set_is_a_group_of_the_partition_and_the_rest_group_without_it():
    a, b, c = node("A", parcels=["7101/24", "7101/40"]), node("B", parcels=["7101/40"]), node("C", parcels=["7101/24"])
    g = graph(a, b, c)
    part = g.partition(g.gather(b.key))
    assert part.group_of(a.key).id == part.group_of(b.key).id
    assert part.group_of(c.key).id != part.group_of(a.key).id
    assert part.group_of(("missing", 1)) is None
    assert "7101" in part.group_of(a.key).label


def test_group_labels_come_from_the_identity_and_two_candidates_have_separate_groups():
    x = node("X", addresses=["הדולב 7"], cities=[CITY_X])
    n = node("N", addresses=["הדולב 7"])
    g = graph(x, n)
    part = g.partition(g.gather(x.key))
    assert part.group_of(x.key).id != part.group_of(n.key).id
    assert "הדולב 7" in part.group_of(x.key).label and "הדולב 7" in part.group_of(n.key).label
    p = node("P", parcels=["7101/15"])
    assert "7101" in graph(p).partition(None).group_of(p.key).label and "15" in graph(p).partition(None).group_of(p.key).label


# --- the facts U7 reads ------------------------------------------------------------------------------------------------

def test_undetermined_units_have_no_identity_or_nothing_to_compare_with_the_property():
    focus = node("F", parcels=["7101/15"])
    blank = node("B")                                              # no identity facts
    address_only = node("A", addresses=["הצאלון 7"])               # nothing in common with a parcel-only property
    other_city = node("C", addresses=["הצאלון 7"], cities=[CITY_Y], parcels=["7102/1"])
    foreign = node("D", parcels=["9000/1"])                        # a parcel that excludes it
    g = graph(focus, blank, address_only, other_city, foreign).gather(focus.key)
    assert [n.key for n in g.undetermined] == [address_only.key, blank.key], "in key order"
    assert g.identified


def test_an_unidentified_focus_gathers_only_itself_and_says_so():
    blank, other = node("B"), node("O", parcels=["7101/15"])
    g = graph(blank, other).gather(blank.key)
    assert [m.key for m in g.members] == [blank.key] and not g.identified and not g.joined


def test_determinism_same_input_in_any_order_gives_the_same_set_order_and_reasons():
    nodes = [node(f"U{i}", unit_no=(i % 3) + 1, parcels=["7101/40"] + (["7101/24"] if i % 2 else []),
                  addresses=[f"הדס {i}"], cities=[CITY_X]) for i in range(8)]
    seen = []
    for seed in range(5):
        shuffled = nodes[:]
        random.Random(seed).shuffle(shuffled)
        g = graph(*shuffled).gather(nodes[0].key)
        seen.append(([m.key for m in g.members], [(j.node.key, j.via, j.link.relation, j.link.reason) for j in g.joined],
                     [(c.node.key, c.link.reason) for c in g.candidates], [n.key for n in g.undetermined]))
    assert all(s == seen[0] for s in seen) and len(seen[0][0]) == 8


def test_a_compendium_of_two_properties_is_two_gathered_sets():
    units = [("P", 1, "8601/5"), ("Q", 1, "8601/6")] * 3
    nodes = [node("K", unit_no=i + 1, parcels=[parcel]) for i, (_, _, parcel) in enumerate(units)]
    g = graph(*nodes).gather(nodes[0].key)
    assert [m.key[1] for m in g.members] == [1, 3, 5]
    assert {link.reason for link in (j.link for j in g.joined)} == {"parcel 8601/5"}
