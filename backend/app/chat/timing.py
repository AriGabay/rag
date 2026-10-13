"""The turn's stage clock (round 9 R16, R17, KTD9): where the turn's time went, per stage and per tool call.

``Workspace.clock`` is fed by ``tools.run_tool`` (one record per tool call: the tool, its stage, its seconds, the size of
its output) and by the engine (the answer's composing, its verification, the final check of the saved text). The
stages are the ones R16 names — locate, read, compute, compose, verify, final. The time between them (the model
choosing the next tool, the request analysis) is not any stage's: it is reported as ``unattributed_seconds``, so the
stage totals never exceed the turn's total. Numbers and tool names only: no content, so the summary is safe to keep in
the diagnostics (``message_diagnostics.resolution["timing"]``; no column of its own).

A read or an inspection also records the first and last page of the sources it returned (``pages``, numbers only), which
the evaluation's reading-completeness measure uses.

A tool call's progress label once it is done (``done_label``) reads what the result holds — passages found, pages
read — from the tool's own output, so the user sees what was found and not only what was started; a call a limit
stopped says so (the time limit, or the reading budget).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

STAGES = ("locate", "read", "compute", "compose", "verify", "final")
# the stage of each tool (KTD9); a tool not listed is counted as reading
TOOL_STAGE = {"search": "locate", "find_documents": "locate", "list_documents": "locate", "outline": "locate",
              "read": "read", "inspect": "read",
              "find_measurements": "compute", "take_value": "compute", "assume": "compute", "calculate": "compute"}
TOOLS_KEPT = 200  # tool records kept for a turn's summary (a turn makes a few dozen at most)


@dataclass
class StageClock:
    """Seconds per stage, and one record per tool call, of one turn. ``now`` is the clock (a test sets its own)."""

    now: Callable[[], float] = time.monotonic
    started: float = 0.0
    seconds: dict[str, float] = field(default_factory=lambda: dict.fromkeys(STAGES, 0.0))
    calls: dict[str, int] = field(default_factory=lambda: dict.fromkeys(STAGES, 0))
    tools: list[dict] = field(default_factory=list)
    waits: dict[str, float] = field(default_factory=dict)  # waits that are no stage's (``wait``): the intent's

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
        or an inspection returned (R16: the eval's reading-completeness measure reads them)."""
        stage = TOOL_STAGE.get(name, "read")
        self.add(stage, seconds)
        if len(self.tools) < TOOLS_KEPT:
            record = {"tool": name, "stage": stage, "seconds": round(max(0.0, seconds), 3), "chars": chars}
            if pages is not None:
                record["pages"] = [pages[0], pages[1]]
            self.tools.append(record)

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
        return out


_SOURCE = re.compile(r'<source id="S\d+"(?! same_as)([^>]*)>')
_DOCUMENT = re.compile(r'\bdocument_id="([^"]+)"')
_TIME_STOP = re.compile(r'<not_read [^>]*status="time_limit"')  # ``tools.TIME_LIMIT``: tools imports this module
_LISTED = re.compile(r'סה"כ (\d+) מסמכים')


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
