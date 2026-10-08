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

Reprocessing a published version (``reindex_version``, KTD7) reads and embeds before any write, keeps the current
reading when the new one is worse (``reading_regression``), and otherwise swaps the whole reading, with its
embeddings and a new ``reading_id``, in one transaction. A reprocess that keeps the current reading ends its job as
``kept_previous`` with the reason on the version (KTD9, ``jobs.fail_job``): the document stays available and no admin
action is needed.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import Connection, text

from app.config import get_settings
from app.db import (  # noqa: F401 - system_ctx re-exported
    TenantContext,
    bump_data_version,
    system_ctx,
    tenant_tx,
)
from app.extraction.base import Block, ExtractionError, ExtractionResult, check_deadline
from app.extraction.geometry import POSITIONS_VERSION
from app.extraction.normalize_text import normalize_for_search, undouble_word
from app.extraction.pdf import READER_VERSION as PDF_READER_VERSION
from app.extraction.regions import shown_hash

logger = logging.getLogger(__name__)

EXTRACTION_VERSION = "rules-v1"
# Version of the document reading itself (blocks, pictures, chunk boundaries), per format. A version processed
# under an older one is queued again by the admin reprocess.
INGESTION_VERSION = "docx-blocks-v4"
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
            "INSERT INTO pages (office_id, document_id, version_id, page_no, text, method, quality, ok, mediabox,"
            " cropbox, rotation, display_width, display_height, printed_label, geometry_issue)"
            " SELECT office_id, :d, :v, page_no, text, method, quality, ok, mediabox, cropbox, rotation,"
            " display_width, display_height, printed_label, geometry_issue FROM pages WHERE version_id = :s"
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
            " content_hash, original_text, spans) SELECT office_id, :d, :v, block_index, kind, section, section_path,"
            " label, paragraph_no, page, media, source, status, note, table_index, text, bbox, method, reader_version,"
            " content_hash, original_text, spans FROM document_blocks WHERE version_id = :s"
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
             " - 'reprocess_regression' - 'reprocess_kept' FROM document_versions WHERE id = :s), extraction_version = (SELECT extraction_version FROM"
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


def _insert_outputs(conn: Connection, info: VersionInfo, result: ExtractionResult,
                    embeddings: tuple[list[str], str] | None = None, report_extra: dict | None = None) -> None:
    """Write a reading. ``embeddings``: each chunk's vector (pgvector text) and the model, when computed before
    the write (reprocessing); otherwise the embed stage fills them. Every reading gets a new ``reading_id`` in its
    ingestion report: what an answer's sources and references are bound to (KTD7). Positions (KTD2) go with it:
    each page's geometry and printed number, each text block's word spans, each text-layer table cell's box
    (``structure.rows[k].cell_boxes``, ``structure.header_boxes``), and the ``positions`` marker the geometry
    backfill selects versions by (KTD3)."""
    blocks = _blocks_of(result)
    for p in result.pages:
        g = p.geometry
        conn.execute(
            text(
                "INSERT INTO pages (office_id, document_id, version_id, page_no, text, method, quality, ok, mediabox,"
                " cropbox, rotation, display_width, display_height, printed_label, geometry_issue)"
                " VALUES (app_office(), :d, :v, :n, :t, :m, :q, :ok, CAST(:mb AS jsonb), CAST(:cb AS jsonb), :r,"
                " :w, :h, :pl, :gi)"
            ),
            {"d": info.document_id, "v": info.id, "n": p.page_no, "t": p.text, "m": p.method,
             "q": round(p.quality, 4), "ok": p.ok,
             "mb": json.dumps(g.mediabox) if g is not None and g.mediabox is not None else None,
             "cb": json.dumps(g.cropbox) if g is not None and g.cropbox is not None else None,
             "r": g.rotation if g is not None else None, "w": g.width if g is not None else None,
             "h": g.height if g is not None else None, "pl": p.printed_label,
             "gi": g.issue if g is not None else None},
        )
    for b in blocks:
        conn.execute(
            text(
                "INSERT INTO document_blocks (office_id, document_id, version_id, block_index, kind, section,"
                " section_path, label, paragraph_no, page, media, source, status, note, table_index, text, bbox,"
                " method, reader_version, content_hash, original_text, spans) VALUES (app_office(), :d, :v, :i, :k,"
                " :s, :sp, :l, :pn, :pg, :m, :src, :st, :n, :ti, :t, CAST(:bb AS jsonb), :me, :rv, :ch, :ot,"
                " CAST(:spans AS jsonb))"
            ),
            {"d": info.document_id, "v": info.id, "i": b.index, "k": b.kind, "s": b.section, "sp": b.section_path,
             "l": b.label, "pn": b.paragraph_no, "pg": b.page, "m": b.media, "src": b.source, "st": b.status,
             "n": b.note, "ti": b.table_index, "t": b.text, "bb": json.dumps(b.bbox) if b.bbox else None,
             "me": b.method, "rv": b.reader_version, "ch": b.content_hash, "ot": b.original_text,
             "spans": json.dumps(b.spans) if b.spans else None},
        )
    for t in result.tables:
        structure = {
            "headers": t.headers, "units": t.units, "ocr": t.ocr, "section": t.section,
            "rows": [{"page": r.page, "cells": r.cells} | ({"cell_boxes": r.cell_boxes} if r.cell_boxes else {})
                     for r in t.rows],
            "source": t.source, "media": t.media, "caption": t.caption, "title": t.title, "notes": t.notes,
            "block_index": t.block_index,
        }
        if t.header_boxes:
            structure["header_boxes"] = t.header_boxes
        conn.execute(
            text(
                "INSERT INTO extracted_tables (office_id, document_id, version_id, table_index, page_start,"
                " page_end, structure) VALUES (app_office(), :d, :v, :i, :ps, :pe, CAST(:s AS jsonb))"
            ),
            {"d": info.document_id, "v": info.id, "i": t.index, "ps": t.page_start, "pe": t.page_end,
             "s": json.dumps(structure, ensure_ascii=False)},
        )
    vectors, model = embeddings if embeddings is not None else ([None] * len(result.chunks), None)
    for c, vec in zip(result.chunks, vectors, strict=True):
        conn.execute(
            text(
                "INSERT INTO chunks (office_id, document_id, version_id, chunk_index, kind, page_list, section,"
                " text, normalized_text, table_index, row_index, block_start, block_end, embedding, embedding_model)"
                " VALUES (app_office(), :d, :v, :i, :k, :pl, :s, :t, :n, :ti, :ri, :bs, :be, CAST(:e AS vector),"
                " :em)"
            ),
            {"d": info.document_id, "v": info.id, "i": c.index, "k": c.kind, "pl": c.page_list, "s": c.section,
             "t": c.text, "n": normalize_for_search(c.text), "ti": c.table_index, "ri": c.row_index,
             "bs": c.block_start, "be": c.block_end, "e": vec, "em": model if vec is not None else None},
        )
    version = INGESTION_VERSION if result.is_docx else PDF_INGESTION_VERSION
    report = result.components | {"ingestion_version": version, "chunks": len(result.chunks),
                                  "reading_id": str(uuid4()), "positions": POSITIONS_VERSION} | (report_extra or {})
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
        from app.extraction.images import PictureReading

        with tenant_tx(self.ctx) as conn:
            row = conn.execute(text(
                "SELECT reading FROM image_readings WHERE content_hash = :h AND reader_version = :r"
                " AND model_config = :m AND crop_scale = :c"),
                {"h": key.content_hash, "r": key.reader_version, "m": key.model_config, "c": key.crop_scale}).first()
        if row is None:
            return None
        return PictureReading.from_json(row.reading)

    def put(self, key, reading) -> None:
        with tenant_tx(self.ctx) as conn:
            conn.execute(text(
                "INSERT INTO image_readings (office_id, content_hash, reader_version, model_config, crop_scale,"
                " status, reading) VALUES (app_office(), :h, :r, :m, :c, :s, CAST(:g AS jsonb))"
                " ON CONFLICT (office_id, content_hash, reader_version, model_config, crop_scale)"
                " DO UPDATE SET status = EXCLUDED.status, reading = EXCLUDED.reading, updated_at = now()"),
                {"h": key.content_hash, "r": key.reader_version, "m": key.model_config, "c": key.crop_scale,
                 "s": reading.status, "g": reading.to_json()})


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
EMBED_BATCH = 64
MISSING_SHOWN = 20  # missing numbers recorded per page
MSG_REGRESSION = ("הקריאה החדשה איבדה מידע ביחס לקריאה הקיימת ({summary}), ולכן נשמרה הקריאה הקיימת והמסמך זמין"
                  " כרגיל. מנהל יכול להחיל את הקריאה החדשה בכל זאת")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


class ReadingRegression(ExtractionError):
    """A new reading is worse than the current one on a measure both readers produce. The current reading stays,
    the regression is recorded on the version (``ingestion.reprocess_regression``) and the job ends at once as
    ``kept_previous`` (permanent: reading again would read the same; KTD9). Nothing waits for an admin; one may
    still apply the new reading on a re-run (``accept_regression``)."""

    def __init__(self, findings: dict):
        super().__init__(MSG_REGRESSION.format(summary=regression_summary(findings)), permanent=True)
        self.findings = findings


def regression_summary(findings: dict) -> str:
    parts = [f"עמוד {p['page']}: חסרים המספרים {', '.join(p['missing_numbers'][:5])}"
             for p in findings.get("pages") or []][:5]
    if findings.get("failed_pages"):
        parts.append("עמודים שלא נקראו: " + ", ".join(str(p) for p in findings["failed_pages"][:10]))
    if findings.get("tables"):
        parts.append(f"טבלאות: {findings['tables']['before']} ← {findings['tables']['after']}")
    return "; ".join(parts)


def _numbers(text_: str) -> set[str]:
    """The number tokens of a text, thousands separators dropped ("1,250,000" and "1250000" are one number)."""
    return {t.replace(",", "") for t in _NUMBER.findall(text_ or "")}


def _missing_numbers(old_text: str, new_numbers: set[str]) -> list[str]:
    """The numbers of an old page's text the new reading of that page no longer has. A word an older reader took
    from a bold font drawn twice (``"2200,,550000"``) counts as present when its single reading is."""
    missing: list[str] = []
    for word in (old_text or "").split():
        found = _numbers(word)
        if found <= new_numbers or _numbers(undouble_word(word)) <= new_numbers:
            continue
        missing += sorted(found - new_numbers)
    return list(dict.fromkeys(missing))


def _page_texts(result: ExtractionResult) -> dict[int, str]:
    """Per page, all text the new reading has there: the page text, its blocks, its table rows and each table's
    caption, title, headers and notes, and (for a reader without blocks) its chunks."""
    out: dict[int, list[str]] = defaultdict(list)
    for p in result.pages:
        out[p.page_no].append(p.text or "")
    for b in result.blocks:
        if b.page is not None:
            out[b.page] += [b.text or "", b.picture_text or ""]
    for t in result.tables:
        for r in t.rows:
            if r.page is not None:
                out[r.page].append(" ".join(r.cells))
        if t.page_start is not None:
            for page in range(t.page_start, (t.page_end or t.page_start) + 1):
                out[page].append(" ".join([t.caption or "", *t.title, *t.headers, *t.notes]))
    if not result.blocks:
        for c in result.chunks:
            for page in c.page_list or []:
                out[page].append(c.text)
    return {page: "\n".join(parts) for page, parts in out.items()}


def _furniture_numbers(result: ExtractionResult) -> set[str]:
    """The numbers of the page furniture the new reading read once for the whole document (a letterhead, a footer
    with an address and phone numbers): its one block stands at the first occurrence, but the numbers are on every
    page it is on, so no page lost them."""
    shown = {r["hash"] for r in result.repeated}
    return set().union(*(_numbers(f"{b.text or ''} {b.picture_text or ''}") for b in result.blocks
                         if b.content_hash and shown_hash(b.content_hash) in shown))


def reading_regression(conn: Connection, version_id: UUID, result: ExtractionResult) -> dict | None:
    """Whether ``result`` reads the version worse than its current reading, on measures both readers produce
    (KTD7): per page, every number of the current page text is still in the new reading of that page; no page
    read before fails now; no table is lost. Unread or uncertain regions the new reader reports are not counted:
    the old reader could not see them. None when the new reading is not worse."""
    old_pages = conn.execute(text("SELECT page_no, text, ok FROM pages WHERE version_id = :v ORDER BY page_no"),
                             {"v": version_id}).all()
    old_tables = conn.execute(text("SELECT count(*) FROM extracted_tables WHERE version_id = :v"),
                              {"v": version_id}).scalar_one()
    texts = _page_texts(result)
    furniture = _furniture_numbers(result)
    new_ok = {p.page_no: p.ok for p in result.pages}
    pages = []
    for p in old_pages:
        missing = _missing_numbers(p.text, _numbers(texts.get(p.page_no, "")) | furniture)
        if missing:
            pages.append({"page": p.page_no, "missing_numbers": missing[:MISSING_SHOWN]})
    failed = [p.page_no for p in old_pages if p.ok and not new_ok.get(p.page_no, False)]
    fewer = len(result.tables) < old_tables
    if not (pages or failed or fewer):
        return None
    out: dict = {"pages": pages, "failed_pages": failed,
                 "ingestion_version": INGESTION_VERSION if result.is_docx else PDF_INGESTION_VERSION,
                 "at": datetime.now(UTC).isoformat()}
    if fewer:
        out["tables"] = {"before": old_tables, "after": len(result.tables)}
    return out


def regression_within(new: dict, recorded: dict | None) -> bool:
    """Whether a newly computed regression is no worse than the one an admin reviewed and accepted: the same
    reader, every page's missing numbers among those recorded for that page, no page failing that was not recorded,
    and no larger table loss. Anything else is a different regression, which the acceptance does not cover."""
    if not recorded or new.get("ingestion_version") != recorded.get("ingestion_version"):
        return False
    seen = {p["page"]: set(p["missing_numbers"]) for p in recorded.get("pages") or []}
    if any(not set(p["missing_numbers"]) <= seen.get(p["page"], set()) for p in new.get("pages") or []):
        return False
    if not set(new.get("failed_pages") or []) <= set(recorded.get("failed_pages") or []):
        return False

    def lost(r: dict) -> int:
        t = r.get("tables") or {}
        return (t.get("before", 0) - t.get("after", 0)) if t else 0

    return lost(new) <= lost(recorded)


def _embed_reading(ctx: TenantContext, info: VersionInfo, result: ExtractionResult,
                   deadline: float) -> tuple[list[str], str]:
    """Every chunk's vector for a new reading, computed before it replaces the current one, so the swap never
    commits a chunk search cannot reach. A chunk whose text the current reading already embedded with the active
    model reuses that vector."""
    from app.providers.embeddings import get_embedding_provider, to_pgvector

    provider = get_embedding_provider()
    texts = list(dict.fromkeys(c.text for c in result.chunks))
    with tenant_tx(ctx) as conn:
        known = {r.text: r.e for r in conn.execute(text(
            "SELECT text, embedding::text AS e FROM chunks WHERE version_id = :v AND embedding IS NOT NULL"
            " AND embedding_model = :m AND text = ANY(:texts)"),
            {"v": info.id, "m": provider.model_id, "texts": texts})}
    todo = [t for t in texts if t not in known]
    for i in range(0, len(todo), EMBED_BATCH):
        check_deadline(deadline)
        batch = todo[i:i + EMBED_BATCH]
        for t, vec in zip(batch, provider.embed_passages(batch), strict=True):
            known[t] = to_pgvector(vec)
    return [known[c.text] for c in result.chunks], provider.model_id


def reindex_version(office_id: UUID, version_id: UUID, accept_regression: bool = False) -> str | None:
    """Read a processed version again under the current reader (KTD7). Extraction and embedding run before any
    write; a gate then compares the new reading with the current one (``reading_regression``). A worse reading is
    recorded on the version and ends the job as ``kept_previous`` (``ReadingRegression``, KTD9), leaving the current
    reading in place and the document available, unless an admin chose to accept the recorded regression
    (``accept_regression``) and the new one is within it (``regression_within``). A transient vision failure raises
    before the gate; the job's bounded attempts read again, regions already read coming from ``image_readings``.
    Otherwise one transaction replaces
    the blocks, pictures, tables, pages and chunks (with their embeddings) under a new ``reading_id``, re-anchors
    the version's measurements in the new reading, drops facts the earlier engine extracted from the old chunks
    and never reviewed (a reviewed fact stays), clears cached answers and moves the data version, so no answer
    built on the old reading is served again. Its records (and their reviews) are kept. Measurements are then
    re-extracted when the office allows the cloud model."""
    from app.measurements.store import measurement_anchors, reanchor_measurements
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
    embeddings = _embed_reading(ctx, info, result, deadline)
    with tenant_tx(ctx) as conn:
        current = conn.execute(text(
            "SELECT v.ingestion FROM document_versions v JOIN documents d ON d.id = v.document_id"
            " AND d.deleted_at IS NULL WHERE v.id = :v AND v.status IN ('ready', 'needs_review') FOR UPDATE OF v"),
            {"v": info.id}).first()
        if current is None:
            return None  # deleted or superseded while it was read
        regression = reading_regression(conn, info.id, result)
        recorded = (current.ingestion or {}).get("reprocess_regression")
        # an acceptance covers the regression the admin reviewed, never a different or a larger one
        if regression is not None and not (accept_regression and regression_within(regression, recorded)):
            regression["current_reading_id"] = (current.ingestion or {}).get("reading_id")
        else:
            anchors = measurement_anchors(conn, info.id)  # where each measurement sits, before the old reading goes
            for table in _DERIVED_TEXT_TABLES:
                conn.execute(text(f"DELETE FROM {table} WHERE version_id = :v"), {"v": info.id})
            records = conn.execute(text("SELECT count(*) FROM occurrences WHERE version_id = :v"),
                                   {"v": info.id}).scalar_one()
            _insert_outputs(conn, info, result, embeddings,
                            {"accepted_regression": regression} if regression is not None else None)
            anchored = reanchor_measurements(conn, info.id, anchors,
                                             ((result.fontmap or {}).get("corrections") or []))
            conn.execute(text("DELETE FROM facts WHERE version_id = :v AND status IN ('auto_validated',"
                              " 'needs_review')"), {"v": info.id})
            conn.execute(text("DELETE FROM fact_extraction_ledger WHERE version_id = :v"), {"v": info.id})
            conn.execute(text("DELETE FROM answer_cache"))
            bump_data_version(conn)
            logger.info("reindexed version %s (%d records kept; measurements %s%s)", info.id, records, anchored,
                        "; regression accepted" if regression is not None else "")
            regression = None
    if regression is not None:
        with tenant_tx(ctx) as conn:
            conn.execute(text(
                "UPDATE document_versions SET ingestion = COALESCE(ingestion, '{}'::jsonb)"
                " || jsonb_build_object('reprocess_regression', CAST(:r AS jsonb)) WHERE id = :v"),
                {"v": info.id, "r": json.dumps(regression, ensure_ascii=False)})
        logger.info("reindex of version %s kept the current reading: %s", info.id, regression_summary(regression))
        raise ReadingRegression(regression)
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


# --- positions for versions read before them (KTD3) ---------------------------------------------------------------

MSG_POSITIONS_NO_FILE = "הקובץ המקורי לא נמצא באחסון, ולכן לא נוספו לו מיקומי מקור"
_POSITIONED_STATUSES = "('ready', 'needs_review', 'superseded')"  # versions with a reading a citation may point at


def backfill_positions(office_id: UUID, version_id: UUID) -> str | None:
    """Give a version read before positions existed the positions a fresh reading stores (U3, KTD3, R13), without a
    new reading: the stored PDF's text layer is read again (no OCR, no vision, no model; font-map-repaired pages
    through the corrections recorded in its ingestion report) and aligned to the existing blocks and tables by text
    (``app.platform.positions``). One transaction writes every page's geometry and printed number, the spans of the
    blocks that aligned, the cell boxes of the tables that aligned, the ``positions`` marker and the counts of what
    aligned (``ingestion.positions_backfill``). The ``reading_id``, the ingestion version, the block numbers and
    texts stay as they are, so no citation of the version becomes stale; a block or table that does not align gets
    nothing. Idempotent: a version that has the marker is left alone, and so is one read again (a new
    ``reading_id``) while this ran. A stored file that is missing ends the job (``ExtractionError``, permanent) with
    nothing written."""
    from app.platform import positions
    from app.platform.storage import get_storage

    ctx = system_ctx(office_id)
    deadline = time.monotonic() + get_settings().job_timeout_seconds
    select = ("SELECT v.storage_key, v.mime_type, v.ingestion FROM document_versions v JOIN documents d ON"
              " d.id = v.document_id AND d.deleted_at IS NULL WHERE v.id = :v AND v.status IN "
              + _POSITIONED_STATUSES)
    with tenant_tx(ctx) as conn:
        row = conn.execute(text(select), {"v": version_id}).first()
        if row is None:
            return None  # deleted, or never read
        ing = row.ingestion or {}
        if row.mime_type != "application/pdf":
            return "skipped"
        if ing.get("positions") == POSITIONS_VERSION:
            return "present"
        pages = [positions.StoredPage(r.page_no, r.method, r.ok) for r in conn.execute(text(
            "SELECT page_no, method, ok FROM pages WHERE version_id = :v ORDER BY page_no"), {"v": version_id})]
        blocks = [positions.StoredBlock(r.block_index, r.kind, r.page, r.method, r.text or "") for r in conn.execute(
            text("SELECT block_index, kind, page, method, text FROM document_blocks WHERE version_id = :v"
                 " ORDER BY block_index"), {"v": version_id})]
        tables = [positions.StoredTable(r.table_index, r.structure or {}) for r in conn.execute(text(
            "SELECT table_index, structure FROM extracted_tables WHERE version_id = :v ORDER BY table_index"),
            {"v": version_id})]
    try:
        data = get_storage().get(row.storage_key)
    except (OSError, ValueError):
        logger.info("positions for version %s: stored file missing", version_id)
        raise ExtractionError(MSG_POSITIONS_NO_FILE, permanent=True) from None
    found = positions.read_positions(data, pages, blocks, tables, ing.get("fontmap"), get_settings(), deadline)
    texts = {b.index: b.text for b in blocks}
    with tenant_tx(ctx) as conn:
        current = conn.execute(text(select + " FOR UPDATE OF v"), {"v": version_id}).first()
        if current is None:
            return None
        now = current.ingestion or {}
        if now.get("positions") == POSITIONS_VERSION:
            return "present"
        if now.get("reading_id") != ing.get("reading_id"):
            return "reread"  # read again meanwhile: the new reading brought its own positions
        for p in found.pages:
            g = p.geometry
            conn.execute(text(
                "UPDATE pages SET mediabox = CAST(:mb AS jsonb), cropbox = CAST(:cb AS jsonb), rotation = :r,"
                " display_width = :w, display_height = :h, printed_label = :pl, geometry_issue = :gi"
                " WHERE version_id = :v AND page_no = :n"),
                {"v": version_id, "n": p.page_no,
                 "mb": json.dumps(g.mediabox) if g is not None and g.mediabox is not None else None,
                 "cb": json.dumps(g.cropbox) if g is not None and g.cropbox is not None else None,
                 "r": g.rotation if g is not None else None, "w": g.width if g is not None else None,
                 "h": g.height if g is not None else None, "pl": p.printed_label,
                 "gi": g.issue if g is not None else None})
        for index, spans in found.spans.items():
            # the text it was aligned to is still the block's text (the guard against a concurrent change)
            conn.execute(text(
                "UPDATE document_blocks SET spans = CAST(:s AS jsonb) WHERE version_id = :v AND block_index = :i"
                " AND text = :t AND spans IS NULL"),
                {"v": version_id, "i": index, "t": texts[index], "s": json.dumps(spans)})
        for index, structure in found.tables.items():
            conn.execute(text(
                "UPDATE extracted_tables SET structure = CAST(:s AS jsonb) WHERE version_id = :v AND table_index = :i"),
                {"v": version_id, "i": index, "s": json.dumps(structure, ensure_ascii=False)})
        record = found.counts | {"pages": len(found.pages), "at": datetime.now(UTC).isoformat()}
        conn.execute(text(
            "UPDATE document_versions SET ingestion = COALESCE(ingestion, '{}'::jsonb)"
            " || jsonb_build_object('positions', CAST(:p AS text), 'positions_backfill', CAST(:r AS jsonb))"
            " WHERE id = :v"),
            {"v": version_id, "p": POSITIONS_VERSION, "r": json.dumps(record)})
    logger.info("positions for version %s: blocks %s, tables %s", version_id, found.counts["blocks"],
                found.counts["tables"])
    return "positioned"
