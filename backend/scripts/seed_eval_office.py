"""Creates the isolated evaluation office C and uploads the synthetic documents of the evaluation sets to it
(tests/fixtures/eval_v5/ and eval_v6/, written by scripts/make_eval_docs.py). Cloud use is enabled for office C only, so the real model can answer there.

Runs against a live stack, like seed_demo.py: the office is bootstrapped with the owner role, everything else
goes through the public API. Idempotent: an existing office C is reused and documents already there are not
uploaded again. It never touches another office: it signs in only as office C's admin, and it refuses to
continue if that office holds a document this script did not upload.

    BACKEND_URL=http://localhost:8000 OWNER_DATABASE_URL=... uv run python scripts/seed_eval_office.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.security import hash_password  # noqa: E402

BACKEND = os.environ.get("BACKEND_URL", "http://localhost:8000")
PASSWORD = os.environ.get("DEMO_PASSWORD", "demo1234")
NAME = "משרד הערכה ג׳ (סינתטי, סט v5)"
ADMIN = "admin-c@eval.test"
FOLDERS = [ROOT / "tests" / "fixtures" / "eval_v5", ROOT / "tests" / "fixtures" / "eval_v6"]


def log(msg: str) -> None:
    print(msg, flush=True)


def login() -> httpx.Client | None:
    c = httpx.Client(base_url=BACKEND, timeout=120)
    r = c.post("/api/auth/login", json={"email": ADMIN, "password": PASSWORD})
    return c if r.status_code == 200 else None


def main() -> int:
    admin = login()
    if admin is None:
        owner = os.environ.get("OWNER_DATABASE_URL")
        if not owner:
            raise SystemExit("OWNER_DATABASE_URL is required to create office C")
        with create_engine(owner).begin() as conn:
            conn.execute(text("SELECT bootstrap_office(:n, :e, :fn, :h)"),
                         {"n": NAME, "e": ADMIN, "fn": "מנהל/ת משרד ההערכה", "h": hash_password(PASSWORD)})
        log("office C created")
        admin = login()
    assert admin is not None
    files = sorted(p for folder in FOLDERS for p in folder.glob("*.docx"))
    expected = {p.name for p in files}
    present = {d["current_version"]["filename"]: d for d in admin.get("/api/documents").json()["documents"]
               if d.get("current_version")}
    foreign = set(present) - expected
    if foreign:
        raise SystemExit(f"office C holds documents this script did not upload: {sorted(foreign)}")
    group = admin.get("/api/admin/groups").json()["groups"][0]["id"]
    for path in files:
        if path.name in present:
            continue
        r = admin.post("/api/documents", data={"group_id": group},
                       files=[("files", (path.name, path.read_bytes(), "application/octet-stream"))])
        r.raise_for_status()
        log(f"upload {path.name}: {r.json()['results'][0]['status']}")
    admin.put("/api/admin/settings", json={"cloud_llm_enabled": True, "acknowledge": True}).raise_for_status()
    deadline = time.time() + 900
    while time.time() < deadline:
        busy = [d for d in admin.get("/api/documents").json()["documents"]
                if d.get("current_version") and d["current_version"]["status"] in ("pending", "processing")]
        if not busy:
            break
        log(f"waiting for the worker: {len(busy)} documents")
        time.sleep(3)
    for d in admin.get("/api/documents").json()["documents"]:
        v = d.get("current_version") or {}
        log(f"{d['title']}: {v.get('status')} {v.get('reading') or ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
