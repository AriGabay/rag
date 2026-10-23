"""Round 22 U7 (R18, R11, KTD13; failure family F2): a technical failure is never presented as a content fact, and a late judge batch
is not starved of its retry.

F2 as the diagnosis found it: the agent loop used most of the turn's deadline; a judge batch call then timed out; its single retry
(``verify._judge_batch``) got almost no time left (a judge call is cut to what the deadline leaves), so the claims were withheld as
"not checked: timeout" and a fully supported answer was lost. The fix reserves a slice of the verification deadline for that retry
(``verify.JUDGE_RETRY_RESERVE_SECONDS``): the first call is cut so the retry keeps a usable window.

The provider here is a fake that mimics a real one's timing: a call is cut to ``min(its purpose's timeout, what the deadline leaves)``,
a call the first one hangs for its whole timeout, and a judge call that is given less than ``NEEDED`` seconds times out. The clock is a
fake one, so nothing sleeps. Synthetic text only; no database."""

from __future__ import annotations

import re
import time
import uuid

import pytest

from app.chat import tools as T
from app.chat import verify
from app.chat.engine import FinalAnswer
from app.chat.verify import verify_answer
from app.providers import llm
from app.providers.llm import CallStatus, StructuredResult
from tests.support.scripted_provider import ScriptedProvider

PURPOSE_TIMEOUT = 45.0  # ``llm_timeout_verify_seconds``
NEEDED = 12.0  # a judge call given less than this many seconds does not finish
SOURCE = "סיכום: השווי למ\"ר הוא 9,500 ₪. דמי השכירות הם 55 ₪ למ\"ר לחודש."
MARKDOWN = "השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות הם 55 ₪ למ\"ר לחודש [S1]."


class Clock:
    """The turn's clock: real monotonic time plus the seconds the fake provider's hanging calls consumed."""

    def __init__(self) -> None:
        self.offset = 0.0

    def now(self) -> float:
        return time.monotonic() + self.offset

    def left(self, deadline: float | None) -> float | None:
        return None if deadline is None else deadline - self.now()


class TimingProvider(ScriptedProvider):
    """A judge whose call number ``n`` (from 1) hangs when ``hangs(n, input)`` (consuming its whole timeout, cut by the deadline like the
    real provider's); every other call answers every unit supported, when it had at least ``NEEDED`` seconds."""

    def __init__(self, clock: Clock, hangs=lambda n, input: False) -> None:
        super().__init__()
        self.clock, self.hangs, self.windows = clock, hangs, []

    def structured(self, purpose, instructions, input, schema, *, max_output_tokens=None, deadline=None, reasoning_effort=None):
        timeout = min(PURPOSE_TIMEOUT, self.clock.left(deadline)) if deadline is not None else PURPOSE_TIMEOUT
        self.windows.append(round(timeout, 1))
        if self.hangs(len(self.windows), input) or timeout < NEEDED:
            self.clock.offset += timeout  # the call hangs until its timeout
            result = StructuredResult(CallStatus.TIMEOUT)
        else:
            units = {int(i) for i in re.findall(r'<unit index="(\d+)"', input)}
            result = StructuredResult(CallStatus.OK, schema.model_validate(
                {"verdicts": [{"index": i, "verdict": "supported", "reason": "x"} for i in sorted(units)]}))
        self.calls.append(type("C", (), {"purpose": purpose, "input": input})())
        return result


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(llm, "deadline_left", c.left)
    monkeypatch.setattr(verify, "deadline_left", c.left, raising=False)
    return c


def _check(provider: TimingProvider, clock: Clock, left: float) -> tuple[verify.VerifyReport, T.Workspace]:
    ws = T.Workspace(ctx=None)
    ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="דוח בדיקה", section="סיכום",
                  location="סעיף \"סיכום\"", kind="context", text=SOURCE)
    answer = FinalAnswer(status="answered", answer_markdown=MARKDOWN, items=[], claims=[], clarification_question="",
                         missing_info="", referenced_document_ids=[], scope_kind="focused", scope_query="", omitted=[],
                         focus=None, requested=[], parts=[])
    return verify_answer(provider, answer, ws, "?", [], deadline=clock.now() + left), ws


def test_f2_a_late_judge_batch_that_times_out_still_gets_its_retry_inside_the_reserved_slice(clock):
    # the agent loop left 50 seconds of the verification window; the first call hangs to its timeout
    p = TimingProvider(clock, lambda n, input: n == 1)
    report, ws = _check(p, clock, 50)
    assert len(p.windows) == 2, p.windows  # the first call and its one retry
    assert p.windows[1] >= NEEDED, f"the retry was given {p.windows[1]}s: starved ({p.windows})"
    assert report.removals() == []  # the claims stand: a fully supported answer is not lost
    assert ws.clock.summary()["not_checked"] == {}


def test_the_first_call_is_cut_so_the_retry_keeps_the_reserve_and_a_roomy_deadline_is_unchanged(clock):
    p = TimingProvider(clock, lambda n, input: n == 1)
    _check(p, clock, 50)
    assert p.windows[0] == pytest.approx(50 - verify.JUDGE_RETRY_RESERVE_SECONDS, abs=1)
    roomy = TimingProvider(clock, lambda n, input: n == 1)
    _check(roomy, clock, 300)
    assert roomy.windows[0] == PURPOSE_TIMEOUT  # nothing is cut when the deadline leaves room for both calls


def test_too_little_time_for_two_windows_gives_the_one_call_everything(clock):
    p = TimingProvider(clock)
    report, _ = _check(p, clock, verify.JUDGE_RETRY_RESERVE_SECONDS + 5)
    assert p.windows == [pytest.approx(verify.JUDGE_RETRY_RESERVE_SECONDS + 5, abs=1)]
    assert report.removals() == []


def test_a_claim_still_unchecked_after_the_retry_is_withheld_recorded_with_its_cause_and_shown_as_not_checked(clock, monkeypatch):
    monkeypatch.setattr(verify, "JUDGE_MAX_UNITS", 1)  # two batches: the first answers, the second times out twice
    p = TimingProvider(clock, lambda n, input: "דמי השכירות" in input.split("<unit", 2)[-1])
    report, ws = _check(p, clock, 50)
    assert len(p.windows) == 3
    removed = report.removals()
    assert [r.cause for r in removed] == ["check_failed"]  # fail closed: the unchecked claim is not kept
    assert all(r.failure_kind == "not_checked" for r in removed)  # never a contradiction, never an absence
    assert ws.clock.summary()["not_checked"] == {"timeout": 1}  # the technical cause, by unit
    assert all("timeout" in r.reason and "לא נבדקה" in r.reason for r in removed)
    assert verify.CHECK_CAUSE_SENTENCES["check_failed"].endswith("ולא נמצאה חסרה.")
