"""Content and combined answers (U10, R24, R25, R29, R34, R35)."""

from __future__ import annotations

import logging
import time

from sqlalchemy import Connection, text

from app.answering.coverage import coverage
from app.answering.verify import allowed_numbers, verify_answer
from app.config import get_settings
from app.db import TenantContext
from app.platform.documents import source_file_url
from app.platform.search import hybrid_search
from app.providers.llm import LLMProvider, MockLLM, cloud_configured, get_cloud_provider

logger = logging.getLogger(__name__)
EVIDENCE_LIMIT = 6


def effective_provider(conn: Connection) -> str:
    """cloud | enabled_no_key | demo_mock | extractive (admin screen shows the same value)."""
    enabled = conn.execute(text("SELECT cloud_llm_enabled FROM office_settings")).scalar_one_or_none()
    if enabled and cloud_configured():
        return "cloud"
    if enabled:
        return "enabled_no_key"
    return "demo_mock" if get_settings().demo_mode else "extractive"


def select_provider(conn: Connection) -> tuple[LLMProvider | None, str]:
    eff = effective_provider(conn)
    if eff == "cloud":
        return get_cloud_provider(), eff
    if eff in ("demo_mock", "enabled_no_key") and get_settings().demo_mode:
        return MockLLM(), "demo_mock"
    return None, "extractive"


def log_usage(conn: Connection, provider: LLMProvider, purpose: str, result, ok: bool) -> None:
    conn.execute(
        text("INSERT INTO provider_usage (office_id, provider, model, purpose, input_tokens, output_tokens,"
             " latency_ms, ok) VALUES (app_office(), :p, :m, :pu, :i, :o, :l, :ok)"),
        {"p": provider.name, "m": provider.model, "pu": purpose, "i": getattr(result, "input_tokens", None),
         "o": getattr(result, "output_tokens", None), "l": getattr(result, "latency_ms", None), "ok": ok},
    )


def _evidence(hits: list[dict], start: int) -> list[dict]:
    out = []
    for i, h in enumerate(hits, start=start):
        page = h["page_list"][0] if h["page_list"] else None
        out.append({
            "evidence_id": f"E{i}", "document_id": str(h["document_id"]), "version_id": str(h["version_id"]),
            "title": h["title"], "page_list": h["page_list"], "section": h["section"], "row": None,
            "snippet": h["snippet"], "text": h["text"], "chunk_id": str(h["chunk_id"]),
            "url": source_file_url(h["document_id"], h["version_id"], page),
        })
    return out


def _extractive(evidence: list[dict]) -> str:
    lines = ["להלן הקטעים הרלוונטיים ביותר מתוך מסמכי המשרד:"]
    for e in evidence[:4]:
        where = f"עמ׳ {', '.join(map(str, e['page_list']))}" if e["page_list"] else (e["section"] or "")
        lines.append(f"• {e['snippet']} [{e['evidence_id']}] ({e['title']}{', ' + where if where else ''})")
    return "\n".join(lines)


def answer_content(conn: Connection, ctx: TenantContext, question: str, c, route: str, numeric=None):
    from app.answering.service import Outcome

    query = question
    if c is not None and (c.neighborhood or c.city) and numeric is not None:
        query = f"{question} {c.neighborhood or ''} {c.city or ''}"
    # Evidence needs at least one lexical or fuzzy term match; a purely semantic neighbour is not a basis.
    hits = [h for h in hybrid_search(conn, query, EVIDENCE_LIMIT * 2) if h["lexical_support"]][:EVIDENCE_LIMIT]
    base = numeric.answer if numeric is not None else None
    start = len(base["sources"]) + 1 if base else 1
    evidence = _evidence(hits, start)
    cov = base["coverage"] if base else coverage(conn, None)
    limitations = list(base["limitations"]) if base else []

    if not evidence:
        if base:
            base["kind"] = "combined"
            base["limitations"] = limitations + ["לא נמצאו קטעי הסבר רלוונטיים במסמכים המורשים."]
            return numeric
        answer = {"kind": "abstain", "provider": "template", "demo": False, "sources": [], "coverage": cov,
                  "text": "לא נמצאו במסמכים שאתם מורשים לראות קטעים רלוונטיים לשאלה. לא ניתנה תשובה.",
                  "limitations": ["החיפוש בוצע רק במסמכי המשרד שעובדו ושאתם מורשים לראות."]}
        return Outcome(answer, c, c.intent if c else "explanation", route)

    provider, provider_label = select_provider(conn)
    calc = base["numeric"] if base else None
    text_out, kind_provider, demo = None, "extractive", False
    provider_failed = False
    if provider is not None:
        started = time.perf_counter()
        try:
            result = provider.answer(question, evidence, calc)
            result.latency_ms = result.latency_ms or int((time.perf_counter() - started) * 1000)
            allowed_ids = {e["evidence_id"] for e in evidence} | {s["evidence_id"] for s in (base["sources"] if base else [])}
            numbers = allowed_numbers([e["text"] for e in evidence] + [s.get("snippet") or "" for s in (base["sources"] if base else [])], calc)
            problems = [] if result.insufficient else verify_answer(result.text, result.used_ids, allowed_ids, numbers)
            log_usage(conn, provider, "answer", result, not problems)
            if result.insufficient:
                limitations.append("לפי הראיות שנמצאו אין בסיס מספיק לתשובה מלאה; מוצגים הקטעים הרלוונטיים.")
            elif problems:
                logger.info("model answer rejected: %s", problems)
                limitations.append("תשובת המודל לא עברה את בדיקות האימות, ולכן מוצגים הקטעים עצמם.")
            else:
                text_out, kind_provider, demo = result.text, ("mock" if provider.demo else "cloud"), provider.demo
        except Exception:  # noqa: BLE001 - provider failure falls back, never leaks details
            logger.warning("provider %s failed", provider.name)
            log_usage(conn, provider, "answer", None, False)
            limitations.append("ספק המודל לא היה זמין; מוצגים הקטעים הרלוונטיים.")
            provider_failed = True
    if text_out is None:
        text_out = _extractive(evidence)
    if provider_label == "extractive":
        limitations.append("שליחת קטעים לספק מודל ענן כבויה במשרד; התשובה מורכבת מקטעי המקור עצמם.")

    sources = (base["sources"] if base else []) + [{k: v for k, v in e.items() if k != "text"} for e in evidence]
    if base:
        answer = dict(base)
        answer.update({"kind": "combined", "text": base["text"] + "\n\n" + text_out, "provider": kind_provider
                       if kind_provider != "extractive" else "template", "demo": demo, "sources": sources,
                       "limitations": limitations})
    else:
        answer = {"kind": "content", "text": text_out, "provider": kind_provider, "demo": demo, "sources": sources,
                  "coverage": cov, "limitations": limitations, "numeric": None}
    rows = (numeric.source_rows if numeric else []) + [
        {"document_id": e["document_id"], "version_id": e["version_id"], "chunk_id": e["chunk_id"],
         "page_list": e["page_list"]} for e in evidence]
    return Outcome(answer, c, c.intent if c else "explanation", route, source_rows=rows,
                   cacheable=not provider_failed)
