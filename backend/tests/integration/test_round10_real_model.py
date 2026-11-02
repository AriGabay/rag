"""Round 10 acceptance with the real model (U11: R36-R38; AE1, AE2, AE3, AE6; KD15): the conversation of the office-A report
and its relatives, on the synthetic round-10 fixtures (``tests/fixtures/round10``: a synthetic standard 98.1 whose section 2
is "שלוש גישות השומה הבסיסיות", a clarification of it, an appraisal titled "תקן 98 - …" that is an appraisal, and three
appraisals whose titles hold "שומה"; and the round-9 file R9B for a clarification between two appraisals) — every name,
address and amount invented — ingested through the worker into a synthetic office of the ``rag_test`` database. Questions go
through the chat API to the configured model and judge (``gpt-6-luna`` with the repository's settings), whose key comes from
the repository's ``.env`` and is never printed. The second edition of the standard is left out: the sequences ask what the
one standard holds, and a second edition would make "the standard holds the topic" ambiguous.

Four sequences, each in a **new conversation, exactly three times**; every attempt is recorded, a failure in any attempt is
reported (a later pass never hides it), and a sequence passes only when all three attempts pass:

- (i) "מה זה דמ״ש ראויים ?" → "מה זה היוון הכנסות ?" → "מה הן שלושת גישות השומה הבסיסיות ?": on the third turn no choice of
  property is asked, nothing is looked up, the intent is ``professional`` and the standard (which holds the topic) is cited;
- (ii) the same conversation, then "הסבר את הגישה השנייה": the ordinal is bound to the second item of the list the third answer
  printed (when the answer printed none, that is the failure: nothing can be bound to a list nobody gave);
- (iii) a clarification between two appraisals is pending, then a new professional question: it is answered as one, the
  clarification expires and is not asked again;
- (iv) a property question on the appraisal titled "תקן 98 - …", then "איך מיישמים את גישת ההשוואה בנכס הזה?": the intent is
  ``mixed``, the property is restored, and both a standard and that appraisal are cited.

Everything is asserted by behaviour - the settled intent, the lookup and the clarification, the ordinal bound, the kinds and
documents cited, the slots stored - never by wording. Per turn, the model calls, tokens, cost and latency (the message's
``usage``) and the wall time are written to ``real_documents/eval/round10/real-model/`` (gitignored and private: answers are
kept for diagnosis), with a summary file next to it listing every attempt of every sequence.

Opt in with ``-m real_model``; skipped without a key. Host only, against ``rag_test`` (``tests/conftest.py`` guard)."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from app import worker
from app.config import get_settings
from app.db import tenant_tx
from app.platform import pipeline
from app.providers import llm
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_chat_units import R9B, SIMILAR_QUESTION

pytestmark = [pytest.mark.real_model, pytest.mark.db]

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANIFEST = json.loads((FIXTURES / "round10" / "manifest.json").read_text(encoding="utf-8"))["documents"]
RESULTS_DIR = ROOT / "real_documents" / "eval" / "round10" / "real-model"
STAMP = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
RESULTS_FILE = RESULTS_DIR / f"results-{STAMP}.json"
SUMMARY_FILE = RESULTS_DIR / f"summary-{STAMP}.json"
RESULTS: list[dict] = []
ATTEMPTS = 3  # exactly three, each in a new conversation
STARTED = time.perf_counter()

ADMIN = "admin-r10@example.test"
# the office: the standard (edition 1), its clarification, the appraisal titled as a standard and three appraisals titled
# with "שומה" (the shape of the reported conversation)
FIXTURE_KEYS = ("standard_ed1", "clarification", "appraisal_titled_as_standard", "appraisal_shape_1", "appraisal_shape_2",
                "appraisal_shape_3")
# the streets of the four appraisals: an answer that names two of them is offering a choice of properties
STREETS = ("הדקל", "התמר", "היסמין", "הברוש")

Q1, Q2, Q3 = "מה זה דמ״ש ראויים ?", "מה זה היוון הכנסות ?", "מה הן שלושת גישות השומה הבסיסיות ?"
Q4 = "הסבר את הגישה השנייה"
GENERAL_QUESTION = "מה זה שווי שוק?"
PROPERTY_QUESTION, APPLY_QUESTION = "מה שווי הנכס ברחוב הדקל 5?", "איך מיישמים את גישת ההשוואה בנכס הזה?"
STANDARD_KINDS = ("standard", "professional")


# --- environment -------------------------------------------------------------------------------------------------------

@pytest.fixture
def real_key(monkeypatch):
    """The repository's model key on this process's settings (never printed); skip without one."""
    from app.config import Settings

    for name in ("OPENAI_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    key = Settings(_env_file=ROOT / ".env").openai_api_key.get_secret_value()
    if not key:
        pytest.skip("no OpenAI key in the environment or the repo .env")
    s = get_settings()
    monkeypatch.setattr(s, "llm_provider", "openai")
    monkeypatch.setattr(s, "openai_api_key", SecretStr(key))
    llm._provider.cache_clear()
    llm._client.cache_clear()
    yield
    llm._provider.cache_clear()
    llm._client.cache_clear()


@pytest.fixture
def office(db, client, monkeypatch, real_key):
    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    o = make_office(db, "משרד בדיקה סינתטי", ADMIN)
    with tenant_tx(o.ctx()) as conn:
        conn.execute(text("UPDATE office_settings SET cloud_llm_enabled = true"))
    login(client, ADMIN)
    o.client, o.ids = client, {}
    return o


def upload(office, path: Path, kind: str | None, monkeypatch) -> str:
    """A fixture uploaded as ``kind`` (None: the default) and read by the worker without OCR, vision or the measurement
    pass: text only. Returns the document id."""
    from app.extraction import ocr

    with monkeypatch.context() as m:
        m.setattr(ocr, "ocr_available", lambda languages: False)
        m.setattr(pipeline, "vision_reader", lambda ctx: None)
        m.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
        data = {"group_id": str(office.default_group_id)} | ({"source_kind": kind} if kind else {})
        r = office.client.post("/api/documents", files=[("files", (path.name, path.read_bytes(), "application/octet-stream"))],
                               data=data)
        assert r.status_code == 200, r.text
        while worker.run_one("round10-real-model"):
            pass
    return r.json()["results"][0]["document_id"]


def ingest(office, monkeypatch, keys=FIXTURE_KEYS) -> None:
    for key in keys:
        d = MANIFEST[key]
        office.ids[key] = upload(office, FIXTURES / d["file"], d["source_kind"], monkeypatch)
    office.std, office.d5 = office.ids.get("standard_ed1"), office.ids.get("appraisal_titled_as_standard")


# --- turns and records -------------------------------------------------------------------------------------------------

def new_conversation(c) -> str:
    r = c.post("/api/chat/conversations")
    assert r.status_code == 200, r.text
    return r.json()["id"]


def usage_of(m: dict, wall: float) -> dict:
    """The turn's model calls, tokens, cost and latency (the message's ``usage``) and its wall time."""
    usage = m.get("usage") or []
    total = lambda k: sum(u.get(k) or 0 for u in usage)  # noqa: E731
    cost = [u.get("cost_usd") for u in usage]
    purposes: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for u in usage:
        purposes[u["purpose"]] = purposes.get(u["purpose"], 0) + 1
        statuses[u.get("status")] = statuses.get(u.get("status"), 0) + 1
    return {"calls": len(usage), "purposes": purposes, "statuses": statuses, "input_tokens": total("input_tokens"),
            "cached_input_tokens": total("cached_input_tokens"), "output_tokens": total("output_tokens"),
            "cost_usd": round(sum(c for c in cost if c is not None), 6) if any(c is not None for c in cost) else None,
            "model_latency_ms": total("latency_ms"), "wall_s": round(wall, 1)}


def flush() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE.write_text(json.dumps({"model": "gpt-6-luna (settings)", "attempts": RESULTS}, ensure_ascii=False, indent=2,
                                       default=str), encoding="utf-8")


class Turn:
    """A finished turn: the message, the settled intent and decisions from its diagnostics, the slots it stored."""

    def __init__(self, office, c, m: dict, question: str, wall: float) -> None:
        self.office, self.m, self.question = office, m, question
        self.answer = m.get("answer") or {}
        d = c.get(f"/api/chat/messages/{m['id']}/diagnostics")
        self.resolution = d.json().get("resolution") or {} if d.status_code == 200 else {}
        with tenant_tx(office.system) as conn:
            self.context = conn.execute(text("SELECT answer->'context' FROM messages WHERE id = :m"), {"m": m["id"]}).scalar_one()
        self.usage = usage_of(m, wall)

    @property
    def status(self) -> str:
        return self.answer.get("status")

    @property
    def request(self) -> dict:
        return self.answer.get("request") or {}

    @property
    def settled(self) -> dict:
        return self.resolution.get("intent") or {}

    @property
    def intent(self) -> str | None:
        return self.settled.get("value")

    @property
    def decisions(self) -> dict:
        return self.resolution.get("decisions") or {}

    @property
    def markdown(self) -> str:
        return self.answer.get("markdown") or ""

    @property
    def cited(self) -> list[dict]:
        return self.answer.get("sources") or []

    def cited_of(self, kinds: tuple[str, ...], document: str | None = None) -> list[dict]:
        return [s for s in self.cited if s.get("source_kind") in kinds and (document is None or s.get("document_id") == document)]

    def record(self) -> dict:
        return {
            "question": self.question, "message_id": self.m.get("id"), "message_status": self.m.get("status"),
            "answer_status": self.status, "intent": self.intent, "claimed": self.settled.get("claimed"),
            "evidence": self.settled.get("evidence"), "reasons": self.settled.get("reasons"),
            "item": self.settled.get("item"), "item_label": self.settled.get("item_label"), "decisions": self.decisions,
            "lookup": self.resolution.get("lookup"), "candidates": len(self.request.get("candidates") or []),
            "request_documents": self.request.get("document_ids"), "topic": (self.context or {}).get("topic"),
            "pending": (self.context or {}).get("pending"),
            "cited": [{"id": s.get("id"), "kind": s.get("source_kind"), "document_id": s.get("document_id"),
                       "title": s.get("title"), "location": s.get("location"), "standard": s.get("standard")}
                      for s in self.cited],
            "usage": self.usage, "markdown": self.markdown,
        }


def ask(office, c, cid: str, question: str, rec: dict) -> Turn:
    """One turn through the API; recorded (and flushed) before anything is asserted about it."""
    started = time.perf_counter()
    r = c.post(f"/api/chat/conversations/{cid}/messages", json={"content": question})
    assert r.status_code == 200, r.text
    m = c.get(f"/api/chat/messages/{r.json()['assistant']['id']}").json()
    t = Turn(office, c, m, question, time.perf_counter() - started)
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

def no_choice_of_property(t: Turn) -> None:
    assert t.status != "clarification", ("a clarification was asked", t.markdown[:300])
    assert not t.request.get("candidates"), t.request.get("candidates")
    assert "lookup" not in t.resolution, t.resolution.get("lookup")
    named = [s for s in STREETS if s in t.markdown]
    assert len(named) < 2, ("the answer lists properties", named)


def professional_with_standard(t: Turn, standard: str) -> None:
    no_choice_of_property(t)
    assert t.intent == "professional", (t.intent, t.settled.get("claimed"), t.settled.get("reasons"))
    assert (t.request.get("document_ids") or []) == [], t.request.get("document_ids")
    assert t.cited_of(STANDARD_KINDS, standard), ("the standard that holds the topic is not cited",
                                                  [(s.get("source_kind"), s.get("title")) for s in t.cited])


# =========================================================================================================================
# (i) the reported conversation, and (ii) the second approach
# =========================================================================================================================

def test_the_reported_conversation_then_the_second_approach_three_times(office, monkeypatch):
    ingest(office, monkeypatch)

    def body(o, c, cid, rec):
        t1, t2, t3 = ask(o, c, cid, Q1, rec), ask(o, c, cid, Q2, rec), ask(o, c, cid, Q3, rec)

        def first_two():
            for t in (t1, t2):
                assert t.status != "clarification" and not t.request.get("candidates"), (t.question, t.markdown[:300])

        check(rec, "i/general questions are answered, not asked about", first_two)
        check(rec, "i", lambda: professional_with_standard(t3, o.std))
        items = ((t3.context or {}).get("topic") or {}).get("items") or []
        rec["items"] = items
        t4 = ask(o, c, cid, Q4, rec)

        def second():
            assert len(items) >= 2, ("the third answer printed no list of items to bind to", t3.markdown[:400])
            assert t4.status != "clarification" and not t4.request.get("candidates"), t4.markdown[:300]
            assert t4.intent == "professional", (t4.intent, t4.settled.get("claimed"), t4.settled.get("reasons"))
            assert t4.settled.get("item") == 2 and t4.settled.get("item_label") == items[1], (t4.settled, items)
            assert str(t4.decisions.get("item", "")).startswith("bound: 2"), t4.decisions
            assert "lookup" not in t4.resolution and (t4.request.get("document_ids") or []) == []

        check(rec, "ii", second)

    run_sequence("i+ii", ("i/general questions are answered, not asked about", "i", "ii"), office, body)


# =========================================================================================================================
# (iii) a pending clarification, then a new professional question
# =========================================================================================================================

def test_a_new_professional_question_is_not_swallowed_by_a_pending_clarification_three_times(office, monkeypatch):
    ingest(office, monkeypatch)
    upload(office, R9B, None, monkeypatch)

    def body(o, c, cid, rec):
        t1 = ask(o, c, cid, SIMILAR_QUESTION, rec)
        def pending():
            assert t1.status == "clarification", ("no clarification was asked", t1.status, t1.markdown[:300])
            assert ((t1.context or {}).get("pending") or {}).get("kind") == "candidates", (t1.context or {}).get("pending")

        check(rec, "iii/setup: the clarification is pending", pending)
        t2 = ask(o, c, cid, GENERAL_QUESTION, rec)

        def new_question():
            professional_with_standard(t2, o.std)
            assert str(t2.decisions.get("pending", "")).startswith("expired"), t2.decisions
            assert (t2.context or {}).get("pending") is None

        check(rec, "iii", new_question)

    run_sequence("iii", ("iii/setup: the clarification is pending", "iii"), office, body)


# =========================================================================================================================
# (iv) a property question, then its application: mixed
# =========================================================================================================================

def test_a_property_question_then_how_to_apply_the_approach_to_it_is_mixed_three_times(office, monkeypatch):
    ingest(office, monkeypatch)

    def body(o, c, cid, rec):
        t1 = ask(o, c, cid, PROPERTY_QUESTION, rec)

        def first():
            assert t1.status != "clarification" and t1.intent == "property", (t1.status, t1.intent, t1.settled.get("claimed"))
            assert t1.cited_of(("appraisal",), o.d5), [(s.get("source_kind"), s.get("title")) for s in t1.cited]
            assert "2,450,000" in t1.markdown, t1.markdown[:300]

        check(rec, "iv/setup: the property question is answered from its appraisal", first)
        t2 = ask(o, c, cid, APPLY_QUESTION, rec)

        def apply():
            assert t2.status != "clarification" and not t2.request.get("candidates"), t2.markdown[:300]
            assert t2.intent == "mixed", (t2.intent, t2.settled.get("claimed"), t2.settled.get("reasons"))
            assert t2.request.get("document_ids") == [o.d5], t2.request.get("document_ids")
            # a professional source of the standard's family — the standard or the clarification of it — beside the property's
            # appraisal: R27 separates the requirement from the data, either professional document carries the requirement
            assert t2.cited_of(STANDARD_KINDS), ("no professional source is cited",
                                                 [(s.get("source_kind"), s.get("title")) for s in t2.cited])
            assert t2.cited_of(("appraisal",), o.d5), ("the property's appraisal is not cited",
                                                      [(s.get("source_kind"), s.get("title")) for s in t2.cited])

        check(rec, "iv", apply)

    run_sequence("iv", ("iv/setup: the property question is answered from its appraisal", "iv"), office, body)


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
