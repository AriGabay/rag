"""Acceptance world for the eight gates of origin §11 (U15).

``build_world`` recreates the demo exactly like ``scripts/seed_demo.py`` but in-process against the
``rag_test`` database: two offices (A, B), groups G1/G2 and the two employees through the admin API,
every synthetic fixture uploaded through ``POST /api/documents`` with the real ``DefaultExtractor``,
the worker drained with ``worker.run_one``, D1v2 uploaded as a new version of D1 after D1 is
processed, and records approved with the seed's own rule (auto-extracted records whose key fields
equal the answer key).

Read-only gate modules share one world (``world``); modules that change data ask for
``mutable_world`` (always fresh) or call ``world.dirty()`` before mutating, so the next module
rebuilds. Building takes a few seconds (OCR of D2 runs only where Tesseract has Hebrew).
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import worker
from app.db import TenantContext, reset_engine, tenant_tx
from eval.truth import (
    EXTRACTION_SQL_OCCURRENCES,
    all_records,
    gt_record_for,
    truth,
    unverified_from_queue,
)
from tests.conftest import _TABLES
from tests.factories import OfficeFixture, make_office

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
PASSWORD = "demo1234"
ADMIN_A, ADMIN_B, DANA, YOSSI = "admin-a@demo.test", "admin-b@demo.test", "dana@demo.test", "yossi@demo.test"

def _load_seed():
    spec = importlib.util.spec_from_file_location("seed_demo_for_acceptance", ROOT / "scripts" / "seed_demo.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SEED = _load_seed()


def heb_ocr_available() -> bool:
    try:
        import pytesseract

        return "heb" in pytesseract.get_languages(config="")
    except Exception:  # noqa: BLE001
        return False


requires_ocr = pytest.mark.skipif(not heb_ocr_available(), reason="tesseract with Hebrew data not installed (run in image)")


def drain(limit: int = 200) -> int:
    n = 0
    while n < limit and worker.run_one("acceptance-worker"):
        n += 1
    return n


@dataclass
class World:
    owner_engine: object
    a: OfficeFixture
    b: OfficeFixture
    app: object
    groups: dict[str, str] = field(default_factory=dict)  # G1, G2, B
    docs: dict[str, dict] = field(default_factory=dict)  # answer-key doc id -> upload result
    unverified: set[str] = field(default_factory=set)  # answer-key ids still in the review queue
    not_extracted: set[str] = field(default_factory=set)  # answer-key ids with no occurrence at all
    data_versions: dict[str, int] = field(default_factory=dict)
    clients: dict[str, TestClient] = field(default_factory=dict)
    unmapped_queue_items: list[str] = field(default_factory=list)
    is_dirty: bool = False

    def client(self, email: str) -> TestClient:
        if email not in self.clients:
            c = TestClient(self.app)
            r = c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
            assert r.status_code == 200, r.text
            self.clients[email] = c
        return self.clients[email]

    def fresh_client(self, email: str) -> TestClient:
        self.clients.pop(email, None)
        return self.client(email)

    def office(self, key: str) -> OfficeFixture:
        return self.a if key == "A" else self.b

    def system(self, key: str) -> TenantContext:
        return self.office(key).system

    @property
    def exclude(self) -> set[str]:
        return self.unverified | self.not_extracted

    def dirty(self) -> None:
        self.is_dirty = True

    def doc_id(self, gt_id: str) -> str:
        return self.docs[gt_id]["document_id"]

    def scalar(self, key: str, sql: str, **params):
        with tenant_tx(self.system(key)) as conn:
            return conn.execute(text(sql), params).scalar()

    def refresh_verification(self) -> None:
        self.unverified, self.unmapped_queue_items = set(), []
        for key in ("A", "B"):
            ids, unmapped = unverified_from_queue(self.client(ADMIN_A if key == "A" else ADMIN_B), key)
            self.unmapped_queue_items += unmapped
            self.unverified |= ids
        self.not_extracted = set()
        for key in ("A", "B"):
            with tenant_tx(self.system(key)) as conn:
                rows = [dict(r._mapping) for r in conn.execute(text(EXTRACTION_SQL_OCCURRENCES)).all()]
            found = {rec["id"] for o in rows
                     if (rec := gt_record_for(o["filename"], o["table_index"], o["row_index"], o["data_kind"]))}
            self.not_extracted |= {r["id"] for r in all_records() if r["_office"] == key and r["id"] not in found}

    def snapshot_versions(self) -> dict[str, int]:
        out = {}
        for key in ("A", "B"):
            out[key] = self.scalar(key, "SELECT version FROM office_data_versions")
        return out

    def close(self) -> None:
        for c in self.clients.values():
            c.close()
        self.clients.clear()


def upload_file(client: TestClient, filename: str, group_id: str | None = None, document_id: str | None = None) -> dict:
    data = {"group_id": group_id} if group_id else {"document_id": document_id}
    r = client.post("/api/documents", data=data,
                    files=[("files", (filename, (FIXTURES / filename).read_bytes(), "application/octet-stream"))])
    assert r.status_code == 200, r.text
    return r.json()["results"][0]


def build_world(owner_engine, with_new_version: bool = True, approve: bool = True) -> World:
    from app.main import create_app

    with owner_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {_TABLES} CASCADE"))
    reset_engine()
    a = make_office(owner_engine, SEED.OFFICES["A"]["name"], ADMIN_A, password=PASSWORD)
    b = make_office(owner_engine, SEED.OFFICES["B"]["name"], ADMIN_B, password=PASSWORD)
    w = World(owner_engine=owner_engine, a=a, b=b, app=create_app())
    ids = SEED.ensure_groups_and_users(w.client(ADMIN_A))
    w.groups = {"G1": ids["G1"], "G2": ids["G2"], "B": str(b.default_group_id)}

    docs = truth()["documents"]
    for d in [d for d in docs if not d.get("version_of")]:
        client = w.client(ADMIN_A if d["office"] == "A" else ADMIN_B)
        group = w.groups["B"] if d["office"] == "B" else w.groups[d["group"]]
        w.docs[d["id"]] = upload_file(client, d["filename"], group_id=group)
    drain()
    if with_new_version:
        for d in [d for d in docs if d.get("version_of")]:
            target = w.docs[d["version_of"]]["document_id"]
            w.docs[d["id"]] = upload_file(w.client(ADMIN_A), d["filename"], document_id=target)
        drain()
    if approve:
        approve_like_seed(w)
    w.refresh_verification()
    w.data_versions = w.snapshot_versions()
    return w


def approve_like_seed(w: World) -> None:
    docs = truth()["documents"]
    for email in (ADMIN_A, ADMIN_B):
        SEED.approve_matching(w.client(email), docs)


_SHARED: dict = {"world": None}


def _still_intact(w: World) -> bool:
    if w is None or w.is_dirty:
        return False
    try:
        return w.snapshot_versions() == w.data_versions
    except Exception:  # noqa: BLE001 - offices truncated by another test module
        return False


@pytest.fixture(scope="module")
def world(owner_engine):
    """The shared, read-only acceptance world (rebuilt when a previous module changed it)."""
    w = _SHARED["world"]
    if not _still_intact(w):
        if w is not None:
            w.close()
        w = build_world(owner_engine)
        _SHARED["world"] = w
    yield w


@pytest.fixture(scope="module")
def mutable_world(owner_engine):
    """A fresh world this module may change; the shared world is rebuilt afterwards."""
    if _SHARED["world"] is not None:
        _SHARED["world"].close()
        _SHARED["world"] = None
    w = build_world(owner_engine)
    w.dirty()
    yield w
    w.close()


def as_uuid(value: str) -> UUID:
    return UUID(str(value))
