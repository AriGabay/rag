"""The stage clock and the progress labels of a finished tool (round 9 U1, R16, R17, KTD9): each tool call is timed
and counted to its stage, the stage totals never exceed the turn's, and a done label says what the result holds.
The clock and the tools are scripted; synthetic text only."""

from __future__ import annotations

from app.chat import tools as T
from app.chat.timing import STAGES, TOOL_STAGE, StageClock, done_label


class Ticker:
    """A clock that moves only when a test says so."""

    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def _ws(ticker: Ticker) -> T.Workspace:
    ws = T.Workspace(ctx=None)
    ws.clock = StageClock(now=ticker)
    return ws


def _handlers(monkeypatch, ticker: Ticker, **seconds_and_output: tuple[float, str]) -> None:
    def make(spend: float, output: str):
        def handler(ws, args):
            ticker.t += spend
            return output
        return handler

    monkeypatch.setattr(T, "HANDLERS", {k: make(*v) for k, v in seconds_and_output.items()})


def test_every_tool_has_a_stage_of_the_six():
    assert set(TOOL_STAGE.values()) <= set(STAGES)
    assert set(TOOL_STAGE) == set(T.HANDLERS)


def test_two_searches_and_a_read_are_recorded_with_their_stage_and_size(monkeypatch):
    ticker = Ticker()
    ws = _ws(ticker)
    _handlers(monkeypatch, ticker, search=(2.0, "תוצאה"), read=(1.5, "טקסט ארוך יותר"))
    for name, args in (("search", '{"query": "א"}'), ("search", '{"query": "ב"}'), ("read", "{}")):
        T.run_tool(ws, name, args)
    assert [(r["tool"], r["stage"], r["seconds"], r["chars"]) for r in ws.clock.tools] == [
        ("search", "locate", 2.0, 5), ("search", "locate", 2.0, 5), ("read", "read", 1.5, 14)]
    ticker.t += 0.5  # the model choosing its next step: no stage's
    s = ws.clock.summary()
    assert s["stages"]["locate"] == {"seconds": 4.0, "calls": 2} and s["stages"]["read"] == {"seconds": 1.5, "calls": 1}
    assert set(s["stages"]) == set(STAGES) and s["stages"]["compute"] == {"seconds": 0.0, "calls": 0}
    assert s["total_seconds"] == 6.0 and s["unattributed_seconds"] == 0.5
    assert sum(v["seconds"] for v in s["stages"].values()) <= s["total_seconds"]


def test_a_failing_tool_is_still_timed_and_an_unknown_tool_is_not(monkeypatch):
    ticker = Ticker()
    ws = _ws(ticker)

    def broken(ws_, args):
        ticker.t += 1.0
        raise T.ToolError("אין")

    monkeypatch.setattr(T, "HANDLERS", {"search": broken})
    assert T.run_tool(ws, "search", '{"query": "א"}').startswith("שגיאה")
    assert T.run_tool(ws, "nonexistent", "{}").startswith("כלי לא קיים")
    assert [r["tool"] for r in ws.clock.tools] == ["search"] and ws.clock.seconds["locate"] == 1.0


def test_the_engines_stages_join_the_tools_in_one_summary():
    ticker = Ticker()
    clock = StageClock(now=ticker)
    clock.tool("calculate", 0.25, 40)
    clock.add("compose", 3.0)
    clock.add("verify", 4.0)
    clock.add("final", 1.0)
    ticker.t += 9.0
    s = clock.summary()
    assert [k for k, v in s["stages"].items() if v["calls"]] == ["compute", "compose", "verify", "final"]
    assert s["total_seconds"] == 9.0 and s["unattributed_seconds"] == 0.75


def test_rounding_never_puts_the_stages_over_the_total():
    ticker = Ticker()
    clock = StageClock(now=ticker)
    for _ in range(6):
        clock.add("locate", 0.0004)
    ticker.t += 0.0021
    s = clock.summary()
    assert sum(v["seconds"] for v in s["stages"].values()) <= s["total_seconds"]


SOURCES = ('<source id="S1" document_id="d1" title="א" location="ב" kind="paragraph">\nטקסט\n</source>\n\n'
           '<source id="S2" document_id="d2" title="א" location="ב" kind="paragraph">\nטקסט\n</source>\n\n'
           '<source id="S3" same_as="S1" document_id="d1" title="א" location="ב"/> (אותו טקסט)')


def test_a_search_says_how_many_passages_and_documents_it_found():
    assert done_label("search", {}, SOURCES) == "נמצאו 2 קטעים ב-2 מסמכים"
    assert done_label("search", {}, SOURCES.split("\n\n")[0]) == "נמצאו 1 קטעים"
    assert done_label("search", {}, 'לא נמצאו קטעים עבור "א".') == "לא נמצאו קטעים"


def test_a_read_says_the_pages_it_read():
    pages = {"target": {"pages": {"document": "D1", "from_page": 3, "to_page": 5}}}
    assert done_label("read", pages, SOURCES) == "נקראו עמודים 3–5"
    assert done_label("read", {"target": {"section": "§2"}}, SOURCES.split("\n\n")[0]) == "נקרא קטע"


def test_a_listing_says_how_many_documents_and_a_failure_or_a_stop_says_so():
    listing = 'תחום: בדיקה\nעמוד 1 מתוך 1; סה"כ 7 מסמכים מתאימים\n- ...'
    assert done_label("find_documents", {}, listing) == "נמצאו 7 מסמכים"
    assert done_label("list_documents", {}, "לא נמצאו מסמכים.") == "לא נמצאו מסמכים"
    assert done_label("read", {}, "שגיאה: המקור אינו זמין") == "הפעולה לא הושלמה"
    assert "מגבלת" in done_label("search", {}, '<not_read tool="search" status="tool_budget" more="x"/>\nטקסט')
