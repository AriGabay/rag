"""Round 13 U4 (R3, R4, R5, R6, R10; KTD3, KTD4, KTD5; AE1-AE4): one decision — same, different or undecided — for two data,
read by the timeline, the coverage conflict reason and the "latest" claim.

Two data of one base key (the property, the metric family, the normalized unit, period, VAT, basis, scenario and stance) are
decided by their facets (``datum.relation``); the "same" pairs join into classes (``timeline.buckets``), a conflict is a pair
of different amounts in overlapping periods inside one class, and a pair across two classes that could not be compared is
marked ``undecided``. The expected decisions are the round-13 manifest's (``tests/fixtures/round13/manifest.json``), read
from the same synthetic cases the fixtures describe: the data are built here from the manifest's own facets, kinds and
numbers, so the test does not depend on the extraction of the .docx files. Database-free; synthetic data only."""

from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from app.chat import coverage, datum, timeline
from app.measurements.values import parse_amount
from tests.unit.test_timeline import Office, as_of

MANIFEST = json.loads((Path(__file__).resolve().parents[1] / "fixtures" / "round13" / "manifest.json")
                      .read_text(encoding="utf-8"))
DOCUMENTS = MANIFEST["documents"]
HEADING = re.compile(r"^\d+\.\s")


def _section_of(blocks: list[str], needle: str) -> str:
    """The numbered heading above the block that holds ``needle`` in a unit's blocks ("" when none)."""
    heading = ""
    for b in blocks:
        if HEADING.match(b):
            heading = b
        elif needle and (b == needle or needle in b):
            return heading
    return ""


class Case:
    """The workspace of a manifest case: every datum of its documents as a registered value (V#), a property per
    ``identity`` address, a version per document, the valuation date as the date, and the section and table the manifest's
    ``where`` says. ``ids`` maps a manifest data id to its V#."""

    def __init__(self, case_id: str) -> None:
        self.o = Office()
        self.ids: dict[str, str] = {}
        spec = next(c for c in MANIFEST["cases"] if c["id"] == case_id)
        for key in spec["documents"]:
            doc = DOCUMENTS[key]
            d = self.o.doc("group:" + doc["identity"]["address"], title=doc["title"])
            version = uuid.uuid4()
            for item in doc["data"]:
                table = item["where"].get("table")
                number = re.search(r"\d[\d,]*(?:\.\d+)?", item["shown"]).group(0)
                kw: dict = {}
                if item["unit"] in ("ILS_per_sqm", "sqm"):
                    kw["area_basis"] = "נטו"
                if table:
                    kw["table"] = (str(version), table["caption"])
                    kw["section"] = _section_of(doc["unit_blocks"], table["caption"])
                else:
                    kw["section"] = _section_of(doc["unit_blocks"], item["where"]["quote"])
                self.ids[item["id"]] = self.o.value(
                    d, number, as_of(doc["valuation_date"]), kind=item["kind"], unit=item["unit"], version=version,
                    facet=item["facet"], **kw)
        self.ws = self.o.ws

    def sids(self, *names: str) -> set[str]:
        return {self.ids[n] for n in names}

    def bucket(self, *names: str) -> timeline.Bucket:
        (bucket,) = [b for b in timeline.buckets(self.ws, self.sids(*names))
                     if all(self.ids[n] in {d.ref for d in b.members} for n in names)]
        return bucket

    def datum(self, bucket: timeline.Bucket, name: str) -> timeline.Datum:
        return next(d for d in bucket.members if d.ref == self.ids[name])


def _refs(pairs) -> set[frozenset[str]]:
    return {frozenset((a.ref, b.ref)) for a, b in pairs}


_CASES = {c["id"]: c for c in MANIFEST["cases"]}


@pytest.mark.parametrize("pair", MANIFEST["pairs"], ids=lambda p: f"{p['case']}:{p['a']}~{p['b']}")
def test_every_manifest_pair_is_decided_as_the_manifest_says(pair):
    case = Case(pair["case"])
    bucket = case.bucket(pair["a"], pair["b"])
    a, b = case.datum(bucket, pair["a"]), case.datum(bucket, pair["b"])
    decided = bucket.relation(a, b)
    assert decided == pair["decision"], pair["why"]
    key = frozenset((a.ref, b.ref))
    assert (key in _refs(bucket.conflicts())) is pair["conflict"], pair["why"]
    # the mark is decided at class level (round 13 U6): a pair the manifest marks undecided carries no mark when any pair across
    # its two classes is decided different (the relation of the pair itself, asserted above, is unchanged)
    classes = [bucket.classes[bucket.class_of(d)] for d in (a, b)]
    marked = pair["undecided_mark"] and bucket.class_relation(*classes) == datum.UNDECIDED
    assert (key in _refs(bucket.undecided())) is marked, pair["why"]
    # the amounts are compared as the manifest says: a percent against a fraction is the same amount
    assert (a.amount == b.amount) is pair["amounts_equal"], pair["why"]


# --- AE1: complementary coefficients -------------------------------------------------------------------------------------

def test_ae1_two_coefficients_of_one_table_are_two_classes_no_conflict_no_timeline_block():
    c = Case("AE1")
    both = c.sids("land.coef_building", "land.coef_sharing")
    assert not coverage.conflicting(c.ws)
    assert [len(g) for g in timeline.groups(c.ws, both)] == [1, 1]
    assert c.o.blocks(both) == []  # no timeline block joins them
    bucket = c.bucket("land.coef_building", "land.coef_sharing")
    assert len(bucket.classes) == 2 and bucket.conflicts() == [] and bucket.undecided() == []
    assert bucket.class_relation(*bucket.classes) == datum.DIFFERENT


# --- AE2: rates of different cash flows ------------------------------------------------------------------------------------

def test_ae2_rates_of_different_cash_flows_are_separate_classes_and_the_bare_rate_is_undecided_against_all():
    c = Case("AE2")
    rates = [n for n in c.ids if n.startswith("cap.")]
    bucket = c.bucket(*rates)
    assert len(bucket.classes) == 6 and bucket.conflicts() == []
    assert not coverage.conflicting(c.ws)
    expected = {frozenset((c.ids["cap.bare"], c.ids[n])) for n in rates if n != "cap.bare"}
    assert _refs(bucket.undecided()) == expected
    assert _CASES["AE2"]["expected"]["conflict_marks"] == 0


def test_ae2_the_undecided_mark_names_the_counterpart_and_is_neither_a_conflict_nor_agreement():
    c = Case("AE2")
    # an earlier appraisal of the same property states the offices rate too, which gives its class a history of two rows
    d2 = c.o.doc("group:" + DOCUMENTS["cap_rates"]["identity"]["address"], title="שומה מוקדמת")
    earlier = c.o.value(d2, "6", as_of("2022-09-30"), kind="rate", unit="percent",
                        facet=datum.facet("התזרים דמי שכירות משרדים", "sentence", "own"), section="3")
    offices, bare = c.ids["cap.offices"], c.ids["cap.bare"]
    # round 13 U6 (KTD7): the rates of the appraisal's own date are its components table; the timeline keeps the earlier row
    (block,) = c.o.blocks({offices})
    assert [r["sid"] for r in block["rows"]] == [earlier] and block["rows"][0]["marks"] == []  # 2022 and 2023 do not overlap
    (table,) = timeline.breakdown(c.ws, {offices})
    rows = {r["sid"]: r for r in table["rows"]}
    assert earlier not in rows and offices in rows and bare in rows
    # round 13 follow-up: the mark is on the unresolved side only. The bare rate (no facet) is undecided against every rate with
    # one and names them; the offices rate, whose only undecided partner is the bare rate, carries no mark
    assert rows[offices]["marks"] == [] and "undecided_with" not in rows[offices]
    assert rows[bare]["marks"] == ["undecided"] and offices in rows[bare]["undecided_with"]
    assert set(rows[bare]["undecided_with"]) <= {r["sid"] for r in table["rows"]} - {bare}
    assert f"{timeline.MARKS['undecided']} ({offices}" in timeline.plain_text([], [table])
    assert not coverage.conflicting(c.ws)


def test_equal_numbers_of_two_cash_flows_stay_two_rows():
    c = Case("AE2")
    bucket = c.bucket("cap.parking", "cap.storage")
    parking, storage = c.datum(bucket, "cap.parking"), c.datum(bucket, "cap.storage")
    assert parking.amount == storage.amount
    assert len(timeline._dedupe([parking, storage])) == 2
    twin = timeline.Datum(**{**parking.__dict__, "ref": "V99", "index": 99})
    assert len(timeline._dedupe([parking, twin])) == 1  # the same facet, document, date and amount: one row


# --- AE3: a real conflict in one component ---------------------------------------------------------------------------------

def test_ae3_the_same_consideration_conflicts_across_appraisals_but_the_equal_written_forms_do_not():
    c = Case("AE3")
    coefs = [f"conflict_{n}.coef" for n in (1, 2, 3, 4)]
    bucket = c.bucket(*coefs)
    expected = _CASES["AE3"]["expected"]
    assert _refs(bucket.conflicts()) == {frozenset(c.sids(*p)) for p in expected["conflicting_pairs"]}
    assert coverage.conflicting(c.ws)
    # round 13 U6 (KTD7): each coefficient sits beside its appraisal's other coefficient at the one date, so its row is in that
    # appraisal's components block (a row belongs to one block), and the conflict mark is on it there
    assert c.o.blocks(c.sids(*coefs)) == []
    marks = {r["sid"]: r["marks"] for b in timeline.breakdown(c.ws, c.sids(*coefs)) for r in b["rows"]}
    assert all("conflict" in marks[c.ids[n]] for n in coefs)
    # the other coefficient of each appraisal (1.1 in all four) is a datum of its own, and 1.1 = 1.1
    locations = [f"conflict_{n}.location" for n in (1, 2, 3, 4)]
    assert not bucket.conflicts(lambda d: d.ref in c.sids(*locations))


def test_ae3_a_percent_and_a_fraction_are_one_amount():
    c = Case("AE3")
    bucket = c.bucket("conflict_1.coef", "conflict_3.coef")
    one, three = (c.datum(bucket, n) for n in ("conflict_1.coef", "conflict_3.coef"))
    assert one.fields == three.fields  # the unit is normalized: percent and ratio of a coefficient meet
    assert one.amount == three.amount == Decimal("0.8")


# --- AE4: different names for one component --------------------------------------------------------------------------------

def test_ae4_one_cash_flow_written_two_ways_is_decided_same_and_compared():
    c = Case("AE4")
    bucket = c.bucket("forms_1.first", "forms_2.first")
    a, b = c.datum(bucket, "forms_1.first"), c.datum(bucket, "forms_2.first")
    assert bucket.class_of(a) == bucket.class_of(b)
    assert _refs(bucket.conflicts()) == {frozenset(c.sids("forms_1.first", "forms_2.first"))}
    assert coverage.conflicting(c.ws)
    # the second cash flow is the same datum in both appraisals, with the same rate: equal, no conflict
    second = c.bucket("forms_1.second", "forms_2.second")
    assert second.class_of(c.datum(second, "forms_1.second")) == second.class_of(c.datum(second, "forms_2.second"))


# --- the unlabelled history ------------------------------------------------------------------------------------------------

def test_two_appraisals_each_with_one_unlabelled_rate_are_one_datum_and_form_a_timeline_block():
    c = Case("history")
    (block,) = c.o.blocks(c.sids("history_1.rate"))
    assert [r["value_text"] for r in block["rows"]] == ["8", "7"]  # newest first
    assert all(r["marks"] == [] for r in block["rows"])
    assert not coverage.conflicting(c.ws)


# --- the decision table (KTD3) ---------------------------------------------------------------------------------------------

UNIT_A, UNIT_B = ("v1", None), ("v2", None)


def site(written: str | None, *, tier: str = "own", family: str = "proportion", unit=UNIT_A, table=None, section: str = "",
         facet: dict | None = None) -> datum.Site:
    f = facet if facet is not None else (datum.facet(written, "row", tier) if written else datum.none())
    return datum.Site(family, f, unit, table, section)


def test_two_own_facets_with_equal_non_empty_cores_are_the_same_whatever_the_unit_or_section():
    a = site("תזרים דמי שכירות ממשרדים", unit=UNIT_A)
    b = site("התזרים דמי שכירות משרדים", unit=UNIT_B, section="אחר")
    assert datum.relation(a, b) == datum.SAME


def test_ae4_prefix_plural_and_quote_marks_do_not_make_two_facets_two():
    a = site('שטחי "מסחר" בתב״ע', family="proportion", unit=UNIT_A)
    b = site('השטחים למסחר בתב"ע', family="proportion", unit=UNIT_B)
    assert datum.core_of(a) == datum.core_of(b)
    assert datum.relation(a, b) == datum.SAME


def test_two_equal_context_tier_cores_are_undecided_not_the_same():
    a = site("תזרים הכנסות משטחי פרסום", tier="context", section="3")
    b = site("תזרים הכנסות משטחי פרסום", tier="context", section="3")
    assert datum.relation(a, b) == datum.UNDECIDED
    assert datum.relation(site("תזרים הכנסות משטחי פרסום", tier="context"), site("תזרים הכנסות משטחי פרסום")) == datum.UNDECIDED


def test_both_headline_data_are_the_same_in_a_family_with_no_facet_or_for_the_only_value_of_the_key_in_the_unit():
    a, b = site(None, family="value", unit=UNIT_A), site(None, family="value", unit=UNIT_B)
    assert datum.relation(a, b, 3, 3) == datum.SAME  # a total needs no facet, however many the unit holds
    a, b = site(None), site(None, unit=UNIT_B)
    assert datum.relation(a, b) == datum.SAME  # the only rate of its key in each unit
    assert datum.relation(a, b, 2, 1) == datum.UNDECIDED  # a unit that holds another: unresolved


def test_two_facets_that_reduce_to_an_empty_core_in_a_component_family_are_undecided_in_a_unit_holding_both():
    generic = datum.facet("שיעור היוון", "row", "own")  # only the metric words: no component
    a, b = site(None, facet=generic), site(None, facet=generic)
    assert not datum.core_of(a)
    assert datum.relation(a, b, 2, 2) == datum.UNDECIDED
    assert datum.relation(a, b, 1, 1) == datum.SAME  # the only one of its key in its unit: the headline datum


def test_each_core_keeping_a_word_the_other_lacks_in_one_table_list_or_section_is_different():
    a = site("מקדם מיקום", family="coefficient", table=("v1", "t"), unit=UNIT_A)
    b = site("מקדם צורת מגרש", family="coefficient", table=("v1", "t"), unit=UNIT_A)
    assert datum.relation(a, b) == datum.DIFFERENT
    a, b = a._replace(table=None, section="3. היוון"), b._replace(table=None, section="3. היוון")
    assert datum.relation(a, b) == datum.DIFFERENT  # the same section path (a list sits in one section)
    assert datum.relation(a, b._replace(appraisal=UNIT_B)) == datum.UNDECIDED  # disjoint across units
    # round 13 follow-up: two own facets of one appraisal unit are different in any section, with nothing saying they sit together
    assert datum.relation(a, b._replace(section="4. סיכום")) == datum.DIFFERENT
    assert datum.relation(a._replace(section=""), b._replace(section="")) == datum.DIFFERENT
    assert datum.relation(a._replace(appraisal=None), b._replace(appraisal=None, section="4. סיכום")) == datum.UNDECIDED
    # a context-tier facet (caption, lead-in, heading) still needs the same table, list or section; own against context neither
    ctx = [site(w, tier="context", family="coefficient", section=sec) for w, sec in
           (("מקדם מיקום", "3. היוון"), ("מקדם צורת מגרש", "3. היוון"), ("מקדם צורת מגרש", "4. סיכום"))]
    assert datum.relation(ctx[0], ctx[1]) == datum.DIFFERENT  # context tier, one section
    assert datum.relation(ctx[0], ctx[2]) == datum.UNDECIDED  # context tier, another section: no structural evidence
    assert datum.relation(a, ctx[2]) == datum.UNDECIDED  # one own, one context, another section


def test_a_subset_is_undecided_and_so_is_a_stated_facet_against_a_headline_value():
    a = site("מקדם הפחתה בשל מגבלות בנייה", family="coefficient", section="3")
    b = site("מקדם הפחתה בשל מגבלות", family="coefficient", section="3")
    assert datum.core_of(b) < datum.core_of(a)
    assert datum.relation(a, b) == datum.UNDECIDED  # one keeps a word the other lacks, not both
    assert datum.relation(a, site(None, family="coefficient", section="3"), 1, 1) == datum.UNDECIDED


def test_the_decision_is_symmetric():
    a = site("מקדם מיקום", family="coefficient", section="3")
    b = site("מקדם צורת מגרש", family="coefficient", section="3")
    c = site(None, family="coefficient", section="3")
    for x, y in ((a, b), (a, c), (c, c)):
        assert datum.relation(x, y, 2, 1) == datum.relation(y, x, 1, 2)


# --- the canonical amount (KTD5) ------------------------------------------------------------------------------------------

def test_amounts_are_compared_normalized_scaled_and_as_fractions():
    assert datum.canonical_amount(Decimal("8.0")) == datum.canonical_amount(Decimal("8")) == Decimal(8)
    assert datum.canonical_amount(Decimal("2450"), scale=1000) == datum.canonical_amount(Decimal("2450000"))
    assert datum.canonical_amount(Decimal("80"), unit="percent", family="coefficient") == Decimal("0.8")
    assert datum.canonical_amount(Decimal("0.80"), unit="ratio", family="coefficient") == Decimal("0.8")
    assert datum.canonical_amount(Decimal("7.25"), unit="percent", family="proportion") == Decimal("0.0725")
    assert datum.canonical_amount(Decimal("80"), unit="percent", family="value") == Decimal(80)  # not a fraction family
    assert datum.canonical_amount(None) is None


def test_a_measurements_amount_is_read_from_its_written_form_a_range_by_its_bounds():
    assert datum.canonical_measurement(None, "8%", unit="percent", family="proportion") == Decimal("0.08")
    assert datum.canonical_measurement(Decimal("8"), "8", unit="percent", family="proportion") == Decimal("0.08")
    assert datum.canonical_measurement(None, "65-85 ₪") == (Decimal(65), Decimal(85))
    assert datum.canonical_measurement(None, "6.5-7.5%", unit="percent", family="proportion") == (
        Decimal("0.065"), Decimal("0.075"))
    assert datum.canonical_measurement(None, "ללא מספר") is None
    assert parse_amount("65-85 ₪").form == "range"


def test_a_value_and_a_measurement_of_one_datum_are_equal_and_a_scaled_value_compares_by_its_amount():
    o = Office()
    d = o.doc()
    when = as_of("2024-01-01")
    v = o.value(d, "8.0", when, kind="rate", unit="percent", period="none")
    m = o.measurement(d, "8", when, metric_kind="rate", unit="percent", period="none")
    assert not coverage.conflicting(o.ws)  # V# 8.0 and M# "8": one amount
    (bucket,) = timeline.buckets(o.ws, {v, m})
    assert [len(c) for c in bucket.classes] == [2]
    o2 = Office()
    d2 = o2.doc()
    o2.value(d2, "2,450", as_of("2024-01-01"), scale=1000)  # a table "(באלפי ₪)"
    o2.value(d2, "2,450,000", as_of("2024-01-01"))
    assert not coverage.conflicting(o2.ws)
    o2.value(d2, "2,500", as_of("2024-01-01"), scale=1000)
    assert coverage.conflicting(o2.ws)


def test_two_headline_values_of_one_property_and_date_with_different_amounts_still_conflict():
    o = Office()
    a = o.value(o.doc(), "2,410,000", as_of("2025-01-20"))
    o.value(o.doc(), "2,300,000", as_of("2025-01-20"))
    assert coverage.conflicting(o.ws)
    (block,) = o.blocks({a})
    assert all(r["marks"] == ["conflict"] for r in block["rows"])


# --- one decision for the timeline and the coverage conflict ------------------------------------------------------------------

def test_a_conflict_is_only_inside_a_class_so_coverage_ignores_undecided_pairs():
    c = Case("AE3")
    only = c.sids("conflict_1.coef", "conflict_2.location")
    assert not coverage.conflicting(c.ws, ids=only)  # undecided across units: marked, never a conflict
    assert coverage.conflicting(c.ws, ids=c.sids("conflict_1.coef", "conflict_2.coef"))
    assert not coverage.conflicting(c.ws, ids=c.sids("conflict_1.coef", "conflict_4.coef"))  # 0.8 = 0.80


# --- recency (KTD4) --------------------------------------------------------------------------------------------------------

def _rate(o: Office, doc, version, amount: str, iso: str, facet: dict, **kw) -> str:
    return o.value(doc, amount, as_of(iso), kind="rate", unit="percent", period="none", version=version, facet=facet, **kw)


def test_a_newer_value_of_a_component_decided_different_is_no_later_value_of_the_cited_one():
    o = Office()
    doc, version = o.doc(), uuid.uuid4()
    offices = datum.facet("תזרים דמי שכירות ממשרדים", "row", "own")
    parking = datum.facet("תזרים דמי שכירות מחניות", "row", "own")
    old = _rate(o, doc, version, "6.5", "2021-01-01", offices, section="3. היוון")
    _rate(o, doc, version, "7.5", "2024-01-01", parking, section="3. היוון")  # a newer rate, but of another cash flow
    assert timeline.later_than(o.ws, {old}) == []
    assert timeline.later_undecided(o.ws, {old}) == []


def test_a_newer_value_of_an_undecided_class_is_named_and_the_decided_later_value_is_not_affected():
    o = Office()
    cited = _rate(o, o.doc(), uuid.uuid4(), "6.5", "2021-01-01", datum.facet("תזרים דמי שכירות ממשרדים", "row", "own"),
                  section="3")
    other = o.doc()
    newer = _rate(o, other, uuid.uuid4(), "7.5", "2024-01-01", datum.none(), section="3")  # facet unresolved, another unit
    assert timeline.later_than(o.ws, {cited}) == []
    ((older, found),) = timeline.later_undecided(o.ws, {cited})
    assert (older.ref, found.ref) == (cited, newer)


def test_the_same_datum_in_a_later_appraisal_is_a_later_value():
    o = Office()
    facet = datum.facet("תזרים דמי שכירות ממשרדים", "row", "own")
    old = _rate(o, o.doc(), uuid.uuid4(), "6.5", "2021-01-01", facet)
    new = _rate(o, o.doc(), uuid.uuid4(), "7.5", "2024-01-01", facet.copy() | {"written": "התזרים דמי שכירות משרדים"})
    ((older, newer),) = timeline.later_than(o.ws, {old})
    assert (older.ref, newer.ref) == (old, new)
    assert timeline.later_undecided(o.ws, {old}) == []
