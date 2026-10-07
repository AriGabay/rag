"""Evaluation of the conversational engine against a running stack, with reference answers fixed in advance.

Usage::

    uv run python -m eval.chat_eval --set <questions.yaml> --out <dir> [--base http://localhost:8000]
                                    [--email ...] [--password ...] [--only C1,C3]

The question set is YAML. It may describe real office documents, so the set and the results belong outside
the repository (``real_documents/`` is ignored by git); the runner holds no document content.

Four separate measures, never folded into one number:

- ``ingestion``: text that must be readable in a document's blocks (what was read from pictures and tables);
- ``retrieval``: for a search query, a text that must be among the top passages;
- ``meaning``: stored measurements that must carry the stated kind, period, VAT, area basis or form;
- ``answers``: conversations. Each turn's answer is checked in two layers, reported separately: the regression
  layer (``must`` / ``must_not`` regular expressions, allowed answer statuses, the documents its sources must and
  must not come from) and the structured layer (``eval.scoring``: required documents, value sets, the meaning
  and attribution of values, coverage and hedging), which fails answers that only contain the right words.

``--rescore <results.json>`` grades stored answers again with the set's current expectations (no model calls),
so two runs can be compared on the same answers and expectations.

Each check passes or fails on its own; the report lists every failure with the text that caused it, so a
person can read the failing answers.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

from eval.scoring import check as structured_check
from eval.scoring import norm

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


def check_ingestion(c: httpx.Client, docs: dict, items: list[dict]) -> list[Result]:
    out = []
    for it in items:
        d = find_doc(docs, it["document"])
        if d is None:
            out.append(Result("ingestion", it["id"], False, ["המסמך לא נמצא"]))
            continue
        v = d["current_version"]
        blocks = c.get(f"/api/documents/{d['id']}/versions/{v['id']}/blocks").json()["blocks"]
        text = norm("\n".join(b["text"] for b in blocks))
        missing = [t for t in it["must_read"] if norm(t) not in text]
        out.append(Result("ingestion", it["id"], not missing, [f"לא נקרא: {m}" for m in missing],
                          {"reading": v.get("reading")}))
    return out


def check_retrieval(c: httpx.Client, items: list[dict]) -> list[Result]:
    out = []
    for it in items:
        hits = c.get("/api/search", params={"q": it["query"], "limit": it.get("k", 8)}).json()["results"]
        texts = [norm(h.get("text") or h.get("snippet") or "") for h in hits]
        rank = next((i + 1 for i, t in enumerate(texts) if norm(it["expect"]) in t), None)
        out.append(Result("retrieval", it["id"], rank is not None,
                          [] if rank else [f"לא בתוצאות הראשונות: {it['expect']}"], {"rank": rank}))
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
            return m
        time.sleep(POLL_SECONDS)


def grade_turn(m: dict, expect: dict, docs: dict) -> list[str]:
    """The regression layer: regular expressions, status and source documents."""
    problems = []
    a = m.get("answer") or {}
    text = norm(a.get("markdown") or "")
    if m["status"] != "done":
        return [f"סטטוס {m['status']}: {m.get('error')}"]
    for pat in expect.get("must", []):
        if not re.search(norm(pat), text):
            problems.append(f"חסר: /{pat}/")
    for pat in expect.get("must_not", []):
        hit = re.search(norm(pat), text)
        if hit:
            problems.append(f"אסור: /{pat}/ ← «{hit.group(0)}»")
    if "status" in expect and a.get("status") not in expect["status"]:
        problems.append(f"סטטוס תשובה {a.get('status')} (צפוי {expect['status']})")
    titles = {s.get("title") for s in a.get("sources") or []}
    for needle in expect.get("source_docs", []):
        if not any(needle in (t or "") for t in titles):
            problems.append(f"אין מקור מהמסמך '{needle}'")
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
            problems, structured, lines = grade(m, t.get("expect") or {}, docs, i + 1)
            ok = ok and not problems and not structured
            detail += lines
            a = m.get("answer") or {}
            turns.append({"ask": t["ask"], "status": m["status"], "answer_status": a.get("status"),
                          "structured_problems": structured,
                          "markdown": a.get("markdown"), "seconds": m["_seconds"], "problems": problems,
                          "sources": [f"{s['id']} {s['title']} — {s['location']}" for s in a.get("sources") or []],
                          "searches": a.get("searches"), "verification": a.get("verification"),
                          "progress": [p["label"] for p in m.get("progress") or []], "error": m.get("error"),
                          # the full payload, so the turn can be rescored later with new expectations
                          "answer": a, "usage": m.get("usage") or []})
        out.append(Result("answers", it["id"], ok, detail, {"category": it.get("category"), "turns": turns,
                                                            "conversation_id": cid}))
    return out


def grade(m: dict, expect: dict, docs: dict, turn: int) -> tuple[list[str], list[str], list[str]]:
    """Both layers for one turn: (regression problems, structured problems, report lines)."""
    problems = grade_turn(m, expect, docs)
    structured = structured_check(m.get("answer"), expect) if m["status"] == "done" else []
    detail = [f"תור {turn}: {p}" for p in problems] + [f"תור {turn} (מבני): {p}" for p in structured]
    return problems, structured, detail


def rescore(spec: dict, stored: list[dict]) -> list[Result]:
    """Stored answers graded again with the set's current expectations."""
    items = {it["id"]: it for it in spec.get("answers", [])}
    out = []
    for r in stored:
        if r["kind"] != "answers" or r["id"] not in items:
            out.append(Result(**r))
            continue
        ok, detail = True, []
        turns = r["data"].get("turns", [])
        for i, (t, spec_turn) in enumerate(zip(turns, items[r["id"]]["turns"], strict=False)):
            m = {"status": t["status"], "error": t.get("error"), "answer": t.get("answer") or {
                "markdown": t.get("markdown"), "status": t.get("answer_status"),
                "sources": [{"title": s.split(" ", 1)[1].rsplit(" — ", 1)[0]} for s in t.get("sources") or []]}}
            problems, structured, lines = grade(m, spec_turn.get("expect") or {}, {}, i + 1)
            t["problems"], t["structured_problems"] = problems, structured
            ok = ok and not problems and not structured
            detail += lines
        out.append(Result("answers", r["id"], ok, detail, r["data"]))
    return out


# list prices of the configured model (USD per million tokens); cached input is billed at the cached rate.
# Assumed for reporting only: the application has no budget logic.
PRICE_INPUT, PRICE_CACHED, PRICE_OUTPUT = 0.75, 0.075, 4.50


def usage_summary(results: list[Result]) -> list[str]:
    """Cost and latency per answered turn: model calls, input / cached / output tokens, price, seconds."""
    turns = [t for r in results if r.kind == "answers" for t in r.data.get("turns", []) if t.get("usage")]
    if not turns:
        return []

    def per(fn) -> list[float]:
        return [fn(t) for t in turns]

    def tok(t, key) -> int:
        return sum(u.get(key) or 0 for u in t["usage"])

    def cost(t) -> float:
        cached = tok(t, "cached_input_tokens")
        return ((tok(t, "input_tokens") - cached) * PRICE_INPUT + cached * PRICE_CACHED
                + tok(t, "output_tokens") * PRICE_OUTPUT) / 1e6

    def mean(xs) -> float:
        return sum(xs) / len(xs)

    def pct(xs, q) -> float:
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(q * len(xs)))]

    calls = per(lambda t: len(t["usage"]))
    by_purpose: dict[str, int] = {}
    for t in turns:
        for u in t["usage"]:
            by_purpose[u.get("purpose") or "-"] = by_purpose.get(u.get("purpose") or "-", 0) + 1
    secs = per(lambda t: t["seconds"])
    unknown_cache = sum(1 for t in turns for u in t["usage"] if u.get("cached_input_tokens") is None)
    return ["## usage", "",
            f"- תורות: {len(turns)}; קריאות למודל לתור: ממוצע {mean(calls):.2f} ("
            + ", ".join(f"{k} {v / len(turns):.2f}" for k, v in sorted(by_purpose.items())) + ")",
            f"- טוקני קלט לתור: ממוצע {mean(per(lambda t: tok(t, 'input_tokens'))):,.0f}; מתוכם מ-cache: "
            f"{mean(per(lambda t: tok(t, 'cached_input_tokens'))):,.0f}"
            + (f" (ב-{unknown_cache} קריאות הספק לא דיווח)" if unknown_cache else ""),
            f"- טוקני פלט לתור: ממוצע {mean(per(lambda t: tok(t, 'output_tokens'))):,.0f}",
            f"- עלות לתור (מחירון {PRICE_INPUT}/{PRICE_CACHED}/{PRICE_OUTPUT}$ למיליון): ממוצע "
            f"{mean(per(cost)):.4f}$, סה\"כ {sum(per(cost)):.3f}$",
            f"- זמן לתור: חציון {pct(secs, 0.5)} שנ׳, p90 {pct(secs, 0.9)} שנ׳, מקסימום {max(secs)} שנ׳", ""]


def report(results: list[Result], path: Path, title: str) -> str:
    lines = [f"# {title}", ""]
    lines += usage_summary(results)
    for kind in ("ingestion", "retrieval", "meaning", "answers"):
        rs = [r for r in results if r.kind == kind]
        if not rs:
            continue
        passed = sum(r.ok for r in rs)
        lines.append(f"## {kind}: {passed}/{len(rs)}")
        if kind == "answers":
            regex_ok = sum(all(not t.get("problems") for t in r.data.get("turns", [])) for r in rs)
            struct_ok = sum(all(not t.get("structured_problems") for t in r.data.get("turns", [])) for r in rs)
            lines.append(f"\nשכבת הביטויים (רגרסיה): {regex_ok}/{len(rs)}; שכבה מבנית: {struct_ok}/{len(rs)}")
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
        lines.append("")
        for r in rs:
            mark = "✓" if r.ok else "✗"
            lines.append(f"- {mark} **{r.id}**" + ("" if r.ok else ": " + " | ".join(r.detail)))
            if kind == "answers":
                for t in r.data.get("turns", []):
                    lines.append(f"  - שאלה: {t['ask']}")
                    lines.append("    - תשובה: " + (t["markdown"] or t.get("error") or "").replace("\n", " ⏎ ")[:1500])
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
    args = ap.parse_args(argv)
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

    def pick(xs: list[dict]) -> list[dict]:
        return [x for x in xs if not only or x["id"] in only]

    results += check_ingestion(c, docs, pick(spec.get("ingestion", [])))
    results += check_retrieval(c, pick(spec.get("retrieval", [])))
    results += check_meaning(c, docs, pick(spec.get("meaning", [])))
    if not args.skip_answers:
        results += check_answers(c, docs, [x for x in pick(spec.get("answers", []))
                                       if x.get("office", "A") == args.office])
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
