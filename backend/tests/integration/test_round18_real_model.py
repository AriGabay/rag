"""Round 18 acceptance with the real model (U9: R14, R16; AE1, AE6, AE7, AE8): the condition, deictic-scope, heading-scope and
candidate-row shapes shown on the configured model and judge (``gpt-6-luna`` with the repository's settings, no override; the key
comes from the repository's ``.env`` through the round-10 ``real_key`` fixture and is never printed). Synthetic documents only
(every name, address and figure invented): one PDF of ``tests/fixtures/round18`` per sequence (``scripts/generate_fixtures.py --only
round18``; the contents are the manifest's), ingested through the worker into a synthetic office of the ``rag_test`` database.

- ``c1`` a condition in a row note (AE1): R18f, a table whose total-area row says the rights apply only under an approved plan.
  Asked for that area, the answer carries the approved-plan condition with a citation next to the value, and the value is kept.
  The condition may be written by the agent, added by the completion repair or attached by the server; a "not verified" note
  is not the condition;
- ``c2`` a deictic scope (AE7): R18g, a sensitivity table, a note right after it that points back ("the table above refers only
  to the income-capitalisation approach"), three unrelated blocks, then another table. Asked for the value at one rate, the scope
  stands next to the value with a citation (written by the agent or attached by the server), and the answer does not say that
  the scope was not verified;
- ``c3`` a method heading (AE8): R18h, a numbered heading naming the income-capitalisation approach over a table, and a later
  section whose own note scopes its table to the comparison approach. Asked for a value of the first table, the heading's method
  stands next to the value with a citation and the later section's note is not applied to it;
- ``c4`` candidate rows (AE6): R18i, a table of per-use parking rates with an annual-subscription row. Asked for the rates per
  use, the SERVER adds no row the question did not ask about (the subscription): no candidate or origin-``source`` item of the
  ledger holds it (and so no completion repair of one), and no span the server wrote states it. A row the AGENT wrote itself in its
  draft (an agent item, an agent sentence) is the model's choice, not the candidate mechanism: it is reported as ``agent_added_unasked_rows``
  (a count), not asserted. The number of asked rows that reached the answer is reported, not asserted.

Each sequence is asked in a **new conversation, exactly three times**; every repetition is recorded before anything is asserted -
a failure in any repetition is reported and a later pass never hides it - and a sequence passes only when all three repetitions
pass. The gating checks are by behaviour and never by wording (a pattern part is matched, so a Hebrew definite article or prefix
does not change the outcome).

Recorded per repetition (``real_documents/eval/round18/real-model/``, gitignored: the documents are synthetic, so the answers
are kept for diagnosis): the whole turn (final text, rounds, tools, item ledger, the spans the server wrote with their kind and
text, usage), the pass or fail of each check, and **the stage that produced the part** - ``agent_draft`` (the agent's own
text carries it), ``completion_repair`` (a verification round asked for it - ``text_found``, ``condition_found``, a missing
qualifier - and the repaired text carries it), ``server_attachment`` (it stands inside a span the server wrote) or
``omitted``. The stage is read from the diagnostics and the server-span registry, so a mixed case is told by the rounds listed in
the record. A summary file per sequence is written when the sequence ends, and one for the run at the end of the module.

Opt in with ``-m real_model``; skipped without a key. Host only, against ``rag_test`` (``tests/conftest.py`` guard)."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_round10_real_model import new_conversation, upload
from tests.integration.test_round10_real_model import real_key as real_key  # noqa: F401 - the key fixture
from tests.integration.test_round17_real_model import (
    CITED,
    SCOPE,
    UNVERIFIED,
    Turn,
    answered,
    around,
    cites_document,
    has,
    reached_all,
    stage,
)

pytestmark = [pytest.mark.real_model, pytest.mark.db]

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANIFEST = json.loads((FIXTURES / "round18" / "manifest.json").read_text(encoding="utf-8"))["documents"]
APPROVED_PLAN, DEICTIC_NOTE = MANIFEST["approved_plan"], MANIFEST["deictic_note"]
METHOD_HEADING, RATE_TABLE = MANIFEST["method_heading"], MANIFEST["rate_table"]
RESULTS_DIR = ROOT / "real_documents" / "eval" / "round18" / "real-model"
STAMP = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
RESULTS_FILE = RESULTS_DIR / f"results-{STAMP}.json"
SUMMARY_FILE = RESULTS_DIR / f"summary-{STAMP}.json"
RESULTS: list[dict] = []
ATTEMPTS = 3  # exactly three, each in a new conversation
STARTED = time.perf_counter()
MODEL = "gpt-6-luna (settings)"

ADMIN = "admin-r18@example.test"


def figure(digits: str) -> re.Pattern:
    """A figure written with or without its thousands separators (``11,800`` / ``11800`` / ``9,384,615``) and not part of a longer
    number, a decimal or an identifier (``S8``)."""
    head = len(digits) % 3 or 3
    parts = [digits[:head], *(digits[i:i + 3] for i in range(head, len(digits), 3))]
    return re.compile(r"(?<![\w.,])" + r"[,.]?".join(parts) + r"(?![\d]|[.,]\d)")


# --- what each sequence looks for -----------------------------------------------------------------------------------------

AREA = figure("1200")  # the total-area row of R18f ("1,200")
PLAN = re.compile(r"ת(?:ו)?כנית\s+(?:ה)?מאושרת")  # "an approved plan", whatever the prefix or the spelling of the noun
ONLY = re.compile(r"רק|בלבד|אך\s+ורק|מותנ")  # informational: the "only" of the condition
VALUE_6 = figure("11800")  # the 6.0% row of R18g (the 0% row of table 5.2 shows the same figure)
CAPITALISED = figure("9384615")  # the capitalised value of R18h
COMPARISON = re.compile(r"גישת\s+ה?השוואה|בגישת\s+ההשוואה")  # the approach the later section's note names
UNASKED_LABEL = re.compile(r"מנוי")  # the annual-subscription row of R18i, which the question does not ask about
UNASKED_VALUE = figure("900")
ASKED_ROWS = {"single_entry": (re.compile(r"בודד"), figure("12")),
              "night_entry": (re.compile(r"לילה"), figure("8")),
              "weekend_entry": (re.compile(r"סוף\s+שבוע"), figure("15"))}
# the signs, in a verification round's problems, that the server asked the agent to write the part (the completion repair)
REPAIR_SIGNS = {"text_found", "condition_found", "missing_qualifier", "scope", "condition"}


# --- environment ----------------------------------------------------------------------------------------------------------

@pytest.fixture
def office(db, client, monkeypatch, real_key):  # noqa: F811 - the key fixture is the imported one
    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    o = make_office(db, "משרד בדיקה סינתטי", ADMIN)
    with tenant_tx(o.ctx()) as conn:
        conn.execute(text("UPDATE office_settings SET cloud_llm_enabled = true"))
    login(client, ADMIN)
    o.client, o.ids = client, {}
    return o


def ingest_pdf(office, monkeypatch, spec: dict) -> str:
    """A round-18 PDF fixture read by the worker; ``office.ids[title]`` is its document id."""
    office.ids[spec["title"]] = upload(office, FIXTURES / spec["file"], "appraisal", monkeypatch)
    return office.ids[spec["title"]]


# --- turns and records ----------------------------------------------------------------------------------------------------

def flush() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE.write_text(json.dumps({"model": MODEL, "attempts": RESULTS}, ensure_ascii=False, indent=2, default=str),
                            encoding="utf-8")


class Turn18(Turn):
    """The round-17 turn record plus the spans the server wrote in the answer (kind and text), the registry the stage of a part
    is read from."""

    def __init__(self, c, m: dict, question: str, wall: float) -> None:
        super().__init__(c, m, question, wall)
        d = c.get(f"/api/chat/messages/{m['id']}/diagnostics")
        body = d.json() if d.status_code == 200 else {}
        resolution = body.get("resolution") or {}
        # what the server asked of the requirements call about each item's condition, and what the call answered (the evidence of
        # a condition that did not reach the answer: no item, no check, a check the call left undecided, or a judge outcome)
        self.condition_trace = {
            "items": [{"id": i.get("id"), "origin": i.get("origin"), "candidate": i.get("candidate"), "state": i.get("state"),
                       "check": i.get("condition_check"), "unit": i.get("condition_unit"),
                       "spans": [(sp.get("place"), (sp.get("text") or "")[:120]) for sp in i.get("condition_spans") or []]}
                      for i in (resolution.get("item_ledger") or {}).get("items") or []],
            "judge": [{"round": r.get("round"), "component": comp.get("id"), "conditions": v.get("conditions")}
                      for r in resolution.get("requirements_trace") or [] for comp in r.get("components") or []
                      for v in comp.get("votes") or [] if v.get("conditions")]}

    @property
    def spans(self) -> list[dict]:
        md = self.markdown
        return [{"kind": s.get("kind"), "start": s.get("start"), "end": s.get("end"), "text": md[s["start"]:s["end"]]}
                for s in self.answer.get("server_spans") or []
                if isinstance(s.get("start"), int) and isinstance(s.get("end"), int)]

    def part_stage(self, pattern: re.Pattern) -> str:
        """The stage the part ``pattern`` matches reached the answer in: ``server_attachment`` when its text stands inside a span
        the server wrote, else ``completion_repair`` when a verification round asked for a part of this kind, else
        ``agent_draft``; ``omitted`` when the answer does not carry it."""
        m = pattern.search(self.markdown)
        if m is None:
            return "omitted" if not any(pattern.search(ln) for ln in self.lines) else "agent_draft"
        if any(s["start"] <= m.start() < s["end"] for s in self.spans):
            return "server_attachment"
        asked = any(p["check"] in REPAIR_SIGNS or p["kind"] in REPAIR_SIGNS for r in self.rounds for p in r)
        return "completion_repair" if asked else "agent_draft"

    def record(self) -> dict:
        return super().record() | {"server_spans": self.spans, "condition_trace": self.condition_trace}


def ask(c, cid: str, question: str, rec: dict) -> Turn18:
    """One turn through the API; recorded (and flushed) before anything is asserted about it."""
    started = time.perf_counter()
    r = c.post(f"/api/chat/conversations/{cid}/messages", json={"content": question})
    assert r.status_code == 200, r.text
    m = c.get(f"/api/chat/messages/{r.json()['assistant']['id']}").json()
    t = Turn18(c, m, question, time.perf_counter() - started)
    rec["turns"].append(t.record())
    flush()
    assert m["status"] == "done", (m.get("status"), m.get("error"))
    return t


def check(rec: dict, name: str, fn) -> None:
    """Run one named assertion of a repetition and record its outcome; a failure never stops the repetition's other checks."""
    try:
        fn()
    except AssertionError as exc:
        rec["checks"][name] = {"outcome": "fail", "failure": str(exc)[:2000]}
    else:
        rec["checks"][name] = {"outcome": "pass"}
    flush()


def sequence_summary(sequence: str) -> dict:
    runs = [r for r in RESULTS if r["sequence"] == sequence]
    attempts = []
    for r in runs:
        info = r.get("info") or {}
        turn = (r.get("turns") or [{}])[-1]
        attempts.append({"attempt": r["attempt"], "outcome": r["outcome"],
                         "checks": {k: v["outcome"] for k, v in r["checks"].items()},
                         "failures": {k: v["failure"] for k, v in r["checks"].items() if v["outcome"] == "fail"},
                         "stage": info.get("stage"), "info": {k: v for k, v in info.items() if k != "stage"},
                         "final_text": turn.get("markdown"), "server_spans": turn.get("server_spans"),
                         "rounds": turn.get("rounds"), "tools": turn.get("tools")})
    passed = sum(a["outcome"] == "pass" for a in attempts)
    return {"model": MODEL, "sequence": sequence, "attempts": attempts, "passed_runs": f"{passed}/{len(attempts)}",
            "passed": len(attempts) == ATTEMPTS and passed == ATTEMPTS}


def run_sequence(sequence: str, checks: tuple[str, ...], office, body) -> None:
    """``body(office, client, conversation_id, record)`` in a new conversation, exactly ``ATTEMPTS`` times; every repetition is
    recorded and none is skipped after a failure. The summary file of the sequence is written before anything is asserted. The
    test fails when any check of any repetition failed (or never ran because a turn did not finish), with every failure in the
    message."""
    failures: list[str] = []
    for n in range(1, ATTEMPTS + 1):
        rec = {"sequence": sequence, "attempt": n, "turns": [], "checks": {}, "info": {}, "outcome": None}
        RESULTS.append(rec)
        try:
            body(office, office.client, new_conversation(office.client), rec)
        except AssertionError as exc:
            rec["aborted"] = str(exc)[:2000]
        for name in checks:
            rec["checks"].setdefault(name, {"outcome": "fail", "failure": "never ran: " + rec.get("aborted", "?")})
        rec["outcome"] = "pass" if all(v["outcome"] == "pass" for v in rec["checks"].values()) else "fail"
        flush()
        failures += [f"attempt {n} / {name}: {v['failure']}" for name, v in rec["checks"].items() if v["outcome"] == "fail"]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / f"summary-{sequence}-{STAMP}.json").write_text(
        json.dumps(sequence_summary(sequence), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    assert not failures, "\n".join(failures)


def base_info(t: Turn18, reached: dict[str, bool], parts: dict[str, str]) -> dict:
    """The record shared by the sequences: which parts reached the answer and in which stage, what the turn reports."""
    return {"parts_reached": reached, "stage": parts, "completeness": t.completeness.get("status"),
            "completed_by_server": t.completed_by_server, "repair_rounds": t.repair_rounds, "tools": t.tools,
            "server_span_kinds": [s["kind"] for s in t.spans],
            "ledger": [(i["label"], i["value"], i["state"], i["declared"], i["origin"]) for i in t.items]}


def with_citation(o, t: Turn18, spans: list[str], doc: str) -> list[str]:
    """The spans that cite ``doc`` (a source or a registered value of it)."""
    return [s for s in spans if CITED.search(s) and doc in t.documents_cited_in(s)]


# =========================================================================================================================
# the four sequences
# =========================================================================================================================

def test_c1_a_row_condition_stands_next_to_the_value_with_its_citation_three_times(office, monkeypatch):
    doc = ingest_pdf(office, monkeypatch, APPROVED_PLAN)

    def condition_kept(o, t: Turn18) -> None:
        spans = around(t, AREA)
        assert spans, ("the value is not stated", t.markdown[:300])
        with_plan = [s for s in spans if PLAN.search(s)]
        assert with_plan, ("the approved-plan condition does not stand next to the value", spans[:2])
        assert with_citation(o, t, with_plan, doc), ("the condition stands next to the value without a citation of the table", with_plan[:2])

    def body(o, c, cid, rec):
        t = ask(c, cid, "מהו השטח הכולל לבנייה בנכס ברחוב הגפן לפי השומה?", rec)
        lines = t.lines
        reached = {"value": any(has(ln, AREA) for ln in lines), "condition": any(has(ln, PLAN) for ln in lines)}
        rec["info"] = base_info(t, reached, {"value": stage(t, reached["value"]), "condition": t.part_stage(PLAN)})
        rec["info"]["only_word_near_condition"] = any(ONLY.search(s) for s in around(t, PLAN))
        check(rec, "answered", lambda: answered(t))
        check(rec, "value_kept", lambda: reached_all({"value": reached["value"]}))
        check(rec, "cited", lambda: cites_document(t, doc))
        check(rec, "condition_with_citation", lambda: condition_kept(o, t))

    run_sequence("c1_row_note_approved_plan_only", ("answered", "value_kept", "cited", "condition_with_citation"), office, body)


def test_c2_a_deictic_scope_after_a_table_is_attached_to_the_value_three_times(office, monkeypatch):
    doc = ingest_pdf(office, monkeypatch, DEICTIC_NOTE)

    def scoped(o, t: Turn18) -> None:
        spans = around(t, VALUE_6)
        assert spans, ("the value is not stated", t.markdown[:300])
        with_scope = [s for s in spans if SCOPE.search(s)]
        assert with_scope, ("the scope of the table does not stand next to the value", spans[:2])
        assert with_citation(o, t, with_scope, doc), ("the scope stands next to the value without a citation of the table", with_scope[:2])

    def not_unverified(t: Turn18) -> None:
        notes = [s for s in t.spans if s["kind"] == "scope_note"]
        said = [s for s in around(t, VALUE_6) if UNVERIFIED.search(s)]
        assert not notes and not said, ("the value carries a note that its scope was not verified", notes[:2], said[:2])

    def body(o, c, cid, rec):
        t = ask(c, cid, "מהו שווי הנכס בשומה בשיעור היוון של 6.0%?", rec)
        lines = t.lines
        reached = {"value": any(has(ln, VALUE_6) for ln in lines), "scope": any(has(ln, SCOPE) for ln in lines)}
        rec["info"] = base_info(t, reached, {"value": stage(t, reached["value"]), "scope": t.part_stage(SCOPE)})
        rec["info"]["only_word_near_scope"] = any(ONLY.search(s) for s in around(t, SCOPE))
        check(rec, "answered", lambda: answered(t))
        check(rec, "value_kept", lambda: reached_all({"value": reached["value"]}))
        check(rec, "cited", lambda: cites_document(t, doc))
        check(rec, "scope_attached", lambda: scoped(o, t))
        check(rec, "no_not_verified_note", lambda: not_unverified(t))

    run_sequence("c2_deictic_note_after_table",
                 ("answered", "value_kept", "cited", "scope_attached", "no_not_verified_note"), office, body)


def test_c3_the_headings_method_reaches_the_value_and_the_later_note_does_not_three_times(office, monkeypatch):
    doc = ingest_pdf(office, monkeypatch, METHOD_HEADING)

    def method_reaches(o, t: Turn18) -> None:
        spans = around(t, CAPITALISED)
        assert spans, ("the value is not stated", t.markdown[:300])
        with_method = [s for s in spans if SCOPE.search(s)]
        assert with_method, ("the heading's method does not stand next to the value", spans[:2])
        assert with_citation(o, t, with_method, doc), ("the method stands next to the value without a citation of the table", with_method[:2])

    def later_note_not_applied(t: Turn18) -> None:
        hits = [s for s in around(t, CAPITALISED) if COMPARISON.search(s)]
        assert not hits, ("the later section's note (the comparison approach) is applied to the value", hits[:2])

    def body(o, c, cid, rec):
        t = ask(c, cid, "מהו השווי המהוון בשומה למשרדים ברחוב האלון?", rec)
        lines = t.lines
        reached = {"value": any(has(ln, CAPITALISED) for ln in lines), "method": any(has(ln, SCOPE) for ln in lines)}
        rec["info"] = base_info(t, reached, {"value": stage(t, reached["value"]), "method": t.part_stage(SCOPE)})
        rec["info"]["comparison_approach_anywhere_in_answer"] = any(has(ln, COMPARISON) for ln in lines)
        rec["info"]["condition_trace"] = t.condition_trace
        check(rec, "answered", lambda: answered(t))
        check(rec, "value_kept", lambda: reached_all({"value": reached["value"]}))
        check(rec, "cited", lambda: cites_document(t, doc))
        check(rec, "method_reaches_value", lambda: method_reaches(o, t))
        check(rec, "later_note_not_applied", lambda: later_note_not_applied(t))

    run_sequence("c3_method_heading_over_table",
                 ("answered", "value_kept", "cited", "method_reaches_value", "later_note_not_applied"), office, body)


def test_c4_candidate_rows_add_no_row_the_question_did_not_ask_for_three_times(office, monkeypatch):
    doc = ingest_pdf(office, monkeypatch, RATE_TABLE)

    def unasked_rows(t: Turn18) -> dict:
        """Where the annual-subscription row (not a per-use rate) appears, by who put it there: ``server`` - a ledger item that is
        not the agent's (a candidate or a span-derived item, whose completion repair is the only way a candidate reaches the answer),
        or a span the server wrote that states it; ``agent`` - a sentence of the answer outside the server's spans and the agent's own
        ledger items, which are the model's choice and only counted."""
        states_it = lambda text: has(text, UNASKED_VALUE) or (has(text, UNASKED_LABEL) and re.search(r"\d", text))  # noqa: E731
        ledger = [(i["label"], i["value"], i["state"], i["origin"]) for i in t.items
                  if has(str(i["label"] or ""), UNASKED_LABEL) or has(str(i["value"] or ""), UNASKED_VALUE)]
        server = [("ledger", x) for x in ledger if x[3] != "agent"]
        server += [("span", sp["kind"], sp["text"].strip()) for sp in t.spans if sp["kind"] != "citation" and states_it(sp["text"])]
        in_spans = [sp["text"] for sp in t.spans if sp["kind"] != "citation"]
        written = [ln for ln in t.lines if states_it(ln) and not any(ln in " ".join(sp.split()) for sp in in_spans)]
        declared = [x for x in ledger if x[3] == "agent"]
        # one row, once: the sentence that states it and the ledger item that declares it are the same row
        return {"server": server, "agent": [("answer", ln) for ln in written] + [("ledger", x) for x in declared],
                "agent_count": max(len(written), len(declared))}

    def no_unasked(t: Turn18) -> None:
        added = unasked_rows(t)["server"]
        assert not added, ("the server added a row the question did not ask about", added[:3])

    def body(o, c, cid, rec):
        t = ask(c, cid, "מהם התעריפים לשימוש בחניון ברחוב התמר?", rec)
        lines = t.lines
        reached = {n: any(has(ln, label) and has(ln, value) for ln in lines) for n, (label, value) in ASKED_ROWS.items()}
        unasked = unasked_rows(t)
        rec["info"] = base_info(t, reached, {n: stage(t, r) for n, r in reached.items()})
        rec["info"] |= {"asked_rows_reached": sum(reached.values()), "asked_rows_total": len(ASKED_ROWS),
                        "unasked_rows_added_by_server": unasked["server"],
                        "agent_added_unasked_rows": unasked["agent_count"], "agent_added_unasked_rows_detail": unasked["agent"],
                        "unasked_label_mentioned": any(has(ln, UNASKED_LABEL) for ln in lines)}
        check(rec, "answered", lambda: answered(t))
        check(rec, "cited", lambda: cites_document(t, doc))
        check(rec, "no_unasked_row_added_by_server", lambda: no_unasked(t))

    run_sequence("c4_candidates_asked_and_unasked_rows", ("answered", "cited", "no_unasked_row_added_by_server"), office, body)


# --- the summary ----------------------------------------------------------------------------------------------------------

def build_summary() -> dict:
    turns = [t for r in RESULTS for t in r.get("turns") or []]
    purposes: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for t in turns:
        for k, n in t["usage"]["purposes"].items():
            purposes[k] = purposes.get(k, 0) + n
        for k, n in t["usage"]["statuses"].items():
            statuses[k] = statuses.get(k, 0) + n
    costs = [t["usage"]["cost_usd"] for t in turns if t["usage"]["cost_usd"] is not None]
    names = list(dict.fromkeys(r["sequence"] for r in RESULTS))
    sequences = {}
    for name in names:
        s = sequence_summary(name)
        sequences[name] = {"passed": s["passed"], "passed_runs": s["passed_runs"],
                           "attempts": [{k: a[k] for k in ("attempt", "outcome", "checks", "failures", "stage", "info")}
                                        for a in s["attempts"]]}
    return {"model": MODEL, "turns": len(turns), "attempts": len(RESULTS), "sequences": sequences,
            "usage_purposes": purposes, "usage_statuses": statuses, "cost_usd": round(sum(costs), 4) if costs else None,
            "model_latency_s": round(sum(t["usage"]["model_latency_ms"] for t in turns) / 1000, 1),
            "wall_s": round(time.perf_counter() - STARTED, 1)}


def teardown_module(module) -> None:  # noqa: ARG001
    if RESULTS:
        flush()
        SUMMARY_FILE.write_text(json.dumps(build_summary(), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        print(f"\nreal_model results: {os.path.relpath(RESULTS_FILE, ROOT)}\nsummary: {os.path.relpath(SUMMARY_FILE, ROOT)}")
