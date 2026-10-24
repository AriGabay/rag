"""The turn's stage clock (round 9 R16, R17, KTD9): where the turn's time went, per stage and per tool call.

``Workspace.clock`` is fed by ``tools.run_tool`` (one record per tool call: the tool, its stage, its seconds, the size of
its output) and by the engine (the answer's composing, its verification, the final check of the saved text). The
stages are the ones R16 names — locate, read, compute, compose, verify, final. The time between them (the model
choosing the next tool, the request analysis) is not any stage's: it is reported as ``unattributed_seconds``, so the
stage totals never exceed the turn's total. Numbers and tool names only: no content, so the summary is safe to keep in
the diagnostics (``message_diagnostics.resolution["timing"]``; no column of its own).

Round 22 U0 (R10, R18, R25) adds the measurement the gate needs, with no effect on any decision or answer text: each search
call's lexical and fused candidate counts and seconds (on the tool's record, ``search``), the judge results that ended
``not_checked`` by cause (``not_checked``), the state of each read target (``reads``: attempted, completed, failed with,
skipped for the tool budget) and the number of multi-page stored tables served (``multi_page_tables``). Counts, statuses
and handles only.

Round 23 U0 (KTD11, R15) adds two more records, again with no effect on any decision or answer text: what the repair gate saw and decided
after each verification (``repair_gate``: the attempt, whether the report was ok or settled, the steps used, the seconds the turn had to the
gate, the answer step's seconds, the kinds of problem a repair would take, and the cause when the gate refused the round), and the read
cursors the agent was given and never followed (``unfollowed_cursors``, from ``Workspace.unfollowed_cursors``, at the turn's end).

A read or an inspection also records the first and last page of the sources it returned (``pages``, numbers only), which
the evaluation's reading-completeness measure uses.

A tool call's progress label once it is done (``done_label``) reads what the result holds — passages found, pages
read — from the tool's own output, so the user sees what was found and not only what was started; a call a limit
stopped says so (the time limit, or the reading budget).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

STAGES = ("locate", "read", "compute", "compose", "verify", "final")
# the stage of each tool (KTD9); a tool not listed is counted as reading
TOOL_STAGE = {"search": "locate", "find_documents": "locate", "list_documents": "locate", "find_standards": "locate",
              "outline": "locate",
              "read": "read", "inspect": "read",
              "find_measurements": "compute", "take_value": "compute", "assume": "compute", "calculate": "compute"}
TOOLS_KEPT = 200  # tool records kept for a turn's summary (a turn makes a few dozen at most)
SEARCHES_KEPT = 12  # search records kept on one tool record (a tool call makes one to a few searches)
READ_TARGETS_KEPT = 200  # read targets kept for a turn's summary
REPAIR_GATES_KEPT = 8  # repair-gate records kept for a turn (one per verification round, a few at most)
CURSORS_KEPT = 40  # unfollowed continuation cursors kept for a turn's summary


@dataclass
class StageClock:
    """Seconds per stage, and one record per tool call, of one turn. ``now`` is the clock (a test sets its own)."""

    now: Callable[[], float] = time.monotonic
    started: float = 0.0
    seconds: dict[str, float] = field(default_factory=lambda: dict.fromkeys(STAGES, 0.0))
    calls: dict[str, int] = field(default_factory=lambda: dict.fromkeys(STAGES, 0))
    tools: list[dict] = field(default_factory=list)
    waits: dict[str, float] = field(default_factory=dict)  # waits that are no stage's (``wait``): the intent's
    # round 22 U0: the searches made since the last tool record (``note_search``; the next ``tool`` takes them), the judge
    # results that ended ``not_checked`` by cause, the state of each read target, and the multi-page stored tables served
    pending_searches: list[dict] = field(default_factory=list)
    searches_memoized: int = 0
    not_checked: dict[str, int] = field(default_factory=dict)
    reads: dict[str, dict] = field(default_factory=dict)
    read_docs: dict[str, set] = field(default_factory=dict)  # round 22 U7: the documents each read target names (never in the summary)
    tables_served: set = field(default_factory=set)
    multi_page: set = field(default_factory=set)
    # round 23 U0: what the repair gate saw and decided after each verification, and the continuation cursors never followed
    repair_gate: list[dict] = field(default_factory=list)
    unfollowed: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.started = self.now()

    def add(self, stage: str, seconds: float) -> None:
        """Count ``seconds`` (never negative) to ``stage``."""
        self.seconds[stage] += max(0.0, seconds)
        self.calls[stage] += 1

    def wait(self, name: str, seconds: float) -> None:
        """Seconds the turn waited for something that is no stage (round 10: the request analysis that settles the
        question's intent); they stay in the turn's unattributed time and are also reported by name."""
        self.waits[name] = self.waits.get(name, 0.0) + max(0.0, seconds)

    def tool(self, name: str, seconds: float, chars: int, pages: tuple[int, int] | None = None) -> None:
        """One tool call: counted to the tool's stage, and kept as a record. ``pages``: the first and last page a read
        or an inspection returned (R16: the eval's reading-completeness measure reads them). The searches the call made
        (``note_search``) are on its record as ``search``."""
        stage = TOOL_STAGE.get(name, "read")
        self.add(stage, seconds)
        searches, memoized = self.pending_searches, self.searches_memoized
        self.pending_searches, self.searches_memoized = [], 0
        if len(self.tools) < TOOLS_KEPT:
            record = {"tool": name, "stage": stage, "seconds": round(max(0.0, seconds), 3), "chars": chars}
            if pages is not None:
                record["pages"] = [pages[0], pages[1]]
            if searches or memoized:
                record["search"] = {"calls": searches[:SEARCHES_KEPT], "run": len(searches), "memoized": memoized}
            self.tools.append(record)

    def note_search(self, record: dict | None) -> None:
        """One search ``memoized_search`` answered: ``record`` (``platform.search.measure_searches``: lexical and fused
        candidates, depth, seconds, summed over the calls it made) when it ran, None when the turn's memo answered it."""
        if record is None:
            self.searches_memoized += 1
        elif len(self.pending_searches) < 4 * SEARCHES_KEPT:
            self.pending_searches.append(record)

    def note_not_checked(self, cause: str) -> None:
        """A judge result that ended ``not_checked`` (R18): by ``cause`` — the judge call's status (``timeout``,
        ``rate_limited``, ``invalid``, ``error``, ``unavailable``...), ``omitted``, ``not_classified`` or ``time_limit``. Never a
        verdict about the claim."""
        self.not_checked[cause] = self.not_checked.get(cause, 0) + 1

    def note_read(self, target: str, tool: str, *, attempted: bool, completed: bool = False, failed_with: str | None = None,
                  budget_skipped: bool = False, documents: Iterable[str] = ()) -> None:
        """The state of one read target (R18): ``attempted`` — the server ran the read; ``completed`` — it returned without
        failing; ``failed_with`` — the refusal key or exception name of a read that failed (or ``time_limit``, a read the
        reading time stopped); ``budget_skipped`` — the tool budget stopped the call before it ran. Later calls on the same
        target add to it: ``completed`` and ``budget_skipped`` stay once set, ``failed_with`` is cleared by a completed read.
        ``documents`` (round 22 U7): the documents the call named (the handles it gave, resolved by the caller), kept apart from the
        record: ``Workspace.failed_reads`` finds the reads of a document through them."""
        st = self.reads.get(target)
        if st is None:
            if len(self.reads) >= READ_TARGETS_KEPT:
                return
            st = self.reads[target] = {"tool": tool, "attempted": False, "completed": False, "failed_with": None,
                                       "budget_skipped": False, "calls": 0}
        st["calls"] += 1
        st["attempted"] = st["attempted"] or attempted
        st["completed"] = st["completed"] or completed
        st["budget_skipped"] = st["budget_skipped"] or budget_skipped
        st["failed_with"] = None if st["completed"] else failed_with or st["failed_with"]
        if documents:
            self.read_docs.setdefault(target, set()).update(str(d) for d in documents)

    def note_table(self, key: tuple, spans_pages: bool) -> None:
        """A stored table served (``key``: its version and index; once per table); ``spans_pages``: its rows are on more than
        one page. ``multi_page_tables`` in the summary counts those."""
        if key not in self.tables_served:
            self.tables_served.add(key)
            if spans_pages:
                self.multi_page.add(key)

    def note_repair_gate(self, record: dict) -> None:
        """What the repair gate saw after one verification round, and its decision (round 23 U0, KTD11): the engine's numbers and labels only.
        Read by no decision."""
        if len(self.repair_gate) < REPAIR_GATES_KEPT:
            self.repair_gate.append(record)

    def note_unfollowed(self, cursors: list[dict]) -> None:
        """The read cursors the turn gave the agent and it never followed (``Workspace.unfollowed_cursors``), kept at the turn's end."""
        self.unfollowed = list(cursors[:CURSORS_KEPT])

    def summary(self) -> dict:
        """The turn so far: every stage with its seconds and calls, the tool records, the turn's seconds and the part
        of it no stage holds. The total is never less than the stages' sum (they are disjoint intervals of the turn;
        rounding is the only way they could exceed it)."""
        stages = {k: {"seconds": round(self.seconds[k], 3), "calls": self.calls[k]} for k in STAGES}
        held = round(sum(s["seconds"] for s in stages.values()), 3)
        total = max(round(self.now() - self.started, 3), held)
        out = {"total_seconds": total, "stages": stages, "unattributed_seconds": round(total - held, 3),
               "tools": list(self.tools)}
        if self.waits:
            out["waits"] = {k: round(v, 3) for k, v in self.waits.items()}
        out["not_checked"] = dict(self.not_checked)
        out["reads"] = {k: dict(v) for k, v in self.reads.items()}
        out["tables_served"] = len(self.tables_served)
        out["multi_page_tables"] = len(self.multi_page)
        out["repair_gate"] = [dict(r) for r in self.repair_gate]
        out["unfollowed_cursors"] = [dict(r) for r in self.unfollowed]
        return out


_SOURCE = re.compile(r'<source id="S\d+"(?! same_as)([^>]*)>')
_DOCUMENT = re.compile(r'\bdocument_id="([^"]+)"')
_TIME_STOP = re.compile(r'<not_read [^>]*status="time_limit"')  # ``tools.TIME_LIMIT``: tools imports this module
_LISTED = re.compile(r'סה"כ (\d+) מסמכים')
_STANDARDS = re.compile(r'סה"כ (\d+) תקנים')


def done_label(name: str, args: dict, output: str) -> str:
    """The progress label of a finished tool call: what its result holds. Counts come from the output the model got
    (passages returned, the page range asked for, the documents listed); a call that failed or was withheld says so."""
    if output.startswith("<not_read"):
        return "נעצר: מגבלת הזמן" if _TIME_STOP.match(output) else "הקריאה נעצרה: מגבלת היקף הקריאה"
    if output.startswith("שגיאה"):
        return "הפעולה לא הושלמה"
    sources = [m.group(1) for m in _SOURCE.finditer(output)]
    if name == "search":
        if not sources:
            return "לא נמצאו קטעים"
        docs = {m.group(1) for s in sources if (m := _DOCUMENT.search(s))}
        return f"נמצאו {len(sources)} קטעים" + (f" ב-{len(docs)} מסמכים" if len(docs) > 1 else "")
    if name == "read":
        target = args.get("target") if isinstance(args.get("target"), dict) else {}
        pages = target.get("pages") if isinstance(target.get("pages"), dict) else None
        if pages:
            return f"נקראו עמודים {pages.get('from_page')}–{pages.get('to_page')}"
        return f"נקרא קטע ({len(sources)} מקורות)" if len(sources) > 1 else "נקרא קטע"
    if name == "find_standards":
        m = _STANDARDS.search(output)
        return f"נמצאו {m.group(1)} תקנים ומסמכים מקצועיים" if m else "לא נמצאו תקנים ומסמכים מקצועיים"
    if name in ("find_documents", "list_documents"):
        m = _LISTED.search(output)
        return f"נמצאו {m.group(1)} מסמכים" if m else "לא נמצאו מסמכים"
    if name == "outline":
        return "נקרא מבנה המסמך"
    if name == "find_measurements":
        return "לא נמצאו נתונים" if output.startswith("לא נמצאו") else "נמצאו נתונים"
    if name == "inspect":
        return "נקרא אזור חזותית"
    if name == "calculate":
        return "החישוב הסתיים"
    return "נרשם"
