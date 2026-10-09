"""Document upload, listing, versions, and authenticated file access (U4, U12)."""

from __future__ import annotations

import hashlib
import io
import logging
import zipfile
from pathlib import PurePath
from types import SimpleNamespace
from typing import Literal
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from fastapi.responses import JSONResponse
from sqlalchemy import Connection, text

from app.audit import audit
from app.chat import reader
from app.config import get_settings
from app.db import TenantContext, bump_data_version, tenant_tx
from app.deps import FORBIDDEN, NOT_FOUND, get_ctx, parse_uuid
from app.extraction.base import PageGeometry
from app.extraction.geometry import POSITIONS_VERSION, geometry_box
from app.extraction.render import (  # noqa: F401 - the view's scales
    PAGE_SCALE,
    PAGE_SCALES,
    REGION_SCALE,
    RENDER_MAX_SIDE,
    RenderError,
    page_max_side,
    render_page,
    render_png,
)
from app.platform.jobs import enqueue_positions, enqueue_processing, positions_job_key
from app.platform.storage import get_storage, storage_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/documents", tags=["documents"])

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

MSG_TYPE = "סוג הקובץ אינו נתמך. ניתן להעלות PDF או DOCX"
MSG_SIZE = "הקובץ גדול מהמותר ({mb}MB)"
MSG_EMPTY = "הקובץ ריק"
MSG_ZIP = "קובץ DOCX פגום או חורג ממגבלות הגודל"
MSG_BATCH = "ניתן להעלות עד {n} קבצים בבת אחת"
MSG_DUP = "הקובץ כבר הועלה למאגר"


def source_file_url(document_id, version_id, page: int | None = None) -> str:
    """Authenticated source link; the browser's PDF viewer opens it at ``#page=N``."""
    return f"/api/documents/{document_id}/versions/{version_id}/file" + (f"#page={page}" if page else "")


def latest_status_counts(conn: Connection) -> dict[str, int]:
    """Count non-deleted documents by the status of their latest version."""
    return dict(conn.execute(text(
        "SELECT status, count(*) FROM (SELECT DISTINCT ON (v.document_id) v.status FROM document_versions v"
        " JOIN documents d ON d.id = v.document_id AND d.deleted_at IS NULL"
        " ORDER BY v.document_id, v.version_no DESC) latest GROUP BY status")).all())


def _sniff(filename: str, data: bytes) -> str | None:
    """Return the mime type when extension and magic bytes agree, else None."""
    ext = PurePath(filename).suffix.lower()
    if ext == ".pdf" and b"%PDF-" in data[:1024]:
        return PDF_MIME
    if ext == ".docx" and data[:4] == b"PK\x03\x04":
        return DOCX_MIME
    return None


def _docx_within_limits(data: bytes) -> bool:
    limit = get_settings().max_docx_uncompressed_mb * 1024 * 1024
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = zf.namelist()
            total = sum(i.file_size for i in zf.infolist())
    except zipfile.BadZipFile:
        return False
    return "word/document.xml" in names and total <= limit


def _can_use_group(conn: Connection, ctx: TenantContext, group_id: UUID) -> bool:
    exists = conn.execute(text("SELECT 1 FROM document_groups WHERE id = :g"), {"g": group_id}).first()
    if not exists:
        return False
    return ctx.is_admin or group_id in ctx.group_ids


def _visible_document(conn: Connection, document_id: UUID) -> bool:
    return conn.execute(
        text("SELECT 1 FROM documents WHERE id = :d AND deleted_at IS NULL"), {"d": document_id}
    ).first() is not None


def _store_one(ctx: TenantContext, filename: str, data: bytes, group_id: UUID, document_id: UUID | None) -> dict:
    settings = get_settings()
    result: dict = {"filename": filename}
    if not data:
        return result | {"status": "rejected", "reason": MSG_EMPTY}
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        return result | {"status": "rejected", "reason": MSG_SIZE.format(mb=settings.max_upload_mb)}
    mime = _sniff(filename, data)
    if mime is None:
        return result | {"status": "rejected", "reason": MSG_TYPE}
    if mime == DOCX_MIME and not _docx_within_limits(data):
        return result | {"status": "rejected", "reason": MSG_ZIP}

    sha = hashlib.sha256(data).hexdigest()
    with tenant_tx(ctx) as conn:
        if document_id is not None:
            group_row = conn.execute(
                text("SELECT group_id FROM documents WHERE id = :d AND deleted_at IS NULL"), {"d": document_id}
            ).first()
            if group_row is None:
                return result | {"status": "rejected", "reason": NOT_FOUND}
            group_id = group_row.group_id
            if not _can_use_group(conn, ctx, group_id):
                return result | {"status": "rejected", "reason": FORBIDDEN}

        clone_from: UUID | None = None
        for hit in conn.execute(text("SELECT * FROM docs_hash_lookup(:s)"), {"s": sha}).all():
            if _visible_document(conn, hit.document_id) and (document_id is None or hit.document_id == document_id):
                return result | {"status": "duplicate", "reason": MSG_DUP, "document_id": str(hit.document_id),
                                 "version_id": str(hit.version_id)}
            if hit.status in ("ready", "needs_review"):
                clone_from = hit.version_id  # same file held where this user cannot see: reuse outputs

        key = storage_key(ctx.office_id, sha)
        get_storage().put(key, data)
        if document_id is None:
            title = PurePath(filename).stem.replace("_", " ")[:200]
            document_id = conn.execute(
                text(
                    "INSERT INTO documents (office_id, group_id, title, created_by)"
                    " VALUES (app_office(), :g, :t, :u) RETURNING id"
                ),
                {"g": group_id, "t": title, "u": ctx.user_id},
            ).scalar_one()
        version_no = conn.execute(
            text("SELECT COALESCE(max(version_no), 0) + 1 FROM document_versions WHERE document_id = :d"),
            {"d": document_id},
        ).scalar_one()
        version_id = conn.execute(
            text(
                "INSERT INTO document_versions (office_id, document_id, version_no, sha256, filename, mime_type,"
                " size_bytes, storage_key, uploaded_by, cloned_from_version_id)"
                " VALUES (app_office(), :d, :n, :s, :f, :m, :b, :k, :u, :c) RETURNING id"
            ),
            {"d": document_id, "n": version_no, "s": sha, "f": filename[:255], "m": mime, "b": len(data), "k": key,
             "u": ctx.user_id, "c": clone_from},
        ).scalar_one()
        enqueue_processing(conn, version_id)
        audit(conn, "upload", ctx.user_id, "document_version", version_id, document_id=document_id,
              version_no=version_no, size=len(data))
    return result | {"status": "accepted", "document_id": str(document_id), "version_id": str(version_id)}


@router.post("")
def upload(
    files: list[UploadFile] = File(...),
    group_id: str | None = Form(None),
    document_id: str | None = Form(None),
    ctx: TenantContext = Depends(get_ctx),
) -> dict:
    settings = get_settings()
    if not ctx.can_upload:
        raise HTTPException(status.HTTP_403_FORBIDDEN, FORBIDDEN)
    if len(files) > settings.max_batch_files:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, MSG_BATCH.format(n=settings.max_batch_files))
    doc_uuid = parse_uuid(document_id) if document_id else None
    if doc_uuid is None:
        if not group_id:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "יש לבחור קבוצת מסמכים")
        group_uuid = parse_uuid(group_id)
        with tenant_tx(ctx) as conn:
            if not _can_use_group(conn, ctx, group_uuid):
                raise HTTPException(status.HTTP_403_FORBIDDEN, FORBIDDEN)
    else:
        group_uuid = UUID(int=0)  # replaced by the document's own group

    results = []
    limit = settings.max_upload_mb * 1024 * 1024
    for f in files:
        data = f.file.read(limit + 1)
        results.append(_store_one(ctx, f.filename or "file", data, group_uuid, doc_uuid))
    return {"results": results}


_PLAIN_COLS = (
    "v.id, v.version_no, v.filename, v.status, v.status_reason, v.page_count, v.pages_incomplete,"
    " v.records_total, v.records_needing_review, v.is_current, v.created_at, v.processed_at, v.mime_type,"
    " v.ingestion"
)
# What was read of a version, counted where it is stored (see ``_reading``).
_READING_COLS = (
    "passages", "tables_count", "measurements_count", "measurements_state",
)
_VERSION_COLS = (
    _PLAIN_COLS + ","
    " (SELECT count(*) FROM chunks c WHERE c.version_id = v.id AND c.kind <> 'table_row') AS passages,"
    " (SELECT count(*) FROM extracted_tables t WHERE t.version_id = v.id) AS tables_count,"
    " (SELECT count(*) FROM measurements m WHERE m.version_id = v.id AND m.status <> 'rejected') AS measurements_count,"
    " (SELECT r.state FROM measurement_runs r WHERE r.version_id = v.id ORDER BY r.updated_at DESC LIMIT 1)"
    " AS measurements_state"
)


def _version_json(row) -> dict:
    return {
        "id": str(row.id), "version_no": row.version_no, "filename": row.filename, "status": row.status,
        "status_reason": row.status_reason, "page_count": row.page_count, "pages_incomplete": row.pages_incomplete,
        "records_total": row.records_total, "records_needing_review": row.records_needing_review,
        "is_current": row.is_current, "created_at": row.created_at.isoformat(),
        "processed_at": row.processed_at.isoformat() if row.processed_at else None, "mime_type": row.mime_type,
        "reading": _reading(row),
    }


def coverage_of(ing: dict) -> list[dict]:
    """The reading report's per-page coverage (``ExtractionResult.coverage``). A report written before coverage
    was recorded lists its unread pictures, grouped by page, without block or box."""
    if "coverage" in ing:
        return ing["coverage"] or []
    pages: dict = {}
    for u in ing.get("unread") or []:
        e = pages.setdefault(u.get("page"), {"page": u.get("page"), "ok": True, "method": None, "corrected": 0,
                                             "regions": []})
        e["regions"].append({"block": None, "kind": "image", "status": "unread", "reason": u.get("reason"),
                             "bbox": None, "section": u.get("section"), "media": u.get("media")})
    return sorted(pages.values(), key=lambda e: (e["page"] is not None, e["page"] or 0))


def _pages_text(pages: list) -> str:
    shown = sorted({p for p in pages if p is not None})
    if not shown:
        return ""
    listed = ", ".join(str(p) for p in shown[:8]) + (", …" if len(shown) > 8 else "")
    return f" בעמוד {listed}" if len(shown) == 1 else f" בעמודים {listed}"


def reading_notes(ing: dict, pages_incomplete: int | None) -> tuple[bool, list[str]]:
    """A version's reading in a few Hebrew words for the model: whether it was only partly read, and what was not
    read, read uncertainly or corrected, with the pages."""
    coverage = coverage_of(ing)
    unread = [(e["page"], r) for e in coverage for r in e["regions"] if r["status"] == "unread"]
    uncertain = [(e["page"], r) for e in coverage for r in e["regions"] if r["status"] == "read_uncertain"]
    failed = [e["page"] for e in coverage if not e["ok"]]
    notes = []
    if unread:
        notes.append(f"{len(unread)} אזורים לא נקראו" + _pages_text([p for p, _ in unread]))
    if uncertain:
        notes.append(f"{len(uncertain)} אזורים נקראו בקריאה לא ודאית" + _pages_text([p for p, _ in uncertain]))
    if failed or pages_incomplete:
        notes.append(f"{len(failed) or pages_incomplete} עמודים לא נקראו" + _pages_text(failed))
    corrected = ing.get("corrected_blocks") or 0
    if corrected:
        notes.append(f"טקסט תוקן ממיפוי גופן פגום ב-{corrected} קטעים"
                     + _pages_text([e["page"] for e in coverage if e["corrected"]]))
    repeated = len(ing.get("repeated_images") or [])
    if repeated:
        notes.append(f"{repeated} תמונות חוזרות נקראו פעם אחת")
    return bool(ing.get("partial")) or bool(pages_incomplete), notes


def _reading(row) -> dict:
    """What was read of a version, kept apart: searchable passages, tables, stored measurements, structured
    records, and pictures by status. ``partial`` when any picture or page was not read: zero structured records
    is not zero searchable content, and a document is never shown as fully read while parts were not.
    ``coverage`` names each page with an unread or uncertain region (with its reason) or with corrected text;
    ``corrected_blocks`` counts the blocks whose text is a verified font-map correction (``corrections`` the
    accepted mappings), ``uncertain_blocks`` the text left uncertain, and ``repeated_images`` the pictures
    repeated through the document and read once."""
    ing = getattr(row, "ingestion", None) or {}
    images = ing.get("images") or {}
    return {
        "passages": getattr(row, "passages", 0) or 0, "tables": getattr(row, "tables_count", 0) or 0,
        "measurements": getattr(row, "measurements_count", 0) or 0,
        "measurements_state": getattr(row, "measurements_state", None),
        "images_total": sum(images.values()), "images": images, "unread": ing.get("unread") or [],
        "partial": bool(ing.get("partial")) or bool(row.pages_incomplete),
        "coverage": coverage_of(ing),
        "uncertain_blocks": sum(u.get("blocks", 0) for u in ing.get("uncertain") or []),
        "corrected_blocks": ing.get("corrected_blocks") or 0,
        "corrections": len((ing.get("fontmap") or {}).get("corrections") or []),
        "repeated_images": len(ing.get("repeated_images") or []),
        "ingestion_version": ing.get("ingestion_version"),
        # a reprocess that kept the previous reading ({reason, attempts, at}), or None (U12)
        "kept_previous": ing.get("reprocess_kept"),
    }


def _list_documents(conn: Connection, ctx: TenantContext, q: str | None, status_filter: str | None,
                    document_id: UUID | None = None) -> list[dict]:
    """One query: documents with their latest version (LATERAL), filtered in SQL."""
    names = [c.split(".")[1] for c in _PLAIN_COLS.split(", ")] + list(_READING_COLS)
    latest_cols = ", ".join(f"lv.{n} AS lv_{n}" for n in names)
    rows = conn.execute(
        text(
            "SELECT d.id, d.title, d.created_at, d.deleted_at, g.id AS group_id, g.name AS group_name,"
            " (SELECT count(*) FROM document_versions vc WHERE vc.document_id = d.id) AS versions_count,"
            f" {latest_cols}"
            " FROM documents d JOIN document_groups g ON g.id = d.group_id"
            f" LEFT JOIN LATERAL (SELECT {_VERSION_COLS} FROM document_versions v WHERE v.document_id = d.id"
            "   ORDER BY v.version_no DESC LIMIT 1) lv ON true"
            " WHERE (:admin OR d.deleted_at IS NULL)"
            " AND (CAST(:q AS text) IS NULL OR d.title ILIKE '%' || CAST(:q AS text) || '%')"
            " AND (CAST(:doc AS uuid) IS NULL OR d.id = CAST(:doc AS uuid))"
            " AND (CAST(:st AS text) IS NULL OR lv.status = CAST(:st AS text))"
            " ORDER BY d.created_at DESC"
        ),
        {"admin": ctx.is_admin, "q": q or None, "doc": document_id, "st": status_filter or None},
    ).all()
    out = []
    for r in rows:
        m = r._mapping
        ver = SimpleNamespace(**{k[3:]: v for k, v in m.items() if k.startswith("lv_")}) if m["lv_id"] else None
        out.append({
            "id": str(r.id), "title": r.title, "group": {"id": str(r.group_id), "name": r.group_name},
            "deleted": r.deleted_at is not None, "created_at": r.created_at.isoformat(),
            "current_version": _version_json(ver) if ver else None, "versions_count": r.versions_count,
        })
    return out


@router.get("")
def list_documents(q: str | None = None, status_filter: str | None = Query(None, alias="status"),
                   ctx: TenantContext = Depends(get_ctx)):
    with tenant_tx(ctx) as conn:
        return {"documents": _list_documents(conn, ctx, q, status_filter)}


@router.get("/{document_id}")
def get_document(document_id: str, ctx: TenantContext = Depends(get_ctx)) -> dict:
    doc_uuid = parse_uuid(document_id)
    with tenant_tx(ctx) as conn:
        docs = _list_documents(conn, ctx, None, None, doc_uuid)
        if not docs:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        versions = conn.execute(
            text(f"SELECT {_VERSION_COLS} FROM document_versions v WHERE v.document_id = :d ORDER BY v.version_no DESC"),
            {"d": doc_uuid},
        ).all()
    return docs[0] | {"versions": [_version_json(v) for v in versions]}


@router.delete("/{document_id}")
def delete_document(document_id: str, ctx: TenantContext = Depends(get_ctx)) -> dict:
    doc_uuid = parse_uuid(document_id)
    with tenant_tx(ctx) as conn:
        row = conn.execute(
            text("SELECT created_by FROM documents WHERE id = :d AND deleted_at IS NULL"), {"d": doc_uuid}
        ).first()
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        if not (ctx.role == "admin" or (ctx.can_upload and row.created_by == ctx.user_id)):
            raise HTTPException(status.HTTP_403_FORBIDDEN, FORBIDDEN)
        conn.execute(
            text("UPDATE documents SET deleted_at = now(), deleted_by = :u WHERE id = :d"),
            {"u": ctx.user_id, "d": doc_uuid},
        )
        bump_data_version(conn)
        audit(conn, "document_delete", ctx.user_id, "document", doc_uuid)
    return {"ok": True}


# --- typed source states ---------------------------------------------------------------------------------------
# A version the user may not see is the uniform 404 (``NOT_FOUND``), whatever the reason: deleted, another group or
# office, or never there. Once the version is visible, a failure to show it is typed (KTD5, R12), so the viewer can
# fall back to the extracted text instead of treating it as lost access: the body is ``{"detail": <Hebrew message>,
# "state": <state>}`` and the state is repeated in the ``X-Source-State`` header.

STATE_STALE = "stale"  # the anchor's reading is no longer the stored one: its block number now means another block
STATE_FILE_MISSING = "file_missing"  # the version's file is not in storage
STATE_RENDER_FAILED = "render_failed"  # the file is there, but the page or region cannot be drawn from it
MSG_STALE = "המיקום המצוטט שייך לקריאה קודמת של המסמך"
MSG_FILE_MISSING = "הקובץ המקורי אינו זמין כעת"
MSG_RENDER_FAILED = "לא ניתן להציג את העמוד מהקובץ המקורי"
_FAILURES = {
    STATE_STALE: (status.HTTP_409_CONFLICT, MSG_STALE),
    STATE_FILE_MISSING: (status.HTTP_422_UNPROCESSABLE_CONTENT, MSG_FILE_MISSING),
    STATE_RENDER_FAILED: (status.HTTP_422_UNPROCESSABLE_CONTENT, MSG_RENDER_FAILED),
}
# a source is never kept by the browser or a proxy: access is checked again on every view (R12)
_NO_STORE = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


class SourceFailure(Exception):
    """A visible version that cannot be shown: answered with its typed state, never as not found."""

    def __init__(self, state: str):
        super().__init__(state)
        self.state = state

    def response(self) -> JSONResponse:
        code, message = _FAILURES[self.state]
        return JSONResponse({"detail": message, "state": self.state}, status_code=code,
                            headers=_NO_STORE | {"X-Source-State": self.state})


def _stored_file(v: reader.Version) -> bytes:
    """The version's original file; a file gone from storage is a typed failure (the version is visible)."""
    try:
        return get_storage().get(v.storage_key)
    except (OSError, ValueError):
        logger.warning("stored file of version %s is missing", v.version_id)
        raise SourceFailure(STATE_FILE_MISSING) from None


@router.get("/{document_id}/versions/{version_id}/file")
def get_file(document_id: str, version_id: str, ctx: TenantContext = Depends(get_ctx)) -> Response:
    """The version's original file, through the same visibility check as its blocks, pages and regions (an old
    conversation's source included: access is the user's access now, not when the answer was given)."""
    doc_uuid, ver_uuid = parse_uuid(document_id), parse_uuid(version_id)
    with tenant_tx(ctx) as conn:
        v = _version_row(conn, doc_uuid, ver_uuid)
        audit(conn, "source_view", ctx.user_id, "document_version", ver_uuid)
    try:
        data = _stored_file(v)
    except SourceFailure as failure:
        return failure.response()
    disposition = "inline" if v.mime_type == PDF_MIME else "attachment"
    return Response(
        content=data,
        media_type=v.mime_type,
        headers=_NO_STORE | {"Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(v.filename)}"},
    )


# --- source view: a version's blocks and its pictures ----------------------------------------------------------

BLOCKS_MAX = 400
_MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "bmp": "image/bmp"}


def _version_row(conn: Connection, doc_uuid: UUID, ver_uuid: UUID):
    """The version through the reader (``app.chat.reader``): visible, of this document, not deleted; else 404."""
    row = reader.version(conn, ver_uuid, doc_uuid)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return row


NO_READING = "none"  # the cited reading predates reading ids


def _same_reading(reading_id: str, stored: str | None) -> bool:
    """Whether the reading a citation names (``none`` for one made before readings had ids) is the stored one."""
    return (None if reading_id == NO_READING else reading_id) == stored


@router.get("/{document_id}/versions/{version_id}/blocks")
def get_blocks(document_id: str, version_id: str, start: int | None = Query(None, ge=0),
               end: int | None = Query(None, ge=0), reading_id: str | None = Query(None, max_length=64),
               ctx: TenantContext = Depends(get_ctx)) -> dict:
    """The version's blocks in reading order (a window when ``start``/``end`` are given), with each table's
    structure: what the source viewer shows around a cited location. ``reading_id``: the reading the citation was
    made from (``none`` for one made before readings had ids). When the version has been read again since, block
    numbers no longer mean what the citation meant: the answer is ``stale`` with no blocks (KTD7)."""
    doc_uuid, ver_uuid = parse_uuid(document_id), parse_uuid(version_id)
    with tenant_tx(ctx) as conn:
        v = _version_row(conn, doc_uuid, ver_uuid)
        if reading_id is not None and not _same_reading(reading_id, v.reading_id):
            return {"document_id": document_id, "version_id": version_id, "title": v.title,
                    "is_current": v.is_current, "mime_type": v.mime_type, "total": 0, "blocks": [],
                    "reading_id": v.reading_id, "stale": True,
                    "file_url": f"/api/documents/{document_id}/versions/{version_id}/file"}
        # the same reader the agent's ``read`` uses, so a citation opens exactly what the model read (KTD8)
        rows = reader.blocks_between(conn, ver_uuid, start, end, BLOCKS_MAX)
        tables = reader.tables_of(conn, ver_uuid, {r.table_index for r in rows if r.table_index is not None})
        total = reader.block_count(conn, ver_uuid)
        audit(conn, "source_view", ctx.user_id, "document_version", ver_uuid)
    blocks = []
    for r in rows:
        b = {"index": r.block_index, "kind": r.kind, "section": r.section, "section_path": list(r.section_path or []),
             "paragraph_no": r.paragraph_no, "page": r.page, "media": r.media, "source": r.source, "status": r.status,
             "note": r.note, "text": r.text, "bbox": r.bbox, "method": r.method, "reader_version": r.reader_version,
             "content_hash": r.content_hash, "original_text": r.original_text}
        if r.table_index is not None and r.table_index in tables:
            st = tables[r.table_index] or {}
            b["table"] = {k: st.get(k) for k in ("headers", "caption", "title", "notes", "source", "media")} | {
                "rows": [x.get("cells") for x in st.get("rows") or []]}
        if r.media and r.media.rsplit(".", 1)[-1].lower() in _MEDIA_TYPES:
            b["media_url"] = f"/api/documents/{document_id}/versions/{version_id}/media/{quote(r.media)}"
        if v.mime_type == PDF_MIME and r.page:
            b["page_url"] = f"/api/documents/{document_id}/versions/{version_id}/pages/{r.page}/image"
            if r.bbox:  # the region of this reading: refused as stale once the version is read again
                b["region_url"] = (f"/api/documents/{document_id}/versions/{version_id}/regions/{r.block_index}/image"
                                   f"?reading_id={quote(v.reading_id or NO_READING, safe='')}")
        blocks.append(b)
    return {"document_id": document_id, "version_id": version_id, "title": v.title, "is_current": v.is_current,
            "mime_type": v.mime_type, "total": total, "blocks": blocks, "reading_id": v.reading_id, "stale": False,
            "file_url": f"/api/documents/{document_id}/versions/{version_id}/file"}


@router.get("/{document_id}/versions/{version_id}/media/{name}")
def get_media(document_id: str, version_id: str, name: str, ctx: TenantContext = Depends(get_ctx)) -> Response:
    """One raster picture from inside a DOCX, by the media name a block of this version names (nothing else in
    the package is reachable)."""
    doc_uuid, ver_uuid = parse_uuid(document_id), parse_uuid(version_id)
    ext = name.rsplit(".", 1)[-1].lower()
    if ext not in _MEDIA_TYPES or "/" in name or "\\" in name:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    with tenant_tx(ctx) as conn:
        v = _version_row(conn, doc_uuid, ver_uuid)
        known = conn.execute(text("SELECT 1 FROM document_blocks WHERE version_id = :v AND media = :m LIMIT 1"),
                             {"v": ver_uuid, "m": name}).first()
    if known is None or v.mime_type != DOCX_MIME:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    try:
        with zipfile.ZipFile(io.BytesIO(get_storage().get(v.storage_key))) as zf:
            data = zf.read(f"word/media/{name}")
    except (KeyError, zipfile.BadZipFile):
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND) from None
    return Response(content=data, media_type=_MEDIA_TYPES[ext],
                    headers={"Cache-Control": "private, max-age=600", "X-Content-Type-Options": "nosniff"})


# --- PDF page and region view ------------------------------------------------------------------------------------
# The source viewer draws an anchor's snapshot rectangles (fractions of the page's display frame) over the page image
# (KTD5). The page image belongs to the file and never changes, so it is served for any visible version, whatever
# reading the anchor came from; a region is cut by a block's stored box, so it is served only for the reading the
# anchor names. A DOCX has no pages: no page or region image is invented for it.

READING_CURRENT = "current"


def _number(value: str) -> int:
    """A page number or block index from the path; anything else is not found."""
    if not (value.isascii() and value.isdigit()) or len(value) > 6:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return int(value)


def _points(value: float) -> str:
    return str(round(float(value), 2)).removesuffix(".0")


def _image_response(data: bytes, headers: dict | None = None) -> Response:
    return Response(content=data, media_type="image/png", headers=_NO_STORE | (headers or {}))


_POSITIONS_STATUSES = ("ready", "needs_review")  # read, with a reading the backfill can give positions to
_POSITIONS_PENDING = ("queued", "running", "failed")  # on its way, or failed for good (a missing file)


def _queue_positions(conn: Connection, v: reader.Version) -> None:
    """Opening a page of a current PDF read before positions queues its geometry-only backfill (U3, KTD3), so its
    later citations get boxes; the view never waits for it and never fails because of it. A queued or running job is
    left alone, and a failed one is not queued again on every view (the admin's bulk backfill retries it)."""
    if not v.is_current or not v.is_pdf:
        return
    try:
        with conn.begin_nested():
            row = conn.execute(text(
                "SELECT v.status, v.ingestion->>'positions' AS positions,"
                " (SELECT j.status FROM jobs j WHERE j.idempotency_key = :k) AS job"
                " FROM document_versions v WHERE v.id = :v"),
                {"v": v.version_id, "k": positions_job_key(v.version_id)}).first()
            if (row is not None and row.status in _POSITIONS_STATUSES and row.positions != POSITIONS_VERSION
                    and row.job not in _POSITIONS_PENDING):
                enqueue_positions(conn, v.version_id)
    except Exception:  # noqa: BLE001 - the backfill is a convenience: the page is shown regardless
        logger.warning("positions backfill of version %s not queued", v.version_id, exc_info=True)


@router.get("/{document_id}/versions/{version_id}/pages/{page_no}/image")
def get_page_image(document_id: str, version_id: str, page_no: str,
                   scale: Literal["normal", "zoom"] = Query("normal"),
                   reading_id: str | None = Query(None, max_length=64),
                   ctx: TenantContext = Depends(get_ctx)) -> Response:
    """One page of a PDF version as an image: only a page number within the version's page count. ``scale``: the
    viewer's tier (``PAGE_SCALES``), capped so that a whole ordinary page reaches it. Headers: ``X-Display-Width``
    and ``X-Display-Height`` (the page's display frame in points, what the anchor's rectangles are fractions of),
    ``X-Render-Scale`` (the scale applied) and ``X-Scale-Tier``; with ``reading_id`` (the anchor's reading),
    ``X-Reading-State`` says whether it is still the stored reading (``current``) or not (``stale``) — the page is
    shown either way, with the anchor's snapshot highlight."""
    doc_uuid, ver_uuid = parse_uuid(document_id), parse_uuid(version_id)
    number = _number(page_no)
    with tenant_tx(ctx) as conn:
        v = _version_row(conn, doc_uuid, ver_uuid)
        if v.mime_type != PDF_MIME or not v.page_count or not 1 <= number <= v.page_count:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        audit(conn, "source_view", ctx.user_id, "document_version", ver_uuid, page=number, scale=scale)
        _queue_positions(conn, v)
    nominal = PAGE_SCALES[scale]
    try:
        page = render_page(_stored_file(v), number, scale=nominal, max_side=page_max_side(nominal))
    except SourceFailure as failure:
        return failure.response()
    except RenderError:
        return SourceFailure(STATE_RENDER_FAILED).response()
    headers = {"X-Display-Width": _points(page.width), "X-Display-Height": _points(page.height),
               "X-Render-Scale": str(round(page.scale, 4)), "X-Scale-Tier": scale}
    if reading_id is not None:
        headers["X-Reading-State"] = READING_CURRENT if _same_reading(reading_id, v.reading_id) else STATE_STALE
    return _image_response(page.png, headers)


def display_box_of(conn: Connection, version_id, page_no: int, bbox) -> list[float]:
    """A block's stored box on ``page_no`` of a version in the frame of the rendered page (what ``render_png``
    crops), from that page's stored geometry; the region view and the chat's visual reading crop the same place."""
    page = conn.execute(text(
        "SELECT mediabox, cropbox, rotation, display_width, display_height, geometry_issue FROM pages"
        " WHERE version_id = :v AND page_no = :n"), {"v": version_id, "n": page_no}).first()
    return _display_box(bbox, page)


def _display_box(bbox, page) -> list[float]:
    """A block's stored box (the text reader's frame) in the frame of the rendered page, with the page's stored
    geometry (``app.extraction.geometry``); the stored box itself for a page without usable geometry (read before
    positions, or one whose positions cannot be converted), as before positions existed."""
    box = [float(x) for x in bbox]
    if page is None or page.mediabox is None or page.rotation is None or page.geometry_issue:
        return box
    geom = PageGeometry(page.mediabox, page.cropbox, page.rotation, page.display_width, page.display_height,
                        page.geometry_issue)
    return geometry_box(box, geom) or box


@router.get("/{document_id}/versions/{version_id}/regions/{block_index}/image")
def get_region_image(document_id: str, version_id: str, block_index: str,
                     reading_id: str = Query(..., max_length=64),
                     ctx: TenantContext = Depends(get_ctx)) -> Response:
    """A region of a PDF version as an image: only a block of this version stored with its page and box, and only
    for the reading the anchor names (``reading_id``, ``none`` for one made before readings had ids). When the
    version was read again since, the block number names another block: the answer is the typed ``stale`` state,
    never the new reading's crop (R11)."""
    doc_uuid, ver_uuid = parse_uuid(document_id), parse_uuid(version_id)
    index = _number(block_index)
    with tenant_tx(ctx) as conn:
        v = _version_row(conn, doc_uuid, ver_uuid)
        if v.mime_type != PDF_MIME:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        if not _same_reading(reading_id, v.reading_id):
            return SourceFailure(STATE_STALE).response()
        block = conn.execute(text("SELECT page, bbox FROM document_blocks WHERE version_id = :v AND block_index = :i"),
                             {"v": ver_uuid, "i": index}).first()
        if block is None or not block.page or not block.bbox or len(block.bbox) != 4:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        crop = display_box_of(conn, ver_uuid, block.page, block.bbox)
        audit(conn, "source_view", ctx.user_id, "document_version", ver_uuid, block=index)
        _queue_positions(conn, v)
    try:
        return _image_response(render_png(_stored_file(v), block.page, crop))
    except SourceFailure as failure:
        return failure.response()
    except RenderError:
        return SourceFailure(STATE_RENDER_FAILED).response()
