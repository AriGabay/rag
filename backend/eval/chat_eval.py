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
- ``answers``: conversations. Each turn's answer is checked with ``must`` / ``must_not`` regular expressions,
  allowed answer statuses, and the documents its sources must (and must not) come from.

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

POLL_SECONDS = 1.5
TURN_TIMEOUT = 300


@dataclass
class Result:
    kind: str
    id: str
    ok: bool
    detail: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)


def norm(s: str) -> str:
    return (s or "").replace("״", '"').replace("׳", "'").replace("–", "-").replace("‏", "")


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
            problems = grade_turn(m, t.get("expect") or {}, docs)
            ok = ok and not problems
            detail += [f"תור {i + 1}: {p}" for p in problems]
            a = m.get("answer") or {}
            turns.append({"ask": t["ask"], "status": m["status"], "answer_status": a.get("status"),
                          "markdown": a.get("markdown"), "seconds": m["_seconds"], "problems": problems,
                          "sources": [f"{s['id']} {s['title']} — {s['location']}" for s in a.get("sources") or []],
                          "searches": a.get("searches"), "verification": a.get("verification"),
                          "progress": [p["label"] for p in m.get("progress") or []], "error": m.get("error")})
        out.append(Result("answers", it["id"], ok, detail, {"category": it.get("category"), "turns": turns,
                                                            "conversation_id": cid}))
    return out


def report(results: list[Result], path: Path, title: str) -> str:
    lines = [f"# {title}", ""]
    for kind in ("ingestion", "retrieval", "meaning", "answers"):
        rs = [r for r in results if r.kind == kind]
        if not rs:
            continue
        passed = sum(r.ok for r in rs)
        lines.append(f"## {kind}: {passed}/{len(rs)}")
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
    args = ap.parse_args(argv)
    spec = yaml.safe_load(Path(args.set).read_text(encoding="utf-8"))
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
        results += check_answers(c, docs, pick(spec.get("answers", [])))
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
