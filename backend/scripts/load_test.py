"""10-user load test against the LIVE demo stack (U15) and write docs/evaluation/load-test.md.

    cd backend && uv run python scripts/load_test.py
    # options: --users 10 --requests 200 --seed 7 --out ../docs/evaluation/load-test.md

Ten concurrent simulated users (threads, one session each, demo users from scripts/seed_demo.py)
mix numeric questions (cached and uncached: years, ranges and filters vary), clarification flows,
follow-ups, content questions, hybrid search and document listing until N requests were sent.
Every HTTP request is timed; the report gives p50/p95/max per route and kind, and the errors.

The stack answers with the demo mock provider. This does NOT measure cloud-model latency; it measures
the application (FastAPI, PostgreSQL, retrieval, local embeddings for the query) on this machine.
Read-only for office data: it only asks questions, searches and lists documents.
"""

from __future__ import annotations

import argparse
import os
import platform
import random
import subprocess
import sys
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import httpx

BACKEND = os.environ.get("BACKEND_URL", "http://localhost:8000")
PASSWORD = os.environ.get("DEMO_PASSWORD", "demo1234")
REPO = Path(__file__).resolve().parents[2]
USERS = ["admin-a@demo.test", "dana@demo.test", "yossi@demo.test", "admin-a@demo.test", "dana@demo.test",
         "admin-b@demo.test", "admin-a@demo.test", "yossi@demo.test", "dana@demo.test", "admin-a@demo.test"]
PLACES = ["בחרוזים", "ברמת גן", "בנחלת גנים", "בגבעתיים", "בשכונת הבורסה"]
AREAS = ["", " בשטח נטו", " בשטח ברוטו", " שטח רשום"]
CONTENT = ["מרפסת שמש הפונה מערבה", "היעדר מעלית", "רעש מכביש ז׳בוטינסקי", "חניה בטאבו", "עסקה בין קרובי משפחה",
           "זכויות בנייה לא מנוצלות", "נבדק מבחוץ בלבד", "היצע מוגבל של דירות גדולות", "קרבה לרכבת הקלה",
           "מבנה לשימור", "תוספת של עד 5% לשווי", "היטל השבחה", "מקדם קומה"]
PREFIXES = ["מה נכתב על", "מה נאמר במסמכים על", "איך השמאי התייחס ל", "באיזו שומה מוזכר", "מה השיקולים לגבי"]
CLARIFY_ONLY = ["מה המחיר או השווי למ״ר בחרוזים ב-2024?", "מחירי עסקאות למ״ר בחרוזים בשנת 2024",
                "מה השווי למ״ר ברמת גן ב-2024?"]


class Budget:
    def __init__(self, n: int):
        self.left, self.lock = n, threading.Lock()

    def take(self) -> bool:
        with self.lock:
            if self.left <= 0:
                return False
            self.left -= 1
            return True


class Recorder:
    def __init__(self):
        self.samples: list[tuple[str, float, int]] = []
        self.errors: list[str] = []
        self.lock = threading.Lock()

    def add(self, route: str, ms: float, status: int, error: str | None = None) -> None:
        with self.lock:
            self.samples.append((route, ms, status))
            if error:
                self.errors.append(f"{route}: {error}")


class SimUser(threading.Thread):
    def __init__(self, idx: int, email: str, budget: Budget, rec: Recorder, seed: int):
        super().__init__(daemon=True)
        self.idx, self.email, self.budget, self.rec = idx, email, budget, rec
        self.rng = random.Random(seed * 100 + idx)
        self.client = httpx.Client(base_url=BACKEND, timeout=120)
        self.conversation: str | None = None

    def call(self, method: str, path: str, label: str, **kw) -> dict | None:
        if not self.budget.take():
            raise StopIteration
        start = time.perf_counter()
        try:
            r = self.client.request(method, path, **kw)
            ms = (time.perf_counter() - start) * 1000
        except Exception as exc:  # noqa: BLE001
            self.rec.add(label, (time.perf_counter() - start) * 1000, 0, type(exc).__name__)
            return None
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else None
        if label == "ask" and body and "answer" in body:
            a = body["answer"]
            label = f"ask:{a['kind']}" + (" (cached)" if a.get("cached") else "")
        self.rec.add(label, ms, r.status_code, None if r.status_code < 400 else f"HTTP {r.status_code} {r.text[:80]}")
        return body

    def ask(self, question=None, clarification=None, new=False) -> dict | None:
        body = {"conversation_id": None if new else self.conversation}
        if question:
            body["question"] = question
        if clarification:
            body["clarification"] = clarification
        data = self.call("POST", "/api/ask", "ask", json=body)
        if data and "conversation_id" in data:
            self.conversation = data["conversation_id"]
        return data

    def numeric_flow(self) -> None:
        rng = self.rng
        if rng.random() < 0.5:
            year = f"ב-{rng.choice([2023, 2024])}"  # popular conditions: often cached
        else:
            a = rng.randint(2015, 2024)
            year = f"בין {a} ל-{rng.randint(a + 1, 2026)}"  # rarer ranges: mostly computed fresh
        q = f"מה מחיר העסקאות למ״ר {rng.choice(PLACES)} {year} לפי תאריך עסקה{rng.choice(AREAS)}?"
        data = self.ask(q, new=True)
        for _ in range(4):  # answer the mixed-attribute clarifications with a random offered option
            a = (data or {}).get("answer", {})
            if a.get("kind") != "clarification":
                break
            option = rng.choice(a["clarification"]["options"])["value"]
            data = self.ask(clarification={"key": a["clarification"]["key"], "value": option})
        if (data or {}).get("answer", {}).get("kind") == "numeric" and rng.random() < 0.6:
            self.ask(f"ומה לגבי {rng.choice([2022, 2023, 2024])}?")

    def step(self) -> None:
        r = self.rng.random()
        if r < 0.40:
            self.numeric_flow()
        elif r < 0.55:
            self.ask(self.rng.choice(CLARIFY_ONLY), new=True)
        elif r < 0.80:
            self.ask(f"{self.rng.choice(PREFIXES)} {self.rng.choice(CONTENT)}?", new=True)
        elif r < 0.92:
            self.call("GET", "/api/search", "search", params={"q": self.rng.choice(CONTENT)})
        else:
            self.call("GET", "/api/documents", "documents")

    def run(self) -> None:
        r = self.client.post("/api/auth/login", json={"email": self.email, "password": PASSWORD})
        if r.status_code != 200:
            self.rec.errors.append(f"login {self.email}: HTTP {r.status_code}")
            return
        try:
            while True:
                self.step()
        except StopIteration:
            pass
        finally:
            self.client.close()


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    k = (len(s) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def machine() -> dict:
    env = {"os": f"{platform.system()} {platform.release()} {platform.machine()}"}
    if sys.platform == "darwin":
        env["cpu"] = sh(["sysctl", "-n", "machdep.cpu.brand_string"])
        env["cores"] = sh(["sysctl", "-n", "hw.ncpu"])
        mem = sh(["sysctl", "-n", "hw.memsize"])
        env["memory"] = f"{int(mem) / 2**30:.0f} GiB" if mem.isdigit() else mem
    docker = sh(["docker", "info", "--format", "{{.ServerVersion}}|{{.NCPU}}|{{.MemTotal}}|{{.OperatingSystem}}"])
    if docker:
        v, ncpu, memt, os_ = (docker.split("|") + ["", "", "", ""])[:4]
        env["docker"] = (f"Docker {v} on {os_}: {ncpu} CPUs, {int(memt) / 2**30:.1f} GiB available to containers"
                         if memt.isdigit() else docker)
    return env


def docker_stats() -> str:
    return sh(["docker", "stats", "--no-stream", "--format", "{{.Name}}: CPU {{.CPUPerc}}, memory {{.MemUsage}}"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--users", type=int, default=10)
    ap.add_argument("--requests", type=int, default=200)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=str(REPO / "docs" / "evaluation" / "load-test.md"))
    args = ap.parse_args(argv)

    health = httpx.get(f"{BACKEND}/api/health", timeout=10).json()
    budget, rec = Budget(args.requests), Recorder()
    users = [SimUser(i, USERS[i % len(USERS)], budget, rec, args.seed) for i in range(args.users)]
    stats_before = docker_stats()
    started = time.perf_counter()
    for u in users:
        u.start()
    peak = ""
    while any(u.is_alive() for u in users):
        time.sleep(1.0)
        if not peak and time.perf_counter() - started > 3:
            peak = docker_stats()
    wall = time.perf_counter() - started

    by_route: dict[str, list[float]] = defaultdict(list)
    for route, ms, _status in rec.samples:
        by_route[route].append(ms)
    every = [ms for _, ms, _ in rec.samples]
    ask_all = [ms for r, ms, _ in rec.samples if r.startswith("ask")]
    failed = [s for s in rec.samples if s[2] == 0 or s[2] >= 400]
    env = machine()

    def row(name, values):
        return (f"| {name} | {len(values)} | {pct(values, .5):.0f} | {pct(values, .95):.0f} | {max(values):.0f} |"
                if values else f"| {name} | 0 | | | |")

    lines = [
        "# Load test (U15)",
        "",
        f"Generated by `cd backend && uv run python scripts/load_test.py` on "
        f"{datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} against {BACKEND}.",
        "",
        "> **Demo mock provider.** Content answers come from the deterministic demo mock (no cloud model is "
        "configured), and numeric answers make no model call at all. These latencies therefore measure the "
        "application, PostgreSQL, retrieval and the local query embedding on this laptop, **not** the latency of "
        "a cloud LLM provider, which would add its own seconds per content answer.",
        "",
        "## Scenario",
        "",
        f"- {args.users} concurrent simulated users (threads), each with its own session, as "
        + ", ".join(sorted({u.email for u in users})) + "; seed " + str(args.seed) + ".",
        f"- {args.requests} HTTP requests in total (logins not counted). Mix per step: 40% numeric flows "
        "(question, random answers to the clarifications, and a follow-up «ומה לגבי …?» 60% of the time; popular "
        "years are often served from the answer cache, random year ranges are mostly computed fresh), 15% "
        "clarification-only questions, 25% content questions (varied wording), 12% hybrid search, 8% document "
        "listing.",
        "- Read-only for office data; the run adds conversations and cache entries only.",
        "",
        "## Results",
        "",
        f"Wall time {wall:.1f} s for {len(rec.samples)} requests ({len(rec.samples) / wall:.1f} req/s). "
        f"Errors: {len(failed)} failed requests" + (f"; {len(rec.errors)} error messages." if rec.errors else "."),
        "",
        "| route / answer kind | requests | p50 ms | p95 ms | max ms |",
        "|---|---|---|---|---|",
        row("**all requests**", every),
        row("**all /api/ask**", ask_all),
    ]
    lines += [row(f"`{name}`", values) for name, values in sorted(by_route.items())]
    if rec.errors:
        lines += ["", "Errors:", ""] + [f"- {e}" for e in rec.errors[:30]]
    lines += [
        "",
        "## Environment",
        "",
        "| item | value |",
        "|---|---|",
        f"| machine | {env.get('cpu', 'n/a')}, {env.get('cores', '?')} cores, {env.get('memory', '?')} RAM ({env['os']}) |",
        f"| Docker | {env.get('docker', 'n/a')} |",
        "| stack | Compose: `backend` (uvicorn, one process), `worker`, `db` (pgvector/pgvector:0.8.7-pg17); "
        "local embeddings `intfloat/multilingual-e5-small` on CPU |",
        f"| health | db role `{health.get('db_role')}`, RLS bypass {health.get('db_bypass_rls')} |",
        "",
        "Container usage before the run:",
        "",
        "```",
        stats_before or "n/a",
        "```",
        "",
        "Container usage a few seconds into the run:",
        "",
        "```",
        peak or "n/a",
        "```",
        "",
        "## Notes",
        "",
        "- `(cached)` rows are exact-condition cache hits (R28): the key includes office, permission scope, "
        "conditions and data version, so the first run after a data change is slower than repeated runs.",
        "- Content answers embed the question with the local e5 model on the backend's CPU; that dominates their "
        "latency here. With a cloud provider enabled, add the provider's response time.",
        "- Browser tests or people using the stack at the same time share the machine and can skew the numbers.",
        "",
    ]
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[lines.index("## Results"):lines.index("## Environment")]))
    print(f"report: {args.out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
