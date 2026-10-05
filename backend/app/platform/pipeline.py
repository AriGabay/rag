"""Document version processing (U4, KTD8).

Stages run in the worker under the job's office with role ``system``:

1. extract  — pages, tables, chunks. One transaction: delete this version's prior outputs, insert.
              A version whose file already exists (processed) in a group the uploader cannot see
              clones those outputs instead of re-running OCR and indexing.
2. embed    — vectors for chunks without one for the active model.
3. publish  — facts, validation, dedup, version status and data-version bump, in one transaction.

Every stage is idempotent, so a crashed job whose lease expired simply runs again. The version
becomes ``ready``/``needs_review`` only in the publish transaction, never in between.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection, text

from app.config import get_settings
from app.db import TenantContext, system_ctx, tenant_tx  # noqa: F401 - system_ctx re-exported
from app.extraction.base import ExtractionResult, check_deadline
from app.extraction.normalize_text import normalize_for_search

logger = logging.getLogger(__name__)

EXTRACTION_VERSION = "rules-v1"


@dataclass
class VersionInfo:
    id: UUID
    document_id: UUID
    storage_key: str
    mime_type: str
    cloned_from: UUID | None


def start_processing(ctx: TenantContext, version_id: UUID) -> VersionInfo | None:
    with tenant_tx(ctx) as conn:
        row = conn.execute(
            text(
                "UPDATE document_versions v SET status = 'processing', status_reason = NULL"
                " FROM documents d WHERE v.id = :v AND d.id = v.document_id AND d.deleted_at IS NULL"
                " AND v.status IN ('pending', 'processing')"
                " RETURNING v.id, v.document_id, v.storage_key, v.mime_type, v.cloned_from_version_id"
            ),
            {"v": version_id},
        ).first()
    if row is None:
        return None
    return VersionInfo(row.id, row.document_id, row.storage_key, row.mime_type, row.cloned_from_version_id)


# --- stage 1 ---------------------------------------------------------------------------------------

def _delete_outputs(conn: Connection, version_id: UUID) -> None:
    conn.execute(
        text("DELETE FROM fact_values WHERE occurrence_id IN (SELECT id FROM occurrences WHERE version_id = :v)"),
        {"v": version_id},
    )
    for table in ("occurrences", "chunks", "extracted_tables", "pages"):
        conn.execute(text(f"DELETE FROM {table} WHERE version_id = :v"), {"v": version_id})


def _source_has_outputs(conn: Connection, source: UUID) -> bool:
    return conn.execute(
        text("SELECT 1 FROM document_versions WHERE id = :s AND status IN ('ready', 'needs_review', 'superseded')"),
        {"s": source},
    ).first() is not None


def clone_outputs(conn: Connection, info: VersionInfo) -> int:
    src = info.cloned_from
    _delete_outputs(conn, info.id)
    params = {"v": info.id, "d": info.document_id, "s": src}
    conn.execute(
        text(
            "INSERT INTO pages (office_id, document_id, version_id, page_no, text, method, quality, ok)"
            " SELECT office_id, :d, :v, page_no, text, method, quality, ok FROM pages WHERE version_id = :s"
        ),
        params,
    )
    conn.execute(
        text(
            "INSERT INTO extracted_tables (office_id, document_id, version_id, table_index, page_start, page_end,"
            " structure) SELECT office_id, :d, :v, table_index, page_start, page_end, structure"
            " FROM extracted_tables WHERE version_id = :s"
        ),
        params,
    )
    conn.execute(
        text(
            "INSERT INTO chunks (office_id, document_id, version_id, chunk_index, kind, page_list, section, text,"
            " normalized_text, embedding, embedding_model) SELECT office_id, :d, :v, chunk_index, kind, page_list,"
            " section, text, normalized_text, embedding, embedding_model FROM chunks WHERE version_id = :s"
        ),
        params,
    )
    page_count = conn.execute(
        text("SELECT page_count FROM document_versions WHERE id = :s"), {"s": src}
    ).scalar_one_or_none()
    pages_incomplete = conn.execute(
        text("SELECT count(*) FROM pages WHERE version_id = :v AND NOT ok"), {"v": info.id}
    ).scalar_one()
    conn.execute(
        text("UPDATE document_versions SET page_count = :p, pages_incomplete = :i WHERE id = :v"),
        {"p": page_count, "i": pages_incomplete, "v": info.id},
    )
    return pages_incomplete


def persist_extraction(conn: Connection, info: VersionInfo, result: ExtractionResult) -> None:
    _delete_outputs(conn, info.id)
    for p in result.pages:
        conn.execute(
            text(
                "INSERT INTO pages (office_id, document_id, version_id, page_no, text, method, quality, ok)"
                " VALUES (app_office(), :d, :v, :n, :t, :m, :q, :ok)"
            ),
            {"d": info.document_id, "v": info.id, "n": p.page_no, "t": p.text, "m": p.method,
             "q": round(p.quality, 4), "ok": p.ok},
        )
    for t in result.tables:
        structure = {
            "headers": t.headers, "units": t.units, "ocr": t.ocr, "section": t.section,
            "rows": [{"page": r.page, "cells": r.cells} for r in t.rows],
        }
        conn.execute(
            text(
                "INSERT INTO extracted_tables (office_id, document_id, version_id, table_index, page_start,"
                " page_end, structure) VALUES (app_office(), :d, :v, :i, :ps, :pe, CAST(:s AS jsonb))"
            ),
            {"d": info.document_id, "v": info.id, "i": t.index, "ps": t.page_start, "pe": t.page_end,
             "s": json.dumps(structure, ensure_ascii=False)},
        )
    for c in result.chunks:
        conn.execute(
            text(
                "INSERT INTO chunks (office_id, document_id, version_id, chunk_index, kind, page_list, section,"
                " text, normalized_text) VALUES (app_office(), :d, :v, :i, :k, :pl, :s, :t, :n)"
            ),
            {"d": info.document_id, "v": info.id, "i": c.index, "k": c.kind, "pl": c.page_list, "s": c.section,
             "t": c.text, "n": normalize_for_search(c.text)},
        )
    conn.execute(
        text("UPDATE document_versions SET page_count = :p, pages_incomplete = :i, extraction_version = :e"
             " WHERE id = :v"),
        {"p": result.page_count, "i": result.pages_incomplete, "e": EXTRACTION_VERSION, "v": info.id},
    )


def extract_stage(ctx: TenantContext, info: VersionInfo, deadline: float) -> None:
    with tenant_tx(ctx) as conn:
        if info.cloned_from and _source_has_outputs(conn, info.cloned_from):
            clone_outputs(conn, info)
            return
    from app.extraction.pipeline import get_extractor
    from app.platform.storage import get_storage

    data = get_storage().get(info.storage_key)
    result = get_extractor().extract(data, info.mime_type, deadline)
    with tenant_tx(ctx) as conn:
        persist_extraction(conn, info, result)


# --- stage 2 ---------------------------------------------------------------------------------------

def embed_stage(ctx: TenantContext, info: VersionInfo, deadline: float) -> None:
    from app.providers.embeddings import get_embedding_provider, to_pgvector

    provider = get_embedding_provider()
    while True:
        with tenant_tx(ctx) as conn:
            rows = conn.execute(
                text(
                    "SELECT id, text FROM chunks WHERE version_id = :v"
                    " AND (embedding IS NULL OR embedding_model IS DISTINCT FROM :m) ORDER BY chunk_index LIMIT 64"
                ),
                {"v": info.id, "m": provider.model_id},
            ).all()
        if not rows:
            return
        check_deadline(deadline)
        vectors = provider.embed_passages([r.text for r in rows])
        with tenant_tx(ctx) as conn:
            for r, vec in zip(rows, vectors, strict=True):
                conn.execute(
                    text("UPDATE chunks SET embedding = CAST(:e AS vector), embedding_model = :m WHERE id = :id"),
                    {"e": to_pgvector(vec), "m": provider.model_id, "id": r.id},
                )


# --- stage 3 ---------------------------------------------------------------------------------------

def publish_stage(ctx: TenantContext, info: VersionInfo) -> str:
    from app.appraisal.publish import publish_version

    with tenant_tx(ctx) as conn:
        return publish_version(conn, info.id, info.document_id, EXTRACTION_VERSION)


# --- orchestration -----------------------------------------------------------------------------------

def mark_failed(ctx: TenantContext, version_id: UUID, reason: str) -> None:
    with tenant_tx(ctx) as conn:
        conn.execute(
            text("UPDATE document_versions SET status = 'failed', status_reason = :r, processed_at = now()"
                 " WHERE id = :v"),
            {"r": reason, "v": version_id},
        )


def mark_retry(ctx: TenantContext, version_id: UUID) -> None:
    with tenant_tx(ctx) as conn:
        conn.execute(
            text("UPDATE document_versions SET status = 'pending', status_reason = :r WHERE id = :v"),
            {"r": "העיבוד ייוסה שוב בקרוב", "v": version_id},
        )


def process_version(office_id: UUID, version_id: UUID) -> str | None:
    """Run all stages. Raises ExtractionError or other exceptions to the worker loop."""
    ctx = system_ctx(office_id)
    deadline = time.monotonic() + get_settings().job_timeout_seconds
    info = start_processing(ctx, version_id)
    if info is None:
        return None  # deleted, superseded, or already published: nothing to do
    extract_stage(ctx, info, deadline)
    embed_stage(ctx, info, deadline)
    return publish_stage(ctx, info)
