from datetime import date
from decimal import Decimal

from app.appraisal.extract import FieldValue, RecordDraft
from app.appraisal.validate import validate


def rec(**fields):
    r = RecordDraft("transaction_price", 3, 0, 1, None, False)
    base = {"city": "רמת גן", "transaction_date": date(2024, 3, 1)}
    for k, v in (base | fields).items():
        r.fields[k] = FieldValue(str(v), v)
    return r


def test_computed_price_per_sqm_has_lineage():
    v = validate(rec(price=Decimal("1000000"), area=Decimal("50")))
    assert v.computed_ppsqm == Decimal("20000.00")
    assert v.calc_definition == "price / area = 1000000 / 50"
    assert v.status == "auto_extracted" and not v.conflict


def test_stated_conflict_is_flagged_and_kept():
    v = validate(rec(price=Decimal("1000000"), area=Decimal("50"), price_per_sqm_stated=Decimal("21000")))
    assert v.conflict and v.status == "needs_review"


def test_stated_within_tolerance_is_not_a_conflict():
    v = validate(rec(price=Decimal("3000000"), area=Decimal("100"), price_per_sqm_stated=Decimal("30050")))
    assert not v.conflict


def test_missing_area_needs_review_and_no_ppsqm():
    v = validate(rec(price=Decimal("1000000")))
    assert v.computed_ppsqm is None and v.missing_critical == ["area"] and v.status == "needs_review"


def test_ocr_records_always_need_review():
    r = rec(price=Decimal("1000000"), area=Decimal("50"))
    r.ocr = True
    assert validate(r).status == "needs_review"
