"""Round 11 acceptance with the real model (U12: R3, R4, R5, R9, R11, R13, R24; AE1, AE2, AE3, AE4): the recency preference on
the synthetic round-11 fixtures (``tests/fixtures/round11``: three appraisals of one invented property, הצאלון 7, with valuation
dates 2022, 2024 and 2025 and later report dates, an appraisal of it with no valuation date, an appraisal of a neighbour, two
rent opinions that differ in period and VAT; the two synthetic standard editions are the round-10 files) - every name, address
and amount invented - ingested through the worker into a synthetic office of the ``rag_test`` database. Questions go through the
chat API to the configured model and judge (``gpt-6-luna`` with the repository's settings), whose key comes from the
repository's ``.env`` and is never printed.

Five sequences, each in a **new conversation, exactly three times**; every attempt is recorded, a failure in any attempt is
reported (a later pass never hides it), and a sequence passes only when all three attempts pass:

- S1 recency default (AE1): "מה שווי הנכס ברחוב הצאלון 7?" with the switch at its default: the first money value is the 2025
  valuation, the older two are in the answer, and ``answer.time`` holds a group of the three valuations newest first with
  each row's source the document of that valuation (the undated appraisal, when present, last); no clarification, intent property;
- S2 switch off: the same question after the conversation is switched off: the mode is ``none``, the dates are still in the
  markdown, nothing is asserted about order;
- S3 explicit follow-up (AE3): the rent question, then "ומה היה השווי ב-2022?": the second turn is the mode ``explicit``, its
  first money value is the 2022 valuation with its date, and the search scope holds the 2022 appraisal;
- S4 the valuation date, not the report date (AE2): a question about one named appraisal: the value is dated with its valuation
  date and never with the date the report was written;
- S5 non-comparable data (AE4): the rent question: no timeline group mixes a monthly rent with VAT, a yearly rent without VAT and
  the monthly series without VAT; that series is in its own group, newest first; a forecast is marked and never first.

S6 (the round-10 regressions, R23) is not repeated here: it would need the round-10 fixtures in the same office and the flows are
asserted by ``test_round10_real_model.py``, which the round re-runs as its own file.

Everything is asserted by behaviour - the mode, the numbers and their order, the timeline rows, the sources in the search scope -
never by wording. Per turn, the whole record (markdown, ``answer.time``, the search scope with each source's title and date line,
the searches, the diagnostics' time decision, the model calls, tokens, cost and latency and the wall time) is written to
``real_documents/eval/round11/real-model/`` (gitignored and private: answers are kept for diagnosis), with a summary file next to
it listing every attempt of every sequence.

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
from tests.integration.test_round10_real_model import new_conversation, upload, usage_of
from tests.integration.test_round10_real_model import real_key as real_key  # noqa: F401 - the key fixture

pytestmark = [pytest.mark.real_model, pytest.mark.db]

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANIFEST = json.loads((FIXTURES / "round11" / "manifest.json").read_text(encoding="utf-8"))["documents"]
RESULTS_DIR = ROOT / "real_documents" / "eval" / "round11" / "real-model"
STAMP = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
RESULTS_FILE = RESULTS_DIR / f"results-{STAMP}.json"
SUMMARY_FILE = RESULTS_DIR / f"summary-{STAMP}.json"
RESULTS: list[dict] = []
ATTEMPTS = 3  # exactly three, each in a new conversation
STARTED = time.perf_counter()

ADMIN = "admin-r11@example.test"
FIXTURE_KEYS = tuple(MANIFEST)  # the seven appraisals and the two standard editions

VALUE_Q = "מה שווי הנכס ברחוב הצאלון 7?"
RENT_Q = "מה דמי השכירות הראויים ברחוב הצאלון 7?"
YEAR_Q = "ומה היה השווי ב-2022?"
NAMED_Q = "מה המועד הקובע ומה השווי בשומה רחוב הצאלון 7 עיר-סינתטית ב?"

MONEY = re.compile(r"\d{1,3}(?:,\d{3})+")
# the dated values of the property by their valuation date: iso -> (manifest key, amount text)
VALUATIONS = {"2025-01-20": ("hatzalon7_c", "2,410,000"), "2024-06-15": ("hatzalon7_b", "2,280,000"),
              "2022-03-10": ("hatzalon7_a", "1,950,000")}
# the rents of the fixtures by comparability class (period, VAT): the amounts are the manifest's own
RENT_CLASS = {"6,600": "month/incl", "75,600": "year/excl"} | {a: "month/excl" for a in ("5,200", "5,700", "6,100", "6,400", "6,900")}


# --- environment -------------------------------------------------------------------------------------------------------

@pytest.fixture
def office(db, client, monkeypatch, real_key):  # noqa: F811 - the key fixture is the imported one
    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    o = make_office(db, "משרד בדיקה סינתטי", ADMIN)
    with tenant_tx(o.ctx()) as conn:
        conn.execute(text("UPDATE office_settings SET cloud_llm_enabled = true"))
    login(client, ADMIN)
    o.client, o.ids = client, {}
    return o


def ingest(office, monkeypatch, keys=FIXTURE_KEYS) -> None:
    for key in keys:
        d = MANIFEST[key]
        office.ids[key] = upload(office, FIXTURES / d["file"], d["source_kind"], monkeypatch)
    office.by_id = {v: k for k, v in office.ids.items()}


# --- turns and records -------------------------------------------------------------------------------------------------

def flush() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE.write_text(json.dumps({"model": "gpt-6-luna (settings)", "attempts": RESULTS}, ensure_ascii=False, indent=2,
                                       default=str), encoding="utf-8")


def dates_in(markdown: str, iso: str) -> bool:
    """Whether the markdown writes the date ``iso`` as dd/mm/yyyy or dd.mm.yyyy (the day and month with or without a zero)."""
    y, m, d = iso.split("-")
    forms = {f"{d}/{m}/{y}", f"{d}.{m}.{y}", f"{int(d)}/{int(m)}/{y}", f"{int(d)}.{int(m)}.{y}", f"{d}/{int(m)}/{y}", f"{int(d)}.{m}.{y}"}
    return any(f in markdown for f in forms)


class Turn:
    """A finished turn: the message, the settled intent and the time decision from its diagnostics, the answer's time block."""

    def __init__(self, office, c, m: dict, question: str, wall: float, switch: bool | None) -> None:
        self.office, self.m, self.question, self.switch = office, m, question, switch
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
    def groups(self) -> list[dict]:
        return self.time.get("timeline") or []

    @property
    def sources(self) -> list[dict]:
        return self.answer.get("sources") or []

    def scope_keys(self) -> set[str]:
        """The manifest keys of the documents in the answer's search scope (its sources)."""
        return {self.office.by_id[s["document_id"]] for s in self.sources if s.get("document_id") in self.office.by_id}

    def money(self) -> list[str]:
        return MONEY.findall(self.markdown)

    def record(self) -> dict:
        return {
            "question": self.question, "switch_set": self.switch, "message_id": self.m.get("id"),
            "message_status": self.m.get("status"), "answer_status": self.status, "intent": self.intent,
            "time_preference": self.m.get("time_preference"), "resolution_time": self.mode,
            "answer_time": self.time, "money_in_markdown": self.money(), "searches": self.answer.get("searches"),
            "scope": [{"id": s.get("id"), "kind": s.get("source_kind"), "title": s.get("title"), "location": s.get("location"),
                       "date_line": s.get("date_line"), "document": self.office.by_id.get(s.get("document_id"))}
                      for s in self.sources],
            "values": [{k: v.get(k) for k in ("id", "value_text", "kind", "unit", "period", "vat", "area_basis", "subject", "stance",
                                              "scenario", "role", "as_of", "title", "location", "provenance")}
                       for v in self.answer.get("values") or []],
            "decisions": self.resolution.get("decisions"), "removed": self.removed, "clarification": self.answer.get("clarification"),
            "usage": self.usage, "markdown": self.markdown,
        }


def ask(office, c, cid: str, question: str, rec: dict, switch: bool | None = None) -> Turn:
    """One turn through the API; recorded (and flushed) before anything is asserted about it. ``switch``: the value sent in
    the body (None: the conversation's own)."""
    started = time.perf_counter()
    body = {"content": question} | ({} if switch is None else {"prefer_recent": switch})
    r = c.post(f"/api/chat/conversations/{cid}/messages", json=body)
    assert r.status_code == 200, r.text
    m = c.get(f"/api/chat/messages/{r.json()['assistant']['id']}").json()
    t = Turn(office, c, m, question, time.perf_counter() - started, switch)
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


def set_switch(c, cid: str, on: bool) -> None:
    r = c.patch(f"/api/chat/conversations/{cid}", json={"prefer_recent": on})
    assert r.status_code == 200 and r.json()["prefer_recent"] is on, r.text


# --- the assertions ----------------------------------------------------------------------------------------------------

def answered(t: Turn) -> None:
    assert t.status != "clarification" and not t.answer.get("clarification"), ("a clarification was asked", t.markdown[:300])
    assert t.intent == "property", (t.intent, t.resolution.get("intent"))


def property_intent(t: Turn) -> None:
    """Only the intent: without a recency preference, several appraisals with different values may be answered by asking
    which one is meant, and that is not a defect of the switch (S2)."""
    assert t.intent == "property", (t.intent, t.resolution.get("intent"))


def in_scope(t: Turn, *keys: str) -> None:
    assert set(keys) <= t.scope_keys(), ("the search scope lacks a document", sorted(set(keys) - t.scope_keys()),
                                         [(s.get("title"), s.get("date_line")) for s in t.sources])


def newest_first(rows: list[dict]) -> bool:
    """Dated rows in non-increasing order of date, undated rows after every dated one."""
    dated = [r["date"] for r in rows if r.get("date")]
    first_undated = next((i for i, r in enumerate(rows) if not r.get("date")), len(rows))
    return dated == sorted(dated, reverse=True) and all(not r.get("date") for r in rows[first_undated:])


def valuation_group(t: Turn) -> list[dict]:
    """The timeline group that holds the three dated valuations: its rows (the group whose dated rows are exactly the three)."""
    want = list(VALUATIONS)
    for g in t.groups:
        rows = g.get("rows") or []
        if [r["date"] for r in rows if r.get("date")] == want:
            return rows
    raise AssertionError(("no timeline group holds the three valuations newest first",
                          [[(r.get("date"), r.get("value_text")) for r in g.get("rows") or []] for g in t.groups]))


def valuation_rows(t: Turn) -> None:
    rows = valuation_group(t)
    assert newest_first(rows), [(r.get("date"), r.get("value_text")) for r in rows]
    for r in rows:
        if r.get("date"):
            key, amount = VALUATIONS[r["date"]]
            assert r["source"]["title"] == MANIFEST[key]["title"] and r["source"]["document_id"] == t.office.ids[key], (r, key)
            assert r["value_text"] == amount, (r, amount)
        else:  # an undated row can only be the appraisal that states no valuation date
            assert r["source"]["document_id"] == t.office.ids["hatzalon7_undated"] and "unknown_date" in r["marks"], r


# =========================================================================================================================
# S1: recency default
# =========================================================================================================================

def test_s1_recency_default_the_latest_valuation_first_and_the_timeline_newest_first_three_times(office, monkeypatch):
    ingest(office, monkeypatch)

    def body(o, c, cid, rec):
        t = ask(o, c, cid, VALUE_Q, rec)
        check(rec, "no clarification, intent property", lambda: answered(t))

        def mode():
            assert (t.mode.get("mode"), t.mode.get("source"), t.mode.get("prefer_recent")) == ("recent", "switch", True), t.mode
            assert t.time.get("mode") == "recent" and t.time.get("prefer_recent") is True, t.time

        check(rec, "mode recent", mode)

        def first_value():
            money = t.money()
            assert money and money[0] == "2,410,000", ("the first value is not the 2025 valuation", money[:6], t.markdown[:400])
            assert {"2,280,000", "1,950,000"} <= set(money), ("an earlier valuation is missing from the answer", money)

        check(rec, "first value is the 2025 valuation, the earlier two follow", first_value)
        check(rec, "timeline newest first, sources of the right documents", lambda: valuation_rows(t))
        check(rec, "the scope holds the three appraisals", lambda: in_scope(t, "hatzalon7_a", "hatzalon7_b", "hatzalon7_c"))

    run_sequence("S1", ("no clarification, intent property", "mode recent", "first value is the 2025 valuation, the earlier two follow",
                        "timeline newest first, sources of the right documents", "the scope holds the three appraisals"), office, body)


# =========================================================================================================================
# S2: the switch off
# =========================================================================================================================

def test_s2_switch_off_runs_without_a_preference_and_the_dates_are_still_shown_three_times(office, monkeypatch):
    ingest(office, monkeypatch)

    def body(o, c, cid, rec):
        set_switch(c, cid, False)
        t = ask(o, c, cid, VALUE_Q, rec)
        check(rec, "intent property (a clarification between the appraisals is allowed)", lambda: property_intent(t))

        def mode():
            assert t.mode.get("mode") == "none" and t.mode.get("prefer_recent") is False, t.mode
            assert (t.m.get("time_preference") or {}).get("prefer_recent") is False, t.m.get("time_preference")
            assert t.time.get("mode") == "none" and t.time.get("prefer_recent") is False, t.time

        check(rec, "mode none, the state recorded off", mode)

        def dates():
            shown = [iso for iso in VALUATIONS if dates_in(t.markdown, iso)]
            assert shown, ("no valuation date is in the answer", t.markdown[:400])
            for iso, (_, amount) in VALUATIONS.items():  # a value the answer gives comes with its own date
                if amount in t.money():
                    assert dates_in(t.markdown, iso), (amount, "is given without its date", iso, t.markdown[:400])

        check(rec, "the dates the sources give are in the markdown", dates)

    run_sequence("S2", ("intent property (a clarification between the appraisals is allowed)", "mode none, the state recorded off",
                        "the dates the sources give are in the markdown"), office, body)


# =========================================================================================================================
# S3: an explicit year in a follow-up
# =========================================================================================================================

def test_s3_an_explicit_year_in_a_follow_up_answers_with_that_years_valuation_three_times(office, monkeypatch):
    ingest(office, monkeypatch)

    def body(o, c, cid, rec):
        t1 = ask(o, c, cid, RENT_Q, rec)
        check(rec, "setup: the rent question is answered", lambda: answered(t1))
        t2 = ask(o, c, cid, YEAR_Q, rec)

        def mode():
            req = t2.mode.get("requested") or {}
            assert (t2.mode.get("mode"), t2.mode.get("source")) == ("explicit", "question"), t2.mode
            assert req.get("start") == req.get("end") == "2022", req
            assert t2.time.get("mode") == "explicit", t2.time

        check(rec, "mode explicit for 2022", mode)

        def value():
            money = t2.money()
            assert money and money[0] == "1,950,000", ("the first value is not the 2022 valuation", money[:6], t2.markdown[:400])
            assert dates_in(t2.markdown, "2022-03-10"), ("the 2022 valuation date is not in the answer", t2.markdown[:400])

        check(rec, "the 2022 valuation answers, with its date", value)
        check(rec, "the scope holds the 2022 appraisal", lambda: in_scope(t2, "hatzalon7_a"))

        def later_outside():  # a later valuation the answer gives is marked, not offered as the 2022 value
            for g in t2.groups:
                for r in g.get("rows") or []:
                    if r.get("date") and not r["date"].startswith("2022"):
                        assert "outside_requested" in r["marks"], (r["date"], r["marks"])

        check(rec, "data outside 2022 is marked as outside", later_outside)

    run_sequence("S3", ("setup: the rent question is answered", "mode explicit for 2022", "the 2022 valuation answers, with its date",
                        "the scope holds the 2022 appraisal", "data outside 2022 is marked as outside"), office, body)


# =========================================================================================================================
# S4: the valuation date, not the report date
# =========================================================================================================================

REPORT = "2025-02-05"  # when the 2024 valuation's report was written


def test_s4_the_value_is_dated_by_its_valuation_date_not_by_the_report_date_three_times(office, monkeypatch):
    ingest(office, monkeypatch)

    def body(o, c, cid, rec):
        t = ask(o, c, cid, NAMED_Q, rec)
        check(rec, "no clarification, intent property", lambda: answered(t))

        def value():
            assert "2,280,000" in t.money(), ("the named appraisal's value is not in the answer", t.money(), t.markdown[:400])
            assert dates_in(t.markdown, "2024-06-15"), ("the valuation date 15/06/2024 is not in the answer", t.markdown[:400])

        check(rec, "2,280,000 with the valuation date 15/06/2024", value)

        def never_report_date():
            y, m, d = REPORT.split("-")
            report = [f"{d}/{m}/{y}", f"{d}.{m}.{y}", f"{int(d)}/{int(m)}/{y}", f"{int(d)}.{int(m)}.{y}"]
            for clause in re.split(r"[.\n;]\s", t.markdown):
                if "2,280,000" in clause:
                    assert not any(f in clause for f in report), ("the value is dated with the report date", clause)
            for f in report:
                for hit in re.finditer(re.escape(f), t.markdown):
                    assert "קובע" not in t.markdown[max(0, hit.start() - 30):hit.start()], ("the report date follows 'קובע'", f)
            for g in t.groups:
                for r in g.get("rows") or []:
                    if r.get("value_text") == "2,280,000":
                        assert r.get("date") == "2024-06-15", r
                    assert r.get("date") != REPORT, r
            for s in t.sources:
                assert not any(f in (s.get("date_line") or "") for f in report), s.get("date_line")

        check(rec, "never dated with the report date 05/02/2025", never_report_date)
        check(rec, "the named appraisal is in the scope", lambda: in_scope(t, "hatzalon7_b"))

    run_sequence("S4", ("no clarification, intent property", "2,280,000 with the valuation date 15/06/2024",
                        "never dated with the report date 05/02/2025", "the named appraisal is in the scope"), office, body)


# =========================================================================================================================
# S5: non-comparable rents
# =========================================================================================================================

def test_s5_rents_that_differ_in_period_or_vat_never_share_a_timeline_three_times(office, monkeypatch):
    ingest(office, monkeypatch)

    def body(o, c, cid, rec):
        t = ask(o, c, cid, RENT_Q, rec)
        check(rec, "no clarification, intent property", lambda: answered(t))

        def classes():
            assert t.groups, ("no timeline", t.markdown[:400])
            for g in t.groups:
                got = {RENT_CLASS.get(r["value_text"], f"unknown:{r['value_text']}") for r in g.get("rows") or []}
                assert len(got) == 1 and not any(c.startswith("unknown") for c in got), (g.get("label"), got,
                                                                                         [r["value_text"] for r in g["rows"]])

        check(rec, "no group mixes monthly with VAT, yearly without VAT and monthly without VAT", classes)

        def monthly_series():
            series = [g for g in t.groups if {RENT_CLASS.get(r["value_text"]) for r in g.get("rows") or []} == {"month/excl"}]
            assert series, ("the monthly series without VAT has no group", [[r["value_text"] for r in g["rows"]] for g in t.groups])
            rows = max(series, key=lambda g: len(g["rows"]))["rows"]
            assert newest_first(rows), [(r.get("date"), r.get("value_text")) for r in rows]
            dated = [(r["date"], r["value_text"]) for r in rows if r.get("date") and "forecast" not in r.get("marks", [])]
            assert dated[:3] == [("2025-01-20", "6,400"), ("2024-06-15", "6,100"), ("2022-03-10", "5,200")], dated
            for r in rows:
                if r["value_text"] == "6,900":  # a forecast is marked and never leads
                    assert "forecast" in r["marks"] and r is not rows[0], r

        check(rec, "the monthly series without VAT is its own group, newest first", monthly_series)

        def unmixed_in_markdown():  # the single-value classes are never rows of the series
            for g in t.groups:
                values = {r["value_text"] for r in g.get("rows") or []}
                if "6,400" in values:
                    assert not values & {"6,600", "75,600"}, values

        check(rec, "the yearly and the with-VAT rents are not rows of the monthly series", unmixed_in_markdown)

    run_sequence("S5", ("no clarification, intent property",
                        "no group mixes monthly with VAT, yearly without VAT and monthly without VAT",
                        "the monthly series without VAT is its own group, newest first",
                        "the yearly and the with-VAT rents are not rows of the monthly series"), office, body)


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
            "sequences": {k: {"attempts": v, "passed": len(v) == ATTEMPTS and all(a["outcome"] == "pass" for a in v)}
                          for k, v in sequences.items()},
            "usage_purposes": purposes, "usage_statuses": statuses, "cost_usd": round(sum(costs), 4) if costs else None,
            "model_latency_s": round(sum(t["usage"]["model_latency_ms"] for t in turns) / 1000, 1),
            "wall_s": round(time.perf_counter() - STARTED, 1)}


def teardown_module(module) -> None:  # noqa: ARG001
    if RESULTS:
        flush()
        SUMMARY_FILE.write_text(json.dumps(build_summary(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nreal_model results: {os.path.relpath(RESULTS_FILE, ROOT)}\nsummary: {os.path.relpath(SUMMARY_FILE, ROOT)}")
