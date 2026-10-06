"""Seed the demo stack with two synthetic offices (U14).

Runs against a live stack: offices are bootstrapped with the owner role, everything else goes
through the public API exactly like a user would (upload -> worker -> review approve).

    docker compose exec backend python scripts/seed_demo.py
    # or from the host: BACKEND_URL=http://localhost:8000 OWNER_DATABASE_URL=... uv run python scripts/seed_demo.py

Records are approved only when every key field equals the synthetic ground truth, so the demo
also shows which extractions a human would still need to check. Each office also gets the
structured attribute registry entries (KTD7), written with the owner role inside that office's
context. All data is synthetic.
"""

from __future__ import annotations

import os
import sys
import time
from decimal import Decimal
from pathlib import Path

import httpx
import yaml
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.answering.attributes import STRUCTURED_ATTRIBUTES  # noqa: E402  (single source of truth, KTD7)
from app.security import hash_password  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
BACKEND = os.environ.get("BACKEND_URL", "http://localhost:8000")
PASSWORD = os.environ.get("DEMO_PASSWORD", "demo1234")
OFFICES = {
    "A": {"name": "שמאות דמו א׳ (סינתטי)", "admin": "admin-a@demo.test"},
    "B": {"name": "שמאות דמו ב׳ (סינתטי)", "admin": "admin-b@demo.test"},
}
GROUPS = {"G1": "שומות רמת גן וגבעתיים", "G2": "שומות פרויקטים"}
EMPLOYEES = [
    {"email": "dana@demo.test", "full_name": "דנה (עובדת, קבוצה 1)", "groups": ["G1"], "can_upload": True},
    {"email": "yossi@demo.test", "full_name": "יוסי (עובד, קבוצה 2)", "groups": ["G2"], "can_upload": False},
]
CHECK_FIELDS = ("price", "area", "area_type", "transaction_date", "valuation_date")


def log(msg: str) -> None:
    print(msg, flush=True)


def client_for(email: str) -> httpx.Client:
    c = httpx.Client(base_url=BACKEND, timeout=120)
    r = c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    r.raise_for_status()
    return c


def owner_engine():
    owner_url = os.environ.get("OWNER_DATABASE_URL")
    return create_engine(owner_url) if owner_url else None


def bootstrap_offices() -> None:
    engine = owner_engine()
    for key, office in OFFICES.items():
        probe = httpx.post(f"{BACKEND}/api/auth/login", json={"email": office["admin"], "password": PASSWORD})
        if probe.status_code == 200:
            log(f"office {key} exists")
            continue
        if engine is None:
            raise SystemExit("OWNER_DATABASE_URL is required to create offices")
        with engine.begin() as conn:
            conn.execute(text("SELECT bootstrap_office(:n, :e, :fn, :h)"),
                         {"n": office["name"], "e": office["admin"], "fn": f"מנהל/ת {office['name']}",
                          "h": hash_password(PASSWORD)})
        log(f"office {key} created")


def seed_structured_attributes(clients: dict[str, httpx.Client]) -> None:
    """Idempotent: existing definitions (same office and key) are left untouched."""
    engine = owner_engine()
    if engine is None:
        raise SystemExit("OWNER_DATABASE_URL is required to seed the attribute registry")
    for key, c in clients.items():
        office_id = c.get("/api/auth/me").json()["office"]["id"]
        with engine.begin() as conn:  # the owner is bound by FORCE RLS too: act inside the office context
            conn.execute(text("SELECT set_config('app.office_id', :o, true)"), {"o": office_id})
            added = sum(conn.execute(
                text("INSERT INTO attribute_definitions (office_id, key, label_he, aliases, value_type, unit_dimension,"
                     " canonical_unit, source, structured_column, status) VALUES (app_office(), :k, :l, :a, 'numeric',"
                     " :d, :u, 'structured', :c, 'active') ON CONFLICT (office_id, key) DO NOTHING"),
                {"k": a["key"], "l": a["label_he"], "a": a["aliases"], "d": a["unit_dimension"],
                 "u": a["canonical_unit"], "c": a["column"]},
            ).rowcount for a in STRUCTURED_ATTRIBUTES)
        log(f"office {key}: {added} structured attributes added")


def ensure_groups_and_users(admin: httpx.Client) -> dict[str, str]:
    groups = {g["name"]: g["id"] for g in admin.get("/api/admin/groups").json()["groups"]}
    ids = {}
    for code, name in GROUPS.items():
        if name not in groups:
            groups[name] = admin.post("/api/admin/groups", json={"name": name}).json()["id"]
        ids[code] = groups[name]
    existing = {u["email"] for u in admin.get("/api/admin/users").json()["users"]}
    for emp in EMPLOYEES:
        if emp["email"] not in existing:
            admin.post("/api/admin/users", json={
                "email": emp["email"], "full_name": emp["full_name"], "password": PASSWORD, "role": "employee",
                "can_upload": emp["can_upload"], "group_ids": [ids[g] for g in emp["groups"]],
            }).raise_for_status()
    return ids


def upload_all(clients: dict[str, httpx.Client], group_ids: dict[str, dict[str, str]], docs: list[dict]) -> None:
    by_id: dict[str, str] = {}
    for doc in [d for d in docs if not d.get("version_of")]:
        office = doc["office"]
        group = group_ids[office].get(doc["group"]) or group_ids[office]["default"]
        path = FIXTURES / doc["filename"]
        r = clients[office].post("/api/documents", data={"group_id": group},
                                 files=[("files", (path.name, path.read_bytes(), "application/octet-stream"))])
        r.raise_for_status()
        res = r.json()["results"][0]
        log(f"upload {doc['id']}: {res['status']} {res.get('reason', '')}")
        if res.get("document_id"):
            by_id[doc["id"]] = res["document_id"]
    wait_for_processing(clients)
    for doc in [d for d in docs if d.get("version_of")]:
        target = by_id.get(doc["version_of"])
        if not target:
            continue
        path = FIXTURES / doc["filename"]
        r = clients[doc["office"]].post("/api/documents", data={"document_id": target},
                                        files=[("files", (path.name, path.read_bytes(), "application/octet-stream"))])
        log(f"upload {doc['id']} as new version of {doc['version_of']}: {r.json()['results'][0]['status']}")
    wait_for_processing(clients)


def wait_for_processing(clients: dict[str, httpx.Client], timeout: float = 900) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        busy = 0
        for c in clients.values():
            for d in c.get("/api/documents").json()["documents"]:
                if d["current_version"] and d["current_version"]["status"] in ("pending", "processing"):
                    busy += 1
        if not busy:
            return
        log(f"waiting for worker: {busy} documents in processing")
        time.sleep(3)
    raise SystemExit("timed out waiting for the worker")


def _norm(value) -> str | None:
    if value is None:
        return None
    try:
        return str(Decimal(str(value)).normalize())
    except Exception:  # noqa: BLE001
        return str(value)


def approve_matching(client: httpx.Client, docs: list[dict]) -> tuple[int, int]:
    truth = {}
    for doc in docs:
        title = Path(doc["filename"]).stem.replace("_", " ")
        for rec in doc.get("records", []):
            truth.setdefault(title, []).append(rec)
    approved = left = 0
    for item in client.get("/api/review/queue").json()["items"]:
        if item["kind"] != "record" or item["verification_status"] != "auto_extracted":
            left += 1
            continue
        s = item["summary"]
        candidates = truth.get(item["document"]["title"], [])
        match = any(all(_norm(s.get(f)) == _norm(r.get(f)) for f in CHECK_FIELDS)
                    and r["data_kind"] == s["data_kind"] for r in candidates)
        if match:
            client.post(f"/api/review/records/{item['id']}/approve", json={"note": "אושר בהשוואה לנתוני הדמו"})
            approved += 1
        else:
            left += 1
    return approved, left


def main() -> None:
    truth = yaml.safe_load((FIXTURES / "ground_truth.yaml").read_text(encoding="utf-8"))
    docs = truth["documents"]
    bootstrap_offices()
    clients = {k: client_for(o["admin"]) for k, o in OFFICES.items()}
    seed_structured_attributes(clients)
    group_ids = {"A": ensure_groups_and_users(clients["A"])}
    group_ids["A"]["default"] = group_ids["A"]["G1"]
    group_ids["B"] = {"default": clients["B"].get("/api/admin/groups").json()["groups"][0]["id"]}
    if not clients["A"].get("/api/documents").json()["documents"]:
        upload_all(clients, group_ids, docs)
    else:
        log("documents already uploaded; skipping uploads")
    for key, c in clients.items():
        approved, left = approve_matching(c, docs)
        log(f"office {key}: approved {approved} records matching ground truth; {left} items left for review")
    log("demo users (password from DEMO_PASSWORD): "
        + ", ".join([o["admin"] for o in OFFICES.values()] + [e["email"] for e in EMPLOYEES]))


if __name__ == "__main__":
    main()
