"""Round 14 acceptance with the real model (U9: R1-R17; AE1, AE5, AE6; the plan's success criteria): the body of an answer and its
components table agree on the synthetic round-14 DOCX fixtures (``tests/fixtures/round14``) - every name, address and amount
invented - ingested through the worker into a synthetic office of the ``rag_test`` database. Questions go through the chat API to
the configured model and judge (``gpt-6-luna`` with the repository's settings), whose key comes from the repository's ``.env`` and is
never printed.

Three sequences, each in a **new conversation, exactly three times**; every attempt is recorded, a failure in any attempt is reported
(a later pass never hides it), and a sequence passes only when all three attempts pass:

- S1 the reported shape (AE1): two reductions of one appraisal, the first with its own consideration and the second explained two
  sentences earlier in its paragraph ("in light of the above, an additional reduction coefficient of 10%");
- S2 a table coefficient and its text reduction (AE6): the coefficient 0.9 of a consideration in a table, and the text's reduction of
  10% for the same consideration (1 - 0.9);
- S3 three equal reductions (AE5): three reductions of 10%, each for a consideration of its own.

Each asks a natural Hebrew question and checks the answer and the table together, per attempt, by behaviour and never by wording (the
gating checks):

- the question is answered, not clarified, as a property question;
- every coefficient or rate the answer registered carries the state the document gives its datum (the consideration it states, or the
  reason it states none), matched by the document and the quoted sentence or row;
- the body and the components table agree: a value the answer cites that belongs to a components block is on screen once, and its
  row states the component of the value's own facet;
- when the answer has a components table, every cited reduction or coefficient the document gives a component is in it;
- no row says "component not stated in the source" for a component the document states, and no row has a component of a value it
  does not belong to;
- no conflict or "undecided" mark appears between the parallel adjustments, and every mark names a row on screen (a counterpart that
  is not on screen is said not to be in the answer);
- S2: a coefficient and the reduction of the same consideration the answer cites are one adjustment, one row;
- S3: three considerations are three rows;
- no grounded number is removed from the answer.

Informational values (the registered values, the derived computations, every decision, the rows, the model calls and their wall time)
are recorded, not gated. Per turn the whole record is written to ``real_documents/eval/round14/real-model/`` (gitignored and private:
answers are kept for diagnosis), with a summary file next to it listing every attempt of every sequence.

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

from app.chat import timeline
from app.chat import tools as T
from app.config import get_settings
from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_round10_real_model import new_conversation, upload
from tests.integration.test_round10_real_model import real_key as real_key  # noqa: F401 - the key fixture
from tests.integration.test_round12_real_model import Turn, answered
from tests.integration.test_round14_sequences import consistent_with_body

pytestmark = [pytest.mark.real_model, pytest.mark.db]

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANIFEST = json.loads((FIXTURES / "round14" / "manifest.json").read_text(encoding="utf-8"))
DOCS = MANIFEST["documents"]
DATUM = {d["id"]: d | {"doc": key} for key, spec in DOCS.items() for d in spec.get("data", [])}
RESULTS_DIR = ROOT / "real_documents" / "eval" / "round14" / "real-model"
STAMP = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
RESULTS_FILE = RESULTS_DIR / f"results-{STAMP}.json"
SUMMARY_FILE = RESULTS_DIR / f"summary-{STAMP}.json"
RESULTS: list[dict] = []
ATTEMPTS = 3  # exactly three, each in a new conversation
STARTED = time.perf_counter()

ADMIN = "admin-r14@example.test"
TOOL_LOG: list[dict] = []  # every tool call of the run, in order (a turn reads its own slice)
FACET_KINDS = ("coefficient", "rate")
GONE = ("removed", "cut", "withheld", "rewritten", "qualified")  # a claim lost, cut, withheld, rewritten or edited
FACET_KEYS = ("status", "part", "tier")


# --- environment -------------------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def record_tools(monkeypatch):
    """Every tool call the engine makes: its name, arguments and the sources its output returned."""
    original = T.run_tool

    def run_tool(ws, name, arguments):
        output = original(ws, name, arguments)
        try:
            args = json.loads(arguments)
        except (TypeError, ValueError):
            args = {"raw": str(arguments)[:200]}
        hits = []
        for m in re.finditer(r"<source ([^>]*)>", output):
            attrs = dict(re.findall(r'(\w+)="([^"]*)"', m.group(1)))
            hits.append({k: attrs.get(k) for k in ("id", "document_id", "title", "location", "date", "kind")})
        TOOL_LOG.append({"name": name, "args": args, "hits": hits, "output_chars": len(output),
                         "error": output.startswith("שגיאה")})
        return output

    monkeypatch.setattr(T, "run_tool", run_tool)


@pytest.fixture
def office(db, client, monkeypatch, real_key):  # noqa: F811 - the key fixture is the imported one
    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    o = make_office(db, "משרד בדיקה סינתטי", ADMIN)
    with tenant_tx(o.ctx()) as conn:
        conn.execute(text("UPDATE office_settings SET cloud_llm_enabled = true"))
    login(client, ADMIN)
    o.client, o.ids, o.monkeypatch, o.by_id = client, {}, monkeypatch, {}
    return o


def ingest(office, monkeypatch, keys: tuple[str, ...]) -> None:
    """The round-14 DOCX fixtures ``keys`` through the worker (single appraisals)."""
    for key in keys:
        d = DOCS[key]
        office.ids[key] = upload(office, FIXTURES / d["file"], d["source_kind"], monkeypatch)
    office.by_id = {v: k for k, v in office.ids.items()}


# --- turns and records -------------------------------------------------------------------------------------------------

def flush() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE.write_text(json.dumps({"model": "gpt-6-luna (settings)", "attempts": RESULTS}, ensure_ascii=False, indent=2,
                                       default=str), encoding="utf-8")


class Turn14(Turn):
    """A finished turn plus what the round-14 checks read: the diagnostics' decisions, the values with their facets, the rows of the
    timeline and of the components table with their states, and the derived computations."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        d = self.office.client.get(f"/api/chat/messages/{self.m['id']}/diagnostics")
        self.decisions = [{k: x.get(k) for k in ("kind", "check", "action", "reason", "text", "failure_kind")}
                          for x in (d.json().get("removed") or [])] if d.status_code == 200 else []

    @property
    def rows(self) -> list[dict]:
        """Every row of the timeline and of the components table."""
        blocks = (self.time.get("timeline") or []) + (self.time.get("breakdown") or [])
        return [r for b in blocks for r in b["rows"]]

    @property
    def table(self) -> list[dict]:
        """The rows of the components table."""
        return [r for b in self.time.get("breakdown") or [] for r in b["rows"]]

    def record(self) -> dict:
        return super().record() | {
            "decisions": self.decisions,
            "values_facets": [{"id": v.get("id"), "kind": v.get("kind"), "value_text": v.get("value_text"),
                               "document": self.key_of(v.get("document_id")), "quote": v.get("quote"),
                               "facet": v.get("facet")} for v in self.values],
            "computations": [{"id": c.get("id"), "result_kind": c.get("result_kind"), "expression": c.get("expression"),
                              "display": c.get("display")} for c in self.answer.get("computations") or []],
            "rows": [{"sid": r["sid"], "value_text": r["value_text"], "facet": r["facet"], "marks": r["marks"],
                      "state": {"component": (r.get("state") or {}).get("component", {}).get("status"),
                                "reason": (r.get("state") or {}).get("reason")},
                      "reduction": (r.get("reduction") or {}).get("sid"), "derived": r.get("derived"),
                      "undecided_with": r.get("undecided_with"), "conflict_with": r.get("conflict_with"),
                      "not_in_answer_with": [x.get("sid") for x in r.get("not_in_answer_with") or []]} for r in self.rows],
            "partials": (self.answer.get("verification") or {}).get("partials"),
            "gaps": [g.get("reason") for g in self.answer.get("gaps") or []],
            "tool_errors": [c for c in self.tools if c.get("error")],
        }


def ask(office, c, cid: str, question: str, rec: dict) -> Turn14:
    """One turn through the API; recorded (and flushed) before anything is asserted about it."""
    started = time.perf_counter()
    first_tool = len(TOOL_LOG)
    r = c.post(f"/api/chat/conversations/{cid}/messages", json={"content": question})
    assert r.status_code == 200, r.text
    m = c.get(f"/api/chat/messages/{r.json()['assistant']['id']}").json()
    t = Turn14(office, c, m, question, time.perf_counter() - started, None, list(TOOL_LOG[first_tool:]))
    rec["turns"].append(t.record())
    flush()
    assert m["status"] == "done", (m.get("status"), m.get("error"))
    return t


def check(rec: dict, name: str, fn) -> None:
    """Run one named assertion of an attempt and record its outcome; a failure never stops the attempt's other checks."""
    try:
        fn()
    except AssertionError as exc:
        rec["checks"][name] = {"outcome": "fail", "failure": str(exc)[:2000]}
    else:
        rec["checks"][name] = {"outcome": "pass"}
    flush()


def run_sequence(sequence: str, checks: tuple[str, ...], office, body) -> None:
    """``body(office, client, conversation_id, record)`` in a new conversation, exactly ``ATTEMPTS`` times; every attempt is
    recorded and none is skipped after a failure. The test fails when any check of any attempt failed (or never ran because
    a turn did not finish), with every failure in the message."""
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
    assert not failures, "\n".join(failures)


# --- the assertions ----------------------------------------------------------------------------------------------------

def plain(s: str | None) -> str:
    """A facet's words compared without quote marks, gershayim and spacing."""
    return re.sub(r"[\s\"'“”„״׳]+", " ", s or "").strip()


def number_of(d: dict) -> str:
    return re.sub(r"[^\d.,]", "", d["shown"].split()[0])


def numbers(s: str) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", s or "")}


def datum_words(d: dict) -> str:
    """The sentence or the row label a datum is quoted by."""
    return plain((d["where"].get("quote") or d["where"]["table"]["row_label"]).rstrip("."))


def expected_datum(t: Turn14, v: dict) -> dict | None:
    """The manifest datum a registered value is: of its document, the same number and, when several share it (three reductions of
    10%), the one whose quoted sentence or row the value's quote holds."""
    key = t.key_of(v.get("document_id"))
    same = [d for d in DATUM.values() if d["doc"] == key and numbers(number_of(d)) == numbers(v.get("value_text") or "")]
    if len(same) > 1:
        quote = plain(v.get("quote"))
        same = [d for d in same if datum_words(d) in quote] or same
    return same[0] if len(same) == 1 else None


def facets_expected(t: Turn14) -> None:
    """Every coefficient or rate registered carries the state the manifest gives its datum: the status, part and tier, the source's
    own words when stated, and the reason when the document states none."""
    checked = 0
    for v in t.values:
        if v.get("kind") not in FACET_KINDS:
            continue
        d = expected_datum(t, v)
        if d is None:
            continue  # a number the manifest does not list as a datum of that document: recorded, not judged
        checked += 1
        want, got = d["facet"], v.get("facet") or {}
        assert all(got.get(k) == want.get(k) for k in FACET_KEYS), (v["id"], d["id"], {k: got.get(k) for k in FACET_KEYS},
                                                                  {k: want.get(k) for k in FACET_KEYS})
        if want.get("status") == "stated":
            assert plain(got.get("written")) == plain(want.get("written")), (v["id"], d["id"], got.get("written"), want.get("written"))
        elif want.get("reason"):
            assert got.get("reason") == want["reason"], (v["id"], d["id"], got.get("reason"), want["reason"])
    assert checked, ("no coefficient or rate of the manifest was registered", [(v.get("id"), v.get("kind"), v.get("value_text")) for v in t.values])


def body_and_table_agree(t: Turn14) -> None:
    """A cited value of a components block is on screen once and its row states its own facet (round 14 U9's consistency check)."""
    consistent_with_body(t.answer)


def on_screen(t: Turn14) -> set[str]:
    """The ids on screen: a row's, and the reduction of an adjustment's row."""
    return {r["sid"] for r in t.rows} | {(r.get("reduction") or {}).get("sid") for r in t.rows} - {None}


def key_of_value(v: dict) -> tuple:
    f = v.get("facet") or {}
    return (plain(f.get("written")) if f.get("status") == "stated" else "", v.get("value_text"))


def cited_listed(t: Turn14) -> None:
    """When the answer has a components table, every coefficient or rate it cites whose component the document states is in it: its
    own row (or the reduction of its coefficient's row), or a registration of the same component and number that is (the model often
    registers a value twice)."""
    if not t.table:
        return
    cited = set(re.findall(r"\[(V\d+)\]", t.markdown))
    by_id = {v["id"]: v for v in t.values}
    shown = on_screen(t)
    listed = {key_of_value(by_id[s]) for s in shown if s in by_id}
    missing = []
    for i in sorted(cited):
        v = by_id.get(i)
        if v is None or v.get("kind") not in FACET_KINDS or (v.get("facet") or {}).get("status") != "stated":
            continue
        if i not in shown and key_of_value(v) not in listed:
            missing.append((i, v.get("value_text"), (v.get("facet") or {}).get("written")))
    assert not missing, ("a cited reduction or coefficient with a stated component is not in the components table", missing,
                         sorted(shown))


def no_missing_component(t: Turn14) -> None:
    """No row says the component is missing for a component the document states: a row of a value the manifest gives a stated
    component has it (and its words), and no row of these documents is shown with ``MSG_NO_FACET``."""
    by_id = {v["id"]: v for v in t.values}
    bad = []
    for r in t.rows:
        if r["facet"] == timeline.MSG_NO_FACET:
            bad.append((r["sid"], r["facet"]))
            continue
        v = by_id.get(r["sid"])
        d = expected_datum(t, v) if v else None
        if d and d["facet"].get("status") == "stated":
            component = (r.get("state") or {}).get("component") or {}
            if component.get("status") != "stated" or plain(component.get("written")) != plain(d["facet"]["written"]):
                bad.append((r["sid"], r["facet"], component.get("status")))
    assert not bad, ("a row does not give the component the document states", bad)


def no_false_marks(t: Turn14) -> None:
    """The adjustments of these documents are parallel components or one adjustment written twice: no conflict, no undecided."""
    marked = [(r["sid"], r["marks"]) for r in t.rows if {"conflict", "undecided"} & set(r["marks"])
              or r.get("conflict_with") or r.get("undecided_with")]
    assert not marked, ("a conflict or undecided mark between parallel adjustments", marked)
    assert "sources_conflict" not in [g.get("reason") for g in t.answer.get("gaps") or []], "a sources_conflict reason"
    assert not [r for r in t.answer.get("requested") or [] if r.get("status") == "sources_conflict"], "a conflict claim was kept"


def marks_name_rows_on_screen(t: Turn14) -> None:
    """A mark names a row on screen; a counterpart that is not on screen is said not to be in the answer (title and location)."""
    screen = on_screen(t) | {r["sid"] for r in t.rows}
    bad = []
    for r in t.rows:
        for k in ("undecided_with", "conflict_with"):
            bad += [(r["sid"], k, x) for x in r.get(k) or [] if x not in screen]
        for x in r.get("not_in_answer_with") or []:
            if x.get("sid") in screen or not (x.get("title") and x.get("location")):
                bad.append((r["sid"], "not_in_answer_with", x.get("sid")))
    assert not bad, ("a mark names a value that is not a row on screen", bad)


def no_grounded_removed(t: Turn14, grounded: set[str], derived: set[str] = frozenset()) -> None:
    """No decision names as the unstated number of a claim one the sources state (the grounded numbers) or one that follows from
    them by the reduction formula (``derived``)."""
    bad = []
    for d in t.decisions:
        if d["action"] not in GONE or d.get("check") != "unstated_number":
            continue
        failing = numbers((d.get("reason") or "").split(":", 1)[-1])
        if failing and failing <= (derived | grounded):
            bad.append({"action": d["action"], "failing": sorted(failing), "text": (d.get("text") or "")[:160]})
    assert not bad, ("a grounded or correctly derived number was cut or removed", bad)


def both_in_answer(t: Turn14, *amounts: str) -> None:
    present = numbers(t.markdown)
    missing = [a for a in amounts if a not in present]
    assert not missing, (f"{missing} are not in the answer", t.markdown[:400])


def info(rec: dict, t: Turn14) -> None:
    """Recorded, not gated: what the model registered and what was lost."""
    rec["info"].update({
        "registered": [(v.get("id"), v.get("kind"), v.get("value_text"), (v.get("facet") or {}).get("status")) for v in t.values],
        "decisions": [(d["action"], d.get("check"), (d.get("reason") or "")[:80]) for d in t.decisions],
        "derived": [c["display"] for c in t.record()["computations"] if c["result_kind"] == "derived"],
        "tool_calls": [c["name"] for c in t.tools], "removed": t.answer.get("verification", {}).get("removed"),
        "edited": t.answer.get("verification", {}).get("edited"),
        "partial": t.answer.get("verification", {}).get("partial")})


COMMON = ("no clarification, intent property", "every coefficient or rate registered carries the state the document gives it",
          "the body and the components table agree", "every cited reduction or coefficient with a stated component is in the table",
          "no row says the component is missing for one the document states",
          "no conflict or undecided mark between parallel adjustments", "every mark names a row on screen",
          "no grounded number removed")


def common_checks(rec: dict, t: Turn14, grounded: set[str], derived: set[str] = frozenset()) -> None:
    check(rec, COMMON[0], lambda: answered(t))
    check(rec, COMMON[1], lambda: facets_expected(t))
    check(rec, COMMON[2], lambda: body_and_table_agree(t))
    check(rec, COMMON[3], lambda: cited_listed(t))
    check(rec, COMMON[4], lambda: no_missing_component(t))
    check(rec, COMMON[5], lambda: no_false_marks(t))
    check(rec, COMMON[6], lambda: marks_name_rows_on_screen(t))
    check(rec, COMMON[7], lambda: no_grounded_removed(t, grounded, derived))


# =========================================================================================================================
# S1: the reported shape (AE1)
# =========================================================================================================================

S1_Q = "מהם מקדמי ההפחתה שהובאו בחשבון בנכס ברחוב הברוש 3, ולאיזה שיקול כל אחד מהם?"
S1_NUMBERS = {"20", "10", "90"}


def test_s1_two_reductions_the_second_explained_two_sentences_earlier_agree_in_the_body_and_the_table_three_times(office, monkeypatch):
    ingest(office, monkeypatch, ("reported",))

    def body(o, c, cid, rec):
        t = ask(o, c, cid, S1_Q, rec)
        info(rec, t)
        common_checks(rec, t, S1_NUMBERS)
        check(rec, "both reductions are in the answer", lambda: both_in_answer(t, "20", "10"))

    run_sequence("S1", (*COMMON, "both reductions are in the answer"), office, body)


# =========================================================================================================================
# S2: a table coefficient and its text reduction (AE6)
# =========================================================================================================================

S2_Q = "מהו מקדם ההפחתה בשל שיתוף במקרקעין בנכס ברחוב התמר 11, ובכמה אחוזים הוא מפחית את השווי לפי הכתוב בשומה?"
S2_NUMBERS = {"0.9", "1.1", "10", "310", "90"}
S2_DERIVED = {"0.1"}  # 1 - 0.9


def one_adjustment_row(t: Turn14) -> None:
    """The coefficient 0.9 and the reduction of 10% for the same consideration, when the answer cites both, are one row: the
    coefficient's, with the reduction in it (or the reduction's own row when the coefficient is not cited), never two rows."""
    by_id = {v["id"]: v for v in t.values}
    cited = set(re.findall(r"\[(V\d+)\]", t.markdown))
    ids: dict[str, set[str]] = {"adjust_match.coef": set(), "adjust_match.reduction": set()}
    for i in cited:
        d = expected_datum(t, by_id[i]) if i in by_id else None
        if d and d["id"] in ids:
            ids[d["id"]].add(i)
    if not all(ids.values()):
        return  # the answer cites one of the two forms only: nothing to join
    rows = {r["sid"]: r for r in t.table}
    joined = [r for r in rows.values() if r["sid"] in ids["adjust_match.coef"]
              and (r.get("reduction") or {}).get("sid") in ids["adjust_match.reduction"]]
    assert len(joined) == 1, ("the coefficient and its reduction are not one row", sorted(rows), [r.get("reduction") for r in rows.values()])
    assert not (set(rows) & ids["adjust_match.reduction"]), ("the reduction is also a row of its own", sorted(rows))


def test_s2_a_table_coefficient_and_its_text_reduction_are_one_adjustment_in_the_body_and_the_table_three_times(office, monkeypatch):
    ingest(office, monkeypatch, ("adjust_match",))

    def body(o, c, cid, rec):
        t = ask(o, c, cid, S2_Q, rec)
        info(rec, t)
        common_checks(rec, t, S2_NUMBERS, S2_DERIVED)
        check(rec, "the coefficient 0.9 is in the answer", lambda: both_in_answer(t, "0.9"))
        check(rec, "the coefficient and its reduction are one row", lambda: one_adjustment_row(t))

    run_sequence("S2", (*COMMON, "the coefficient 0.9 is in the answer", "the coefficient and its reduction are one row"), office, body)


# =========================================================================================================================
# S3: three equal reductions (AE5)
# =========================================================================================================================

S3_Q = "מהן ההפחתות שהובאו בחשבון בנכס ברחוב הדס 5, ולאיזה שיקול כל אחת?"
S3_NUMBERS = {"10", "90"}


def three_rows(t: Turn14) -> None:
    """Of the three considerations the answer gives a reduction of 10% (it cites a registered value whose component the document
    states), each is its own row, with its component, once: the table never merges equal reductions of different adjustments."""
    by_id = {v["id"]: v for v in t.values}
    cited = {i for i in re.findall(r"\[(V\d+)\]", t.markdown) if i in by_id and (by_id[i].get("facet") or {}).get("status") == "stated"}
    wanted = {plain(by_id[i]["facet"]["written"]) for i in cited}
    if len(wanted) < 2 or not t.table:
        return
    facets = [plain(r["facet"]) for r in t.table]
    missing = sorted(w for w in wanted if w not in facets)
    assert not missing, ("a consideration the answer gives has no row of its own", missing, facets)
    assert len(facets) == len(set(facets)), ("a consideration is a row twice", facets)


def test_s3_three_equal_reductions_for_three_considerations_are_three_rows_with_no_conflict_three_times(office, monkeypatch):
    ingest(office, monkeypatch, ("three_equal",))

    def body(o, c, cid, rec):
        t = ask(o, c, cid, S3_Q, rec)
        info(rec, t)
        common_checks(rec, t, S3_NUMBERS)
        check(rec, "three considerations are three rows", lambda: three_rows(t))

    run_sequence("S3", (*COMMON, "three considerations are three rows"), office, body)


# --- the summary -------------------------------------------------------------------------------------------------------

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
    sequences: dict[str, list[dict]] = {}
    for r in RESULTS:
        sequences.setdefault(r["sequence"], []).append(
            {"attempt": r["attempt"], "outcome": r["outcome"], "checks": {k: v["outcome"] for k, v in r["checks"].items()},
             "failures": {k: v["failure"] for k, v in r["checks"].items() if v["outcome"] == "fail"}, "info": r.get("info")})
    return {"model": "gpt-6-luna (settings)", "turns": len(turns), "attempts": len(RESULTS),
            "sequences": {k: {"attempts": v, "passed": len(v) == ATTEMPTS and all(a["outcome"] == "pass" for a in v),
                              "passed_runs": f"{sum(a['outcome'] == 'pass' for a in v)}/{len(v)}"}
                          for k, v in sequences.items()},
            "usage_purposes": purposes, "usage_statuses": statuses, "cost_usd": round(sum(costs), 4) if costs else None,
            "model_latency_s": round(sum(t["usage"]["model_latency_ms"] for t in turns) / 1000, 1),
            "wall_s": round(time.perf_counter() - STARTED, 1)}


def teardown_module(module) -> None:  # noqa: ARG001
    if RESULTS:
        flush()
        SUMMARY_FILE.write_text(json.dumps(build_summary(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nreal_model results: {os.path.relpath(RESULTS_FILE, ROOT)}\nsummary: {os.path.relpath(SUMMARY_FILE, ROOT)}")
