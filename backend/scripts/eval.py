"""Run the evaluation set against the LIVE demo stack and write docs/evaluation/eval-results.md (U15).

    cd backend && uv run python scripts/eval.py
    # options: --questions eval/questions.yaml --out ../docs/evaluation/eval-results.md --only N01,T04 --json out.json

Needs the Compose stack seeded with scripts/seed_demo.py (BACKEND_URL, default http://localhost:8000;
demo password from DEMO_PASSWORD, default demo1234). Expected values are computed from
tests/fixtures/ground_truth.yaml (eval/truth.py) for each item's conditions, the asking user's groups,
and the live review queue: records the queue still lists are not verified and are excluded.

Table-extraction and field metrics read the live database read-only through the runtime role in each
office's system context (EVAL_DATABASE_URL, default built from the repository .env). They are
reported separately from answer quality.

The answering provider of the demo stack is the deterministic mock (or the extractive fallback). These
numbers are NOT a benchmark of a cloud model and the latencies are not cloud-provider latencies.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))  # before scripts/, so `eval` is the package, not this file

from eval.flows import ask_flow  # noqa: E402
from eval.truth import (  # noqa: E402
    EXTRACTION_SQL_OCCURRENCES,
    EXTRACTION_SQL_TABLES,
    MIXED_KEYS,
    USERS,
    Filters,
    by_filename,
    docs,
    expected_clarifications,
    expected_stats,
    field_accuracy,
    gt_record_for,
    price_token,
    table_accuracy,
    title_of,
    unverified_from_queue,
)

BACKEND = os.environ.get("BACKEND_URL", "http://localhost:8000")
PASSWORD = os.environ.get("DEMO_PASSWORD", "demo1234")
REPO = ROOT.parent
GROUP_NAMES = {"שומות רמת גן וגבעתיים": "G1", "שומות פרויקטים": "G2"}  # scripts/seed_demo.py
ANSWERED = ("numeric", "combined", "content")
NUMERIC_FIELDS = ("record_count", "mean_price_per_sqm", "weighted_price_per_sqm", "median_price_per_sqm",
                  "min_price_per_sqm", "max_price_per_sqm")


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def rate(ok: int, n: int) -> str:
    return f"{ok}/{n} ({100 * ok / n:.1f}%)" if n else "n/a"


def big_numbers(s: str) -> list[str]:
    out = []
    for m in re.findall(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?", s or ""):
        v = float(m.replace(",", ""))
        if v >= 1000 and not (1950 <= v <= 2100 and "," not in m and "." not in m):
            out.append(m)
    return out


class Live:
    """Logged-in clients and the live document / verification state."""

    def __init__(self):
        self.clients: dict[str, httpx.Client] = {}
        self.versions: dict[str, dict[str, str]] = {}  # document id -> {version id: filename}
        self.files: dict[str, bytes] = {}

    def client(self, email: str) -> httpx.Client:
        if email not in self.clients:
            c = httpx.Client(base_url=BACKEND, timeout=120)
            r = c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
            r.raise_for_status()
            self.clients[email] = c
        return self.clients[email]

    def load(self) -> None:
        a, b = self.client("admin-a@demo.test"), self.client("admin-b@demo.test")
        self.docs_a = [d for d in a.get("/api/documents").json()["documents"] if not d["deleted"]]
        self.docs_b = [d for d in b.get("/api/documents").json()["documents"] if not d["deleted"]]
        self.ids_a = {d["id"] for d in self.docs_a}
        self.ids_b = {d["id"] for d in self.docs_b}
        self.group_of = {d["id"]: GROUP_NAMES.get(d["group"]["name"], d["group"]["name"]) for d in self.docs_a}
        # titles per office: browser tests may upload copies of office A fixtures into office B
        self.by_title = {"A": {d["title"]: d["id"] for d in self.docs_a},
                         "B": {d["title"]: d["id"] for d in self.docs_b}}
        unverified_a, unmapped_a = unverified_from_queue(a, "A")
        unverified_b, unmapped_b = unverified_from_queue(b, "B")
        self.unverified = unverified_a | unverified_b
        self.unmapped = unmapped_a + unmapped_b
        self.office_ids = {k: self.client(e).get("/api/auth/me").json()["office"]["id"]
                           for k, e in (("A", "admin-a@demo.test"), ("B", "admin-b@demo.test"))}

    def doc_id(self, gt_doc: str) -> str | None:
        return self.by_title[docs()[gt_doc]["office"]].get(title_of(gt_doc))

    def forbidden(self, user: str, rule: str | None) -> set[str]:
        office, groups = USERS[user]
        other_office = self.ids_b if office == "A" else self.ids_a
        if rule == "other_office":
            return other_office
        if rule == "other_group":
            return other_office | {d for d in self.ids_a if groups and self.group_of.get(d) not in groups}
        return set()

    def filename(self, client: httpx.Client, document_id: str, version_id: str) -> str | None:
        if document_id not in self.versions:
            r = self.client("admin-a@demo.test").get(f"/api/documents/{document_id}")
            if r.status_code != 200:
                r = self.client("admin-b@demo.test").get(f"/api/documents/{document_id}")
            self.versions[document_id] = {v["id"]: v["filename"] for v in r.json().get("versions", [])}
        return self.versions[document_id].get(version_id)

    def file(self, client: httpx.Client, url: str) -> tuple[int, str, bytes]:
        base = url.partition("#")[0]
        if base not in self.files:
            r = client.get(base)
            self.files[base] = (r.status_code, r.headers.get("content-type", ""), r.content)
        return self.files[base]


def page_text(content_type: str, data: bytes, page: int | None) -> str:
    if content_type.startswith("application/pdf"):
        import pdfplumber

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            if page is None or page > len(pdf.pages):
                return ""
            return pdf.pages[page - 1].extract_text() or ""
    import docx  # DOCX: the whole document (no physical pages)

    d = docx.Document(io.BytesIO(data))
    parts = [p.text for p in d.paragraphs]
    for t in d.tables:
        for row in t.rows:
            parts.extend(c.text for c in row.cells)
    return "\n".join(parts)


def evidence_check(live: Live, client, answer: dict, data_kind: str) -> tuple[int, int, list[str]]:
    """Record sources (not retrieved passages) whose file page shows the record's price."""
    ok = n = 0
    problems = []
    for s in answer.get("sources", []):
        if s.get("chunk_id"):
            continue  # a retrieved passage of a combined answer, not a calculation source
        n += 1
        filename = live.filename(client, s["document_id"], s["version_id"])
        row = s.get("row")
        rec = gt_record_for(filename or "", 0 if row is not None else None, row, data_kind)
        status, ctype, data = live.file(client, s["url"])
        if rec is None or status != 200:
            problems.append(f"source {s.get('title')} p{s.get('page_list')} row {row}: "
                            f"{'unmapped' if rec is None else f'HTTP {status}'}")
            continue
        page = (s.get("page_list") or [None])[0]
        if docs()[rec["_doc"]]["kind"] == "pdf_scanned":
            ok += 1  # image-only page: the stored OCR text is checked by the acceptance suite
            continue
        token = price_token(rec["id"])
        text_ = re.sub(r"\s+", " ", page_text(ctype, data, page))
        if token in text_ and (page == rec["page"] or rec["page"] is None):
            ok += 1
        else:
            problems.append(f"{rec['id']}: price {token} not on cited page {page} (answer key page {rec['page']})")
    return ok, n, problems


def run_item(live: Live, item: dict) -> list[dict]:
    user = item["user"]
    client = live.client(user)
    office, groups = USERS[user]
    results = []
    conversation = None
    previous: Filters | None = None
    for i, turn in enumerate(item["turns"], start=1):
        expect = turn["expect"]
        filters = Filters.of(expect["filters"]) if expect.get("filters") else None
        if "clarify" in turn:
            answers = turn["clarify"] or {}
        elif filters:
            answers = {k: getattr(filters, k) for k in ("data_kind", "date_field", *MIXED_KEYS)
                       if getattr(filters, k) is not None}
        else:
            answers = {}
        started = time.perf_counter()
        flow = ask_flow(client, turn["ask"], answers, conversation)
        total_ms = (time.perf_counter() - started) * 1000
        conversation = flow.conversation_id
        a = flow.answer
        kind = a.get("kind", "error")
        reasons: list[str] = []
        res = {"id": item["id"], "turn": i, "category": item["category"], "user": user, "question": turn["ask"],
               "expected": expect["outcome"], "got": kind, "provider": a.get("provider"),
               "cached": bool(a.get("cached")), "latencies_ms": flow.latencies_ms, "total_ms": total_ms,
               "requests_cached": [bool(x.get("cached")) for x in flow.transcript],
               "clarifications": flow.clarifications, "sources": len(a.get("sources", []))}
        if flow.error:
            reasons.append(flow.error)
        out = expect["outcome"]
        numeric = a.get("numeric")
        if out in ("numeric", "combined"):
            exp = expected_stats(filters, office, groups, live.unverified)
            res["expected_numeric"] = exp.as_answer()
            if exp.count == 0:
                reasons.append("SPEC: no verified answer-key records match these filters")
            if kind != out:
                reasons.append(f"kind {kind} (expected {out})")
            elif numeric:
                got = {k: numeric.get(k) for k in NUMERIC_FIELDS}
                res["got_numeric"] = got
                diff = {k: (got[k], exp.as_answer()[k]) for k in NUMERIC_FIELDS if got[k] != exp.as_answer()[k]}
                if diff:
                    reasons.append("numbers differ (got, expected): " + json.dumps(diff, ensure_ascii=False))
                res["numeric_exact"] = not diff
            else:
                reasons.append("no numeric block")
            # a follow-up inherits the conditions confirmed in the conversation
            inherited = tuple(k for k in MIXED_KEYS if previous is not None and getattr(previous, k) is not None)
            want_clar = expected_clarifications(filters, tuple(expect.get("stated", [])) + inherited, office,
                                                groups, live.unverified)
            asked = [k for k in flow.clarifications if k in MIXED_KEYS]
            res["clarification_precise"] = asked == want_clar
            if asked != want_clar:
                res["clarification_note"] = f"asked {asked}, needed {want_clar}"
        if kind in ("numeric", "combined") and numeric:
            ok, n, problems = evidence_check(live, client, a, (filters.data_kind if filters else
                                                               "transaction_price"))
            res["evidence"] = (ok, n)
            res["evidence_problems"] = problems
        if out == "clarification":
            if kind != "clarification":
                reasons.append(f"kind {kind} (expected a clarification about {expect['key']})")
            elif a["clarification"]["key"] != expect["key"]:
                reasons.append(f"asked about {a['clarification']['key']} (expected {expect['key']})")
        if out == "abstain" and kind != "abstain":
            reasons.append(f"kind {kind} (expected abstain)")
        if out in ("clarification", "abstain") and (numeric or big_numbers(a.get("text", ""))):
            reasons.append("a number was shown")
            res["number_shown"] = True
        if out == "content" and kind != "content":
            reasons.append(f"kind {kind} (expected content)")
        if expect.get("sources"):
            want = [(live.doc_id(s["doc"]), s["page"]) for s in expect["sources"]]
            hit = any(s["document_id"] == d and (p is None or p in s.get("page_list", []))
                      for s in a.get("sources", []) for d, p in want)
            res["source_recall"] = hit
            if not hit:
                reasons.append("expected source not cited: " + ", ".join(
                    f"{s['doc']} p.{s['page']}" for s in expect["sources"]) + " | got: " + ", ".join(
                    f"{s['title'][:3].strip()} p{s.get('page_list')}" for s in a.get("sources", [])))
        if expect.get("forbid"):
            bad = {s["document_id"] for s in a.get("sources", [])} & live.forbidden(user, expect["forbid"])
            res["isolation_ok"] = not bad
            if bad:
                reasons.append(f"LEAK: {len(bad)} source(s) outside the user's scope")
        res["ok"] = not reasons
        res["reasons"] = reasons
        res["text"] = (a.get("text") or a.get("detail") or "")[:240]
        results.append(res)
        if filters is not None:
            previous = filters
    return results


def extraction_metrics(live: Live) -> dict:
    url = os.environ.get("EVAL_DATABASE_URL")
    if not url:
        env = {}
        env_file = REPO / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
        if not env.get("RAG_APP_PASSWORD"):
            return {"error": "no EVAL_DATABASE_URL and no RAG_APP_PASSWORD in .env"}
        url = f"postgresql+psycopg://rag_app:{env['RAG_APP_PASSWORD']}@localhost:5433/rag"
    from sqlalchemy import create_engine, text

    tables, occurrences, models = [], [], Counter()
    try:
        engine = create_engine(url)
        for office, office_id in live.office_ids.items():
            mine = {d["filename"] for d in by_filename().values() if d["office"] == office}
            with engine.begin() as conn:
                conn.execute(text("SELECT set_config('app.office_id', :o, true), set_config('app.user_id', '', true),"
                                  " set_config('app.role', 'system', true)"), {"o": office_id})
                tables += [dict(r._mapping) for r in conn.execute(text(EXTRACTION_SQL_TABLES)) if r.filename in mine]
                occurrences += [dict(r._mapping) for r in conn.execute(text(EXTRACTION_SQL_OCCURRENCES))
                                if r.filename in mine]
                for m, n in conn.execute(text("SELECT embedding_model, count(*) FROM chunks GROUP BY 1")):
                    models[m] += n
        engine.dispose()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"database not reachable: {type(exc).__name__}"}
    return {"tables": table_accuracy(tables), "fields": field_accuracy(occurrences), "embedding_models": dict(models)}


def environment(live: Live) -> dict:
    def sh(cmd: list[str]) -> str:
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""

    env = {"date": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"), "backend": BACKEND,
           "python": platform.python_version(), "os": f"{platform.system()} {platform.release()} {platform.machine()}"}
    if sys.platform == "darwin":
        env["cpu"] = sh(["sysctl", "-n", "machdep.cpu.brand_string"])
        env["cores"] = sh(["sysctl", "-n", "hw.ncpu"])
        mem = sh(["sysctl", "-n", "hw.memsize"])
        env["memory"] = f"{int(mem) / 2**30:.0f} GiB" if mem.isdigit() else mem
    docker = sh(["docker", "info", "--format", "{{.ServerVersion}}|{{.NCPU}}|{{.MemTotal}}|{{.OperatingSystem}}"])
    if docker:
        v, ncpu, memt, os_ = (docker.split("|") + ["", "", "", ""])[:4]
        env["docker"] = f"Docker {v} on {os_}, {ncpu} CPUs, {int(memt) / 2**30:.1f} GiB" if memt.isdigit() else docker
    env["commit"] = sh(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"])
    env["health"] = live.client("admin-a@demo.test").get("/api/health").json()
    for key, email in (("A", "admin-a@demo.test"), ("B", "admin-b@demo.test")):
        s = live.client(email).get("/api/admin/settings").json()
        env[f"provider_{key}"] = s.get("effective_provider")
    env["demo_mode"] = live.client("admin-a@demo.test").get("/api/auth/me").json().get("demo_mode")
    return env


def summarize(results: list[dict]) -> dict:
    m: dict = {}
    m["turns"] = len(results)
    m["items"] = len({r["id"] for r in results})
    m["correct"] = sum(r["ok"] for r in results)
    num = [r for r in results if r["expected"] in ("numeric", "combined")]
    m["numeric"] = (sum(bool(r.get("numeric_exact")) for r in num), len(num))
    neg = [r for r in results if r["expected"] in ("abstain", "clarification")]
    m["justified"] = (sum(r["got"] in ("abstain", "clarification") and not r.get("number_shown") for r in neg), len(neg))
    m["exact_negative"] = (sum(r["ok"] for r in neg), len(neg))
    answerable = [r for r in results if r["expected"] in ANSWERED]
    m["answered"] = (sum(r["got"] in ANSWERED for r in answerable), len(answerable))
    m["unjustified_abstain"] = (sum(r["got"] in ("abstain", "clarification") for r in answerable), len(answerable))
    rec = [r for r in results if "source_recall" in r]
    m["source_recall"] = (sum(r["source_recall"] for r in rec), len(rec))
    ev = [r for r in results if "evidence" in r]
    m["evidence_answers"] = (sum(1 for r in ev if r["evidence"][0] == r["evidence"][1] and r["evidence"][1] > 0), len(ev))
    m["evidence_sources"] = (sum(r["evidence"][0] for r in ev), sum(r["evidence"][1] for r in ev))
    iso = [r for r in results if "isolation_ok" in r]
    m["isolation"] = (sum(r["isolation_ok"] for r in iso), len(iso))
    cp = [r for r in results if "clarification_precise" in r]
    m["clarification_precision"] = (sum(r["clarification_precise"] for r in cp), len(cp))
    lat = [ms for r in results for ms in r["latencies_ms"]]
    m["latency_requests"] = lat
    m["latency_turns"] = [r["total_ms"] for r in results]
    pairs = [(ms, c) for r in results for ms, c in zip(r["latencies_ms"], r["requests_cached"], strict=False)]
    m["latency_fresh"] = [ms for ms, c in pairs if not c]
    m["latency_cached"] = [ms for ms, c in pairs if c]
    m["providers"] = Counter(r["provider"] for r in results)
    m["cached"] = sum(r["cached"] for r in results)
    by_cat = defaultdict(lambda: [0, 0])
    for r in results:
        by_cat[r["category"]][0] += r["ok"]
        by_cat[r["category"]][1] += 1
    m["by_category"] = dict(sorted(by_cat.items()))
    return m


def fmt_ms(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.0f} ms"


def write_report(path: Path, env: dict, m: dict, results: list[dict], extraction: dict, live: Live,
                 elapsed: float) -> None:
    lat, turns = m["latency_requests"], m["latency_turns"]
    lines = [
        "# Evaluation results (U15)",
        "",
        f"Generated by `cd backend && uv run python scripts/eval.py` on {env['date']} against the live demo "
        f"stack ({env['backend']}), commit `{env.get('commit') or 'n/a'}`. Run time {elapsed:.0f} s.",
        "",
        "> **Not a cloud-model benchmark.** The demo stack answers content questions with the deterministic "
        f"mock provider (effective provider: office A `{env.get('provider_A')}`, office B `{env.get('provider_B')}`; "
        f"providers seen in answers: {dict(m['providers'])}). Numeric answers are SQL + templates with no model "
        "call. Latencies below are local mock latencies, not cloud-provider latencies. All data is synthetic "
        "(R36); quality on real appraisal reports is **pending** until authorized real documents are supplied.",
        "",
        "## Environment",
        "",
        "| item | value |",
        "|---|---|",
        f"| machine | {env.get('cpu', 'n/a')}, {env.get('cores', '?')} cores, {env.get('memory', '?')} RAM ({env['os']}) |",
        f"| Docker | {env.get('docker', 'n/a')} |",
        f"| embedding model in the stack (chunks.embedding_model) | {', '.join(f'`{k}` ({v} chunks)' for k, v in (extraction.get('embedding_models') or {}).items()) or 'n/a'} |",
        f"| answer provider | `{env.get('provider_A')}` (demo mode: {env.get('demo_mode')}) |",
        f"| database role / RLS bypass | `{env['health'].get('db_role')}` / {env['health'].get('db_bypass_rls')} |",
        f"| evaluation set | {m['items']} items, {m['turns']} scored turns (`backend/eval/questions.yaml`) |",
        f"| verification state | {len(live.unverified)} answer-key records still in the review queue and excluded "
        f"from expectations: {', '.join(sorted(live.unverified)) or 'none'} |",
        "",
        "## Answer quality",
        "",
        "| metric | result | definition |",
        "|---|---|---|",
        f"| answer correctness (all turns) | {rate(m['correct'], m['turns'])} | outcome, numbers, sources and isolation all as expected |",
        f"| numeric exact match | {rate(*m['numeric'])} | count, mean, weighted, median, min, max equal the ground-truth oracle (Decimal, ROUND_HALF_UP) |",
        f"| justified abstention / clarification | {rate(*m['justified'])} | questions without basis that got an abstention or clarification and no number |",
        f"| ... with the exact expected behaviour | {rate(*m['exact_negative'])} | right kind (and the right clarification key) |",
        f"| answered rate (coverage) | {rate(*m['answered'])} | answerable questions that got a numeric/content answer (a system refusing everything scores 0) |",
        f"| unjustified abstention / clarification | {rate(*m['unjustified_abstain'])} | answerable questions left at an abstention or an open clarification |",
        f"| source recall (content) | {rate(*m['source_recall'])} | expected document + physical page among the cited sources |",
        f"| evidence coverage (numeric answers) | {rate(*m['evidence_answers'])} | answers whose every record source opens and shows the record's price on the cited page |",
        f"| evidence coverage (sources) | {rate(*m['evidence_sources'])} | individual record sources that pass that check |",
        f"| isolation | {rate(*m['isolation'])} | turns with a scope rule and no source from another office / group |",
        f"| clarification precision | {rate(*m['clarification_precision'])} | numeric turns where the system asked exactly the mixed-attribute clarifications the verified data requires |",
        f"| latency per request p50 / p95 / max | {fmt_ms(pct(lat, .5))} / {fmt_ms(pct(lat, .95))} / {fmt_ms(max(lat) if lat else None)} | {len(lat)} `/api/ask` calls, incl. clarification answers |",
        f"| ... computed (not from cache) p50 / p95 | {fmt_ms(pct(m['latency_fresh'], .5))} / {fmt_ms(pct(m['latency_fresh'], .95))} | {len(m['latency_fresh'])} requests, incl. every clarification step (never cached) |",
        f"| ... served from cache p50 / p95 | {fmt_ms(pct(m['latency_cached'], .5))} / {fmt_ms(pct(m['latency_cached'], .95))} | {len(m['latency_cached'])} requests |",
        f"| latency per turn p50 / p95 | {fmt_ms(pct(turns, .5))} / {fmt_ms(pct(turns, .95))} | question plus its clarification round-trips |",
        f"| answers served from cache | {m['cached']}/{m['turns']} | exact-condition cache hits (R28); repeated runs hit more |",
        "",
        "### By category",
        "",
        "| category | correct |",
        "|---|---|",
    ]
    lines += [f"| {cat} | {rate(ok, n)} |" for cat, (ok, n) in m["by_category"].items()]
    lines += ["", "## Table and field extraction (separate from answer quality)", ""]
    if "error" in extraction:
        lines.append(f"Not measured: {extraction['error']}.")
    else:
        t, f = extraction["tables"], extraction["fields"]
        lines += [
            "Compared with `ground_truth.yaml` for every current document version in both offices (read-only, "
            "runtime role, system context).",
            "",
            "| metric | result |",
            "|---|---|",
            f"| table header accuracy | {rate(*t['headers'])} |",
            f"| table cell accuracy | {rate(*t['cells'])} |",
            f"| row physical-page accuracy | {rate(*t['row_pages'])} |",
            f"| critical-field accuracy (data kind, city, price, area, area type, transaction/valuation date) | {rate(*f['critical'])} |",
            f"| all-field accuracy (17 fields per record) | {rate(*f['all'])} |",
            f"| records extracted / expected | {f['records_found']}/{f['records_expected']} (extra occurrences: {f['extra']}) |",
            "",
            "| document | headers | cells | row pages |",
            "|---|---|---|---|",
        ]
        lines += [f"| {d} | {v['headers']} | {v['cells']} | {v['row_pages']} |" for d, v in t["per_doc"].items()]
        if f["missing"] or f["critical_errors"]:
            lines += ["", "Field errors:", ""]
            lines += [f"- missing record: {x}" for x in f["missing"]]
            lines += [f"- {x}" for x in f["critical_errors"][:40]]
    failures = [r for r in results if not r["ok"]]
    lines += ["", f"## Failures ({len(failures)})", ""]
    if not failures:
        lines.append("None.")
    for r in failures:
        lines.append(f"- **{r['id']}** turn {r['turn']} ({r['category']}, {r['user']}): «{r['question']}» — "
                     f"expected `{r['expected']}`, got `{r['got']}`. " + "; ".join(r["reasons"]))
    notes = [r for r in results if r.get("clarification_note") or r.get("evidence_problems")]
    if notes:
        lines += ["", "## Notes (not counted as failures)", ""]
        for r in notes:
            if r.get("clarification_note"):
                lines.append(f"- {r['id']} turn {r['turn']}: clarifications {r['clarification_note']}")
            for p in r.get("evidence_problems", []):
                lines.append(f"- {r['id']} turn {r['turn']}: evidence — {p}")
    lines += ["", "## All turns", "", "| id | turn | category | expected | got | provider | ok | ms |", "|---|---|---|---|---|---|---|---|"]
    lines += [f"| {r['id']} | {r['turn']} | {r['category']} | {r['expected']} | {r['got']} | {r['provider']} | "
              f"{'yes' if r['ok'] else '**no**'} | {r['total_ms']:.0f} |" for r in results]
    lines += [
        "",
        "## How to read this",
        "",
        "- Expected numbers are recomputed from the answer key for each item's conditions; records still in the "
        "review queue are excluded, so a correct system must not count them.",
        "- The cache is keyed on conditions and data version; questions asked earlier (by this script, the browser "
        "tests or a person) can be served from it, which shortens latencies and can mask a cache defect.",
        "- Real-document quality evaluation is pending: run the same script against a stack seeded with authorized "
        "real reports and a manually verified answer key in the same YAML format.",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--questions", default=str(ROOT / "eval" / "questions.yaml"))
    ap.add_argument("--out", default=str(REPO / "docs" / "evaluation" / "eval-results.md"))
    ap.add_argument("--only", default="")
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    items = yaml.safe_load(Path(args.questions).read_text(encoding="utf-8"))["items"]
    if args.only:
        wanted = set(args.only.split(","))
        items = [i for i in items if i["id"] in wanted]
    started = time.perf_counter()
    live = Live()
    live.load()
    env = environment(live)
    results = []
    for item in items:
        for r in run_item(live, item):
            results.append(r)
            mark = "ok " if r["ok"] else "FAIL"
            print(f"{mark} {r['id']}.{r['turn']:<2} {r['expected']:>13} -> {r['got']:<13} {r['total_ms']:6.0f} ms"
                  + ("" if r["ok"] else "  " + "; ".join(r["reasons"])[:200]), flush=True)
    m = summarize(results)
    extraction = extraction_metrics(live)
    elapsed = time.perf_counter() - started
    write_report(Path(args.out), env, m, results, extraction, live, elapsed)
    if args.json:
        Path(args.json).write_text(json.dumps({"env": env, "results": results}, ensure_ascii=False, indent=1,
                                              default=str), encoding="utf-8")
    lat = m["latency_requests"]
    print(f"\ncorrect {rate(m['correct'], m['turns'])} | numeric exact {rate(*m['numeric'])} | justified "
          f"abstention {rate(*m['justified'])} | answered {rate(*m['answered'])} | source recall "
          f"{rate(*m['source_recall'])} | evidence {rate(*m['evidence_answers'])} | isolation {rate(*m['isolation'])}"
          f" | p50 {fmt_ms(pct(lat, .5))} p95 {fmt_ms(pct(lat, .95))}")
    if "error" not in extraction:
        print(f"tables: cells {rate(*extraction['tables']['cells'])}, headers {rate(*extraction['tables']['headers'])}"
              f" | critical fields {rate(*extraction['fields']['critical'])}")
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
