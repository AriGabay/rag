"""Round 12 acceptance with the real model (U12: R19, R20, R22; AE1-AE7): time ranking, property identity, coverage wording,
attribute provenance, history dates and conflicts on the synthetic round-12 fixtures (``tests/fixtures/round12``, plus
files of several appraisal units and appraisals built with ``tests.support.unit_files`` - a DOCX is one unit) - every
name, address and amount invented - ingested into a synthetic office of the ``rag_test`` database. Questions go through the
chat API to the configured model and judge (``gpt-6-luna`` with the repository's settings), whose key comes from the
repository's ``.env`` and is never printed.

Seven sequences, each in a **new conversation, exactly three times**; every attempt is recorded, a failure in any attempt is
reported (a later pass never hides it), and a sequence passes only when all three attempts pass:

- S1 two cities (AE1, R1, R2): the same street and number in two synthetic cities and once with no city: the answer is the
  asked city's value; no value of the other city or of the cityless appraisal (an unlinked candidate) is cited;
- S2 a property over two parcels (AE2, R3, R4): the latest appraisal first, the earlier one with its own date and source, and
  the separate lot's value never cited; the coverage record holds exactly the two appraisals, with the parcel reason;
- S3 a compendium of six appraisal units of one property (R10): the latest unit first, then "ומה היה השווי ב-2022?": the
  mode ``explicit`` and the 2022 unit's value with its date;
- S4 the latest appraisal of fifteen, beyond the relevance window (AE3, R20): the 2026 appraisal's value is the first value,
  the first one cited and the first one read;
- S5 an attribute the source does not state (AE5, R6, R7): a rent with no period keeps its number and no period is given
  as the source's;
- S6 history (AE6, R16, R17): a history table's prior values and a prior-appraisal section's keep their own dates, never the
  report's valuation date or the unit's;
- S7 partial coverage and a conflict (AE4, AE7, R12, R18): an undated appraisal of the property keeps the check partial, the
  note names it and says "הנתון המאוחר ביותר במקורות שנבדקו"; an undated datum of another amount is never presented as dated.

Everything is asserted by meaning, sources and completeness - the numbers and their order, the documents the answer cites, the
dates given to each value, the payload's coverage record and note - never by exact wording. Per turn the whole record (the
markdown, ``answer.time`` with its coverage record, the sources and values with their documents and dates, every tool call's
arguments - the unit, the document ids and the scope - and the documents and dates of the sources it returned, the diagnostics'
time decision, the model calls, tokens, cost and latency and the wall time) is written to
``real_documents/eval/round12/real-model/`` (gitignored and private: answers are kept for diagnosis), with a summary file
next to it listing every attempt of every sequence.

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

from app.chat import tools as T
from app.chat.timeframe import LATEST_CHECKED
from app.chat.verify import _calls_latest
from app.config import get_settings
from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_property_scope import DOCS12, FIXTURES, compendium
from tests.integration.test_round10_real_model import new_conversation, upload, usage_of
from tests.integration.test_round10_real_model import real_key as real_key  # noqa: F401 - the key fixture
from tests.integration.test_round11_real_model import MONEY, dates_in
from tests.integration.test_round12_sequences import PROPERTY_X, amount_of, chain, unit_appraisal

pytestmark = [pytest.mark.real_model, pytest.mark.db]

ROOT = Path(__file__).resolve().parents[3]
RESULTS_DIR = ROOT / "real_documents" / "eval" / "round12" / "real-model"
STAMP = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
RESULTS_FILE = RESULTS_DIR / f"results-{STAMP}.json"
SUMMARY_FILE = RESULTS_DIR / f"summary-{STAMP}.json"
RESULTS: list[dict] = []
ATTEMPTS = 3  # exactly three, each in a new conversation
STARTED = time.perf_counter()

ADMIN = "admin-r12@example.test"
TOOL_LOG: list[dict] = []  # every tool call of the run, in order (a turn reads its own slice)


# --- environment -------------------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def record_tools(monkeypatch):
    """Every tool call the engine makes: its name, arguments and the sources its output returned (document, title, location,
    date line, kind) - the unit, the documents and the scope each search read."""
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
        coverage = ws.time_coverage if isinstance(ws.time_coverage, dict) else {}
        TOOL_LOG.append({"name": name, "args": args, "hits": hits, "scope": coverage.get("scope"), "via": coverage.get("via"),
                         "property_wide": T.MSG_PROPERTY_SEARCHED in output, "output_chars": len(output)})
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
    """The round-12 DOCX fixtures ``keys`` through the worker (single appraisals)."""
    for key in keys:
        d = DOCS12[key]
        office.ids[key] = upload(office, FIXTURES / d["file"], d["source_kind"], monkeypatch)
    office.by_id = {v: k for k, v in office.ids.items()}


def register(office, key: str, doc: str) -> str:
    office.ids[key] = doc
    office.by_id[doc] = key
    return doc


# --- turns and records -------------------------------------------------------------------------------------------------

def flush() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE.write_text(json.dumps({"model": "gpt-6-luna (settings)", "attempts": RESULTS}, ensure_ascii=False, indent=2,
                                       default=str), encoding="utf-8")


CITED = re.compile(r"\[([SV]\d+)\]")


class Turn:
    """A finished turn: the message, the settled intent and the time decision from its diagnostics, the answer's time block,
    the turn's tool calls."""

    def __init__(self, office, c, m: dict, question: str, wall: float, switch: bool | None, tools: list[dict]) -> None:
        self.office, self.m, self.question, self.switch, self.tools = office, m, question, switch, tools
        self.answer = m.get("answer") or {}
        d = c.get(f"/api/chat/messages/{m['id']}/diagnostics")
        body = d.json() if d.status_code == 200 else {}
        self.resolution = body.get("resolution") or {}
        self.removed = [{k: x.get(k) for k in ("kind", "check", "action", "reason")} for x in body.get("removed") or []]
        self.usage = usage_of(m, wall)

    @property
    def status(self) -> str | None:
        return self.answer.get("status")

    @property
    def intent(self) -> str | None:
        return (self.resolution.get("intent") or {}).get("value")

    @property
    def mode(self) -> dict:
        return self.resolution.get("time") or {}

    @property
    def markdown(self) -> str:
        return self.answer.get("markdown") or ""

    @property
    def time(self) -> dict:
        return self.answer.get("time") or {}

    @property
    def coverage(self) -> dict:
        return self.time.get("coverage") or {}

    @property
    def values(self) -> list[dict]:
        return self.answer.get("values") or []

    @property
    def sources(self) -> list[dict]:
        return self.answer.get("sources") or []

    def key_of(self, doc: str | None) -> str:
        return self.office.by_id.get(doc, doc or "?")

    def money(self) -> list[str]:
        return MONEY.findall(self.markdown)

    def cited_keys(self) -> list[str]:
        """The fixture keys of the documents the markdown cites ([S#] passages and [V#] values), in order of first citation."""
        docs = {s["id"]: s.get("document_id") for s in self.sources} | {v["id"]: v.get("document_id") for v in self.values}
        out: list[str] = []
        for ref in CITED.findall(self.markdown):
            key = self.key_of(docs.get(ref))
            if key not in out:
                out.append(key)
        return out

    def clauses(self, amount: str) -> list[str]:
        """The sentences or lines of the markdown that give ``amount``."""
        return [c for c in re.split(r"(?<=[.!?])\s+|\n", self.markdown) if amount in c]

    def searches(self) -> list[dict]:
        return [c for c in self.tools if c["name"] == "search"]

    def record(self) -> dict:
        return {
            "question": self.question, "switch_set": self.switch, "message_id": self.m.get("id"),
            "message_status": self.m.get("status"), "answer_status": self.status, "intent": self.intent,
            "time_preference": self.m.get("time_preference"), "resolution_time": self.mode, "answer_time": self.time,
            "money_in_markdown": self.money(), "cited_documents": self.cited_keys(),
            "tool_calls": [c | {"hits": [h | {"key": self.key_of(h.get("document_id"))} for h in c["hits"]]} for c in self.tools],
            "sources": [{"id": s.get("id"), "kind": s.get("source_kind"), "title": s.get("title"), "location": s.get("location"),
                         "date_line": s.get("date_line"), "document": self.key_of(s.get("document_id"))} for s in self.sources],
            "values": [{"id": v.get("id"), "value_text": v.get("value_text"), "kind": v.get("kind"), "period": v.get("period"),
                        "vat": v.get("vat"), "as_of": v.get("as_of"), "provenance": v.get("provenance"),
                        "document": self.key_of(v.get("document_id")), "location": v.get("location")} for v in self.values],
            "verification": self.answer.get("verification"), "decisions": self.resolution.get("decisions"),
            "removed": self.removed, "clarification": self.answer.get("clarification"), "usage": self.usage,
            "markdown": self.markdown,
        }


def ask(office, c, cid: str, question: str, rec: dict, switch: bool | None = None) -> Turn:
    """One turn through the API; recorded (and flushed) before anything is asserted about it."""
    started = time.perf_counter()
    first_tool = len(TOOL_LOG)
    body = {"content": question} | ({} if switch is None else {"prefer_recent": switch})
    r = c.post(f"/api/chat/conversations/{cid}/messages", json=body)
    assert r.status_code == 200, r.text
    m = c.get(f"/api/chat/messages/{r.json()['assistant']['id']}").json()
    t = Turn(office, c, m, question, time.perf_counter() - started, switch, list(TOOL_LOG[first_tool:]))
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
        rec = {"sequence": sequence, "attempt": n, "turns": [], "checks": {}, "outcome": None}
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

def answered(t: Turn) -> None:
    assert t.status != "clarification" and not t.answer.get("clarification"), ("a clarification was asked", t.markdown[:300])
    assert t.intent == "property", (t.intent, t.resolution.get("intent"))


def first_value(t: Turn, amount: str) -> None:
    money = t.money()
    assert money and money[0] == amount, (f"the first value is not {amount}", money[:6], t.markdown[:400])


def never_cited(t: Turn, amounts: tuple[str, ...], keys: tuple[str, ...]) -> None:
    """No value of an unlinked candidate or another property is given, and none of its documents is cited."""
    given = [a for a in amounts if a in t.money()]
    assert not given, ("a value of an unlinked appraisal is in the answer", given, t.markdown[:400])
    cited = [k for k in t.cited_keys() if k in keys]
    assert not cited, ("an unlinked appraisal is cited", cited)


def with_own_date(t: Turn, amount: str, iso: str, source_key: str) -> None:
    """The value is given with its own date and from its own appraisal."""
    clauses = t.clauses(amount)
    assert clauses, (f"{amount} is not in the answer", t.markdown[:400])
    assert any(dates_in(c, iso) for c in clauses) or dates_in(t.markdown, iso), (f"{amount} is given without its date {iso}", clauses)
    assert source_key in t.cited_keys(), (f"{amount} is not cited from {source_key}", t.cited_keys())


def cites(t: Turn, key: str) -> None:
    assert key in t.cited_keys(), (f"{key} is not cited", t.cited_keys())


def cited_first(t: Turn, key: str) -> None:
    assert t.cited_keys()[:1] == [key], (f"{key} is not the first cited", t.cited_keys())


DENIES = re.compile(r"אי\s+אפשר|לא\s+ניתן|מועדו\s+לא|לא\s+ידוע|אינו\s+ידוע")  # a sentence that says the date is not known
STATES_NOTHING = re.compile(r"לא\s+צוינ|לא\s+מצוינ|אינ\w*\s+מצוינ|אינ\w*\s+מצי\w*|לא\s+נאמר|ללא\s+ציון")
QUALIFIED = "במקורות שנבדקו"


def partial_wording(t: Turn) -> None:
    """Partial coverage: the server's note says the datum is the latest in the sources checked, and no sentence of the answer
    that gives a cited datum calls it the latest without saying so (the server's own gap lines are not the model's claims)."""
    cov = t.coverage
    assert cov and not cov.get("complete"), ("the coverage is not partial", {k: cov.get(k) for k in ("scope", "complete", "blocking")})
    assert LATEST_CHECKED in (cov.get("note") or ""), ("the note lacks the phrase", cov.get("note"))
    assert LATEST_CHECKED in (t.time.get("text") or ""), "the copy text lacks the note"
    for sentence in re.split(r"(?<=[.!?])\s+|\n", t.markdown):
        if CITED.search(sentence) and _calls_latest(sentence) and not DENIES.search(sentence):
            assert QUALIFIED in sentence, ("an unqualified 'latest' under partial coverage", sentence)


def coverage_units(t: Turn) -> set[str]:
    return {t.key_of(u["document_id"]) for u in t.coverage.get("units") or []}


# =========================================================================================================================
# S1: two cities, one street and number (AE1)
# =========================================================================================================================

S1_Q = "מה שווי הנכס ברחוב הדולב 7 בעיר-סינתטית?"


def test_s1_two_cities_the_asked_citys_value_and_no_value_of_the_other_city_or_the_cityless_appraisal_three_times(office, monkeypatch):
    ingest(office, monkeypatch, ("cities_a", "cities_b", "cities_nocity"))

    def body(o, c, cid, rec):
        t = ask(o, c, cid, S1_Q, rec)
        check(rec, "no clarification, intent property", lambda: answered(t))
        check(rec, "the first value is the asked city's (2,500,000)", lambda: first_value(t, "2,500,000"))
        check(rec, "no value of the other city or of the cityless appraisal, and neither is cited",
              lambda: never_cited(t, ("1,900,000", "2,700,000"), ("cities_b", "cities_nocity")))
        check(rec, "the asked city's appraisal is cited", lambda: cites(t, "cities_a"))

        def coverage():
            cov = t.coverage
            assert cov, "no coverage record in a recent-mode property answer"
            if cov.get("scope") == "property":  # the appraisals proven to be the property's: the asked city's only
                assert coverage_units(t) == {"cities_a"}, coverage_units(t)
            else:  # a search window: the record says so, is never complete, and the answer is the latest of what was checked
                partial_wording(t)

        check(rec, "the coverage is the property's own appraisals, or an honest partial window", coverage)

    run_sequence("S1", ("no clarification, intent property", "the first value is the asked city's (2,500,000)",
                        "no value of the other city or of the cityless appraisal, and neither is cited",
                        "the asked city's appraisal is cited",
                        "the coverage is the property's own appraisals, or an honest partial window"), office, body)


# =========================================================================================================================
# S2: a property over two parcels (AE2)
# =========================================================================================================================

S2_Q = "מה שווי הנכס ברחוב הדס 5, ומה היו ההערכות הקודמות שלו?"


def test_s2_the_two_parcel_property_latest_first_the_earlier_with_its_date_and_never_the_separate_lot_three_times(office, monkeypatch):
    ingest(office, monkeypatch, ("parcels_ab", "parcels_b", "parcels_c"))

    def body(o, c, cid, rec):
        t = ask(o, c, cid, S2_Q, rec)
        check(rec, "no clarification, intent property", lambda: answered(t))
        check(rec, "the first value is the latest appraisal's (3,650,000)", lambda: first_value(t, "3,650,000"))
        check(rec, "the earlier appraisal's value comes with its own date and source",
              lambda: with_own_date(t, "3,200,000", "2023-06-30", "parcels_ab"))
        check(rec, "the latest appraisal is cited first", lambda: cited_first(t, "parcels_b"))
        check(rec, "the separate lot's value and document never appear", lambda: never_cited(t, ("2,100,000",), ("parcels_c",)))

        def coverage():
            cov = t.coverage
            assert cov, "no coverage record"
            assert "parcels_c" not in coverage_units(t) and not (cov.get("candidates") or {}).get("count"), cov.get("candidates")
            if cov.get("scope") == "property":
                assert coverage_units(t) == {"parcels_ab", "parcels_b"}, coverage_units(t)
                assert (cov.get("property") or {}).get("reasons") == ["parcel 8401/40"], cov.get("property")

        check(rec, "the coverage holds the two appraisals with the parcel reason and never the lot", coverage)

    run_sequence("S2", ("no clarification, intent property", "the first value is the latest appraisal's (3,650,000)",
                        "the earlier appraisal's value comes with its own date and source", "the latest appraisal is cited first",
                        "the separate lot's value and document never appear",
                        "the coverage holds the two appraisals with the parcel reason and never the lot"), office, body)


# =========================================================================================================================
# S3: a compendium, then a year (R10)
# =========================================================================================================================

S3_Q1, S3_Q2 = "מה שווי הנכס ברחוב הגפן 15?", "ומה היה השווי ב-2022?"
GAFEN = {"2025-06-30": "3,100,000", "2024-06-30": "2,900,000", "2023-06-30": "2,700,000", "2022-06-30": "2,500,000",
         "2021-06-30": "2,300,000", "2020-06-30": "2,100,000"}


def test_s3_a_compendium_latest_unit_first_then_the_2022_unit_with_its_date_three_times(office, monkeypatch):
    register(office, "compendium_one", compendium(office, "compendium_one")[0])

    def body(o, c, cid, rec):
        t1 = ask(o, c, cid, S3_Q1, rec)
        check(rec, "setup: answered as a property question", lambda: answered(t1))
        check(rec, "the first value is the latest unit's (3,100,000) with its date",
              lambda: (first_value(t1, "3,100,000"), with_own_date(t1, "3,100,000", "2025-06-30", "compendium_one")))
        t2 = ask(o, c, cid, S3_Q2, rec)

        def mode():
            req = t2.mode.get("requested") or {}
            assert (t2.mode.get("mode"), t2.mode.get("source")) == ("explicit", "question"), t2.mode
            assert req.get("start") == req.get("end") == "2022", req

        check(rec, "mode explicit for 2022", mode)
        check(rec, "the first value is the 2022 unit's (2,500,000) with its date",
              lambda: (first_value(t2, "2,500,000"), with_own_date(t2, "2,500,000", "2022-06-30", "compendium_one")))

        def reads_the_year():  # the value is the 2022 unit's own, and any search ranked that unit first
            mine = [v for v in t2.values if v.get("value_text") == "2,500,000"]  # registered or only cited as a passage
            assert all((v.get("as_of") or {}).get("date") == "2022-06-30" for v in mine), (
                "the registered value is not dated by the 2022 unit", [(v.get("value_text"), v.get("as_of")) for v in t2.values])
            cites(t2, "compendium_one")
            # a search that names a unit reads that unit alone (the model's own choice): only the others rank the units
            hits = [h for c in t2.searches() if not c["args"].get("unit") for h in c["hits"]
                    if t2.key_of(h.get("document_id")) == "compendium_one"]
            if hits and len(t2.searches()) == 1:
                assert hits[0].get("date") == "למועד הקובע 30/06/2022", ("the 2022 unit is not read first", hits[0].get("date"))

        check(rec, "the value is the 2022 unit's, dated 30/06/2022, and a search reads that unit first", reads_the_year)

    run_sequence("S3", ("setup: answered as a property question", "the first value is the latest unit's (3,100,000) with its date",
                        "mode explicit for 2022", "the first value is the 2022 unit's (2,500,000) with its date",
                        "the value is the 2022 unit's, dated 30/06/2022, and a search reads that unit first"), office, body)


# =========================================================================================================================
# S4: the latest of fifteen, beyond the relevance window (AE3, R20)
# =========================================================================================================================

S4_Q = "מה שווי הנכס ברחוב הערבה 2?"
WINDOW = tuple(f"window_{n:02d}" for n in range(1, 15)) + ("window_latest",)


def test_s4_the_latest_of_fifteen_appraisals_is_the_first_value_the_first_cited_and_the_first_read_three_times(office, monkeypatch):
    ingest(office, monkeypatch, WINDOW)

    def body(o, c, cid, rec):
        t = ask(o, c, cid, S4_Q, rec)
        check(rec, "no clarification, intent property", lambda: answered(t))
        check(rec, "the first value is the 2026 appraisal's (4,350,000)", lambda: first_value(t, "4,350,000"))
        check(rec, "the 2026 appraisal is cited first", lambda: cited_first(t, "window_latest"))

        def read_first():
            hits = [h for c in t.searches() for h in c["hits"]]
            assert hits and t.key_of(hits[0].get("document_id")) == "window_latest", (
                "the first hit read is not the latest appraisal's", [t.key_of(h.get("document_id")) for h in hits[:4]])

        check(rec, "the first search returned the 2026 appraisal first", read_first)

        def covers():
            cov = t.coverage
            assert cov and cov.get("scope") == "property", ("the property was not identified", cov.get("scope"))
            assert len(cov["units"]) == 15 and t.key_of(cov["units"][0]["document_id"]) == "window_latest", "inventory not newest first"
            assert cov.get("complete"), ("the check is not complete for 'latest'", cov.get("blocking"))

        check(rec, "the inventory lists the fifteen appraisals newest first and is complete for the latest", covers)

    run_sequence("S4", ("no clarification, intent property", "the first value is the 2026 appraisal's (4,350,000)",
                        "the 2026 appraisal is cited first", "the first search returned the 2026 appraisal first",
                        "the inventory lists the fifteen appraisals newest first and is complete for the latest"), office, body)


# =========================================================================================================================
# S5: an attribute the source does not state (AE5)
# =========================================================================================================================

S5_Q = "מה שכר הדירה בנכס ברחוב השקד 2?"
PERIOD_WORDS = re.compile(r"לחודש|בחודש|חודשי|לשנה|בשנה|שנתי|ל-?חודש|פר\s+חודש|per\s+month|monthly", re.I)


def test_s5_a_rent_with_no_stated_period_keeps_its_number_and_no_period_is_given_as_the_sources_three_times(office, monkeypatch):
    ingest(office, monkeypatch, ("rent_no_period",))

    def body(o, c, cid, rec):
        t = ask(o, c, cid, S5_Q, rec)
        check(rec, "no clarification, intent property", lambda: answered(t))
        check(rec, "the source-backed number stays (5,000)", lambda: first_value(t, "5,000"))

        def no_invented_period():
            for clause in t.clauses("5,000"):
                found = PERIOD_WORDS.findall(clause)
                assert not found or STATES_NOTHING.search(clause), ("a period the source does not state", clause)
            for v in t.values:
                if v.get("value_text") == "5,000":
                    assert (v.get("provenance") or {}).get("period") != "source", ("a model-asserted period registered as the source's", v)

        check(rec, "no period is given for the rent as the source's", no_invented_period)
        check(rec, "the rent is cited from its appraisal", lambda: cites(t, "rent_no_period"))

    run_sequence("S5", ("no clarification, intent property", "the source-backed number stays (5,000)",
                        "no period is given for the rent as the source's", "the rent is cited from its appraisal"), office, body)


# =========================================================================================================================
# S6: history keeps its own dates (AE6)
# =========================================================================================================================

S6_Q_TABLE = "מה היו ההערכות הקודמות של הנכס ברחוב הרימון 8, ובאילו תאריכים?"
S6_Q_SECTION = "מה הייתה ההערכה הקודמת של הנכס ברחוב הרימון 11, ובאיזה תאריך?"
S6_Q_TWO_DIGIT = "מה היו ההערכות הקודמות של הנכס ברחוב הרימון 10?"


def test_s6_prior_values_keep_their_own_dates_never_the_reports_or_the_units_valuation_date_three_times(office, monkeypatch):
    ingest(office, monkeypatch, ("dp_history_table", "dp_prior_section_4digit", "dp_prior_section_ae6"))

    def body(o, c, cid, rec):
        table = ask(o, c, cid, S6_Q_TABLE, rec)
        check(rec, "table: no clarification, intent property", lambda: answered(table))
        check(rec, "table: the prior values come with their own dates", lambda: (
            with_own_date(table, "2,050,000", "2021-12-31", "dp_history_table"),
            with_own_date(table, "2,300,000", "2023-12-31", "dp_history_table")))

        def table_not_report_date():
            for amount in ("2,050,000", "2,300,000"):
                for clause in table.clauses(amount):
                    assert not dates_in(clause, "2025-06-30") and not dates_in(clause, "2026-01-20"), (
                        "a prior value carries the unit's valuation date or the report date", clause)
            for v in table.values:
                assert (v.get("as_of") or {}).get("date") not in ("2025-06-30", "2026-01-20"), v

        check(rec, "table: never the valuation date 30/06/2025 or the report date", table_not_report_date)

        c2 = new_conversation(c)
        section = ask(o, c, c2, S6_Q_SECTION, rec)
        check(rec, "section: no clarification, intent property", lambda: answered(section))
        check(rec, "section: the prior value comes with its own date (31/12/2019)",
              lambda: with_own_date(section, "1,800,000", "2019-12-31", "dp_prior_section_4digit"))

        def section_not_unit_date():
            for clause in section.clauses("1,800,000"):
                assert not dates_in(clause, "2023-12-31"), ("the prior value carries the unit's valuation date", clause)
            for v in section.values:
                if v.get("value_text") == "1,800,000":
                    assert (v.get("as_of") or {}).get("date") != "2023-12-31", v

        check(rec, "section: never the unit's valuation date 31/12/2023", section_not_unit_date)

        c3 = new_conversation(c)
        two = ask(o, c, c3, S6_Q_TWO_DIGIT, rec)

        def two_digit_not_unit_date():  # the prior dates are written 31.12.19 / 31.12.21 (two-digit years)
            for amount in ("1,800,000", "2,000,000"):
                for clause in two.clauses(amount):
                    assert not dates_in(clause, "2023-12-31"), ("a prior value carries the unit's valuation date", clause)
            for v in two.values:
                assert (v.get("as_of") or {}).get("date") != "2023-12-31", v

        check(rec, "two-digit years: no prior value carries the unit's valuation date 31/12/2023", two_digit_not_unit_date)

    run_sequence("S6", ("table: no clarification, intent property", "table: the prior values come with their own dates",
                        "table: never the valuation date 30/06/2025 or the report date",
                        "section: no clarification, intent property",
                        "section: the prior value comes with its own date (31/12/2019)",
                        "section: never the unit's valuation date 31/12/2023",
                        "two-digit years: no prior value carries the unit's valuation date 31/12/2023"), office, body)


# =========================================================================================================================
# S7: partial coverage and a conflict (AE4, AE7)
# =========================================================================================================================

S7_Q = "מה השווי העדכני של הנכס ברחוב הגפן 15?"
UNDATED_AMOUNT = "2,000,000"


def test_s7_an_undated_appraisal_keeps_the_check_partial_the_note_names_it_and_its_amount_is_never_dated_three_times(office, monkeypatch):
    docs = chain(office, range(2022, 2025), "s7")
    undated = unit_appraisal(office, "s7-none", valuation=None, amount=UNDATED_AMOUNT, vat=True, **PROPERTY_X)
    for year, doc in docs.items():
        register(office, f"s7-{year}", doc)
    register(office, "s7-none", undated)
    latest, middle = amount_of(2024), amount_of(2023)

    def body(o, c, cid, rec):
        t = ask(o, c, cid, S7_Q, rec)
        check(rec, "no clarification, intent property", lambda: answered(t))
        check(rec, f"the first value is the latest dated appraisal's ({latest})", lambda: first_value(t, latest))
        check(rec, "the latest dated appraisal is cited first", lambda: (_ for _ in ()).throw(AssertionError(t.cited_keys()))
              if t.cited_keys()[:1] != ["s7-2024"] else None)

        def note_names_the_gap():
            partial_wording(t)
            cov = t.coverage
            assert "שומה סינתטית s7-none" in (cov.get("note") or ""), ("the note does not name the undated appraisal", cov.get("note"))
            assert "undated" in (cov.get("blocking") or []), cov.get("blocking")

        check(rec, "partial coverage: the phrase, and the note names the undated appraisal", note_names_the_gap)

        def earlier_with_dates():
            if middle in t.money():  # an earlier value the answer gives keeps its own date and source
                with_own_date(t, middle, "2023-06-30", "s7-2023")

        check(rec, "an earlier value in the answer has its own date and source", earlier_with_dates)

        def undated_not_dated():
            rows = [r for g in t.time.get("timeline") or [] for r in g["rows"]]
            for clause in t.clauses(UNDATED_AMOUNT):
                assert not any(dates_in(clause, f"{y}-06-30") for y in (2022, 2023, 2024)), ("the undated amount is given a date", clause)
            for r in rows:
                if r["value_text"] == UNDATED_AMOUNT:
                    assert r["date"] is None and "unknown_date" in r["marks"], r
            for v in t.values:
                if v.get("value_text") == UNDATED_AMOUNT:
                    assert (v.get("as_of") or {}).get("date") is None, v

        check(rec, "the undated amount is never given a date", undated_not_dated)

        def conflict_visible():  # AE7: a registered undated amount of another value conflicts with the dated ones, visibly
            rows = [r for g in t.time.get("timeline") or [] for r in g["rows"]]
            undated_rows = [r for r in rows if r["value_text"] == UNDATED_AMOUNT]
            assert not undated_rows or "conflict" in undated_rows[0]["marks"], undated_rows

        check(rec, "a registered undated datum of another amount is marked as a conflict", conflict_visible)

    run_sequence("S7", ("no clarification, intent property", f"the first value is the latest dated appraisal's ({latest})",
                        "the latest dated appraisal is cited first", "partial coverage: the phrase, and the note names the undated appraisal",
                        "an earlier value in the answer has its own date and source", "the undated amount is never given a date",
                        "a registered undated datum of another amount is marked as a conflict"), office, body)


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
             "failures": {k: v["failure"] for k, v in r["checks"].items() if v["outcome"] == "fail"}})
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
