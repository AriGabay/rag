"""Gate 4 (origin §11): questions without sufficient basis get a clarification or an explicit
abstention — never an invented number."""

from __future__ import annotations

import pytest

from eval.flows import ask_flow
from tests.acceptance.support import ADMIN_A, ADMIN_B, big_numbers

pytestmark = pytest.mark.db


def _no_number(answer: dict) -> None:
    assert answer.get("numeric") in (None, {}), answer.get("numeric")
    assert not big_numbers(answer.get("text", "")), answer.get("text")


@pytest.mark.parametrize("question,answers", [
    ("מה מחיר העסקאות למ״ר בשכונת נווה צדק ב-2022 לפי תאריך עסקה?", {}),
    ("כמה עסקאות היו בשכונת רמת החייל ב-2021?", {"data_kind": "transaction_price", "date_field": "transaction_date"}),
], ids=["unknown-neighborhood", "unknown-neighborhood-count"])
def test_unknown_place_abstains(world, question, answers):
    # note: these abstentions get cached under the remaining conditions (see the cache xfail below),
    # so later tests in this module use other years
    a = ask_flow(world.client(ADMIN_A), question, answers).answer
    assert a["kind"] == "abstain", a["kind"]
    _no_number(a)


def test_year_without_data_abstains_with_coverage(world):
    a = ask_flow(world.client(ADMIN_A), "מה מחיר העסקאות למ״ר בחרוזים ב-2019 לפי תאריך עסקה?").answer
    assert a["kind"] == "abstain"
    _no_number(a)
    assert a["coverage"] and "text" in a["coverage"]
    assert "לא" in a["text"]


def test_transactions_in_a_year_only_present_as_valuation_dates_abstain(world):
    """D8: its transactions are from 2023; asked by transaction date 2024 there is nothing in נחלת גנים."""
    a = ask_flow(world.client(ADMIN_A), "מה מחיר העסקאות למ״ר בנחלת גנים שנחתמו ב-2024?").answer
    assert a["kind"] == "abstain"
    _no_number(a)


@pytest.mark.parametrize("question,key", [
    ("מה המחיר או השווי למ״ר בחרוזים ב-2024?", "data_kind"),
    ("מה מחיר למ״ר ברמת גן בשכונת חרוזים בשנת 2024?", "data_kind"),
    ("מחירי עסקאות למ״ר בחרוזים בשנת 2024", "date_field"),
    ("מה השווי למ״ר ברמת גן ב-2024?", "date_field"),
    ("מחיר למ״ר בעסקאות שנחתמו ב-2024 בחרוזים", "area_type"),
], ids=["price-or-value", "core-example", "year-without-date-field", "value-year-without-date-field", "mixed-area"])
def test_missing_result_changing_condition_asks(world, question, key):
    a = ask_flow(world.client(ADMIN_A), question).answer
    assert a["kind"] == "clarification" and a["clarification"]["key"] == key
    assert len(a["clarification"]["options"]) >= 2
    _no_number(a)


def test_office_b_unknown_neighborhood_of_office_a_abstains(world):
    """בורוכוב exists only in office A's documents; office B gets an abstention, not A's data."""
    a = ask_flow(world.client(ADMIN_B), "מה מחיר העסקאות למ״ר בשכונת בורוכוב?").answer
    assert a["kind"] == "abstain"
    _no_number(a)


@pytest.mark.xfail(strict=True, reason=(
    "BUG: a city the office has no data for is silently dropped by the rules parser "
    "(app/answering/parser.py:_place / parse_question only flags unknown 'שכונת X' phrases), so the "
    "question is answered for every city in the office: 'בחיפה' returns Ramat Gan figures."))
def test_unknown_city_abstains(world):
    a = ask_flow(world.client(ADMIN_A), "מה מחיר העסקאות למ״ר בחיפה ב-2023 לפי תאריך עסקה?",
                 {"area_type": "net", "property_type": "apartment", "vat_basis": "included"}).answer
    assert a["kind"] in ("abstain", "clarification")
    _no_number(a)


@pytest.mark.xfail(strict=True, reason=(
    "BUG: office B asking about גבעתיים (a city only office A has) gets office B's Harozim numbers: the "
    "unknown city is dropped (app/answering/parser.py) and the answer shows 2 records without the city."))
def test_office_b_city_it_does_not_have_abstains(world):
    a = ask_flow(world.client(ADMIN_B), "מחיר למ״ר בעסקאות בגבעתיים שנחתמו ב-2024").answer
    assert a["kind"] in ("abstain", "clarification")
    _no_number(a)


@pytest.mark.xfail(strict=True, reason=(
    "BUG: a content question with no basis in the documents is answered from unrelated passages. "
    "hybrid_search (app/platform/search.py:60) has no relevance floor (the semantic list always returns "
    "neighbours) and MockLLM.answer (app/providers/llm.py:78) accepts an overlap on stop words such as "
    "'על', so the answer quotes an irrelevant sentence instead of abstaining."))
def test_content_question_without_basis_abstains(world):
    a = ask_flow(world.client(ADMIN_A), "מה נכתב על בריכת שחייה על הגג?").answer
    assert a["kind"] == "abstain" or any("אין בסיס" in lim for lim in a.get("limitations", []))


@pytest.mark.xfail(strict=True, reason=(
    "BUG: an unknown-place abstention is cached under the remaining conditions (the unknown place is not part "
    "of the key): app/answering/service.py run_question returns parsed.conditions with the abstention and "
    "app/answering/api.py ask() caches every 'abstain'. A later question with the same conditions and no "
    "place then gets 'אין במאגר המשרד רשומות עבור \"פלורנטין\"' instead of its numbers."))
def test_unknown_place_abstention_does_not_poison_the_cache(world):
    client = world.client(ADMIN_B)
    first = ask_flow(client, "מה מחיר העסקאות למ״ר בשכונת פלורנטין ב-2023 לפי תאריך עסקה?").answer
    assert first["kind"] == "abstain"
    plain = ask_flow(client, "מה מחיר העסקאות למ״ר ב-2023 לפי תאריך עסקה?").answer
    assert "פלורנטין" not in plain.get("text", "")
    assert plain["kind"] == "numeric" and plain["numeric"]["record_count"] == 1  # DB1-T02


@pytest.mark.xfail(strict=True, reason=(
    "BUG: a clarification answer sent when no clarification is pending is treated as an empty question: "
    "app/answering/service.py run_question falls through to parse_question('') and app/answering/content.py "
    "answers it from whatever passages hybrid search returns. Expected a 409/422 or a re-asked question."))
def test_clarification_without_pending_question_is_not_answered_from_random_passages(world):
    client = world.client(ADMIN_A)
    conversation = client.post("/api/conversations").json()["id"]
    r = client.post("/api/ask", json={"conversation_id": conversation,
                                      "clarification": {"key": "area_type", "value": "net"}})
    assert r.status_code >= 400 or (r.json()["answer"]["kind"] in ("clarification", "abstain")
                                    and not r.json()["answer"].get("sources"))
