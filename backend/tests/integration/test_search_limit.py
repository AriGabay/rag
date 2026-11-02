"""Round 22 U2 (R10): the lexical stage returns at most the candidates it was asked for, over a real pool larger than the limit,
equal to it and smaller than it; the IDF weights still come from the pool. Invented text, the synthetic office B."""

import pytest

from app.db import tenant_tx
from app.platform.search import CANDIDATES, SMALL_SCOPE_CANDIDATES, _lexical, _Sql
from tests.factories import make_office
from tests.integration.test_search import add_chunks

pytestmark = pytest.mark.db

POOL = 60


@pytest.fixture
def pool(db):
    office = make_office(db, "משרד ב", "limit-b@example.test")
    add_chunks(office, office.default_group_id,
               [f"סעיף {i}: תיאור הנכס מגרש {i} ומבנה בשכונת בדיקה" for i in range(POOL)], "9" * 64)
    return office


def run(office, n, tokens=("מגרש", "מבנה")):
    with tenant_tx(office.ctx()) as conn:
        return _lexical(conn, list(tokens), _Sql(), n)


def test_a_limit_below_the_pool_returns_exactly_the_limit(pool):
    assert len(run(pool, CANDIDATES)) == CANDIDATES
    assert len(run(pool, 7)) == 7


def test_a_limit_equal_to_or_above_the_pool_returns_every_match(pool):
    assert len(run(pool, POOL)) == POOL
    assert len(run(pool, SMALL_SCOPE_CANDIDATES)) == POOL


def test_no_match_in_the_pool_returns_nothing(pool):
    assert run(pool, CANDIDATES, ("שולחן",)) == []


def test_tied_candidates_come_back_in_chunk_order_on_every_run(pool):
    first = run(pool, CANDIDATES)
    assert first == run(pool, CANDIDATES)
    with tenant_tx(pool.ctx()) as conn:
        from sqlalchemy import text
        order = {r.id: r.chunk_index for r in conn.execute(text("SELECT id, chunk_index FROM chunks"))}
    assert [order[c] for c in first] == sorted(order[c] for c in first)
