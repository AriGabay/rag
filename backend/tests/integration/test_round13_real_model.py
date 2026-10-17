"""Round 13 acceptance with the real model (U15: R1-R3, R8-R13, R24; AE1, AE2, AE5, AE8): the facets of coefficients and rates, the
conflict marks, the derived reduction and the appraiser's address on the synthetic round-13 DOCX fixtures
(``tests/fixtures/round13``) - every name, address and amount invented - ingested through the worker into a synthetic office of
the ``rag_test`` database. Questions go through the chat API to the configured model and judge (``gpt-6-luna`` with the
repository's settings), whose key comes from the repository's ``.env`` and is never printed.

Four sequences, each in a **new conversation, exactly three times**; every attempt is recorded, a failure in any attempt is
reported (a later pass never hides it), and a sequence passes only when all three attempts pass:

- S1 complementary coefficients (AE1): a land table with two reduction coefficients, each for its own consideration;
- S2 rates of different cash flows (AE2): five rates each with its cash flow (two of them 7.25%) and a bare one;
- S3 a derived reduction (AE5): the coefficient 0.8 and the reduction it stands for (20%, which is 1 - 0.8);
- S4 the appraiser's address (AE8): two appraisals whose declaration blocks give the appraiser's office in another city.

Per attempt, by behaviour and never by wording (the gating checks):

- a question is answered, not clarified, as a property question;
- every coefficient or rate the answer registered carries the facet the manifest expects for that datum (status, part, tier and the
  source's own words), matched by the document and the quoted row or sentence;
- the components table lists every coefficient or rate the answer cites, when it cites two or more facets (S1, S2);
- no conflict mark on any row of the timeline or the components table, no ``sources_conflict`` reason, no conflict claim kept
  (all the data of one appraisal are parallel components, never one datum);
- no grounded number is removed: no decision of the diagnostics (a removal, a cut, a withholding or an edit) names as the
  unstated number of a claim a number the sources state or, for S3, the reduction that follows from them;
- S3: the coefficient 0.8 stays in the answer;
- S4: the unit identity of each appraisal holds the subject's address alone (no city conflict, a label naming one address), and the
  appraiser's office street, city, name and licence appear nowhere in the answer.

Informational values (the registered values, the derived computations, every decision, the model calls) are recorded, not gated.
Per turn the whole record is written to ``real_documents/eval/round13/real-model/`` (gitignored and private: answers are kept for
diagnosis), with a summary file next to it listing every attempt of every sequence.

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

from app.chat import contexts
from app.chat import property as P
from app.chat import tools as T
from app.config import get_settings
from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_round10_real_model import new_conversation, upload
from tests.integration.test_round10_real_model import real_key as real_key  # noqa: F401 - the key fixture
from tests.integration.test_round12_real_model import Turn, answered

pytestmark = [pytest.mark.real_model, pytest.mark.db]

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANIFEST = json.loads((FIXTURES / "round13" / "manifest.json").read_text(encoding="utf-8"))
DOCS = MANIFEST["documents"]
DATUM = {d["id"]: d | {"doc": key} for key, spec in DOCS.items() for d in spec.get("data", [])}
RESULTS_DIR = ROOT / "real_documents" / "eval" / "round13" / "real-model"
STAMP = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
RESULTS_FILE = RESULTS_DIR / f"results-{STAMP}.json"
SUMMARY_FILE = RESULTS_DIR / f"summary-{STAMP}.json"
RESULTS: list[dict] = []
ATTEMPTS = 3  # exactly three, each in a new conversation
STARTED = time.perf_counter()

ADMIN = "admin-r13@example.test"
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
    """The round-13 DOCX fixtures ``keys`` through the worker (single appraisals)."""
    for key in keys:
        d = DOCS[key]
        office.ids[key] = upload(office, FIXTURES / d["file"], d["source_kind"], monkeypatch)
    office.by_id = {v: k for k, v in office.ids.items()}


# --- turns and records -------------------------------------------------------------------------------------------------

def flush() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE.write_text(json.dumps({"model": "gpt-6-luna (settings)", "attempts": RESULTS}, ensure_ascii=False, indent=2,
                                       default=str), encoding="utf-8")


class Turn13(Turn):
    """A finished turn plus what the round-13 checks read: the diagnostics' decisions (each removed, cut, edited or
    cited claim with its check, reason and text), the values with their facets, the rows of the timeline and the components table."""

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
    def labels(self) -> list[str]:
        return [b.get("label") or "" for b in (self.time.get("timeline") or []) + (self.time.get("breakdown") or [])]

    def record(self) -> dict:
        return super().record() | {
            "decisions": self.decisions,
            "values_facets": [{"id": v.get("id"), "kind": v.get("kind"), "value_text": v.get("value_text"),
                               "document": self.key_of(v.get("document_id")), "quote": v.get("quote"),
                               "facet": v.get("facet")} for v in self.values],
            "computations": [{"id": c.get("id"), "result_kind": c.get("result_kind"), "expression": c.get("expression"),
                              "display": c.get("display")} for c in self.answer.get("computations") or []],
            "rows": self.rows, "gaps": [g.get("reason") for g in self.answer.get("gaps") or []],
            "tool_errors": [c for c in self.tools if c.get("error")],
        }


def ask(office, c, cid: str, question: str, rec: dict) -> Turn13:
    """One turn through the API; recorded (and flushed) before anything is asserted about it."""
    started = time.perf_counter()
    first_tool = len(TOOL_LOG)
    r = c.post(f"/api/chat/conversations/{cid}/messages", json={"content": question})
    assert r.status_code == 200, r.text
    m = c.get(f"/api/chat/messages/{r.json()['assistant']['id']}").json()
    t = Turn13(office, c, m, question, time.perf_counter() - started, None, list(TOOL_LOG[first_tool:]))
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


def expected_datum(t: Turn13, v: dict) -> dict | None:
    """The manifest datum a registered value is: of its document, the same number and, when several share it (7.25% twice), the
    one whose quoted row or sentence the value's quote holds."""
    key = t.key_of(v.get("document_id"))
    same = [d for d in DATUM.values() if d["doc"] == key and numbers(number_of(d)) == numbers(v.get("value_text") or "")]
    if len(same) > 1:
        quote = plain(v.get("quote"))
        same = [d for d in same if plain((d["where"].get("quote") or d["where"]["table"]["row_label"]).rstrip(".")) in quote] or same
    return same[0] if len(same) == 1 else None


def facets_expected(t: Turn13) -> None:
    """Every coefficient or rate registered carries the facet the manifest gives its datum."""
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
    assert checked, ("no coefficient or rate of the manifest was registered", [(v.get("id"), v.get("kind"), v.get("value_text")) for v in t.values])


def no_conflict(t: Turn13) -> None:
    """All the data are parallel components of one appraisal: no conflict mark, reason or kept claim."""
    marked = [(r["sid"], r["marks"]) for r in t.rows if "conflict" in r["marks"] or r.get("conflict_with")]
    assert not marked, ("a conflict mark between components", marked)
    assert "sources_conflict" not in [g.get("reason") for g in t.answer.get("gaps") or []], "a sources_conflict reason"
    assert not [r for r in t.answer.get("requested") or [] if r.get("status") == "sources_conflict"], "a conflict claim was kept"


def components_listed_in(values: list[dict], rows: list[dict], markdown: str) -> None:
    """When the answer cites coefficients or rates of two or more facets (a stated one, or the source's silence), the components table
    lists every one of them: a row of the table is the value cited or a registration of the same facet and number (the model often
    registers a value twice). The records are plain dicts (an attempt's recorded turn is checked the same way)."""
    cited = set(re.findall(r"\[(V\d+)\]", markdown))
    by_id = {v["id"]: v for v in values}

    def key(v: dict) -> tuple:
        f = v.get("facet") or {}
        return (plain(f.get("written")) if f.get("status") == "stated" else "", v.get("value_text"))

    wanted = [v for i, v in by_id.items() if i in cited and v.get("kind") in FACET_KINDS]
    if len({k[0] for k in map(key, wanted)}) < 2:
        return
    listed = {key(by_id[r["sid"]]) for r in rows if r["sid"] in by_id}
    missing = sorted({(v["id"], v.get("value_text")) for v in wanted if key(v) not in listed})
    assert not missing, ("a cited coefficient or rate is not in the components table", missing, sorted(r["sid"] for r in rows))


def components_listed(t: Turn13) -> None:
    components_listed_in(t.values, t.rows, t.markdown)


def no_grounded_removed(t: Turn13, grounded: set[str], derived: set[str] = frozenset()) -> None:
    """No decision names as the unstated number of a claim one the sources state (the grounded numbers) or one that follows from
    them by the reduction formula (``derived``): those must verify, and a claim holding a wrong derived number is edited, not its
    grounded number removed."""
    bad = []
    for d in t.decisions:
        if d["action"] not in GONE or d.get("check") != "unstated_number":
            continue
        failing = numbers((d.get("reason") or "").split(":", 1)[-1])
        if failing and failing <= (derived | grounded):
            bad.append({"action": d["action"], "failing": sorted(failing), "text": (d.get("text") or "")[:160]})
    assert not bad, ("a grounded or correctly derived number was cut or removed", bad)


def both_in_answer(t: Turn13, *amounts: str) -> None:
    present = numbers(t.markdown)
    missing = [a for a in amounts if a not in present]
    assert not missing, (f"{missing} are not in the answer", t.markdown[:400])


def info(rec: dict, t: Turn13) -> None:
    """Recorded, not gated: what the model registered and what was lost."""
    rec["info"].update({
        "registered": [(v.get("id"), v.get("kind"), v.get("value_text"), (v.get("facet") or {}).get("status")) for v in t.values],
        "decisions": [(d["action"], d.get("check"), (d.get("reason") or "")[:80]) for d in t.decisions],
        "derived": [c["display"] for c in t.record()["computations"] if c["result_kind"] == "derived"],
        "tool_calls": [c["name"] for c in t.tools], "removed": t.answer.get("verification", {}).get("removed"),
        "edited": t.answer.get("verification", {}).get("edited")})


# =========================================================================================================================
# S1: complementary coefficients (AE1)
# =========================================================================================================================

S1_Q = "מהם מקדמי ההפחתה בנכס ברחוב הצבעוני 14?"
LAND_NUMBERS = {"0.8", "0.9", "620", "4200", "1874880", "90"}


def test_s1_two_coefficients_of_two_considerations_each_with_its_facet_and_no_conflict_three_times(office, monkeypatch):
    ingest(office, monkeypatch, ("land_table",))

    def body(o, c, cid, rec):
        t = ask(o, c, cid, S1_Q, rec)
        info(rec, t)
        check(rec, "no clarification, intent property", lambda: answered(t))
        check(rec, "every coefficient registered carries its facet", lambda: facets_expected(t))
        check(rec, "no conflict mark between the two coefficients", lambda: no_conflict(t))
        check(rec, "no grounded number removed", lambda: no_grounded_removed(t, LAND_NUMBERS))
        check(rec, "both coefficients are in the answer", lambda: both_in_answer(t, "0.8", "0.9"))
        check(rec, "the components table lists both coefficients", lambda: components_listed(t))

    run_sequence("S1", ("no clarification, intent property", "every coefficient registered carries its facet",
                        "no conflict mark between the two coefficients", "no grounded number removed",
                        "both coefficients are in the answer", "the components table lists both coefficients"), office, body)


# =========================================================================================================================
# S2: rates of different cash flows (AE2)
# =========================================================================================================================

S2_Q = "מהם שיעורי ההיוון בנכס ברחוב הכלנית 8?"
CAP_NUMBERS = {"6.5", "7.25", "8.5", "9", "10", "90"}


def equal_rates_stay_apart(t: Turn13) -> None:
    """Two registered rates of one number (7.25% for two cash flows) are two rows of the components table, never one."""
    equal = [v for v in t.values if v.get("kind") == "rate" and v.get("value_text") == "7.25"]
    quotes = {plain(v.get("quote")) for v in equal}
    if len(quotes) < 2:
        return  # the model registered at most one of them: nothing to keep apart
    sids = {r["sid"] for r in t.rows if r["value_text"] == "7.25"}
    assert {v["id"] for v in equal} <= sids, ("a 7.25% rate is missing from the components table", sorted(sids), [v["id"] for v in equal])


def test_s2_rates_of_five_cash_flows_and_a_bare_one_each_with_its_facet_never_a_conflict_three_times(office, monkeypatch):
    ingest(office, monkeypatch, ("cap_rates",))

    def body(o, c, cid, rec):
        t = ask(o, c, cid, S2_Q, rec)
        info(rec, t)
        check(rec, "no clarification, intent property", lambda: answered(t))
        check(rec, "every rate registered carries its facet", lambda: facets_expected(t))
        check(rec, "no conflict mark between the rates", lambda: no_conflict(t))
        check(rec, "no grounded number removed", lambda: no_grounded_removed(t, CAP_NUMBERS))
        check(rec, "two equal rates stay two rows", lambda: equal_rates_stay_apart(t))
        check(rec, "the components table lists every rate the answer cites", lambda: components_listed(t))

    run_sequence("S2", ("no clarification, intent property", "every rate registered carries its facet",
                        "no conflict mark between the rates", "no grounded number removed", "two equal rates stay two rows",
                        "the components table lists every rate the answer cites"), office, body)


# =========================================================================================================================
# S3: a derived reduction (AE5)
# =========================================================================================================================

S3_Q = "מהו מקדם ההפחתה בשל זכויות בנייה חלקיות בנכס ברחוב הצבעוני 14, ובכמה אחוזים הוא מפחית את השווי?"
DERIVED = {"20", "10"}  # 1 - 0.8 and 1 - 0.9, shown as percents


def test_s3_a_derived_reduction_is_kept_beside_its_coefficient_and_nothing_grounded_is_removed_three_times(office, monkeypatch):
    ingest(office, monkeypatch, ("land_table",))

    def body(o, c, cid, rec):
        t = ask(o, c, cid, S3_Q, rec)
        info(rec, t)
        check(rec, "no clarification, intent property", lambda: answered(t))
        check(rec, "every coefficient registered carries its facet", lambda: facets_expected(t))
        check(rec, "no conflict mark", lambda: no_conflict(t))
        check(rec, "no grounded or derived number removed", lambda: no_grounded_removed(t, LAND_NUMBERS, DERIVED))
        check(rec, "the coefficient 0.8 stays in the answer", lambda: both_in_answer(t, "0.8"))

    run_sequence("S3", ("no clarification, intent property", "every coefficient registered carries its facet", "no conflict mark",
                        "no grounded or derived number removed", "the coefficient 0.8 stays in the answer"), office, body)


# =========================================================================================================================
# S4: the appraiser's address (AE8)
# =========================================================================================================================

S4_QUESTIONS = {"appraiser_office": "מה שטח הנכס ברחוב הרקפת 18?", "appraiser_office_table": "מה שטח הנכס ברחוב הנורית 6?"}


def one_address(office, key: str) -> None:
    """The unit identity of the appraisal ``key`` holds the subject's address alone, its city and no city conflict, and its
    label names one address (the manifest's expected identity)."""
    want = DOCS[key]["identity"]
    with tenant_tx(office.ctx()) as conn:
        graph = P.build_graph(conn)
    nodes = [n for n in graph.nodes if str(n.document_id) == office.ids[key]]
    assert nodes, f"{key} is in no node of the property graph"
    (core,) = contexts.identifiers(f"רחוב {want['street']} {want['number']}").addresses
    for n in nodes:
        ident = n.identity
        assert ident.addresses == {core}, (key, sorted(ident.addresses), core)
        assert ident.cities == {want["city"]} and not ident.city_conflict, (key, sorted(ident.cities), ident.city_conflict)
    label = P.label_of(nodes)
    assert label == f"{core[0]} {core[1]}, {want['city']}" and label.count(",") == 1, label


def office_details_absent(t: Turn13) -> None:
    """The appraiser's office street, its city, the expert's name and licence are nowhere in the answer, its time block or its labels."""
    gone = next(c for c in MANIFEST["cases"] if c["id"] == "AE8")["expected"]["no_facts_from"]
    shown = " ".join([t.markdown, json.dumps(t.time, ensure_ascii=False), " ".join(t.labels)])
    leaked = [k for k in ("street", "city", "appraiser_name", "licence") if gone[k] in shown]
    assert not leaked, ("the appraiser's details are in the answer", leaked, t.markdown[:300])


def test_s4_the_appraisers_office_address_never_joins_the_subjects_identity_three_times(office, monkeypatch):
    ingest(office, monkeypatch, tuple(S4_QUESTIONS))

    def body(o, c, cid, rec):
        asked = []
        for n, question in enumerate(S4_QUESTIONS.values()):
            t = ask(o, c, cid if n == 0 else new_conversation(c), question, rec)
            info(rec, t)
            asked.append(t)
        check(rec, "no clarification, intent property", lambda: [answered(t) for t in asked])
        check(rec, "each unit identity holds one address, its city and no city conflict",
              lambda: [one_address(o, key) for key in S4_QUESTIONS])
        check(rec, "the appraiser's details are not in the answers", lambda: [office_details_absent(t) for t in asked])
        check(rec, "no conflict mark or reason", lambda: [no_conflict(t) for t in asked])

    run_sequence("S4", ("no clarification, intent property", "each unit identity holds one address, its city and no city conflict",
                        "the appraiser's details are not in the answers", "no conflict mark or reason"), office, body)


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
