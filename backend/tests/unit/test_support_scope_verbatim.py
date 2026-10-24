"""Round 23: the judge-support check compares a quote with the source exactly (flattened blanks only). The loose comparison of ``take_value``
and the anchor locator (``meaning.loose_contains``) was tried here and withdrawn: on stored real drafts every quote it newly admitted became a
wrong "read and not used" line. A real difference fails either way. Synthetic text only."""

from __future__ import annotations

import uuid

import pytest

from app.chat import verify
from app.chat.tools import Workspace

SOURCE = "הנכס כולל מחסן בקומת המרתף ששטחו 410 מ\"ר, ולצדו חדר מכונות בשטח של 36 מ\"ר.\nסך הכול 446 מ\"ר."


def _scope(text: str) -> tuple[verify.SupportScope, str, object]:
    ws = Workspace(ctx=None)
    src = ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="דוח בדיקה", section="שטחים", location="עמוד 2",
                        kind="context", text=text)
    return verify.SupportScope(ws, []), src.sid, src


def _verbatim(text: str, quote: str) -> bool:
    scope, sid, src = _scope(text)
    return scope.verbatim(sid, src, scope.flat(quote))


@pytest.mark.parametrize("stored", [
    "הנכס כולל מחסן בקומת המרתף ששטחו 410 מ\"ר, ולצדו חדר   מכונות\nבשטח  של 36 מ\"ר.",  # spacing differs
    SOURCE,
])
def test_a_support_quote_is_verbatim_when_the_source_spaces_differently(stored):
    assert _verbatim(stored, "ולצדו חדר מכונות בשטח של 36 מ\"ר")


@pytest.mark.parametrize("stored", [
    "הנכס כולל מחסן בקומת המרתף ששטחו 410 מ\"ר, ולצדו חדר מכו-\nנות בשטח של 36 מ\"ר.",  # a word split by a line-wrap hyphen
    "הנכס כולל מחסן בקומת המרתף ששטחו 410 מ\"ר, ולצדו חדר מכו\nנות בשטח של 36 מ\"ר.",  # a word split by a bare line break
])
def test_a_support_quote_of_a_source_that_splits_a_word_is_not_admitted(stored):
    assert not _verbatim(stored, "ולצדו חדר מכונות בשטח של 36 מ\"ר")


@pytest.mark.parametrize("quote", ["ולצדו חדר מכונות בשטח של 37 מ\"ר",  # a number
                                   "ולצדו חדר קירור בשטח של 36 מ\"ר",  # a word
                                   "ולצדו חדר מכונות בשטח של 36 מ\"ר ועוד חדר"])  # more than the source holds
def test_a_quote_with_a_different_word_or_number_is_not_verbatim(quote):
    stored = "הנכס כולל מחסן בקומת המרתף ששטחו 410 מ\"ר, ולצדו חדר מכו-\nנות בשטח של 36 מ\"ר."
    assert not _verbatim(stored, quote)
