"""Document version processing (U4, KTD8).

Stages run in the worker under the job's office with role ``system``:

1. extract  — pages, tables, chunks. One transaction: delete this version's prior outputs, insert.
              A version whose file already exists (processed) in a group the uploader cannot see
              clones those outputs instead of re-running OCR and indexing. Picture content already read in
              another document of the office is taken from ``image_readings`` (``ImageReadingStore``).
              A vision failure that is transient or a configuration error fails the job before anything is
              written, so the version keeps its earlier reading.
2. embed    — vectors for chunks without one for the active model.
3. publish  — facts, validation, dedup, version status and data-version bump, in one transaction.

Every stage is idempotent, so a crashed job whose lease expired simply runs again. The version
becomes ``ready``/``needs_review`` only in the publish transaction, never in between.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import logging
import time
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import Connection, text

from app.config import get_settings
from app.db import (  # noqa: F401 - system_ctx re-exported
    TenantContext,
    bump_data_version,
    system_ctx,
    tenant_tx,
)
from app.extraction.base import Block, ExtractionResult, check_deadline
from app.extraction.normalize_text import normalize_for_search
from app.extraction.pdf import READER_VERSION as PDF_READER_VERSION

logger = logging.getLogger(__name__)

EXTRACTION_VERSION = "rules-v1"
# Version of the document reading itself (blocks, pictures, chunk boundaries), per format. A version processed
# under an older one is queued again by the admin reprocess.
INGESTION_VERSION = "docx-blocks-v3"
PDF_INGESTION_VERSION = PDF_READER_VERSION


def ingestion_version(mime_type: str | None) -> str:
    """The current reader version of a file type: a PDF's block reader, or the DOCX reader."""
    return PDF_INGESTION_VERSION if mime_type == "application/pdf" else INGESTION_VERSION


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
    for table in ("occurrences", "chunks", "extracted_tables", "pages", "document_blocks"):
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
            " normalized_text, embedding, embedding_model, table_index, row_index, block_start, block_end)"
            " SELECT office_id, :d, :v, chunk_index, kind, page_list, section, text, normalized_text, embedding,"
            " embedding_model, table_index, row_index, block_start, block_end FROM chunks WHERE version_id = :s"
        ),
        params,
    )
    conn.execute(
        text(
            "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section, section_path,"
            " label, paragraph_no, page, media, source, status, note, table_index, text, bbox, method, reader_version,"
            " content_hash, original_text) SELECT office_id, :d, :v, block_index, kind, section, section_path, label,"
            " paragraph_no, page, media, source, status, note, table_index, text, bbox, method, reader_version,"
            " content_hash, original_text FROM document_blocks WHERE version_id = :s"
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
        text("UPDATE document_versions SET page_count = :p, pages_incomplete = :i, ingestion = (SELECT ingestion"
             " FROM document_versions WHERE id = :s), extraction_version = (SELECT extraction_version FROM"
             " document_versions WHERE id = :s) WHERE id = :v"),
        {"p": page_count, "i": pages_incomplete, "v": info.id, "s": src},
    )
    return pages_incomplete


def _blocks_of(result: ExtractionResult) -> list[Block]:
    """The result's blocks. The built-in readers always produce them; for an extractor that returns chunks only (a
    plug-in adapter), each chunk becomes one block on its first page (tables are their own blocks) and the chunk
    points at it."""
    if result.blocks:
        return result.blocks
    blocks: list[Block] = []
    for c in result.chunks:
        if c.kind == "table_row":
            continue
        page = c.page_list[0] if c.page_list else None
        blocks.append(Block(index=len(blocks), kind="paragraph", text=c.text, section=c.section,
                            section_path=[c.section] if c.section else [], page=page))
        c.block_start = c.block_end = blocks[-1].index
    for t in result.tables:
        blocks.append(Block(index=len(blocks), kind="table", text="\n".join(" | ".join(r.cells) for r in t.rows),
                            section=t.section, section_path=[t.section] if t.section else [], page=t.page_start,
                            source="ocr" if t.ocr else "text", table_index=t.index,
                            status="read_uncertain" if t.ocr else "read"))
        t.block_index = blocks[-1].index
        for c in result.chunks:
            if c.kind == "table_row" and c.table_index == t.index:
                c.block_start = c.block_end = t.block_index
    return blocks


def persist_extraction(conn: Connection, info: VersionInfo, result: ExtractionResult) -> None:
    _delete_outputs(conn, info.id)
    _insert_outputs(conn, info, result)


def _insert_outputs(conn: Connection, info: VersionInfo, result: ExtractionResult) -> None:
    blocks = _blocks_of(result)
    for p in result.pages:
        conn.execute(
            text(
                "INSERT INTO pages (office_id, document_id, version_id, page_no, text, method, quality, ok)"
                " VALUES (app_office(), :d, :v, :n, :t, :m, :q, :ok)"
            ),
            {"d": info.document_id, "v": info.id, "n": p.page_no, "t": p.text, "m": p.method,
             "q": round(p.quality, 4), "ok": p.ok},
        )
    for b in blocks:
        conn.execute(
            text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, label, paragraph_no, page, media, source, status, note, table_index, text, bbox,"
                " method, reader_version, content_hash, original_text) VALUES (app_office(), :d, :v, :i, :k, :s,"
                " :sp, :l, :pn, :pg, :m, :src, :st, :n, :ti, :t, CAST(:bb AS jsonb), :me, :rv, :ch, :ot)"
            ),
            {"d": info.document_id, "v": info.id, "i": b.index, "k": b.kind, "s": b.section, "sp": b.section_path,
             "l": b.label, "pn": b.paragraph_no, "pg": b.page, "m": b.media, "src": b.source, "st": b.status,
             "n": b.note, "ti": b.table_index, "t": b.text, "bb": json.dumps(b.bbox) if b.bbox else None,
             "me": b.method, "rv": b.reader_version, "ch": b.content_hash, "ot": b.original_text},
        )
    for t in result.tables:
        structure = {
            "headers": t.headers, "units": t.units, "ocr": t.ocr, "section": t.section,
            "rows": [{"page": r.page, "cells": r.cells} for r in t.rows],
            "source": t.source, "media": t.media, "caption": t.caption, "title": t.title, "notes": t.notes,
            "block_index": t.block_index,
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
                " text, normalized_text, table_index, row_index, block_start, block_end) VALUES (app_office(), :d,"
                " :v, :i, :k, :pl, :s, :t, :n, :ti, :ri, :bs, :be)"
            ),
            {"d": info.document_id, "v": info.id, "i": c.index, "k": c.kind, "pl": c.page_list, "s": c.section,
             "t": c.text, "n": normalize_for_search(c.text), "ti": c.table_index, "ri": c.row_index,
             "bs": c.block_start, "be": c.block_end},
        )
    version = INGESTION_VERSION if result.is_docx else PDF_INGESTION_VERSION
    report = result.components | {"ingestion_version": version, "chunks": len(result.chunks)}
    conn.execute(
        text("UPDATE document_versions SET page_count = :p, pages_incomplete = :i, extraction_version = :e,"
             " ingestion = CAST(:g AS jsonb) WHERE id = :v"),
        {"p": result.page_count, "i": result.pages_incomplete, "e": EXTRACTION_VERSION, "v": info.id,
         "g": json.dumps(report, ensure_ascii=False)},
    )


def extract_stage(ctx: TenantContext, info: VersionInfo, deadline: float) -> None:
    with tenant_tx(ctx) as conn:
        if info.cloned_from and _source_has_outputs(conn, info.cloned_from):
            clone_outputs(conn, info)
            return
    from app.platform.storage import get_storage

    data = get_storage().get(info.storage_key)
    result = _extract(data, info.mime_type, deadline, vision_reader(ctx), ImageReadingStore(ctx))
    with tenant_tx(ctx) as conn:
        persist_extraction(conn, info, result)


def _extract(data: bytes, mime_type: str, deadline: float, vision, readings=None) -> ExtractionResult:
    from app.extraction.pipeline import get_extractor

    extractor = get_extractor()
    kwargs = {}
    if vision is not None:  # extractors written before pictures were read by the vision model take no reader
        kwargs["vision"] = vision
    if readings is not None and "readings" in inspect.signature(extractor.extract).parameters:
        kwargs["readings"] = readings
    return extractor.extract(data, mime_type, deadline, **kwargs)


class ImageReadingStore:
    """The office's readings of picture content (``image_readings``, KTD5), for region reading during ingestion
    only: every access runs under the office's system context, which the table's policies require."""

    def __init__(self, ctx: TenantContext):
        self.ctx = ctx

    def get(self, key):
        from app.extraction.images import PictureReading, PictureTable

        with tenant_tx(self.ctx) as conn:
            row = conn.execute(text(
                "SELECT reading FROM image_readings WHERE content_hash = :h AND reader_version = :r"
                " AND model_config = :m AND crop_scale = :c"),
                {"h": key.content_hash, "r": key.reader_version, "m": key.model_config, "c": key.crop_scale}).first()
        if row is None:
            return None
        data = dict(row.reading)
        data["tables"] = [PictureTable(**t) for t in data.get("tables") or []]
        return PictureReading(**data)

    def put(self, key, reading) -> None:
        with tenant_tx(self.ctx) as conn:
            conn.execute(text(
                "INSERT INTO image_readings (office_id, content_hash, reader_version, model_config, crop_scale,"
                " status, reading) VALUES (app_office(), :h, :r, :m, :c, :s, CAST(:g AS jsonb))"
                " ON CONFLICT (office_id, content_hash, reader_version, model_config, crop_scale)"
                " DO UPDATE SET status = EXCLUDED.status, reading = EXCLUDED.reading, updated_at = now()"),
                {"h": key.content_hash, "r": key.reader_version, "m": key.model_config, "c": key.crop_scale,
                 "s": reading.status, "g": json.dumps(dataclasses.asdict(reading), ensure_ascii=False)})


def vision_reader(ctx: TenantContext):
    """The vision model reader for pictures, only when the office's provider mode is cloud (the same consent
    that lets document text reach the model); None otherwise (pictures are read by OCR alone)."""
    from app.extraction.vision import ModelVisionReader
    from app.providers.status import Mode, office_provider_state

    with tenant_tx(ctx) as conn:
        state = office_provider_state(conn)
    if state.mode != Mode.CLOUD:
        return None
    return ModelVisionReader(ctx.office_id)


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
    outcome = publish_stage(ctx, info)
    if vision_reader(ctx) is not None:
        from app.measurements.extract import EXTRACTION_VERSION as MEASURE_VERSION
        from app.platform.jobs import enqueue_measurements

        with tenant_tx(ctx) as conn:
            enqueue_measurements(conn, info.id, MEASURE_VERSION)
    return outcome


# --- reindexing and measurements ----------------------------------------------------------------------------

_DERIVED_TEXT_TABLES = ("chunks", "extracted_tables", "pages", "document_blocks")


def reindex_version(office_id: UUID, version_id: UUID) -> str | None:
    """Read a processed version again under the current reader: its blocks, pictures, tables, chunks and
    embeddings are replaced; its records (and their reviews) are kept. Facts the earlier engine extracted from
    the old chunks and never reviewed go (a reviewed fact stays); cached answers are dropped and the data version
    moves, so no answer built on the old reading is served again. Measurements are then re-extracted when the
    office allows the cloud model."""
    from app.platform.jobs import enqueue_measurements
    from app.platform.storage import get_storage

    ctx = system_ctx(office_id)
    deadline = time.monotonic() + get_settings().job_timeout_seconds
    with tenant_tx(ctx) as conn:
        row = conn.execute(text(
            "SELECT v.id, v.document_id, v.storage_key, v.mime_type, v.cloned_from_version_id FROM document_versions v"
            " JOIN documents d ON d.id = v.document_id AND d.deleted_at IS NULL WHERE v.id = :v"
            " AND v.status IN ('ready', 'needs_review')"), {"v": version_id}).first()
    if row is None:
        return None
    info = VersionInfo(row.id, row.document_id, row.storage_key, row.mime_type, None)
    result = _extract(get_storage().get(info.storage_key), info.mime_type, deadline, vision_reader(ctx),
                      ImageReadingStore(ctx))
    with tenant_tx(ctx) as conn:
        for table in _DERIVED_TEXT_TABLES:
            conn.execute(text(f"DELETE FROM {table} WHERE version_id = :v"), {"v": info.id})
        records = conn.execute(text("SELECT count(*) FROM occurrences WHERE version_id = :v"), {"v": info.id}).scalar_one()
        _insert_outputs(conn, info, result)
        conn.execute(text("DELETE FROM facts WHERE version_id = :v AND status IN ('auto_validated', 'needs_review')"),
                     {"v": info.id})
        conn.execute(text("DELETE FROM fact_extraction_ledger WHERE version_id = :v"), {"v": info.id})
        conn.execute(text("DELETE FROM answer_cache"))
        bump_data_version(conn)
        logger.info("reindexed version %s (%d records kept)", info.id, records)
    embed_stage(ctx, info, deadline)
    if vision_reader(ctx) is not None:
        from app.measurements.extract import EXTRACTION_VERSION as MEASURE_VERSION

        with tenant_tx(ctx) as conn:
            enqueue_measurements(conn, info.id, MEASURE_VERSION)
    return "reindexed"


def run_measurements(office_id: UUID, version_id: UUID) -> str:
    from app.measurements.extract import extract_version
    from app.providers.llm import get_selected_provider

    ctx = system_ctx(office_id)
    if vision_reader(ctx) is None:  # same consent: the office's cloud mode
        return "skipped"
    result = extract_version(ctx, version_id, get_selected_provider())
    with tenant_tx(ctx) as conn:
        bump_data_version(conn)
    return result.state
