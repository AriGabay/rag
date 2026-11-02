"""A number in a professional question is no address (round 12 U7, KTD6; review: "תקן 17", "סעיף 5.2", a one-digit house number).

``tools._question_gathered`` reads the property a question names by an address or a parcel. A standard's number, a section
number or a floor is not one, so the graph is never built for such a question; and a professional-intent question does not
take the question-named property path at all, since the appraisal pool is only an example for it. The connection given is a
stand-in that fails on any use: nothing may be read. Synthetic wording only."""

from __future__ import annotations

import pytest

from app.chat import contexts, tools
from app.chat.tools import Workspace


class NoDatabase:
    def __getattr__(self, name):
        raise AssertionError(f"the database was used ({name})")


def _ws(question: str, intent: str = "professional") -> Workspace:
    ws = Workspace(ctx=None)
    ws.user_messages = [{"text": question, "current": True}]
    ws.intent = intent
    return ws


NUMBERED = [
    "מה קובע תקן 17 לגבי השווי האחרון של נכס?",
    "מה אומר סעיף 5.2 בתקן על השווי העדכני?",
    "מה כתוב בסעיף 5 על תקן 17 בשנה האחרונה?",
    "מה השווי בהדס 5?",           # a one-digit house number with no street cue
    "מה השווי בקומה 3 בשנת 2024?",
]


@pytest.mark.parametrize("question", NUMBERED)
def test_a_standard_or_section_number_or_a_bare_house_number_names_no_property_and_reads_nothing(question):
    assert not contexts.identifiers(question).addresses and not contexts.identifiers(question).parcels
    assert tools._question_gathered(_ws(question), NoDatabase()) is None


@pytest.mark.parametrize("question", NUMBERED)
def test_a_professional_question_does_not_search_the_appraisals_as_a_primary_pool_so_the_question_path_stays_off(question):
    passes, _, _ = tools._plan(_ws(question), NoDatabase(), None, 6)
    assert not any(p.pool == tools.APPRAISAL_POOL and p.role == tools.PRIMARY for p in passes)
    assert [p.pool for p in passes] == [tools.PROFESSIONAL_POOL, tools.APPRAISAL_POOL]


def test_a_question_that_does_name_an_address_is_read_as_one():
    assert contexts.identifiers("מה השווי של הנכס ברחוב הדס 5?").addresses == {("הדס", "5")}
