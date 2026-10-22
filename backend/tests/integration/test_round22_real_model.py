"""Round 22 U11 with the real model (KTD10, R19; AE5): history keeps the meaning the server added to an earlier answer as a labelled
system note ("הערת מערכת על ..."), and the model must not take that note for a source fact. OPT-IN: ``-m real_model``; skipped without a
provider key (the repository's ``.env`` through the round-10 ``real_key`` fixture; the key is never printed). Host only, against
``rag_test``. Never run by the development workflow; the orchestrator runs it on the configured model (``gpt-6-luna``, no override).

Scenario (synthetic office, the round-20 lot-groups PDF of ``tests/fixtures/round20``, every name and figure invented): a two-turn
conversation in a new conversation, exactly three times.

1. The first answer states group A's area and the lease condition that the footnote of the document attaches to group A's contract; the
   server writes that condition next to the value (a ``qualifier`` or ``scope_note`` span), so turn two's history carries it as a system note.
2. The second turn asks for group B's area. The footnote belongs to group A's contract only.

Checks (by behaviour, never by wording; a pattern part is matched, so a Hebrew prefix does not change the outcome):

- ``shape_produced``: the first answer states group A's area with the condition (otherwise the repetition is counted as ``shape not produced``
  and is not a pass);
- ``note_not_copied_as_a_fact``: the second answer's own text (outside the spans the server wrote) states no condition of the land
  authority, no server span of it carries the first answer's condition or its claim ids, and the label ``הערת מערכת`` is not written in it as
  if it were a source;
- ``property_kept``: the second answer states group B's area and cites the same document, so the conversation's property is unchanged.

Recorded per repetition under ``real_documents/eval/round22/real-model/`` (gitignored)."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.integration.test_round10_real_model import new_conversation
from tests.integration.test_round10_real_model import real_key as real_key  # noqa: F401 - the key fixture
from tests.integration.test_round17_real_model import answered, cites_document, has
from tests.integration.test_round19_real_model import stated
from tests.integration.test_round21_real_model import (
    A4_FIRST,
    A4_SECOND,
    AREA_A,
    AREA_B,
    CONDITION,
    LOTS,
    SERVER_CONDITION_NOTE,
    Turn21,
    ingest_pdf,
)
from tests.integration.test_round21_real_model import (
    office as office,  # noqa: F401 - the fixture, taken by name
)

pytestmark = [pytest.mark.real_model, pytest.mark.db]

ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = ROOT / "real_documents" / "eval" / "round22" / "real-model"
STAMP = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
RESULTS_FILE = RESULTS_DIR / f"history-note-{STAMP}.json"
ATTEMPTS = 3
CHECKS = ("answered_first", "shape_produced", "answered_second", "note_not_copied_as_a_fact", "property_kept")
SYSTEM_NOTE_LABEL = re.compile(r"הערת\s+מערכת")
RESULTS: list[dict] = []


def _flush() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE.write_text(json.dumps({"model": "gpt-6-luna (settings)", "attempts": RESULTS}, ensure_ascii=False, indent=2, default=str),
                            encoding="utf-8")


def _ask(c, cid: str, question: str, rec: dict) -> Turn21:
    started = time.perf_counter()
    r = c.post(f"/api/chat/conversations/{cid}/messages", json={"content": question})
    assert r.status_code == 200, r.text
    m = c.get(f"/api/chat/messages/{r.json()['assistant']['id']}").json()
    t = Turn21(c, m, question, time.perf_counter() - started)
    rec["turns"].append(t.record())
    _flush()
    assert m["status"] == "done", (m.get("status"), m.get("error"))
    return t


def _check(rec: dict, name: str, fn) -> None:
    try:
        fn()
    except AssertionError as exc:
        rec["checks"][name] = {"outcome": "fail", "failure": str(exc)[:2000]}
    else:
        rec["checks"][name] = {"outcome": "pass"}
    _flush()


def _condition_in(t: Turn21) -> bool:
    return has(t.markdown, CONDITION)


def _note_not_copied(first: Turn21, second: Turn21) -> None:
    bad = []
    carriers = [s for s in second.spans if CONDITION.search(s["text"]) or SERVER_CONDITION_NOTE.search(s["text"])]
    if carriers:
        bad.append(("a server span of the second answer carries the first answer's condition", [(s["kind"], s["text"][:160]) for s in carriers]))
    shared = {c for s in first.spans for c in s["claim_ids"]} & {c for s in second.spans for c in s["claim_ids"]}
    if shared:
        bad.append(("a claim id of the first answer's additions is in the second answer's registry", sorted(shared)))
    beside = [ln[:200] for ln in second.stated_lines if has(ln, CONDITION)]
    if beside:
        bad.append(("the second answer states the condition of the first as a fact", beside[:2]))
    labelled = [ln[:200] for ln in second.stated_lines if SYSTEM_NOTE_LABEL.search(ln)]
    if labelled:
        bad.append(("the second answer writes the system note's label as if it were a source", labelled[:2]))
    assert not bad, bad


def _attempt(office, doc: str, n: int) -> dict:
    """One repetition in a new conversation; recorded (and flushed) before anything is asserted."""
    rec = {"sequence": "history_note_not_copied", "attempt": n, "turns": [], "checks": {}, "info": {}, "outcome": None}
    RESULTS.append(rec)
    try:
        cid = new_conversation(office.client)
        first = _ask(office.client, cid, A4_FIRST, rec)
        produced = _condition_in(first) and stated(first, AREA_A)
        rec["info"] = {"shape_produced": produced,
                       "first_spans": [(s["kind"], s["text"][:160], s["claim_ids"], s["datum"]) for s in first.spans]}
        second = _ask(office.client, cid, A4_SECOND, rec)
        rec["info"] |= {"second_spans": [(s["kind"], s["text"][:160], s["claim_ids"], s["datum"]) for s in second.spans]}
        _check(rec, "answered_first", lambda: answered(first))
        _check(rec, "shape_produced", lambda: _shape(first, produced))
        _check(rec, "answered_second", lambda: answered(second))
        _check(rec, "note_not_copied_as_a_fact", lambda: _note_not_copied(first, second))
        _check(rec, "property_kept", lambda: _property_kept(second, doc))
    except AssertionError as exc:
        rec["aborted"] = str(exc)[:2000]
    for name in CHECKS:
        rec["checks"].setdefault(name, {"outcome": "fail", "failure": "never ran: " + rec.get("aborted", "?")})
    rec["outcome"] = "pass" if all(v["outcome"] == "pass" for v in rec["checks"].values()) else "fail"
    _flush()
    return rec


def test_history_note_is_not_copied_as_a_fact(office, monkeypatch):  # noqa: F811
    doc = ingest_pdf(office, monkeypatch, LOTS)
    failures: list[str] = []
    for n in range(1, ATTEMPTS + 1):
        rec = _attempt(office, doc, n)
        failures += [f"attempt {n} / {name}: {v['failure']}" for name, v in rec["checks"].items() if v["outcome"] == "fail"]
    assert not failures, failures


def _shape(first: Turn21, produced: bool) -> None:
    assert produced, ("SHAPE NOT PRODUCED: the first answer does not state group A's area with the lease condition", first.markdown[:500])


def _property_kept(second: Turn21, doc: str) -> None:
    assert stated(second, AREA_B), ("the second answer does not state group B's area", second.markdown[:500])
    cites_document(second, doc)
