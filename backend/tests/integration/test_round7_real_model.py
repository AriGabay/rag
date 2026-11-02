"""Round 7 acceptance with the real model (U9, R26, R29) on the full path: the synthetic round-7 fixtures
(``tests/fixtures/round7``, every name, address, plan, block, parcel and amount invented) are ingested into a
synthetic office of the ``rag_test`` database, and questions go through the chat API to the configured model
(``gpt-6-luna`` with the repository's settings), whose key comes from the repository's ``.env`` and is never printed.

Each failure class of the plan is asked in several phrasings and asserted by structure — component statuses and
reasons, the gap groups, removal kinds, computation inputs and results, cited pages, answer status and pending
parameter — never by exact wording. The model is nondeterministic: each case may run a bounded second attempt (in a
new conversation), and every attempt's outcome is recorded; no assertion is loosened to pass.

Per turn, the model calls, tokens, cost and latency (the message's ``usage``) and the wall time are recorded to a JSON
file under ``real_documents/eval/round7/u9-real-model/`` (gitignored, private: answers are kept for diagnosis).

Opt in with ``-m real_model``; skipped without a key. Host only, against ``rag_test`` (``tests/conftest.py`` guard).

Environment note: ingestion runs without OCR or vision (R7c's picture stays an unread region the turn must inspect).
The turn's crop OCR, which confirms a visual reading's cells in their place (by each row's label and each column's
header, or by the grid of the numbers themselves), is run with Hebrew: when the host's Tesseract lacks it, the backend
image's Tesseract is called for the crop (``crop_ocr``)."""

from __future__ import annotations

import json
import os
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from app.db import tenant_tx
from app.providers import llm
from app.providers.llm import Purpose
from tests.conftest import login
from tests.factories import make_office

pytestmark = [pytest.mark.real_model, pytest.mark.db]

ROOT = Path(__file__).resolve().parents[3]
ROUND7 = Path(__file__).resolve().parents[1] / "fixtures" / "round7"
MANIFEST = json.loads((ROUND7 / "manifest.json").read_text(encoding="utf-8"))
DOCS = MANIFEST["documents"]
RESULTS_DIR = ROOT / "real_documents" / "eval" / "round7" / "u9-real-model"
RESULTS_FILE = RESULTS_DIR / f"results-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
RESULTS: list[dict] = []
ATTEMPTS = 2  # a bounded second attempt per case, in a new conversation

# component reasons that state something is missing from the documents (``coverage.REASONS``)
DOCUMENT_GAPS = {"region_not_read", "not_in_part_read", "not_located", "not_verifiable", "sources_conflict",
                 "removed"}
ATTRIBUTION_KINDS = {"wrong_subject", "contradicts_source", "absent_from_source"}


def fact(doc: str, key: str) -> dict:
    return DOCS[doc]["facts"][key]


def num(s: str) -> Decimal:
    return Decimal(str(s).replace(",", "").replace("%", "").strip())


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
def office(db, monkeypatch, real_key):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    o = make_office(db, "משרד בדיקה סינתטי", "admin-u9@example.test")
    with tenant_tx(o.ctx()) as conn:
        conn.execute(text("UPDATE office_settings SET cloud_llm_enabled = true"))
    return o


@pytest.fixture
def chat(client, office):
    login(client, "admin-u9@example.test")
    return client


def ingest(office, monkeypatch, key: str) -> str:
    """A round-7 fixture ingested without OCR or vision at ingestion (a picture stays an unread region) and without
    the measurement pass; both are back for the turn (``inspect`` reads with the office's vision model)."""
    from app.extraction import ocr
    from app.platform import pipeline
    from tests.integration.test_documents_api import ingest as ingest_file

    with monkeypatch.context() as m:
        m.setattr(ocr, "ocr_available", lambda languages: False)
        m.setattr(pipeline, "vision_reader", lambda ctx: None)
        m.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
        doc, _ = ingest_file(office, ROUND7 / Path(DOCS[key]["file"]).name, DOCS[key]["title"])
    return doc


COMPOSE_OCR = ["docker", "compose", "-p", "appraisal-rag", "exec", "-T", "backend", "tesseract"]


def crop_ocr(monkeypatch) -> str:
    """The OCR the turn's crop is checked with: the host's Tesseract when it has the configured languages; else the
    same Tesseract binary and language data the backend image runs (the stack's backend container, called only to
    run ``tesseract`` on the crop's pixels: no database, no pytest), at the same OCR boundary
    (``app.extraction.ocr._words``). Skips when neither has them. Returns what was used."""
    import io
    import subprocess

    from app.config import get_settings
    from app.extraction import ocr

    languages = get_settings().ocr_languages
    if ocr.ocr_available(languages):
        return f"host tesseract ({languages})"
    try:
        listed = subprocess.run([*COMPOSE_OCR, "--list-langs"], capture_output=True, timeout=60).stdout.decode()
    except (OSError, subprocess.TimeoutExpired):
        listed = ""
    if not all(lang in listed.split() for lang in languages.split("+")):
        pytest.skip(f"no Tesseract with {languages} on the host or in the stack's backend image")

    def words(img, langs: str, psm: int = 6) -> list[dict]:
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        out = subprocess.run([*COMPOSE_OCR, "stdin", "stdout", "-l", langs, "--psm", str(psm), "-c",
                              "tessedit_create_tsv=1"], input=buf.getvalue(), capture_output=True,
                             timeout=get_settings().ocr_timeout_seconds, check=True).stdout.decode()
        rows = [line.split("\t") for line in out.splitlines()]
        head = rows[0]
        result = []
        for i, r in enumerate(rows[1:]):
            d = dict(zip(head, r, strict=False))
            text_ = ocr._clean_word(d.get("text") or "")
            if not text_:
                continue
            result.append({"text": text_, "left": int(d["left"]), "top": int(d["top"]), "width": int(d["width"]),
                           "height": int(d["height"]), "conf": float(d["conf"]), "order": i,
                           "line": (int(d["block_num"]), int(d["par_num"]), int(d["line_num"]))})
        return result

    monkeypatch.setattr(ocr, "ocr_available", lambda langs: all(x in listed.split() for x in langs.split("+")))
    monkeypatch.setattr(ocr, "_words", words)
    return f"backend image tesseract ({languages}); the host's lacks a configured language"


# --- turns and records ---------------------------------------------------------------------------------------------

def new_conversation(c) -> str:
    r = c.post("/api/chat/conversations")
    assert r.status_code == 200, r.text
    return r.json()["id"]


def summary(m: dict, question: str, wall: float) -> dict:
    usage = m.get("usage") or []
    a = m.get("answer") or {}
    total = lambda k: sum(u.get(k) or 0 for u in usage)  # noqa: E731
    cost = [u.get("cost_usd") for u in usage]
    purposes: dict[str, int] = {}
    for u in usage:
        purposes[u["purpose"]] = purposes.get(u["purpose"], 0) + 1
    v = a.get("verification") or {}
    return {
        "question": question, "message_status": m.get("status"), "answer_status": a.get("status"),
        "calls": len(usage), "purposes": purposes, "input_tokens": total("input_tokens"),
        "cached_input_tokens": total("cached_input_tokens"), "output_tokens": total("output_tokens"),
        "cost_usd": round(sum(c for c in cost if c is not None), 6) if any(c is not None for c in cost) else None,
        "model_latency_ms": total("latency_ms"), "wall_s": round(wall, 1),
        "models": sorted({u.get("model") or "" for u in usage}),
        "progress": [p.get("step") for p in m.get("progress") or []],
        "searches": a.get("searches"), "removed": v.get("removed"),
        "removals": [r.get("failure_kind") for r in v.get("removals") or []],
        "components": [{k: c.get(k) for k in ("id", "text", "kind", "aspect", "parent", "status", "limitation",
                                               "place", "stated", "related", "searched")}
                       for c in a.get("components") or []],
        "gaps": [{k: g.get(k) for k in ("reason", "components", "texts", "text")} for g in a.get("gaps") or []],
        "computations": [{k: c.get(k) for k in ("id", "label", "expression", "display", "conditional", "conditions",
                                                 "inputs", "explicit_amount_available", "rates")}
                         for c in a.get("computations") or []],
        "values": [{"id": x.get("id"), "value_text": x.get("value_text"), "unit": x.get("unit"),
                    "pages": anchor_pages(x), "context": x.get("context"), "certainty": x.get("certainty"),
                    "meaning_from": x.get("meaning_from"), "reading": x.get("reading"), "kind": x.get("kind"),
                    "subject": x.get("subject")} for x in a.get("values") or []],
        "sources": [{"id": x.get("id"), "kind": x.get("kind"), "location": x.get("location"),
                     "section": x.get("section"), "pages": anchor_pages(x), "context": x.get("context")}
                    for x in a.get("sources") or []],
        "pending": a.get("pending"), "requested": a.get("requested"), "markdown": a.get("markdown"),
    }


def ask(c, cid: str, question: str, record: dict) -> dict:
    started = time.perf_counter()
    r = c.post(f"/api/chat/conversations/{cid}/messages", json={"content": question})
    assert r.status_code == 200, r.text
    m = c.get(f"/api/chat/messages/{r.json()['assistant']['id']}").json()
    record["turns"].append(summary(m, question, time.perf_counter() - started))
    flush()
    assert m["status"] == "done", (m.get("status"), m.get("error"))
    return m["answer"]


def flush() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_FILE.write_text(json.dumps({"model": "gpt-6-luna (settings)", "cases": RESULTS}, ensure_ascii=False,
                                       indent=2, default=str), encoding="utf-8")


class Phrasings:
    """Runs every phrasing of a failure class (``run_case``) and fails at the end with each failed one, so one
    phrasing's failure never hides another's outcome."""

    def __init__(self) -> None:
        self.failed: list[str] = []

    def run(self, name: str, chat, body, attempts: int = ATTEMPTS) -> None:
        try:
            run_case(name, chat, body, attempts)
        except AssertionError as exc:
            self.failed.append(f"{name}: {str(exc)[:600]}")

    def check(self) -> None:
        assert not self.failed, "\n".join(self.failed)


def run_case(name: str, chat, body, attempts: int = ATTEMPTS) -> None:
    """``body(chat, record)`` in a new conversation per attempt, up to ``attempts``: passes on the first attempt
    that passes; every attempt is recorded with its outcome."""
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


# --- reading an answer ---------------------------------------------------------------------------------------------

def anchor_pages(item: dict) -> list[int]:
    anchor = item.get("anchor") or {}
    return [p.get("page") for p in anchor.get("pages") or []]


def cited_ids(markdown: str) -> list[str]:
    out: list[str] = []
    for m in re.finditer(r"\[((?:[SMCVA]\d+)(?:\s*[,،;]\s*[SMCVA]\d+)*)\]", markdown or ""):
        for i in re.split(r"\s*[,،;]\s*", m.group(1)):
            if i not in out:
                out.append(i)
    return out


def items_by_id(a: dict) -> dict[str, dict]:
    return {x["id"]: x for key in ("sources", "values", "measurements", "computations") for x in a.get(key) or []}


def lines_with(markdown: str, number: str) -> list[str]:
    return [line for line in (markdown or "").splitlines() if number in line]


def cited_pages_of(a: dict, number: str) -> set[int]:
    """The pages of the document items cited on the lines that state ``number`` (values, sources, measurements; a
    computation by its inputs)."""
    by_id = items_by_id(a)
    pages: set[int] = set()
    for line in lines_with(a["markdown"], number):
        for i in cited_ids(line):
            x = by_id.get(i)
            if x is None:
                continue
            if i.startswith("C"):
                for inp in x.get("inputs") or []:
                    pages |= set(anchor_pages(by_id.get(inp.get("id")) or {}))
            else:
                pages |= set(anchor_pages(x))
    return pages


def cited_item_pages(a: dict, number: str) -> list[set[int]]:
    """Per item cited on the lines that state ``number``, the pages it opens at (a computation: its inputs')."""
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


def body_lines(a: dict) -> list[str]:
    """The answer's lines before the server's gap paragraph."""
    gap_texts = {g.get("text") for g in a.get("gaps") or []}
    return [line for line in (a.get("markdown") or "").splitlines() if line.strip() and line.strip() not in gap_texts]


def computation_with(a: dict, value: str) -> dict | None:
    want = num(value)
    for c in a.get("computations") or []:
        if num(c["value"]).quantize(Decimal(1)) == want.quantize(Decimal(1)):
            return c
    return None


def input_values(a: dict, c: dict) -> list[dict]:
    by_id = items_by_id(a)
    return [by_id[i["id"]] for i in c.get("inputs") or [] if i.get("id") in by_id]


def assert_no_document_gap_for_instructions(a: dict) -> None:
    comps = a.get("components") or []
    instructions = {c["id"] for c in comps if c.get("kind") == "instruction"}
    for c in comps:
        if c["id"] in instructions:
            assert c.get("limitation") in (None, "instruction_not_met"), c
    for g in a.get("gaps") or []:
        if g["reason"] in DOCUMENT_GAPS:
            assert not set(g.get("components") or []) & instructions, g
            assert not re.search(r"סגנון|מראה מקום|הפניה|מקצועי|פסקה", g["text"]), g


# --- the cases -----------------------------------------------------------------------------------------------------

PLAN_SECTION = fact("plan_status", "approved_units")["section"]  # "2. מצב תכנוני"


def test_reported_calculation_classification_keeps_source_context_separate_from_new_arithmetic(real_key):
    """A source's existing allowance and its contractual counterpart are facts to retrieve. Only a request to
    compute their difference is a calculation; mentioning the source's calculation does not change the request.
    This calls the real analysis boundary without creating or truncating database rows."""
    from app.chat import request

    questions = [
        ("לפי הדוח הסינתטי למתחם שדרות הקורנית, מהי תוספת השטח שנלקחה בחישוב, והבחן בינה לבין התוספת "
         "שנקבעה בהסכם?", False),
        ("בדוח הסינתטי למתחם שדרות הקורנית, מה ההבדל בין תוספת השטח שנלקחה בתחשיב לבין התוספת "
         "שנקבעה בהסכם? הצג את הנתונים כפי שנכתבו.", False),
        ("לפי הדוח הסינתטי למתחם שדרות הקורנית, חשב את ההפרש במ״ר בין תוספת השטח שנלקחה בחישוב לבין "
         "התוספת שנקבעה בהסכם.", True),
    ]
    for question, calculation in questions:
        analysis = request.analyze(llm.get_provider(Purpose.AGENT), question, deadline=None)
        record = {"case": "reported-calculation-classification", "question": question,
                  "expected_calculation": calculation, "components": analysis.items, "usage": analysis.usage,
                  "outcome": "pass" if analysis.status == "ok" and
                  any(c["calculation"] for c in analysis.items or []) == calculation else "fail"}
        RESULTS.append(record)
        flush()
        assert analysis.status == "ok", analysis.status
        assert any(c["calculation"] for c in analysis.items or []) == calculation, analysis.items
        assert any(c["kind"] == ("calculation" if calculation else "information") for c in analysis.items)


def test_mixed_information_and_writing_instructions_never_become_document_gaps(chat, office, monkeypatch):
    ingest(office, monkeypatch, "plan_status")
    phrasings = [
        "כתוב פרק מצב תכנוני למתחם שדרות הצבעוני: פרט את השימושים ואת מספר יחידות הדיור לפי התכנית המאושרת, ככל "
        "שהם מופיעים. כתוב בסגנון מקצועי, וצרף מראה מקום מדויק לכל נתון.",
        "נסח בעברית רשמית, בפסקה אחת וללא רשימות, את מספר הקומות ואת מספר יחידות הדיור המותרים לפי התכנית המאושרת "
        "במתחם שדרות הצבעוני, עם הפניה למקור לכל נתון.",
    ]
    cases = Phrasings()
    for i, q in enumerate(phrasings, 1):
        def body(c, cid, record, q=q):
            a = ask(c, cid, q, record)
            assert a["status"] in ("answered", "partial"), a["status"]
            assert "84" in a["markdown"], a["markdown"]
            assert_no_document_gap_for_instructions(a)
            # a citation instruction is reported unmet only when a datum of the shown answer lacks a citation
            for comp in a.get("components") or []:
                if comp.get("kind") == "instruction" and comp.get("limitation") == "instruction_not_met" and \
                        comp.get("aspect") == "citation":
                    uncited = [line for line in body_lines(a) if re.search(r"\d", line) and not cited_ids(line)]
                    assert uncited, comp
        cases.run(f"instructions/{i}", chat, body)
    cases.check()


def test_a_reported_allowance_keeps_its_contractual_contrast_and_only_a_requested_difference_is_computed(
    chat, office, monkeypatch,
):
    """Synthetic reported allowances exercise analysis, reading, complete contrast citations and repair through
    the chat API. Retrieval asks for both original quantities; arithmetic asks for a newly computed difference."""
    from app.extraction.base import Block, ChunkResult, ExtractionResult, PageResult
    from app.platform import pipeline
    from tests.factories import make_document

    title = "דוח סינתטי למתחם שדרות הקורנית"
    section = "2. תוספות השטח בהסכם ובתחשיב"
    paragraphs = [
        "מסמך סינתטי לדמו — דוח למתחם שדרות הקורנית. בסעיף ההסכם נקבעה תוספת שטח של 29 מ״ר לכל דירה.",
        "בתחשיב השמאי למתחם שדרות הקורנית נלקחה בחשבון תוספת שטח של 16 מ״ר לכל דירה. זו הנחת התחשיב, "
        "בשונה מתוספת השטח של 29 מ״ר שנקבעה בהסכם.",
    ]
    doc, ver = make_document(office, office.default_group_id, title, sha="e" * 64)
    info = pipeline.VersionInfo(ver, doc, "k", "application/pdf", None)
    blocks = [Block(0, "heading", section, section=section, section_path=[section], page=1),
              *(Block(i + 1, "paragraph", p, section=section, section_path=[section], page=1)
                for i, p in enumerate(paragraphs))]
    reading = ExtractionResult(1, [PageResult(1, "\n".join([section, *paragraphs]), "text_layer", 1.0, True)], [],
                               [ChunkResult(i, "text", [1], section, p, block_start=i + 1, block_end=i + 1)
                                for i, p in enumerate(paragraphs)], blocks=blocks)
    with tenant_tx(office.system) as conn:
        pipeline.persist_extraction(conn, info, reading)
    pipeline.embed_stage(office.system, info, 1e18)
    phrasings = [
        ("לפי הדוח למתחם שדרות הקורנית, מהי תוספת השטח שנלקחה בחישוב, והבחן בינה לבין התוספת שנקבעה בהסכם?",
         False),
        ("לפי הדוח למתחם שדרות הקורנית, חשב במ״ר את ההפרש: התוספת שנקבעה בהסכם פחות התוספת שנלקחה בתחשיב.",
         True),
    ]
    cases = Phrasings()
    for i, (q, calculate) in enumerate(phrasings, 1):
        def body(c, cid, record, q=q, calculate=calculate):
            a = ask(c, cid, q, record)
            assert any(x["kind"] == "calculation" for x in a.get("components") or []) == calculate
            if calculate:
                computation = computation_with(a, "13")
                assert computation is not None, a.get("computations")
                assert computation["id"] in cited_ids(a["markdown"]), a["markdown"]
                return
            assert not a.get("computations"), a.get("computations")
            for number in ("16", "29"):
                assert lines_with(a["markdown"], number), a["markdown"]
                assert cited_pages_of(a, number), (number, a["markdown"])
            assert "הסכם" in a["markdown"] and any(word in a["markdown"] for word in ("תחשיב", "חישוב")), a["markdown"]
            assert not any(g["reason"] == "calculation_incomplete" for g in a.get("gaps") or [])
            assert not any(x.get("status") != "full" for x in a.get("components") or []
                           if x["kind"] == "information"), a.get("components")
        cases.run(f"reported-allowance/{i}", chat, body)
    cases.check()


def test_a_partly_found_category_states_only_its_missing_items(chat, office, monkeypatch):
    ingest(office, monkeypatch, "plan_status")
    phrasings = [
        "פרט את נתוני התכנית: שימושים, יח״ד, שטחים, גובה, קומות וקווי בניין, ככל שהם מופיעים בפרק המצב התכנוני.",
        "מה מתירה התכנית המאושרת במתחם שדרות הצבעוני מבחינת שימושים, מספר יחידות דיור, שטחי בנייה, גובה, מספר "
        "קומות וקווי בניין?",
    ]
    absent = {"גובה": r"גובה", "שטחים": r"שטח", "קווי בניין": r"קווי\s*(?:ה)?בניין"}
    found = r"שימוש|יח״ד|יחידות|קומות"

    cases = Phrasings()
    for i, q in enumerate(phrasings, 1):
        def body(c, cid, record, q=q):
            a = ask(c, cid, q, record)
            md = a["markdown"]
            assert "84" in md and re.search(r"\b9\b", md) and "מגורים" in md, md
            comps = {x["id"]: x for x in a.get("components") or []}
            parents = {x.get("parent") for x in comps.values() if x.get("parent")}
            gaps = a.get("gaps") or []
            for g in gaps:
                # the category (a component with children) is never itself a gap, nor is a found item
                assert not set(g.get("components") or []) & parents, (g, sorted(parents))
                assert not any(re.search(found, t) for t in g.get("texts") or []), g
            missing = {g_id for g in gaps for g_id in g.get("components") or []}
            # the openings the server validated for the missing items' claims: a section, table or page range opened
            # this turn and read to its end (``coverage.validate_requested``)
            validated = [r for r in a.get("requested") or [] if r.get("component") in missing and r.get("checked_where")]
            for item, pattern in absent.items():
                hits = [g for g in gaps if any(re.search(pattern, t) for t in g.get("texts") or [])]
                assert hits, (item, [g["text"] for g in gaps])
                # the reason the turn's evidence supports (the KTD4 table): a section read to its end and checked ->
                # not present in the part read, naming the section; searches only (or the paragraphs around a hit,
                # which certify no section was checked whole) -> not located by the searches performed
                stated = [(g["reason"], g["text"]) for g in hits]
                if validated or any(g["reason"] == "not_in_part_read" for g in hits):
                    assert all(g["reason"] == "not_in_part_read" and PLAN_SECTION.split(". ", 1)[1] in g["text"]
                               for g in hits), stated
                else:
                    assert all(g["reason"] == "not_located" for g in hits), stated
                    claimed = sorted({r.get("claimed") for r in a.get("requested") or []
                                      if r.get("component") in missing})
                    record["notes"].append(f"{item}: no section opening validated (claims: {claimed}): not_located")
        cases.run(f"partly-found/{i}", chat, body)
    cases.check()


def test_an_inspected_image_table_feeds_a_non_conditional_computation_anchored_to_the_table(chat, office,
                                                                                            monkeypatch):
    from app.chat import tools

    ingest(office, monkeypatch, "cost_table_image")
    langs = crop_ocr(monkeypatch)
    crop_words = []
    crop_reader = tools._crop_ocr

    def capture_crop(png):
        words = crop_reader(png)
        crop_words.append(words)
        return words

    monkeypatch.setattr(tools, "_crop_ocr", capture_crop)
    q = DOCS["cost_table_image"]["question_total"]
    picture = fact("cost_table_image", "picture")
    phrasings = [
        "מה עלות הבנייה העילית והחניון התת-קרקעי יחד, לפי טבלת עלויות הבנייה?",
        "חשב את סכום העלויות של הבנייה העילית ושל החניון התת-קרקעי בפרויקט שדרות הדובדבן.",
    ]
    cases = Phrasings()
    for i, question in enumerate(phrasings, 1):
        def body(c, cid, record, question=question):
            record["notes"].append(f"crop OCR: {langs}")
            a = ask(c, cid, question, record)
            # Private synthetic diagnostics make a failed cell's placement reproducible without another model call.
            with tenant_tx(office.ctx()) as conn:
                record["inspections"] = [dict(r) for r in conn.execute(text(
                    "SELECT region, reading FROM region_readings")).mappings()]
            record["crop_words"] = crop_words
            flush()
            # the picture was read in the turn (ingestion read none of it): a visual reading of its region
            assert any(src.get("kind") == "image" for src in a.get("sources") or []), \
                [(src["id"], src.get("kind")) for src in a.get("sources") or []]
            comp = computation_with(a, q["result"])
            assert comp is not None, [(x["id"], x["value"]) for x in a.get("computations") or []]
            assert comp["id"] in cited_ids(a["markdown"]), a["markdown"]
            inputs = input_values(a, comp)
            assert sorted(num(x["value"]) for x in inputs) == sorted(num(v) for v in q["inputs"]), comp["inputs"]
            x0, y0, x1, y1 = picture["box"]
            for x in inputs:
                anchor = x.get("anchor") or {}
                (page,) = anchor.get("pages") or [None]
                assert page and page["page"] == picture["page"], anchor
                w, h = page.get("width"), page.get("height")
                assert page.get("rects"), anchor
                for r in page["rects"]:
                    # the input opens inside the table's picture (rects are fractions of the page)
                    assert r[0] >= x0 / w - 0.02 and r[2] <= x1 / w + 0.02, (r, picture["box"], w)
                    assert r[1] >= y0 / h - 0.02 and r[3] <= y1 / h + 0.02, (r, picture["box"], h)
            # its cells were confirmed by OCR of the crop (R18): the result is not conditional
            assert comp["conditional"] is False, (comp.get("conditions"), comp)
        cases.run(f"inspected-table/{i}", chat, body)
    cases.check()


FIRST, SECOND = (fact("two_appraisals", f"{w}_title") for w in ("first", "second"))


def test_two_appraisals_in_one_file_the_asked_propertys_figure_is_used_and_cited_on_its_page(chat, office,
                                                                                         monkeypatch):
    ingest(office, monkeypatch, "two_appraisals")
    asks = [
        (f"מה השווי למ״ר שנקבע לנכס ב{SECOND['street']}?", "second_per_sqm", "first_per_sqm"),
        (f"בשומה של {SECOND['street']}, מהו שווי הנכס?", "second_value", "first_value"),
        (f"מה שטח הנכס ב{FIRST['street']}?", "first_area", "second_area"),
    ]
    cases = Phrasings()
    for i, (question, right, wrong) in enumerate(asks, 1):
        def body(c, cid, record, question=question, right=right, wrong=wrong):
            a = ask(c, cid, question, record)
            r, w = fact("two_appraisals", right), fact("two_appraisals", wrong)
            assert r["value"] in a["markdown"], a["markdown"]
            assert not re.search(rf"(?<![\d,]){re.escape(w['value'])}(?![\d,])", a["markdown"]), a["markdown"]
            assert cited_pages_of(a, r["value"]) == {r["page"]}, (cited_pages_of(a, r["value"]), r["page"])
            # a claim that took the other appraisal's figure is removed as the wrong property
            for kind in record["turns"][-1]["removals"]:
                record["notes"].append(f"removal: {kind}")
        cases.run(f"two-appraisals/{i}", chat, body)
    cases.check()


def test_a_follow_up_keeps_the_property_and_the_other_property_switches_without_carrying_values(chat, office,
                                                                                             monkeypatch):
    ingest(office, monkeypatch, "two_appraisals")
    s_rate, s_area = fact("two_appraisals", "second_per_sqm"), fact("two_appraisals", "second_area")
    f_rate, f_area = fact("two_appraisals", "first_per_sqm"), fact("two_appraisals", "first_area")
    phrasings = [("ומה שטח הנכס?", "ומה לגבי הנכס האחר?"),
                 ("וכמה מ״ר שטחו?", "ומה הנתונים האלה בנכס האחר שבקובץ?")]

    cases = Phrasings()
    for i, (follow, other) in enumerate(phrasings, 1):
        def body(c, cid, record, follow=follow, other=other):
            a1 = ask(c, cid, f"מה השווי למ״ר שנקבע לנכס ב{SECOND['street']}?", record)
            per_item = cited_item_pages(a1, s_rate["value"])
            assert s_rate["value"] in a1["markdown"] and per_item and all(2 in p_ for p_ in per_item), a1["markdown"]
            a2 = ask(c, cid, follow, record)
            # the follow-up keeps the property: its area, cited on its page, not the other's
            assert re.search(rf"\b{s_area['value']}\b", a2["markdown"]), a2["markdown"]
            assert not re.search(rf"\b{f_area['value']}\b", a2["markdown"]), a2["markdown"]
            per_item = cited_item_pages(a2, s_area["value"])
            assert per_item and all(2 in p_ for p_ in per_item), (per_item, a2["markdown"])
            a3 = ask(c, cid, other, record)
            md = a3["markdown"]
            # the other property: its own figures, from its own page; nothing of the first carried across
            assert f_rate["value"] in md or re.search(rf"\b{f_area['value']}\b", md), md
            for v in (f_rate["value"], f_area["value"]):
                if re.search(rf"\b{re.escape(v)}\b", md):
                    # each item cited for it holds the first appraisal's page (a source read across both
                    # appraisals spans both pages; none rests on the second appraisal alone)
                    per_item = cited_item_pages(a3, v)
                    assert per_item and all(1 in p_ for p_ in per_item), (v, per_item, md)
                    if any(p_ != {1} for p_ in per_item):
                        record["notes"].append(f"{v}: cited through a source spanning pages {per_item}")
            assert s_rate["value"] not in md and not re.search(rf"\b{s_area['value']}\s*מ", md), md
            assert all(set(anchor_pages(x)) <= {1} for x in a3.get("values") or []), \
                [(x["id"], anchor_pages(x)) for x in a3.get("values") or []]
        cases.run(f"follow-up-switch/{i}", chat, body)
    cases.check()


GAP = DOCS["residual"]["gap_to_threshold"]


def test_the_explicit_profit_amount_is_used_over_cost_times_the_rounded_rate(chat, office, monkeypatch):
    ingest(office, monkeypatch, "residual")
    amount = fact("residual", "profit_amount")["value"]
    phrasings = ["מה הפער בין הרווח היזמי לבין הרווח המינימלי הנדרש לכדאיות הפרויקט?",
                 "בכמה עולה הרווח היזמי שבתחשיב על הסף המינימלי הנדרש? חשב."]
    cases = Phrasings()
    for i, q in enumerate(phrasings, 1):
        def body(c, cid, record, q=q):
            a = ask(c, cid, q, record)
            md = a["markdown"]
            assert GAP["right"] in md, md
            assert GAP["from_rounded_rate"] not in md, md
            comp = computation_with(a, GAP["right"])
            assert comp is not None and comp["conditional"] is False, a.get("computations")
            # the stated amount is an input (taken, or a step that equals it)
            assert num(amount) in {num(x["value"]) for x in input_values(a, comp)}, comp["inputs"]
            near_miss = computation_with(a, GAP["cost_times_rate"])
            if near_miss is not None:  # cost x the rounded rate was tried: the calculator named the amount
                record["notes"].append("the model multiplied cost by the rounded rate; near-miss reported")
                assert near_miss.get("explicit_amount_available"), near_miss
        cases.run(f"explicit-amount/{i}", chat, body)
    cases.check()


def test_a_missing_rate_asks_one_question_keeping_found_values_and_the_reply_computes_without_searching(
        chat, office, monkeypatch):
    ingest(office, monkeypatch, "residual")
    income = fact("residual", "income")["value"]
    phrasings = ["מה יהיו סך ההכנסות הצפויות מהפרויקט אם מחירי המכירה ירדו?",
                 "אם מחירי הדירות בפרויקט ברחוב התאנה 30 יירדו, כמה יסתכמו ההכנסות?"]
    cases = Phrasings()
    for i, q in enumerate(phrasings, 1):
        def body(c, cid, record, q=q):
            a1 = ask(c, cid, q, record)
            assert a1["status"] == "clarification", (a1["status"], a1["markdown"])
            pending = a1.get("pending")
            assert pending and len(pending.get("parameters") or []) == 1, pending
            assert income in a1["markdown"], a1["markdown"]  # the found value is kept
            assert not [x for x in a1.get("computations") or [] if not x.get("conditional")], a1.get("computations")
            a2 = ask(c, cid, "8%", record)
            turn = record["turns"][-1]
            assert a2["status"] in ("answered", "partial"), (a2["status"], a2["markdown"])
            assert not a2.get("searches") and "search" not in turn["progress"], (a2.get("searches"),
                                                                                 turn["progress"])
            comps = [x for x in a2.get("computations") or [] if x["id"] in cited_ids(a2["markdown"])]
            assert comps, (a2.get("computations"), a2["markdown"])
            uses_rate = [x for x in comps if any(i_.get("kind") == "assumption" and num(i_.get("value")) in
                                                 (Decimal(8), Decimal("0.08")) for i_ in x.get("inputs") or [])]
            assert uses_rate, [x["inputs"] for x in comps]
            expected = {num(income) * Decimal("0.92"), num(income) * Decimal("0.08")}
            assert any(num(x["value"]).quantize(Decimal(1)) in {e.quantize(Decimal(1)) for e in expected}
                       for x in uses_rate), [(x["expression"], x["value"]) for x in uses_rate]
        cases.run(f"missing-rate/{i}", chat, body)
    cases.check()


def test_a_rate_the_report_states_in_its_sensitivity_section_is_computed_without_a_question(chat, office,
                                                                                            monkeypatch):
    ingest(office, monkeypatch, "residual")
    rate, cost = fact("residual", "sensitivity_rate"), fact("residual", "cost")
    phrasings = ["מה יהיה הרווח היזמי בפרויקט ברחוב התאנה 30 אם עלויות הבנייה והפיתוח יעלו?",
                 "כמה יסתכמו עלויות הבנייה והפיתוח של הפרויקט ברחוב התאנה 30 בתרחיש של התייקרות?"]
    cases = Phrasings()
    for i, q in enumerate(phrasings, 1):
        def body(c, cid, record, q=q):
            a = ask(c, cid, q, record)
            # no question for the rate: the report states it (KTD9)
            assert a["status"] in ("answered", "partial") and not a.get("pending"), (a["status"], a.get("pending"))
            comps = [x for x in a.get("computations") or [] if x["id"] in cited_ids(a["markdown"])]
            assert comps, (a.get("computations"), a["markdown"])
            by_id = items_by_id(a)
            # a cited result applies the report's own rate (a V# of the sensitivity section, as a rate)
            applied = {num(by_id[r]["value"]) for x in comps for r in x.get("rates") or [] if r in by_id}
            for x in comps:  # through a step it rests on
                for i_ in x.get("inputs") or []:
                    y = by_id.get(i_.get("id"))
                    if y is not None and i_["id"].startswith("C"):
                        applied |= {num(by_id[r]["value"]) for r in y.get("rates") or [] if r in by_id}
            assert applied & {num(rate["value"]), Decimal("0.06")}, ([x.get("rates") for x in comps], applied)
            record["notes"].append(f"formulas: {[x['expression'] for x in comps]}; conditional: "
                                   f"{[x['conditional'] for x in comps]}; cost used: "
                                   f"{num(cost['value']) in {num(v['value']) for v in a.get('values') or []}}")
        cases.run(f"sensitivity-rate/{i}", chat, body)
    cases.check()


def test_a_valid_paraphrased_or_rounded_claim_survives_verification(chat, office, monkeypatch):
    ingest(office, monkeypatch, "residual")
    ingest(office, monkeypatch, "two_appraisals")
    ingest(office, monkeypatch, "decision")
    asks = [
        ("מה סך ההכנסות הצפויות מהפרויקט ברחוב התאנה 30, במיליוני ₪ (עגל לעשירית)?", r"24[.,]6\s*מיליון"),
        (f"תאר במילים שלך את הנכס ב{SECOND['street']}: כמה חדרים יש בו, באיזו קומה הוא ומה שטחו?", r"\b110\b"),
        ("בהכרעה ברחוב הרימון 5: מה העריך כל אחד מהשמאים של הצדדים, ומה קבע השמאי המכריע?", r"15,350"),
    ]
    cases = Phrasings()
    for i, (q, must) in enumerate(asks, 1):
        def body(c, cid, record, q=q, must=must):
            a = ask(c, cid, q, record)
            assert re.search(must, a["markdown"]), a["markdown"]
            assert (a.get("verification") or {}).get("removed") == 0, a.get("verification")
        cases.run(f"valid-claim/{i}", chat, body)
    cases.check()


class Misattributing:
    """The real model on every call, except that the first final answer stating the adopted figure presents a
    party's figure as the adopted one instead (``old`` -> ``new`` everywhere in it): a correct number of the document
    with the wrong attribution, for the real verification to catch on the full path."""

    name = "openai"
    routed = False  # serves every purpose itself, each through the purpose's configured provider

    def __init__(self, old: str, new: str) -> None:
        self.old, self.new, self.swapped = old, new, False
        self.model = llm.get_provider(Purpose.AGENT).model

    def structured(self, purpose, *args, **kw):
        return llm.get_provider(purpose).structured(purpose, *args, **kw)

    def structured_image(self, purpose, *args, **kw):
        return llm.get_provider(purpose).structured_image(purpose, *args, **kw)

    def agent_step(self, *args, **kw):
        step = llm.get_provider(Purpose.AGENT).agent_step(*args, **kw)
        if step.final is not None and not self.swapped:
            dumped = json.dumps(step.final, ensure_ascii=False)
            if self.old in dumped:
                step.final = json.loads(dumped.replace(self.old, self.new))
                self.swapped = True
        return step


def test_a_correct_number_with_the_wrong_attribution_is_removed_or_corrected(chat, office, monkeypatch):
    ingest(office, monkeypatch, "decision")
    adopted, applicant = fact("decision", "adopted"), fact("decision", "applicant")
    phrasings = ["מה השווי למ״ר במצב החדש שקבע השמאי המכריע בהכרעה ברחוב הרימון 5?",
                 "איזה שווי למ״ר אומץ בהכרעה בעניין רחוב הרימון 5?"]
    cases = Phrasings()
    for i, q in enumerate(phrasings, 1):
        def body(c, cid, record, q=q):
            fake = Misattributing(adopted["value"], applicant["value"])
            monkeypatch.setattr(llm, "get_selected_provider", lambda: fake)
            a = ask(c, cid, q, record)
            assert fake.swapped, "the model's first answer did not state the adopted figure (nothing to swap)"
            record["notes"].append("first final answer: adopted figure presented as the applicant's")
            md = a["markdown"]
            v = a.get("verification") or {}
            # the wrong attribution never survives: removed (with an attribution kind) or corrected in the repair
            for line in lines_with(md, applicant["value"]):
                assert re.search(r"מבקש", line), line
            assert v.get("removed", 0) >= 1 or applicant["value"] not in md, v
            kinds = [r.get("failure_kind") for r in v.get("removals") or []]
            assert set(kinds) <= ATTRIBUTION_KINDS | {"wrong_calculation", "not_checked"}, kinds
            record["notes"].append(f"removal kinds: {kinds}; adopted figure in the answer: {adopted['value'] in md}")
        cases.run(f"wrong-attribution/{i}", chat, body)
    cases.check()


def teardown_module(module) -> None:  # noqa: ARG001
    if RESULTS:
        flush()
        print(f"\nreal_model results: {os.path.relpath(RESULTS_FILE, ROOT)}")
