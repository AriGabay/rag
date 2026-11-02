"""Smoke check of the synthetic clarification fixtures (round 26, U1): each generated file opens and holds the
strings ``truth.yaml`` lists for it, and none of the strings it says must be absent. No database."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from docx import Document

CLARIFY = Path(__file__).resolve().parents[1] / "fixtures" / "clarify_v1"
TRUTH = yaml.safe_load((CLARIFY / "truth.yaml").read_text(encoding="utf-8"))
FILES = TRUTH["files"]


def _text(path: Path) -> str:
    doc = Document(path)
    parts = [p.text for p in doc.paragraphs]
    parts += [c.text for t in doc.tables for r in t.rows for c in r.cells]
    return "\n".join(parts)


def test_truth_lists_every_file_and_every_kind():
    on_disk = {p.name for p in CLARIFY.glob("*.docx")}
    assert on_disk == {f["file"] for f in FILES}
    kinds = {f["kind"] for f in FILES}
    assert kinds == {"bound", "two_dates", "comparables", "gross_net", "conflict", "same_address_a",
                     "same_address_b"}
    assert sum(f["kind"] == "two_dates" for f in FILES) == 2


@pytest.mark.parametrize("entry", FILES, ids=[f["kind"] + str(i) for i, f in enumerate(FILES)])
def test_file_opens_and_holds_its_truth(entry):
    text = _text(CLARIFY / entry["file"])
    missing = [f["text"] for f in entry["facts"] if f["text"] not in text]
    assert not missing, missing
    present = [s for s in entry.get("absent", []) if s in text]
    assert not present, present


def test_bound_file_has_one_title_and_three_units():
    entry = next(f for f in FILES if f["kind"] == "bound")
    doc = Document(CLARIFY / entry["file"])
    titles = [p.text for p in doc.paragraphs if p.style.name == "Title"]
    assert len(titles) == entry["titled_units"] == 1
    assert sum(1 for f in entry["facts"] if f["key"].endswith(".valuation_date")) == entry["units"] == 3


def test_two_dates_differ_in_date_value_and_rent():
    a, b = (f for f in FILES if f["kind"] == "two_dates")
    for key in ("valuation_date", "value_ils", "rent_per_sqm_month_ils"):
        va = next(x["value"] for x in a["facts"] if x["key"] == key)
        vb = next(x["value"] for x in b["facts"] if x["key"] == key)
        assert va != vb, key


def test_comparables_means_differ_materially():
    entry = next(f for f in FILES if f["kind"] == "comparables")
    d = entry["derived_not_in_document"]
    assert entry["comparables"] == 6
    assert abs(d["simple_mean_price_per_sqm"] - d["area_weighted_mean_price_per_sqm"]) > 0.05 * (
        d["area_weighted_mean_price_per_sqm"])


def test_conflict_file_holds_two_different_values_in_two_tables():
    entry = next(f for f in FILES if f["kind"] == "conflict")
    doc = Document(CLARIFY / entry["file"])
    datum = entry["conflict"]["datum"]
    values = [r.cells[1].text for t in doc.tables for r in t.rows if r.cells[0].text == datum]
    assert len(values) == 2 and values[0] != values[1]


def test_same_address_pair_shares_street_number_but_not_neighbourhood():
    a, b = (f for f in FILES if f["kind"].startswith("same_address"))
    assert a["street_and_number"] == b["street_and_number"]
    assert a["neighbourhood"] != b["neighbourhood"]
