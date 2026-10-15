"""A passage of an appraisal opened by any route carries the unit's valuation date line (round 13 U9, KTD10, R13).

``Source.date_line`` was set only on a search hit. The verifier backs a date that equals the valuation date of the unit a
cited passage is in (``verify._valuation_dates``), so a passage opened by ``read`` — or taken some other way — must carry the
line too. The tools run directly under a workspace; every document is synthetic."""

from __future__ import annotations

import pytest

from app.chat import tools as T
from app.chat import verify
from tests.factories import make_office
from tests.integration.test_tools_pools import run
from tests.integration.test_tools_recency import NONE, QUERY, dated, ws_for

pytestmark = pytest.mark.db

LINE = T.MSG_DATE_VALUATION.format(date="15/07/2024")


@pytest.fixture
def office(db):
    return make_office(db, "משרד א", "admin-a@example.test")


def test_a_passage_opened_by_reading_a_source_carries_the_units_valuation_date_line(office):
    dated(office, "r1", "15/07/2024")
    ws = ws_for(office, NONE)
    run(ws, "search", query=QUERY, limit=3)
    hit = next(iter(ws.sources.values()))
    hit.date_line = None  # as a source created by a route that never set it
    out = T.tool_read(ws, {"source": hit.sid})
    opened = list(ws.sources.values())[-1]
    assert opened.sid != hit.sid and opened.date_line == LINE and f'date="{LINE}"' in out
    assert verify._valuation_dates(opened) == [("2024-07-15", "day")]


def test_a_source_a_tool_added_without_a_line_is_stamped_when_the_call_ends(office):
    dated(office, "r2", "15/07/2024")
    ws = ws_for(office, NONE)
    run(ws, "search", query=QUERY, limit=3)
    hit = next(iter(ws.sources.values()))
    hit.date_line = None
    before = len(ws.sources)
    T._stamp_new_sources(ws, 0)
    assert hit.date_line == LINE and len(ws.sources) == before
