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

A reference answer found wrong is corrected in the set, not silently: the turn keeps the original expectation in
``reference_corrected: {date, evidence, was}`` beside the corrected ``expect``. Every run and rescore grades such
a turn both ways, and the report gives the original automated score and the score after reference corrections
apart, with each correction and its evidence. The cost table gives the cost per conversation that passed.

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
            d = c.get(f"/api/chat/messages/{mid}/diagnostics")  # what verification removed, and why
            m["_diagnostics"] = d.json() if d.status_code == 200 else None
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
            original = original_grade(m, t, docs)
            ok = ok and not problems and not structured
            detail += lines
            a = m.get("answer") or {}
            turns.append({"ask": t["ask"], "status": m["status"], "answer_status": a.get("status"),
                          "structured_problems": structured, "passed_original_reference": original,
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


def grade(m: dict, expect: dict, docs: dict, turn: int) -> tuple[list[str], list[str], list[str]]:
    """Both layers for one turn: (regression problems, structured problems, report lines)."""
    problems = grade_turn(m, expect, docs)
    structured = structured_check(m.get("answer"), expect) if m["status"] == "done" else []
    detail = [f"תור {turn}: {p}" for p in problems] + [f"תור {turn} (מבני): {p}" for p in structured]
    return problems, structured, detail


def corrections_of(spec_turns: list[dict]) -> list[dict]:
    """The reference corrections of a conversation's turns, for the report (without the original reference)."""
    return [{"turn": i + 1, **{k: v for k, v in t["reference_corrected"].items() if k != "was"}}
            for i, t in enumerate(spec_turns) if t.get("reference_corrected")]


def original_grade(m: dict, spec_turn: dict, docs: dict) -> bool | None:
    """For a turn whose reference was corrected: whether it passed under the original (wrong) reference."""
    fix = spec_turn.get("reference_corrected")
    if not fix:
        return None
    problems, structured, _ = grade(m, fix.get("was") or {}, docs, 0)
    return not problems and not structured


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
            t["passed_original_reference"] = original_grade(m, spec_turn, {})
            ok = ok and not problems and not structured
            detail += lines
        out.append(Result("answers", r["id"], ok, detail,
                          r["data"] | {"corrections": corrections_of(items[r["id"]]["turns"])}))
    return out


def call_cost(u: dict) -> float | None:
    """One call's estimated cost: as priced when it was logged, else from the price table by the call's model
    and token buckets; None when the model is unknown or unpriced (never counted as free)."""
    if "cost_usd" in u:
        return u["cost_usd"]
    from app.providers.llm import usage_cost

    return usage_cost(u.get("model"), **{k: u.get(k) for k in ("input_tokens", "cached_input_tokens",
                                                                  "cache_write_tokens", "output_tokens")})


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

    def pct(xs, q) -> float:
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(q * len(xs)))]

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
             f"- זמן לתור: חציון {pct(secs, 0.5)} שנ׳, p90 {pct(secs, 0.9)} שנ׳, מקסימום {max(secs)} שנ׳",
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
        return not t.get("problems") and not t.get("structured_problems")
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
        lines.append("")
        for r in rs:
            mark = "✓" if r.ok else "✗"
            lines.append(f"- {mark} **{r.id}**" + ("" if r.ok else ": " + " | ".join(r.detail)))
            if kind == "answers":
                for t in r.data.get("turns", []):
                    lines.append(f"  - שאלה: {t['ask']}")
                    lines.append("    - תשובה: " + (t["markdown"] or t.get("error") or "").replace("\n", " ⏎ ")[:1500])
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
