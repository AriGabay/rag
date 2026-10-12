"""Round 9 acceptance with the real model (U9, U15, U11, U6; AE2-AE5): two-level retrieval inside a file of several
appraisals, the bounded re-read, a computation after the tool budget, and a file that was not read. The synthetic
round-9 fixtures (``tests/fixtures/round9``: R9a a compendium of three appraisals and a comparables appendix, R9b two
similar names, R9c a table that continues across a page break) — every project, street, block, parcel and amount
invented — are ingested through the worker into a synthetic office of the ``rag_test`` database, and questions go
through the chat API to the configured model and judge (``gpt-6-luna`` with the repository's settings), whose key comes
from the repository's ``.env`` and is never printed.

Each example is asserted by behaviour — what the saved answer states and cites, the payload (the unit of every source,
the pages they open), the rows and the log — never by exact wording. The model is nondeterministic: each case may run a
bounded second attempt (in a new conversation), every attempt's outcome is recorded, and no assertion is loosened to
pass.

Where a case needs a defect that the real model does not produce on demand, it is made from outside and everything after
it is the real path: ``SourceCut`` edits the model's first final answer so that the claim stating a table's total cites one
source only, and cuts that source's text to the total's own row — the part of a table that lacks its header and unit, as
when a claim cites only the page of the table the total is on — before the first verification (the real judge, the real re-read, the real final check follow); ``Recorder`` passes every agent step
through to the real provider and records the ``tool_choice`` it was given and the status that came back; an unread file
is a version inserted as ``failed`` with a page-limit reason.

Per turn, the model calls, tokens, cost and latency (the message's ``usage``) and the wall time are recorded to a JSON
file under ``real_documents/eval/round9/real-model/`` (gitignored, private: answers are kept for diagnosis), with a
summary file next to it: the ``verify_reread`` and ``verify_final`` call counts, the span-validity rate, whether the
``allowed_tools`` choice was accepted, and every case's attempts.

Opt in with ``-m real_model``; skipped without a key. Host only, against ``rag_test`` (``tests/conftest.py`` guard)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from app.chat import engine, final_check, verify
from app.config import get_settings
from app.db import tenant_tx
from app.providers import llm
from app.providers.llm import Purpose
from tests.conftest import login
from tests.factories import make_office

pytestmark = [pytest.mark.real_model, pytest.mark.db]

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANIFEST = json.loads((FIXTURES / "round9" / "manifest.json").read_text(encoding="utf-8"))["documents"]
RESULTS_DIR = ROOT / "real_documents" / "eval" / "round9" / "real-model"
STAMP = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
RESULTS_FILE = RESULTS_DIR / f"results-{STAMP}.json"
SUMMARY_FILE = RESULTS_DIR / f"summary-{STAMP}.json"
RESULTS: list[dict] = []
ATTEMPTS = 2  # a bounded second attempt per case, in a new conversation
STARTED = time.perf_counter()

UNITS = MANIFEST["compendium"]["units"]  # first, second, third (appraisals) and the comparables appendix
FACTS = MANIFEST["compendium"]["facts"]
SIMILAR = MANIFEST["similar_names"]
SPLIT_TABLE = MANIFEST["picture_title"]["facts"]["table"]
SECOND_NAME = UNITS[1]["aliases"][0]  # "גבעת הרקפות": the project name, in no file title
SECOND_PAGES = set(range(UNITS[1]["first_page"], UNITS[1]["last_page"] + 1))
SECOND_VALUE, THIRD_RENT = FACTS["second_value"]["value"], FACTS["third_rent_sqm"]["value"]
APPENDIX_NAMES = sorted({row[0] for row in FACTS["appendix_table"]["rows"]})
ALEF_RENT, BET_RENT = SIMILAR["facts"]["alef_rent"]["value"], SIMILAR["facts"]["bet_rent"]["value"]
TOTAL = SPLIT_TABLE["total"]  # "3,455": the table's total, alone on the second page

# an answer that says no single appraisal holds both terms, or asks which one is meant (AE3)
NO_SINGLE = re.compile(
    r"אף\s+שומה|אין\s+(?:שומה|יחידה|הערכה|שמאות)\s+(?:אחת|בודדת|משותפת)|אין\s+בו\s+יחידה|שומות\s+(?:שונות|נפרדות)"
    r"|ביחידות\s+שונות|בשומות\s+(?:שונות|נפרדות)|לא\s+(?:באותה|באף)\s+שומה|אינ(?:ן|ה|ם)\s+מופיע\S*\s+(?:יחד|באותה)"
    r"|לאיזו\s+(?:שומה|מהן|מהשומות)|איזו\s+שומה|לאיזה\s+(?:פרויקט|מתחם|שומה)|איזה\s+(?:פרויקט|מתחם|שומה)")
# a rent that is stated as absent, or a request for more (AE5)
ABSENT = re.compile(r"לא\s+(?:נמצא|נמצאו|מצאתי|צוין|צוינו|מופיע|מופיעה|מופיעים)|אין\s+(?:בשומה|במסמך|נתון|ציון|מידע)"
                    r"|אינ(?:ו|ה|ם|ן)\s+(?:מצוינ|מופיע|כתוב)|לא\s+(?:כתוב|כתובים|נקרא)")
NOT_FOUND = re.compile(r"לא\s+(?:נמצא|נמצאה|נמצאו|אותר|אותרה|אותרו)|אינו\s+קיים|אינה\s+קיימת")  # U6: never said of the file
NOT_READ = re.compile(r"לא\s+(?:נקרא|נקראה|נקראו|ניתן\s+היה\s+לקרוא|ניתן\s+לקרוא)|טרם\s+נקרא|לא\s+עובד")
API_ERRORS = ("error", "invalid", "unsupported", "auth", "model_unavailable", "quota", "rate_limited", "timeout")
TOO_LONG = "המסמך ארוך מהמותר: 1,000 עמודים (המותר 400)"
FAILED_TITLE = "שומה - הזית 9 נווה ים"
READY_TITLE = "שומה - הגפן 12 רמת השרון"


# --- instrumentation: counters read at the real boundaries; nothing in the application is changed ----------------------

class Probe:
    """Counts what the verification does, from outside: every judge call by usage purpose (``verify.judge`` wrapped) and
    the judge's claims whose span the server found in its unit against the whole-unit fallbacks (``unit_claims``)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        self.data = {"claims_exact": 0, "claims_fallback": 0, "units_with_claims": 0, "units_fallback": 0,
                     "fallback_reasons": {}, "judge_calls": {}, "incomplete": 0, "halved": 0}

    def take(self) -> dict:
        with self.lock:
            out, self.data = self.data, {}
            self.reset()
            return out

    def judge(self, original):
        def wrapped(provider, batch, rendered, usage, deadline=None, coverage=None, *, policy="", label="verify"):
            result = original(provider, batch, rendered, usage, deadline, coverage, policy=policy, label=label)
            with self.lock:
                self.data["judge_calls"][label] = self.data["judge_calls"].get(label, 0) + 1
                if result[1] == "incomplete":
                    self.data["incomplete"] += 1
                    if len(batch.units) > 1:
                        self.data["halved"] += 1
            return result
        return wrapped

    def unit_claims(self, original):
        def wrapped(u, v, ws=None):
            claims = original(u, v, ws)
            if v.claims:  # the judge returned claims for this unit: were their spans exact?
                with self.lock:
                    self.data["units_with_claims"] += 1
                    if claims and claims[0].fallback:
                        self.data["units_fallback"] += 1
                        self.data["claims_fallback"] += len(v.claims)
                        reasons = self.data["fallback_reasons"]
                        reasons[claims[0].fallback] = reasons.get(claims[0].fallback, 0) + 1
                    else:
                        self.data["claims_exact"] += len(claims)
            return claims
        return wrapped


PROBE = Probe()


class Recorder:
    """The real model on every call; each agent step's ``tool_choice`` and the status it came back with are recorded
    (U11: the step after the tool budget carries an ``allowed_tools`` choice, and the provider must accept it)."""

    name = "openai"
    routed = False  # serves every purpose itself, each through the purpose's configured provider

    def __init__(self) -> None:
        self.model = llm.get_provider(Purpose.AGENT).model
        self.steps: list[dict] = []

    def structured(self, purpose, *args, **kw):
        return llm.get_provider(purpose).structured(purpose, *args, **kw)

    def structured_image(self, purpose, *args, **kw):
        return llm.get_provider(purpose).structured_image(purpose, *args, **kw)

    def agent_step(self, *args, **kw):
        step = llm.get_provider(Purpose.AGENT).agent_step(*args, **kw)
        choice = kw.get("tool_choice")
        self.steps.append({"choice": choice if isinstance(choice, str) or choice is None else choice.get("type"),
                           "tools": [t["name"] for t in choice.get("tools", [])] if isinstance(choice, dict) else None,
                           "status": getattr(step.status, "value", str(step.status)), "detail": step.detail,
                           "calls": [c.name for c in step.calls or []]})
        return step

    def allowed_tools_steps(self) -> list[dict]:
        return [s for s in self.steps if s["choice"] == "allowed_tools"]


def record_steps(monkeypatch) -> Recorder:
    fake = Recorder()
    monkeypatch.setattr(llm, "get_selected_provider", lambda: fake)
    return fake


class SourceCut:
    """Wraps the engine's ``verify_answer``: before the first verification that meets a line stating ``number``, the
    model's first final answer is edited so that the first line stating it becomes ``claim`` citing one source only —
    a table of the turn that states the number, else the source the line's citations rest on — and that source's text
    is cut to the number's own row on the page the manifest puts it on, with the column names of a row-form source
    taken away: the part of a table that lacks its header and unit. The anchors of the source stay, so the bounded
    re-read reads the table's headers where the table is. ``applied``: it happened."""

    def __init__(self, original, number: str, claim: str, page: int) -> None:
        self.original, self.number, self.claim, self.page, self.applied = original, number, claim, page, False

    def __call__(self, provider, answer, ws, *args, **kw):
        if not self.applied:
            self.cut(answer, ws)
        return self.original(provider, answer, ws, *args, **kw)

    @staticmethod
    def behind(ws, cited: str) -> list[str]:
        """The sources a citation rests on: a source itself, the source a verified value was taken from, the sources
        behind a result's inputs."""
        if cited in ws.values:
            return [ws.values[cited].source_id]
        if cited in ws.computations:
            return list(ws.computations[cited].sources)
        return [cited]

    def cut(self, answer, ws) -> None:
        lines = answer.answer_markdown.splitlines()
        for n, line in enumerate(lines):
            if self.number not in line:
                continue
            held = [s for c in cited_ids(line) for s in self.behind(ws, c)
                    if s in ws.sources and self.number in ws.sources[s].text]
            # a table of the turn that states the number is the one cited when the model read the table itself
            tables = [sid for sid, s in ws.sources.items() if self.number in s.text and s.kind in ("table", "table_row")]
            if not held and not tables:
                continue
            sid = (tables or held)[0]
            source = ws.sources[sid]
            row = [ln for ln in source.text.splitlines() if self.number in ln][-1]
            row = " | ".join(cell.split(": ", 1)[-1] for cell in row.split(" | "))
            source.text = f"[עמוד {self.page}]\n{row}"
            lines[n] = f"{self.claim} [{sid}]."
            answer.answer_markdown = "\n".join(lines)
            self.applied = True
            return


# --- environment ---------------------------------------------------------------------------------------------------

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
def probe(monkeypatch):
    PROBE.reset()
    monkeypatch.setattr(verify, "judge", PROBE.judge(verify.judge))
    monkeypatch.setattr(verify, "unit_claims", PROBE.unit_claims(verify.unit_claims))
    monkeypatch.setattr(final_check, "unit_claims", PROBE.unit_claims(final_check.unit_claims))
    yield PROBE


@pytest.fixture
def office(db, monkeypatch, real_key, probe):
    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    o = make_office(db, "משרד בדיקה סינתטי", "admin-r9@example.test")
    with tenant_tx(o.ctx()) as conn:
        conn.execute(text("UPDATE office_settings SET cloud_llm_enabled = true"))
    return o


@pytest.fixture
def chat(client, office):
    login(client, "admin-r9@example.test")
    return client


def ingest(chat, office, monkeypatch, key: str) -> str:
    """A round-9 fixture ingested through the worker (the units are derived by its jobs) without OCR or vision and
    without the measurement pass: text-layer PDFs only. Returns the document id."""
    from app import worker
    from app.extraction import ocr
    from app.platform import pipeline
    from tests.helpers import upload

    path = FIXTURES / MANIFEST[key]["file"]
    with monkeypatch.context() as m:
        m.setattr(ocr, "ocr_available", lambda languages: False)
        m.setattr(pipeline, "vision_reader", lambda ctx: None)
        m.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
        r = upload(chat, [(path.name, path.read_bytes())], group_id=office.default_group_id)
        assert r.status_code == 200, r.text
        while worker.run_one("round9-real-model"):
            pass
    return r.json()["results"][0]["document_id"]


# --- turns and records ---------------------------------------------------------------------------------------------

def new_conversation(c) -> str:
    r = c.post("/api/chat/conversations")
    assert r.status_code == 200, r.text
    return r.json()["id"]


def summary(m: dict, question: str, wall: float, probe_data: dict) -> dict:
    usage = m.get("usage") or []
    a = m.get("answer") or {}
    total = lambda k: sum(u.get(k) or 0 for u in usage)  # noqa: E731
    cost = [u.get("cost_usd") for u in usage]
    purposes: dict[str, int] = {}
    statuses: dict[str, int] = {}
    agent: dict[str, int] = {}
    for u in usage:
        purposes[u["purpose"]] = purposes.get(u["purpose"], 0) + 1
        statuses[u.get("status")] = statuses.get(u.get("status"), 0) + 1
        if u["purpose"] == "agent":
            agent[u.get("status")] = agent.get(u.get("status"), 0) + 1
    v = a.get("verification") or {}
    return {
        "question": question, "message_id": m.get("id"), "message_status": m.get("status"),
        "answer_status": a.get("status"), "calls": len(usage), "purposes": purposes, "usage_statuses": statuses,
        "agent_statuses": agent,
        "input_tokens": total("input_tokens"), "cached_input_tokens": total("cached_input_tokens"),
        "output_tokens": total("output_tokens"),
        "cost_usd": round(sum(c for c in cost if c is not None), 6) if any(c is not None for c in cost) else None,
        "model_latency_ms": total("latency_ms"), "wall_s": round(wall, 1), "probe": probe_data,
        "limits_hit": a.get("limits_hit"),
        "unit": {k: (a.get("unit") or {}).get(k) for k in ("unit_no", "display", "pages")} if a.get("unit") else None,
        "verification": {k: v.get(k) for k in ("judged", "judge_status", "removed", "partial", "annotated",
                                               "final_digest", "actions", "removals", "completeness")},
        "gaps": [{k: g.get(k) for k in ("reason", "components", "texts", "text")} for g in a.get("gaps") or []],
        "sources": [{"id": s.get("id"), "pages": anchor_pages(s), "unit": (s.get("unit") or {}).get("no")}
                    for s in a.get("sources") or []],
        "markdown": a.get("markdown"),
    }


def flush() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE.write_text(json.dumps({"model": "gpt-6-luna (settings)", "cases": RESULTS}, ensure_ascii=False,
                                       indent=2, default=str), encoding="utf-8")


def ask(c, cid: str, question: str, record: dict, status: str = "done") -> dict:
    PROBE.take()
    started = time.perf_counter()
    r = c.post(f"/api/chat/conversations/{cid}/messages", json={"content": question})
    assert r.status_code == 200, r.text
    mid = r.json()["assistant"]["id"]
    m = c.get(f"/api/chat/messages/{mid}").json()
    record["turns"].append(summary(m, question, time.perf_counter() - started, PROBE.take()))
    record["message_id"], record["conversation_id"] = mid, cid
    d = c.get(f"/api/chat/messages/{mid}/diagnostics")  # kept with the turn, for diagnosis before any assertion
    if d.status_code == 200:
        record["rounds"] = d.json().get("rounds")
        record["log"] = [{k: e.get(k) for k in ("claim_id", "action", "text", "remaining", "verdict", "failure_kind",
                                                 "cause", "reason", "post_check", "basis", "rereads", "ordinal")}
                         for e in d.json()["removed"]]
    flush()
    assert m["status"] == status, (m.get("status"), m.get("error"))
    return m["answer"]


def run_case(name: str, chat, body, attempts: int = ATTEMPTS) -> None:
    """``body(chat, conversation_id, record)`` in a new conversation per attempt, up to ``attempts``: passes on the first
    attempt that passes; every attempt is recorded with its outcome."""
    last: AssertionError | None = None
    for n in range(1, attempts + 1):
        record = {"case": name, "attempt": n, "turns": [], "outcome": None, "failure": None, "notes": []}
        RESULTS.append(record)
        try:
            body(chat, new_conversation(chat), record)
        except AssertionError as exc:
            record["outcome"], record["failure"] = "fail", str(exc)[:3000]
            last = exc
            flush()
            continue
        record["outcome"] = "pass"
        flush()
        return
    assert last is not None
    raise last


class Phrasings:
    """Every phrasing of a case (``run_case``); fails at the end with each failed one, so one phrasing's failure never
    hides another's outcome."""

    def __init__(self) -> None:
        self.failed: list[str] = []

    def run(self, name: str, chat, body, attempts: int = ATTEMPTS) -> None:
        try:
            run_case(name, chat, body, attempts)
        except AssertionError as exc:
            self.failed.append(f"{name}: {str(exc)[:600]}")

    def check(self) -> None:
        assert not self.failed, "\n".join(self.failed)


# --- reading an answer ---------------------------------------------------------------------------------------------

def cited_ids(markdown: str) -> list[str]:
    out: list[str] = []
    for m in re.finditer(r"\[((?:[SMCVA]\d+)(?:\s*[,،;]\s*[SMCVA]\d+)*)\]", markdown or ""):
        for i in re.split(r"\s*[,،;]\s*", m.group(1)):
            if i not in out:
                out.append(i)
    return out


def lines_with(markdown: str, number: str) -> list[str]:
    return [line for line in (markdown or "").splitlines() if number in line]


def anchor_pages(item: dict) -> list[int]:
    anchor = item.get("anchor") or {}
    return [p.get("page") for p in anchor.get("pages") or []]


def items_by_id(a: dict) -> dict[str, dict]:
    return {x["id"]: x for key in ("sources", "values", "measurements", "computations") for x in a.get(key) or []}


def cited_sources(a: dict) -> list[dict]:
    """The sources the answer cites in its text (all the payload's sources when it cites none by id)."""
    by_id = items_by_id(a)
    ids = [i for i in cited_ids(a["markdown"]) if i.startswith("S") and i in by_id]
    return [by_id[i] for i in ids] or [s for s in a.get("sources") or [] if s.get("id")]


def has_number(markdown: str, number: str) -> bool:
    """``number`` as a number of its own, not a part of a longer one (or of an id)."""
    return re.search(rf"(?<![\d,.\-]){re.escape(number)}(?![\d,]|\.\d)", markdown or "") is not None


def log_of(c, record: dict) -> list[dict]:
    """The gated per-claim log of the turn (``GET /messages/{id}/diagnostics``), in the text's order."""
    r = c.get(f"/api/chat/messages/{record['message_id']}/diagnostics")
    assert r.status_code == 200, r.text
    log = r.json()["removed"]
    record["log"] = [{k: e.get(k) for k in ("claim_id", "action", "text", "remaining", "verdict", "failure_kind",
                                             "cause", "reason", "post_check", "basis", "rereads", "ordinal")}
                     for e in log]
    flush()
    return log


def saved_content(office, message_id: str) -> str:
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT content FROM messages WHERE id = :m"), {"m": message_id}).scalar_one()


def assert_saved_is_checked(c, office, record: dict) -> None:
    """The saved text, the answer the API serves and the answer after a refresh are the same text, and its digest is the
    one the final check recorded."""
    mid, cid = record["message_id"], record["conversation_id"]
    served = c.get(f"/api/chat/messages/{mid}").json()
    stored = saved_content(office, mid)
    refreshed = next(m for m in c.get(f"/api/chat/conversations/{cid}/messages").json()["messages"] if m["id"] == mid)
    v = served["answer"]["verification"]
    assert v.get("final_digest") == hashlib.sha256(stored.encode("utf-8")).hexdigest(), v.get("final_digest")
    assert served["content"] == stored == served["answer"]["markdown"], "the served text is not the saved text"
    assert refreshed["content"] == stored == refreshed["answer"]["markdown"], "the refreshed text differs"


def assert_inside_unit(a: dict, unit_no: int, pages: set[int]) -> list[dict]:
    """Every source the answer cites lies in the unit's page range and carries the unit in its payload."""
    sources = cited_sources(a)
    assert sources, a["markdown"]
    for s in sources:
        assert set(anchor_pages(s)) <= pages, (s.get("id"), anchor_pages(s), sorted(pages))
        if s["id"].startswith("S"):
            assert (s.get("unit") or {}).get("no") == unit_no, (s.get("id"), s.get("unit"))
    return sources


# --- the cases -----------------------------------------------------------------------------------------------------

def test_ae2_a_question_naming_the_second_project_is_answered_from_its_pages_alone(chat, office, monkeypatch):
    """AE2 (R6, R7, R12, R13; U9). The second appraisal is named by its project name, which is in no file title: every
    cited source lies in that appraisal's page range, carries its unit in the payload, and no comparable of the appendix
    is presented as the asked property."""
    ingest(chat, office, monkeypatch, "compendium")

    def body(c, cid, record):
        a = ask(c, cid, f"מה שווי הנכס בשומה של {SECOND_NAME}?", record)
        md = a["markdown"]
        assert a["status"] != "clarification", md
        assert has_number(md, SECOND_VALUE), md
        sources = assert_inside_unit(a, 2, SECOND_PAGES)
        unit = a.get("unit") or {}
        assert (unit.get("unit_no"), unit.get("pages")) == (2, [UNITS[1]["first_page"], UNITS[1]["last_page"]]), unit
        assert not [n for n in APPENDIX_NAMES if n in md], [n for n in APPENDIX_NAMES if n in md]
        for first_or_third in (FACTS["first_value"]["value"], FACTS["third_value"]["value"]):
            assert first_or_third not in md, md
        record["notes"].append(f"cited sources: {[(s['id'], anchor_pages(s)) for s in sources]}")

    run_case("ae2/second-project-by-name", chat, body)


def test_ae3_terms_split_between_two_appraisals_are_not_answered_as_if_one_holds_both(chat, office, monkeypatch):
    """AE3 (R11; U9). One term is the first appraisal's, the other the third's: the answer asks which appraisal is meant
    or says that no single appraisal holds both. It never gives one appraisal's figure as the answer for both."""
    ingest(chat, office, monkeypatch, "compendium")
    one, three = UNITS[0]["aliases"][1], UNITS[2]["aliases"][0]  # "האלון" and "נחל הדס"
    phrasings = [f"מה שווי הנכס בשומה שמופיעים בה גם {one} וגם {three}?",
                 f"באיזו שומה בקובץ מופיעות יחד {one} ו{three}, ומה מספר הגוש שלה?"]
    cases = Phrasings()
    for i, q in enumerate(phrasings, 1):
        def body(c, cid, record, q=q):
            a = ask(c, cid, q, record)
            md = a["markdown"]
            assert a["status"] == "clarification" or NO_SINGLE.search(md), md  # it asks which one, or says none holds both
            both = [v for v in (FACTS["first_value"]["value"], FACTS["third_value"]["value"]) if v in md]
            assert len(both) < 2 or NO_SINGLE.search(md), md  # both figures only beside the statement that they differ
            units = {(s.get("unit") or {}).get("no") for s in a.get("sources") or [] if s.get("unit")}
            record["notes"].append(f"status {a['status']}; units of the sources: {sorted(x for x in units if x)}")

        cases.run(f"ae3/split-terms/{i}", chat, body)
    cases.check()


def test_ae4_similar_names_get_a_clarification_listing_both_and_the_reply_picks_the_unit(chat, office, monkeypatch):
    """AE4 (R13; U9). Words that fit both of R9b's appraisals ("מתחם הגפן") give a clarification listing both with what
    tells them apart; the reply "השני" — or the distinguishing letter — answers from the second. On the first message
    and after a first turn alike, the clarification is the server's (units that tie on name fit are asked about before
    any model call): it names both and gives each something beside the title — its pages and identifiers."""
    ingest(chat, office, monkeypatch, "similar_names")
    alef, bet = SIMILAR["units"][0], SIMILAR["units"][1]
    question = "מה דמי השכירות החודשיים במתחם הגפן?"
    prime = "כמה שומות יש בקובץ R9b synthetic two similar names, ומהם שמותיהן?"
    follow = "ומה דמי השכירות החודשיים במתחם הגפן?"
    details = (("עמוד 1", "הגפן 5", ALEF_RENT), ("עמוד 2", "הגפן 9", BET_RENT))

    def clarified(c, cid, record, first: str | None):
        if first:
            ask(c, cid, first, record)
        a = ask(c, cid, follow if first else question, record)
        md = a["markdown"]
        assert a["status"] == "clarification", (a["status"], md)
        assert alef["title"] in md and bet["title"] in md, md
        for page, street, figure in details:  # what tells each apart beside its title
            assert page in md or street in md or figure in md, (page, street, figure, md)
        if first:  # the server's clarification: the candidates with their pages and identifiers
            assert "עמוד 1" in md and "עמוד 2" in md and "הגפן 5" in md and "הגפן 9" in md, md
            assert len((a.get("request") or {}).get("candidates") or []) == 2, a.get("request")
        return a

    def reply(words: str, first: str | None = None):
        def body(c, cid, record):
            clarified(c, cid, record, first)
            a = ask(c, cid, words, record)
            md = a["markdown"]
            assert a["status"] != "clarification", md
            assert has_number(md, BET_RENT), md
            assert not has_number(md, ALEF_RENT), md
            for s in cited_sources(a):
                assert (s.get("unit") or {}).get("no") in (2, None) and set(anchor_pages(s)) <= {2}, s
            assert (a.get("unit") or {}).get("unit_no") == 2, a.get("unit")
        return body

    cases = Phrasings()
    cases.run("ae4/first-message-reply-second", chat, reply("השני"))
    cases.run("ae4/first-message-reply-letter", chat, reply("מתחם הגפן ב"))
    cases.run("ae4/after-a-turn-reply-second", chat, reply("השני", prime))
    cases.check()


def test_ae5_the_rent_after_a_price_stays_on_the_same_unit_without_a_clarification(chat, office, monkeypatch):
    """AE5 (R14; U10). A price answer on the second appraisal of R9a, then "ומה דמי השכירות?": the same unit, no
    clarification. The rent is written in the third appraisal only, so the answer either has no source outside the
    second appraisal's pages and says the rent is not stated there, or cites the second's own: never the third's rent."""
    ingest(chat, office, monkeypatch, "compendium")

    def body(c, cid, record):
        first = ask(c, cid, f"מה שווי הנכס בשומה של {SECOND_NAME}?", record)
        assert has_number(first["markdown"], SECOND_VALUE), first["markdown"]
        a = ask(c, cid, "ומה דמי השכירות?", record)
        md = a["markdown"]
        assert a["status"] != "clarification", md
        assert not has_number(md, THIRD_RENT), md  # the third appraisal's rent
        for s in a.get("sources") or []:
            assert set(anchor_pages(s)) <= SECOND_PAGES, (s.get("id"), anchor_pages(s))
            if s.get("unit"):
                assert s["unit"]["no"] == 2, s
        located = ((a.get("request") or {}).get("located_unit")) or a.get("unit") or {}
        assert located.get("unit_no") == 2, (a.get("request"), a.get("unit"))
        assert a.get("gaps") or a["status"] in ("not_found", "partial") or ABSENT.search(md) or cited_sources(a), md
        record["notes"].append(f"status {a['status']}; gaps {len(a.get('gaps') or [])}; "
                               f"sources {[(s['id'], anchor_pages(s)) for s in a.get('sources') or []]}")

    run_case("ae5/rent-after-price", chat, body)


def test_a_claim_that_lacks_its_tables_header_is_re_read_with_the_real_judge_and_kept(chat, office, monkeypatch):
    """KTD5 (R13-R15; U9, U15). R9c's table has its header on page 1 and its total row alone after the break. The sources
    a total's claim cites are cut to the total's own row (``SourceCut``, before the first verification), so the real judge
    cannot tell what the figure is the total of: it asks for the table's headers, the server reads them, and a real
    ``verify_reread`` call (the usage's purpose) judges the claim again; a claim that comes through supported leaves no
    entry in the removal log, so the call itself is the evidence. The claim ends supported (kept, cited) or properly gapped, and is
    never removed as a contradiction."""
    ingest(chat, office, monkeypatch, "picture_title")
    doc = MANIFEST["picture_title"]["title"]  # the file's title: the question names the table's file
    phrasings = [f"במסמך {doc}: מה סך השטח במ״ר של כל היחידות שבטבלה?",
                 f"לפי הטבלה במסמך {doc}, כמה מ״ר בסך הכל יש בכל יחידות הבניין?"]
    cases = Phrasings()
    for i, q in enumerate(phrasings, 1):
        def body(c, cid, record, q=q):
            cut = SourceCut(engine.verify_answer, TOTAL, f"סך השטח של כל היחידות בטבלה הוא {TOTAL} מ״ר",
                           SPLIT_TABLE["total_page"])
            monkeypatch.setattr(engine, "verify_answer", cut)
            try:
                a = ask(c, cid, q, record)
            finally:
                monkeypatch.setattr(engine, "verify_answer", cut.original)
            assert cut.applied, "no line of the model's answer stated the total with a source to cut"
            md = a["markdown"]
            turn = record["turns"][-1]
            log = log_of(c, record)
            rereads = [x for e in log for x in e["rereads"]]
            record["notes"].append(f"verify_reread calls: {turn['purposes'].get('verify_reread', 0)}; rereads: "
                                   f"{[(x.get('scope'), x.get('outcome')) for x in rereads]}")
            assert turn["purposes"].get("verify_reread", 0) > 0, (turn["purposes"], rereads)
            about = [e for e in log if TOTAL in (e["text"] or "")]
            assert not [e for e in about if e["verdict"] == "contradicts" or e["failure_kind"] == "contradicts_source"
                        or e["action"] == "removed" and e["cause"] != "check_failed"], about
            if TOTAL in md:  # supported: kept, with a citation
                assert any(cited_ids(line) for line in lines_with(md, TOTAL)), md
                record["notes"].append("the total is kept and cited")
            else:  # properly gapped: the answer says so, with the server's gap or marker
                assert a.get("gaps") or about, (md, log)
                record["notes"].append(f"the total is gapped: {[(e['action'], e['cause']) for e in about]}")
            assert_saved_is_checked(c, office, record)

        cases.run(f"reread/split-table-total/{i}", chat, body)
    cases.check()


def test_a_computation_still_runs_after_the_tool_budget_and_the_provider_accepts_the_allowed_tools_choice(
        chat, office, monkeypatch):
    """U11 (R15-R18). The turn's tool-output budget is small, so it is spent by the read of the second appraisal's figures.
    The step after it carries an ``allowed_tools`` choice (the compute tools only): the provider accepts it (no API error
    in the usage, the step's status ok), and the computation on the values already read is made (a C# of the answer) or
    the answer gives an honest gap."""
    ingest(chat, office, monkeypatch, "compendium")
    monkeypatch.setattr(get_settings(), "chat_tool_output_chars", 2000)
    area = FACTS["second_area"]["value"]

    def body(c, cid, record):
        fake = record_steps(monkeypatch)
        try:
            a = ask(c, cid, f"מה שווי הנכס ומה שטחו בשומה של {SECOND_NAME}? חשב את השווי למ״ר: חלק את השווי בשטח.",
                    record)
        finally:
            record["agent_steps"] = fake.steps
            record["notes"].append(f"agent steps (choice, status): {[(s['choice'], s['status']) for s in fake.steps]}")
            flush()
        turn = record["turns"][-1]
        assert "tool_budget" in (a.get("limits_hit") or []), a.get("limits_hit")  # the budget was spent by the reading
        after = fake.allowed_tools_steps()
        assert after, fake.steps  # the step after the budget carried the allowed_tools choice
        assert all(set(s["tools"]) == {"calculate", "take_value", "assume"} for s in after), after
        assert all(s["status"] == "ok" for s in after), after  # the provider accepted it
        bad = {k: n for k, n in turn["agent_statuses"].items() if k in API_ERRORS}
        assert not bad, bad  # no API error in the agent calls of the turn's usage
        md = a["markdown"]
        if a.get("computations"):
            assert any(i.startswith("C") for i in cited_ids(md)), md
            record["notes"].append(f"computations: {[c_.get('id') for c_ in a['computations']]}")
        else:  # the budget left no room for the inputs: the answer says what was not read
            assert a.get("gaps") or a["status"] in ("partial", "not_found"), (a["status"], md)
            record["notes"].append("no calculation: an honest gap")
        record["notes"].append(f"area {area}: stated {has_number(md, area)}")

    run_case("u11/compute-after-tool-budget", chat, body)


def test_a_file_that_was_not_read_is_named_with_that_state_never_called_not_found(chat, office, monkeypatch):
    """U6 (KTD11; R5, R18, R25). A version inserted as ``failed`` with a page-limit reason is a visible file nobody can
    search. A question whose words are its title's gets an answer that names the file and says it was not read; the answer
    never says it was not found."""
    from tests.integration.test_chat_coverage import add_doc
    from tests.integration.test_chat_unread_documents import add_unread

    add_doc(office, office.default_group_id, READY_TITLE, ["שומה לנכס ברמת השרון: השווי למ\"ר בנוי הוא 9,500 ₪."], "1" * 64)
    add_unread(office, office.default_group_id, FAILED_TITLE, "failed", TOO_LONG)

    def body(c, cid, record):
        a = ask(c, cid, "מה שווי הנכס בשומה של הזית 9 נווה ים?", record)
        md = a["markdown"]
        assert "הזית 9" in md, md  # the file is named
        assert NOT_READ.search(md), md  # and said not to have been read
        assert not NOT_FOUND.search(md), NOT_FOUND.search(md).group(0)
        assert not [g for g in a.get("gaps") or [] if g.get("reason") in ("not_found_search", "not_in_documents")], a["gaps"]
        reasons = [g.get("reason") for g in a.get("gaps") or []]
        record["notes"].append(f"status {a['status']}; gap reasons {reasons}; the reason stated: {'1,000' in md}")
        assert_saved_is_checked(c, office, record)

    run_case("u6/unread-file-named", chat, body)


# --- the summary -----------------------------------------------------------------------------------------------------

def build_summary() -> dict:
    turns = [t for r in RESULTS for t in r.get("turns") or []]
    probe = [t["probe"] for t in turns if t.get("probe")]
    exact = sum(p["claims_exact"] for p in probe)
    fallback = sum(p["claims_fallback"] for p in probe)
    units = sum(p["units_with_claims"] for p in probe)
    units_fb = sum(p["units_fallback"] for p in probe)
    purposes: dict[str, int] = {}
    statuses: dict[str, int] = {}
    for t in turns:
        for k, n in t["purposes"].items():
            purposes[k] = purposes.get(k, 0) + n
        for k, n in t.get("usage_statuses", {}).items():
            statuses[k] = statuses.get(k, 0) + n
    judge_calls: dict[str, int] = {}
    for p in probe:
        for k, n in p["judge_calls"].items():
            judge_calls[k] = judge_calls.get(k, 0) + n
    cases: dict[str, list[str]] = {}
    for r in RESULTS:
        cases.setdefault(r["case"], []).append(f"{r['attempt']}:{r['outcome']}")
    steps = [s for r in RESULTS for s in r.get("agent_steps") or []]
    allowed = [s for s in steps if s["choice"] == "allowed_tools"]
    costs = [t["cost_usd"] for t in turns if t.get("cost_usd") is not None]
    return {
        "model": "gpt-6-luna (settings)", "turns": len(turns), "attempts": len(RESULTS),
        "cases": {k: {"attempts": v, "passed": any(a.endswith(":pass") for a in v)} for k, v in cases.items()},
        "verify_reread_calls": purposes.get("verify_reread", 0), "verify_final_calls": purposes.get("verify_final", 0),
        "verify_calls": purposes.get("verify", 0), "usage_purposes": purposes, "usage_statuses": statuses,
        "judge_calls_probe": judge_calls,
        "allowed_tools": {"steps": len(allowed), "accepted": sum(1 for s in allowed if s["status"] == "ok"),
                          "statuses": sorted({s["status"] for s in allowed})},
        "span_validity": {"claims_exact": exact, "claims_fallback": fallback,
                          "claim_rate": round(exact / (exact + fallback), 4) if exact + fallback else None,
                          "units_with_claims": units, "units_fallback": units_fb},
        "incomplete_replies": sum(p["incomplete"] for p in probe), "halvings": sum(p["halved"] for p in probe),
        "cost_usd": round(sum(costs), 4) if costs else None,
        "model_latency_s": round(sum(t["model_latency_ms"] for t in turns) / 1000, 1),
        "wall_s": round(time.perf_counter() - STARTED, 1),
    }


def teardown_module(module) -> None:  # noqa: ARG001
    if RESULTS:
        flush()
        SUMMARY_FILE.write_text(json.dumps(build_summary(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nreal_model results: {os.path.relpath(RESULTS_FILE, ROOT)}\nsummary: {os.path.relpath(SUMMARY_FILE, ROOT)}")
