"""Round 23 closure: the confirming (second) reading keeps the evidence the first reading had for the same claim. A cited unit is shown, besides
the sources of its own place, a source another sentence of the answer cites (or the turn read) that is of the same document, version and appraisal
unit as one of its own sources and states a number of the claim that none of its own sources states (a continuation of a table in another stored
block). The judge is scripted; these tests prove what the confirming call is shown. Invented texts only."""

from __future__ import annotations

import re
import uuid

import pytest

from app.chat import verify
from app.chat.engine import FinalAnswer
from app.chat.tools import Workspace
from app.chat.verify import assign_ids, split_units, verify_answer
from app.config import get_settings
from app.providers.llm import Purpose
from tests.support.scripted_provider import ScriptedProvider


def _answer(markdown: str) -> FinalAnswer:
    return FinalAnswer(status="answered", answer_markdown=markdown, items=[], claims=[], clarification_question="",
                       missing_info="", referenced_document_ids=[], scope_kind="focused", scope_query="", omitted=[],
                       focus=None, requested=[], parts=[])


@pytest.fixture(autouse=True)
def _confirm_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "chat_confirm_removals", 3)


def _ws(*specs: dict) -> Workspace:
    """Sources S1.. from ``specs``: ``text`` and, by default, document 0, version 0, appraisal unit "u1", contexts [1]; ``doc``/``ver`` pick the
    document and version by number, ``unit`` the appraisal unit id (None: no unit), ``contexts`` the appraisal contexts."""
    ids: dict = {}
    ws = Workspace(ctx=None)
    for n, s in enumerate(specs):
        d = ids.setdefault(("d", s.get("doc", 0)), uuid.uuid4())
        v = ids.setdefault(("v", s.get("ver", s.get("doc", 0))), uuid.uuid4())
        unit = s.get("unit", "u1")
        ws.add_source(document_id=d, version_id=v, title=s.get("title", "דוח בדיקה"), section="טבלה", location=f"עמוד {n + 1}", kind="context", text=s["text"] + " נתון נוסף 31 ₪.",
                      unit={"id": unit} if unit is not None else None, contexts=list(s.get("contexts", [1])))
    return ws


def _units(input: str) -> dict[int, str]:
    return {int(i): t for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S)}


def _judge(marker: str, seen: list) -> ScriptedProvider:
    """The first reading fails the unit holding ``marker`` and supports the rest; the confirming reading (told by its policy) supports its claim
    and records its input in ``seen``."""
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        confirming = verify.JUDGE_CONFIRM_POLICY in instructions
        if confirming:
            seen.append(input)
        out = []
        for i, t in _units(input).items():
            v = "unsupported" if marker in t and not confirming else "supported"
            out.append({"index": i, "reason": "בדיקה", "verdict": v, **({"claims": [{"span": t, "verdict": v}]} if confirming else {})})
        return {"verdicts": out}

    p.on(Purpose.VERIFY, respond, repeat=True)
    return p


def _confirm_input(ws: Workspace, md: str, marker: str) -> str | None:
    """The input of the confirming call of ``md`` (None: there was none)."""
    seen: list = []
    verify_answer(_judge(marker, seen), _answer(md), ws, "?", [])
    return seen[0] if seen else None


def _candidates(shown: str) -> list[str]:
    return re.findall(r'<source id="(S\d+)"[^>]*candidate="true"', shown)


# (the claim, the figure, the year, the source of the top of the table - states the figure under another label, the year only in its title -, the
# continuation block: states the year, in the body)
SHAPES = [
    ("דמי השכירות בקומה השלישית בשנת 2021 היא 7,350 ₪", "7,350", "2021", "טבלת שכירות. סך כללי לבניין 7,350 ₪.", "שורות נוספות. הנתונים לשנת 2021."),
    ("עלות הריצוף במבואה בשנת 2017 היא 12,480 ₪", "12,480", "2017", "פירוט עלויות. יתרה לתשלום 12,480 ₪.", "המשך הטבלה. עלויות שנת 2017."),
    ("דמי הניהול לדירה בשנת 2023 הם 905 ₪", "905", "2023", "פרק הוצאות. תשלום קבוע 905 ₪.", "המשך. שנת 2023 כולה."),
]


def _top(year: str, text: str, **kw) -> dict:
    return {"text": text, "title": f"דוח כספי {year}", **kw}


@pytest.mark.parametrize(("claim", "figure", "year", "top", "rest"), SHAPES, ids=["rent", "floor", "fees"])
@pytest.mark.parametrize("continuation_first", [False, True])
def test_a_continuation_block_another_sentence_cites_is_shown_in_the_confirming_call(claim, figure, year, top, rest, continuation_first):
    # the block continuing the table has the same document, version and unit but another window of the appraisal contexts
    a, b = (_top(year, rest, contexts=[1, 2]), _top(year, top)) if continuation_first else (_top(year, top), _top(year, rest, contexts=[1, 2]))
    ws = _ws(a, b)
    mine, sibling = ("S2", "S1") if continuation_first else ("S1", "S2")
    shown = _confirm_input(ws, f"{claim} [{mine}].\n\nנתון נוסף הוא 31 ₪ [{sibling}].", figure)
    assert shown is not None and _candidates(shown) == [sibling], shown


def test_the_read_source_of_the_same_unit_in_another_window_is_shown_too():
    # nobody cites it: the turn read it
    claim, figure, year, top, rest = SHAPES[1]
    ws = _ws(_top(year, top), _top(year, rest, contexts=[1, 3]))
    shown = _confirm_input(ws, f"{claim} [S1].", figure)
    assert shown is not None and _candidates(shown) == ["S2"], shown


def test_the_places_of_today_come_first_then_the_continuations_and_the_total_is_capped():
    claim, figure, year, top, rest = SHAPES[2]
    ws = _ws(_top(year, top),
             _top(year, rest + " א", contexts=[1, 2]),  # continuations
             _top(year, rest + " ב", contexts=[1, 2]),
             _top(year, "שורה ג. " + rest),  # the same place as the own source
             _top(year, rest + " ד", contexts=[1, 4]))
    md = f"{claim} [S1].\n\n" + "\n\n".join(f"נתון {w} הוא 31 ₪ [S{n}]." for n, w in zip(range(2, 6), ["ראשון", "שני", "שלישי", "רביעי"], strict=True))
    shown = _confirm_input(ws, md, figure)
    assert shown is not None
    assert _candidates(shown) == ["S4", "S2", "S3"] and len(_candidates(shown)) == verify.CANDIDATE_SOURCES, _candidates(shown)


# --- negative controls -------------------------------------------------------------------------------------------------------------------

CLAIM, FIG, YEAR, TOP, REST = SHAPES[0]


@pytest.mark.parametrize("other", [{"unit": "u2"}, {"unit": None}, {"unit": "u2", "contexts": [1]}, {"contexts": [4]}, {"contexts": []}],
                         ids=["another-unit", "no-unit", "another-unit-same-contexts", "disjoint-contexts", "no-contexts"])
def test_a_source_of_another_appraisal_unit_or_context_is_not_shown(other):
    ws = _ws(_top(YEAR, TOP), _top(YEAR, REST, **{"contexts": [1, 2], **other}))
    shown = _confirm_input(ws, f"{CLAIM} [S1].\n\nנתון נוסף הוא 31 ₪ [S2].", FIG)
    assert shown is not None and _candidates(shown) == [], shown


@pytest.mark.parametrize("other", [{"doc": 1}, {"ver": 1}, {"doc": 1, "ver": 1}], ids=["another-file", "another-version", "another-both"])
def test_a_source_of_another_document_or_version_is_not_shown(other):
    ws = _ws(_top(YEAR, TOP), _top(YEAR, REST, contexts=[1, 2], **other))
    shown = _confirm_input(ws, f"{CLAIM} [S1].\n\nנתון נוסף הוא 31 ₪ [S2].", FIG)
    assert shown is not None and _candidates(shown) == [], shown


def test_a_source_that_holds_only_the_numbers_the_own_source_holds_is_not_shown():
    # the own source already states the year (and the figure): the sibling would only repeat them
    ws = _ws(_top(YEAR, TOP + f" שנת {YEAR}."), _top(YEAR, REST + f" סך כללי {FIG} ₪.", contexts=[1, 2]))
    shown = _confirm_input(ws, f"{CLAIM} [S1].\n\nנתון נוסף הוא 31 ₪ [S2].", FIG)
    assert shown is not None and _candidates(shown) == [], shown


def test_a_source_that_holds_a_different_number_is_not_shown():
    ws = _ws(_top(YEAR, TOP), _top(YEAR, "המשך. הנתונים לשנת 2014. סכום 8,820 ₪.", contexts=[1, 2]))
    shown = _confirm_input(ws, f"{CLAIM} [S1].\n\nנתון נוסף הוא 31 ₪ [S2].", FIG)
    assert shown is not None and _candidates(shown) == [], shown


def test_a_source_nobody_cites_and_the_turn_did_not_read_is_not_a_candidate():
    ws = _ws(_top(YEAR, TOP))  # the continuation was never read in this turn
    shown = _confirm_input(ws, f"{CLAIM} [S1].", FIG)
    assert shown is not None and _candidates(shown) == [], shown


# --- an uncited unit is unchanged ------------------------------------------------------------------------------------------------------------

def test_an_uncited_unit_gets_the_candidates_of_the_first_reading():
    ws = _ws(_top(YEAR, TOP), _top(YEAR, REST, contexts=[1, 2]), _top(YEAR, REST, doc=1))
    units = split_units(f"{CLAIM}.\n\nנתון נוסף הוא 31 ₪ [S1].")
    assign_ids(units, ws.claim_salt)
    u = units[0]
    expected = verify._unit_candidates(u, ws)
    assert not u.ids and expected and len(expected) <= verify.CANDIDATE_SOURCES
    assert verify._confirm_candidates(u, units, ws, set()) == expected
