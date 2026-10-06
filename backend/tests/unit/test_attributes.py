"""Attribute registry matching rules and the structured compute whitelist (U6, KTD7). No database."""

from decimal import Decimal

import pytest

from app.answering.attributes import STRUCTURED_ATTRIBUTES, canonical_unit_for, labels_match, proposed_key
from app.answering.conditions import QueryConditions
from app.answering.templates import structured_text
from app.appraisal.query import (
    OPERATIONS,
    STRUCTURED_COLUMNS,
    RecordResult,
    UnsupportedColumn,
    compute_records,
)


@pytest.mark.parametrize(("a", "b"), [
    ("שטח הממ״ד", "שטח ממ״ד"),
    ('שטח הממ"ד', "שטח ממ״ד"),       # ASCII gershayim
    ("  שטח   ממ״ד ", "שטח ממ״ד"),
    ("שטח של הממ״ד", "שטח ממ״ד"),
    ("גודל הממ״ד", "גודל ממ״ד"),
    ("מחיר למ״ר", "מחיר מ״ר"),
])
def test_prefix_and_article_normalization_matches(a, b):
    assert labels_match(a, b) and labels_match(b, a)


@pytest.mark.parametrize(("a", "b"), [
    ("שטח מחסן", "שטח ממ״ד"),   # trigram ~0.4, but a different attribute
    ("שטח", "שטח ממ״ד"),
    ("גודל ממ״ד", "שטח ממ״ד"),  # semantic synonyms merge only via an interpreter-chosen handle
    ("", "שטח"),
])
def test_different_labels_never_match(a, b):
    assert not labels_match(a, b)


def test_proposed_key_is_stable_under_normalization():
    assert proposed_key('שטח ממ"ד') == proposed_key("שטח  ממ״ד")
    assert proposed_key("שטח ממ״ד") != proposed_key("שטח מחסן")


def test_dimension_to_canonical_unit():
    assert canonical_unit_for("area") == "sqm"
    assert canonical_unit_for("length") == "m"
    assert canonical_unit_for("count") == "unit"
    assert canonical_unit_for("currency") == "ILS"
    assert canonical_unit_for("year") == "year"
    assert canonical_unit_for("percent") == "percent"
    assert canonical_unit_for("no-such-dimension") is None
    assert canonical_unit_for(None) is None


def test_structured_attributes_use_only_whitelisted_columns():
    assert {a["column"] for a in STRUCTURED_ATTRIBUTES} == set(STRUCTURED_COLUMNS)


def test_non_whitelisted_column_is_rejected_before_any_sql():
    c = QueryConditions(data_kind="transaction_price")
    for column in ("users.password_hash", "t.area; DROP TABLE users", "transactions.match_key", "area"):
        with pytest.raises(UnsupportedColumn, match="not a structured attribute column"):
            compute_records(None, c, column, "mean")
    with pytest.raises(ValueError, match="unknown operation"):
        compute_records(None, c, "transactions.area", "mode")
    with pytest.raises(ValueError, match="weighted_mean"):
        compute_records(None, c, "transactions.area", "weighted_mean")
    assert "weighted_mean" in OPERATIONS and "range" in OPERATIONS


def _result(op, values, **kw):
    vals = [Decimal(v) for v in values]
    return RecordResult(column="transactions.area", operation=op, count=len(vals), values=vals,
                        minimum=min(vals) if vals else None, maximum=max(vals) if vals else None, **kw)


def test_structured_text_states_label_operation_count_values_and_unit():
    c = QueryConditions(data_kind="transaction_price", neighborhood="חרוזים")
    body, limitations = structured_text(c, "שטח", "mean", _result("mean", ["50", "80.5", "100"], value=Decimal("76.83")),
                                        "sqm", uncertain=0)
    assert "שטח" in body and "ממוצע" in body and "76.83 מ״ר" in body
    assert "נמצאו 3 עסקאות ייחודיות" in body and "50, 80.5, 100" in body and "שכונת חרוזים" in body
    assert limitations and all("למ״ר" not in x for x in limitations)


def test_structured_text_range_and_count_and_many_values():
    c = QueryConditions(data_kind="transaction_price")
    many = [str(i) for i in range(1, 8)]
    body, _ = structured_text(c, "מספר חדרים", "range", _result("range", many, value=Decimal("6")), "room", uncertain=2)
    assert "טווח" in body and "1–7" in body and "הערכים" not in body  # values listed only when n <= 5
    body, lims = structured_text(c, "מספר חדרים", "count", _result("count", ["3", "4"], value=Decimal("2")), "room",
                                 uncertain=2)
    assert "מספר העסקאות" in body and "2" in body
    assert any("2 זוגות" in x for x in lims)
