"""Evaluation of the conversational engine against a running stack, with reference answers fixed in advance.

Usage::

    uv run python -m eval.chat_eval --set <questions.yaml> --out <dir> [--base http://localhost:8000]
                                    [--email ...] [--password ...] [--only C1,C3] [--sha <file>]

The question set is YAML. It may describe real office documents, so the set and the results belong outside
the repository (``real_documents/`` is ignored by git); the runner holds no document content.

Separate measures, never folded into one number:

- ``ingestion``: a value read where the document puts it (``eval.scoring.structure_check``): an item names
  ``document``, ``value`` and, for a table, ``row`` and ``column`` (and ``table``, a caption, title or heading
  fragment), or for prose a ``heading`` and ``near`` text; ``unit`` and ``page`` when given. The same number in
  another row or column fails;
- ``ingestion_text``: the legacy layer, ``must_read`` texts that must appear anywhere in a document's blocks;
- ``retrieval``: for a search query, every text of ``expect_all`` (or the one ``expect``) among the top ``k``
  passages, with the rank of each and the missing ones named;
- ``meaning``: stored measurements that must carry the stated kind, period, VAT, area basis or form;
- ``answers``: conversations. Each turn's answer is checked in three layers, reported separately: the regression
  layer (``must`` / ``must_not`` regular expressions, allowed answer statuses, the documents its sources must and
  must not come from), the structured layer (``eval.scoring``: required documents, value sets, the meaning
  and attribution of values, coverage and hedging), which fails answers that only contain the right words, and
  the calculation layer (``calc: [{value, precision}]``: a result shown at the stated precision);
- per-turn measures of round 9 (R29), each beside the others and never folded into the conversation's pass or one
  number, each only for a turn that names its expectation in ``expect`` (or beside it): unit identification
  (``expect_unit_pages: [first, last]`` file pages, with ``expect_document``, a title fragment), source location
  (``expect_source_pages``), reading completeness (``expect_read_pages``), and per stage time (median and p90 per
  stage from the diagnostics' ``timing``) with the cost per turn. The pages read come from the cited sources (the
  stage clock's tool records hold no pages); the report says so.

Every run records what it ran against (a ``run`` record first in the results): the checkout's commit (``git
rev-parse``, with whether the tree had changes) or the ``--build`` given, the backend image id given with
``--image``, and for each document used (checked, or cited by an answer) its ``ingestion_version`` and
``reading_id``. The documents that ingestion items check keep their blocks there too, as evidence. The report
adds the model seen per purpose in the calls' usage.

``--rescore <results.json>`` grades stored results again with the set's current expectations (no model calls, no
stack): answers from their stored payload, ingestion from the stored blocks, retrieval from the stored passages,
so two runs can be compared on the same answers and expectations. A stored result without its evidence (from a
run before it was kept) is kept as it was.

A reference answer found wrong is corrected in the set, not silently: the turn keeps the original expectation in
``reference_corrected: {date, evidence, was}`` beside the corrected ``expect``. Every run and rescore grades such
a turn both ways, and the report gives the original automated score and the score after reference corrections
apart, with each correction and its evidence. The cost table gives the cost per conversation that passed.

``--sha <file>`` (round 13 U1, KTD18) holds a ``shasum -a 256`` line (``<hex>  <name>``) of a frozen set. The set given
with ``--set`` is hashed before anything runs (a rescore included) and a set that differs is refused with exit code 2,
naming the expected and the actual hash (``check_set_sha``). Without it nothing changes.

``source_docs_mode: if_needed`` (round 13 U1, KTD18) on a turn's ``expect`` is for a mixed question, whose professional
part needs a glossary citation only when its answer needs one. The turn passes ``source_docs`` for a fragment when a
cited source's title holds it; otherwise only when ``professional_support`` finds at least one professional statement
and every one is verified by another cited source (the rule is in that function). ``professional_not_asked: true`` marks
a reference whose professional part was not asked, so an answer without a professional statement passes;
``professional_patterns: [regex]`` names professional statements by their words, for a statement that cites an
appraisal. The default mode (``cited``) is unchanged.

Each check passes or fails on its own; the report lists every failure with the text that caused it, so a
person can read the failing answers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

from app.chat.verify import ROW_ACTIONS, split_units
from app.platform import source_kinds
from eval.scoring import (
    calc_check,
    norm,
    norm_pattern,
    percentile,
    retrieval_check,
    stage_time_stats,
    structure_check,
    turn_measures,
    turn_timing,
)
from eval.scoring import check as structured_check

POLL_SECONDS = 1.5
TURN_TIMEOUT = 300


@dataclass
class Result:
    kind: str
    id: str
    ok: bool
    detail: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)


def login(base: str, email: str, password: str) -> httpx.Client:
    c = httpx.Client(base_url=base, timeout=60)
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    r.raise_for_status()
    return c


def documents(c: httpx.Client) -> dict[str, dict]:
    out = {}
    for d in c.get("/api/documents").json()["documents"]:
        if not d.get("deleted") and d.get("current_version"):
            out[d["title"]] = d
    return out


def find_doc(docs: dict[str, dict], needle: str) -> dict | None:
    for title, d in docs.items():
        if needle in title:
            return d
    return None


# what a block keeps as evidence for a rescore: its place, status, text and table structure
EVIDENCE_KEYS = ("index", "kind", "page", "section", "section_path", "status", "text", "table")


def doc_info(d: dict, reading_id: str | None, blocks: list[dict] | None = None) -> dict:
    """A document used by the run: its current version, the ingestion version and reading it was answered from,
    and (for documents that ingestion items check) its blocks as evidence."""
    v = d["current_version"]
    info = {"id": d["id"], "title": d["title"], "version_id": v["id"],
            "ingestion_version": (v.get("reading") or {}).get("ingestion_version"), "reading_id": reading_id}
    if blocks is not None:
        info["blocks"] = [{k: b.get(k) for k in EVIDENCE_KEYS} for b in blocks]
    return info


def fetch_blocks(c: httpx.Client, d: dict, *, first_only: bool = False) -> tuple[list[dict], str | None]:
    """A document's current blocks in reading order, page by page of the endpoint's window, and its reading id;
    ``first_only``: one block, for the reading id alone."""
    url = f"/api/documents/{d['id']}/versions/{d['current_version']['id']}/blocks"
    blocks: list[dict] = []
    start, reading_id = 0, None
    while True:
        r = c.get(url, params={"start": start, "end": 0 if first_only else 1_000_000}).json()
        reading_id = r.get("reading_id")
        window = r.get("blocks") or []
        blocks += window
        if first_only or not window or len(blocks) >= (r.get("total") or 0):
            return blocks, reading_id
        start = window[-1]["index"] + 1


def grade_ingestion(it: dict, info: dict | None) -> list[Result]:
    """One ingestion item against a document's blocks: the structural check when it names a value, the legacy
    text check when it has ``must_read``; each its own result."""
    layers = [k for k, on in (("ingestion", "value" in it), ("ingestion_text", bool(it.get("must_read")))) if on]
    if info is None:
        return [Result(k, it["id"], False, ["המסמך לא נמצא"], {"layer": "structure"} if k == "ingestion" else {})
                for k in layers]
    blocks = info.get("blocks") or []
    meta = {"document_id": info["id"], "reading_id": info.get("reading_id")}
    out = []
    if "ingestion" in layers:
        problems = structure_check(blocks, it)
        out.append(Result("ingestion", it["id"], not problems, problems, meta | {"layer": "structure"}))
    if "ingestion_text" in layers:
        text = norm("\n".join(b.get("text") or "" for b in blocks))
        missing = [t for t in it["must_read"] if norm(t) not in text]
        out.append(Result("ingestion_text", it["id"], not missing, [f"לא נקרא: {m}" for m in missing], meta))
    return out


def check_ingestion(c: httpx.Client, docs: dict, items: list[dict], used: dict[str, dict]) -> list[Result]:
    """``used``: the run's documents by id, filled here with the blocks of every document an item checks."""
    out = []
    for it in items:
        d = find_doc(docs, it["document"])
        if d is not None and "blocks" not in used.get(d["id"], {}):
            blocks, reading_id = fetch_blocks(c, d)
            used[d["id"]] = doc_info(d, reading_id, blocks)
        out += grade_ingestion(it, used[d["id"]] if d is not None else None)
    return out


def grade_retrieval(it: dict, hits: list[str], titles: list[str] | None = None) -> Result:
    problems, data = retrieval_check(hits, it)
    return Result("retrieval", it["id"], not problems, problems, data | {"hits": hits, "titles": titles or []})


def check_retrieval(c: httpx.Client, items: list[dict]) -> list[Result]:
    out = []
    for it in items:
        hits = c.get("/api/search", params={"q": it["query"], "limit": it.get("k", 8)}).json()["results"]
        out.append(grade_retrieval(it, [h.get("text") or h.get("snippet") or "" for h in hits],
                                   [h.get("title") or "" for h in hits]))
    return out


def check_meaning(c: httpx.Client, docs: dict, items: list[dict]) -> list[Result]:
    out = []
    for it in items:
        d = find_doc(docs, it["document"])
        rows = c.get("/api/review/measurements", params={"document_id": d["id"], "q": it["value_contains"]}
                     ).json()["items"] if d else []
        want = it["expect"]
        cands = [m for m in rows if norm(it["value_contains"]) in norm(m["value_text"])
                 and (not it.get("metric_contains") or norm(it["metric_contains"]) in norm(m["metric"] + " " + m["quote"]))]
        detail = []
        good = None
        for m in cands:
            bad = [f"{k}={m.get(k)!r} (צפוי {v!r})" for k, v in want.items()
                   if (m.get(k) or "") != v and not (k == "area_basis" and v and v in (m.get(k) or ""))]
            if not bad:
                good = m
                break
            detail.append(f"{m['metric']} = {m['value_text']}: " + "; ".join(bad))
        if not cands:
            detail.append("לא נמצא נתון מתאים")
        out.append(Result("meaning", it["id"], good is not None, [] if good else detail))
    return out


def run_turn(c: httpx.Client, cid: str, content: str) -> dict:
    r = c.post(f"/api/chat/conversations/{cid}/messages", json={"content": content})
    r.raise_for_status()
    mid = r.json()["assistant"]["id"]
    started = time.monotonic()
    while True:
        m = c.get(f"/api/chat/messages/{mid}").json()
        if m["status"] not in ("running", "cancelling") or time.monotonic() - started > TURN_TIMEOUT:
            m["_seconds"] = round(time.monotonic() - started, 1)
            d = c.get(f"/api/chat/messages/{mid}/diagnostics")  # what verification removed, and why
            m["_diagnostics"] = d.json() if d.status_code == 200 else None
            return m
        time.sleep(POLL_SECONDS)


SOURCE_DOCS_MODES = ("cited", "if_needed")


class SetHashMismatch(Exception):
    """The question set is not the one whose hash was recorded when it was frozen."""


def check_set_sha(set_file: str | Path, sha_file: str | Path) -> str:
    """Round 13 U1 (KTD18): the sha256 of ``set_file`` must equal the one in ``sha_file`` (a ``shasum -a 256`` line,
    ``<hex>  <name>``; the first word of the first non-empty line). Returns the hash; raises ``SetHashMismatch`` naming
    the expected and the actual hash otherwise, or when the file holds no hash."""
    lines = [x for x in Path(sha_file).read_text(encoding="utf-8").splitlines() if x.strip()]
    expected = lines[0].split()[0].lower() if lines else ""
    actual = hashlib.sha256(Path(set_file).read_bytes()).hexdigest()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise SetHashMismatch(f"{sha_file} holds no sha256 hash (expected a `shasum -a 256` line); actual {actual}")
    if expected != actual:
        raise SetHashMismatch(f"{set_file} differs from the frozen set: expected {expected}, actual {actual}")
    return actual


def fragments_of(needle: str | list[str]) -> tuple[str, ...]:
    """A ``source_docs`` entry as its title fragments: one string, or a list of alternatives for a document the office
    stores under more than one title (round 13 U14: the same glossary is stored twice, once under a title the
    extraction garbled), any of which names it."""
    return (needle,) if isinstance(needle, str) else tuple(needle)


def title_holds(title: str | None, needle: str | list[str]) -> bool:
    return any(f in (title or "") for f in fragments_of(needle))


def professional_support(m: dict, expect: dict, needle: str | list[str]) -> list[str]:
    """The ``source_docs`` check of one fragment in ``if_needed`` mode, for a fragment no cited source's title holds
    (round 13 U1, KTD18): the problems, none when the answer needs no glossary citation.

    The payload has no marker on a claim saying it is professional (``claims`` is empty, ``components`` kinds are
    information, calculation, instruction, clarification), so the rule reads what the stored turn does hold:

    - a *professional statement* is a statement of the saved text (``split_units``) that cites a source whose
      ``source_kind`` is a standard or another professional document, or whose words match one of the turn's
      ``professional_patterns``; and, from the diagnostics, a claim the server took out (``removed``, ``withheld``,
      ``cut``) that was checked against such a source or matches a pattern (a removed one has no text to cite);
    - it is *verified by another cited source* when it cites a source of the answer whose title does not hold the
      fragment, and survives the verification: the claims that failed are the ones removed, cut or withheld, so a
      statement still standing in the text is a supported or partly supported one, provided the verifier ran
      (``verification.judged`` and ``judge_status == "ok"``).

    The answer passes when it has at least one professional statement, none was removed and every one is verified. It
    fails when a professional statement was removed, when one is not supported by another cited source, when the
    verification did not run, and when it has no professional statement at all, unless the turn's ``expect`` sets
    ``professional_not_asked`` (that waives only the missing statement). A professional statement that cites only an
    appraisal is seen only through ``professional_patterns``."""
    a = m.get("answer") or {}
    shown = " / ".join(fragments_of(needle))
    patterns = [re.compile(norm_pattern(p)) for p in expect.get("professional_patterns", [])]
    by_id = {s.get("id"): s for s in a.get("sources") or []}
    problems: list[str] = []

    def professional_kind(s: dict) -> bool:
        return s.get("kind") != "listing" and s.get("source_kind") not in (None, source_kinds.APPRAISAL)

    statements = []
    for u in split_units(a.get("markdown") or ""):
        cited = [by_id[i] for i in u.ids if i in by_id]
        text = norm(u.text)
        if any(professional_kind(s) for s in cited) or any(p.search(text) for p in patterns):
            statements.append((text, cited))
    verification = a.get("verification") or {}
    diagnostics = m.get("_diagnostics")
    handled = [d for d in (diagnostics or {}).get("removed") or [] if d.get("action") in ROW_ACTIONS]
    if diagnostics is None and (verification.get("removed") or verification.get("removals")):
        problems.append("הוסרו טענות מהתשובה ואין אבחון שמראה אם אחת מהן מקצועית")
    for d in handled:
        if any(s.get("type") == "sources" and professional_kind(s) for s in d.get("sources") or []) \
                or any(p.search(norm(d.get("text") or "")) for p in patterns):
            problems.append(f"טענה מקצועית הוסרה ({d['action']}) ואין ציטוט מהמסמך '{shown}': "
                            f"«{norm(d.get('text') or '')[:120]}»")
    if not statements:
        if not problems and not expect.get("professional_not_asked"):
            problems.append(f"אין בתשובה טענה מקצועית ואין בה ציטוט מהמסמך '{shown}'")
        return problems
    for text, cited in statements:
        if not any(not title_holds(s.get("title"), needle) for s in cited):
            problems.append(f"הטענה המקצועית אינה נתמכת במקור מצוטט אחר מ'{shown}': «{text[:120]}»")
    if not verification.get("judged") or verification.get("judge_status") != "ok":
        problems.append(f"הטענות המקצועיות לא אומתו (judged={verification.get('judged')}, "
                        f"judge_status={verification.get('judge_status')})")
    return problems


def grade_turn(m: dict, expect: dict, docs: dict) -> list[str]:
    """The regression layer: regular expressions, status and source documents."""
    problems = []
    a = m.get("answer") or {}
    text = norm(a.get("markdown") or "")
    if m["status"] != "done":
        return [f"סטטוס {m['status']}: {m.get('error')}"]
    for pat in expect.get("must", []):
        if not re.search(norm_pattern(pat), text):
            problems.append(f"חסר: /{pat}/")
    for pat in expect.get("must_not", []):
        hit = re.search(norm_pattern(pat), text)
        if hit:
            problems.append(f"אסור: /{pat}/ ← «{hit.group(0)}»")
    if "status" in expect and a.get("status") not in expect["status"]:
        problems.append(f"סטטוס תשובה {a.get('status')} (צפוי {expect['status']})")
    titles = {s.get("title") for s in a.get("sources") or []}
    mode = expect.get("source_docs_mode", "cited")
    if mode not in SOURCE_DOCS_MODES:
        problems.append(f"מצב source_docs לא מוכר '{mode}' (המצבים: {', '.join(SOURCE_DOCS_MODES)})")
        mode = "cited"
    for needle in expect.get("source_docs", []):
        if any(title_holds(t, needle) for t in titles):
            continue
        if mode == "if_needed":
            problems += professional_support(m, expect, needle)
        else:
            problems.append(f"אין מקור מהמסמך '{' / '.join(fragments_of(needle))}'")
    for needle in expect.get("not_source_docs", []):
        if any(needle in (t or "") for t in titles):
            problems.append(f"מקור ממסמך שלא אמור להופיע '{needle}'")
    return problems


def check_answers(c: httpx.Client, docs: dict, items: list[dict]) -> list[Result]:
    out = []
    for it in items:
        cid = c.post("/api/chat/conversations").json()["id"]
        turns = []
        ok = True
        detail = []
        for i, t in enumerate(it["turns"]):
            m = run_turn(c, cid, t["ask"])
            expect = t.get("expect") or {}
            problems, structured, calc, lines = grade(m, expect, docs, i + 1)
            original = original_grade(m, t, docs)
            measures = turn_measures(m.get("answer"), measure_expect(t), turn_timing({"diagnostics": m["_diagnostics"]}))
            ok = ok and not problems and not structured and not calc
            detail += lines
            a = m.get("answer") or {}
            turns.append({"ask": t["ask"], "status": m["status"], "answer_status": a.get("status"),
                          "structured_problems": structured, "passed_original_reference": original,
                          "calc_problems": calc, "calc_checked": bool(expect.get("calc")), "measures": measures,
                          "markdown": a.get("markdown"), "seconds": m["_seconds"], "problems": problems,
                          "sources": [f"{s['id']} {s['title']} — {s['location']}" for s in a.get("sources") or []],
                          "searches": a.get("searches"), "verification": a.get("verification"),
                          "diagnostics": m.get("_diagnostics"),
                          "progress": [p["label"] for p in m.get("progress") or []], "error": m.get("error"),
                          # the full payload, so the turn can be rescored later with new expectations
                          "answer": a, "usage": m.get("usage") or []})
        out.append(Result("answers", it["id"], ok, detail, {"category": it.get("category"), "turns": turns,
                                                            "conversation_id": cid,
                                                            "corrections": corrections_of(it["turns"])}))
    return out


MEASURE_KEYS = ("expect_document", "expect_unit_pages", "expect_source_pages", "expect_read_pages")


def measure_expect(spec_turn: dict) -> dict:
    """The round 9 measure expectations of a turn: in its ``expect``, or named beside it."""
    return {k: spec_turn[k] for k in MEASURE_KEYS if k in spec_turn} | (spec_turn.get("expect") or {})


def grade(m: dict, expect: dict, docs: dict, turn: int) -> tuple[list[str], list[str], list[str], list[str]]:
    """The three layers for one turn: (regression problems, structured problems, calculation problems, report
    lines)."""
    problems = grade_turn(m, expect, docs)
    done = m["status"] == "done"
    structured = structured_check(m.get("answer"), expect) if done else []
    calc = calc_check(m.get("answer"), expect) if done else []
    detail = ([f"תור {turn}: {p}" for p in problems] + [f"תור {turn} (מבני): {p}" for p in structured]
              + [f"תור {turn} (חישוב): {p}" for p in calc])
    return problems, structured, calc, detail


def corrections_of(spec_turns: list[dict]) -> list[dict]:
    """The reference corrections of a conversation's turns, for the report (without the original reference)."""
    return [{"turn": i + 1, **{k: v for k, v in t["reference_corrected"].items() if k != "was"}}
            for i, t in enumerate(spec_turns) if t.get("reference_corrected")]


def original_grade(m: dict, spec_turn: dict, docs: dict) -> bool | None:
    """For a turn whose reference was corrected: whether it passed under the original (wrong) reference."""
    fix = spec_turn.get("reference_corrected")
    if not fix:
        return None
    problems, structured, calc, _ = grade(m, fix.get("was") or {}, docs, 0)
    return not problems and not structured and not calc


def rescore(spec: dict, stored: list[dict]) -> list[Result]:
    """Stored results graded again with the set's current expectations, without a model call or the stack:
    answers from their stored payload, ingestion items from the blocks the run record keeps, retrieval from the
    stored passages. A result whose evidence was not stored is kept as it was."""
    items = {it["id"]: it for it in spec.get("answers", [])}
    searches = {it["id"]: it for it in spec.get("retrieval", [])}
    run = next((r for r in stored if r["kind"] == "run"), None)
    evidence = {e["title"]: e for e in ((run or {}).get("data", {}).get("documents") or {}).values()
                if e.get("blocks") is not None}
    regraded = {(x.kind, x.id): x for it in spec.get("ingestion", [])
                if (info := find_doc(evidence, it["document"])) is not None for x in grade_ingestion(it, info)}
    out = []
    for r in stored:
        kind = r["kind"]
        if kind == "ingestion" and (r.get("data") or {}).get("layer") != "structure":
            kind = "ingestion_text"  # stored before the structural layer: the text layer
        if kind in ("ingestion", "ingestion_text"):
            out.append(regraded.pop((kind, r["id"]), None) or Result(**(r | {"kind": kind})))
            continue
        if kind == "retrieval" and r["id"] in searches and "hits" in (r.get("data") or {}):
            out.append(grade_retrieval(searches[r["id"]], r["data"]["hits"], r["data"].get("titles")))
            continue
        if kind != "answers" or r["id"] not in items:
            out.append(Result(**r))
            continue
        ok, detail = True, []
        turns = r["data"].get("turns", [])
        for i, (t, spec_turn) in enumerate(zip(turns, items[r["id"]]["turns"], strict=False)):
            m = {"status": t["status"], "error": t.get("error"), "_diagnostics": t.get("diagnostics"),
                 "answer": t.get("answer") or {
                "markdown": t.get("markdown"), "status": t.get("answer_status"),
                "sources": [{"title": s.split(" ", 1)[1].rsplit(" — ", 1)[0], "location": s.rsplit(" — ", 1)[-1]}
                            for s in t.get("sources") or []]}}
            expect = spec_turn.get("expect") or {}
            problems, structured, calc, lines = grade(m, expect, {}, i + 1)
            t["problems"], t["structured_problems"], t["calc_problems"] = problems, structured, calc
            t["calc_checked"] = bool(expect.get("calc"))
            t["measures"] = turn_measures(m["answer"], measure_expect(spec_turn), turn_timing(t))
            t["passed_original_reference"] = original_grade(m, spec_turn, {})
            ok = ok and not problems and not structured and not calc
            detail += lines
        out.append(Result("answers", r["id"], ok, detail,
                          r["data"] | {"corrections": corrections_of(items[r["id"]]["turns"])}))
    # items the stored run did not check, graded from the evidence it kept
    return out + list(regraded.values())


def _git(*args: str) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=Path(__file__).resolve().parent, capture_output=True, text=True,
                           timeout=10, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip()


def build_info(build: str | None, image: str | None) -> dict:
    """What the run ran against: the checkout's commit at run time (None outside a checkout) and whether its
    tracked files had changes, the build named with ``--build`` (the stack may run another build than the
    checkout), and the backend image id given with ``--image``."""
    commit = _git("rev-parse", "HEAD") or None
    changes = _git("status", "--porcelain", "--untracked-files=no") if commit else None
    return {"commit": commit, "dirty": bool(changes), "build": build or None, "image": image or None}


def run_record(info: dict, used: dict[str, dict]) -> Result:
    return Result("run", "run", True, [], info | {"documents": used})


def models_per_purpose(results: list[Result]) -> dict[str, list[str]]:
    """The models each purpose's calls ran on, as the usage of the stored turns records them."""
    seen: dict[str, set[str]] = {}
    for r in results:
        if r.kind == "answers":
            for t in r.data.get("turns", []):
                for u in t.get("usage") or []:
                    seen.setdefault(u.get("purpose") or "-", set()).add(u.get("model") or "לא ידוע")
    return {k: sorted(v) for k, v in sorted(seen.items())}


def run_lines(results: list[Result]) -> list[str]:
    run = next((r for r in results if r.kind == "run"), None)
    models = models_per_purpose(results)
    if run is None and not models:
        return []
    lines = ["## build", ""]
    if run is not None:
        d = run.data
        lines.append(f"- commit: {d.get('commit') or 'לא ידוע'}" + (" (עם שינויים שלא נשמרו)" if d.get("dirty") else "")
                     + (f"; build: {d['build']}" if d.get("build") else "")
                     + f"; image: {d.get('image') or 'לא נמסר'}")
    if models:
        lines.append("- מודל לפי מטרה: " + "; ".join(f"{k}: {', '.join(v)}" for k, v in models.items()))
    docs = sorted(((run.data.get("documents") or {}) if run else {}).values(), key=lambda e: e.get("title") or "")
    if docs:
        lines += ["", "| מסמך | ingestion_version | reading_id |", "|---|---|---|"]
        lines += [f"| {e.get('title')} | {e.get('ingestion_version') or '—'} | {e.get('reading_id') or '—'} |"
                  for e in docs]
    return lines + [""]


def call_cost(u: dict) -> float | None:
    """One call's estimated cost: as priced when it was logged, else from the price table by the call's model
    and token buckets; None when the model is unknown or unpriced (never counted as free)."""
    if "cost_usd" in u:
        return u["cost_usd"]
    from app.providers.llm import TOKEN_FIELDS, usage_cost

    return usage_cost(u.get("model"), **{k: u.get(k) for k in TOKEN_FIELDS})


def usage_summary(results: list[Result]) -> list[str]:
    """Cost and latency per turn (failed and cancelled turns included: their calls were billed): model calls,
    token buckets with the cache shares, and the cost per purpose and model at the price of each call's model."""
    turns = [t for r in results if r.kind == "answers" for t in r.data.get("turns", []) if t.get("usage")]
    if not turns:
        return []
    calls = [u for t in turns for u in t["usage"]]

    def tok(us, key) -> int:
        return sum(u.get(key) or 0 for u in us)

    def cost(us) -> float:
        return sum(c for u in us if (c := call_cost(u)) is not None)

    def share(us, key) -> str:
        total = tok(us, "input_tokens")
        return f"{tok(us, key) / total:.0%}" if total else "—"

    def mean(xs) -> float:
        return sum(xs) / len(xs)

    by_purpose: dict[str, int] = {}
    for u in calls:
        by_purpose[u.get("purpose") or "-"] = by_purpose.get(u.get("purpose") or "-", 0) + 1
    secs = [t["seconds"] for t in turns]
    unknown_cache = sum(1 for u in calls if u.get("cached_input_tokens") is None)
    unpriced = sum(1 for u in calls if call_cost(u) is None)
    total = cost(calls)
    lines = ["## usage", "",
             f"- תורות: {len(turns)}; קריאות למודל לתור: ממוצע {len(calls) / len(turns):.2f} ("
             + ", ".join(f"{k} {v / len(turns):.2f}" for k, v in sorted(by_purpose.items())) + ")",
             f"- טוקני קלט לתור: ממוצע {mean([tok(t['usage'], 'input_tokens') for t in turns]):,.0f}; מתוכם נקראו "
             f"מ-cache {share(calls, 'cached_input_tokens')}, נכתבו ל-cache {share(calls, 'cache_write_tokens')}"
             + (f" (ב-{unknown_cache} קריאות הספק לא דיווח)" if unknown_cache else ""),
             f"- טוקני פלט לתור: ממוצע {mean([tok(t['usage'], 'output_tokens') for t in turns]):,.0f}",
             f"- עלות לתור (לפי מחירון המודל של כל קריאה): ממוצע {total / len(turns):.4f}$, סה\"כ {total:.3f}$"
             + (f" — בלי {unpriced} קריאות שעלותן לא ידועה (מודל לא מתומחר או ללא דיווח טוקנים)" if unpriced else ""),
             f"- זמן לתור: חציון {percentile(secs, 0.5)} שנ׳, p90 {percentile(secs, 0.9)} שנ׳, מקסימום {max(secs)} שנ׳",
             f"- עלות לשיחה שעברה: {cost_per_pass(results, total)}", "",
             "| מטרה | מודל | קריאות | קלט | מ-cache | כתיבה ל-cache | פלט | עלות | עלות לתור |",
             "|---|---|---|---|---|---|---|---|---|"]
    groups: dict[tuple[str, str], list[dict]] = {}
    for u in calls:
        groups.setdefault((u.get("purpose") or "-", u.get("model") or "לא ידוע"), []).append(u)
    for (purpose, model), us in sorted(groups.items()):
        lines.append(f"| {purpose} | {model} | {len(us)} | {tok(us, 'input_tokens'):,} | "
                     f"{share(us, 'cached_input_tokens')} | {share(us, 'cache_write_tokens')} | "
                     f"{tok(us, 'output_tokens'):,} | {cost(us):.4f}$ | {cost(us) / len(turns):.5f}$ |")
    return lines + [""]


def cost_per_pass(results: list[Result], total: float) -> str:
    passed = sum(r.ok for r in results if r.kind == "answers")
    return f"{total / passed:.4f}$ ({passed} שיחות עברו)" if passed else "אין שיחות שעברו"


def original_score(rs: list[Result]) -> int:
    """Conversations that passed under the original references: a corrected turn counts as it graded before."""
    def turn_ok(t: dict) -> bool:
        if t.get("passed_original_reference") is not None:
            return t["passed_original_reference"]
        return not t.get("problems") and not t.get("structured_problems") and not t.get("calc_problems")
    return sum(all(turn_ok(t) for t in r.data.get("turns", [])) for r in rs)


def resolution_lines(turn: dict) -> list[str]:
    """How a failed turn's follow-up was resolved, to tell the stage of the failure: the resolving model's parse,
    the server's decision on each field, the entity lookup, and the request the answer was held to."""
    res = (turn.get("diagnostics") or {}).get("resolution")
    req = (turn.get("answer") or {}).get("request")
    if not res and not req:
        return []
    out = []
    if res:
        parse = res.get("parse") or {}
        claimed = ", ".join(f"{c['field']}=«{c['user_words']}»" for c in parse.get("changed_fields") or [])
        out.append(f"    - פענוח: {parse.get('relation')} / {parse.get('scope')}; מדד {parse.get('metric_kind')}, "
                   f"סקאלה {parse.get('scale')}; שינויים: {claimed or 'אין'}")
        if res.get("decisions"):
            out.append("    - החלטות השרת: " + "; ".join(f"{k}: {v}" for k, v in res["decisions"].items()))
        if res.get("lookup"):
            lk = res["lookup"]
            out.append(f"    - איתור ישות: {lk.get('kind')} («{lk.get('query')}»): "
                       + ", ".join(d.get("title", "") for d in lk.get("documents") or []))
    if req:
        out.append(f"    - הבקשה שאושרה: מדד {req.get('metric_kind')}, יחידה {req.get('unit')}, מוחזק ל-"
                   f"{','.join(req.get('approved') or []) or '—'}; מסמכים {len(req.get('document_ids') or [])}")
    return out


def stage_time_lines(results: list[Result]) -> list[str]:
    """Time per stage (R29): median, p90 and maximum seconds over the turns that ran the stage, with the turns counted
    and the mean calls; a stage no stored turn has a timing for shows "—" (results from before it was recorded)."""
    turns = [t for r in results if r.kind == "answers" for t in r.data.get("turns", [])]
    if not turns:
        return []

    def cell(x, fmt: str = "{:.2f}") -> str:
        return fmt.format(x) if x is not None else "—"

    stats = stage_time_stats(turns)
    with_timing = stats["total"]["n"]
    lines = ["## זמן לפי שלב", "",
             f"תורות עם תזמון: {with_timing} מתוך {len(turns)}; שניות, רק תורות שהשלב רץ בהן.", "",
             "| שלב | תורות | חציון | p90 | מקסימום | קריאות לתור |", "|---|---|---|---|---|---|"]
    for stage, st in stats.items():
        lines.append(f"| {stage} | {st['n']} | {cell(st['median'])} | {cell(st['p90'])} | {cell(st['max'])} | "
                     f"{cell(st['calls'], '{:.1f}')} |")
    return lines + [""]


def turn_cost(t: dict) -> float | None:
    """A turn's cost from its calls' usage; None without usage (calls of unknown price count as nothing)."""
    return sum(c for u in t["usage"] if (c := call_cost(u)) is not None) if t.get("usage") else None


def _mark(ok: bool | None, detail: str = "") -> str:
    return "—" if ok is None else ("✓" if ok else "✗") + (f" {detail}" if detail else "")


def _pages(xs: list[int]) -> str:
    return ", ".join(map(str, xs))


def measure_cells(t: dict) -> list[str]:
    """One turn's cell per measure, each apart: unit identification, source location, reading completeness,
    calculation, completeness after verification (the structured layer), then time and cost."""
    m = t.get("measures") or {}
    unit, loc, read = m.get("unit"), m.get("location"), m.get("reading")
    done = t.get("status") == "done"
    cost = turn_cost(t)

    def fraction(x: dict | None) -> str:
        if x is None:
            return "—"
        return f"{x['found']}/{x['of']}" + (f" (חסרים: {_pages(x['missing'])})" if x["missing"] else "")

    return [
        _mark(unit["passed"], f"(מחוץ ליחידה: {_pages(unit['outside'])})" if unit['outside'] else
              ("" if unit["passed"] else "(אין מקור)")) if unit else "—",
        fraction(loc), fraction(read),
        _mark(not t.get("calc_problems") if t.get("calc_checked") else None),
        _mark(not t.get("structured_problems") if done else None),
        f"{t['seconds']}" if t.get("seconds") is not None else "—",
        f"{cost:.4f}$" if cost is not None else "—"]


def measure_lines(rs: list[Result]) -> list[str]:
    """The separate measures per turn (R29), one column each, and their totals over the turns that name them."""
    rows = [(r.id, n, t) for r in rs for n, t in enumerate(r.data.get("turns", []), 1)]
    if not rows:
        return []
    named = {k: [t["measures"][k] for _, _, t in rows if k in (t.get("measures") or {})]
             for k in ("unit", "location", "reading")}
    totals = []
    if named["unit"]:
        totals.append(f"זיהוי יחידה: {sum(u['passed'] for u in named['unit'])}/{len(named['unit'])} תורות")
    for key, name in (("location", "מיקום מקורות"), ("reading", "שלמות קריאה")):
        if named[key]:
            totals.append(f"{name}: ממוצע {sum(x['fraction'] for x in named[key]) / len(named[key]):.0%} "
                          f"({len(named[key])} תורות)")
    lines = ["", "מדדים נפרדים לתור (אינם נספרים בציון השיחה):"]
    if totals:
        lines += ["", "- " + "; ".join(totals)]
    if named["reading"]:
        lines.append("- שלמות הקריאה נמדדת לפי עמודי המקורות המצוטטים; רשומות הקריאה בתזמון אינן נושאות עמודים, "
                     "ועמוד שנקרא ולא צוטט אינו נראה")
    lines += ["", "| שיחה | תור | זיהוי יחידה | מיקום מקורות | שלמות קריאה | חישוב | שלמות אחרי אימות | זמן (שנ׳) | עלות |",
              "|---|---|---|---|---|---|---|---|---|"]
    lines += [f"| {cid} | {n} | " + " | ".join(measure_cells(t)) + " |" for cid, n, t in rows]
    return lines


def report(results: list[Result], path: Path, title: str) -> str:
    lines = [f"# {title}", ""]
    lines += run_lines(results)
    lines += usage_summary(results)
    lines += stage_time_lines(results)
    for kind in ("ingestion", "ingestion_text", "retrieval", "meaning", "answers"):
        rs = [r for r in results if r.kind == kind]
        if not rs:
            continue
        passed = sum(r.ok for r in rs)
        lines.append(f"## {kind}: {passed}/{len(rs)}")
        if kind == "answers":
            regex_ok = sum(all(not t.get("problems") for t in r.data.get("turns", [])) for r in rs)
            struct_ok = sum(all(not t.get("structured_problems") for t in r.data.get("turns", [])) for r in rs)
            calc_rs = [r for r in rs if any(t.get("calc_checked") for t in r.data.get("turns", []))]
            calc_ok = sum(all(not t.get("calc_problems") for t in r.data.get("turns", [])) for r in calc_rs)
            lines.append(f"\nשכבת הביטויים (רגרסיה): {regex_ok}/{len(rs)}; שכבה מבנית: {struct_ok}/{len(rs)}"
                         + (f"; שכבת חישוב: {calc_ok}/{len(calc_rs)}" if calc_rs else ""))
            fixes = [(r.id, c) for r in rs for c in r.data.get("corrections") or []]
            if fixes:
                lines.append(f"\nציון אוטומטי מול הייחוס המקורי: {original_score(rs)}/{len(rs)}; אחרי תיקוני ייחוס: "
                             f"{passed}/{len(rs)}")
                lines += [f"- תיקון ייחוס {cid} תור {c['turn']} ({c.get('date', '')}): {c.get('evidence', '')}"
                          for cid, c in fixes]
        if kind == "answers":
            cats: dict[str, list[Result]] = {}
            for r in rs:
                cats.setdefault(r.data.get("category") or "-", []).append(r)
            lines.append("")
            lines.append("| קטגוריה | עברו |")
            lines.append("|---|---|")
            for k, v in sorted(cats.items()):
                lines.append(f"| {k} | {sum(x.ok for x in v)}/{len(v)} |")
            secs = [t["seconds"] for r in rs for t in r.data.get("turns", [])]
            if secs:
                lines.append(f"\nזמן לתור: חציון {sorted(secs)[len(secs) // 2]} שנ׳, מקסימום {max(secs)} שנ׳")
            lines += measure_lines(rs)
        lines.append("")
        for r in rs:
            mark = "✓" if r.ok else "✗"
            lines.append(f"- {mark} **{r.id}**" + ("" if r.ok else ": " + " | ".join(r.detail)))
            if kind == "answers":
                for t in r.data.get("turns", []):
                    lines.append(f"  - שאלה: {t['ask']}")
                    lines.append("    - תשובה: " + (t.get("markdown") or (t.get("answer") or {}).get("markdown") or t.get("error") or "").replace("\n", " ⏎ ")[:1500])
                    if not r.ok:
                        lines += resolution_lines(t)
        lines.append("")
    text_ = "\n".join(lines)
    path.write_text(text_, encoding="utf-8")
    return text_


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--email", default="admin-a@demo.test")
    ap.add_argument("--password", default="demo1234")
    ap.add_argument("--only", default="")
    ap.add_argument("--skip-answers", action="store_true")
    ap.add_argument("--rescore", default="")
    ap.add_argument("--office", default="A", help="run the answer items of this office only (items without "
                    "an `office` key belong to A); sign in with that office's --email")
    ap.add_argument("--build", default="", help="the build the stack runs, recorded beside the checkout's commit")
    ap.add_argument("--image", default="", help="the backend image id, recorded in the report")
    ap.add_argument("--sha", default="", help="a `shasum -a 256` line of the frozen set: a --set that differs is "
                    "refused (exit 2) before anything runs")
    args = ap.parse_args(argv)
    if args.sha:
        try:
            check_set_sha(args.set, args.sha)
        except (SetHashMismatch, OSError) as e:
            print(f"refused: {e}", file=sys.stderr)
            return 2
    spec = yaml.safe_load(Path(args.set).read_text(encoding="utf-8"))
    if args.rescore:
        stored = json.loads(Path(args.rescore).read_text(encoding="utf-8"))
        results = rescore(spec, stored)
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (out / f"rescored-{stamp}.json").write_text(json.dumps([r.__dict__ for r in results], ensure_ascii=False,
                                                               indent=1), encoding="utf-8")
        text_ = report(results, out / f"rescored-{stamp}.md", spec.get("title", "הערכה") + " (ניקוד מחדש)")
        print("\n".join(line for line in text_.splitlines() if line.startswith("## ") or "שכבה מבנית" in line))
        return 0
    only = {x for x in args.only.split(",") if x}
    c = login(args.base, args.email, args.password)
    docs = documents(c)
    results: list[Result] = []
    used: dict[str, dict] = {}

    def pick(xs: list[dict]) -> list[dict]:
        return [x for x in xs if not only or x["id"] in only]

    results += check_ingestion(c, docs, pick(spec.get("ingestion", [])), used)
    results += check_retrieval(c, pick(spec.get("retrieval", [])))
    meaning = pick(spec.get("meaning", []))
    results += check_meaning(c, docs, meaning)
    if not args.skip_answers:
        results += check_answers(c, docs, [x for x in pick(spec.get("answers", []))
                                       if x.get("office", "A") == args.office])
    # every other document the run used: checked for meaning, or cited by an answer
    by_id = {d["id"]: d for d in docs.values()}
    cited = {s.get("document_id") for r in results if r.kind == "answers" for t in r.data.get("turns", [])
             for s in (t.get("answer") or {}).get("sources") or []}
    for d in [find_doc(docs, it["document"]) for it in meaning] + [by_id.get(i) for i in cited]:
        if d is not None and d["id"] not in used:
            used[d["id"]] = doc_info(d, fetch_blocks(c, d, first_only=True)[1])
    results.insert(0, run_record(build_info(args.build, args.image), used))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (out / f"results-{stamp}.json").write_text(json.dumps([r.__dict__ for r in results], ensure_ascii=False, indent=1),
                                               encoding="utf-8")
    text_ = report(results, out / f"report-{stamp}.md", spec.get("title", "הערכה"))
    print("\n".join(line for line in text_.splitlines() if line.startswith("## ")))
    for r in results:
        if not r.ok:
            print(f"FAIL {r.kind} {r.id}: {' | '.join(r.detail)[:400]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
