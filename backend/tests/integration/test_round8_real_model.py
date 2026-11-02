"""Round 8 acceptance with the real model (U9, AE1-AE8): claim-level verification and the verified final text on the
full path. The synthetic round-8 fixtures (``tests/fixtures/round8``) and two round-7 ones (``tests/fixtures/round7``) —
every name, address, plan, block, parcel and amount invented — are ingested into a synthetic office of the ``rag_test``
database, and questions go through the chat API to the configured model and judge (``gpt-6-luna`` with the repository's
settings), whose key comes from the repository's ``.env`` and is never printed.

Each acceptance example is asserted by behaviour — what the saved answer states, what the log and the rows say, the
digest of the saved text — never by exact wording. The model is nondeterministic: each case may run a bounded second
attempt (in a new conversation), every attempt's outcome is recorded, and no assertion is loosened to pass.

Where an example needs a defect that the real model does not produce on demand (a datum without its citation, a
sentence that mixes a supported figure with an unsupported date, a figure from the wrong heading), the model's first
final answer is edited the way ``test_round7_real_model.Misattributing`` does (``Tampering``), and everything after it —
the real judge, the real re-read, the real final check — is the real path. A case that needs the judge to fail injects a
failure into the final check's judge calls only (``Probe.fail_final``).

Per turn, the model calls, tokens, cost and latency (the message's ``usage``) and the wall time are recorded to a JSON file
under ``real_documents/eval/round8/u9-real-model/`` (gitignored, private: answers are kept for diagnosis), with a summary
file next to it: the span-validity rate (the judge's claims whose span was an exact part of its unit against the
whole-unit fallbacks), the ``verify_final`` and ``verify_reread`` call counts and how often a judge reply was incomplete
and halved.

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

from app.chat import final_check, verify
from app.chat.compose import MARKER
from app.db import tenant_tx
from app.providers import llm
from app.providers.llm import Purpose
from tests.conftest import login
from tests.factories import make_office

pytestmark = [pytest.mark.real_model, pytest.mark.db]

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MANIFESTS = {name: json.loads((FIXTURES / name / "manifest.json").read_text(encoding="utf-8"))["documents"]
             for name in ("round7", "round8")}
RESULTS_DIR = ROOT / "real_documents" / "eval" / "round8" / "u9-real-model"
STAMP = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
RESULTS_FILE = RESULTS_DIR / f"results-{STAMP}.json"
SUMMARY_FILE = RESULTS_DIR / f"summary-{STAMP}.json"
RESULTS: list[dict] = []
ATTEMPTS = 2  # a bounded second attempt per case, in a new conversation
STARTED = time.perf_counter()

CUT_TEXT = "חלק שלא אומת הושמט"  # the words of the marker a cut leaves (``compose.MARKER``)
REMOVAL_ACTIONS = ("removed", "withheld")
DATE = re.compile(r"\d{1,2}[./]\d{1,2}[./]\d{2,4}")
NEGATION = re.compile(r"\bלא\b|\bאין\b|\bללא\b|\bחסר|\bאינו\b|\bאינה\b")
APPROVAL = re.compile(r"אושר|אישור|מאושר")
INJECTED_DATE = "01/01/2020"
# a "1,200 מ״ר" in any spelling of the unit
AREA_1 = re.compile(r"1,200\s*(?:מ.{0,2}ר|מטר)")
CITATION = re.compile(r"\s*\[(?:[SMCVA]\d+)(?:\s*[,،;]\s*[SMCVA]\d+)*\]")


def facts(doc_set: str, doc: str) -> dict:
    return MANIFESTS[doc_set][doc]["facts"]


# --- instrumentation: counters read at the real boundaries; nothing in the application is changed ----------------------

class Probe:
    """Counts what the verification does, from outside: the judge's claims whose span the server found in its unit
    (``unit_claims`` wrapped) against the whole-unit fallbacks, every judge call by usage purpose, and the incomplete
    replies a batch was halved for (``verify.judge`` wrapped; ``_judge_batch`` splits a truncated reply of a batch of
    two units or more). With ``fail_final`` the final check's judge calls fail (a technical failure, never a verdict)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.fail_final = False
        self.reset()

    def reset(self) -> None:
        self.data = {"claims_exact": 0, "claims_fallback": 0, "units_with_claims": 0, "units_fallback": 0,
                     "fallback_reasons": {}, "judge_calls": {}, "incomplete": 0, "halved": 0, "injected_failures": 0}

    def take(self) -> dict:
        with self.lock:
            out, self.data = self.data, {}
            self.reset()
            return out

    def add(self, key: str, n: int = 1) -> None:
        with self.lock:
            self.data[key] += n

    def judge(self, original):
        def wrapped(provider, batch, rendered, usage, deadline=None, coverage=None, *, policy="", label="verify"):
            if self.fail_final and label == "verify_final":
                with self.lock:
                    self.data["injected_failures"] += 1
                    self.data["judge_calls"][label] = self.data["judge_calls"].get(label, 0) + 1
                return {}, "error", []
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


# --- environment ---------------------------------------------------------------------------------------------------

@pytest.fixture
def real_key(monkeypatch):
    """The repository's model key on this process's settings (never printed); skip without one."""
    from app.config import Settings, get_settings

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
    PROBE.fail_final = False
    PROBE.reset()
    monkeypatch.setattr(verify, "judge", PROBE.judge(verify.judge))
    monkeypatch.setattr(verify, "unit_claims", PROBE.unit_claims(verify.unit_claims))
    monkeypatch.setattr(final_check, "unit_claims", PROBE.unit_claims(final_check.unit_claims))
    yield PROBE
    PROBE.fail_final = False


@pytest.fixture
def office(db, monkeypatch, real_key, probe):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    o = make_office(db, "משרד בדיקה סינתטי", "admin-u9r8@example.test")
    with tenant_tx(o.ctx()) as conn:
        conn.execute(text("UPDATE office_settings SET cloud_llm_enabled = true"))
    return o


@pytest.fixture
def chat(client, office):
    login(client, "admin-u9r8@example.test")
    return client


def ingest(office, monkeypatch, doc_set: str, key: str) -> str:
    """A fixture ingested through the extraction pipeline without OCR or vision and without the measurement pass (as the
    round-7 real-model test does): text-layer PDFs only."""
    from app.extraction import ocr
    from app.platform import pipeline
    from tests.integration.test_documents_api import ingest as ingest_file

    spec = MANIFESTS[doc_set][key]
    with monkeypatch.context() as m:
        m.setattr(ocr, "ocr_available", lambda languages: False)
        m.setattr(pipeline, "vision_reader", lambda ctx: None)
        m.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
        doc, _ = ingest_file(office, FIXTURES / doc_set / Path(spec["file"]).name, spec["title"])
    return doc


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
    for u in usage:
        purposes[u["purpose"]] = purposes.get(u["purpose"], 0) + 1
    v = a.get("verification") or {}
    return {
        "question": question, "message_id": m.get("id"), "message_status": m.get("status"),
        "answer_status": a.get("status"), "calls": len(usage), "purposes": purposes,
        "input_tokens": total("input_tokens"), "cached_input_tokens": total("cached_input_tokens"),
        "output_tokens": total("output_tokens"),
        "cost_usd": round(sum(c for c in cost if c is not None), 6) if any(c is not None for c in cost) else None,
        "model_latency_ms": total("latency_ms"), "wall_s": round(wall, 1), "probe": probe_data,
        "verification": {k: v.get(k) for k in ("judged", "judge_status", "removed", "partial", "annotated",
                                               "final_digest", "actions", "removals", "completeness")},
        "gaps": [{k: g.get(k) for k in ("reason", "components", "texts", "text")} for g in a.get("gaps") or []],
        "markdown": a.get("markdown"),
    }


def flush() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE.write_text(json.dumps({"model": "gpt-6-luna (settings)", "cases": RESULTS}, ensure_ascii=False,
                                       indent=2, default=str), encoding="utf-8")


def ask(c, cid: str, question: str, record: dict) -> dict:
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
    assert m["status"] == "done", (m.get("status"), m.get("error"))
    return m["answer"]


def run_case(name: str, chat, body, attempts: int = ATTEMPTS) -> None:
    """``body(chat, conversation_id, record)`` in a new conversation per attempt, up to ``attempts``: passes on the first
    attempt that passes; every attempt is recorded with its outcome."""
    last: AssertionError | None = None
    for n in range(1, attempts + 1):
        record = {"case": name, "attempt": n, "turns": [], "outcome": None, "failure": None, "notes": []}
        RESULTS.append(record)
        PROBE.fail_final = False
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


# --- editing the model's first final answer (a defect the model does not produce on demand) ------------------------------

class Tampering:
    """The real model on every call, except that the first final answer ``edit`` changes is replaced by the edited one:
    a defect for the real verification to meet on the full path. ``applied`` — it happened."""

    name = "openai"
    routed = False  # serves every purpose itself, each through the purpose's configured provider

    def __init__(self, edit) -> None:
        self.edit, self.applied = edit, False
        self.model = llm.get_provider(Purpose.AGENT).model

    def structured(self, purpose, *args, **kw):
        return llm.get_provider(purpose).structured(purpose, *args, **kw)

    def structured_image(self, purpose, *args, **kw):
        return llm.get_provider(purpose).structured_image(purpose, *args, **kw)

    def agent_step(self, *args, **kw):
        step = llm.get_provider(Purpose.AGENT).agent_step(*args, **kw)
        if step.final is not None and not self.applied:
            markdown = step.final.get("answer_markdown") or ""
            edited = self.edit(markdown)
            if edited and edited != markdown:
                step.final = {**step.final, "answer_markdown": edited}
                self.applied = True
        return step


def tamper(monkeypatch, edit) -> Tampering:
    fake = Tampering(edit)
    monkeypatch.setattr(llm, "get_selected_provider", lambda: fake)
    return fake


def add_approval_date(markdown: str) -> str:
    """The supported area with an approval date the document never states (AE1's sentence)."""
    return AREA_1.sub(lambda m: f"{m.group(0)}, שאושר ביום {INJECTED_DATE}", markdown, count=1)


def strip_citations(markdown: str) -> str:
    return CITATION.sub("", markdown)


def swap_once(old: str, new: str):
    return lambda markdown: markdown.replace(old, new, 1) if old in markdown else None


# --- reading an answer ---------------------------------------------------------------------------------------------

def clauses(markdown: str) -> list[str]:
    """The answer cut into sentences, lines and clauses (a comma that is followed by a space)."""
    return [c.strip() for c in re.split(r"[.;\n]|(?<=\S),\s", markdown or "") if c.strip()]


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


def cited_item_pages(a: dict, number: str) -> list[set[int]]:
    by_id = items_by_id(a)
    out = []
    for line in lines_with(a["markdown"], number):
        for i in cited_ids(line):
            x = by_id.get(i)
            if x is None:
                continue
            if i.startswith("C"):
                out.append({p for inp in x.get("inputs") or [] for p in anchor_pages(by_id.get(inp.get("id")) or {})})
            else:
                out.append(set(anchor_pages(x)))
    return out


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


def digest(markdown: str) -> str:
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def assert_saved_is_checked(c, office, record: dict) -> None:
    """AE6/AE8: the saved text, the answer the API serves and the answer after a refresh are the same text, and its digest
    is the one the final check recorded."""
    mid, cid = record["message_id"], record["conversation_id"]
    served = c.get(f"/api/chat/messages/{mid}").json()
    stored = saved_content(office, mid)
    refreshed = next(m for m in c.get(f"/api/chat/conversations/{cid}/messages").json()["messages"] if m["id"] == mid)
    v = served["answer"]["verification"]
    assert v.get("final_digest") == digest(stored), (v.get("final_digest"), digest(stored))
    assert served["content"] == stored == served["answer"]["markdown"], "the served text is not the saved text"
    assert refreshed["content"] == stored == refreshed["answer"]["markdown"], "the refreshed text differs"
    assert refreshed["answer"]["verification"]["final_digest"] == v["final_digest"]
    spans = served["answer"].get("server_spans") or []
    assert all(0 <= s["start"] <= s["end"] <= len(stored) for s in spans), spans  # the registry indexes the saved text


def assert_cut_is_accounted(a: dict, log: list[dict]) -> int:
    """A cut leaves a numbered marker in the text, one per cut claim, and the log holds the claim it replaced."""
    markers = len(re.findall(re.escape(CUT_TEXT), a["markdown"]))
    cuts = [e for e in log if e["action"] == "cut"]
    assert markers == len(cuts) == (a["verification"].get("actions") or {}).get("cut", 0), (markers, len(cuts))
    for e in cuts:
        assert e.get("ordinal") and MARKER.format(n=e["ordinal"]) in a["markdown"], e
        assert e["failure_kind"] and e["verdict"] in ("unsupported", "contradicts", "unchecked"), e
    return markers


def approval_date_asserted(markdown: str) -> list[str]:
    """Clauses that state a date as the approval's, without negating it."""
    return [c for c in clauses(markdown) if DATE.search(c) and APPROVAL.search(c) and not NEGATION.search(c)]


# --- the cases -----------------------------------------------------------------------------------------------------

AREAS = facts("round8", "plot_areas")
REPORT_DATE = AREAS["report_date"]["value"]  # 05/06/2024: the date of the report, never an approval's
Q_AREA_DATE = "מהו השטח העיקרי בתא שטח 1 לפי המצב המאושר, ומתי אושר? ענה במשפט אחד."


def test_ae1_the_supported_area_survives_and_nothing_unsupported_about_its_date_is_asserted(chat, office, monkeypatch):
    """AE1 (R8, R9, R10). The document gives plot 1's main area and no approval date (and states the report's own date).
    First the model's own answer; then the model's answer with an approval date written into the area sentence, so that the
    mixed sentence is certain: the area stays with its plot and unit, the date clause is cut with the server's marker, the
    log says the date was not supported, and the edited sentence was verified."""
    ingest(office, monkeypatch, "round8", "plot_areas")

    def natural(c, cid, record):
        a = ask(c, cid, Q_AREA_DATE, record)
        md = a["markdown"]
        assert AREA_1.search(md), md
        assert not approval_date_asserted(md), approval_date_asserted(md)
        log = log_of(c, record)
        record["notes"].append(f"cuts: {assert_cut_is_accounted(a, log)}; actions: {a['verification'].get('actions')}")
        assert_saved_is_checked(c, office, record)

    def injected(c, cid, record):
        fake = tamper(monkeypatch, add_approval_date)
        a = ask(c, cid, Q_AREA_DATE, record)
        assert fake.applied, "the model's first answer did not state the area (nothing to edit)"
        md = a["markdown"]
        assert AREA_1.search(md), md  # the supported area is kept, with its unit
        assert "תא שטח 1" in md or "1,200" in md
        assert INJECTED_DATE not in md and not approval_date_asserted(md), md
        log = log_of(c, record)
        # the first verification found the date unsupported (the diagnostics' first round names it)
        caught = [p for rnd in record.get("rounds") or [] for p in rnd if INJECTED_DATE in (p.get("text") or "")]
        assert caught and caught[0]["failure_kind"] == "absent_from_source", record.get("rounds")
        dated = [e for e in log if INJECTED_DATE in (e["text"] or "")]
        if dated:  # a cut: the server's marker is in the text, and the log says the date was not supported
            assert dated[0]["action"] == "cut", dated[0]
            assert_cut_is_accounted(a, log)
            assert CUT_TEXT in md, md
            assert dated[0]["failure_kind"] == "absent_from_source", dated[0]
        else:  # put right by the repair round instead: the date is gone from the text, which is what is asserted
            record["notes"].append(f"no cut: handled as {[e['action'] for e in log]} after a repair round")
        verified = record["turns"][-1]["purposes"].get("verify_final", 0)
        if dated:
            assert verified >= 1 or dated[0]["post_check"] not in ("n/a", "", None), (verified, dated[0])  # edit checked
        assert_saved_is_checked(c, office, record)

    cases = Phrasings()
    cases.run("ae1/natural", chat, natural)
    cases.run("ae1/date-clause-injected", chat, injected)
    cases.check()


def test_ae2_a_datum_without_a_citation_is_cited_never_removed_for_lacking_one(chat, office, monkeypatch):
    """AE2 (R2). The model's first answer states both areas without any citation. The server cites them from the sources
    the turn read (or the repair round does); neither datum is removed or withheld, and no removal says the source lacks
    a figure that the source states."""
    ingest(office, monkeypatch, "round8", "plot_areas")
    stated = {"1,200": "תא שטח 1", "950": "תא שטח 2"}

    def body(c, cid, record):
        fake = tamper(monkeypatch, strip_citations)
        a = ask(c, cid, "מהו השטח העיקרי בכל אחד משני תאי השטח לפי המצב המאושר? ענה בשורה נפרדת לכל תא שטח.", record)
        assert fake.applied, "the model's first answer carried no citation (nothing to strip)"
        md = a["markdown"]
        log = log_of(c, record)
        for number in stated:
            assert number in md, (number, md)
            assert any(cited_ids(line) for line in lines_with(md, number)), (number, md)  # cited, by the server or the model
        removed = [e for e in log if e["action"] in REMOVAL_ACTIONS and any(n in (e["text"] or "") for n in stated)]
        assert not removed, [(e["action"], e["failure_kind"], e["text"]) for e in removed]
        assert not [e for e in log if e["failure_kind"] == "absent_from_source" and e["action"] in REMOVAL_ACTIONS
                    and any(n in (e["text"] or "") for n in stated)]
        cited = a["verification"].get("actions", {}).get("cited", 0)
        record["notes"].append(f"cited by the server: {cited}; actions: {a['verification'].get('actions')}")
        assert_saved_is_checked(c, office, record)

    run_case("ae2/uncited-datum", chat, body)


def test_ae3_a_failed_final_check_withholds_the_changed_claim_and_never_calls_it_wrong(chat, office, monkeypatch):
    """AE3 (R14, R19). The real judge answers the first verification; the judge calls of the final check (and their one
    retry) fail. The sentence the server changed (the date clause cut) cannot be checked, so it is withheld with the cause
    ``check_failed``: not contradicted, not missing. The unchanged line stays."""
    ingest(office, monkeypatch, "round8", "plot_areas")

    def body(c, cid, record):
        fake = tamper(monkeypatch, add_approval_date)
        PROBE.fail_final = True
        a = ask(c, cid, "מהו השטח העיקרי בתא שטח 1, ומתי אושר? ומהו השטח העיקרי בתא שטח 2? ענה בשורה נפרדת לכל תא שטח.",
                record)
        injected = record["turns"][-1]["probe"]["injected_failures"]
        assert fake.applied, "the model's first answer did not state plot 1's area (nothing to edit)"
        assert injected >= 1, "no final-check judge call was made (nothing to fail)"
        md = a["markdown"]
        log = log_of(c, record)
        withheld = [e for e in log if e["action"] == "withheld"]
        assert withheld and all(e["cause"] == "check_failed" for e in withheld), [(e["action"], e["cause"]) for e in log]
        assert not [e for e in log if e["verdict"] == "contradicts" or e["failure_kind"] == "contradicts_source"], log
        assert INJECTED_DATE not in md, md
        rows = a["verification"].get("removals") or []
        assert rows and all(r["failure_kind"] == "not_checked" for r in rows if r.get("action") == "withheld"), rows
        assert all(r.get("cause") == "check_failed" for r in rows if r.get("action") == "withheld"), rows
        assert "950" in md, md  # the line the check did not have to redo stays
        record["notes"].append(f"final-check judge calls failed: {injected}")
        assert_saved_is_checked(c, office, record)

    run_case("ae3/final-check-fails", chat, body)


def test_ae4_the_proposed_figure_is_never_presented_as_approved(chat, office, monkeypatch):
    """AE4 (R7, R16, R18). The same plot has coverage 40% under the approved heading and 55% in a table under the proposed
    heading, whose sentences never say "proposed" themselves. Whatever the question, a clause stating 55 says it is the
    proposed state (or the claim is gone), and a clause stating 40 does not call it proposed. Then the model's own
    approved figure is replaced by the proposed one: it must be removed, cut or put right."""
    ingest(office, monkeypatch, "round8", "plot_areas")

    def wrongly_attributed(md: str) -> list[str]:
        bad = []
        for c in clauses(md):
            if "55" in c and re.search(r"מאושר|שאושר|אושרה", c) and not re.search(r"מוצע|הוצע|הצעה|טרם", c):
                bad.append(c)
            if re.search(r"(?<![\d.,])40\s*%", c) and re.search(r"מוצע|הוצע|הצעה", c) and not re.search(r"מאושר", c):
                bad.append(c)
        return bad

    asks = [("מה התכסית בתא שטח 1 לפי המצב המוצע ולפי המצב המאושר?", True, True),
            ("מה התכסית בתא שטח 1?", None, None),
            ("מהי התכסית שאושרה בתא שטח 1?", False, True)]
    cases = Phrasings()
    for i, (question, proposed_expected, approved_expected) in enumerate(asks, 1):
        def body(c, cid, record, question=question, proposed_expected=proposed_expected,
                 approved_expected=approved_expected):
            a = ask(c, cid, question, record)
            md = a["markdown"]
            assert not wrongly_attributed(md), (wrongly_attributed(md), md)
            if proposed_expected is False:
                assert not [c_ for c_ in clauses(md) if "55" in c_ and not re.search(r"מוצע|הוצע|הצעה|טרם", c_)], md
            if approved_expected:
                assert "40" in md, md
            assert_saved_is_checked(c, office, record)
        cases.run(f"ae4/heading-split/{i}", chat, body)

    def injected(c, cid, record):
        fake = tamper(monkeypatch, swap_once("40%", "55%"))
        a = ask(c, cid, "מהי התכסית שאושרה בתא שטח 1?", record)
        assert fake.applied, "the model's first answer did not state 40% (nothing to swap)"
        md = a["markdown"]
        assert not wrongly_attributed(md), (wrongly_attributed(md), md)
        assert not [c_ for c_ in clauses(md) if "55" in c_ and not re.search(r"מוצע|הוצע|הצעה|טרם", c_)], md
        log = log_of(c, record)
        handled = [e for e in log if "55" in (e["text"] or "")]
        caught = [p for rnd in record.get("rounds") or [] for p in rnd if "55" in (p.get("text") or "")]
        assert handled or caught, (log, record.get("rounds"))  # the wrong claim was met by verification, not kept
        record["notes"].append(f"handled as: {[e['action'] for e in handled]}; caught in round 1: {len(caught)}")
        assert_saved_is_checked(c, office, record)
    cases.run("ae4/approved-figure-swapped", chat, injected)
    cases.check()


def test_ae5_what_rests_on_a_removed_input_is_removed_or_qualified_with_it(chat, office, monkeypatch):
    """AE5 (R11). The model's profit figure is replaced by a wrong one, so the claim stating that input fails; whatever
    claim the log records as resting on it (its ``basis``) is removed, withheld, cut, rewritten or qualified, never kept
    as it stood. A conclusion computed by the calculator from the verified inputs is not touched by this."""
    ingest(office, monkeypatch, "round7", "residual")
    profit, threshold, gap = (facts("round7", "residual")[k]["value"] for k in ("profit_amount", "threshold", "income"))
    wrong = "3,450,000"

    def body(c, cid, record):
        fake = tamper(monkeypatch, swap_once(profit, wrong))
        a = ask(c, cid, "האם הרווח היזמי בתחשיב עולה על הרווח המינימלי הנדרש לכדאיות הפרויקט, ובכמה?", record)
        assert fake.applied, "the model's first answer did not state the profit (nothing to swap)"
        md = a["markdown"]
        assert wrong not in md, md
        log = log_of(c, record)
        by_id = {e["claim_id"]: e for e in log}
        failed_inputs = {e["claim_id"] for e in log if wrong in (e["text"] or "") and e["action"] != "cited"}
        assert failed_inputs, [(e["action"], e["text"]) for e in log]  # the wrong input was handled
        record["notes"].append(f"input claims handled: {[by_id[i]['action'] for i in failed_inputs]}")
        dependents = [e for e in log if e["basis"] in failed_inputs]
        for e in dependents:
            assert e["action"] in ("removed", "withheld", "cut", "rewritten", "qualified"), e
        record["notes"].append(f"dependents: {[(e['action'], e['verdict']) for e in dependents]}; threshold {threshold},"
                               f" income {gap}")
        assert_saved_is_checked(c, office, record)

    run_case("ae5/dependent-on-removed-input", chat, body)


def test_ae6_server_written_text_is_saved_exactly_as_checked(chat, office, monkeypatch):
    """AE6 (R16, R17). A request with one part the documents lack gets the server's gap paragraph after the model's
    answer. The saved text is the checked text: its digest is the recorded one, the API and a refresh serve the same bytes,
    the registry of the server's spans indexes it, and when no model-written sentence changed the final check made no
    judge call."""
    ingest(office, monkeypatch, "round8", "plot_areas")

    def body(c, cid, record):
        a = ask(c, cid, "מהו השטח העיקרי בתא שטח 1 לפי המצב המאושר, ומהו מספר החניות בתא שטח 1?", record)
        md = a["markdown"]
        assert AREA_1.search(md), md
        gaps = a.get("gaps") or []
        assert gaps and all(g["text"] in md for g in gaps), (gaps, md)  # the gap paragraph is in the saved text
        kinds = {s["kind"] for s in a.get("server_spans") or []}
        assert "gap_paragraph" in kinds, kinds
        assert_saved_is_checked(c, office, record)
        actions = a["verification"].get("actions") or {}
        final_calls = record["turns"][-1]["purposes"].get("verify_final", 0)
        if not any(actions.get(k) for k in ("removed", "cut", "rewritten", "withheld")) and not a["verification"]["removed"]:
            assert final_calls == 0, (final_calls, actions)  # only server spans changed: no judge call
        record["notes"].append(f"verify_final calls: {final_calls}; actions: {actions}")

    run_case("ae6/saved-is-checked", chat, body)


def test_ae7_a_partly_found_category_lists_only_its_missing_component(chat, office, monkeypatch):
    """AE7 (R20). Plot 1's main area and plot 2's main and service areas are in the document; plot 1's service area is not.
    Only that component is listed as missing; the area category and the found items are not."""
    ingest(office, monkeypatch, "round8", "plot_areas")
    phrasings = ["מהם השטחים העיקריים ושטחי השירות של תא שטח 1 ושל תא שטח 2 לפי המצב המאושר?",
                 "פרט את שטחי הבנייה בתא שטח 1 ובתא שטח 2 במצב המאושר: שטח עיקרי ושטחי שירות."]
    cases = Phrasings()
    for i, q in enumerate(phrasings, 1):
        def body(c, cid, record, q=q):
            a = ask(c, cid, q, record)
            md = a["markdown"]
            for number in ("1,200", "950", "310"):
                assert number in md, (number, md)
            comps = {x["id"]: x for x in a.get("components") or []}
            parents = {x.get("parent") for x in comps.values() if x.get("parent")}
            gaps = a.get("gaps") or []
            assert gaps, (a.get("components"), md)
            for g in gaps:
                assert not set(g.get("components") or []) & parents, (g, sorted(parents))  # never the category itself
                for t in g.get("texts") or []:
                    assert not re.search(r"עיקרי", t), g  # a found item is not listed as missing
            missing_texts = [t for g in gaps for t in g.get("texts") or []]
            assert any(re.search(r"שירות", t) for t in missing_texts), missing_texts
            assert not any("תא שטח 2" in t and "שירות" in t for t in missing_texts), missing_texts
            assert_saved_is_checked(c, office, record)
        cases.run(f"ae7/one-missing-component/{i}", chat, body)
    cases.check()


def test_ae8_what_is_shown_returned_and_stored_is_one_text_and_each_detail_belongs_to_its_claim(chat, office,
                                                                                              monkeypatch):
    """AE8 (R17, R22, R23). Two claims are cut out of the answer (an approval date the document never states, written
    after each of the two areas). A cut is the server's own edit, so the rows survive without a repair round (a wrong
    figure would be put right by the model's repair round and leave no row). The answer shown, the one the API returns
    and the one after a refresh are identical and carry the same digest, and the rows of the normal path pair with the
    log's entries by claim id, in the text's order, with the same action and kind; nothing of the cut drafts reaches the
    normal path."""
    ingest(office, monkeypatch, "round8", "plot_areas")
    second_date = "02/02/2021"

    def edit(markdown: str) -> str | None:
        edited = add_approval_date(markdown)
        edited = re.sub(r"950\s*(?:מ.{0,2}ר|מטר)", lambda m: f"{m.group(0)}, שאושר ביום {second_date}", edited, count=1)
        return edited if INJECTED_DATE in edited and second_date in edited else None

    def body(c, cid, record):
        fake = tamper(monkeypatch, edit)
        a = ask(c, cid, "מהו השטח העיקרי בכל אחד משני תאי השטח לפי המצב המאושר?", record)
        assert fake.applied, "the model's first answer did not state both areas (nothing to edit)"
        md = a["markdown"]
        assert INJECTED_DATE not in md and second_date not in md, md
        log = log_of(c, record)
        rows = a["verification"].get("removals") or []
        row_log = [e for e in log if e["action"] in verify.ROW_ACTIONS]
        assert len(rows) >= 2 and len(rows) == len(row_log), (rows, [(e["action"], e["text"]) for e in log])
        assert [r["claim_id"] for r in rows] == [e["claim_id"] for e in row_log]  # paired by id, in the text's order
        for r, e in zip(rows, row_log, strict=True):
            assert r["action"] == e["action"] and r["failure_kind"] == e["failure_kind"], (r, e)
            assert r["text"] == verify.REMOVAL_SENTENCES.get(r["failure_kind"]) or r.get("cause"), r
        assert len({r["claim_id"] for r in rows}) == len(rows)
        served = json.dumps(c.get(f"/api/chat/messages/{record['message_id']}").json(), ensure_ascii=False)
        assert INJECTED_DATE not in served and second_date not in served, "a cut draft reached the normal path"
        assert_cut_is_accounted(a, log)
        assert_saved_is_checked(c, office, record)

    run_case("ae8/one-text-claim-linked-details", chat, body)


def test_a_total_on_the_second_page_of_a_table_is_kept_and_its_reread_is_counted(chat, office, monkeypatch):
    """KTD5/KTD10. A table's header is on page 1 and its total row alone on page 2: a claim with the total lacks the
    header it needs, which the bounded re-read supplies. The total is kept and cited; the calls of the re-read are recorded."""
    ingest(office, monkeypatch, "round8", "split_table")
    table = facts("round8", "split_table")["table"]
    assert table["header_page"] == 1 and table["total_page"] == 2
    phrasings = ["מה סך השטח העיקרי בכל תאי השטח לפי הטבלה?",
                 "מהו סה״כ השטח העיקרי שבטבלה, ומהו השטח העיקרי של תא שטח 36?"]
    cases = Phrasings()
    for i, q in enumerate(phrasings, 1):
        def body(c, cid, record, q=q):
            a = ask(c, cid, q, record)
            md = a["markdown"]
            assert table["total"] in md, md
            pages = cited_item_pages(a, table["total"])
            assert pages, md
            # where the citation opens (page 2 is the total's own place): recorded, not asserted — this case counts the
            # re-read; the citation's place is the citations feature's own acceptance
            record["notes"].append(f"pages the total's citation opens: {pages}")
            log = log_of(c, record)
            assert not [e for e in log if e["action"] in REMOVAL_ACTIONS and table["total"] in (e["text"] or "")], log
            turn = record["turns"][-1]
            record["notes"].append(f"verify_reread calls: {turn['purposes'].get('verify_reread', 0)}; rereads in the log: "
                                   f"{[x for e in log for x in e['rereads']]}")
            assert_saved_is_checked(c, office, record)
        cases.run(f"split-table-total/{i}", chat, body)
    cases.check()


# --- the summary -----------------------------------------------------------------------------------------------------

def build_summary() -> dict:
    turns = [t for r in RESULTS for t in r.get("turns") or []]
    probe = [t["probe"] for t in turns if t.get("probe")]
    exact = sum(p["claims_exact"] for p in probe)
    fallback = sum(p["claims_fallback"] for p in probe)
    units = sum(p["units_with_claims"] for p in probe)
    units_fb = sum(p["units_fallback"] for p in probe)
    purposes: dict[str, int] = {}
    for t in turns:
        for k, n in t["purposes"].items():
            purposes[k] = purposes.get(k, 0) + n
    judge_calls: dict[str, int] = {}
    for p in probe:
        for k, n in p["judge_calls"].items():
            judge_calls[k] = judge_calls.get(k, 0) + n
    reasons: dict[str, int] = {}
    for p in probe:
        for k, n in p["fallback_reasons"].items():
            reasons[k] = reasons.get(k, 0) + n
    cases: dict[str, list[str]] = {}
    for r in RESULTS:
        cases.setdefault(r["case"], []).append(f"{r['attempt']}:{r['outcome']}")
    costs = [t["cost_usd"] for t in turns if t.get("cost_usd") is not None]
    return {
        "model": "gpt-6-luna (settings)", "turns": len(turns), "attempts": len(RESULTS),
        "cases": {k: {"attempts": v, "passed": any(a.endswith(":pass") for a in v)} for k, v in cases.items()},
        # the judge's claims with a span the server found in the unit, against the whole-unit fallbacks (KTD1)
        "span_validity": {"claims_exact": exact, "claims_fallback": fallback,
                          "claim_rate": round(exact / (exact + fallback), 4) if exact + fallback else None,
                          "units_with_claims": units, "units_fallback": units_fb,
                          "unit_rate": round(1 - units_fb / units, 4) if units else None,
                          "fallback_reasons": reasons},
        "verify_final_calls": purposes.get("verify_final", 0), "verify_reread_calls": purposes.get("verify_reread", 0),
        "verify_calls": purposes.get("verify", 0), "usage_purposes": purposes, "judge_calls_probe": judge_calls,
        "incomplete_replies": sum(p["incomplete"] for p in probe), "halvings": sum(p["halved"] for p in probe),
        "injected_final_check_failures": sum(p["injected_failures"] for p in probe),
        "cost_usd": round(sum(costs), 4) if costs else None,
        "model_latency_s": round(sum(t["model_latency_ms"] for t in turns) / 1000, 1),
        "wall_s": round(time.perf_counter() - STARTED, 1),
    }


def teardown_module(module) -> None:  # noqa: ARG001
    if RESULTS:
        flush()
        SUMMARY_FILE.write_text(json.dumps(build_summary(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nreal_model results: {os.path.relpath(RESULTS_FILE, ROOT)}\nsummary: {os.path.relpath(SUMMARY_FILE, ROOT)}")
