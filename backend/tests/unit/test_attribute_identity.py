"""``same_attribute``: one meaning, one definition; another word, measure word, qualifier or unit is another."""

import pytest

from app.answering.attributes import same_attribute


@pytest.mark.parametrize(("a", "b"), [
    ("שטח המרפסת", "שטח המרפסות"),
    ("גודל המחסן", "שטח המחסן"),  # two measure words of one measure
    ("שטח של המחסן", "שטח המחסן"),
    ("המחסן שטח", "שטח המחסן"),
])
def test_same(a, b):
    assert same_attribute(a, "area", b, "area")
    assert same_attribute(a, None, b, "area")  # a definition without a dimension yet


@pytest.mark.parametrize(("a", "da", "b", "db"), [
    ("גובה החלון", "length", "רוחב החלון", "length"),
    ("שטח המחסן נטו", "area", "שטח המחסן", "area"),
    ("שטח המחסן", "area", "שטח המחסן", "volume"),
    ("מספר המחסנים", "count", "שטח המחסן", "area"),
    ("שטח המחסן", "area", "שטח המחסן בבניין", "area"),
    ("שטח", "area", "גודל", "area"),  # no distinctive word: never merged by measure words alone
    ("שנת הבנייה", "year", "שנת השיפוץ", "year"),
])
def test_different(a, da, b, db):
    assert not same_attribute(a, da, b, db)
